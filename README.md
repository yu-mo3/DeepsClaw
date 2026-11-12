# DeepsClaw

一个能挂在终端和 QQ 上的 AI agent，顺带把 agent 该有的零件都自己写了一遍。

起因很简单：想要一个能读文件、跑命令、还会自己查资料的小助手，但不想为了这点事把整个 LangChain 拖进来。于是主循环、工具、消息总线、网关、渠道、模型 Provider 都自己写了。代码不算少，好处是每一层都拆得比较开——想换模型、加渠道、加工具，基本只动一处。

现在它能做的事：

- 在终端里聊天，历史自动存盘，关掉再打开能接着聊
- 挂成 QQ 机器人，群里 @ 它、私聊都行
- 读写文件、执行命令、联网搜索、抓网页，模型自己决定什么时候用哪个
- 接任意 MCP Server，别人写好的工具配一行就能用
- 对话太长时自动压缩中间那段，长会话不会把上下文撑爆
- 把"过程长、结论短"的活派给子智能体，不占主对话的上下文

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-supported-6E56CF)

> 先说清楚：这是一个按"参考实现"来写的项目，不是生产级框架。入站处理是串行的，也没有测试套件，已知限制都列在后面。想看得更深，可以翻 [ARCHITECTURE.md](ARCHITECTURE.md)（代码结构、流程图、踩坑清单）和 [HANDOVER.md](HANDOVER.md)（接手说明）。

## 跑起来

环境要求不高：Python 3.10 以上（开发机是 3.14，代码用了 `X | None` 这种写法，没碰 3.11 独有的特性），再加一个 OpenAI 兼容接口的模型密钥——DeepSeek、百炼、硅基流动、OpenAI 都行。

依赖装四个：

```bash
pip install python-dotenv openai httpx PyYAML
```

- `openai`：所有模型调用都走它，不装项目起不来
- `httpx`：联网搜索和抓网页用，openai 也依赖它
- `python-dotenv`：读 `.env`，老配置兼容用
- `PyYAML`：解析技能文件开头的 frontmatter，`agent/skills.py` 顶层就 import 了

另外两个是可选包，不装也能跑，只是对应功能关掉：

```bash
pip install qq-botpy   # 用 QQ 渠道才需要
pip install mcp        # 接 MCP Server 才需要
```

配置从模板复制一份，填上密钥：

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

只写这些就能启动，其余字段都有默认值，用到再补。然后：

```bash
python main.py
```

启动后的横幅大概长这样：

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

也可以问完就走：

```bash
echo "把 README 里的错别字找出来" | python main.py
```

## 配置

配置都在项目根目录的 `config.json` 里，模板是 [config.example.json](config.example.json)。

优先级是：环境变量 > `.env` > `config.json` > 内置默认值。注意是逐项合并，不是整份替换——`config.json` 里没写的项会继续往下找。所以"我明明改了却不生效"最常见的原因，就是环境里有个同名的旧变量把它盖掉了。

完整字段（大部分可以省略）：

