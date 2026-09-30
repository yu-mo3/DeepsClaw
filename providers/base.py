"""LLM Provider 层的公共数据结构和抽象接口。

这一层负责把各家模型 API 的差异挡在外面，向上只暴露两种东西：
统一的响应对象 LLMResponse（以及其中的 ToolCallRequest），
和统一的调用入口 LLMProvider.chat。

上层 agent 循环因此不需要知道背后接的是哪家模型，换 provider 只改一行实例化。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCallRequest:
    """模型发起的一次工具调用请求。

    对应各家 API 返回的 tool_calls 数组里的一项（OpenAI 格式下是
    id / function.name / function.arguments 三件套），已在本层拍平成
    扁平结构，上层拿到的 arguments 就是可直接展开的 dict。

    Attributes:
        id: 调用标识，回传工具结果时必须原样带回，用于和请求配对。
        name: 要调用的工具名，对应 Tool.name。
        arguments: 工具参数，已解析成 dict。
        reasoning_content: 模型在决定调用该工具前的推理过程。部分带"思考"能力的
            模型（如 Kimi-K2.5、DeepSeek-flash）会在这类响应里附带该字段，它不是
            给人看的装饰——多轮对话中把它原样回传，模型才能保持推理连贯，丢掉会
            导致后续轮次"忘了自己为什么这么调"。普通模型返回 None。
    """

    id: str
    name: str
    arguments: dict[str, Any]
    reasoning_content: str | None = None


@dataclass
class LLMResponse:
    """一次模型调用的统一响应。

    文本回复和工具调用共用同一个结构：只回文本时 tool_calls 为空，只调工具时
    content 为 None，两者也可能同时出现，所以判断"要不要执行工具"必须用
    has_tool_calls，而不是看 content 是否为空。

    Attributes:
        content: 模型的文本回复，没有则为 None。
        tool_calls: 模型请求的工具调用列表，没有则为空列表。
        finish_reason: 结束原因，原样透传 API 的取值。常见为 "stop"（正常结束）、
            "tool_calls"（要调工具）、"length"（被 max_tokens 截断）。上层据此
            判断异常终止，所以 provider 实现不要自行改写这个字段。
        usage: token 用量，形如 {"prompt_tokens": ..., "completion_tokens": ...,
            "total_tokens": ...}。各家字段名不完全一致，本层不做归一化，原样保留
            以便排查成本问题。
    """

    content: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        """是否包含工具调用请求，即 agent 循环是否需要进入"执行工具"分支。"""
        return len(self.tool_calls) > 0


class LLMProvider(ABC):
    """大模型服务商的统一抽象接口。

    子类负责对接具体厂商的 SDK 或 HTTP 接口，并完成两件事：

    1. **入参翻译**：把 messages / tools 转成该厂商需要的请求格式；
    2. **出参归一**：把厂商的响应转成 LLMResponse，尤其是把 tool_calls 拍平成
       ToolCallRequest，并确保 reasoning_content 不丢。

    messages 采用 OpenAI 的通用格式（``{"role": ..., "content": ...}`` 的列表），
    它是事实标准、各家基本兼容；tools 直接收 ToolRegistry.get_definitions() 的
    输出。这样上层只维护一份消息历史，不必为不同 provider 存两套。

    实现注意：网络超时、限流、鉴权失败等应当抛异常交给上层决定重试策略；而模型
    正常返回但内容异常的情况（如 arguments 是非法 JSON）不应抛异常——把 arguments
    置为 {}，让工具层报"缺少参数"回传给模型，模型通常能自我纠正并重发。
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        """发送一轮对话请求并等待模型响应。

        Args:
            messages: 完整的对话历史，OpenAI 格式。实现方需原样发送全部历史
                （本层不维护会话状态），包括 assistant 发起的 tool_calls 和
                后续 role="tool" 的工具结果消息。
            tools: 可调用工具的 function calling 定义，通常来自
                ToolRegistry.get_definitions()。为 None 或空列表时表示本轮
                不允许调用工具。
            model: 指定模型名，为 None 时使用 provider 的默认模型。

        Returns:
            归一化后的响应，工具调用各项已解析成 ToolCallRequest。

        Raises:
            Exception: 网络、鉴权、限流等调用层面的失败，由各实现按自身 SDK
                的异常类型抛出，上层负责重试或降级。
        """
        raise NotImplementedError
