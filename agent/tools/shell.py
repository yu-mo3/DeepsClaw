"""Shell 命令执行工具。

给 Agent 一个能真正跑命令的出口。这个能力的下限很低：模型的一次误判就可能是
``rm -rf /`` 或者往 ``/dev/sda`` 里 ``dd``，而这两件事都没有撤销键。所以本模块
的重点不在"怎么跑命令"，而在**先拦后跑**——命令文本先过一遍 deny_patterns，
命中任意一条就直接拒绝执行。

拦截表同时覆盖 Windows（cmd / PowerShell）与 WSL Ubuntu（bash）两侧，并且刻意
宁枉勿纵：像 ``echo "别用 rm -rf"`` 这种把危险词当普通文本的调用也会被拦下。
误报的代价是模型换个说法重试，漏报的代价可能是用户的数据，两者不对等，所以
规则一律从严。删除是其中唯一的例外——只拦"能一次删掉很多"的形态（递归、通配符、
删目录），单个文件的普通 ``rm`` / ``del`` 属于日常操作，默认放行。要放宽或收紧
就往 deny_patterns 里增删条目，不必动 execute 的逻辑。

除此之外还有三道"防爆"，都是为了让"跑命令"不至于把 agent 拖死：

- **超时**：单条命令最多跑 COMMAND_TIMEOUT 秒，到点杀进程并如实告知。否则
  ``ping -t``、直接 ``python`` 这类会等输入的命令能让 agent 永远卡在同一步上；
- **输出截断**：单次最多回给模型 MAX_OUTPUT_CHARS 字符，避免一句 ``cat 大文件``
  把上下文撑爆；
- **stdin 接 DEVNULL**：命令读不到输入会自己结束，而不是静静地挂在那里。

典型用法::

    registry = ToolRegistry()
    registry.register(ExecTool("/path/to/workspace"))
    print(await registry.execute("exec", {"command": "python --version"}))
"""

import asyncio
import locale
import logging
import os
import re
from typing import Any

from agent.tools.base import Tool

logger = logging.getLogger(__name__)

#: 单条命令的最长执行时间（秒），超时即杀掉进程。
COMMAND_TIMEOUT = 60.0

#: 单次返回给模型的输出上限（字符），超出部分截断。
MAX_OUTPUT_CHARS = 10000


def _rule(label: str, pattern: str) -> tuple[str, re.Pattern[str]]:
    """编译一条拦截规则。

    统一开 IGNORECASE（``RM -RF`` 和 ``Format`` 照样要拦）和 VERBOSE
    （让正则能带缩进和行内注释，否则这一长串没人敢改）。

    Args:
        label: 危险类别，命中后原样回给模型，让它知道踩了哪条线。
        pattern: VERBOSE 模式下的正则源码，literal 空格要写成 ``\\s``。

    Returns:
        (类别, 已编译正则) 二元组。
    """
    return label, re.compile(pattern, re.IGNORECASE | re.VERBOSE)