```jsonc
{
  "api_key": "sk-xxx",                    // 主模型密钥（必填，或用 .env 的 LLM_API_KEY）
  "base_url": "https://api.deepseek.com/v1",
  "models": {
    "main":     "deepseek-chat",           // 主对话用的模型
    "subagent": "qwen3.8-flash",           // 子智能体的默认模型
    "cheap":    "deepseek-chat"            // 其余键是别名，见下面
  },

  "workspace": ".",                       // 文件工具的沙箱根目录
  "identity_file": "identity.md",         // 人设文件（相对 workspace）
  "skills_dir": "skills",                 // 技能目录（相对项目根目录）
  "max_iterations": 50,                   // 单轮最多调用几次模型
  "timeout": 120,                         // 单次请求超时（秒）
  "log_level": "INFO",
  "extra_body": { "thinking": { "type": "enabled" } },  // 原样透传给接口的额外参数

  "memory":  { "file": "workspace/memory/MEMORY.md", "token_budget": 24000 },
  "tools":   { "bocha_api_key": "" },

  "subagent": {
    "api_key": "",                        // 留空则子智能体不可用；填主密钥可复用主模型账号
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

### 模型别名

`models` 里除了 `main` 和 `subagent`，其他键都可以当成别名用：

```json
"models": { "main": "deepseek-reasoner", "cheap": "qwen3.8-flash" }
```

模型调 `spawn_subagent(task, model="cheap")` 时，`"cheap"` 会被翻译成真实模型名。

留这一层是因为模型记不住厂商那串模型 ID，也不该让它记。人在配置里起个短名，它才有可能主动挑便宜的那个。启动横幅会显示配了哪些别名。

### 关于 .env

项目早期用 `.env` 管配置，现在推荐只用 `config.json`——配置项一多，哪几项属于同一个功能，在扁平的环境变量里根本看不出来。

不过 `.env` 仍然会被读取，优先级还比 `config.json` 高，所以老配置不用搬家。键名和 json 字段基本一一对应：`LLM_API_KEY` ↔ `api_key`、`LLM_MODEL` ↔ `models.main`、`QQ_APP_ID` ↔ `channels.qq.app_id`、`MCP_SERVERS` ↔ `mcp_servers`，依此类推。

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

`workspace/` 里是运行数据，不是代码。备份或者迁移的时候拷走这一个目录就够了。

## 架构

一条消息大概这么走：

```text
    你输入                渠道                 总线                网关                 大脑
  ─────────▶  CLIChannel.start()  ──▶  inbound_queue  ──▶  _process_inbound  ──▶  AgentLoop
                                                              │                      │
                                                   按 session_key 选 agent      调模型 + 调工具
                                                              │                      │
    看到回答  ◀──  CLIChannel.send()  ◀──  outbound_queue  ◀──  _dispatch_outbound ◀─┘
```

渠道、网关、agent 三层互相不认识，唯一的交界是总线：

- 渠道只认 `MessageBus`，把用户消息推进 inbound，把回复发出去。它不知道 agent 的存在，所以能单独测、也能复用。
- 网关只做路由：收到消息，按 `渠道:用户` 算出会话键，把活交给那个会话的 agent；回复再按 `channel` 字段找回原来的渠道。
- agent 只管输入输出：给它一段文本，它调模型、调工具，还你一段文本。它甚至不知道对面是终端还是 QQ。

这么分的好处很实际：加一个渠道不用碰 agent，换一个模型不用碰渠道，加一个工具不用碰任何一层。

每个会话有自己的 agent，历史互不串台：

```text
cli:local        → AgentLoop #1（独立历史、独立工具窗口）
qq:用户A          → AgentLoop #2
qq:用户B          → AgentLoop #3
```

网关按会话键缓存 agent。如果每条消息都新建一个，历史就断在每句话上，用户会得到一个"每次都失忆"的机器人。

## 内置工具

模型能用的工具在启动时装配，`/tools` 能看到当前实际装了什么。

| 工具 | 作用 | 注意 |
|---|---|---|
| `read_file` | 读文本文件 | 路径必须在工作区内，超 16000 字符截断 |
| `write_file` | 写文件 | 自动建父目录，整文件覆盖，写入前校验沙箱 |
| `list_dir` | 列目录 | 显示大小与类型，单次最多 500 项 |
| `exec` | 执行命令 | 有超时、输出截断、危险命令拦截、环境变量白名单 |
| `web_search` | 联网搜索 | 需要博查 key，否则这个工具提示未配置 |
| `web_fetch` | 抓网页正文 | 去掉脚本样式转成可读文本，带 SSRF 防护 |
| `save_memory` | 写长期记忆 | 路径只来自配置，模型指定不了写到别处 |
| `spawn_subagent` | 派临时子智能体 | 配了密钥才注册，深度上限 2 |
| `server__tool` | MCP 来的工具 | 名字带 server 前缀，数量取决于连上了哪些 server |

### 写个新工具

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

在 `main.py` 的 `_build_registry()` 里 `registry.register(WeatherTool())` 就行。

`description` 是模型判断"什么时候该用这个工具"的唯一依据，写清使用场景比写清功能更有用。

## 子智能体

有些活做起来很啰嗦：读十几个文件只为回答一个问题、试三次才跑通一条命令。这类"过程长、结论短"的任务可以派给子智能体——它自己查、自己试，最后只把结论交回来。

```text
主 agent ──spawn_subagent(task)──▶ 临时 AgentLoop（自己的模型、工具、一次性历史）
   ▲                                        │
   └────────── 只把结论交回来 ───────────────┘
