"""OpenAI 兼容接口的 Provider 实现。

面向所有遵循 OpenAI Chat Completions 协议的服务：DeepSeek、Kimi、Qwen、
以及本地跑的 vLLM / Ollama 等，靠 base_url 区分，代码不用改。

当前项目主力接 DeepSeek，所以有两处是专门为它准备的：

1. **reasoning_content 的取用与保留**：DeepSeek 的思考模式在返回 tool_calls 时
   会附带推理过程，且要求下一轮请求把它原样带回，否则直接返回 400。取用位置
   各家不一——DeepSeek 放在 tool_call 上，有些模型放在 message 顶层，
   这里两处都查，具体见 _extract_tool_calls。
2. **extra_body 透传**：DeepSeek 的思考模式开关、缓存控制等参数走 extra_body，
   由构造参数透传下去，不必为了新参数改这个文件。

异常边界按 providers.base 的约定执行：调用层面的失败（网络、鉴权、限流）不向上抛，
转成 finish_reason="error" 的响应，让 agent 循环能统一处理。
"""

import json
import logging
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)

from providers.base import LLMProvider, LLMResponse, ToolCallRequest

logger = logging.getLogger(__name__)


class OpenAICompatProvider(LLMProvider):
    """通过 OpenAI SDK 调用任意 OpenAI 兼容接口的 Provider。

    职责是把 SDK 的返回结构翻译成本项目的统一响应：tool_calls 拍平成
    ToolCallRequest，usage 提出来，异常兜成错误响应。

    这个类不做重试——AsyncOpenAI 自带 max_retries（默认 2 次），对限流和
    瞬时网络抖动已经够用；再往上的重试策略（比如降级到别的模型）属于 agent
    循环的决策，放在这里会藏得太深。

    实例可以复用，内部 AsyncOpenAI 客户端自带连接池，整个进程建一个即可。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 120.0,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        """构造 Provider。

        Args:
            api_key: 服务商 API key。
            base_url: 接口地址，如 DeepSeek 的 https://api.deepseek.com/v1。
            model: 默认模型名，chat() 未指定 model 时使用。
            timeout: 单次请求超时（秒）。SDK 默认是 600 秒，对交互式 agent 太长，
                思考模型又比普通模型慢，这里折中取 120 秒。
            extra_body: 透传给接口的额外请求体参数，如 DeepSeek 思考模式开关
                ``{"thinking": {"type": "enabled"}}``。为 None 时不传。
        """
        self.model = model
        self._extra_body = extra_body
        # 客户端持有关键资源（连接池），建好后就不要再改。
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        """调用接口并归一化响应，调用失败时返回 finish_reason="error" 的响应。"""
        params: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
        }
        # 空列表也视作"本轮不给工具"：有些服务收到 tools=[] 会直接报参数错误，
        # 所以宁可不传这个字段。
        if tools:
            params["tools"] = tools
        if self._extra_body:
            params["extra_body"] = self._extra_body

        try:
            response = await self._client.chat.completions.create(**params)
        except Exception as exc:
            # 只捕获 Exception：CancelledError / KeyboardInterrupt 继承自
            # BaseException，必须继续向上传播，否则 Ctrl+C 会失去响应。
            logger.error("模型调用失败: %s", exc, exc_info=True)
            return LLMResponse(content=f"错误：{_describe_error(exc)}", finish_reason="error")

        if not response.choices:
            logger.error("模型返回了空的 choices 列表")
            return LLMResponse(content="错误：模型返回了空的结果列表", finish_reason="error")

        choice = response.choices[0]
        message = choice.message
        # 部分思考模型在工具调用轮次返回 content=""，统一归一成 None，
        # 避免上层把空字符串当成"有文本回复"打印出一行空白。
        content = message.content or None
        # message 顶层的推理过程，作为 tool_call 身上没有该字段时的回退值。
        message_reasoning = getattr(message, "reasoning_content", None)

        return LLMResponse(
            content=content,
            tool_calls=_extract_tool_calls(message.tool_calls, message_reasoning),
            finish_reason=choice.finish_reason or "stop",
            usage=_extract_usage(getattr(response, "usage", None)),
        )


def _extract_tool_calls(
    raw_tool_calls: Any,
    message_reasoning: str | None,
) -> list[ToolCallRequest]:
    """把 SDK 的 tool_calls 转成 ToolCallRequest 列表。

    Args:
        raw_tool_calls: SDK 返回的 tool_calls，可能为 None。
        message_reasoning: message 顶层的 reasoning_content，用于回退。

    Returns:
        转换后的列表。arguments 解析失败时该项为 {}，不抛异常。
    """
    if not raw_tool_calls:
        return []

    result: list[ToolCallRequest] = []
    for tc in raw_tool_calls:
        function = getattr(tc, "function", None)
        raw_arguments = getattr(function, "arguments", None) if function else None
        name = getattr(function, "name", None) if function else None

        # 推理过程的两个来源：优先取 tool_call 自带的（DeepSeek 这类），
        # 没有再用 message 顶层的（Kimi 这类）。丢了它下一轮请求会 400。
        reasoning = getattr(tc, "reasoning_content", None) or message_reasoning

        result.append(
            ToolCallRequest(
                id=getattr(tc, "id", None) or "",
                name=name or "",
                arguments=_parse_arguments(raw_arguments, name),
                reasoning_content=reasoning,
            )
        )
    return result


def _parse_arguments(raw: Any, tool_name: str | None) -> dict[str, Any]:
    """把 arguments 解析成 dict。

    模型给出的 arguments 是 JSON 字符串，且确实可能给出非法 JSON（多吐了引号、
    被 max_tokens 截断等）。这里不抛异常而是返回 {}：工具层随后会报"缺少参数"
    并把它回传给模型，模型通常能自我纠正重发；直接抛异常则会炸掉整轮对话。
    """
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("工具 %s 的参数不是合法 JSON（%s），已按空参数处理: %r", tool_name, exc, raw)
        return {}
    if not isinstance(parsed, dict):
        logger.warning("工具 %s 的参数不是 JSON 对象，已按空参数处理: %r", tool_name, parsed)
        return {}
    return parsed


def _extract_usage(usage: Any) -> dict[str, Any]:
    """把 SDK 的 usage 对象转成 dict，取不到时返回空字典。

    不做字段归一化——各家除了标准的 prompt/completion tokens，还会带自己的
    扩展字段（如 DeepSeek 的 prompt_cache_hit_tokens），原样保留便于排查成本。
    """
    if usage is None:
        return {}
    # pydantic v2 模型用 model_dump，旧版 SDK 用 to_dict，两者都没有时退化成 dict()。
    for method in ("model_dump", "to_dict"):
        func = getattr(usage, method, None)
        if callable(func):
            try:
                return func()
            except Exception:  # noqa: BLE001 - 转 dict 失败不该影响主流程
                logger.debug("usage.%s() 调用失败，尝试下一种方式", method)
    try:
        return dict(usage)
    except Exception:  # noqa: BLE001
        logger.warning("无法解析 usage 字段: %r", usage)
        return {}


def _describe_error(exc: Exception) -> str:
    """把异常翻译成给模型和用户看的可读描述。

    分类只是为了给出可操作的提示——agent 循环拿到的是文本，如果这里只回一段
    SDK 原始报错，排查时还得再去猜是哪类问题。
    """
    detail = str(exc).strip() or type(exc).__name__

    # APITimeoutError 是 APIConnectionError 的子类，必须先判超时。
    if isinstance(exc, APITimeoutError):
        return f"请求超时，模型未在限定时间内返回（{detail}）"
    if isinstance(exc, APIConnectionError):
        return f"无法连接到模型服务，请检查 base_url 与网络（{detail}）"
    if isinstance(exc, AuthenticationError):
        return f"鉴权失败，请检查 api_key（{detail}）"
    if isinstance(exc, RateLimitError):
        return f"触发限流或余额不足，请稍后重试（{detail}）"
    if isinstance(exc, BadRequestError):
        # 参数不被接受，常见于上下文超长、模型名错误、思考模式回传格式不对。
        return f"请求被服务端拒绝，请检查模型名与上下文长度（{detail}）"
    if isinstance(exc, APIStatusError):
        return f"服务端返回错误状态 {exc.status_code}（{detail}）"
    return f"调用模型失败 - {type(exc).__name__}: {detail}"
