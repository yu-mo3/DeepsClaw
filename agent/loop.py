"""Agent 主循环。

把 provider、工具注册表、ContextBuilder 三者串成一个能跑的任务循环：
请求模型 → 若要调工具就执行 → 把结果喂回去 → 再请求，直到模型给出最终答复。

    user 输入 ──┐
                ▼
          ┌───────────┐   无 tool_calls   ┌──────────┐
          │ 调用模型   │ ────────────────▶ │ 返回文本  │
          └───────────┘                   └──────────┘
                │ 有 tool_calls
                ▼
          ┌───────────┐   结果写回 messages
          │ 执行工具   │ ──────────┐
          └───────────┘           │
                ▲                 │
                └─────────────────┘

循环过程中的每一步都会翻译成**输出事件**推给 OutputSink（见 agent.events）：
模型的思考与正文增量、工具调用与结果、错误、以及收尾。本模块不打印任何东西，
呈现方式由 sink 决定，将来接 WebSocket 前端时不必改这里一行代码。

围绕这个循环有两类"防爆"措施，因为 agent 最常见的失控不是崩溃，而是转圈：

- **_check_tool_loop**：模型一轮接一轮地发起完全相同的工具调用（陷进死胡同），
  轻微时给模型塞一句提醒，严重时直接熔断中止；
- **max_steps**：工具各不相同但一直不收敛（任务本身没边），到步数上限就停。

两者目的不同，缺一个都挡不住对应的失控方式。

典型用法::

    loop = AgentLoop(provider, registry, context_builder)
    print(await loop.run("帮我把 main.py 里的 TODO 都找出来"))
    # loop.history 即完整会话历史，可直接落盘
"""

import asyncio
import json
import logging
from collections import deque
from enum import Enum
from typing import Any

from agent.context import ContextBuilder
from agent.events import (
    REASON_CANCELLED,
    REASON_ERROR,
    REASON_FINISHED,
    REASON_LOOP_BREAK,
    REASON_MAX_STEPS,
    AgentEvent,
    ContentDelta,
    ErrorEvent,
    NullSink,
    OutputSink,
    ThinkingDelta,
    ToolCallEvent,
    ToolResultEvent,
    TurnEndEvent,
)
from agent.tools.registry import ToolRegistry
from providers.base import LLMProvider, LLMResponse, StreamDelta, ToolCallRequest
from session.manager import SessionManager

logger = logging.getLogger(__name__)

#: 工具结果在事件里的预览上限（字符）。模型拿到的始终是全文，只有订阅者收到截断——
#: read_file 读一个几 MB 的文件，原样推到前端会直接把页面打死。
TOOL_RESULT_PREVIEW_LIMIT = 2000


class LoopCheck(Enum):
    """单次工具调用的防爆判定结果。"""

    OK = "ok"
    """正常，照常执行。"""

    WARN = "warn"
    """重复次数偏高，执行但附带提醒。"""

    BREAK = "break"
    """判定为死循环，中止本轮任务。"""


