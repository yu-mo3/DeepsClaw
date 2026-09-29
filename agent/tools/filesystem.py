"""文件系统工具集。

提供读文件、写文件、列目录三个工具，共同点是都被限制在一个 workspace
根目录内——模型给出的路径先经过校验，落在工作区之外的一律拦截。

沙箱逻辑集中在 _WorkspaceTool._resolve 里，三个工具共用同一份实现，
避免"改了一处漏了两处"导致某个工具偷偷变成越界通道。

典型用法::

    registry = ToolRegistry()
    for tool in (ReadFileTool("/path/to/workspace"),
                 WriteFileTool("/path/to/workspace"),
                 ListDirTool("/path/to/workspace")):
        registry.register(tool)
"""

import os
from typing import Any

from agent.tools.base import Tool

# 单次读取返回给模型的字符上限。超出部分截断，防止一个超大文件把上下文撑爆。
MAX_READ_CHARS = 16000


class _PathEscapeError(ValueError):
    """用户路径越出 workspace 边界（或无法安全判定归属）。"""


class _WorkspaceTool(Tool):
    """带工作区沙箱的文件系统工具基类。

    负责两件事，子类不必重复实现：

    1. 在构造时固化 workspace 根目录，并做归一化处理；
    2. 提供 _resolve() 把用户传入的路径解析成绝对路径，同时校验它确实落在
       workspace 内，越界则抛 _PathEscapeError。

    子类只需在 execute 里调用 self._resolve()，把 _PathEscapeError 转成错误
    字符串返回即可，不需要自己写路径判断。
    """

    def __init__(self, workspace: str) -> None:
        """初始化工作区。

        Args:
            workspace: 工作区根目录，可以是相对路径，内部会解析成绝对路径。
        """
        # realpath 解析符号链接，保证比较时两边都是"真实"路径——否则工作区内
        # 一个指向外部的软链接就能绕过校验。
        self.workspace = os.path.realpath(workspace)
        # normcase 在 Windows 上会转小写并统一分隔符，在 POSIX 上是恒等操作。
        # 只用于比较，不用于实际文件操作，所以单独存一份。
        self._workspace_key = os.path.normcase(self.workspace)

    def _resolve(self, user_path: str) -> str:
        """把用户路径解析为 workspace 内的绝对路径，越界则拒绝。

        校验用的是 commonpath 而不是 startswith：字符串前缀比较会把
        ``/ws-evil`` 误判成在 ``/ws`` 之内（详见下方注释）。

        Args:
            user_path: 模型传入的相对路径；也接受工作区内的绝对路径。

        Returns:
            可直接用于文件操作的绝对路径。

        Raises:
            _PathEscapeError: 路径落在 workspace 之外，或跨盘符无法比较。
        """
        # user_path 是绝对路径时 join 会忽略 workspace，交给后面的校验兜住。
        absolute = os.path.realpath(os.path.join(self.workspace, user_path))
        key = os.path.normcase(absolute)

        try:
            inside = os.path.commonpath([self._workspace_key, key]) == self._workspace_key
        except ValueError:
            # Windows 上两个不同盘符的路径无法求公共前缀，直接判定为越界。
            inside = False

        if not inside:
            raise _PathEscapeError(f"路径 {user_path!r} 超出工作区范围，已拦截")

        return absolute


class ReadFileTool(_WorkspaceTool):
    """读取工作区内文本文件的工具。

    以 UTF-8 读取（非法字节用替换字符兜住，不会因编码问题直接失败），
    内容超过 MAX_READ_CHARS 字符时截断并在末尾注明原始长度，让模型知道
    自己看到的是不完整的——否则它可能基于残缺内容做出错误判断。
    """

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "读取工作区内一个文本文件的完整内容。"
            f"单次最多返回 {MAX_READ_CHARS} 个字符，超出部分会被截断并在末尾标注；"
            "如需了解大文件全貌，可先用 list_dir 查看文件大小。仅支持文本文件。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件路径，相对于工作区根目录，例如 src/main.py",
                },
            },
            "required": ["file_path"],
        }

    async def execute(self, file_path: str) -> str:
        try:
            path = self._resolve(file_path)
        except _PathEscapeError as exc:
            return f"错误：{exc}"

        if os.path.isdir(path):
            return f"错误：{file_path!r} 是目录，请改用 list_dir 查看其内容"

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as exc:
            return f"错误：读取 {file_path!r} 失败 - {exc}"

        if not content:
            return f"（{file_path!r} 是空文件）"

        if len(content) > MAX_READ_CHARS:
            total = len(content)
            content = (
                content[:MAX_READ_CHARS]
                + f"\n\n...[内容已截断：文件共 {total} 字符，以上为前 {MAX_READ_CHARS} 字符]"
            )

        return content


