"""本地终端的渠道实现。

把终端当渠道：键盘输入 = 收到消息，打印回复 = 发消息。接上总线之后，CLI 与飞书、
QQ 在 agent 眼里没有区别——同一条 inbound_queue、同一条 outbound_queue，因此 agent
完全不必知道"这次是谁在跟我说话"。

一次问答的来回是**接力**式的，不是各跑各的：

    start()  ──读入一行──▶ publish_inbound ──▶ 挂起等 _response_event
                                                      │
                        agent 取走、跑完一轮，把回复推回总线
                                                      ▼
    下一行输入 ◀── 回到循环顶 ◀── _response_event.set() ◀── send() 打印回复

为什么要这个 Event：agent 跑一轮可能要几十秒，期间必须让终端空着等，而不是继续读
输入。若能继续读，用户敲的第二句会挤进带上下文的同一轮里，两次提问的回复叠成一团。
Event 正是把"先答完前一句"这件事变成一次 await——它不由渠道自己 set，而是由 send()
在回复真的打印完之后 set，所以"看到回答"和"能再提问"天然是同一时刻。

典型用法::

    bus = MessageBus()
    channel = CLIChannel(bus)
    channel.tool_names = agent.registry.list_tools()   # 由装配处注入，见下
    exit_code = await channel.start()

**本模块不认识 AgentLoop**：工具列表和清历史都由 main.py 在创建之后注入（tool_names
与 _clear_callback 两个属性），CLI 只负责把命令翻译成调用。这样渠道可以被单独启动
（配一个假总线就能测），也不会出现"渠道反向依赖 agent"的绕圈依赖。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from typing import Callable, TextIO

from bus.queue import InboundMessage, MessageBus, OutboundMessage
from channels.base import Channel

logger = logging.getLogger(__name__)

#: 用户输入提示符。与 REPLY_PREFIX 一样，选"人/机器"两个不同的前缀，是为了让长对话里
#: 一眼就能分清哪句是谁说的——纯靠颜色区分在日志、重定向、截图里都会失效。
PROMPT = "你 > "

#: 助手回复的前缀。回复走 stdout（与界面分流），前缀固定加在这里。
#:
#: 刻意**不带 ANSI 颜色**：stdout 会被用户重定向成文件（python main.py > answer.txt），
#: 那时前缀里混进转义码既难看又碍事。区分靠"前缀本身就是个明确标记"就够了。
REPLY_PREFIX = "AI > "

#: 退出并让进程返回 0 的命令。带上 /quit 是因为打字习惯因人而异，多认一个不费事。
EXIT_COMMANDS = ("/exit", "/quit")

#: 清空当前会话历史：由装配处注入的回调负责，CLI 自己不知道历史存在哪。
CLEAR_COMMANDS = ("/clear", "/reset")

#: 打印已装配的工具名。
TOOLS_COMMAND = "/tools"

HELP_TEXT = """\
本地命令（以 / 开头，不会发给模型）：
  /help    显示这份帮助
  /clear   清空会话历史（同时删除磁盘上的会话文件）
  /reset   同 /clear
  /tools   列出已装配的工具
  /exit    退出程序（等同于 /quit，Ctrl+C 同效）

其他任何输入都会作为问题发给模型，等回复打印完才会再次出现提示符。

