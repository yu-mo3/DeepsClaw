"""dshclaw 命令行入口。

把配置、模型客户端、工具和 agent 循环接成一个能对话的进程：

    Config ─▶ OpenAICompatProvider ─┐
           ├─▶ ToolRegistry         ├─▶ AgentLoop ─▶ 交互循环
           ├─▶ SkillsLoader ─┐      │   (read_file / write_file / list_dir / exec
           └─▶ ContextBuilder ┘──────┘    / web_search / web_fetch / save_memory)

    python main.py                              # 交互模式，可连续追问
    echo "看看工作区有哪些文件" | python main.py   # 一次性提问

交互模式下以 / 开头的是本地命令（/help 查看全部），不会发给模型。

输出全部走事件（见 agent.events），终端这一侧的呈现由 ConsoleSink 负责：回答
**逐字流式**写到 stdout，思考过程、工具调用与错误写到 stderr。启动横幅、输入
提示符、本地命令的回显这些"程序界面"也一律走 stderr，于是 stdout 上只剩模型
回答：`python main.py > answer.txt` 得到的是干净的结果，而 `2> think.txt` 能
单独留下完整的思考过程。
"""

import asyncio
import json
import logging
import os
import sys
from typing import TextIO

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
    OutputSink,
    ThinkingDelta,
    ToolCallEvent,
    ToolResultEvent,
    TurnEndEvent,
)
from agent.loop import AgentLoop
from agent.skills import SkillsLoader
from agent.skills import SkillsLoader
from agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
from agent.tools.memory import MemoryTool
from agent.tools.registry import ToolRegistry
from agent.tools.shell import ExecTool
from agent.tools.web_fetch import WebFetchTool
from agent.tools.web_search import WebSearchTool
from config import Config, DATA_MEMORY_FILE
from providers.openai_compat import OpenAICompatProvider
from session.manager import SessionManager

logger = logging.getLogger("dshclaw")

#: 交互模式的输入提示符
PROMPT = "你 > "

#: 交互模式固定使用的会话标识。将来接多个渠道时，这里换成 "渠道:用户" 即可，
#: 会话文件也会随之分开。
SESSION_KEY = "cli:direct"

