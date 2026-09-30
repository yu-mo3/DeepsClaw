"""dshclaw 命令行入口。

把配置、模型客户端、工具、上下文构造器和 agent 循环接成一个能对话的进程：

    读 .env ─▶ Config ─▶ OpenAICompatProvider   ─┐
                     ├─▶ ToolRegistry             ├─▶ AgentLoop ─▶ 交互循环
                     └─▶ ContextBuilder          ─┘   (read_file / write_file / list_dir)

用法::

    python main.py                        # 交互模式，可以连续追问
    python main.py 看看工作区有哪些文件    # 单次提问，答完即退出（适合脚本调用）

交互模式下以 ``/`` 开头的是本地命令（``/help`` 查看全部），不会发给模型。
日志走 stderr，stdout 只留模型输出，方便重定向到文件或接管道。
"""

import argparse
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
  /help     显示这份帮助
  /tools    列出已注册的工具
  /history  显示当前会话累计的消息条数
  /reset    清空会话历史，从头开始
  /exit     退出（等同于 /quit）

其他任何输入都会作为问题发给模型。"""


def _setup_console() -> None:
    """把被重定向的标准输出切到 UTF-8。

    Windows 上 stdout 不是真实控制台时（重定向到文件、跑在 Git Bash 里），Python
    按系统编码输出，中文环境是 GBK，中文会变乱码。真实控制台本身已经是 UTF-8，
    不需要也不应该动它。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if encoding.startswith("utf8"):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # 流已被替换成不支持重配置的对象，保持原样即可，不值得为此中断启动。
            pass


class _ThirdPartyNoiseFilter(logging.Filter):
    """挡掉第三方库低于 WARNING 的日志。

    按 logger 名**前缀**匹配，而不是按全名写死：openai SDK 3.x 已经把 httpx 换成了
    一个叫 httpx2 的包，logger 名跟着变，写死 "httpx" 会静默失效——屏幕上继续刷
    请求日志，而你以为已经关掉了。前缀匹配对这种改名免疫。

    挂在 handler 上而不是 logger 上：logger 上的 filter 只对直接记在该 logger 的
    记录生效，管不了子 logger 冒泡上来的记录。
    """

    NOISY_PREFIXES = ("httpx", "httpcore", "openai", "urllib3", "requests")

    def filter(self, record: logging.LogRecord) -> bool:
        # 警告以上一律放行：限流、重试、连接异常都可能由这些库报出来。
        if record.levelno >= logging.WARNING:
            return True
        return not record.name.startswith(self.NOISY_PREFIXES)


def _setup_logging(level: str) -> None:
    """配置日志：写 stderr、带时间戳，并把第三方库的噪声压下去。

    日志走 stderr 而不是 stdout，是为了让 stdout 只有模型回复——这样才能
    ``python main.py "问题" > 答案.txt`` 拿到干净的结果。
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # 第三方库在 INFO 级别会把每次请求的 URL、重试过程全打出来，正常使用时是纯噪声。
    # 排查网络问题时把 LOG_LEVEL 调到 DEBUG，这里就不装了。
    if logging.getLogger().level > logging.DEBUG:
        noise_filter = _ThirdPartyNoiseFilter()
        for handler in logging.getLogger().handlers:
            handler.addFilter(noise_filter)


def _build_agent(cfg: Config) -> AgentLoop:
    """按配置装配出可用的 AgentLoop。

    这是全项目唯一把各层接起来的地方：换 provider、增删工具都只改这一个函数，
    其余各层互相不知道对方的存在。
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
    for tool in (ReadFileTool(workspace), WriteFileTool(workspace), ListDirTool(workspace)):
        registry.register(tool)

    context = ContextBuilder(workspace=workspace, identity_file=cfg.identity_file)

    return AgentLoop(provider, registry, context, max_steps=cfg.max_steps)


def _print_banner(cfg: Config, agent: AgentLoop) -> None:
    """打印启动信息。只显示定位问题需要的参数，密钥绝不出现。"""
    print(f"dshclaw · 模型 {cfg.model} · 工作区 {cfg.workspace}")
    print(f"可用工具：{'、'.join(agent.registry.list_tools())}")
    print("输入 /help 查看命令，/exit 退出。\n")


def _print_reply(reply: str) -> None:
    """打印模型回复。回复为空时给一句占位，避免用户以为程序没反应。"""
    print(reply if reply.strip() else "[模型没有返回文本内容]")
    print()


