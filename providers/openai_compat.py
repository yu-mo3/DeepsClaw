"""OpenAI 兼容接口的 Provider 实现。

面向所有遵循 OpenAI Chat Completions 协议的服务：DeepSeek、Kimi、Qwen、
以及本地跑的 vLLM / Ollama 等，靠 base_url 区分，代码不用改。

**一律以流式方式请求**，再自己把分片累积成完整的 LLMResponse。这样做不是为了
"顺手支持流式"，而是为了让流式与非流式只有一套解析逻辑：正文怎么拼、思考怎么留、
tool_calls 怎么归并，全都只有一处实现，不会出现"带回调和不带回调结果不一致"
这类只在边角发作的 bug。用户配了思考模式时，流式也是唯一能把思考实时吐出来的方式。

三处是专门为思考模型（本项目主力 DeepSeek）准备的：

1. **reasoning_content 的取用与保留**：思考模式下模型会附带推理过程，其中**工具
   调用轮次的推理必须原样回传**，否则下一轮请求直接 400。非流式响应里它挂在
   message 顶层或 tool_call 上，流式响应里则作为一个普通的文本增量先于 tool_calls
   到达——后者拿不到"这段推理属于哪次调用"的归属信息，所以统一收进
   LLMResponse.reasoning_content，同时给每个 ToolCallRequest 也挂一份保持旧形状。
2. **extra_body 透传**：思考模式开关、缓存控制等参数走 extra_body，由构造参数
   透传下去，不必为了新参数改这个文件。**stream / stream_options 例外**，这两个键
   由本模块掌管，见 _build_extra_body。
3. **用量统计**：流式下 usage 需要显式开关（stream_options）才会下发，个别老版本
   兼容服务不认这个参数会直接 400，chat() 遇到时会去掉它重试一次。

异常边界按 providers.base 的约定执行：调用层面的失败（网络、鉴权、限流、中途断流）
不向上抛，转成 finish_reason="error" 的响应，让 agent 循环能统一处理。
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

from providers.base import LLMProvider, LLMResponse, OnDelta, StreamDelta, ToolCallRequest

logger = logging.getLogger(__name__)


class OpenAICompatProvider(LLMProvider):
    """通过 OpenAI SDK 调用任意 OpenAI 兼容接口的 Provider。

    职责是把 SDK 的流式分片翻译成本项目的统一响应：正文与思考按序拼接，
    tool_calls 按 index 归并，usage 提出来，异常兜成错误响应。

    这个类不做重试——AsyncOpenAI 自带 max_retries（默认 2 次），对限流和
    瞬时网络抖动已经够用；再往上的重试策略（比如降级到别的模型）属于 agent
    循环的决策，放在这里会藏得太深。唯一的例外是 stream_options 被服务端拒绝
    时的降级重试，那是协议兼容问题，只有这一层能修。

    实例可以复用，内部 AsyncOpenAI 客户端自带连接池，整个进程建一个即可。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 120.0,
        extra_body: dict[str, Any] | None = None,
        include_usage: bool = True,
    ) -> None:
        """构造 Provider。

        Args:
            api_key: 服务商 API key。
            base_url: 接口地址，如 DeepSeek 的 https://api.deepseek.com/v1。
            model: 默认模型名，chat() 未指定 model 时使用。
            timeout: 单次请求超时（秒）。SDK 默认是 600 秒，对交互式 agent 太长，
                思考模型又比普通模型慢，这里折中取 120 秒。注意流式下这个值管的是
                **两次数据之间的间隔**而不是总时长：一个跑十分钟但每条分片都很快的
                长回答不会超时，反而是"迟迟吐不出第一个字"才会触发。
            extra_body: 透传给接口的额外请求体参数，如 DeepSeek 思考模式开关
                ``{"thinking": {"type": "enabled"}}``。为 None 时不传。
            include_usage: 是否索取 token 用量。开启后每次请求多带一个
                stream_options 参数；若服务端不认它（会 400），chat() 会自动去掉
                重试并记住这个结果，之后不再带。
        """
        _reject_unsupported_extra_body(extra_body)
        self.model = model
        self._extra_body = extra_body
        self._include_usage = include_usage
        # 客户端持有关键资源（连接池），建好后就不要再改。
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        on_delta: OnDelta | None = None,
    ) -> LLMResponse:
        """调用接口并归一化响应，调用失败时返回 finish_reason="error" 的响应。"""
        params: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            # stream 必须作为显式参数交给 SDK：它靠这个 kwarg 决定返回流对象而不是
            # 解析好的 JSON，只把 stream 写进 extra_body 会让客户端按普通响应解析
            # 一个 SSE 流，报错完全指不到真正的原因。
            "stream": True,
            "extra_body": self._build_extra_body(),
        }
        # 空列表也视作"本轮不给工具"：有些服务收到 tools=[] 会直接报参数错误，
        # 所以宁可不传这个字段。
        if tools:
            params["tools"] = tools

        try:
            response = await self._stream_once(params, on_delta)
        except BadRequestError as exc:
            response = await self._retry_without_usage(params, on_delta, exc)
        except Exception as exc:
            # 只捕获 Exception：CancelledError / KeyboardInterrupt 继承自
            # BaseException，必须继续向上传播，否则 Ctrl+C 会失去响应。
            return _error_response(exc)

        if response is None:
            logger.error("模型没有返回任何分片")
            return LLMResponse(content="错误：模型返回了空的结果列表", finish_reason="error")
        return response

    def _build_extra_body(self) -> dict[str, Any]:
        """拼出本次请求要透传的额外字段。

        **stream / stream_options 必须由我们最后写入**：SDK 里 extra_body 的值会
        覆盖显式 kwargs，用户哪天在 LLM_EXTRA_BODY 里加上 ``"stream": false``，
        就会把请求悄悄变回非流式，而我们仍按流去迭代，报出来的错和真实原因差着
        十万八千里。字典后写者胜，正好用来兜住这种配置。
        """
        body = dict(self._extra_body or {})
        for key in ("stream", "stream_options"):
            if key in body:
                logger.warning("extra_body 里的 %r 会被流式实现覆盖（该键由 Provider 掌管）", key)
        body["stream"] = True
        if self._include_usage:
            body["stream_options"] = {"include_usage": True}
        else:
            body.pop("stream_options", None)
        return body

    async def _stream_once(
        self,
        params: dict[str, Any],
        on_delta: OnDelta | None,
    ) -> LLMResponse | None:
        """发一次流式请求并累积成完整响应。

        Returns:
            完整响应；一个分片都没收到时返回 None（流里的空 choices 是合法的，
            不能据此判错，所以只能按"整条流一个分片都没有"来判定）。

        Raises:
            Exception: 请求失败或中途断流都会往上抛，由 chat() 统一兜成错误响应。
        """
        accumulator = _StreamAccumulator()
        try:
            # async with 保证中途退出（含 Ctrl+C 取消）时关掉底层响应：不关的话
            # 反复中断会在连接池里挂一堆半开连接。
            async with await self._client.chat.completions.create(**params) as stream:
                async for chunk in stream:
                    delta = accumulator.feed(chunk)
                    if delta is not None and on_delta is not None:
                        # inline await，不 spawn task：天然背压、天然保序。慢的接收方
                        # 只会拖慢读取，不会让事件乱序，也不会丢。
                        await on_delta(delta)
        except Exception as exc:
            # 断流是流式特有的失败方式：请求成功、内容收了一半、连接没了。这时
            # 已经推给用户的内容收不回来，所以错误信息里要带上已收到的量，让上层
            # 和用户都知道"看到的那半截不作数"。
            received = accumulator.received_chars
            if received:
                raise _StreamInterrupted(received, exc) from exc
            raise
        return accumulator.build() if accumulator.chunks else None

    async def _retry_without_usage(
        self,
        params: dict[str, Any],
        on_delta: OnDelta | None,
        exc: BadRequestError,
    ) -> LLMResponse | None:
        """被服务端拒绝 stream_options 时的降级重试。

        少数 OpenAI 兼容服务（老版本 vLLM / Ollama 等）不认这个参数，去掉它即可，
        代价只是拿不到 token 用量。返回 None 表示重试仍然失败，由调用方统一报错。
        """
        extra_body = params["extra_body"]
        if "stream_options" not in extra_body:
            logger.error("模型调用失败: %s", exc, exc_info=True)
            return _error_response(exc)

        logger.warning("服务端拒绝了 stream_options（%s），去掉后重试一次", exc)
        extra_body.pop("stream_options")
        try:
            response = await self._stream_once(params, on_delta)
        except Exception as retry_exc:
            return _error_response(retry_exc)

        # 只有重试**确实成功**才记住降级：否则一次"上下文超长"之类的 400 会被
        # 误判成"服务不支持 stream_options"，从此永久丢掉用量统计。
        self._include_usage = False
        return response

    async def aclose(self) -> None:
        """关闭底层 HTTP 客户端，释放连接池。

        进程退出前调用，让连接正常关闭而不是留给操作系统回收。关闭后实例不可
        再用；调用方应当在确定不再发起请求时再调。
        """
        await self._client.close()