屏幕上几种输出的区别：
  你 >     你输入的问题
  AI >     模型给你的回答（stdout，重定向到文件拿到的就是这些）
  灰色内容  思考过程、工具调用、错误提示，都是过程信息，不属于回答"""

HELP_COMMANDS = ("/help", "/h")

#: 消息里的 chat_id。终端只有一个会话，用固定值即可——会话键因此稳定成 cli:direct，
#: 和 session.manager 里那套 "渠道:会话" 的命名对得上。
CHAT_ID = "direct"

#: 发送者标识。终端背后只有本机用户，没有账号体系，固定一个值就够。
SENDER_ID = "local"


class CLIChannel(Channel):
    """终端渠道：读一行、发一条、等回复、再读下一行。

    用**接力**而不是"读输入"和"打印回复"两个协程各跑各的，是因为终端是块共享画布：
    回复要逐字打出来，用户又可能在回复打到一半时敲下一句。分两个协程就得自己保证
    打字机输出中间不插进提示符，而接力式把这件事变成了顺序执行的必然结果——
    start() 在等回复时根本不读键盘。

    可注入的依赖（out / err / _read_line 三个参数）只为测试存在：装配时全用默认值，
    测试里塞一个 io.StringIO 和一份写死的输入，就能把整段交互跑完，不必去 monkeypatch
    全局的 sys.stdin。
    """

    def __init__(
        self,
        bus: MessageBus,
        out: TextIO | None = None,
        err: TextIO | None = None,
        read_line: Callable[[str], str] | None = None,
    ) -> None:
        """初始化。

        Args:
            bus: 消息总线。
            out: 回复与命令提示的输出流，默认 sys.stdout。可注入以方便测试。
            err: 横幅、提示符、EOF 换行这些"程序界面"的输出流，默认 sys.stderr。
                分流的原因和 main.py 一样：stdout 上只留模型回答，重定向到文件才是
                干净的。
            read_line: 读一行的函数，默认内置 input。签名是"提示符 -> 一行文本"，
                会被丢进线程执行（见 start）。测试里换成返回脚本化内容的函数即可。
        """
        super().__init__("cli", bus)
        self._out = sys.stdout if out is None else out
        self._err = sys.stderr if err is None else err
        self._read_line = input if read_line is None else read_line

        #: 回复到达的信号：send() 里 set，start() 里 clear + wait。
        #: 初值是**未置位**——还没问就问"回了吗"，答案只能是没回。
        self._response_event = asyncio.Event()

        #: 已装配的工具名，由 main.py 创建后注入。默认空列表而不是 None：/tools 和
        #: 横幅都直接遍历它，调用点不必判空。
        self.tool_names: list[str] = []

        #: 清空历史的回调，由 main.py 注入（通常是 agent.clear_history）。签名是
        #: "怎么清历史是 agent 的事"——CLI 只负责在收到 /clear 时喊一声。
        self._clear_callback: Callable[[], None] | None = None

        #: stop() 置位的退出意图。start() 的等待点是 await，只有置位加 set 一起做，
        #: 它才能从"等回复"里醒过来（见 stop）。
        self._stopping = False

        #: 启动横幅文本，由装配处注入（main.py 的 _print_banner 生成）。
        #:
        #: 为什么由渠道来打、而不是入口打：**横幅要等到渠道真正就绪时再出现**。早打的
        #: 话，后面各渠道启动时刷出的日志会紧跟在提示符后面（"你 > 10:42:20 [INFO] QQ
        #: 渠道启动中…"），看起来像提示符被日志吃掉了，用户根本不知道现在能不能输入。
        #: 放在这里，横幅与第一个提示符之间不会插进任何别的输出。
        self._banner: str = ""

    @property
    def terminal_width(self) -> int:
        """终端宽度（列）。非终端（重定向、管道）时给 72 列的兜底值。

        暴露出来是给装配处生成横幅用的：横幅在渠道这里打印，宽度也该由渠道说了算，
        否则生成横幅时探测到的只是那根管道（见 main._print_banner 的说明）。
        """
        try:
            return shutil.get_terminal_size((72, 24)).columns
        except Exception:  # noqa: BLE001 - 探测失败不该影响启动
            return 72

    @property
    def waiting_for_reply(self) -> bool:
        """是否正卡在"等回复"上。

        给装配处和测试一个只读的观察点：为 True 说明这一轮还没答完，此时任何写
        stdout 的动作都会和打字机输出抢行。
        """
        return not self._response_event.is_set()

    async def start(self) -> int:
        """跑终端交互循环，直到用户退出或输入结束。

        输入用 ``asyncio.to_thread`` 丢给线程执行：input() 是阻塞调用，直接在这里
        调会把整个事件循环钉住——同循环里的 agent 协程再也没机会跑，用户敲下问题后
        永远等不到回复。丢进线程后，读键盘期间循环仍然活着，agent 照常干活。

        读入和等待回复串行发生（读一行 → 等这一行的回复 → 再读下一行），所以同一
        时刻只会有一个线程卡在 input() 上。

        Returns:
            退出码：0 正常退出（/exit、/quit、Ctrl+D）。返回值与 main.py 的约定一致，
            装配处可以直接拿它当进程退出码。
        """
        # 就绪了才亮出横幅和提示符，见 _banner 的说明。
        if self._banner:
            print(self._banner, file=self._err, flush=True)

        while not self._stopping:
            try:
                text = (await asyncio.to_thread(self._read_line, PROMPT)).strip()
            except EOFError:
                # Ctrl+D / Ctrl+Z 结束输入。补一个换行：非终端输入（管道）不会自动换行，
                # 不补的话后面的输出会接在提示符那一行上。
                print(file=self._err, flush=True)
                return 0

            if not text:
                continue  # 空行只是回车，不算提问

            if text.startswith("/"):
                if self._handle_command(text):
                    return 0
                continue

            # 先清掉上一轮的置位再发消息：Event 一旦 set 就一直是 set 状态，不清的话
            # 下面的 wait() 会立刻返回，第二句提问就不会等回复了。
            self._response_event.clear()
            await self.bus.publish_inbound(
                InboundMessage(
                    channel=self.name,
                    sender_id=SENDER_ID,
                    chat_id=CHAT_ID,
                    content=text,
                )
            )
            # 把控制权交出去，直到 send() 把回复打完。agent 正是在这段时间里被调度、
            # 取走上面的消息的。
            await self._response_event.wait()

        return 0

    async def send(self, message: OutboundMessage) -> None:
        """打印一条回复，并放行下一行输入。

        set 放在打印**之后**：先从等待中醒来再去打印，用户就会看到提示符比回答先出现。

        Args:
            message: 要显示的回复。
        """
        print(f"{REPLY_PREFIX}{message.content}", file=self._out, flush=True)
        self._response_event.set()

    async def stop(self) -> None:
        """收工：让卡在"等回复"的 start() 醒过来。

        CLI 没有连接要关，但**必须 set 一次**：start() 若正停在 await 上，光置
        _stopping 是没人看得到的——它已经不在循环顶部了，要等下一轮才有机会检查。
        set 把它唤醒，它回到循环顶部看见 _stopping，正常返回 0。

        终端里按 Ctrl+C 退出走的是 main.py 的 KeyboardInterrupt 路径，不经过这里。
        """
        self._stopping = True
        self._response_event.set()

    def _handle_command(self, text: str) -> bool:
        """处理一条以 / 开头的本地命令。

        Args:
            text: 用户输入的整行，已 strip。

        Returns:
            True 表示该退出，False 表示继续下一轮。
        """
        command = text.split()[0].lower()

        if command in EXIT_COMMANDS:
            return True

        if command in CLEAR_COMMANDS:
            if self._clear_callback is not None:
                self._clear_callback()
                print("会话历史已清空。", file=self._out, flush=True)
            else:
                # 兜底：没注入回调说明这个渠道是单独跑起来的（测试、或将来别处复用），
                # 不说清楚的话用户会以为历史真被清了。
                print("当前未接会话管理，历史未清空。", file=self._err, flush=True)
            return False

        if command == TOOLS_COMMAND:
            if self.tool_names:
                print("可用工具：" + "、".join(self.tool_names), file=self._out, flush=True)
            else:
                print("没有装配任何工具。", file=self._out, flush=True)
            return False

        if command in HELP_COMMANDS:
            print(HELP_TEXT, file=self._out, flush=True)
            return False

        # 不认识的 / 命令当成命令处理并提示，而不是原样发给模型：模型看到 "/wat" 只会
        # 正经回答一通，用户反而更难发现是命令打错了。
        print(f"未知命令 {command}，输入 /help 查看可用命令。", file=self._err, flush=True)
        return False