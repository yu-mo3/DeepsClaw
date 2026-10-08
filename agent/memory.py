"""历史压缩：把过长的对话历史折叠成一条摘要。

LLM 的上下文窗口是有限的，而会话历史只增不减——不做处理的话，聊到几十轮之后每一轮
请求都会带着完整历史，token 成本线性上涨，最终顶爆窗口、接口直接返 400。本模块就是
那道闸：估算历史长度，超预算时把**中间那段旧消息**交给模型摘要成一条 system 消息，
只保留第一条（system prompt）和最近的对话。

为什么保留"第一条 + 最近若干条"这个形状：

- **第一条**是 System Prompt，里面有人设、工作区、技能与长期记忆，丢了 agent 就失忆；
- **最近的对话**是当前任务的上下文，模型正靠它往下推理，压缩掉会让它前言不搭后语；
- **中间的旧消息**才是可压缩的：它们的价值已经被后续轮次消化过一遍，留个结论就够。

两处与"教科书实现"不同的地方，都是踩过才知道疼的：

1. **结构安全**：切入位置不能落在"assistant 带 tool_calls"和它对应的 tool 结果之间。
   工具调用是一组的，切开之后剩下的 tool 消息找不到发起它的那条 assistant 消息，
   接口会以结构非法直接 400。所以取尾部时会向前吸附到最近的组起点（见
   _align_tail_start）。
2. **压缩结果也要落盘**：摘要追加进 ``{workspace}/workspace/memory/HISTORY.md``，
   被丢掉的原始消息就此留个档。日后想追查"模型当时为什么这么说"，这是唯一线索。

token 估算用的是"字符数 ÷ 2"这种粗糙算法，图的是**够快够保守**：真按 tokenizer 精确数，
每次压缩前都要多跑一遍分词；而估算偏大只会让我们早一点压缩，不会漏压。中文在这个口径
下会被高估（一个汉字往往不到 0.5 token），属于有意的保守。

典型用法::

    consolidator = MemoryConsolidator(provider, workspace, token_budget=6000)
    messages = await consolidator.maybe_consolidate(messages)
"""

import json
import logging
import os
from datetime import datetime
from typing import Any

from providers.base import LLMProvider

logger = logging.getLogger(__name__)

#: 默认的 token 预算。超过它才触发压缩——太小会让刚聊两轮的历史就被摘要掉（细节全丢），
#: 太大会一直不触发直到顶爆窗口。实际取值应当明显小于模型的上下文窗口。
DEFAULT_TOKEN_BUDGET = 24000

#: 压缩后保留的**最近**消息条数，保证当前任务还有上下文。
KEEP_RECENT = 6

#: 至少要这么多条消息才谈得上压缩：1 条 System Prompt + KEEP_RECENT 条最近 + 至少 1 条旧消息。
MIN_MESSAGES_TO_COMPRESS = KEEP_RECENT + 2

#: 摘要请求的提示语。要求"只输出摘要"，避免模型把客套话也写进历史。
SUMMARY_PROMPT = (
    "请用 3-5 句话概括以下对话的关键信息，保留重要的事实和结论，"
    "省略过程细节和寒暄。只输出摘要，不要其他内容。"
)

#: 摘要生成失败时的占位文本。宁可留下一句"这里丢过东西"，也不要让压缩后出现空内容——
#: 模型看到空洞的 system 消息只会更困惑。
SUMMARY_FAILED = "（摘要生成失败，旧消息已丢弃）"

#: 摘要在 system 消息里的前缀。加前缀是为了让模型（和排查的人）一眼看出这条不是人设，
#: 而是被折叠过的历史。
SUMMARY_PREFIX = "[历史摘要]: "