class _StreamInterrupted(Exception):
    """流式响应收到一半就断了。

    单独包一层是为了让 _describe_error 既保住原始异常的分类（超时 / 连接失败 /
    服务端报错各有各的提示语），又能补上"已经收到了多少内容"这个流式特有的信息。
    """

    def __init__(self, received: int, cause: Exception) -> None:
        super().__init__(str(cause))
        self.received = received
        self.cause = cause


class _StreamAccumulator:
    """把流式分片拼回一个完整的 LLMResponse。

    流式协议的形态决定了这里必须"边收边攒"：

    - 正文与思考是按序到达的字符串碎片，直接往后拼；
    - tool_calls 是**按 index 分片**的：首片带 id 和函数名，后续片只带 arguments
      的字符串片段，而且 JSON 可能被断在任意位置，所以只能按 index 归并后整体
      解析，不能一片算一次调用；
    - 有相当一部分分片不含任何文本（角色片、usage 尾片），它们只贡献元信息，
      不产生增量——上层不必收到一堆空事件。

    只负责累积，不负责判断成败：没有分片、参数是坏 JSON 这些情况都不在这里抛异常。
    """

    def __init__(self) -> None:
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._tool_slots: dict[int, dict[str, Any]] = {}
        self._finish_reason: str | None = None
        self._usage: dict[str, Any] = {}
        self.chunks = 0

    @property
    def received_chars(self) -> int:
        """已收到的正文字符数，用于断流时说明"看到的那半截有多大"。"""
        return sum(len(part) for part in self._content)

    def feed(self, chunk: Any) -> StreamDelta | None:
        """吃下一个分片，返回其中的文本增量；该分片没有文本时返回 None。"""
        self.chunks += 1

        # usage 的落点各家不一：标准做法是单独发一个 choices 为空的尾片，但也有
        # 服务把它塞在带 finish_reason 的那片里，所以每片都看一眼。
        usage = getattr(chunk, "usage", None)
        if usage:
            self._usage = _extract_usage(usage)

        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return None
        choice = choices[0]

        if getattr(choice, "finish_reason", None):
            self._finish_reason = choice.finish_reason

        delta = getattr(choice, "delta", None)
        if delta is None:
            return None

        if getattr(delta, "tool_calls", None):
            _merge_tool_call_deltas(self._tool_slots, delta.tool_calls)

        content = getattr(delta, "content", None) or ""
        reasoning = getattr(delta, "reasoning_content", None) or ""
        if content:
            self._content.append(content)
        if reasoning:
            self._reasoning.append(reasoning)
        if not content and not reasoning:
            return None
        return StreamDelta(content=content, reasoning=reasoning)

    def build(self) -> LLMResponse:
        """收尾，产出与"非流式响应"等价的完整对象。"""
        content = "".join(self._content)
        reasoning = "".join(self._reasoning)
        return LLMResponse(
            # 空串归一为 None（既有行为）：思考模型在工具轮次会返回 content=""，
            # 留着会让上层把"没有文本"当成"有文本回复"，打印出一行空白。
            content=content or None,
            tool_calls=_finalize_tool_calls(self._tool_slots, reasoning or None),
            finish_reason=self._finish_reason or "stop",
            usage=self._usage,
            reasoning_content=reasoning or None,
        )


