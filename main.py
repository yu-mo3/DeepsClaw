"""dshclaw 命令行入口。

把配置、模型客户端、工具和 agent 循环接成一个能对话的进程：

    Config ─▶ OpenAICompatProvider ─┐
           ├─▶ ToolRegistry         ├─▶ AgentLoop ─▶ 交互循环
           └─▶ ContextBuilder       ─┘   (read_file / write_file / list_dir)

    python main.py                              # 交互模式，可连续追问
    echo "看看工作区有哪些文件" | python main.py   # 一次性提问

交互模式下以 / 开头的是本地命令（/help 查看全部），不会发给模型。
日志走 stderr，stdout 只留模型输出，方便直接重定向。
"""

import asyncio
import logging
import sys

from agent.context import ContextBuilder
from agent.loop import AgentLoop
from agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
from agent.tools.registry import ToolRegistry
from config import Config
from providers.openai_compat import OpenAICompatProvider

logger = logging.getLogger("dshclaw")

#: 交互模式的输入提示符
PROMPT = "你 > "

HELP_TEXT = """\
本地命令（以 / 开头，不会发给模型）：
  /help    显示这份帮助
  /reset   清空会话历史，从头开始
  /exit    退出（等同于 /quit）

其他任何输入都会作为问题发给模型。"""


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


def _build_agent(cfg: Config) -> AgentLoop:
    """按配置装配 AgentLoop —— 全项目唯一把各层接起来的地方。"""
    provider = OpenAICompatProvider(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        model=cfg.model,
        timeout=cfg.timeout,
        extra_body=cfg.extra_body,
    )
    registry = ToolRegistry()
    workspace = str(cfg.workspace)
    for tool in (ReadFileTool(workspace), WriteFileTool(workspace), ListDirTool(workspace)):
        registry.register(tool)
    context = ContextBuilder(workspace=workspace, identity_file=cfg.identity_file)
    return AgentLoop(provider, registry, context, max_steps=cfg.max_steps)


def _print_banner(cfg: Config, agent: AgentLoop) -> None:
    """打印启动信息。密钥绝不出现。"""
    print(f"dshclaw · 模型 {cfg.model} · 工作区 {cfg.workspace}")
    print(f"可用工具：{'、'.join(agent.registry.list_tools())}")
    print("输入 /help 查看命令，/exit 退出。\n")


def _print_reply(reply: str) -> None:
    """打印模型回复；空回复给一句占位，免得看起来像程序没反应。"""
    print(reply if reply.strip() else "[模型没有返回文本内容]")
    print()


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


def _run_turn(loop: asyncio.AbstractEventLoop, agent: AgentLoop, text: str) -> None:
    """跑完一轮 agent。

    **复用同一个事件循环**而不是每轮 asyncio.run()：AsyncOpenAI 的连接池绑定在
    创建它的事件循环上，换循环会让连接失效。

    Ctrl+C 只取消本轮，会话继续。这是安全的——AgentLoop 只在整轮成功或明确熔断
    时才写历史，中途取消不会留下"有 tool_calls 却没有工具结果"的半截消息。
    """
    task = loop.create_task(agent.run(text))
    try:
        reply = loop.run_until_complete(task)
    except KeyboardInterrupt:
        _finish_task(loop, task)
        print("\n[已中断本轮任务，会话历史未受影响]\n", file=sys.stderr)
        return
    except Exception as exc:
        # 代码层面的意外错误（网络类失败已由 provider 兜成文本）。历史同样干净，
        # 报出来继续用，比整个进程崩掉有意义。
        logger.exception("本轮执行失败")
        print(f"\n[本轮执行失败] {type(exc).__name__}: {exc}\n", file=sys.stderr)
        return
    _print_reply(reply)


def _repl(loop: asyncio.AbstractEventLoop, agent: AgentLoop) -> int:
    """交互循环：读一行、跑一轮、再读一行。

    input() 阻塞在主线程，但此时事件循环没在跑，两者不冲突。Ctrl+D / Ctrl+Z
    结束输入视为正常退出。
    """
    while True:
        try:
            raw = input(PROMPT)
        except EOFError:
            print()
            return 0

        text = raw.strip()
        if not text:
            continue

        if text.startswith("/"):
            command = text.split()[0].lower()
            if command in ("/exit", "/quit"):
                return 0
            if command == "/help":
                print(HELP_TEXT)
            elif command == "/reset":
                agent.history.clear()
                print("会话历史已清空。")
            else:
                print(f"未知命令 {command}，输入 /help 查看可用命令。")
            print()
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

    agent = _build_agent(cfg)
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
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
