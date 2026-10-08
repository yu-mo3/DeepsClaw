"""长期记忆工具。

给 Agent 一支笔，让它把"下次还该记得的事"写进 MEMORY.md。没有这个写入侧，长期记忆
就只是个空槽：System Prompt 每轮都读 MEMORY.md，而没人往里写，读到的永远是空的。

记忆文件的位置由构造参数给定（默认见 :data:`DEFAULT_MEMORY_PATH`），落在数据目录的
``workspace/memory/MEMORY.md``——不是项目根目录。这一点很重要：它既是"项目的数据"，
又要能被文件工具读到（用户可能直接让 agent 读它），放在数据目录下两边都满足。

**记什么**由模型自己判断，工具只负责安全地写。四类内容值得记，都写进了工具描述：
用户偏好、项目约定、用户给出的稳定事实、踩过的坑；而"本次任务的临时状态"不该记——
那是会话历史的事，记进来只会让每轮请求都背上无用的负担。

文件格式是 markdown，按类别分节，**同名类别下的一行就是一条**::

    # 长期记忆

    ## 用户偏好
    - [2026-10-08] 回答用中文，先给结论

这个形态是刻意选的：它同时满足"agent 可读"（纯文本、结构清晰）与"人类可维护"
（用户可以直接用编辑器增删条目），而且同名条目按类别 upsert——模型重复记同一件事时
是**更新那一行**，不是无限追加。没有这一点，记忆文件会越滚越长，最后把上下文挤干。

写入是原子的（先写同目录临时文件再 os.replace）：记忆文件每轮都要读，不能出现
"读到半截文件"的窗口。

典型用法::

    registry = ToolRegistry()
    registry.register(MemoryTool("workspace/memory/MEMORY.md"))
"""

import logging
import os
import re
from datetime import datetime
from typing import Any

from agent.tools.base import Tool

logger = logging.getLogger(__name__)

#: 记忆文件的默认位置，相对进程当前目录。与 config.DATA_MEMORY_FILE 保持一致；
#: 实际路径由 main.py 从配置传入，这里只作为直接使用本工具时的兜底。
DEFAULT_MEMORY_PATH = os.path.join("workspace", "memory", "MEMORY.md")

#: 注入 System Prompt 的记忆上限（字符），与 agent.context.MAX_MEMORY_CHARS 对应。
#: 本工具在写入前用它把关：超了就不写、并让模型去精简，而不是写进去再由读取侧截断。
MAX_MEMORY_CHARS = 4000

#: 单条记忆内容的上限（字符）。一条动辄几百字，记忆就退化成流水账了。
MAX_ITEM_CHARS = 200

#: 类别名允许的字符：给模型自由发挥的空间，但不接受会破坏 markdown 结构的字符。
_CATEGORY_RE = re.compile(r"[^\w\u4e00-\u9fff\-·]+")

#: 条目行：- [日期] 内容
_ITEM_RE = re.compile(r"^-\s*\[([^\]]*)\]\s*(.*)$")

#: 类别标题行：## 类别
_HEADING_RE = re.compile(r"^##\s+(.*)$")

#: 文件头的固定标题。
_FILE_TITLE = "# 长期记忆"


