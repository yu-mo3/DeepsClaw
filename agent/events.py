"""Agent 的输出事件与输出端（Sink）。

AgentLoop 不直接打印任何东西，它把"发生了什么"翻译成一组事件推给 OutputSink，
由 sink 决定怎么呈现：CLI 打成文字、WebSocket 转成 JSON 推给前端、测试里收进
列表。想给页面做"思考中…"折叠块 + 打字机效果，接的就是这一层。

**事件契约**（前端照着这份写即可，``to_dict()`` 的输出就是最终 JSON）::

    {"type": "thinking",    "text": "用户想看工作区…", "step": 0, "ts": 1759900000.12}
    {"type": "tool_call",   "id": "call_1", "name": "list_dir",
     "arguments": {"dir_path": "."}, "step": 0, "ts": ...}
    {"type": "tool_result", "id": "call_1", "name": "list_dir",
     "result": "main.py\\n…", "truncated": false, "length": 128, "step": 0, "ts": ...}
    {"type": "thinking",    "text": "目录里有 main.py…", "step": 1, "ts": ...}
    {"type": "content",     "text": "工作区里", "step": 1, "ts": ...}
    {"type": "content",     "text": "有这些文件…", "step": 1, "ts": ...}
    {"type": "turn_end",    "reply": "工作区里有这些文件…", "reason": "finished", "ts": ...}

字段含义：

============  ==========================================================
``type``      事件类型，见下表的取值
``step``      本轮内第几次模型调用（0 起）。**前端按 step 变化开新块**——
              一次 run() 里模型可能被调用多次，只按类型聚合会把多段思考
              糊成一团、把多轮回答粘成同一个气泡
``ts``        Unix 时间戳（秒，float）。只用于展示（如计算首字延迟），
              **不要用于排序**
``reason``    仅 ``turn_end`` 有：finished / error / loop_break /
              max_steps / cancelled
============  ==========================================================

**顺序保证**（AgentLoop 串行 inline await 投递，sink 不得重排、不得并发发送）：

1. 同一 step 的思考/正文增量严格按模型吐字的顺序到达；
2. 某个 step 的 ``tool_call`` 出现在该 step 最后一个增量之后；
3. ``tool_result`` 紧跟它对应的 ``tool_call``；
4. ``error`` 一定在 ``turn_end`` 之前；
5. ``turn_end`` 恰好一次，且一定是最后一条。

另外两条约定：

- **``turn_end`` 就是"会话空闲"信号**。同一个 AgentLoop 在 turn 进行中不能再
  run()，所以前端"收到 turn_end 之前禁用发送键"的规则由事件契约直接提供。
- **``turn_end.reason == "error"`` 表示本 turn 已渲染的流式内容作废**：流式下
  失败可能发生在收到一半时，推出去的字收不回来，前端应把气泡标记为失败而不是
  当成完整回答。

典型用法::

    async def push(event):
        await websocket.send_json(event.to_dict())

    loop = AgentLoop(provider, registry, context, sink=CallbackSink(push))
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields
from typing import Any, Awaitable, Callable, ClassVar

#: TurnEndEvent.reason 的全部取值
REASON_FINISHED = "finished"
"""正常跑完，reply 是模型的最终答复。"""
REASON_ERROR = "error"
"""模型调用失败，reply 是错误提示，本轮已渲染的内容作废。"""
REASON_LOOP_BREAK = "loop_break"
"""检测到工具调用死循环，提前中止，reply 是熔断说明。"""
REASON_MAX_STEPS = "max_steps"
"""达到单轮最大步数仍未收敛，reply 是提示。"""
REASON_CANCELLED = "cancelled"
"""本轮被取消（用户中断）。"""


@dataclass
class AgentEvent:
    """所有输出事件的基类。

    只有 ``type`` 和 ``ts`` 两个公共成员；子类各自带自己的负载字段。
    ``ts`` 用 init=False，一是让子类字段不必迁就它，二是顺手保证子类的必填字段
    不会触发"non-default argument follows default argument"。
    """

    #: 事件类型标识，子类覆盖。ClassVar 不算 dataclass 字段，不会进 to_dict。
    type: ClassVar[str] = "event"

    #: 事件产生时刻。compare=False 让测试可以直接比较事件列表而不用管时间戳。
    ts: float = field(default_factory=time.time, init=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """转成可直接 JSON 序列化的字典，也是发给前端的最终形态。

        ``type`` 一定在第一个键上，方便前端日志里一眼看出是什么事件。
        """
        data: dict[str, Any] = {"type": self.type}
        for item in fields(self):
            data[item.name] = getattr(self, item.name)
        return data


@dataclass
class ThinkingDelta(AgentEvent):
    """思考内容的一段增量（模型的 reasoning_content）。"""

    type: ClassVar[str] = "thinking"
    text: str = ""
    step: int = 0


@dataclass
class ContentDelta(AgentEvent):
    """正文内容的一段增量，前端按到达顺序追加就得到完整回答。"""

    type: ClassVar[str] = "content"
    text: str = ""
    step: int = 0


@dataclass
class ToolCallEvent(AgentEvent):
    """模型发起了一次工具调用（在真正执行之前发出）。"""

    type: ClassVar[str] = "tool_call"
    id: str = ""
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    step: int = 0


@dataclass
class ToolResultEvent(AgentEvent):
    """一次工具调用的结果。

    ``result`` 是**预览**，可能被截断（``truncated`` 为 True，``length`` 是原始
    字符数）——模型拿到的始终是全文，只有订阅者收到的是预览。截断在 AgentLoop
    统一做，因为 read_file 一个几 MB 的文件足以把前端页面打死。
    """

    type: ClassVar[str] = "tool_result"
    id: str = ""
    name: str = ""
    result: str = ""
    truncated: bool = False
    length: int = 0
    step: int = 0


@dataclass
class ErrorEvent(AgentEvent):
    """模型调用层面的失败（超时、鉴权、限流、断流等已归一的错误）。"""

    type: ClassVar[str] = "error"
    message: str = ""
    step: int = 0


@dataclass
class TurnEndEvent(AgentEvent):
    """一轮对话结束，必为最后一条事件。

    ``reply`` 是本轮的最终文本，用它兜底最省事：错误提示、熔断说明这类内容不会
    走流式增量，前端只靠拼 content 是拼不出来的。正常跑完时 ``reply`` 等于
    前面所有 content 增量拼起来的文本。
    """

    type: ClassVar[str] = "turn_end"
    reply: str = ""
    reason: str = REASON_FINISHED


class OutputSink(ABC):
    """Agent 事件的输出端。

    CLI 打印、WebSocket 推送、测试收集都只是换一个实现，AgentLoop 只认这个抽象。

    两条硬约定，实现方必须遵守，否则上层保证的顺序会失效：

    1. **不要在 emit 里并发发送**——例如内部 ``asyncio.create_task`` 一个转发
       任务。那会让事件乱序到达前端。慢就让它慢：AgentLoop 是 inline await 的，
       背压天然成立。
    2. **emit 抛异常是允许的**——AgentLoop 会兜住并记 warning，不影响本轮任务的
       结果。UI 断连不该让正在跑的 agent 任务失败。
    """

    @abstractmethod
    async def emit(self, event: AgentEvent) -> None:
        """投递一条事件。实现里应当尽快返回，不要做耗时操作。"""
        raise NotImplementedError


class NullSink(OutputSink):
    """什么都不做的 sink。

    AgentLoop 默认用它，省得每个调用点都判一遍 sink 是不是 None。
    """

    async def emit(self, event: AgentEvent) -> None:
        return None


class CallbackSink(OutputSink):
    """把事件交给一个回调函数。

    接前端时最省事的一层胶水::

        async def push(event):
            await websocket.send_json(event.to_dict())

        loop = AgentLoop(provider, registry, context, sink=CallbackSink(push))

    回调本身是 await 的，所以 WebSocket 发送、队列投递这类异步操作可以直接写进去，
    不必再包一层后台任务——那样反而会丢序。
    """

    def __init__(self, callback: Callable[[AgentEvent], Awaitable[None]]) -> None:
        self._callback = callback

    async def emit(self, event: AgentEvent) -> None:
        await self._callback(event)