class AgentLoop:
    """驱动"模型思考 - 工具执行"循环的会话主体。

    一次 run() 就是一轮完整的用户交互：可能只问一句得一句，也可能内部转十几圈
    工具才收尾。对调用方而言只有一个入口和一个字符串返回。

    实例持有会话状态（history），是**有状态**的：同一个 AgentLoop 连续 run()
    会自动带上之前的上下文，因此一个会话对应一个实例，不要跨用户复用。

    模型返回 finish_reason="error" 时不写入历史，直接返回错误文本。因为模型
    实际上什么都没说过，把它当成功的一轮记进去，下一轮上下文里会凭空多出
    "用户提问 + 无回复"的组合，重试时还可能出现连续两条相同提问。
    """

    #: 同一工具+同一参数在窗口内重复达到该次数即熔断中止
    CIRCUIT_BREAK_THRESHOLD = 20
    #: 重复达到该次数开始给模型提示
    WARN_THRESHOLD = 10
    #: 防爆检测的滑动窗口大小（只看最近多少次工具调用）
    LOOP_WINDOW_SIZE = 30

    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        context: ContextBuilder,
        model: str | None = None,
        max_steps: int = 50,
        history: list[dict] | None = None,
        sink: OutputSink | None = None,
        session_manager: SessionManager | None = None,
        session_key: str = "cli:direct",
    ) -> None:
        """初始化。

        Args:
            provider: 模型提供方，负责一次 chat 调用。
            registry: 工具注册表，提供定义列表与执行能力。
            context: 上下文构造器，负责拼 System Prompt 和 messages。
            model: 指定模型名，为 None 时用 provider 的默认模型。
            max_steps: 单轮 run() 内最多调用多少次模型，防止任务不收敛时无限跑。
                每次工具调用后都要再问一次模型，所以它约等于"最多几轮工具"。
            history: 已有的会话历史，用于恢复会话。**按引用持有**。显式传了它就
                以它为准（同一进程内换会话用）；为 None 时改从 session_manager 恢复，
                两者都没有则从空历史开始。
            sink: 输出事件的接收方，为 None 时用 NullSink（什么都不做）。CLI 传
                打印到终端的 sink，接前端时传推 WebSocket 的 sink，本类不关心区别。
            session_manager: 会话持久化器。为 None 时本类退化成纯内存模式（测试
                与不落盘的嵌入场景用），run() 照常工作，只是不写磁盘。
            session_key: 会话标识，决定读写的 JSONL 文件，如 "cli:direct"。
        """
        self.provider = provider
        self.registry = registry
        self.context = context
        self.model = model
        self.max_steps = max_steps
        self.session_manager = session_manager
        self.session_key = session_key
        if history is not None:
            self.history: list[dict] = history
        elif session_manager is not None:
            self.history = _repair_history(session_manager.get_history(session_key))
            if self.history:
                logger.info(
                    "从会话 %s 恢复了 %d 条历史消息", session_key, len(self.history)
                )
        else:
            self.history = []
        self._sink: OutputSink = sink or NullSink()
        # 本轮是第几次模型调用，打给事件的 step 字段用——前端按它分块，否则同一个
        # turn 里多段思考会糊成一团、多轮回答会粘成同一个气泡。
        self._step = 0
        # 滑动窗口只记录最近若干次调用的指纹，超出自动丢弃。
        self._tool_window: deque[str] = deque(maxlen=self.LOOP_WINDOW_SIZE)

    async def run(self, user_input: str = "") -> str:
        """执行一轮 Agent 循环，返回最终给用户看的文本。

        全程把模型的思考、正文、工具调用推给 sink；返回值仍然保留（调用方可能
        拿它落盘或做二次加工），但控制台那条路的呈现已经由事件负责。

        Args:
            user_input: 本轮用户输入。为空时表示"接着上次继续"——历史里已经有
                工具结果或需要模型补充说明，此时不追加新的用户消息。

        Returns:
            模型的最终回复文本；出错、熔断或步数用尽时，返回对应的说明文本。
            无论是哪种结局，输出端都会先收到一条 TurnEndEvent。
        """
        # 每轮重置：防爆针对的是"本次任务陷进死胡同"，用户上一轮问过什么不该算进来。
        self._tool_window.clear()
        self._step = 0

        messages = self.context.build_messages(self.history, user_input)
        turn: list[dict] = []
        if user_input:
            await self._remember(turn, {"role": "user", "content": user_input})

        reply = ""
        reason = REASON_FINISHED
        try:
            for _ in range(self.max_steps):
                response = await self.provider.chat(
                    messages,
                    tools=self.registry.get_definitions(),
                    model=self.model,
                    on_delta=self._on_delta,
                )

                if response.finish_reason == "error":
                    # 不写入历史：模型没真正回复过，记进去会污染后续上下文。
                    logger.error("模型调用失败，中止本轮: %s", response.content)
                    reason = REASON_ERROR
                    reply = response.content or "错误：模型调用失败"
                    await self._emit(ErrorEvent(message=reply, step=self._step))
                    return reply

                assistant_message = self._build_assistant_message(response)
                messages.append(assistant_message)
                await self._remember(turn, assistant_message)

                if not response.has_tool_calls:
                    self._save_to_history(turn)
                    reply = response.content or ""
                    return reply

                aborted = await self._run_tool_calls(response, messages, turn)
                if aborted is not None:
                    reason = REASON_LOOP_BREAK
                    reply = aborted
                    return reply

                # 工具跑完要再问一次模型，那算本轮的新一步。
                self._step += 1

            # 走到这里说明步数耗尽：历史最后是一条 tool 消息，结构仍然合法，
            # 补一条 assistant 说明后照常保存。
            notice = (
                f"已达到单轮最大步数（{self.max_steps} 步）仍未完成任务，已停止。"
                "请把需求拆分成更小的步骤后重试。"
            )
            turn.append({"role": "assistant", "content": notice})
            self._persist(turn[-1])
            self._save_to_history(turn)
            logger.warning("达到 max_steps=%d，强制结束本轮", self.max_steps)
            reason = REASON_MAX_STEPS
            reply = notice
            return reply
        except asyncio.CancelledError:
            # 取消不写历史，这正是 main.py 敢承诺"中途取消不留半截消息"的原因。
            reason = REASON_CANCELLED
            raise
        except Exception:
            # 预期外的异常（代码 bug、context 构建失败等）不额外发 ErrorEvent：
            # 调用方手里有异常对象本身，比一段字符串全。只标 reason，让前端能把
            # busy 状态解开。ErrorEvent 只服务上面那条已归一的模型失败路径。
            reason = REASON_ERROR
            raise
        finally:
            # 四个显式出口 + 取消 + 异常两条隐形出口，统一在这里收尾。逐个 return
            # 前手写 emit 一定会漏其中某一条。
            await self._emit(TurnEndEvent(reply=reply, reason=reason))

    async def _emit(self, event: AgentEvent) -> None:
        """把事件推给输出端。

        **本方法永不抛异常**：sink 是外接的（将来可能是往 WebSocket 推），它崩了
        不能带崩正在跑的 agent 任务——UI 断连不该让任务失败。所以这里吞掉异常只
        记 warning，provider 那边也因此可以放心地不做防御（见 OnDelta 契约）。
        """
        try:
            await self._sink.emit(event)
        except Exception:  # noqa: BLE001 - 输出端故障不能影响任务本身
            logger.warning("投递输出事件 %s 时出错，已忽略", event.type, exc_info=True)

    async def _on_delta(self, delta: StreamDelta) -> None:
        """把 provider 的增量翻译成输出事件。

        一段增量里思考和正文可能同时有（少见但合法），分别发两条：前端是往两个
        不同的容器里追加的，混在一条里反而要它自己拆。
        """
        if delta.reasoning:
            await self._emit(ThinkingDelta(text=delta.reasoning, step=self._step))
        if delta.content:
            await self._emit(ContentDelta(text=delta.content, step=self._step))

    async def _run_tool_calls(
        self,
        response: LLMResponse,
        messages: list[dict],
        turn: list[dict],
    ) -> str | None:
        """执行本条 assistant 消息里的全部工具调用。

        Args:
            response: 含 tool_calls 的模型响应。
            messages: 本轮发给模型的完整消息列表，工具结果就地追加。
            turn: 本轮新增消息，与 messages 同步累积，最终写入历史。

        Returns:
            熔断时返回给用户的说明文本；正常执行完毕返回 None。
        """
        verdict, repeats, label = self._check_tool_loop(response.tool_calls)

        if verdict is LoopCheck.BREAK:
            logger.warning("检测到工具调用死循环: %s 重复 %d 轮，熔断", label, repeats)
            # 关键一步：这条 assistant 消息已经进了 messages，其中每个 tool_call
            # 都必须在历史里有对应的 tool 结果，否则下次请求会被接口以结构非法
            # 拒绝（400）。既然整轮都不执行，就逐个补占位结果，不能直接跳出。
            for pending in response.tool_calls:
                # 模型确实发起了这些调用，如实广播出去（前端才好显示"以下调用被
                # 拒绝执行"）；但占位消息是给接口看的结构性补丁、不是真结果，
                # 所以只发 ToolCallEvent，不发 ToolResultEvent。
                await self._emit(
                    ToolCallEvent(
                        id=pending.id,
                        name=pending.name,
                        arguments=pending.arguments,
                        step=self._step,
                    )
                )
                placeholder = {
                    "role": "tool",
                    "tool_call_id": pending.id,
                    "content": "[未执行] 本次调用因检测到重复循环被中止。",
                }
                messages.append(placeholder)
                await self._remember(turn, placeholder)

            notice = (
                f"⚠️ 检测到工具调用陷入循环：{label} 这组调用已原样重复了 {repeats} 轮，"
                "已中止本轮任务。请换一种问法，或告诉我更明确的目标。"
            )
            # 中止说明也记进历史，保证"用户看到的"和"模型记得的"一致。
            self._save_to_history([*turn, {"role": "assistant", "content": notice}])
            return notice

        hint = ""
        if verdict is LoopCheck.WARN:
            hint = (
                f"\n\n[系统提示] 你已经原样重复了 {repeats} 轮相同的工具调用（{label}），"
                "说明这条路走不通。请停止重复，改为根据已有信息直接回答，"
                "或换用其他工具/参数。"
            )

        for index, tool_call in enumerate(response.tool_calls):
            # 调用与结果交错发出，而不是"先全发调用、再全发结果"：这样订阅者拿到的
            # 顺序就是真实时间线，前端不必自己按 id 配对。
            await self._emit(
                ToolCallEvent(
                    id=tool_call.id,
                    name=tool_call.name,
                    arguments=tool_call.arguments,
                    step=self._step,
                )
            )
            result = await self.registry.execute(tool_call.name, tool_call.arguments)
            # 提示只挂在整轮的第一个结果上，多个调用重复挂同样的文字只是噪声。
            if index == 0 and hint:
                result += hint

            await self._emit(_tool_result_event(tool_call, result, self._step))

            tool_message = {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            }
            messages.append(tool_message)
            await self._remember(turn, tool_message)

        return None

    def _build_assistant_message(self, response: LLMResponse) -> dict[str, Any]:
        """把 LLMResponse 还原成可回传给接口的 assistant 消息。

        和 providers 层的解析互逆：那边把 arguments 从 JSON 字符串解析成 dict，
        这边要再序列化回去——接口要求 function.arguments 是字符串。

        两个易错点：

        - **reasoning_content 放在消息顶层**，不在单个 tool_call 里。DeepSeek
          的思考模式要求随 tool_calls 一起回传，位置放错会直接返 400。
        - **没有工具调用时不带 tool_calls 字段**，而不是给个空列表：部分服务端
          对空数组的处理不一致，索性不传。
        """
        message: dict[str, Any] = {"role": "assistant", "content": response.content}

        if response.tool_calls:
            message["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": json.dumps(tool_call.arguments, ensure_ascii=False),
                    },
                }
                for tool_call in response.tool_calls
            ]
            # 优先取响应级的推理：流式下它是一整段、先于 tool_calls 到达，没有
            # "这段推理属于哪次调用"的归属信息；tool_call 上那份是同内容的副本，
            # 留给按旧形状取用的实现。取不到再回退到 tool_call 级。
            reasoning = response.reasoning_content or next(
                (tc.reasoning_content for tc in response.tool_calls if tc.reasoning_content),
                None,
            )
            if reasoning:
                message["reasoning_content"] = reasoning

        return message

    def _check_tool_loop(
        self, tool_calls: list[ToolCallRequest]
    ) -> tuple[LoopCheck, int, str]:
        """把本轮调用记入滑动窗口，并判定是否构成重复循环。

        指纹是**整轮的调用组合**：先给每个调用算"工具名 + 规范化参数"，参数用
        sort_keys 排序再序列化（不排序的话模型换个键序就能绕过检测），再把一轮里
        所有调用的指纹排序后拼成一个整体。

        按轮计数而不是按单次调用计数，是因为窗口大小固定为 LOOP_WINDOW_SIZE 时，
        后者对多调用循环存在盲区：模型若每轮固定发起 3 个调用，同一指纹在 30 的
        窗口里最多只能凑到 10 次，永远够不到 20 的熔断线；每轮 4 个调用连 10 次
        的警告线都够不着。而"每轮读一个文件、改一处、再读"恰恰是最常见的死循环
        形态。按轮计数则不受轮内调用个数影响。

        只统计**完全一致**的组合：同一工具换个参数属于正常探索，不算循环。

        Args:
            tool_calls: 本轮模型请求的全部工具调用，调用方保证非空。

        Returns:
            (判定结果, 该组合在窗口内出现的次数, 供提示语使用的调用描述)。
        """
        descriptions = [
            f"{tc.name}({json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False)})"
            for tc in tool_calls
        ]
        signature = "|".join(sorted(descriptions))
        self._tool_window.append(signature)
        repeats = self._tool_window.count(signature)

        # 描述只取工具名，避免提示语里塞进一长串参数 JSON。
        label = "、".join(sorted({tc.name for tc in tool_calls}))

        if repeats >= self.CIRCUIT_BREAK_THRESHOLD:
            return LoopCheck.BREAK, repeats, label
        if repeats >= self.WARN_THRESHOLD:
            return LoopCheck.WARN, repeats, label
        return LoopCheck.OK, repeats, label

    def _persist(self, message: dict) -> None:
        """把一条消息写进会话文件（没配 session_manager 时是空操作）。"""
        if self.session_manager is not None:
            self.session_manager.save_message(self.session_key, message)

    async def _remember(self, turn: list[dict], message: dict) -> None:
        """把一条新消息同时记进本轮 turn 与会话文件。

        三处消息（用户输入、assistant 回复、工具结果）统一走这一个入口，是为了让
        "内存里有的，磁盘上也有"这件事只有一个实现点——散在四五个 append 后面各写
        一次 save_message，迟早会漏掉其中一处，而漏掉的后果是重启后历史错位。

        两个列表的用途不同：turn 在本轮结束时整批进 history（熔断/出错时不进），
        会话文件则从第一条起就落盘。

        Args:
            turn: 本轮新增的消息列表，就地追加。
            message: 要记录的消息。
        """
        turn.append(message)
        self._persist(message)

    def clear_history(self) -> None:
        """清空会话历史：内存与磁盘一起清。

        只清内存会让用户在重启后又看到刚删掉的对话，只清磁盘则当前进程还在用旧的
        上下文——两处都清，行为才和用户按下这个命令时的预期一致。
        """
        self.history.clear()
        if self.session_manager is not None:
            self.session_manager.clear(self.session_key)

    def _save_to_history(self, messages: list[dict]) -> None:
        """把本轮新增的消息追加进会话历史。

        Args:
            messages: 本轮新增的 user / assistant / tool 消息，按发生顺序排列。
        """
        self.history.extend(messages)