class MemoryTool(Tool):
    """读写长期记忆文件（MEMORY.md）的工具。

    无状态、可并发调用；路径只来自构造参数，模型无法指定写到哪里——一个能按参数任意
    写文件的"记忆工具"就是个后门。
    """

    def __init__(self, memory_path: str = DEFAULT_MEMORY_PATH) -> None:
        """初始化。

        Args:
            memory_path: MEMORY.md 的路径，可以是相对进程当前目录的路径（默认）或
                绝对路径。父目录不存在时在写入前自动创建。
        """
        self.memory_path = memory_path

    @property
    def name(self) -> str:
        return "save_memory"

    @property
    def description(self) -> str:
        return (
            "管理长期记忆：把值得跨会话记住的事写进 MEMORY.md，或查看、删除已有记忆。"
            "记忆每轮都会自动出现在你的上下文里，所以只记真正长期有效的东西："
            "用户的稳定偏好（语言、格式、称呼）、项目的约定与规范、用户明确要求你记住的事实、"
            "反复踩过的坑。**不要记**本次任务的过程、临时决定、工具输出原文——那些属于会话历史。"
            "同一类别下内容相同的条目会自动更新日期，不会重复堆积。"
            "action：add 写入或更新一条（默认）；read 列出全部记忆；remove 删除一条。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要记住（或删除）的内容，一句话写完；read 时可省略",
                },
                "category": {
                    "type": "string",
                    "description": "类别，如 用户偏好 / 项目约定 / 重要事实 / 经验教训，默认 通用",
                },
                "action": {
                    "type": "string",
                    "description": "add（默认）/ read / remove",
                },
            },
            "required": ["content"],
        }

    async def execute(
        self,
        content: str = "",
        category: str = "通用",
        action: str = "add",
    ) -> str:
        """执行一次记忆操作。

        Args:
            content: 记忆内容；read 时可以留空。
            category: 类别名，会被规整成能安全当标题用的文本。
            action: add / read / remove，取值不合法时按 add 处理。

        Returns:
            操作结果的可读描述；出错时返回说明文本，不抛异常。
        """
        act = (action or "add").strip().lower()
        keyword = (content or "").strip()
        section = _clean_category(category)

        if act == "read":
            return self._read_all()

        if not keyword:
            return "错误：content 不能为空，请给出要记住（或删除）的内容。"
        if len(keyword) > MAX_ITEM_CHARS:
            return (
                f"错误：单条记忆不能超过 {MAX_ITEM_CHARS} 字符（当前 {len(keyword)}）。"
                "记忆要短：把这件事压缩成一句话再记，细节留在会话或文档里。"
            )

        try:
            entries = _parse(self._load())
        except OSError as exc:
            logger.warning("读取记忆文件 %s 失败: %s", self.memory_path, exc)
            return f"错误：读取记忆文件失败 - {exc}"

        if act == "remove":
            return self._remove(entries, section, keyword)

        return self._upsert(entries, section, keyword)

    def _load(self) -> str:
        """读取记忆文件；不存在时返回空串（记忆是陆续攒出来的，不是错误）。"""
        if not os.path.isfile(self.memory_path):
            return ""
        with open(self.memory_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def _write(self, entries: list[tuple[str, str, str]]) -> None:
        """原子写回记忆文件：先写同目录临时文件，再整体替换。

        记忆文件每轮都会被读，不能出现"读到半截"的窗口；直接 truncate + write 就有这个
        窗口，而 os.replace 在同一分区上是原子的。
        """
        parent = os.path.dirname(os.path.abspath(self.memory_path))
        os.makedirs(parent, exist_ok=True)
        text = _render(entries)
        tmp_path = self.memory_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp_path, self.memory_path)

    def _upsert(self, entries: list[tuple[str, str, str]], section: str, keyword: str) -> str:
        """写入或更新一条记忆。"""
        today = datetime.now().strftime("%Y-%m-%d")

        # 同一类别下内容完全相同的条目视为同一条：刷新日期而不是再加一行。
        for index, (entry_section, _stamp, text) in enumerate(entries):
            if entry_section == section and text == keyword:
                entries[index] = (entry_section, today, text)
                return self._commit(entries, f"已更新记忆（{section}）：{keyword}")

        entries.append((section, today, keyword))
        size = len(_render(entries))
        if size > MAX_MEMORY_CHARS:
            return (
                f"错误：记忆将超过上限（{MAX_MEMORY_CHARS} 字符），已放弃写入以免挤占上下文。"
                "请先用 action=read 查看现有记忆，再用 action=remove 删掉过时或重复的条目。"
            )

        return self._commit(entries, f"已记住（{section}）：{keyword}（共 {len(entries)} 条，{size} 字符）")

    def _remove(self, entries: list[tuple[str, str, str]], section: str, keyword: str) -> str:
        """删除一条记忆。内容需要完整匹配，避免模型用半句话误删别的条目。"""
        kept = [e for e in entries if not (e[0] == section and e[2] == keyword)]
        if len(kept) == len(entries):
            return (
                f"没有找到（{section}）下内容为 {keyword!r} 的记忆。"
                "内容需要与写入时完全一致，可先用 action=read 查看现有条目。"
            )
        return self._commit(kept, f"已删除记忆（{section}）：{keyword}（剩余 {len(kept)} 条）")

    def _commit(self, entries: list[tuple[str, str, str]], success: str) -> str:
        """落盘并返回成功文案；写失败时返回错误文案（不抛异常）。"""
        try:
            self._write(entries)
        except OSError as exc:
            logger.warning("写入记忆文件 %s 失败: %s", self.memory_path, exc)
            return f"错误：写入记忆文件失败 - {exc}"
        return success

    def _read_all(self) -> str:
        """列出全部记忆，供模型核对后再更新或删除。"""
        try:
            entries = _parse(self._load())
        except OSError as exc:
            return f"错误：读取记忆文件失败 - {exc}"

        if not entries:
            return f"长期记忆为空（{self.memory_path} 尚无条目）。"

        lines = [f"长期记忆共 {len(entries)} 条（文件 {self.memory_path}）："]
        for section, stamp, text in entries:
            lines.append(f"- [{section}] {text}（{stamp}）")
        return "\n".join(lines)


def _clean_category(category: str) -> str:
    """把类别名规整成能安全当标题用的文本。

    换行、井号这类字符会把 markdown 结构搞坏，统一压掉；过长或为空时退回"通用"。
    """
    name = _CATEGORY_RE.sub(" ", category or "").strip()
    name = " ".join(name.split())
    return name[:24] or "通用"


def _parse(text: str) -> list[tuple[str, str, str]]:
    """解析记忆文件，返回 [(类别, 日期, 内容), ...]。

    容错优先：认不出的行直接跳过，而不是报错——记忆文件人和模型都可能编辑，一次手误
    不该让整份记忆读不出来。
    """
    entries: list[tuple[str, str, str]] = []
    section = "通用"
    for line in text.splitlines():
        heading = _HEADING_RE.match(line.strip())
        if heading:
            section = _clean_category(heading.group(1))
            continue
        item = _ITEM_RE.match(line.strip())
        if not item:
            continue
        body = item.group(2).strip()
        if body:
            entries.append((section, item.group(1).strip() or "-", body))
    return entries


def _render(entries: list[tuple[str, str, str]]) -> str:
    """把条目渲染回 markdown 文本。

    类别按首次出现的顺序排列，文件结构因此稳定、diff 干净，人也读得顺——每写一条就
    重排一次的话，任何一次编辑都会在 diff 里翻江倒海。
    """
    lines = [_FILE_TITLE, ""]
    sections: list[str] = []
    for section, _stamp, _text in entries:
        if section not in sections:
            sections.append(section)

    for section in sections:
        lines.append(f"## {section}")
        for entry_section, stamp, text in entries:
            if entry_section == section:
                lines.append(f"- [{stamp}] {text}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