class WriteFileTool(_WorkspaceTool):
    """写入工作区内文本文件的工具。

    父目录不存在时自动创建，因此模型可以直接写出 ``a/b/c.py`` 这样的深层
    路径，不必先调一次建目录的工具。写入是整文件覆盖，不做追加。
    """

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "把文本内容写入工作区内的文件，父目录不存在会自动创建。"
            "注意是覆盖写入：目标文件已存在时，原有内容会被完整替换。"
            "写入前如需保留原内容，请先用 read_file 读取。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文件路径，相对于工作区根目录，例如 src/main.py",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的完整文本内容",
                },
            },
            "required": ["file_path", "content"],
        }

    async def execute(self, file_path: str, content: str) -> str:
        try:
            path = self._resolve(file_path)
        except _PathEscapeError as exc:
            return f"错误：{exc}"

        parent = os.path.dirname(path)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as exc:
                return f"错误：创建目录 {parent!r} 失败 - {exc}"

        try:
            # newline="" 关掉换行符转换，保证写出的字节与传入内容完全一致。
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(content)
        except OSError as exc:
            return f"错误：写入 {file_path!r} 失败 - {exc}"

        return f"已写入 {file_path!r}（{len(content)} 字符）"


class ListDirTool(_WorkspaceTool):
    """列出工作区内某个目录内容的工具。

    每个条目：目录名带 ``/`` 后缀，文件名附带人类可读的大小，两者都能让
    模型快速判断下一步该读哪个文件、该不该直接读。结果按名称排序，保证
    同样的目录每次输出一致（也便于前缀缓存命中）。
    """

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return (
            "列出工作区内指定目录的条目，返回名称与大小，按名称排序。"
            "目录名以 / 结尾。不传参数时列出工作区根目录，"
            "适合用来先摸清项目结构再决定读哪些文件。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "dir_path": {
                    "type": "string",
                    "description": "目录路径，相对于工作区根目录，默认为工作区根目录",
                },
            },
            "required": [],
        }

    async def execute(self, dir_path: str = ".") -> str:
        try:
            path = self._resolve(dir_path)
        except _PathEscapeError as exc:
            return f"错误：{exc}"

        if not os.path.exists(path):
            return f"错误：目录 {dir_path!r} 不存在"
        if not os.path.isdir(path):
            return f"错误：{dir_path!r} 不是目录，请改用 read_file 读取"

        try:
            with os.scandir(path) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            return f"错误：读取目录 {dir_path!r} 失败 - {exc}"

        if not entries:
            return f"目录 {dir_path!r} 为空"

        lines = [_format_entry(e) for e in entries]
        dirs = sum(1 for e in entries if _safe_is_dir(e))
        lines.append(f"\n共 {len(entries)} 项（{dirs} 个目录，{len(entries) - dirs} 个文件）")
        return "\n".join(lines)


def _safe_is_dir(entry: os.DirEntry) -> bool:
    """判断 DirEntry 是否为目录，遇到失效链接/权限问题一律当普通文件处理。"""
    try:
        return entry.is_dir()
    except OSError:
        return False


def _format_entry(entry: os.DirEntry) -> str:
    """把一个目录条目格式化成一行输出。"""
    if _safe_is_dir(entry):
        return f"{entry.name}/"
    try:
        return f"{entry.name:<40} {_format_size(entry.stat().st_size)}"
    except OSError:
        # 失效的符号链接等取不到大小的情况，用 ? 占位而不是让整个列表失败。
        return f"{entry.name:<40} ?"


def _format_size(num_bytes: int) -> str:
    """把字节数转成人类可读的大小，例如 1.2 KB。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