def _merge_tool_call_deltas(slots: dict[int, dict[str, Any]], raw_tool_calls: Any) -> None:
    """把一批 tool_call 增量按 index 归并进 slots。

    index 是协议里唯一的归并依据：同一个 index 的多个分片属于同一次调用。
    id 和函数名只在首片出现（后续分片是 None），所以取**第一个非空值**而不是覆盖；
    arguments 则相反，是真正的字符串片段，必须往后拼。
    """
    for raw in raw_tool_calls:
        index = getattr(raw, "index", None)
        if index is None:
            # 个别服务不发 index。这种实现也基本不会把参数分片，退化成"一片一次调用"。
            index = len(slots)
        slot = slots.get(index)
        if slot is None:
            slot = {"id": "", "name": "", "arguments": "", "reasoning": ""}
            slots[index] = slot

        if not slot["id"] and getattr(raw, "id", None):
            slot["id"] = raw.id

        function = getattr(raw, "function", None)
        if function is not None:
            if not slot["name"] and getattr(function, "name", None):
                slot["name"] = function.name
            arguments = getattr(function, "arguments", None)
            if arguments:
                slot["arguments"] += arguments

        # 少数模型（DeepSeek 等）把推理过程挂在 tool_call 分片上而不是 delta 顶层，
        # 两处都收，漏掉会让下一轮回传缺字段。
        reasoning = getattr(raw, "reasoning_content", None)
        if reasoning:
            slot["reasoning"] += reasoning