HELP_TEXT = """\
本地命令（以 / 开头，不会发给模型）：
  /help    显示这份帮助
  /clear   清空会话历史（同时删除磁盘上的会话文件）
  /reset   同 /clear
  /exit    退出（等同于 /quit）

其他任何输入都会作为问题发给模型。

会话会自动持久化：下次启动同一个会话时，历史会自动恢复。"""


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
    for noisy in ("httpx", "httpx2", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class ConsoleSink(OutputSink):
    """把 agent 事件打到终端：思考、工具、错误走 stderr，回答走 stdout。

    分流是为了保住一条老约定——stdout 上只有回答。于是
    ``python main.py > answer.txt`` 拿到的还是干净的结果，``2> think.txt`` 能单独
    留下完整的思考过程，`| grep` 之类也不会被思考文字污染。

    只负责呈现，不改变任何行为：漏打某条事件不影响 run() 的返回值和会话历史。
    """

    #: 每个 step 的思考块的标题
    THINKING_HEADER = "── 思考 ──"

    def __init__(self, out: TextIO | None = None, err: TextIO | None = None) -> None:
        """初始化。

        Args:
            out: 回答的输出流，默认 sys.stdout。可注入以方便测试——用 io.StringIO
                断言，不必去 monkeypatch 全局的 sys.stdout。
            err: 思考与诊断信息的输出流，默认 sys.stderr。
        """
        self._out = sys.stdout if out is None else out
        self._err = sys.stderr if err is None else err
        # 思考块的标题只在块首打一次，之后的分片直接续写。
        self._thinking_open = False
        # stdout 上是否有没换行的半行（流式回答不带换行符，收尾时要补）。
        self._line_open = False
        # 整轮是否真的流过正文——turn_end 用它决定要不要打占位。
        self._streamed_content = False

    async def emit(self, event: AgentEvent) -> None:
        """按事件类型分派到对应的呈现方式。"""
        if isinstance(event, ThinkingDelta):
            self._write_thinking(event.text)
            return

        # 任何非思考内容都不该和思考挤在同一行，先给思考块收尾。
        self._close_thinking()

        if isinstance(event, ContentDelta):
            self._write_content(event.text)
        elif isinstance(event, ToolCallEvent):
            arguments = json.dumps(event.arguments, ensure_ascii=False)
            self._write_err(f"[工具] {event.name}({arguments})")
        elif isinstance(event, ToolResultEvent):
            self._write_err(self._format_tool_result(event))
        elif isinstance(event, ErrorEvent):
            self._write_err(f"[错误] {event.message}")
        elif isinstance(event, TurnEndEvent):
            self._write_turn_end(event)

    def _write_thinking(self, text: str) -> None:
        """思考增量写 stderr，块首补一行标题。"""
        if not self._thinking_open:
            self._write_err(self.THINKING_HEADER)
            self._thinking_open = True
        self._err.write(text)
        # 不 flush 在 Windows 上就是假流式：内容会攒在缓冲区里，等下一块才吐。
        self._err.flush()

    def _write_content(self, text: str) -> None:
        """正文增量写 stdout，逐片 flush 才有打字机效果。"""
        self._out.write(text)
        self._out.flush()
        self._line_open = True
        self._streamed_content = True

    def _write_turn_end(self, event: TurnEndEvent) -> None:
        """收尾：补换行，并按结局决定要不要额外提示。

        **正常情况下绝不重打 reply**——正文已经逐片流过了，再打一遍屏幕上就是
        两份。只有 reply 没走过流式增量的那几条出口（熔断、步数耗尽）才补打。
        """
        if self._line_open:
            print(file=self._out, flush=True)
            self._line_open = False

        if event.reason == REASON_FINISHED:
            if not self._streamed_content:
                # 整轮一个字都没流出来（模型返回了空内容），给个占位，免得看起来
                # 像程序没反应——这是原来 _print_reply 的行为。
                self._write_out("[模型没有返回文本内容]")
            return

        if event.reason in (REASON_LOOP_BREAK, REASON_MAX_STEPS):
            # 这两条是"任务被中止"，说明文字由 loop 生成、没走过流式增量，
            # 不补打用户就什么也看不到。走 stderr：它是系统提示，不是回答。
            self._write_err(f"\n{event.reply}")
            return

        if event.reason in (REASON_ERROR, REASON_CANCELLED):
            # error 的文案已经由 ErrorEvent 打过一遍；cancelled 的中断提示由
            # _run_turn 负责。两种都不补打，重复说一遍只会变成噪声。
            return

    @staticmethod
    def _format_tool_result(event: ToolResultEvent) -> str:
        """把工具结果压成一行摘要，超长预览只留开头。"""
        preview = event.result.strip().replace("\n", " ⏎ ")
        if len(preview) > 200:
            preview = preview[:200] + "…"
        suffix = f"（共 {event.length} 字符，已截断）" if event.truncated else ""
        return f"[结果] {event.name} → {preview}{suffix}"

    def _close_thinking(self) -> None:
        """给思考块补一个换行，免得后面的内容接在同一行上。"""
        if not self._thinking_open:
            return
        self._err.write("\n")
        self._err.flush()
        self._thinking_open = False

    def _write_out(self, text: str) -> None:
        print(text, file=self._out, flush=True)

    def _write_err(self, text: str) -> None:
        print(text, file=self._err, flush=True)


def _build_agent(cfg: Config, sink: OutputSink | None = None) -> AgentLoop:
    """按配置装配 AgentLoop —— 全项目唯一把各层接起来的地方。

    技能在这里加载而不是在 ContextBuilder 里：技能目录扫一次就够，装载时算好摘要
    传进去，每轮拼 prompt 直接用现成字符串，不必把磁盘扫描摊到每次对话上。

    Args:
        cfg: 运行配置。
        sink: 输出事件的接收方，为 None 时 agent 什么都不往外推（测试方便）。
    """
    provider = OpenAICompatProvider(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        model=cfg.model,
        timeout=cfg.timeout,
        extra_body=cfg.extra_body,
    )
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

    # 技能必须在建 ContextBuilder 之前加载好：它的摘要要进 System Prompt。
    skills = SkillsLoader(os.path.join(workspace, "skills"))
    skills_summary = skills.build_skills_summary()
    if skills_summary:
        # 走 stderr：它和横幅一样属于"程序的界面"，stdout 上只留模型回答。
        print(f"已加载技能 {len(skills.list_skills())} 个", file=sys.stderr)

    context = ContextBuilder(
        workspace=workspace,
        identity_file=cfg.identity_file,
        skills_summary=skills_summary,
        memory_file=str(cfg.memory_file),
    )

    # 会话文件放在**工作区**内的 workspace/sessions 下，而不是项目根目录：运行时数据
    # 与代码分开，迁移工作区时历史跟着走，也不会污染仓库根。
    session_manager = SessionManager(os.path.join(workspace, "workspace", "sessions"))

    return AgentLoop(
        provider,
        registry,
        context,
        max_steps=cfg.max_steps,
        sink=sink,
        session_manager=session_manager,
        session_key=SESSION_KEY,
    )


def _print_banner(cfg: Config, agent: AgentLoop) -> None:
    """打印启动信息。密钥绝不出现。

    走 stderr：它和提示符、命令回显一样都是"程序的界面"，不是模型说的话。
    stdout 上只留模型回答，``python main.py > answer.txt`` 拿到的才是干净的。
    """
    print(f"dshclaw · 模型 {cfg.model} · 工作区 {cfg.workspace}", file=sys.stderr)
    print(f"可用工具：{'、'.join(agent.registry.list_tools())}", file=sys.stderr)
    # 会话是持久的：把身份和恢复到的历史条数一起说出来，用户才知道
    # "这是接着上次聊"还是"开了一个新会话"。
    print(
        f"会话 {agent.session_key}："
        + (f"已恢复 {len(agent.history)} 条历史消息" if agent.history else "新会话"),
        file=sys.stderr,
    )
    print("输入 /help 查看命令，/exit 退出。", file=sys.stderr)
    print("思考与工具调用实时显示（它们走 stderr，stdout 只有回答）。\n", file=sys.stderr)


def _finish_task(loop: asyncio.AbstractEventLoop, task: asyncio.Task) -> None:
    """给被 Ctrl+C 打断的任务收尾，让事件循环回到干净状态。

    不能用 ``run_until_complete(task)``：若 task 已带着 KeyboardInterrupt 结束，
    asyncio 会认定循环早已停止而跳过 stop()，run_forever 永不返回，进程假死。
    包一层 gather(return_exceptions=True) 才能既等到任务结束，异常又不穿出来。
    """
    if task.done():
        task.exception()  # 取一次，消掉 "exception was never retrieved" 警告
        return
    task.cancel()
    try:
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
    except (KeyboardInterrupt, SystemExit):
        pass  # 收尾阶段又按了一次 Ctrl+C，不等了


def _shutdown_asyncgens(loop: asyncio.AbstractEventLoop) -> None:
    """收掉还挂着的异步生成器，必须在 loop.close() 之前跑。

    **这是流式输出引入的新问题**：openai SDK 读 SSE 时会在内部建一串异步
    生成器，其中读到底层字节的那个在读到 ``[DONE]`` 后就停在 yield 上不再前进
    （SDK 自己只保证关掉 HTTP 响应，不管这些生成器）。它们被 GC 时，事件循环
    的终结器钩子会为 ``aclose()`` 排一个任务——而任务还没跑循环就关了，于是
    退出时冒出 "Task was destroyed but it is pending"。

    asyncio.run() 会自动做这件事，本程序的循环是手搓的（要跨轮复用连接池），
    得自己补上。
    """
    try:
        loop.run_until_complete(loop.shutdown_asyncgens())
    except Exception:  # noqa: BLE001 - 收尾失败不该影响退出码
        logger.debug("清理异步生成器时出错", exc_info=True)


def _run_turn(loop: asyncio.AbstractEventLoop, agent: AgentLoop, text: str) -> None:
    """跑完一轮 agent。

    **复用同一个事件循环**而不是每轮 asyncio.run()：AsyncOpenAI 的连接池绑定在
    创建它的事件循环上，换循环会让连接失效。

    Ctrl+C 只取消本轮，会话继续。这是安全的——AgentLoop 只在整轮成功或明确熔断
    时才写历史，中途取消不会留下"有 tool_calls 却没有工具结果"的半截消息。

    不消费 run() 的返回值：回答已经由 sink 流式打完了，返回的那个字符串是给
    外部调用方落盘/二次加工用的。
    """
    task = loop.create_task(agent.run(text))
    try:
        loop.run_until_complete(task)
    except KeyboardInterrupt:
        _finish_task(loop, task)
        print("\n[已中断本轮任务，会话历史未受影响]\n", file=sys.stderr)
        return
    except Exception as exc:
        # 代码层面的意外错误（网络类失败已由 provider 兜成文本）。历史同样干净，
        # 报出来继续用，比整个进程崩掉有意义。
        logger.exception("本轮执行失败")
        print(f"\n[本轮执行失败] {type(exc).__name__}: {exc}\n", file=sys.stderr)


def _repl(loop: asyncio.AbstractEventLoop, agent: AgentLoop) -> int:
    """交互循环：读一行、跑一轮、再读一行。

    input() 阻塞在主线程，但此时事件循环没在跑，两者不冲突。Ctrl+D / Ctrl+Z
    结束输入视为正常退出。
    """
    while True:
        # 提示符自己写到 stderr，不用 input(PROMPT)：input 的提示串固定走 stdout，
        # 会把 "你 > " 混进重定向得到的回答文件里。
        print(PROMPT, end="", file=sys.stderr, flush=True)
        try:
            raw = input()
        except EOFError:
            print(file=sys.stderr)
            return 0

        # 终端里回车会把光标带到下一行，管道输入不会——不补一个换行，思考块和
        # 回答就会接在 "你 > " 后面。
        if not sys.stdin.isatty():
            print(file=sys.stderr)

        text = raw.strip()
        if not text:
            continue

        if text.startswith("/"):
            command = text.split()[0].lower()
            if command in ("/exit", "/quit"):
                return 0
            if command == "/help":
                print(HELP_TEXT, file=sys.stderr)
            elif command in ("/clear", "/reset"):
                # 内存与磁盘一起清：只清内存的话，重启后刚删掉的对话又会出现。
                agent.clear_history()
                print(f"会话历史已清空（含会话文件 {agent.session_key}）。", file=sys.stderr)
            else:
                print(f"未知命令 {command}，输入 /help 查看可用命令。", file=sys.stderr)
            print(file=sys.stderr)
            continue

        _run_turn(loop, agent, text)


def main() -> int:
    """程序入口。退出码：0 正常，1 配置错误，130 被 Ctrl+C 打断。"""
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

    # 整个进程一个 sink。将来接前端时，换成推 WebSocket 的实现即可，其余不动。
    agent = _build_agent(cfg, ConsoleSink())
    _print_banner(cfg, agent)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return _repl(loop, agent)
    except KeyboardInterrupt:
        print("\n已退出。", file=sys.stderr)
        return 130
    finally:
        try:
            loop.run_until_complete(agent.provider.aclose())
        except Exception:  # noqa: BLE001 - 关连接失败不影响退出码
            logger.debug("关闭模型客户端时出错", exc_info=True)
        _shutdown_asyncgens(loop)
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
