"""MCP（Model Context Protocol）客户端：把外部 MCP Server 的工具接进 agent。

agent 的工具箱原本是**写在代码里**的：想加一个能力，就得写一个 Tool 子类、再装进 main.py。
MCP 换了一种活法——能力由**外部进程**提供，本模块负责连上去、问它"你有什么工具"、把每个
工具包装成项目里的 Tool，再在调用时转发过去：

    MCPClientManager.connect_all()
        ├─ 每个 server 拉起一个子进程（stdio 传输：在管道上用 JSON-RPC 说话）
        ├─ session.list_tools() 拿到清单
        └─ 每个工具包成一个 MCPTool ─▶ 注册进 ToolRegistry ─▶ agent 就能调了

于是"加能力"变成"往 mcp_servers/ 丢一个文件 + 在 .env 里登记一行"，agent 侧一行不用改。

## 为什么必须手动 __aenter__（不是风格问题，是会卡死）

`stdio_client()` 与 `ClientSession()` 都是**异步上下文管理器**，内部各自起一个 anyio
任务组跑后台循环：前者负责读写管道，后者负责收 JSON-RPC 响应并按 id 唤醒等待者。
`with` 语法糖是"进入时建任务组、退出时拆任务组"，而这里要的东西正好相反——连上之后
**必须让那些后台任务一直活着**，否则 `initialize()` 发出去的请求没有任何人在读回音，直接
死等。所以本模块手动 `await ctx.__aenter__()`、退出时手动 `await ctx.__aexit__(...)`，
把这个生命周期攥在自己手里。

由此带出**第二条铁律**：手动进入的上下文，它的 `__aexit__` 必须在**同一个 asyncio 任务**里
调用。anyio 的 cancel scope 按任务记账，跨任务退出会直接报
"Attempted to exit cancel scope in a different task than it was entered in"。
所以装配处的写法必须是 connect_all() 与 shutdown() 在同一个协程里成对出现
（见 main.py 的 _serve），不能把 shutdown 塞进网关的清理逻辑、也不能丢进别的任务。

## 出错时不能让一个 server 拖垮整台 agent

连接、初始化、列工具，任何一步失败或超时，都只把这**一个** server 跳过并记一条警告：
MCP Server 是外部进程，写错了、没装依赖、起得慢都很常见，而它们全都不是"用户的事"——
agent 少了几个工具仍然能干活，起不来才是灾难。

## 日志约定：不用 emoji

本模块所有输出走 logging（默认 stderr），并且**不含 emoji**：Windows 控制台默认是 GBK
代码页，往上面打印表情会抛 UnicodeEncodeError 把进程带崩。需要视觉标记时用 [MCP] / [!]
这类纯 ASCII 前缀。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.tools.base import Tool

logger = logging.getLogger(__name__)

#: 工具名里分隔"server 名"与"工具名"的记号。
#:
#: 用两个下划线是有讲究的：MCP 工具名自己常用单下划线（search_poetry），拿单下划线拼会
#: 分不清边界。形如 poetry__search_poetry 一眼能看出"哪个 server 的哪个工具"，
#: 也几乎不可能和内置工具重名。
TOOL_NAME_SEPARATOR = "__"

#: 连接一个 server 的默认超时（秒）。
#:
#: 30 秒是给"启动稍慢"留的余量（要装依赖、要连网络、要预热模型都可能慢）；超过它说明这个
#: server 多半起不来了，跳过比继续等更划算——用户还在终端前面等着。
DEFAULT_CONNECT_TIMEOUT = 30.0


class MCPClientManager:
    """管理若干个 MCP Server 的连接与它们的工具。

    每个 server 一条连接，生命期是**整个进程**：连上就保持，直到 shutdown()。因此这里的状态
    都是长命的，而 connect_all() 与 shutdown() 必须在同一个任务里成对调用（理由见模块文档
    那条铁律）。

    连接是**尽力而为**的：任何一个 server 连不上、超时、列不出工具，都只跳过它自己，其余照连。
    所以 connect_all() 之后"能用的工具可能比配置里少"，这是设计如此，不是 bug。
    """

    def __init__(self, mcp_config: dict[str, dict[str, Any]]) -> None:
        """初始化（**不建任何连接**，只记配置）。

        Args:
            mcp_config: server 名 -> 配置，形如
                {"poetry": {"command": "python", "args": ["mcp_servers/poetry_server.py"],
                "env": {}, "cwd": null, "enabled": true}}。
                真正的连接发生在 connect_all() 里；构造函数是同步的，装配处拿它当普通对象用即可。
        """
        self.mcp_config = dict(mcp_config or {})

        #: server 名 -> stdio_client 的上下文管理器（负责子进程与管道）
        self._context_managers: dict[str, Any] = {}
        #: server 名 -> ClientSession 的上下文管理器（负责 JSON-RPC 的消息循环）
        self._session_managers: dict[str, Any] = {}
        #: server 名 -> 已初始化的 ClientSession
        self._sessions: dict[str, ClientSession] = {}
        #: 所有 server 的工具，已包成 MCPTool
        self._tools: list[MCPTool] = []

    async def _connect_one(self, server_name: str, server_config: dict[str, Any]) -> None:
        """连上一个 MCP Server，并把它的工具收集起来。

        三步，全部手动进入（原因见模块文档）：

        1. stdio_client(params).__aenter__() → 拿到 (read, write) 两个流，后台跑起管道循环；
        2. ClientSession(read, write).__aenter__() → 消息循环起来，能收发 JSON-RPC 了；
        3. initialize() → 握手；随后 list_tools() 取工具清单。

        **任何一步失败都要把已经进入的那几层退掉**，否则那对后台任务会一直挂着，到进程收尾时
        变成"在别的任务里退出取消作用域"的怪错误——比失败本身更难查。

        Args:
            server_name: server 名，用于日志与工具名前缀。
            server_config: 该 server 的配置（command / args / env / cwd）。

        Raises:
            Exception: 连接或握手的异常原样抛出，由 connect_all() 决定跳过。
        """
        params = StdioServerParameters(
            command=server_config["command"],
            # args 必须是字符串列表：MCP 直接拿它拼命令行，混进非字符串会在子进程启动时炸。
            args=[str(a) for a in server_config.get("args") or []],
            env=server_config.get("env") or None,
            cwd=server_config.get("cwd") or None,
        )

        context = stdio_client(params)
        session_manager: Any = None
        try:
            read, write = await context.__aenter__()
            session_manager = ClientSession(read, write)
            session: ClientSession = await session_manager.__aenter__()
            await session.initialize()
        except BaseException:
            # 半途而废：把已经进入的层按反序退掉，绝不留悬挂的任务组。
            # 捕 BaseException 是为了**取消**也能退干净——Ctrl+C 恰好落在连接中间时，
            # 不退就会在收尾时炸；退完再把异常原样抛出去。
            await self._dispose(context, session_manager)
            raise

        tools = (await session.list_tools()).tools
        self._context_managers[server_name] = context
        self._session_managers[server_name] = session_manager
        self._sessions[server_name] = session

        # 先记下这批工具的名字，再逐个收——重名检查要基于"已收下的全部工具"，
        # 否则同一批里出现两个同名工具时检查不出来。
        existing = {tool.name for tool in self._tools}
        accepted = 0
        for tool in tools:
            candidate = "{}{}{}".format(server_name, TOOL_NAME_SEPARATOR, tool.name)
            if candidate in existing:
                # 工具名撞车只跳过这一个，不抛异常：下游的 ToolRegistry.register()
                # 遇到重名是直接 raise 的，不在这里拦下就会让**整个进程起不来**——
                # 而用户只是配了个名字撞了的 server，罪不至此。
                logger.warning(
                    "[MCP] [!] 工具名 %s 与已有工具重复，已跳过该工具（换一个 server 名可避开）",
                    candidate,
                )
                continue
            existing.add(candidate)
            accepted += 1
            self._tools.append(
                MCPTool(
                    server_name=server_name,
                    tool_name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.inputSchema or {"type": "object", "properties": {}},
                    client_manager=self,
                )
            )
        logger.info(
            "[MCP] 已连接 %s：%d 个工具（%s）",
            server_name,
            accepted,
            "、".join(t.tool_name for t in self._tools if t.server_name == server_name) or "无",
        )

    @staticmethod
    async def _dispose(context: Any, session_manager: Any) -> None:
        """按反序退出"手动进入"的两层上下文，任何一层失败都不影响另一层。

        退出顺序必须是 session 先、stdio 后：消息循环跑在管道之上，先拆管道会让消息循环
        读到一个已关闭的流。

        单个退出失败只记日志：这个函数本身就运行在失败路径上（连接半途而废），在这里再抛
        异常只会盖掉真正的错因。
        """
        for label, manager in (("session", session_manager), ("stdio", context)):
            if manager is None:
                continue
            try:
                await manager.__aexit__(None, None, None)
            except BaseException:  # noqa: BLE001 - 见 docstring：能清多少算多少
                # 捕 BaseException 而不是 Exception：这里的 __aexit__ 有可能抛 CancelledError
                # （anyio 在作用域顺序不对时会这么干）。若让它冒出去，剩下几个 server 就再也
                # 没人关了——收尾阶段的"一根筋"比"半途而废"危害大得多。
                logger.debug("[MCP] 退掉 %s 上下文时出错", label, exc_info=True)

    async def connect_all(self, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> None:
        """按配置依次连接所有 server。

        **逐个连、不并发**：连接要拉子进程、要握手，并发启动一屏子进程既难排查也容易抢资源
        （尤其几个 server 都要装依赖时）。顺序连的代价只是启动慢一点，而这个开销一个进程
        只付一次。

        每个 server 单独设超时，超时或出错就跳过它、继续下一个——一个坏掉的 server 不该让
        agent 起不来（理由见模块文档）。

        Args:
            timeout: 单个 server 的连接超时（秒），涵盖拉进程、握手与列工具。
        """
        enabled = {
            name: cfg
            for name, cfg in self.mcp_config.items()
            if cfg and cfg.get("command") and cfg.get("enabled", True)
        }
        if not enabled:
            logger.debug("[MCP] 没有启用的 server（配置为空，或全都 enabled=false）")
            return

        logger.info("[MCP] 开始连接 %d 个 server：%s", len(enabled), "、".join(enabled))
        for server_name, server_config in enabled.items():
            try:
                await asyncio.wait_for(
                    self._connect_one(server_name, server_config), timeout=timeout
                )
            except asyncio.TimeoutError:
                logger.warning("[MCP] [!] %s 连接超时（超过 %ss），已跳过", server_name, timeout)
            except Exception as exc:  # noqa: BLE001 - 见 docstring：一个 server 不该拖垮全局
                logger.warning(
                    "[MCP] [!] %s 连接失败，已跳过：%s: %s",
                    server_name,
                    type(exc).__name__,
                    exc,
                )
                logger.debug("[MCP] %s 的连接堆栈", server_name, exc_info=True)

        logger.info(
            "[MCP] 连接结束：%d/%d 个 server 可用，共 %d 个工具",
            len(self._sessions),
            len(enabled),
            len(self._tools),
        )

    def get_tools(self) -> list[MCPTool]:
        """取所有已连接 server 的工具（可能为空列表）。

        返回的是**已经包装好的 Tool 实例**，可以直接 registry.register()——装配处不必知道
        它们来自 MCP 还是内置。
        """
        return list(self._tools)

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
        """调用某个 server 上的某个工具，返回它给的文本。

        **不抛异常**：server 掉线、工具名写错、调用本身失败，都返回一段可读的说明。这与项目里
        其他工具一致（见 Tool.execute 的约定）——错误描述回传给模型，它有机会换个参数或换条路；
        抛异常则会让整轮任务断在这里。

        Args:
            server_name: server 名。
            tool_name: 该 server 上的工具名（**不含**前缀）。
            arguments: 工具参数，键名要和该工具的 inputSchema 对得上。

        Returns:
            工具返回的文本；失败时是以"错误："开头的说明。
        """
        session = self._sessions.get(server_name)
        if session is None:
            return "错误：MCP server {} 未连接，无法调用 {}".format(server_name, tool_name)

        try:
            result = await session.call_tool(tool_name, arguments)
        except Exception as exc:  # noqa: BLE001 - 见 docstring：失败要变成文本
            logger.warning("[MCP] [!] 调用 %s/%s 失败：%s", server_name, tool_name, exc)
            return "错误：调用 {}/{} 失败 - {}: {}".format(
                server_name, tool_name, type(exc).__name__, exc
            )

        text = self._content_to_text(result)
        if getattr(result, "isError", False):
            # 工具"跑通了但业务失败"：MCP 用 isError 表达这种情况，原样标出来。
            logger.info("[MCP] %s/%s 返回业务错误", server_name, tool_name)
            return "错误：{}".format(text or "工具执行失败")
        return text or "（工具没有返回内容）"

    @staticmethod
    def _content_to_text(result: Any) -> str:
        """把 CallToolResult 的内容块拼成一段文本。

        MCP 的返回是**内容块列表**（文本、图片、资源引用……），不保证是纯文本。这里只取文本块：
        图片与资源引用没法直接塞进模型的消息里，而本项目也没有多模态通道，硬拼进去只会得到
        一串看不懂的 JSON。真需要图片时该走的是"存成文件再告诉模型路径"，那是另一个功能。
        """
        parts: list[str] = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts)

    async def shutdown(self) -> None:
        """断开所有连接：**按连接的逆序**逐个退，每个内部先退 session 再退 stdio。

        三条顺序都不能错，错一条就炸——而且炸法各不相同：

        1. **server 之间要逆序**。每次连接都会往当前任务上压一对 anyio cancel scope
           （管道一个、消息循环一个），多个 server 就是一叠作用域。anyio 要求它们**后进先出**，
           而"连接顺序"就是进栈顺序，所以断开必须反着来。按正序退等于从栈底往上抽，
           anyio 会直接取消当前任务，表现为 shutdown 抛 CancelledError——
           项目里踩过这个坑：一个 server 时完全正常，配上第二个就必炸；
        2. **server 内部要 session 先、stdio 后**。消息循环跑在管道之上：先拆管道会让消息循环
           读到一个已关闭的流；
        3. **不能用 asyncio.gather 并发退**。所有 __aexit__ 必须跑在**当前任务**里——anyio 的
           cancel scope 按任务记账，丢进别的任务会报 "Attempted to exit cancel scope in a
           different task than it was entered in"。

        同理**不给单个退出设 asyncio.wait_for 超时**：它靠"取消"来实现超时，而取消正好会污染
        上面那叠作用域，把"某个 server 退得慢"升级成"整场 shutdown 崩掉"。卡住的兜底由 SDK
        自己负责（stdio_client 关管道时会先等进程退出，超时则依次 SIGTERM / SIGKILL）。

        单个退出失败只记日志、继续退下一个：收尾阶段要的是"能关的都关掉"，一个倔强的 server
        不该留下其余的连接不放。
        """
        if not self._context_managers:
            return

        logger.info("[MCP] 正在断开 %d 个 server", len(self._context_managers))
        # reversed：连接顺序的**逆序**，见上面第 1 条。这不是风格问题，错序会炸。
        for server_name in reversed(list(self._context_managers)):
            await self._dispose(
                self._context_managers.pop(server_name, None),
                self._session_managers.pop(server_name, None),
            )
            self._sessions.pop(server_name, None)

        self._tools.clear()
        logger.info("[MCP] 已全部断开")


class MCPTool(Tool):
    """把 MCP Server 上的一个工具，伪装成本项目的一个 Tool。

    它自己不含业务逻辑，只做三件翻译：

    - **名字**：server__tool，避免不同 server 的同名工具撞车；
    - **说明与参数**：直接用 MCP 给的 description 与 inputSchema，不重写、不加工——
      server 作者最清楚自己的工具怎么用，二次转述只会丢信息；
    - **调用**：execute() 转发给 client_manager.call_tool()。

    于是 agent 循环完全看不出"这个工具来自外部进程"：它和 read_file 走同一条注册、分发、
    回传路径。
    """

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        description: str,
        input_schema: dict[str, Any],
        client_manager: MCPClientManager,
    ) -> None:
        """初始化。

        Args:
            server_name: 所属 server 名。
            tool_name: 在 server 上的原名（不含前缀）。
            description: 工具说明，来自 MCP。
            input_schema: 参数的 JSON Schema，来自 MCP，原样保留。
            client_manager: 连接的管理者，execute 通过它转发调用。
        """
        self.server_name = server_name
        self.tool_name = tool_name
        self._description = description
        self._input_schema = input_schema
        self._client_manager = client_manager

    @property
    def name(self) -> str:
        """工具在全项目内的唯一名：server__tool。"""
        return "{}{}{}".format(self.server_name, TOOL_NAME_SEPARATOR, self.tool_name)

    @property
    def description(self) -> str:
        """工具说明。MCP 没给说明时补一句兜底，免得模型面对一个没有描述的工具。"""
        if self._description:
            return self._description
        return "MCP 工具 {}/{}（该 server 未提供说明）".format(self.server_name, self.tool_name)

    @property
    def parameters(self) -> dict[str, Any]:
        """参数定义：MCP 给的 inputSchema，原样透出。"""
        return self._input_schema

    async def execute(self, **kwargs: Any) -> str:
        """转发到 MCP Server 执行。

        参数**不做任何猜测或补齐**：模型看到的就是 inputSchema，就该按它给参数；缺参数时由
        server 自己报错，那比客户端自作主张填默认值更清楚。
        """
        return await self._client_manager.call_tool(self.server_name, self.tool_name, kwargs)

    def to_function_definition(self) -> dict[str, Any]:
        """组装成 OpenAI function calling 定义。

        与基类唯一的差别是**参数的键名**：MCP 用的是 inputSchema（大驼峰），OpenAI 要的是
        parameters。其余字段（name / description）与基类那套完全一致——覆盖整个方法而不是
        复制父类代码，是为了让"哪一处不同"一眼可见。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._input_schema,
            },
        }