def _finish_task(loop: asyncio.AbstractEventLoop, task: asyncio.Task) -> None:
    """把被 Ctrl+C 打断的任务收尾，让事件循环回到干净状态。

    注意这里**不能**用 ``loop.run_until_complete(task)`` 收尾：如果 task 已经带着
    KeyboardInterrupt 结束，asyncio 的 _run_until_complete_cb 会认定"循环早已停止"
    而跳过 stop()，于是 run_forever 永远不返回，进程直接卡死。包一层
    ``gather(return_exceptions=True)`` 既能等到任务真正结束，异常又不会穿透到
    这层控制逻辑里。
    """
    if task.done():
        # 异常已经在 run_until_complete 里抛给我们了；这里再取一次只是为了消掉
        # "Task exception was never retrieved" 警告。
        task.exception()
        return
    # 真实的 Ctrl+C 发生在线程阻塞于 select() 时，此刻 task 还没结束，是挂起的。
    task.cancel()
    try:
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
    except (KeyboardInterrupt, SystemExit):
        # 用户在收尾阶段又按了一次 Ctrl+C，放弃等待即可。
        pass


def _run_turn(loop: asyncio.AbstractEventLoop, agent: AgentLoop, text: str) -> None:
    """在当前事件循环上跑完一轮 agent，并处理本轮出错或被打断。

    **复用同一个事件循环**而不是每轮 asyncio.run()：provider 内部的
    AsyncOpenAI 连接池绑定在创建它的事件循环上，换循环会导致连接失效。

    Ctrl+C 只取消当前这一轮，会话本身继续。这是安全的：AgentLoop 只在整轮成功
    或明确熔断时才写历史，中途取消不会在历史里留下"有 tool_calls 却没有工具结果"
    的半截消息，所以下一轮照样能正常提问。
    """
    task = loop.create_task(agent.run(text))
    try:
        reply = loop.run_until_complete(task)
    except KeyboardInterrupt:
        _finish_task(loop, task)
        print("\n[已中断本轮任务，会话历史未受影响]\n", file=sys.stderr)
        return
    except Exception as exc:
        # 到这里说明是代码层面的意外错误（网络类失败已由 provider 兜成文本）。
        # 同样不影响历史，报出来继续用，比整个进程崩掉有意义。
        logger.exception("本轮执行失败")
        print(f"\n[本轮执行失败] {type(exc).__name__}: {exc}\n", file=sys.stderr)
        return

    _print_reply(reply)


def _repl(loop: asyncio.AbstractEventLoop, agent: AgentLoop) -> int:
    """交互循环：读一行、跑一轮、再读一行。

    input() 同步阻塞在主线程里，但此时事件循环没有在跑，两者不冲突；放进线程池
    反而会在退出时和线程回收较劲。Ctrl+D / Ctrl+Z 结束输入视为正常退出。
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
            elif command == "/tools":
                print("已注册工具：" + "、".join(agent.registry.list_tools()))
            elif command == "/history":
                print(f"当前会话累计 {len(agent.history)} 条消息。")
            elif command == "/reset":
                agent.history.clear()
                print("会话历史已清空。")
            else:
                print(f"未知命令 {command}，输入 /help 查看可用命令。")
            print()
            continue

        _run_turn(loop, agent, text)


def main() -> int:
    """程序入口。返回进程退出码：0 正常，1 配置错误，130 被 Ctrl+C 打断。"""
    _setup_console()

    parser = argparse.ArgumentParser(
        prog="dshclaw",
        description="一个能读写工作区文件的命令行编程助手。",
    )
    parser.add_argument(
        "prompt",
        nargs="*",
        help="直接提问。给定时不进入交互模式，答完即退出",
    )
    args = parser.parse_args()

    try:
        cfg = Config.from_env()
    except ValueError as exc:
        # 配置错误在启动时就说清楚是哪一项，不带栈。
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1

    _setup_logging(cfg.log_level)
    logger.debug("启动配置: %r", cfg)

    if not cfg.workspace.is_dir():
        print(
            f"警告：工作区 {cfg.workspace} 不存在，文件工具将无法读写。"
            "请检查 .env 里的 AGENT_WORKSPACE。",
            file=sys.stderr,
        )

    agent = _build_agent(cfg)
    question = " ".join(args.prompt).strip()

    # 单次提问模式：stdout 只留答案，方便重定向。
    if question:
        return asyncio.run(_run_once(agent, question))

    _print_banner(cfg, agent)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return _repl(loop, agent)
    except KeyboardInterrupt:
        # 在输入提示符上按 Ctrl+C：安静退出，不打印异常栈。
        print("\n已退出。", file=sys.stderr)
        return 130
    finally:
        try:
            loop.run_until_complete(agent.provider.aclose())
        except Exception:  # noqa: BLE001 - 关连接失败不该影响退出码
            logger.debug("关闭模型客户端时出错", exc_info=True)
        loop.close()


async def _run_once(agent: AgentLoop, question: str) -> int:
    """跑单次提问，把结果打到 stdout。"""
    try:
        reply = await agent.run(question)
    except Exception as exc:
        logger.exception("执行失败")
        print(f"执行失败 - {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    _print_reply(reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