class ExecTool(Tool):
    """在工作目录下执行 Shell 命令并返回输出的工具。

    命令交给系统默认 shell 解释（Windows 上是 ``%COMSPEC% /c``，即 cmd.exe；
    Linux / WSL 上是 ``/bin/sh -c``），标准错误合并进标准输出，因为对模型而言
    "为什么失败"和"输出是什么"同样重要，分开还得让它自己拼。

    检查顺序是先匹配拦截表、再启动进程，命中就根本不 fork——不是"跑了再拦"，
    危险命令不会有哪怕一瞬间的生效窗口。

    实例只持有只读的工作目录，可并发调用。
    """

    #: 危险命令拦截表：(危险类别, 正则)。命令文本命中任意一条即拒绝执行。
    #:
    #: 用 search 而不是 fullmatch，所以 ``cd /tmp && rm -rf /`` 这类拼接命令里的
    #: 危险片段照样会被抓出来；跨词的模式一律用 ``[^\n]*`` 连接，能容忍任意空白、
    #: 参数顺序和引号。子类想增删规则直接覆盖这个类属性即可。
    deny_patterns: list[tuple[str, re.Pattern[str]]] = [
        # 1. 删除——只拦"一次能删掉很多"的形态。单个文件的普通删除
        #    （rm a.txt / del a.txt）是日常操作，不在拦截范围内。
        _rule(
            "递归或批量删除",
            r"""
                \brm\b[^\n]*\s(?:-[a-z]*[rf]|--(?:recursive|force))(?:\s|$)  # rm -r / -f / -rf
              | \brm\b[^\n]*\*                  # rm * / rm "*.log"：通配符一次删一批
              | \b(?:del|erase)\b[^\n]*\s/[a-z] # del /f /q /s /a 等破坏性开关
              | \b(?:del|erase)\b[^\n]*[*?]     # del *.txt
              | \bRemove-Item\b[^\n]*(?:-(?:recurse|force|r|f)\b|[*?])
              | \b(?:rmdir|rd)\b                # 删目录一律拦：目录里可能还有东西
              | \b(?:shred|sdelete)\b           # 覆写式擦除，比删除更不可逆
              | \bfind\b[^\n]*\s-delete\b       # find -delete
              | \btruncate\b[^\n]*\s-s\s*0\b    # 把文件内容清零
            """,
        ),
        # 2. 格式化——建文件系统、分区表操作
        _rule(
            "格式化磁盘或分区",
            r"""
                \bmkfs(?:\.\w+)?\b              # mkfs / mkfs.ext4 / mkfs.ntfs
              | \b(?:mke2fs|newfs|mkdosfs|mkntfs)\b
              | \bformat(?:\.(?:com|exe))?\b(?=[^\n]*[a-z]:)   # Windows format C:
              | \b(?:fdisk|sfdisk|cfdisk|parted|wipefs|diskpart)\b
            """,
        ),
        # 3. 权限升级——拿到 root / Administrator 就能绕过上面所有规则
        _rule(
            "权限升级",
            r"""
                \b(?:sudo|sudoedit|doas|pkexec|gsudo|runas)\b
              | \bsu\b                          # su / su - root（\b 天然排除 sudo）
              | Start-Process[^\n]*\s-Verb\s+RunAs\b   # PowerShell 提权启动
            """,
        ),
        # 4. 关机重启
        _rule(
            "关机、重启或注销",
            r"""
                \b(?:shutdown|reboot|poweroff|logoff)\b
              | \bhalt\b
              | \binit\s+[06]\b
              | \b(?:systemctl|service)\s+(?:reboot|poweroff|halt|suspend|hibernate)\b
              | \b(?:Stop|Restart)-Computer\b
            """,
        ),
        # 5. 危险权限修改——世界可写、setuid、把属主递归改给 root
        _rule(
            "危险权限修改",
            r"""
                \bchmod\b[^\n]*(?:\b0?777\b|\b0?666\b|\b0{3,4}\b|a\+rwx|\+s\b|\bo\+w\b)
              | \bchown\b[^\n]*\s-R\b           # 递归改属主
              | \b(?:takeown|cacls)\b
              | \bicacls\b[^\n]*/(?:grant|setowner|reset)\b
              | \bumask\s+0{2,4}\b
            """,
        ),
        # 6. 打开网络后门——监听端口、反向 shell、下载即执行、放行防火墙
        _rule(
            "打开网络后门",
            r"""
                \b(?:nc|ncat|netcat|socat|cryptcat|telnetd|xinetd)\b
              | /dev/tcp/                       # bash 反向 shell 的经典写法
              | \|\s*(?:sudo\s+)?(?:(?:ba|z|k)?sh|iex)\b      # curl x | bash
              | \b(?:curl|wget|iwr|Invoke-WebRequest|Invoke-RestMethod)\b
                    [^\n]*\|[^\n]*(?:sh|bash|iex|python|perl|ruby)\b   # 下载后立刻执行
              | \bssh\b[^\n]*\s-R\b              # 反向端口转发
              | \bnetsh\b[^\n]*(?:advfirewall|portproxy)
              | \bufw\s+allow\b
              | \biptables\b[^\n]*-j\s+ACCEPT\b
              | \b(?:msfvenom|msfconsole|meterpreter)\b
              | \bNew-Object\s+System\.Net\.Sockets\b
              | \bDownloadString\b
              | \bpowershell\b[^\n]*-e(?:nc(?:odedcommand)?)?\b     # -enc 编码载荷
              | \bbase64\b[^\n]*-d[^\n]*\|       # base64 -d | sh
            """,
        ),
        # 7. 覆写设备文件与磁盘镜像
        _rule(
            "覆写设备文件或磁盘镜像",
            r"""
                \bdd\b[^\n]*of\s*=\s*(?:/dev/(?!null\b)|\\\\\.\\|[a-z]:)
              | >\s*/dev/(?:sd|hd|vd|nvme|mmcblk|loop|sr|fd|ram|mem|port|disk)\w*
              | >\s*\\\\\.\\[A-Za-z]                  # 重定向到 Windows 原始设备
              | \\\\\.\\(?:PhysicalDrive|Global)       # \\.\PhysicalDrive0
              | \bhdparm\b[^\n]*--write
              | \bmkswap\s+(?:/dev/|\\\\\.\\)
              | \bblkdiscard\b
            """,
        ),
        # 8. 覆写系统关键文件——改 /etc/passwd 加个账号的效果等同提权
        _rule(
            "覆写系统关键文件",
            r"""
                >\s*/etc/(?:passwd|shadow|gshadow|sudoers|fstab|hosts)\b
              | >\s*/(?:boot|sys|proc)/\w
              | >\s*[a-z]:\\Windows\\(?:System32|SysWOW64)\b
            """,
        ),
        # 9. 进程炸弹与杀光全部进程
        _rule(
            "进程炸弹或杀光全部进程",
            r"""
                :\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;?\s*:      # bash fork bomb
              | %0\s*\|\s*%0                                   # Windows fork bomb
              | \bkill\s+(?:-\w+\s+)*-1\b                      # 杀掉本用户全部进程
              | \bkillall5\b
            """,
        ),
    ]

    def __init__(self, workspace: str = ".") -> None:
        """初始化。

        Args:
            workspace: 命令的工作目录，可以是相对路径，内部转成绝对路径后固化，
                之后不再受进程当前目录变化的影响。
        """
        # 只用 abspath 不用 realpath：这里是把路径交给子进程当 cwd，不做沙箱边界
        # 比较，保留软链接原样反而更贴近用户在 .env 里配置的路径。
        self.workspace = os.path.abspath(workspace)

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return (
            "在工作目录下执行一条 Shell 命令，返回合并后的标准输出与标准错误。"
            "适合运行 git、python、pip、ls 等命令来查看或验证结果。"
            f"命令最长运行 {COMMAND_TIMEOUT:g} 秒，超时会被终止并返回提示；"
            f"输出超过 {MAX_OUTPUT_CHARS} 字符会被截断。"
            "递归删除、通配符批量删除、删目录、格式化、权限升级、关机重启、"
            "危险权限修改、网络后门、覆写设备或系统关键文件等危险命令会被安全策略拦截；"
            "单个文件的普通删除（rm a.txt / del a.txt）可以正常使用。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 Shell 命令，例如 python --version 或 git status",
                },
            },
            "required": ["command"],
        }

    async def execute(self, command: str) -> str:
        """执行命令并返回输出。

        Args:
            command: 命令行文本，交给系统 shell 解释。

        Returns:
            命令输出；被拦截、超时或无法启动时返回对应的错误描述文本。
        """
        blocked = self._check_denied(command)
        if blocked is not None:
            return (
                f"错误：命令被安全策略拦截（{blocked}），已拒绝执行。"
                "请换一种方式完成目标，或把具体需求告诉我。"
            )

        try:
            # create_subprocess_shell 会自动带上平台默认 shell：
            # Windows 走 %COMSPEC% /c，POSIX 走 /bin/sh -c。
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=self.workspace,
                # stdin 接 DEVNULL：读不到输入的命令会立刻结束，而不是把这一步挂死。
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            return f"错误：无法在工作目录 {self.workspace!r} 下启动命令 - {exc}"

        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            await self._kill(proc)
            return (
                f"错误：命令超过 {COMMAND_TIMEOUT:g} 秒仍未结束，已强制终止。"
                "如果是需要交互输入或长时间运行的程序，请换用非交互的参数调用。"
            )

        text = _decode_output(raw).strip()
        if not text:
            text = "（命令没有产生输出）"
        elif len(text) > MAX_OUTPUT_CHARS:
            total = len(text)
            text = (
                text[:MAX_OUTPUT_CHARS]
                + f"\n...[输出已截断：共 {total} 字符，以上为前 {MAX_OUTPUT_CHARS} 字符]"
            )

        if proc.returncode:
            # 非 0 退出不等于工具报错（grep 没匹配到也会返回 1），所以用陈述句
            # 把退出码交给模型，由它自己判断这算不算失败。
            return f"命令以退出码 {proc.returncode} 结束（非 0 通常表示失败），输出：\n{text}"
        return text

    def _check_denied(self, command: str) -> str | None:
        """检查命令是否命中拦截表。

        Args:
            command: 模型给出的完整命令文本。

        Returns:
            命中的危险类别；未命中返回 None。
        """
        for label, pattern in self.deny_patterns:
            if pattern.search(command):
                logger.warning("拦截危险命令（%s）：%s", label, command)
                return label
        return None

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process) -> None:
        """杀掉超时的子进程并回收。

        Windows 上 kill 只能结束直接的子进程，它再派生的孙进程（比如
        ``cmd /c ping -t`` 里的 ping）可能继续存活，这是平台限制，不做额外处理。
        """
        try:
            proc.kill()
        except ProcessLookupError:
            return  # 在杀之前自己退出了，没什么可做的

        try:
            await proc.wait()
        except Exception:  # noqa: BLE001 - 进程都要杀了，等待失败也没有补救手段
            logger.debug("回收超时子进程时出错", exc_info=True)


def _decode_output(raw: bytes) -> str:
    """把子进程输出解码成文本。

    先按 UTF-8 解——Linux / WSL 下的输出基本都是 UTF-8，中文 Windows 上的
    cmd.exe 则跟随控制台代码页输出 GBK，硬按 UTF-8 解会整段变乱码。所以解码
    失败时退回系统本地编码，非法字节统一用替换字符兜住，绝不因为编码问题抛异常
    把一次成功的命令变成失败。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False), errors="replace")