```

它的几个特点：

- 系统提示只有一句话："你是任务专员，完成任务直接输出结果"，没有人设、记忆和技能
- 不写磁盘，过程不会在 `workspace/sessions/` 留下一次性文件
- 看不到主对话，`task` 就是它知道的全部，所以派活时要把背景说清楚
- 不能反问，只干一件事，干完就散
- 最多再往下派一层（`max_depth=2`）

判断要不要派，我自己用一句话：这件事得翻好几个文件、跑好几条命令才答得出来吗？用户只关心最后那句话吗？两个都是"是"，就派给它。

| 适合派 | 不适合派 |
|---|---|
| 扫多个文件才能回答（配置从哪读的、哪个环节负责重试） | 一步就能做完的小事（看目录、读一个小文件） |
| 连续试错才有结果（定位一个报错） | 需要用户拍板的事（它不能反问，只会猜） |
| 汇总一大批材料（搜索结果、日志归类） | 要改用户东西的事（落笔改动自己来） |

这套判断写在 [identity.md](identity.md) 里，是提示词的一部分。原因挺实际：工具装上了、说明也写了，模型默认还是倾向自己往下刨，得给它一个可当场自检的动作，它才会真的去用。

想让子智能体换个便宜模型：

```json
"models": { "main": "deepseek-reasoner", "cheap": "qwen3.8-flash" },
"subagent": { "api_key": "sk-百炼密钥", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1" }
```

子任务天然是"多跑几次模型、少动脑子"的活，很适合换成便宜模型。配了 `cheap` 别名之后，模型可以自己说 `spawn_subagent(task, model="cheap")`。

## 接入 MCP Server

[MCP（Model Context Protocol）](https://modelcontextprotocol.io) 让 agent 从外部进程加载工具，别人写好的 server 不用改一行代码，配一行就能用。

加一个本地 server：

```json
"mcp_servers": {
  "poetry": { "command": "python", "args": ["mcp_servers/poetry_server.py"] }
}
```

加一个第三方 server（多数现成的 MCP Server 是 Node 生态的，靠 `npx` 拉起）：

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

需要本机有 `node` 和 `npx`。首次运行要下载包，会慢几秒，连接超时默认 30 秒。

连上之后日志大概是这样：

```text
[MCP] 开始连接 2 个 server：poetry、everything
[MCP] 已连接 poetry：3 个工具（search_poetry、random_poetry、list_poets）
[MCP] 已连接 everything：13 个工具（echo、get-sum、...）
[MCP] 连接结束：2/2 个 server 可用，共 16 个工具
```

工具名是 `server名__工具名`（两个下划线），在 `/tools` 里能看到，agent 直接就能调：

```text
你 > 随便背一首古诗
[工具] poetry__random_poetry({})
[结果] poetry__random_poetry → 《春晓》—— 孟浩然（唐）
AI > 《春晓》—— 孟浩然（唐）：春眠不觉晓，处处闻啼鸟……
```

几个容易踩的点：

- 一个连不上不影响其他：失败或超时只跳过那一个，agent 照常启动
- 路径按项目根解析：`args` 里的相对路径（比如 `mcp_servers/poetry_server.py`）会按项目根目录解析成绝对路径，`cwd` 默认也是项目根，从哪个目录启动都不影响连接
- 命令建议写 `python`：命令由客户端直接执行、不经过 shell，裸命令名走 PATH；解释器路径含空格时必须写绝对路径并加引号
- 子进程的 stdout 是协议通道：自己写 server 时千万别往 stdout 打日志，要打就写 stderr

完整说明和排查表在 [mcp_servers/README.md](mcp_servers/README.md)。

自己写一个 server 也不麻烦：

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

放进 `mcp_servers/`，在 `config.json` 里登记，重启即可。示例见 [poetry_server.py](mcp_servers/poetry_server.py)。

## 接入新渠道

以飞书为例，一个文件就够了：

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

然后在 `main.py` 的渠道列表里加一行，agent 侧不用改。

两条硬约束（写在 `channels/base.py` 的文档里）：

1. `name` 全局唯一且固定——它既是消息的路由键，也是会话键（`feishu:用户id`）的前缀
2. `start()` 占住自己的协程——循环写在里面，`start` 返回即表示渠道已经退出

已有的渠道：

| 渠道 | 状态 | 说明 |
|---|---|---|
| `cli` | 可用 | 终端交互，`/help` 看本地命令 |
| `qq` | 可用 | QQ 官方机器人，群聊（@机器人）+ 单聊 |
| `feishu` | 待实现 | 配置项已支持（`channels.feishu`），适配器待补 |
| `web` | 待实现 | 配置项已支持（`channels.web`），适配器待补 |

## 常见问题

<details>
<summary>启动报「配置错误：未配置模型密钥」</summary>

两种配法选一种：在 `config.json` 里写 `api_key` / `base_url` / `models.main`，或者设环境变量 `LLM_API_KEY=sk-xxx`（`.env` 文件同样会被读取）。

注意优先级是环境变量 > `.env` > `config.json`，环境里如果已经有一个旧的同名变量，它会盖掉文件里的配置。

</details>

<details>
<summary>模型明明配了却不生效</summary>

看启动横幅的「配置」那一行，它显示本次真正读到的是哪个文件。如果显示「仅 .env / 环境变量」而你以为在用 `config.json`，多半是路径不对——`config.json` 要放在项目根目录。

</details>

<details>
<summary>思考过程看不见，或者模型不思考了</summary>

检查 `config.json` 的 `extra_body`：DeepSeek 需要 `{"thinking": {"type": "enabled"}}`，百炼之类的服务商不认这个参数（子智能体默认走百炼，所以子智能体的 Provider 刻意不透传它）。

</details>

<details>
<summary>搜出来的结果是空的 / web_search 提示未配置</summary>

联网搜索走博查 API，需要在 `tools.bocha_api_key` 里填 key（[申请地址](https://open.bochaai.com)）。不填不影响其他功能，只是这一个工具用不了。

</details>

<details>
<summary>QQ 机器人收不到消息</summary>

按顺序查：`qq-botpy` 装了吗；`channels.qq.app_id` 和 `app_secret` 都填了吗（只填一半会明确告警并跳过）；机器人有群聊和单聊权限吗；最后，群里必须 @ 它才会推送，这是 QQ 平台的规则，不是 bug。

</details>

<details>
<summary>接入的 MCP Server 没连上</summary>

启动日志里会有 `[MCP] [!] xxx 连接失败，已跳过`。逐项排查见 [mcp_servers/README.md](mcp_servers/README.md) 的排查表。最常见的两个原因：命令写错（把命令单独在终端跑一遍就知道了），或者首次 `npx` 下载超时（重跑一次通常会命中缓存）。

</details>

<details>
<summary>写到一半的文件、跑一半的命令</summary>

`exec` 有超时和输出截断，文件工具做了沙箱校验，路径逃不出工作区。但 agent 仍然可能改坏你的文件，重要改动请先备份，或者在 [identity.md](identity.md) 的「动手前必须过的检查点」里加更严格的确认要求。

</details>

<details>
<summary>会话历史在哪里？怎么清空？</summary>

`workspace/sessions/<渠道>_<会话>.jsonl`，一行一条消息，可以直接看，也可以直接删。在对话里用 `/clear` 会同时清掉内存和磁盘上的历史。

</details>

## 几个看起来奇怪的设计

写代码时有些取舍乍看有点绕，这里一并解释。更细的说明在各个模块的文档字符串里。

- 进出两条队列。合成一条的话，agent 正在发回复而暂时没人取时，后面的用户消息就全堵住了。
- 消息是纯数据。不持有渠道客户端引用、不带回调，所以能直接断言、落盘、转发。
- 会话键是 `渠道:用户`。同一个人在 QQ 和终端各聊各的；想按外部账号合并，得先有身份映射表，那是另一个功能。
- 工具结果给模型全文、给前端预览。`read_file` 读一个几 MB 的文件，原样推到终端会把屏幕刷爆。
- MCP 的上下文必须手动 `__aenter__`。用 `with` 语法糖会在初始化后就拆掉后台任务，`initialize()` 直接死等。
- MCP 断开要按连接的逆序。多个 server 会叠成一摞 anyio cancel scope，正序退会撞作用域检查——一个 server 时完全正常，两个就必炸。这个坑挺隐蔽的。
- `AgentLoop` 出错时把失败变成文本返回。网络失败、熔断、步数耗尽都兜成一段说明；抛异常会让整轮任务断在半路，渠道那边什么都收不到。
- 工具循环熔断。同一组工具调用重复 20 次就中止——模型陷入死循环时，每轮都在烧钱。

几个数字，顺便说下为什么是这些数：

| 项 | 值 | 为什么 |
|---|---|---|
| 主 agent 最大步数 | 50 | 单轮最多调 50 次模型，再多说明任务该拆了 |
| 子 agent 最大步数 | 20 | 只干一件事，20 步还没结论说明方向错了 |
| MCP 连接超时 | 30s | 给装依赖、下载包留余量，同时不让用户干等 |
| 工具重复熔断 | 20 次 | 见上，10 次开始给模型提示 |
| 历史压缩预算 | 24000 tokens | 可配，0 表示关闭 |

## 安全提醒

- 密钥不要提交。含密钥的 `config.json` 和 `.env` 都在 `.gitignore` 里；要分享配置请用 `config.example.json`，它不含密钥。
- 文件工具有沙箱。只能读写 `workspace` 指定的目录，路径逃逸会被拒绝。
- 命令执行是真的执行。`exec` 会真正运行命令，不要让不可信的输入用这个工具。危险命令黑名单和子进程环境变量白名单只是降低误伤面；黑名单是文本正则，防不住被注入的模型（写脚本再执行就能绕过），真正的隔离要靠受控的 `workspace` 和用户侧确认。
- agent 会改文件。默认人设要求它改动前先确认，但那只是提示词层面的约束，不是沙箱。重要目录请自行备份，或者把 `workspace` 指到一个安全位置。
- 敏感文件有名字黑名单。`read_file` / `write_file` 会拒绝 `.env`、`config.json`、`secrets.json`、`id_rsa` 这类文件（`.env.example` 等模板除外）。不过黑名单不是完备的访问控制，真正的隔离还是靠 `workspace` 范围。

## 已知限制

- 入站处理是串行的。`_process_inbound` 单协程消费，某个会话跑一轮要 30 秒时，其他人的消息就排在后面。要并发得按会话分派 worker，还得给缓存加锁。
- 飞书和 Web 渠道只有配置项，适配器还没写。
- 渠道退出即整体收工。终端敲 `/exit` 会连带停掉 QQ 渠道，语义上就是"退出程序"。想长期挂机就别敲 `/exit`，Ctrl+C 同样是优雅退出。
- 没有内置测试套件。验证靠手工跑和临时脚本，`pytest` 缓存目录是历史遗留。
- 单进程。没有分布式、没有任务队列，进程退出就全停了。

## 想改的话

欢迎 issue 和 PR。项目有几条不太正式但还算稳定的风格约定，写在各个模块的文档字符串里：

1. 文档字符串写"为什么"，不只写"做什么"——每个不显然的取舍都留了理由
2. 失败要降级：工具和渠道的报错都变成可读文本交回模型或记日志，别往上层抛
3. 能做成配置项或扩展点，就别写死在代码里
4. 日志别用 emoji：Windows 默认代码页是 GBK，emoji 会让进程直接崩，`[MCP]`、`[!]` 这类 ASCII 前缀是安全的

动手之前建议翻一下 [ARCHITECTURE.md](ARCHITECTURE.md) 的第 7、8 节，坑和接口约定都在那儿。
