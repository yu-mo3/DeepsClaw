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

import json
import logging
from collections import deque
from enum import Enum
from typing import Any

from agent.context import ContextBuilder
from agent.tools.registry import ToolRegistry
from providers.base import LLMProvider, LLMResponse, ToolCallRequest

logger = logging.getLogger(__name__)


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
    ) -> None:
        """初始化。

        Args:
            provider: 模型提供方，负责一次 chat 调用。
            registry: 工具注册表，提供定义列表与执行能力。
            context: 上下文构造器，负责拼 System Prompt 和 messages。
            model: 指定模型名，为 None 时用 provider 的默认模型。
            max_steps: 单轮 run() 内最多调用多少次模型，防止任务不收敛时无限跑。
                每次工具调用后都要再问一次模型，所以它约等于"最多几轮工具"。
            history: 已有的会话历史，用于恢复会话。**按引用持有**，调用方
                可以直接把 loop.history 落盘做持久化。
        """
        self.provider = provider
        self.registry = registry
        self.context = context
        self.model = model
        self.max_steps = max_steps
        self.history: list[dict] = history if history is not None else []
        # 滑动窗口只记录最近若干次调用的指纹，超出自动丢弃。
        self._tool_window: deque[str] = deque(maxlen=self.LOOP_WINDOW_SIZE)

    async def run(self, user_input: str = "") -> str:
        """执行一轮 Agent 循环，返回最终给用户看的文本。

        Args:
            user_input: 本轮用户输入。为空时表示"接着上次继续"——历史里已经有
                工具结果或需要模型补充说明，此时不追加新的用户消息。

        Returns:
            模型的最终回复文本；出错、熔断或步数用尽时，返回对应的说明文本。
        """
        # 每轮重置：防爆针对的是"本次任务陷进死胡同"，用户上一轮问过什么不该算进来。
        self._tool_window.clear()

        messages = self.context.build_messages(self.history, user_input)
        turn: list[dict] = []
        if user_input:
            turn.append({"role": "user", "content": user_input})

        for _ in range(self.max_steps):
            response = await self.provider.chat(
                messages,
                tools=self.registry.get_definitions(),
                model=self.model,
            )

            if response.finish_reason == "error":
                # 不写入历史：模型没真正回复过，记进去会污染后续上下文。
                logger.error("模型调用失败，中止本轮: %s", response.content)
                return response.content or "错误：模型调用失败"

            assistant_message = self._build_assistant_message(response)
            messages.append(assistant_message)
            turn.append(assistant_message)

            if not response.has_tool_calls:
                self._save_to_history(turn)
                return response.content or ""

            aborted = await self._run_tool_calls(response, messages, turn)
            if aborted is not None:
                return aborted

        # 走到这里说明步数耗尽：历史最后是一条 tool 消息，结构仍然合法，
        # 补一条 assistant 说明后照常保存。
        notice = (
            f"已达到单轮最大步数（{self.max_steps} 步）仍未完成任务，已停止。"
            "请把需求拆分成更小的步骤后重试。"
        )
        self._save_to_history([*turn, {"role": "assistant", "content": notice}])
        logger.warning("达到 max_steps=%d，强制结束本轮", self.max_steps)
        return notice

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
                placeholder = {
                    "role": "tool",
                    "tool_call_id": pending.id,
                    "content": "[未执行] 本次调用因检测到重复循环被中止。",
                }
                messages.append(placeholder)
                turn.append(placeholder)

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
            result = await self.registry.execute(tool_call.name, tool_call.arguments)
            # 提示只挂在整轮的第一个结果上，多个调用重复挂同样的文字只是噪声。
            if index == 0 and hint:
                result += hint

            tool_message = {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            }
            messages.append(tool_message)
            turn.append(tool_message)

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
            # 一次响应的多个 tool_call 通常共享同一段推理，取第一个非空的即可。
            reasoning = next(
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

    def _save_to_history(self, messages: list[dict]) -> None:
        """把本轮新增的消息追加进会话历史。

        Args:
            messages: 本轮新增的 user / assistant / tool 消息，按发生顺序排列。
        """
        self.history.extend(messages)
