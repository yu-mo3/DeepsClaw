"""DeepsClaw 命令行入口：装配多渠道网关。

程序里只有这一处知道"从哪来到哪去"：其余各层都只认自己那一小块接口，把哪些实现
接进哪个位置是这里的事。

    CLIChannel ─▶ inbound_queue ─▶ Gateway._process_inbound
                                        │  session_key = "cli:local"
                                        ▼
                              AgentLoop（每会话一个，按 key 缓存）
                                        │
       CLIChannel.send() ◀── Gateway._dispatch_outbound ◀── outbound_queue

于是加一个渠道这件事，对 agent、工具、模型这几层是**完全不可见**的：写一个新的
Channel 子类，加进 channels 列表就完了（见 channels/base.py 的两条约定）。

    python main.py                                # 交互模式，可连续追问
    echo "看看工作区有哪些文件" | python main.py     # 一次性提问

模块文档里那条"stdout 只有回答"的约定仍然成立，但**打印回复的人换成了渠道**：
以前是 ConsoleSink 把流式正文写到 stdout，现在由 Gateway 把 agent 的返回值发布成
OutboundMessage，交给 CLIChannel 打印。所以 agent 不再挂那个会写 stdout 的 sink，
只挂 TerminalSink 负责思考与工具（走 stderr）——否则同一句话会被打两遍。

思考过程之所以不再逐字流式，是因为 sink 是"每条会话一个"的东西，而渠道式的架构里
回复要经过总线：agent 的返回值才是权威正文，流式增量没有一条回总线的路。终端上看
到的思考仍是完整的，只是按整块而非逐字吐出。要改回逐字流式，得让 sink 也能发
OutboundMessage，那是另一套设计（见 Gateway 模块文档末尾的取舍）。
"""

import asyncio
import io
import json
import logging
import os
import shutil
import sys
import unicodedata
from typing import TextIO

from agent.context import ContextBuilder
from agent.events import (
    REASON_CANCELLED,
    REASON_ERROR,
    REASON_FINISHED,
    REASON_LOOP_BREAK,
    REASON_MAX_STEPS,
    AgentEvent,
    ErrorEvent,
    OutputSink,
    ThinkingDelta,
    ToolCallEvent,
    ToolResultEvent,
    TurnEndEvent,
)
from agent.loop import AgentLoop
from agent.memory import MemoryConsolidator
from agent.skills import SkillsLoader
from agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
from agent.tools.memory import MemoryTool
from agent.tools.registry import ToolRegistry
from agent.tools.shell import ExecTool
from agent.tools.spawn import SpawnSubagentTool
from agent.tools.web_fetch import WebFetchTool
from agent.tools.web_search import WebSearchTool
from bus.queue import MessageBus
from channels.base import Channel
from channels.cli import CLIChannel
from config import Config, DATA_MEMORY_FILE
from gateway import Gateway
from providers.openai_compat import OpenAICompatProvider
from session.manager import SessionManager

logger = logging.getLogger("DeepsClaw")


#: 终端 ANSI 颜色。只在"程序界面"（横幅、思考、工具调用）上用，且一律走 stderr，
#: 因此不会污染 `python main.py > answer.txt` 拿到的纯文本回答。
#: 重定向到文件时它们只是几个不可见字节，不影响可读性。
class C:
    """一组够用的 ANSI 转义：只为了让人一眼分得清"界面"与"回答"。"""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    VALUE = "\033[97m"

#: 本地命令与输入提示符的定义现在归渠道所有：/help、/clear、/tools、/exit 都在
#: channels/cli.py 里，那里的 HELP_TEXT 是唯一一份，本模块不再重复一遍。


