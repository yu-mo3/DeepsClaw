# DeepsClaw

一个**多渠道、可扩展**的 AI Agent 框架：把 LLM、工具、渠道（终端 / QQ）和 MCP Server 接成一个能对话、能动手、能挂机跑的服务。

你给它一个终端窗口，它会读文件、写代码、跑命令、联网搜索；你给它一个 QQ 机器人，它就在群里陪你聊天查资料；你给它一个 MCP Server，它立刻多出一批别人写好的工具——**agent 侧一行代码都不用改**。

> 项目定位：一份"把 agent 该有的零件都装好"的参考实现。每一层都刻意拆开、每处取舍都写在注释里，方便你换掉其中一层而不动其他。

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![MCP](https://img.shields.io/badge/MCP-supported-6E56CF)
![Channels](https://img.shields.io/badge/Channels-CLI%20%7C%20QQ-blue)
![Status](https://img.shields.io/badge/status-active-brightgreen)

---

## 目录

- [它能做什么](#它能做什么)
- [快速开始](#快速开始)
- [配置](#配置)
- [项目结构](#项目结构)
- [架构](#架构)
- [内置工具](#内置工具)
- [子智能体](#子智能体)
- [接入 MCP Server](#接入-mcp-server)
- [接入新渠道](#接入新渠道)
- [常见问题](#常见问题)
- [开发笔记](#开发笔记几个刻意的设计)

---

## 它能做什么

| 能力 | 说明 |
|---|---|
| 💬 **多轮对话** | 会话自动落盘，关掉终端再打开还能接着聊（JSONL，一行一条） |
| 🔧 **8 个内置工具** | 读写文件、列目录、执行命令、联网搜索与抓取、写长期记忆、派子智能体 |
| 🧠 **思考过程可见** | 支持带 reasoning 的模型（DeepSeek / Kimi 等），思考流式打在 stderr，回答单独走 stdout |
| 🌐 **多渠道** | 终端（CLI）与 QQ 官方机器人已实现；渠道是插件，加一个文件就行 |
| 🤖 **子智能体** | 把"过程长、结论短"的活派给临时专员，5 分钟的调查不占你的上下文 |
| 🔌 **MCP 支持** | 连接任意 MCP Server（本地或 `npx` 拉起的第三方），工具自动出现在 `/tools` 里 |
| 📦 **历史压缩** | 对话太长时自动把中间那段摘要掉，长会话不会撑爆上下文 |
| ⚙️ **单文件配置** | 一个 `config.json` 管模型、渠道、子智能体、MCP、工具 |
| 🎯 **模型可分工** | 主对话用强模型、子任务用便宜模型，配置里起个别名就行 |

---

## 快速开始

### 1. 环境要求

- **Python 3.11+**（开发环境为 3.14；类型注解用的是 PEP 604 的 `X | None` 与 `from __future__ import annotations`）
- 一个 OpenAI 兼容接口的模型密钥（DeepSeek / 百炼 / 硅基流动 / OpenAI…都行）

### 2. 安装依赖

```bash
# 必装：读配置、调模型
pip install python-dotenv openai

# 可选：用 QQ 渠道才需要
pip install qq-botpy

# 可选：要接入 MCP Server 才需要（不装则 MCP 功能自动关闭）
pip install mcp
```
### 3. 写配置

复制模板，填上你的密钥：

```bash
cp config.example.json config.json
```
```json
{
  "api_key": "sk-你的密钥",
  "base_url": "https://api.deepseek.com/v1",
  "models": { "main": "deepseek-chat" }
}
```
> 只写这两三行就能跑。其余字段全有默认值，需要时再补（见[配置](#配置)）。

### 4. 跑起来

```bash
python main.py
```
```text
┌─────────────────────────────DeepsClaw──────────────────────────────┐
  模型 deepseek-chat
  工作区 /home/you/DeepsClaw/workspace
  配置 /home/you/DeepsClaw/config.json
  工具 共 8 个 · read_file、write_file、list_dir、exec、web_search、web_fetch…
  子智能体 可用 · qwen3.8-flash @ 阿里云百炼
├────────────────────────────────────────────────────────────────────┤
  直接输入问题即可对话；以 / 开头的本地命令不会发给模型。
└────────────────────────────────────────────────────────────────────┘

你 > 帮我看看这个目录里都有什么
  ── 思考 ──
  用户想知道目录结构，先列一下再看看有没有值得展开的。
[工具] list_dir({"dir_path": "."})
[结果] list_dir → agent/  bus/  channels/  ...
AI > 这是一个 Python 项目，分成 agent / bus / channels 三层……
```
### 5. 也可以一次性提问

```bash
echo "把 README 里的错别字找出来" | python main.py
```
---

## 配置

全部配置集中在项目根目录的 **`config.json`**（模板见 [`config.example.json`](config.example.json)）。

### 优先级

```text
环境变量  >  .env  >  config.json  >  内置默认值
```
**逐项合并**，不是整份替换：`config.json` 里没写的项照样能从 `.env` 或默认值取到。所以你可以：

- 只用 `config.json`——最省事；
- `config.json` 写通用配置、`.env` 只放密钥——适合把配置提交给团队；
- `LLM_MODEL=xxx python main.py` 临时覆盖某一项做实验。

### 完整字段

```jsonc
{
  "api_key": "sk-xxx",                    // 主模型密钥（必填，或用 .env 的 LLM_API_KEY）
  "base_url": "https://api.deepseek.com/v1",
  "models": {
    "main":     "deepseek-chat",           // 主对话用的模型
    "subagent": "qwen3.8-flash",           // 子智能体的默认模型
    "cheap":    "deepseek-chat"            // 其余键是"别名"，见下方说明
  },

  "workspace": ".",                       // 文件工具的沙箱根目录
  "identity_file": "identity.md",         // 人设文件（相对 workspace）
  "max_iterations": 50,                   // 单轮最多调用几次模型
  "timeout": 120,                         // 单次请求超时（秒）
  "log_level": "INFO",
  "extra_body": { "thinking": { "type": "enabled" } },  // 原样透传给接口的额外参数

  "memory":  { "file": "workspace/memory/MEMORY.md", "token_budget": 24000 },
  "tools":   { "bocha_api_key": "" },

  "subagent": {
    "api_key": "",                        // 留空则回退主模型密钥
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
  },

  "channels": {
    "qq":     { "app_id": "", "app_secret": "" },   // 两项都填才启用
    "feishu": { "app_id": "", "app_secret": "" },   // 配置已支持，渠道实现待补
    "web":    { "enabled": false, "host": "127.0.0.1", "port": 8080 }
  },

  "mcp_servers": {
    "poetry": {
      "command": "python",
      "args": ["mcp_servers/poetry_server.py"],
      "description": "古诗词查询（给人和日志看的备注）"
    }
  }
}
```
> **每个字段都可以省略**，省了就用默认值。

### 模型别名：让 agent 自己挑便宜模型

`models` 里除 `main` / `subagent` 外的键都是**给 `spawn_subagent` 用的别名**：

```json
"models": { "main": "deepseek-reasoner", "cheap": "qwen3.8-flash" }
```
模型调 `spawn_subagent(task, model="cheap")` 时，`"cheap"` 会被翻译成真实模型名。
**为什么要有这层**：模型记不住厂商那串模型 ID，也不该记——让人在配置里起短名，它才可能主动挑"便宜的那个"。横幅里会显示别名表，一眼能看出配了哪些。

### 关于 .env 的遗留支持

项目早期用 `.env` 管配置，现在**推荐只用 `config.json`**——配置项一多，"哪几项属于同一个功能"在扁平的环境变量里根本看不出来。

`.env` 仍然会被读取（优先级高于 `config.json`），所以老配置不用搬家。它的键名与 json 字段一一对应：`LLM_API_KEY` ↔ `api_key`、`LLM_MODEL` ↔ `models.main`、`QQ_APP_ID` ↔ `channels.qq.app_id`、`AGENT_MAX_STEPS` ↔ `max_iterations`、`MCP_SERVERS` ↔ `mcp_servers`……

---
## 项目结构

```text
DeepsClaw/
├── main.py                   入口：装配所有零件并启动（唯一知道"谁接谁"的地方）
├── gateway.py                网关：按会话把消息分给对的 agent，把回复分给对的渠道
├── config.py                 配置：读 config.json / .env / 环境变量，三级合并
├── identity.md               人设：写进 System Prompt，决定 agent 的性格与纪律
├── config.example.json       配置模板（不含密钥，可提交）
│
├── agent/                    大脑
│   ├── loop.py               主循环：调模型 → 执行工具 → 再调模型，直到有结论
│   ├── context.py            拼 System Prompt 与 messages
│   ├── events.py             输出事件与 Sink（终端呈现、测试收集都是它的实现）
│   ├── memory.py             历史压缩：把过长的历史折叠成一条摘要
│   ├── skills.py             技能加载器（skills/ 下的 SKILL.md）
│   └── tools/                工具箱
│       ├── base.py           Tool 抽象基类：name / description / parameters / execute
│       ├── registry.py       注册与分发：模型的 tool_call → 具体的工具实例
│       ├── filesystem.py     read_file / write_file / list_dir（带沙箱校验）
│       ├── shell.py          exec（超时、输出截断、工作区限制）
│       ├── web_search.py     联网搜索（博查）
│       ├── web_fetch.py      抓网页并转成可读文本
│       ├── memory.py         save_memory（写长期记忆）
│       ├── spawn.py          spawn_subagent（派临时子智能体）
│       └── mcp.py            MCP 客户端：把外部 MCP Server 的工具接进来
│
├── bus/                      神经：渠道与 agent 之间的队列
│   └── queue.py              InboundMessage / OutboundMessage / MessageBus
│
├── channels/                 感官：每个渠道一个适配器
│   ├── base.py               Channel 抽象基类（start / send / stop）
│   ├── cli.py                终端渠道
│   └── qq.py                 QQ 官方机器人渠道
│
├── providers/                嘴：把各家模型 API 的差异挡在外面
│   ├── base.py               LLMProvider 抽象 + LLMResponse / ToolCallRequest
│   └── openai_compat.py      OpenAI 兼容实现（DeepSeek / 百炼 / 硅基流动 / vLLM…）
│
├── session/                  记忆：会话落盘
│   └── manager.py            JSONL 读写，一个会话一个文件
│
├── mcp_servers/              自己写的 MCP Server
│   ├── README.md             怎么加别人的 MCP Server + 排查表
│   └── poetry_server.py      示例：古诗词查询（3 个工具）
│
├── skills/                   技能：给模型看的操作手册
│   ├── code-review/SKILL.md
│   └── weather/SKILL.md
│
└── workspace/                运行时数据 + 文件工具的读写范围（与代码分开）
    ├── sessions/             每个会话一个 .jsonl
    └── memory/MEMORY.md      长期记忆
```
> `workspace/` 是运行数据，不是代码。迁移或备份时拷走它一个目录就够了。

---

## 架构

### 消息怎么流动

```text
    你输入                渠道                 总线                网关                 大脑
  ─────────▶  CLIChannel.start()  ──▶  inbound_queue  ──▶  _process_inbound  ──▶  AgentLoop
                                                              │                      │
                                                   按 session_key 选 agent      调模型 + 调工具
                                                              │                      │
    看到回答  ◀──  CLIChannel.send()  ◀──  outbound_queue  ◀──  _dispatch_outbound ◀─┘
```
### 为什么这么分

三层各不认识对方，**唯一的交界是总线**：

- **渠道**只认 `MessageBus`：把用户消息推进 inbound，把回复发出去。它不知道 agent 的存在，
  所以能被单独测试，也能被复用（比如只做转发）；
- **网关**只做路由：收到 `InboundMessage`，按 `渠道:用户` 算出会话键，把活交给那个会话的 agent；
  回复则按 `channel` 字段找回原来的渠道；
- **agent** 只认自己的输入输出：给它一段文本，它调模型、调工具，还你一段文本。
  它甚至不知道"用户"是终端还是 QQ。

这样分层的好处很实际：**加一个渠道不用碰 agent，换一个模型不用碰渠道，加一个工具不用碰任何一层**。

### 每个会话一个 agent，会话之间不串台

```text
cli:local        → AgentLoop #1（独立历史、独立工具窗口）
qq:用户A          → AgentLoop #2
qq:用户B          → AgentLoop #3
```
网关按会话键缓存 agent。**如果每条消息都新建一个**，历史就断在每句话上，用户会得到一个"每次都失忆"的机器人。

---

## 内置工具

| 工具 | 作用 | 关键约束 |
|---|---|---|
| `read_file` | 读文本文件 | 路径必须在工作区内 |
| `write_file` | 写文件 | 自动建父目录；写入前校验沙箱 |
| `list_dir` | 列目录 | 显示大小与类型 |
| `exec` | 执行命令 | 有超时、输出截断、工作区限制 |
| `web_search` | 联网搜索 | 需要博查 API key，否则该工具提示未配置 |
| `web_fetch` | 抓网页正文 | 自动去掉脚本样式，转成可读文本 |
| `save_memory` | 写长期记忆 | 路径只来自配置，模型无法指定写到别处 |
| `spawn_subagent` | 派临时子智能体 | 需配密钥；有一层深度上限 |
| `server__tool` | MCP 来的工具 | 名字带 server 前缀；数量取决于连上了哪些 server |

用 `/tools` 看当前实际装配了哪些。

### 写一个新工具

```python
from agent.tools.base import Tool
from typing import Any

class WeatherTool(Tool):
    @property
    def name(self) -> str:
        return "get_weather"

    @property
    def description(self) -> str:
        return "查询指定城市的实时天气。适合用户问出门穿什么、要不要带伞时使用。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名"}},
            "required": ["city"],
        }

    async def execute(self, city: str = "", **kwargs: Any) -> str:
        return f"{city} 今天晴，25℃"
```
在 `main.py` 的 `_build_registry()` 里 `registry.register(WeatherTool())` 即可。

> `description` 是模型判断"什么时候该用这个工具"的唯一依据，**写清使用场景比写清功能更重要**。

---
## 子智能体

有些任务塞进主对话会污染上下文：读完十几个文件只为回答一个问题、试三次才跑通的一段命令。
这类"**过程很长、结论很短**"的活可以派给一个临时专员：

```text
主 agent ──spawn_subagent(task)──▶ 临时 AgentLoop（自己的模型、工具、一次性历史）
   ▲                                        │
   └────────── 只把结论交回来 ───────────────┘
```
它长这样：

- **系统提示只有一句话**："你是任务专员，完成任务直接输出结果"——不带人设、记忆与技能；
- **不写磁盘**：历史只活在这次调用里，不会在 `workspace/sessions/` 留下一堆一次性文件；
- **看不到主对话**：`task` 就是它知道的全部，所以派活时必须把背景写清楚；
- **不能反问**：它只干一件事，干完就散；
- **有深度上限**：`max_depth=2`，子智能体最多再派一层。

### 什么时候该派

判断标准一句话：**"这件事要我翻好几个文件、跑好几条命令才答得出来吗？用户只关心最后那句话吗？"**
两个都是"是"，就派给它。

| 该派 | 不该派 |
|---|---|
| 扫多个文件才能回答（"配置从哪读的""哪个环节负责重试"） | 一步就能做完的小事（看目录、读一个小文件） |
| 连续试错才有结果（定位一个报错） | 需要用户拍板的事（它不能反问，只会猜） |
| 汇总一大批材料（搜索结果、日志归类） | 要改用户东西的事（落笔改动自己来） |

> 这套判断写在 [`identity.md`](identity.md) 里，是**人设的一部分**。原因很实际：工具装上了、说明也写了，模型默认还是倾向"自己动手往下刨"——
> 得给它一个可当场自检的动作，它才会真的去用。

### 给子智能体换个模型

```json
"models": { "main": "deepseek-reasoner", "cheap": "qwen3.8-flash" },
"subagent": { "api_key": "sk-百炼密钥", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1" }
```
子任务天然是"多跑几次模型、少动脑子"的活，很适合换成便宜模型。配了 `cheap` 别名后，模型可以自己说 `spawn_subagent(task, model="cheap")`。

---

## 接入 MCP Server

[MCP（Model Context Protocol）](https://modelcontextprotocol.io) 让 agent 从**外部进程**加载工具：
别人写好的 server 不用改一行代码，配一行就能用。

### 加一个本地 server

```json
"mcp_servers": {
  "poetry": { "command": "python", "args": ["mcp_servers/poetry_server.py"] }
}
```
### 加一个第三方 server

多数现成的 MCP Server 是 Node 生态的，靠 `npx` 拉起：

```json
"mcp_servers": {
  "filesystem": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "./"]
  },
  "everything": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-everything"]
  }
}
```
> 需要本机有 `node` + `npx`。首次运行要下载包，会慢几秒（连接超时默认 30 秒）。

### 连上之后

```text
[MCP] 开始连接 2 个 server：poetry、everything
[MCP] 已连接 poetry：3 个工具（search_poetry、random_poetry、list_poets）
[MCP] 已连接 everything：13 个工具（echo、get-sum、...）
[MCP] 连接结束：2/2 个 server 可用，共 16 个工具
```
工具名是 `server名__工具名`（两个下划线），在 `/tools` 里能看到，agent 也就能直接调用：

```text
你 > 随便背一首古诗
[工具] poetry__random_poetry({})
[结果] poetry__random_poetry → 《春晓》—— 孟浩然（唐）
AI > 《春晓》—— 孟浩然（唐）：春眠不觉晓，处处闻啼鸟……
```
### 三个要知道的行为

- **一个连不上不影响其他**：失败或超时只跳过那一个，agent 照常启动；
- **路径含空格要加引号**：命令由客户端直接执行、不经过 shell，Windows 上建议写解释器绝对路径；
- **子进程的 stdout 是协议通道**：自己写 server 时绝不能往 stdout 打日志，要打就写 stderr。

完整说明与排查表见 [`mcp_servers/README.md`](mcp_servers/README.md)。

### 自己写一个 MCP Server

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("my-server")

@mcp.tool()
def my_tool(keyword: str) -> str:
    """一句话说清这个工具什么时候用。"""
    return "结果"

if __name__ == "__main__":
    mcp.run(transport="stdio")
```
放进 `mcp_servers/`，在 `config.json` 里登记，重启即可。示例见 [`poetry_server.py`](mcp_servers/poetry_server.py)。

---

## 接入新渠道

以"飞书"为例，只需要一个文件：

```python
from channels.base import Channel
from bus.queue import InboundMessage, OutboundMessage

class FeishuChannel(Channel):
    def __init__(self, bus):
        super().__init__("feishu", bus)   # 名字必须全局唯一：它既是路由键，也是会话键前缀

    async def start(self) -> int:
        # 把收到的消息拍平后推进总线
        await self.bus.publish_inbound(InboundMessage(
            channel=self.name, sender_id=..., chat_id=..., content=...))
        return 0

    async def send(self, message: OutboundMessage) -> None:
        # 把回复发出去；必须 await 到真正发完
        ...
```
然后在 `main.py` 的渠道列表里加一行。**agent 侧一行都不用改**。

两条硬约束（写在 `channels/base.py` 的文档里）：

1. **`name` 全局唯一且固定**——它既是消息的路由键，也是会话键（`feishu:用户id`）的前缀；
2. **`start()` 占住自己的协程**——循环写在里面，`start` 返回即表示渠道已退出。

### 已有的渠道

| 渠道 | 状态 | 说明 |
|---|---|---|
| `cli` | ✅ 可用 | 终端交互，`/help` 看本地命令 |
| `qq` | ✅ 可用 | QQ 官方机器人，群聊（@机器人）+ 单聊 |
| `feishu` | 🚧 待实现 | 配置项已支持（`channels.feishu`），适配器待补 |
| `web` | 🚧 待实现 | 配置项已支持（`channels.web`），适配器待补 |

---
## 常见问题

<details>
<summary><b>启动报「配置错误：未配置模型密钥」</b></summary>

两种配法选一种：

- 在 `config.json` 里写 `{"api_key": "sk-xxx", "base_url": "...", "models": {"main": "..."}}`；
- 或者设环境变量 `LLM_API_KEY=sk-xxx`（`.env` 文件同样会被读取，见[关于 .env 的遗留支持](#关于-env-的遗留支持)）。

注意优先级是**环境变量 > .env > config.json**：如果环境里已有一个旧的同名变量，它会盖掉文件里的配置。

</details>

<details>
<summary><b>模型明明配了却不生效</b></summary>

看启动横幅的「配置」那一行——它显示本次真正读到的是哪个文件。
若显示「仅 .env / 环境变量」而你以为在用 `config.json`，多半是路径不对（`config.json` 要在项目根目录）。

</details>

<details>
<summary><b>思考过程看不见 / 模型不思考了</b></summary>

检查 `config.json` 的 `extra_body`：DeepSeek 需要 `{"thinking": {"type": "enabled"}}`，
百炼等其它服务商不认这个参数（子智能体默认走百炼，所以子智能体的 Provider 刻意不透传它）。

</details>

<details>
<summary><b>Q：搜出来的结果是空的 / web_search 提示未配置</b></summary>

联网搜索走博查 API，需要在 `tools.bocha_api_key` 填 key（[申请地址](https://open.bochaai.com)）。
不填不影响其它功能，只是这一个工具不可用。

</details>

<details>
<summary><b>QQ 机器人收不到消息</b></summary>

按顺序查：

1. `qq-botpy` 装了吗（`pip install qq-botpy`）；
2. `channels.qq.app_id` / `app_secret` 都填了吗（只填一半会明确告警并跳过）；
3. 机器人有「群聊」「单聊」权限吗；
4. **群里必须 @ 机器人才会推送**，这是 QQ 平台的规则，不是 bug。

</details>

<details>
<summary><b>接入的 MCP Server 没连上</b></summary>

启动日志里会有 `[MCP] [!] xxx 连接失败，已跳过`。逐项排查见 [`mcp_servers/README.md`](mcp_servers/README.md) 的排查表；
最常见的两个原因：命令写错（把命令单独在终端跑一遍就知道），或首次 `npx` 下载超时（重跑一次通常会命中缓存）。

</details>

<details>
<summary><b>写到一半的文件、跑一半的命令</b></summary>

`exec` 有超时与输出截断；文件工具做了沙箱校验，路径逃不出工作区。
但 agent 仍可能改坏你的文件——**重要改动请先备份**，或在人设里加更严格的确认要求（见 `identity.md` 的「动手前必须过的检查点」）。

</details>

<details>
<summary><b>会话历史在哪里？怎么清空？</b></summary>

`workspace/sessions/<渠道>_<会话>.jsonl`，一行一条消息，可以直接看、也可以直接删。
在对话里用 `/clear` 会同时清掉内存与磁盘上的历史。

</details>

---

## 开发笔记：几个刻意的设计

这些取舍都写在对应文件的文档字符串里，摘几条出来：

| 设计 | 为什么 |
|---|---|
| **两层队列（进/出分开）** | 合成一条的话，agent 发回复时若一时没人取，后面的用户消息就全堵住了 |
| **消息是值对象** | 不持有渠道客户端引用、不带回调，所以能被直接断言、被落盘、被转发 |
| **`session_key` = 渠道:用户** | 同一人在 QQ 和终端有各自的上下文；按外部账号合并需要身份映射表，是另一个功能 |
| **工具结果回传模型的是全文、推给前端的是预览** | `read_file` 读一个几 MB 的文件，原样推给终端会把屏幕刷爆 |
| **MCP 的上下文必须手动 `__aenter__`** | 用 `with` 语法糖会在初始化后就拆掉后台任务，`initialize()` 直接死等 |
| **MCP 断开要按连接的逆序** | 多个 server 会叠成一摞 anyio cancel scope，先进先出地退会撞作用域检查（一个 server 时完全正常，两个就必炸） |
| **`AgentLoop` 出错时把失败变成文本返回** | 网络失败、熔断、步数耗尽都兜成一段说明；抛出异常会让整轮任务断在半路，渠道那边什么都收不到 |
| **工具循环熔断** | 同一组工具调用重复 20 次就中止——模型陷入死循环时，每轮都在烧钱 |

### 几个数字

| 项 | 值 | 为什么是这个数 |
|---|---|---|
| 主 agent 最大步数 | 50 | 单轮最多调 50 次模型；再多说明任务该拆了 |
| 子 agent 最大步数 | 20 | 只干一件事，20 步还没结论说明方向错了 |
| MCP 连接超时 | 30s | 给"装依赖、下载包"留余量，同时不让用户干等 |
| 工具重复熔断 | 20 次 | 见上；10 次开始给模型提示 |
| 历史压缩预算 | 24000 tokens | 可配；0 表示关闭 |

---

## 安全提醒

- **密钥不要提交**：含密钥的 `config.json` 与 `.env` 都已在 `.gitignore` 里；要分享配置请用 `config.example.json`（它不含任何密钥）。
- **文件工具有沙箱**：只能读写 `workspace` 指定的目录，路径逃逸会被拒绝。
- **命令执行是真实执行的**：`exec` 会真正运行命令，**不要给不可信的输入开放这个工具**。
- **agent 会改文件**：默认人设要求它在改动用户代码前先确认，但这只是**提示词层面的约束**，不是沙箱。重要目录请自行备份，或把 `workspace` 指到一个安全位置。
- **敏感文件**：`identity.md` 里已写明禁止读取 `.env` 等隐私文件——同样是提示词约束，不要把它当作访问控制。

---

## 已知限制

- **入站处理是串行的**：`_process_inbound` 单协程消费，某个会话跑一轮要 30 秒时，其他人的消息会排在它后面。要并发得按会话分派 worker 并给缓存加锁；
- **飞书 / Web 渠道只有配置项**，适配器还没写；
- **渠道退出即整体收工**：终端敲 `/exit` 会连带停掉 QQ 渠道（语义上"退出程序"）。想长期挂机就别敲 `/exit`，用 Ctrl+C 同样是优雅退出；
- **没有内置测试套件**：验证靠手工跑与临时脚本，`pytest` 缓存目录是历史遗留；
- **单进程**：没有分布式、没有任务队列，进程退出即全部停止。

---

## 如何贡献

欢迎 issue 与 PR。这个项目的风格约定写在每个模块的文档字符串里，改动前建议先读一读：

1. **文档字符串写"为什么"**，而不只是"做什么"——每个非显然的取舍都留了理由；
2. **失败要降级、不要抛出**：工具与渠道的失败都应变成可读文本交回模型或记日志；
3. **新增能力优先加"扩展点"**：能做成配置项或插件，就不要写死在代码里；
4. **不要用 emoji 打日志**：Windows 默认代码页是 GBK，会让进程直接崩掉（`[MCP]` / `[!]` 这类 ASCII 前缀是安全的）。

---

## License

MIT

> ⚠️ 仓库目前**还没有 `LICENSE` 文件**。GitHub 只有在检测到该文件时才会显示协议信息，
> 建议补一个——把标准的 MIT 全文存成 `LICENSE` 即可。在此之前默认版权保留，他人无权直接复用。