def _finalize_tool_calls(
    slots: dict[int, dict[str, Any]],
    message_reasoning: str | None,
) -> list[ToolCallRequest]:
    """把归并好的槽位转成 ToolCallRequest 列表，按 index 顺序输出。"""
    result: list[ToolCallRequest] = []
    for index in sorted(slots):
        slot = slots[index]
        name = slot["name"]
        result.append(
            ToolCallRequest(
                id=slot["id"],
                name=name,
                arguments=_parse_arguments(slot["arguments"], name),
                # 流式下思考先于 tool_calls 到达，协议上没有"这段推理属于哪次调用"
                # 的归属信息，只能整段挂到每个调用上。DeepSeek 要求回传时带上它，
                # 缺了下一轮直接 400，所以这里宁可重复也不能为空。
                reasoning_content=slot["reasoning"] or message_reasoning or None,
            )
        )
    return result


def _reject_unsupported_extra_body(extra_body: dict[str, Any] | None) -> None:
    """拦住流式实现撑不住的配置，在构造时就失败。

    目前只拦 ``n > 1``：多候选在流式下是交错下发的，而 tool_calls 靠分片里的
    index 归并，两路候选的 index 会串在一起，聚合层无法区分——这是配置错误，
    不是运行时故障，所以按本项目"配置问题早失败"的一贯做法当场报错。
    """
    if not extra_body:
        return
    n = extra_body.get("n")
    if isinstance(n, int) and n > 1:
        raise ValueError(
            f"extra_body 里的 n={n} 与流式请求不兼容：多候选的增量会交错到达，"
            "工具调用无法正确归并。请去掉 n 或将其设为 1。"
        )


def _parse_arguments(raw: Any, tool_name: str | None) -> dict[str, Any]:
    """把 arguments 解析成 dict。

    模型给出的 arguments 是 JSON 字符串，且确实可能给出非法 JSON（多吐了引号、
    被 max_tokens 截断、流式断流等）。这里不抛异常而是返回 {}：工具层随后会报
    "缺少参数"并把它回传给模型，模型通常能自我纠正重发；直接抛异常则会炸掉整轮对话。
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


def _error_response(exc: Exception) -> LLMResponse:
    """把异常转成给上层统一处理的错误响应。"""
    logger.error("模型调用失败: %s", exc, exc_info=True)
    return LLMResponse(content=f"错误：{_describe_error(exc)}", finish_reason="error")


def _describe_error(exc: Exception) -> str:
    """把异常翻译成给模型和用户看的可读描述。

    分类只是为了给出可操作的提示——agent 循环拿到的是文本，如果这里只回一段
    SDK 原始报错，排查时还得再去猜是哪类问题。
    """
    # 断流要先把"已经收到多少"说出来，再套用原因自己的描述。
    if isinstance(exc, _StreamInterrupted):
        return (
            f"流式响应中断，本轮结果不完整（已收到 {exc.received} 字符）："
            f"{_describe_error(exc.cause)}"
        )

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