def _setup_console() -> None:
    """Windows 下输出被重定向或走管道时切到 UTF-8，避免中文乱码。

    真实控制台本身已经是 UTF-8，重复设一次是空操作，所以不必先判断。
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _setup_logging(level: str) -> None:
    """配置日志，并把第三方库在 INFO 级别的刷屏压掉。

    日志走 stderr 是为了让 stdout 只剩模型回复，这样才能直接重定向到文件。
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    if logging.getLogger().level <= logging.DEBUG:
        return  # 调成 DEBUG 就是来看请求细节的，不做压制
    # httpx 和 httpx2 是两个不同的 logger：openai SDK 3.x 用的 httpx 是改名后的包。
    #
    # botpy（QQ SDK）也在这里：它在连接时会刷一长串 INFO（登录中、连接中、鉴权中、心跳
    # 维持启动……），而这些行正好落在"你 > "提示符之后，把提示符顶得看不见，用户根本
    # 不知道现在能不能输入。真想看这些细节时把 LOG_LEVEL 调成 DEBUG，压制会自动让路。
    for noisy in ("httpx", "httpx2", "httpcore", "openai", "botpy"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class TerminalSink(OutputSink):
    """终端上的"过程显示"：思考、工具调用与错误，一律写 stderr。

    **回复不归它管**——正文由 Gateway 发布成 OutboundMessage，交给 CLIChannel 打印，
    这里再打一遍就会出现两份一模一样的回答。所以本类相对早期的 ConsoleSink 少了
    stdout 那一侧：它只负责"agent 在干什么"，不负责"agent 说了什么"。

    分流规则没变：问答之外的诊断信息全走 stderr，于是
    `python main.py > answer.txt` 拿到的仍是干净的回答。

    只负责呈现，不改变任何行为：漏打某条事件不影响 agent 的返回值与会话历史。
    """

    #: 思考块的标题。缩进两格再配灰色，是为了让"过程"整体退到背景里。
    THINKING_HEADER = "  ── 思考 ──"

    #: 工具调用与结果的行首。与思考一样属于"过程"，不进 stdout。
    TOOL_CALL_MARK = "[工具]"
    TOOL_RESULT_MARK = "[结果]"
    ERROR_MARK = "[错误]"

    def __init__(self, err: TextIO | None = None) -> None:
        """初始化。

        Args:
            err: 思考与诊断信息的输出流，默认 sys.stderr。可注入以方便测试——用
                io.StringIO 断言，不必去 monkeypatch 全局的 sys.stderr。
        """
        self._err = sys.stderr if err is None else err
        # 思考块的标题只在块首打一次，之后的分片直接续写。
        self._thinking_open = False

    async def emit(self, event: AgentEvent) -> None:
        """按事件类型分派：思考原样续写，工具与错误各压一行。"""
        if isinstance(event, ThinkingDelta):
            self._write_thinking(event.text)
            return

        # 任何非思考内容都不该和思考挤在同一行，先给思考块收尾。
        self._close_thinking()

        if isinstance(event, ToolCallEvent):
            arguments = json.dumps(event.arguments, ensure_ascii=False)
            self._write_err(
                f"{C.CYAN}{self.TOOL_CALL_MARK}{C.RESET} {event.name}({arguments})"
            )
        elif isinstance(event, ToolResultEvent):
            self._write_err(self._format_tool_result(event))
        elif isinstance(event, ErrorEvent):
            self._write_err(f"{C.RED}{self.ERROR_MARK}{C.RESET} {event.message}")
        elif isinstance(event, TurnEndEvent):
            self._write_turn_end(event)

    def _write_thinking(self, text: str) -> None:
        """思考增量写 stderr，块首补一行标题。"""
        if not self._thinking_open:
            self._write_err(f"{C.DIM}{self.THINKING_HEADER}{C.RESET}")
            self._thinking_open = True
        self._err.write(text)
        # 不 flush 在 Windows 上就是假流式：内容会攒在缓冲区里，等下一块才吐。
        self._err.flush()

    def _write_turn_end(self, event: TurnEndEvent) -> None:
        """收尾：按结局补上那些不走正文的诊断提示。

        **绝不重打 reply**。正常结束时正文已由渠道打印；熔断、步数耗尽这几条出口的
        说明文字虽然会经由 agent 的返回值原样发给用户，但那是渠道的事，这里再打
        一遍就成了两份。只有错误与取消要说一句——它们的文案在 ErrorEvent 与中断
        提示里已经给过，也不再重复。
        """
        if event.reason in (REASON_LOOP_BREAK, REASON_MAX_STEPS):
            self._write_err(f"{C.DIM}[本轮任务被中止，原因见下面的 AI > 回答]{C.RESET}")
            return

        if event.reason in (REASON_ERROR, REASON_CANCELLED):
            return  # 文案已由 ErrorEvent / 中断提示打过，重复说一遍只是噪声

        if event.reason == REASON_FINISHED and not event.reply:
            # 模型一个字都没返回：渠道那边会兜一个占位文本，这里不再多嘴。
            return

    @classmethod
    def _format_tool_result(cls, event: ToolResultEvent) -> str:
        """把工具结果压成一行摘要，超长预览只留开头。

        失败的调用（工具返回的文本以"错误："开头）用红色标出来：一屏几十行过程信息里，
        "哪一步栽了"是排查时最先要找的东西，让它自动显眼比让用户逐行读划算。
        """
        preview = event.result.strip().replace("\n", " ⏎ ")
        if len(preview) > 200:
            preview = preview[:200] + "…"
        suffix = f"（共 {event.length} 字符，已截断）" if event.truncated else ""
        colour = C.RED if event.result.lstrip().startswith("错误") else C.GREEN
        return (
            f"{colour}{cls.TOOL_RESULT_MARK}{C.RESET} "
            f"{event.name} → {colour}{preview}{suffix}{C.RESET}"
        )

    def _close_thinking(self) -> None:
        """给思考块补一个换行，免得后面的内容接在同一行上。"""
        if not self._thinking_open:
            return
        self._err.write("\n")
        self._err.flush()
        self._thinking_open = False

    def _write_err(self, text: str) -> None:
        print(text, file=self._err, flush=True)


def _setup_logging_done() -> None:
    """占位：保留 _setup_logging 的调用点不动。"""
    return None


def _build_session_manager(cfg: Config) -> SessionManager:
    """建会话持久化器。

    会话文件放在**工作区**内的 workspace/sessions 下，而不是项目根目录：运行时数据
    与代码分开，迁移工作区时历史跟着走，也不会污染仓库根。
    """
    return SessionManager(os.path.join(str(cfg.workspace), "workspace", "sessions"))


def _build_subagent_provider(cfg: Config, model: str | None) -> OpenAICompatProvider:
    """造一个子智能体用的 Provider（默认走阿里云百炼）。

    每次派生都新建一个**独立实例**，而不是复用主 agent 那个：子任务的模型与服务商都
    可以和主对话不同（本项目的默认装配就是如此），密钥与 base_url 也跟着不同，共用一份
    连接池只会把两套凭据搅在一起。子 agent 跑完就关，不长期占着连接。

    extra_body 刻意不透传：主模型那份是给 DeepSeek 思考模式准备的开关，百炼并不认，
    原样带过去会被接口直接拒掉。

    Args:
        cfg: 运行配置。
        model: spawn_subagent 按次指定的模型名；为 None 时用配置里的默认子智能体模型。

    Returns:
        一个尚未使用的 Provider，调用方负责用完 aclose。
    """
    return OpenAICompatProvider(
        api_key=cfg.subagent_api_key or "",
        base_url=cfg.subagent_base_url,
        model=model or cfg.subagent_model,
        timeout=cfg.timeout,
    )


def _build_registry(cfg: Config) -> ToolRegistry:
    """装配工具注册表。**整个进程只建一个**。

    工具实例是无状态的（同一次会话里可能被并发调用，实现上就要求它们不存状态），
    所以一个注册表喂给所有会话的 agent 是安全的。整进程一份还有个实际好处：它同时
    是"这个进程到底装了什么工具"的唯一真相，横幅和 /tools 都从它这里取，不会出现
    "打印的和真正加载的不是同一批"。

    Args:
        cfg: 运行配置，其中的路径与密钥决定各工具的参数。

    Returns:
        注册好全部工具的注册表，注册顺序即 /tools 的显示顺序。
    """
    registry = ToolRegistry()
    workspace = str(cfg.workspace)
    tools = (
        ReadFileTool(workspace),
        WriteFileTool(workspace),
        ListDirTool(workspace),
        ExecTool(workspace),
        WebSearchTool(cfg.bocha_api_key),
        WebFetchTool(),
        # 长期记忆的写入侧：路径只来自配置，模型无法指定写到别处。
        MemoryTool(str(cfg.memory_file)),
    )
    for tool in tools:
        registry.register(tool)

    # 子智能体：**配了密钥才注册**。它是个"有构造参数"的工具（要注入 provider 工厂），
    # 没法像上面那样塞进元组统一加——位置不同，理由也不同，单独一段更清楚。
    if cfg.subagent_api_key:
        registry.register(
            SpawnSubagentTool(
                provider_factory=lambda model: _build_subagent_provider(cfg, model),
                tools_registry=registry,
                workspace=workspace,
            )
        )
        logger.info(
            "已启用子智能体工具 spawn_subagent（%s @ %s）",
            cfg.subagent_model,
            cfg.subagent_base_url,
        )
    else:
        logger.debug("未配置 SUBAGENT_API_KEY，spawn_subagent 不可用")
    return registry


def _build_agent(
    cfg: Config,
    provider: OpenAICompatProvider,
    registry: ToolRegistry,
    session_manager: SessionManager,
    session_key: str,
    sink: OutputSink | None = None,
) -> AgentLoop:
    """按配置装配一个会话的 AgentLoop。**每个会话各建一个**。

    session_key 是这里的必填参数而不是常量：一个进程里同时住着好几个会话，谁的历史
    写进哪个文件全看它。缺了它就会所有渠道的人共用一个历史，串台。

    技能在这里加载而不是在 ContextBuilder 里：技能目录扫一次就够，装载时算好摘要
    传进去，每轮拼 prompt 直接用现成字符串，不必把磁盘扫描摊到每次对话上。技能摘要
    与身份文件是**全局的**，所以每次新建会话重复读一遍稍显浪费；等将来会话多了，
    可以把它提到 gateway 外面缓存。现在按最直白的写法来。

    Args:
        cfg: 运行配置。
        provider: 模型客户端。整个进程共用这一个——它内部有连接池，每个会话各建一个
            会白白多出好几条长连接。
        registry: 工具注册表，同样进程共用。工具无状态，各会话共享一份即可；它也决定
            模型能调用哪些工具，每个会话拿到的是同一批。
        session_manager: 会话持久化器，多个 agent 共用（它自己没有状态）。
        session_key: 本会话的标识，决定读写的 JSONL 文件。
        sink: 过程事件的接收方，为 None 时 agent 什么都不往外推（测试方便）。
    """
    workspace = str(cfg.workspace)
    skills = SkillsLoader(os.path.join(workspace, "skills"))
    context = ContextBuilder(
        workspace=workspace,
        identity_file=cfg.identity_file,
        skills_summary=skills.build_skills_summary(),
        memory_file=str(cfg.memory_file),
    )

    # 历史压缩：只有配了 token 预算（AGENT_TOKEN_BUDGET）才开启，否则传 None，
    # 行为与从前完全一致。摘要用的 provider 就是同一个模型客户端。
    consolidator = (
        MemoryConsolidator(provider, workspace, token_budget=cfg.token_budget)
        if cfg.token_budget
        else None
    )

    return AgentLoop(
        provider,
        registry,
        context,
        max_steps=cfg.max_steps,
        sink=sink,
        session_manager=session_manager,
        session_key=session_key,
        consolidator=consolidator,
    )


def _display_width(text: str) -> int:
    """算一段文字在终端里占几列。

    中文字符占两列，len() 只数了 1，于是"用 len 补齐再画边框"的结果就是右边框随着
    中文个数忽长忽短。表意文字（W/F，即中文、日文、全角标点）按 2 列算，其余按 1 列。

    Args:
        text: 待测量的文本（不含 ANSI 颜色码）。

    Returns:
        该文本占用的终端列数。
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _print_banner(cfg: Config, registry: ToolRegistry, width: int | None = None) -> str:
    """生成启动横幅文本（**返回**而不直接打印）。密钥绝不出现。

    为什么是"返回文本"而不是就地打印：横幅要等**渠道就绪**时才出现。早打的话，后面
    各渠道启动刷出的日志会紧跟在提示符后面（"你 > 10:42:20 [INFO] QQ 渠道启动中…"），
    看起来像提示符被日志吃掉了，用户根本不知道现在能不能输入。所以这里只生成，由
    CLIChannel 在自己的 start() 里、打印提示符之前打出来（见它的 _banner 属性）。

    排版按**显示宽度**对齐（见 _display_width）：中文占两列，直接按 len 补空格会让
    右边框参差不齐。

    Args:
        cfg: 运行配置。
        registry: **已经装好工具的**注册表。必须传真的那一个进来：早期版本在这里就地
            新建了一个空注册表，于是横幅永远显示"可用工具："后面空着，而 /tools 也
            跟着报"没有装配任何工具"——打印的和实际加载的不是同一批，比不打印更误导。
        width: 目标终端宽度（列）。为 None 时自行探测，探测不到用 72。横幅是先生成、
            后打印的，生成时若去问 shutil.get_terminal_size，问到的只是当前那根管道，
            所以调用方（渠道）应当把它知道的真实宽度传进来。

    Returns:
        带 ANSI 颜色的横幅整段文本。
    """
    if width is None:
        try:
            width = shutil.get_terminal_size((72, 24)).columns
        except Exception:  # noqa: BLE001 - 取不到宽度不是错误，退回默认值
            width = 72
    inner = max(40, min(width - 2, 96))  # 留出两侧留白，过宽反而不好看

    err = io.StringIO()

    def line(label: str, value: str, colour: str = C.VALUE) -> None:
        """画一行"标签 + 值"。标签按**显示宽度**补齐，中文才不会把值挤歪。"""
        gap = max(1, 5 - _display_width(label))
        print(f"  {C.DIM}{label}{C.RESET}{' ' * gap}{colour}{value}{C.RESET}", file=err)

    def fit(value: str, limit: int) -> str:
        """按显示宽度截断，超出时以 … 结尾。"""
        if _display_width(value) <= limit:
            return value
        kept, used = "", 0
        for ch in value:
            step = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if used + step > limit - 1:
                break
            kept += ch
            used += step
        return kept + "…"

    skills = 0
    if cfg.workspace.is_dir():
        skills = len(SkillsLoader(os.path.join(str(cfg.workspace), "skills")).list_skills())

    # 顶框：标题居中。三段（左横线 + 标题 + 右横线）的显示宽度加起来必须正好是 inner，
    # 否则边框右边对不齐——中文按 2 列算，这里不能直接用 len。
    title = "DeepsClaw"
    pad = max(0, inner - _display_width(title) - 2)
    left = pad // 2
    print(f"{C.DIM}┌{'─' * left}{C.RESET}{C.BOLD}{C.CYAN}{title}{C.RESET}"
          f"{C.DIM}{'─' * (pad - left)}┐{C.RESET}", file=err)

    line("模型", cfg.model)
    line("工作区", fit(str(cfg.workspace), inner - 6))
    # 子智能体单独一行、不进下面的工具清单：它是"随配置开关"的能力，值得一眼看到，
    # 而不是在工具名列表里被宽度截断掉（它还排在最后）。
    tools = [name for name in registry.list_tools() if name != "spawn_subagent"]
    # 工具清单：越靠后的越优先保留，铺不下就在前面用 … 收尾。
    #
    # 为什么要"从末尾往前铺"而不是从头往后截：末尾那几项是最新加进来的能力，也正是
    # 用户最想确认"到底装上没有"的。窄终端（重定向到文件时探不到宽度，会退回 80 列）下，
    # 从头截断会把它们整个吃掉——想证明功能已启用，反而最看不见。
    budget = inner - 16
    if tools:
        listed = tools[-1]  # 先占位：它必须出现，剩下的位置才轮到别人
        kept = 0
        for name in reversed(tools[:-1]):
            candidate = f"{name}、{listed}"
            if _display_width(candidate) > budget:
                break
            listed = candidate
            kept += 1
        # 只在前面真被省掉时才加省略号。加号本身占两列，可能刚好把最后一项顶出去，
        # 那时宁可丢掉"较早的那一项"也不要丢最后一项——它才是这个清单的重点。
        if kept < len(tools) - 1:
            while listed and _display_width("…、" + listed) > budget:
                listed = listed.split("、", 1)[-1]
            listed = "…、" + listed
    else:
        listed = "（未装配）"
    # 被截断时给个去处：横幅是概览，完整清单在 /tools 里（它不受宽度限制）。
    more = "　（/tools 看全部）" if listed.startswith("…") else ""
    line("工具", f"共 {len(tools)} 个 · {listed}{more}")
    line("技能", f"{skills} 个" if skills else "无")
    if cfg.token_budget:
        line("压缩", f"开启 · 预算约 {cfg.token_budget} tokens")
    if cfg.subagent_api_key:
        # 明确标出"可以派活"以及用哪个模型：它和主模型是两个不同的服务商，
        # 出问题时用户第一眼就该知道"这条回复可能是另一个模型给的"。
        line("子智能体", f"可用 · {cfg.subagent_model} @ 阿里云百炼")

    print(f"{C.DIM}├{'─' * (inner - 2)}┤{C.RESET}", file=err)
    print(f"  {C.CYAN}直接输入问题即可对话；以 / 开头的本地命令不会发给模型。{C.RESET}", file=err)
    print(f"  {C.DIM}/help 命令 · /tools 工具 · /clear 清历史 · /exit 或 Ctrl+C 退出"
          f"{C.RESET}", file=err)
    print(f"  {C.DIM}回答以 {C.RESET}{C.BOLD}AI >{C.RESET}{C.DIM} 开头；"
          f"思考、工具调用与错误是灰色过程信息，不进入回答。{C.RESET}", file=err)
    print(f"{C.DIM}└{'─' * (inner - 2)}┘{C.RESET}", file=err)
    return err.getvalue()

async def _serve(gateway: Gateway, provider: OpenAICompatProvider) -> int:
    """跑网关，直到某个渠道退出或用户按 Ctrl+C。

    这里只剩"启动"和"收尾"两件事了：**退出信号由 Gateway.run() 自己产生**——它在
    任意一条渠道结束时返回渠道给出的退出码，并把其余任务收干净。早期版本在这一层
    用 asyncio.wait 去等"网关任务先完成"，那是错的：网关内部要等的那两条消费循环是
    while True，网关任务根本不会自己完成，于是等的人永远等不到，程序卡死。谁掌握
    子任务，谁就该负责判断"该收工了"，这件事只能在网关内部做。

    Args:
        gateway: 已装配好的网关。
        provider: 模型客户端，返回前关掉它的连接池。

    Returns:
        退出码：0 正常退出（/exit、Ctrl+D），130 被 Ctrl+C 打断。
    """
    try:
        return await gateway.run()
    except asyncio.CancelledError:
        # Ctrl+C：Runner 取消了本协程。网关的 finally 已经把渠道停干净了，这里只需要
        # 把取消继续往上抛，让 Runner 按中断处理。
        print("\\n[已中断]\\n", file=sys.stderr)
        raise
    finally:
        try:
            await provider.aclose()
        except Exception:  # noqa: BLE001 - 关连接失败不影响退出码
            logger.debug("关闭模型客户端时出错", exc_info=True)


async def _run(cfg: Config) -> int:
    """装配网关并跑起来，直到用户退出或按 Ctrl+C。

    装配顺序是有讲究的：**注册表 → 模型客户端 → 网关 → 渠道注入**。注册表排在最前面，
    因为横幅要拿它列工具清单、渠道也要拿它填 tool_names，两者必须是同一个装满工具的
    实例（见 _print_banner 的说明）；横幅本身跟着渠道注入一起交出去，由渠道在就绪时打印。

    连接池绑在**创建它的事件循环**上，所以 provider 必须在这里、而不是在协程外面
    创建——这也是整个装配被搬进 async 函数的原因。

    Returns:
        退出码：0 正常退出（/exit、Ctrl+D），130 被 Ctrl+C 打断。
    """
    registry = _build_registry(cfg)

    provider = OpenAICompatProvider(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        model=cfg.model,
        timeout=cfg.timeout,
        extra_body=cfg.extra_body,
    )
    session_manager = _build_session_manager(cfg)

    def factory(session_key: str) -> AgentLoop:
        """给网关的工厂：按会话造 agent。

        这里是**唯一**把"会话键"和"agent"绑在一起的地方。每次被调用都意味着一个新
        会话出现，所以顺手把"恢复了多少历史"打到 stderr——用户需要知道这是接着上次
        聊还是从头开始。
        """
        agent = _build_agent(
            cfg, provider, registry, session_manager, session_key, TerminalSink()
        )
        restored = len(agent.history)
        print(
            f"会话 {session_key}：" + (f"已恢复 {restored} 条历史消息" if restored else "新会话"),
            file=sys.stderr,
        )
        return agent

    bus = MessageBus()
    channels: list[Channel] = [CLIChannel(bus)]

    # QQ 渠道按配置启用：两个凭据都配了才加进来。qq-botpy 因此是**可选依赖**——不启用
    # QQ 的人不必装它，装了的人也不必为它改任何代码（导入只发生在条件成立时）。
    if cfg.qq_app_id and cfg.qq_app_secret:
        # 导入放在这里而不是模块顶部：没装 qq-botpy 时，只有真正要用 QQ 的人才会碰到
        # ImportError，报错信息直接点出缺哪个包，不会连累只跑 CLI 的场景。
        from channels.qq import QQChannel

        channels.append(QQChannel(bus, app_id=cfg.qq_app_id, app_secret=cfg.qq_app_secret))
        print(f"QQ 渠道已启用（appid={cfg.qq_app_id}）", file=sys.stderr)
    elif cfg.qq_app_id or cfg.qq_app_secret:
        print(
            "警告：QQ_APP_ID / QQ_APP_SECRET 只配了一半，QQ 渠道未启用。",
            file=sys.stderr,
        )

    gateway = Gateway(bus, channels, factory)

    # 装配之后的注入：渠道不认识 agent，也不该认识。工具清单与"清历史"这两个能力
    # 由这里递进去，CLI 只管把它们接到 /tools 和 /clear 上。
    channel = channels[0]
    channel.tool_names = registry.list_tools()
    # 横幅不在这里打：它交给渠道、由渠道在**就绪时**打出。早打的话，后面各渠道启动刷出的
    # 日志会紧跟在提示符后面，看起来像提示符被吞掉了（见 CLIChannel._banner）。宽度也由
    # 渠道给——生成横幅时探测到的只是当前这根管道，而不是真正的终端。
    channel._banner = _print_banner(cfg, registry, width=channel.terminal_width)

    def clear_history() -> None:
        """/clear：清掉**当前**会话的历史。

        取的是缓存里那个 agent 而不是新建一个：清空的意义就是让这个会话从头开始，
        而它下一次提问仍会回到同一个实例上（网关按 key 缓存），所以必须清它手里的
        历史，不能另建一个空的来糊弄。
        """
        key = next(iter(gateway._agents), None)
        if key is None:
            print("还没有会话可清。", file=sys.stderr)
            return
        gateway._agents[key].clear_history()
        print(f"会话历史已清空（含会话文件 {key}）。", file=sys.stderr)

    channel._clear_callback = clear_history
    # QQ 渠道也接上同样的两个能力。用 setattr 而不是直接赋值：/tools 与 /clear 是
    # CLIChannel 的约定，别的渠道不必实现它们，有就接上、没有就算了。
    for other in channels[1:]:
        if hasattr(other, "tool_names"):
            setattr(other, "tool_names", registry.list_tools())
        if hasattr(other, "_clear_callback"):
            setattr(other, "_clear_callback", clear_history)

    return await _serve(gateway, provider)


def main() -> int:
    """程序入口。退出码：0 正常，1 配置错误，130 被 Ctrl+C 打断。

    用 asyncio.Runner 而不是手搓事件循环：它会在退出时替我们收掉异步生成器和挂着的
    任务（早期手写的那两段 _finish_task / _shutdown_asyncgens 就是为了补这件事，现在
    交给标准库）。Runner.run 在被取消时不会把 CancelledError 抛给调用方，而是直接
    抛 KeyboardInterrupt，所以下面按中断处理。
    """
    _setup_console()

    if len(sys.argv) > 1:
        # 本程序不接启动参数。静默忽略会让人以为"提问没生效"，所以明确说一声。
        print('提示：忽略启动参数。一次性提问请用  echo "你的问题" | python main.py',
              file=sys.stderr)

    try:
        cfg = Config.from_env()
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1

    _setup_logging(cfg.log_level)
    logger.debug("启动配置: %r", cfg)

    if not cfg.workspace.is_dir():
        print(f"警告：工作区 {cfg.workspace} 不存在，文件工具将无法读写。"
              "请检查 .env 里的 AGENT_WORKSPACE。", file=sys.stderr)

    # 横幅不在这一层打：它要展示工具清单，而工具是在 _run 里装配的（见那里的顺序说明）。
    try:
        with asyncio.Runner() as runner:
            return runner.run(_run(cfg))
    except KeyboardInterrupt:
        print("\n已退出。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())