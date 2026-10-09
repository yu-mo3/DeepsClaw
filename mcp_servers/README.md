# MCP Server 接入说明

本目录放自己的 MCP Server；外部现成的 MCP Server 不需要拷进来，在 `.env` 里登记一行就能用。

## 一、怎么加一个（三步）

**1. 装好它的启动条件**

| server 类型 | 需要什么 | 例子 |
|---|---|---|
| Node / TypeScript 生态的绝大多数 | `node` + `npx` | `npx -y @modelcontextprotocol/server-everything` |
| Python 生态（用 uv 发布的） | `uvx` | `uvx mcp-server-fetch` |
| Python 脚本 / 自己写的 | 解释器路径 + 脚本路径 | `python mcp_servers/poetry_server.py` |
| 编译好的可执行文件 | 那个 exe 的路径 | `./bin/server` |

本机已具备：**node v22、npx 12**；**没有 uv/uvx**（要用 Python 生态的现成 server 得先装 uv）。

**2. 在 `.env` 里登记**

简单式（多个 server 用 `;` 分隔）：

    MCP_SERVERS=poetry=python mcp_servers/poetry_server.py;everything=npx -y @modelcontextprotocol/server-everything

完整式（要配 `env` / `cwd` / `enabled` 时用 JSON）：

    MCP_SERVERS={"poetry": {"command": "python", "args": ["mcp_servers/poetry_server.py"]}}

**3. 重启，看 `/tools`**

工具名形如 `server名__工具名`（两个下划线）。启动日志里会有一行：

    [MCP] 已连接 poetry：3 个工具（search_poetry、random_poetry、list_poets）
    [MCP] 连接结束：1/1 个 server 可用，共 3 个工具

## 二、路径带空格一定要加引号

Windows 上解释器路径几乎必然带空格（`C:\Program Files\...`），命令里的空格又用来分隔参数，
所以**带空格的路径要用引号包起来**：

    MCP_SERVERS=poetry="C:\Program Files\Python314\python.exe" mcp_servers/poetry_server.py

不引的话路径会被切成两半，连不上。另外建议直接写解释器的绝对路径：命令由客户端**直接执行、
不经过 shell**，"python" 不一定在 PATH 里（虚拟环境、多版本共存时尤其如此）。

## 三、几个要知道的行为

- **一个连不上不影响其他**：连接失败或超时（默认 30 秒）只会跳过那个 server，agent 照常启动。
  日志里是 `[MCP] [!] xxx 连接失败，已跳过`；
- **首启动可能慢**：`npx -y` 第一次要下载包（实测约 8 秒，之后走缓存）。真遇到慢的 server，
  首次连接超时属于正常，重跑一次通常会命中缓存；
- **子进程随 agent 退出被回收**：收工时会按连接的**逆序**逐个断开；
- **`enabled: false` 可以临时停用**某个 server，而不必把它从配置里删掉；
- **给 server 起个好名字**：工具名的前缀就是 server 名，名字撞了工具名前缀也会撞
  （`poetry` 与 `poetry2` 是两个不同的 server，各有各的一套工具）；
- **子进程的 stdout 是协议通道**：如果你自己写 server，绝不能往 stdout 打日志（会破坏 JSON-RPC），
  要打就写 stderr。

## 四、排查

| 现象 | 先看哪里 |
|---|---|
| 日志里根本没有 `[MCP]` 开头的信息 | `MCP_SERVERS` 没被读到。**注意配置优先级：真实环境变量 > `.env`**，环境里有个旧的同名变量会盖掉文件里的配置 |
| `[!] xxx 连接失败` | 把配置里的命令单独在终端跑一遍，看它自己报什么错 |
| `[!] xxx 连接超时` | 首次多半是在下载依赖；也可能是那个 server 在等输入（比如没加 `-y` 的 npx 会问 "Ok to proceed?"） |
| 连上了但 `/tools` 里没有它的工具 | `[MCP] 已连接 xxx：0 个工具` 说明那个 server 没暴露工具 |
| 想临时关掉 | 把那一项删掉，或改用 JSON 写法加 `"enabled": false` |

## 五、内置示例

`poetry_server.py`：古诗词查询（12 首唐诗宋词），暴露三个工具
`search_poetry` / `random_poetry` / `list_poets`。它是本项目的参照实现——照它的结构
（FastMCP + `mcp.run(transport="stdio")`）可以复制出新的 server。