def _repair_history(history: list[dict]) -> list[dict]:
    """丢弃历史末尾"有 tool_calls 却没有对应结果"的残段。

    会话文件是边跑边追加的，进程可能死在"assistant 已经发出 tool_calls、工具结果还没
    写上"的那一刻（Ctrl+C、断电、崩溃）。这段残缺历史一旦原样回传，接口会以结构非法
    直接返 400，而且这个会话从此再也打不开——比丢一轮对话严重得多。

    修法是丢弃末尾这组未完成的调用；前面的历史都是成对完整的，保留下来。历史上会出现
    这种情况的唯一入口就是崩溃，因此"从末尾往前丢"足够用，不需要做全量校验。

    Args:
        history: 从会话文件读回的消息列表。

    Returns:
        可安全回传给接口的列表；无需修复时原样返回。
    """
    # 结果按 tool_call_id 索引，且只在整组齐全时才算完成——半途而废的组同样要丢。
    results: dict[str, dict] = {
        m["tool_call_id"]: m for m in history if m.get("tool_call_id")
    }
    for index in range(len(history) - 1, -1, -1):
        message = history[index]
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        expected = [call.get("id") for call in message["tool_calls"]]
        if all(call_id in results for call_id in expected):
            continue
        logger.warning(
            "会话历史末尾有 %d 次未完成的工具调用，已丢弃该残段（进程很可能被中断过）",
            len(expected),
        )
        return history[:index]
    return history


def _tool_result_event(tool_call: ToolCallRequest, result: str, step: int) -> ToolResultEvent:
    """把工具结果包成事件，超长的截成预览。

    **模型拿到的始终是全文**（进 messages 的是 result 本身），截断只针对订阅者：
    read_file 读一个几 MB 的文件，原样推到前端会直接把页面打死。这个区分统一在
    这里做——放到各个 sink 里做等于每个实现都要重写一遍同样的规则，还容易忘。
    """
    length = len(result)
    if length <= TOOL_RESULT_PREVIEW_LIMIT:
        return ToolResultEvent(
            id=tool_call.id,
            name=tool_call.name,
            result=result,
            truncated=False,
            length=length,
            step=step,
        )
    return ToolResultEvent(
        id=tool_call.id,
        name=tool_call.name,
        result=result[:TOOL_RESULT_PREVIEW_LIMIT] + "…",
        truncated=True,
        length=length,
        step=step,
    )