class MemoryConsolidator:
    """对话历史压缩器。

    无状态、可在多轮之间复用；除了压缩时写 HISTORY.md 之外不碰磁盘。实例不持有会话
    历史，输入输出都是 messages 列表，因此谁调它、调几次都不影响正确性。
    """

    def __init__(
        self,
        provider: LLMProvider,
        workspace: str,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
        history_file: str | None = None,
    ) -> None:
        """初始化。

        Args:
            provider: 模型提供方，仅用于生成摘要（不传工具、不流式）。
            workspace: 工作区路径，压缩记录写进它下面的 ``workspace/memory/HISTORY.md``。
            token_budget: token 预算，超过才压缩。默认 6000，是给中等窗口模型留足余量
                的保守值；窗口大的模型可以调高，否则会过早压缩、丢失细节。
            history_file: 压缩记录文件的显式路径。为 None 时按 workspace 推导（默认
                行为）；测试或特殊部署可以指定它，避免在真实工作区里留下文件。
        """
        self.provider = provider
        self.workspace = workspace
        self.token_budget = token_budget
        self.history_file = history_file or os.path.join(
            workspace, "workspace", "memory", "HISTORY.md"
        )

    def estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """估算消息列表占用的 token 数。

        把每条消息序列化成 JSON 后累加字符数，再除以 2。用 JSON 而不是只数 content，
        是因为 role、tool_calls 这些结构字段同样占 token，只数正文会低估。

        Args:
            messages: 要估算的消息列表。

        Returns:
            估算值（偏保守，通常不会低估）。
        """
        total_chars = 0
        for message in messages:
            try:
                # ensure_ascii=False：中文按 1 个字符算，而不是 6 个 \uXXXX 转义字符，
                # 否则中文历史会被高估到离谱、每轮都触发压缩。
                total_chars += len(json.dumps(message, ensure_ascii=False))
            except (TypeError, ValueError):
                # 消息里混进了不可序列化的对象时不能整个炸掉：按 str() 的体量粗算。
                # 估算偏一点无所谓，压缩判断不该因为一条脏数据停摆。
                total_chars += len(str(message))
        return total_chars // 2

    async def maybe_consolidate(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """历史超预算就压缩，否则原样返回。

        Args:
            messages: 当前完整的消息列表（含开头的 System Prompt）。

        Returns:
            压缩后的新列表；没超预算、或消息太少不值得压缩时，原样返回入参。
        """
        if not messages:
            return messages

        estimated = self.estimate_tokens(messages)
        if estimated <= self.token_budget:
            return messages

        # 消息太少：压缩会把仅有的上下文也吃掉，此时超预算只能如实告知。常见于
        # "单条消息特别长"（比如刚 read_file 读了一个大文件）。
        if len(messages) < MIN_MESSAGES_TO_COMPRESS:
            logger.info(
                "token 估算 %d 超过预算 %d，但只有 %d 条消息，压无可压（多半是单条消息过长）",
                estimated, self.token_budget, len(messages),
            )
            return messages

        first_message = messages[0]
        tail_start = _align_tail_start(messages, len(messages) - KEEP_RECENT)
        old_messages = messages[1:tail_start]
        if not old_messages:
            return messages

        logger.info(
            "历史超预算（约 %d tokens > %d），压缩中间 %d 条旧消息",
            estimated, self.token_budget, len(old_messages),
        )
        summary = await self._summarize(old_messages)

        # 第一条（System Prompt）原样保留，摘要紧跟其后：它代表"更早发生的事"，
        # 放在近期对话之前才符合时间线。
        summary_message = {
            "role": "system",
            "content": SUMMARY_PREFIX + summary,
        }
        compressed = [first_message, summary_message, *messages[tail_start:]]

        self._save_to_history(summary, len(old_messages))
        logger.info(
            "压缩完成：消息 %d -> %d 条，估算 %d -> %d tokens",
            len(messages), len(compressed), estimated, self.estimate_tokens(compressed),
        )
        return compressed

    async def _summarize(self, messages: list[dict[str, Any]]) -> str:
        """把一批旧消息交给模型摘要成一段话。

        工具调用与工具结果**整条跳过**：它们通常又长又偏过程（一次 read_file 的全文、
        一次命令输出），塞进摘要请求既费钱，又把真正该留的结论淹掉。

        Args:
            messages: 要被压缩的旧消息。

        Returns:
            摘要文本；调用失败时返回占位文本，绝不抛异常——压缩是"顺手做的保养"，
            它失败不该让正在跑的一轮对话崩掉。
        """
        conversation = []
        for message in messages:
            if message.get("tool_calls") or message.get("tool_call_id"):
                continue
            content = message.get("content")
            if not content:
                continue  # 只有 tool_calls、没有正文的 assistant 消息同样没有摘要价值
            conversation.append(f"{message.get('role', 'unknown')}: {content}")

        if not conversation:
            # 全是工具消息：没有可摘要的对话内容，直接留一句说明，省掉一次模型调用。
            return "（这段历史只有工具调用记录，没有可摘要的对话内容）"

        request = [
            {
                "role": "user",
                "content": f"{SUMMARY_PROMPT}\n\n对话内容：\n" + "\n".join(conversation),
            }
        ]

        try:
            response = await self.provider.chat(messages=request, tools=None)
        except Exception as exc:  # noqa: BLE001 - 失败要降级成文本，不能打断当前这轮
            logger.warning("生成历史摘要失败: %s: %s", type(exc).__name__, exc)
            return SUMMARY_FAILED

        # provider 层把调用层面的失败归一成 finish_reason="error" 而不抛异常，
        # 所以这里必须显式判它，否则"错误：模型调用失败"会被当成摘要写进历史。
        if response.finish_reason == "error":
            logger.warning("生成历史摘要失败: %s", response.content)
            return SUMMARY_FAILED

        summary = (response.content or "").strip()
        if not summary:
            logger.warning("摘要请求返回了空内容")
            return SUMMARY_FAILED
        return summary

    def _save_to_history(self, summary: str, original_count: int) -> None:
        """把这次压缩的摘要追加进 HISTORY.md。

        用追加而不是覆盖：这份文件是"历史上都压过什么"的档案，覆盖掉等于把被丢弃的旧
        消息彻底抹去。写失败只记日志——压缩本身已经成功，不该因为归档失败而回滚。

        Args:
            summary: 摘要内容。
            original_count: 被压缩掉的旧消息条数。
        """
        content = (
            f"## {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"压缩了 {original_count} 条旧消息\n\n"
            f"{summary}\n\n---\n\n"
        )

        try:
            parent = os.path.dirname(os.path.abspath(self.history_file))
            os.makedirs(parent, exist_ok=True)
            with open(self.history_file, "a", encoding="utf-8", newline="\n") as f:
                f.write(content)
        except OSError as exc:
            logger.warning("写入压缩记录 %s 失败: %s", self.history_file, exc)


def _align_tail_start(messages: list[dict[str, Any]], start: int) -> int:
    """把"保留尾部"的起点向前吸附，保证不切开一组工具调用。

    工具调用是一组的：assistant 消息带 tool_calls，紧随其后的若干条 tool 消息通过
    tool_call_id 与它配对。切入点若落在这一组中间，压缩后的历史里就会出现"找不到
    发起者的工具结果"，接口会以结构非法直接 400，而这条会话此后每一轮都失败。

    做法是从起点往前扫：只要发现"某条被保留的 tool 消息，它的发起者还在被压缩的部分"，
    就把起点挪到那个发起者上，重复到稳定为止。向前多留几条不会出错，向后切错一条就出事。
    """
    while start > 1:
        # 被保留的那部分里，所有 tool 消息引用的 tool_call_id
        referenced = {
            m["tool_call_id"] for m in messages[start:] if m.get("tool_call_id")
        }
        if not referenced:
            break

        moved = False
        for index in range(start - 1, 0, -1):
            owner = messages[index]
            if owner.get("role") != "assistant" or not owner.get("tool_calls"):
                continue
            ids = {call.get("id") for call in owner["tool_calls"]}
            if ids & referenced:
                # 这组调用的发起者本来会被压掉，把它连同它后面的一起留在尾部。
                start = index
                moved = True
                break
        if not moved:
            break
    return start
