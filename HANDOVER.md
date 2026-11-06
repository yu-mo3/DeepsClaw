# DeepsClaw 项目交接文档

> 面向：**接手这个项目的开发者**。
> 目的：看完这一份，能跑起来、能改功能、不踩已知的坑。
> 写法：大白话，不绕弯；每说一个功能都告诉你**代码在哪个文件**。

---

## 1. 项目概述

### 这是个什么东西

一句话：**一个自己写的 AI Agent 框架**。你给它一个问题，它会自己决定要不要读文件、跑命令、
联网搜索，然后把答案给你。它不止能在终端里用，还能挂成 QQ 机器人，也能加载别人写的 MCP 工具。

跟"调一次大模型 API"的区别就在这：**它会自己动手，并且能挂到多个渠道上长期运行**。

### 它解决什么问题

| 问题 | 这个项目的做法 |
|---|---|
| 模型只会聊天，不会动你的文件 | 给它一套工具（读写文件、跑命令、联网），它自己判断该用哪个 |
| 换个模型厂商就要改代码 | Provider 层把各家 API 差异挡住，换厂商只改配置 |
| 想在 QQ / 终端 / 网页都用同一个 agent | 渠道层做成插件，agent 完全不知道用户在哪个渠道 |
| 想加个能力就得改 agent 代码 | MCP 支持：外部进程提供工具，配一行就能用 |
| 长对话把上下文撑爆 | 历史压缩：自动把中间那段折叠成摘要 |
| 复杂任务的中途过程污染对话 | 子智能体：把"过程长结论短"的活派出去，只说结论 |

### 现在实际能干什么（跑起来的状态）

- 在**终端**里多轮对话，会话自动存盘，关掉再开能接着聊；
- 挂成 **QQ 官方机器人**（群聊要 @ 它、也能单聊）；
- 用 8 个**内置工具**干活：读/写文件、列目录、执行命令、联网搜索、抓网页、写长期记忆、派子智能体；
- 连 **MCP Server** 拿别人的工具（仓库里自带一个古诗词查询的示例）；
- 用 `skills/` 下的技能文件给模型补操作手册。

### 业务目标 / 定位

从代码结构看，这个项目的定位是**"一份把 agent 该有的零件都装好的参考实现"**：
每一层都拆得很清楚（渠道 / 网关 / agent / 工具 / Provider），每处取舍都在文档字符串里写了理由。
所以它的价值不只在"能用"，更在"能改"——你想换掉其中一层，不用动其他层。

---
## 2. 功能清单（每条都标了代码在哪）

说明：下表按"功能模块"分组。**加粗的是文件路径**，后面括号里是那个文件里负责这件事的类或函数。

### 2.1 对话与大脑（agent 核心）

| 功能 | 代码位置 |
|---|---|
| **多轮对话主循环**：调模型 → 执行工具 → 再调模型，直到有结论 | `agent/loop.py`（`AgentLoop.run`、`_run_tool_calls`） |
| **和模型对话的每一轮**：拼历史、发请求、收流式回复 | `agent/loop.py`（`run` 里的 for 循环、`provider.chat` 调用） |
| **System Prompt 组装**：人设 + 时间 + 工作区 + 长期记忆 + 技能摘要 | `agent/context.py`（`ContextBuilder.build_system_prompt`、`build_messages`） |
| **人设加载**：读 `identity.md`，读不到就用内置默认人设 | `agent/context.py`（`_load_identity`、`DEFAULT_IDENTITY`） |
| **长期记忆注入**：把 `MEMORY.md` 读出来塞进 System Prompt，超 4000 字截断 | `agent/context.py`（`_load_memory`、`MAX_MEMORY_CHARS`） |
| **历史压缩**：对话太长时把中间那段摘要成一条 system 消息 | `agent/memory.py`（`MemoryConsolidator.maybe_consolidate`、`_summarize`） |
| **压缩时归档原文**：被折叠掉的原文写进 `HISTORY.md` | `agent/memory.py`（`_save_to_history`） |
| **死循环熔断**：同一组工具调用重复 10 次警告、20 次中止 | `agent/loop.py`（`_check_tool_loop`、`LoopCheck`、`CIRCUIT_BREAK_THRESHOLD`） |
| **输出事件流**：思考 / 正文 / 工具调用 / 工具结果 / 错误 / 轮次结束 | `agent/events.py`（`AgentEvent` 及 6 个子类、`OutputSink`） |
| **技能加载**：扫 `skills/*/SKILL.md`，解析 frontmatter，生成摘要进 System Prompt | `agent/skills.py`（`SkillsLoader.build_skills_summary`、`load_skill`、`_parse_frontmatter`） |

### 2.2 模型接入（Provider 层）

| 功能 | 代码位置 |
|---|---|
| **Provider 抽象接口**：统一 `chat()`、`LLMResponse`、`ToolCallRequest` | `providers/base.py`（`LLMProvider`、`LLMResponse`、`ToolCallRequest`） |
| **OpenAI 兼容实现**：DeepSeek / 百炼 / 硅基流动 / vLLM 都用它 | `providers/openai_compat.py`（`OpenAICompatProvider.chat`） |
| **强制流式 + 分片累积**：流式和非流式共用一套解析逻辑 | `providers/openai_compat.py`（`_StreamAccumulator`、`_merge_tool_call_deltas`、`_finalize_tool_calls`） |
| **思考内容（reasoning_content）保留与回传** | `providers/openai_compat.py`（`_StreamAccumulator` 里的 reasoning 处理） |
| **接口报错兜成"错误响应"而不是抛异常** | `providers/openai_compat.py`（把异常转成 `finish_reason="error"`） |
| **stream_options 被老服务拒绝时自动去掉重试** | `providers/openai_compat.py`（`_reject_unsupported_extra_body`） |
| **extra_body 透传**（思考模式开关等） | `providers/openai_compat.py`（`_build_extra_body`） |

### 2.3 工具系统

| 功能 | 代码位置 |
|---|---|
| **工具抽象基类**：name / description / parameters / execute | `agent/tools/base.py`（`Tool`） |
| **工具注册与分发**：注册、导出 function 定义、按名字执行并兜异常 | `agent/tools/registry.py`（`ToolRegistry.register`/`get_definitions`/`execute`/`list_tools`/`iter_tools`） |
| **读文件**（超 16000 字符截断） | `agent/tools/filesystem.py`（`ReadFileTool`、`MAX_READ_CHARS`） |
| **写文件**（自动建父目录） | `agent/tools/filesystem.py`（`WriteFileTool`） |
| **列目录**（带大小） | `agent/tools/filesystem.py`（`ListDirTool`） |
| **工作区沙箱校验**：路径逃不出 workspace | `agent/tools/filesystem.py`（`_WorkspaceTool`、`_PathEscapeError`） |
| **执行命令**（超时 60 秒、输出截 10000 字符、危险命令拦截） | `agent/tools/shell.py`（`ExecTool`、`COMMAND_TIMEOUT`、`MAX_OUTPUT_CHARS`、`_rule` 里的拦截表） |
| **联网搜索**（博查 API，默认 5 条，超 8000 字符截断） | `agent/tools/web_search.py`（`WebSearchTool`、`API_URL`、`SEARCH_TIMEOUT`） |
| **抓网页正文**（去脚本样式、转文本、限制 2MB / 12000 字符） | `agent/tools/web_fetch.py`（`WebFetchTool`、`_TextExtractor`） |
| **防 SSRF**：拒内网/环回/云元数据地址，逐跳校验重定向 | `agent/tools/web_fetch.py`（`_BLOCKED_NETWORKS`、`_BLOCKED_HOSTNAMES`、`SafeNetworkBackend`） |
| **写长期记忆**（按分类追加到 MEMORY.md，超上限拒写） | `agent/tools/memory.py`（`MemoryTool`、`MAX_MEMORY_CHARS`、`MAX_ITEM_CHARS`） |
| **子智能体**（派临时 agent 干活，只回结论） | `agent/tools/spawn.py`（`SpawnSubagentTool`、`SubagentContext`、`DummySessionManager`） |
| **MCP 工具接入**（连外部进程，把工具包成本项目 Tool） | `agent/tools/mcp.py`（`MCPClientManager`、`MCPTool`） |

### 2.4 渠道（用户从哪跟它说话）

| 功能 | 代码位置 |
|---|---|
| **渠道抽象基类**：`start()` 接收、`send()` 发送、`stop()` 收尾 | `channels/base.py`（`Channel`、`NullChannel`） |
| **终端渠道**：读一行、发出去、等回复、打印 | `channels/cli.py`（`CLIChannel.start`） |
| **终端本地命令**：`/help` `/clear` `/reset` `/tools` `/exit` `/quit` | `channels/cli.py`（`_handle_command`、`HELP_TEXT`） |
| **终端输出分流**：回答走 stdout、提示符与过程信息走 stderr | `channels/cli.py`（`send`、`REPLY_PREFIX`、`_out`/`_err`） |
| **启动横幅在就绪时才打**（避免被渠道日志顶掉提示符） | `channels/cli.py`（`_banner` 属性）+ `main.py`（`_print_banner`） |
| **QQ 群聊消息**（被 @ 才推送，去掉 `<@!号码>` 前缀） | `channels/qq.py`（`QQBotClient.on_group_at_message_create`、`_strip_at`） |
| **QQ 单聊消息**（用 C2CMessage，取 `author.user_openid`） | `channels/qq.py`（`QQBotClient.on_c2c_message_create`） |
| **QQ 发消息**（群 / 单聊是两个接口，靠记下的会话类型选） | `channels/qq.py`（`QQChannel.send`、`_send_group`、`_send_c2c`、`_session_types`） |
| **QQ 连网关**（异步上下文 + `start(appid, secret)`） | `channels/qq.py`（`QQChannel.start`） |

### 2.5 消息总线与网关

| 功能 | 代码位置 |
|---|---|
| **消息数据结构** | `bus/queue.py`（`InboundMessage`、`OutboundMessage`） |
| **两条队列**：进站队列 + 出站队列 | `bus/queue.py`（`MessageBus`、`inbound_queue`、`outbound_queue`） |
| **入站处理**：取消息 → 算会话键 → 找/建 agent → 跑一轮 → 发回复 | `gateway.py`（`Gateway._process_inbound`） |
| **出站分发**：按渠道名找回渠道并发送 | `gateway.py`（`Gateway._dispatch_outbound`） |
| **会话路由**：`渠道:用户` 决定用哪个 agent | `gateway.py`（`Gateway._session_key`、`_get_agent`） |
| **退出规则**：渠道正常退出就整体收工；渠道崩了只摘掉它自己 | `gateway.py`（`Gateway.run`） |
| **并发启动所有渠道 + 两条消费循环** | `gateway.py`（`Gateway.run` 里的 `asyncio.wait`）+ `main.py` |

### 2.6 会话与存储

| 功能 | 代码位置 |
|---|---|
| **会话落盘**：一个会话一个 `.jsonl`，一行一条消息 | `session/manager.py`（`SessionManager.save_message`、`get_history`） |
| **会话文件名转换**：`cli:local` → `cli_local.jsonl` | `session/manager.py`（`_get_session_path`、`_KEY_TO_FILE`） |
| **崩溃残段修复**：丢掉末尾"有 tool_calls 却没结果"的半截历史 | `agent/loop.py`（`_repair_history`） |
| **清空会话**（内存 + 磁盘一起） | `agent/loop.py`（`clear_history`）+ `session/manager.py`（`clear`） |

### 2.7 配置

| 功能 | 代码位置 |
|---|---|
| **读 `config.json`**，格式错误当场报错 | `config.py`（`load_config_file`） |
| **三级优先合并**：环境变量 > .env > config.json > 默认值 | `config.py`（`_pick`、`_pick_int`、`_pick_float`、`_pick_bool`、`_pick_json`） |
| **模型别名**：`models.cheap` 之类短名 → 真实模型 ID | `config.py`（`Config.resolve_model`、`_resolve_models`） |
| **MCP server 解析**（JSON 段 或 `MCP_SERVERS` 环境变量两种写法） | `config.py`（`_resolve_mcp_servers`、`_parse_mcp_env`） |
| **配置错误类型**（只拦这个，不当成程序 bug） | `config.py`（`ConfigError`） |
| **密钥遮蔽**：打印配置时密钥变成 `sk-749…` | `config.py`（`_mask`、`Config.__repr__`） |

### 2.8 装配与启动

| 功能 | 代码位置 |
|---|---|
| **装配所有零件**（注册表 → Provider → 网关 → 渠道 → 注入） | `main.py`（`_assemble_and_serve`） |
| **建工具注册表**（内置 7 个 + 子智能体 + MCP 工具） | `main.py`（`_build_registry`） |
| **按会话造 agent**（会话键 ↔ AgentLoop 绑定的唯一位置） | `main.py`（`_assemble_and_serve` 里的 `factory`） |
| **终端过程显示**（思考灰、工具青、结果绿、错误红） | `main.py`（`TerminalSink`） |
| **MCP 连接与断开**（必须在同一个任务里成对） | `main.py`（`_run` 的 try/finally）+ `agent/tools/mcp.py` |
| **启动横幅生成**（按显示宽度对齐中文） | `main.py`（`_print_banner`、`_display_width`） |
| **日志配置**（压掉 httpx / openai / botpy 的刷屏） | `main.py`（`_setup_logging`） |
| **程序入口与退出码**（0 正常 / 1 配置错 / 130 Ctrl+C） | `main.py`（`main`） |

### 2.9 自带示例

| 功能 | 代码位置 |
|---|---|
| **古诗词查询 MCP Server**：三个工具（搜诗、随机一首、列诗人） | `mcp_servers/poetry_server.py`（`search_poetry`、`random_poetry`、`list_poets`、`POEMS`） |
| **技能示例** | `skills/code-review/SKILL.md`、`skills/weather/SKILL.md` |
---
## 3. 技术栈

### 语言与运行环境

| 项 | 值 | 说明 |
|---|---|---|
| 语言 | Python | 全项目只有 Python，没有别的语言 |
| 开发环境版本 | **3.14.3**（Windows） | 代码里实际用到的下限更低，见下面说明 |
| 并发模型 | `asyncio` 单进程单事件循环 | 渠道、网关、agent 全在同一个循环里跑 |
| 类型注解风格 | `from __future__ import annotations` + `X \| None` | PEP 604 写法，**Python 3.10+ 可用** |
| 包管理 | pip（**没有 requirements.txt / pyproject.toml**） | 依赖要手装，见第 5 节 |

> 关于版本：`README.md` 写的是 3.11+，但代码里没用到 3.11 独有的语法（`Self`、`ExceptionGroup` 等都没用）。
> 保守地板是 **3.10**。不过实际只在 3.14 上验证过，建议直接用 3.11+ 以免踩标准库行为差异。

### 前端

**没有前端**。用户界面只有两处，都在 Python 里：

- 终端界面：`channels/cli.py` + `main.py` 的 `TerminalSink`（用 ANSI 颜色，无第三方 UI 库）；
- QQ 机器人界面：`channels/qq.py`（走 QQ 官方 SDK）。

配置里虽然有 `channels.web`（host / port）字段，但**Web 渠道尚未实现**——那只是预留的配置项。

### 数据库

**没有数据库**（不需要 MySQL / Redis / SQLite）。所有持久化都是纯文件：

| 数据 | 形式 | 位置 | 代码 |
|---|---|---|---|
| 会话历史 | JSONL（一行一条 JSON） | `workspace/sessions/<会话键>.jsonl` | `session/manager.py` |
| 长期记忆 | Markdown | `workspace/memory/MEMORY.md` | `agent/tools/memory.py` |
| 被压缩掉的旧历史 | Markdown | `workspace/memory/HISTORY.md` | `agent/memory.py` |
| 人设 | Markdown | `identity.md`（项目根目录） | `agent/context.py` |
| 技能 | Markdown + YAML frontmatter | `skills/*/SKILL.md` | `agent/skills.py` |
| 配置 | JSON | `config.json`（项目根目录） | `config.py` |

选 JSONL 而不是一个大 JSON，原因写在 `session/manager.py` 里：对话是**追加**出来的，
追加写最坏只丢最后一条，而"读-改-写"一旦被打断会把整个历史截断成半截文件。

### 第三方依赖

| 包 | 用途 | 用在哪 | 是否必需 |
|---|---|---|---|
| `python-dotenv` | 读 `.env` 文件（历史遗留支持） | `config.py` | **必需**（import 在模块顶层） |
| `openai` | 调用所有 OpenAI 兼容接口的模型 | `providers/openai_compat.py` | **必需** |
| `httpx` | HTTP 客户端（联网搜索、抓网页） | `agent/tools/web_search.py`、`agent/tools/web_fetch.py` | **必需**（openai 也依赖它） |
| `PyYAML` | 解析技能文件的 frontmatter | `agent/skills.py` | **必需**（import 在模块顶层） |
| `mcp` | MCP 客户端（连外部工具进程） | `agent/tools/mcp.py` | 可选（不装则 MCP 功能不可用） |
| `qq-botpy` | QQ 官方机器人 SDK | `channels/qq.py` | 可选（不装则 QQ 渠道不可用） |

> ⚠️ **注意 import 的位置**：`openai` 在 `providers/openai_compat.py` 顶层导入，而 `main.py` 会导入它，
> 所以 `openai` 不装的话**整个项目起不来**。而 `mcp` 和 `qq-botpy` 的导入是**放在函数里按需执行**的
> （见 `main.py` 的 `_assemble_and_serve`），所以它们不装也不影响其他功能。

### 外部服务

| 服务 | 用途 | 配置项 |
|---|---|---|
| 任意 OpenAI 兼容模型接口 | 主力模型 | `api_key` / `base_url` / `models.main` |
| 阿里云百炼 | 子智能体默认模型 | `subagent.api_key` / `subagent.base_url` |
| 博查 Web Search | `web_search` 工具 | `tools.bocha_api_key` |
| QQ 开放平台 | QQ 渠道 | `channels.qq.app_id` / `app_secret` |
---
## 4. 目录结构解析

### 4.1 目录树（带白话说明）

```text
DeepsClaw/
│
├── main.py                     【入口 + 总装配】
│                                 程序从这开始。它负责把工具、模型、渠道、网关拼起来。
│                                 想知道"启动时到底干了什么"就看这里。
├── gateway.py                  【网关：消息的交通警察】
│                                 收到用户消息 → 算出是谁发的 → 找到对应的 agent → 把活给它；
│                                 回复则按渠道名找回渠道发出去。
├── config.py                   【配置读取】
│                                 读 config.json / .env / 环境变量，合并成一份配置对象。
├── identity.md                 【人设文件】
│                                 写"agent 是什么性格、守什么规矩"。⚠️ 注意路径问题，见第 6 节。
├── config.json                 【主配置（含密钥，已被 gitignore）】
├── config.example.json         【配置模板（不含密钥，可提交）】
├── README.md                   【给人看的项目介绍】
├── HANDOVER.md                 【就是本文档】
│
├── agent/                      【大脑：思考与决策】
│   ├── loop.py                     主循环。整个项目最核心的文件：
│   │                               调模型 → 模型说要调工具 → 执行 → 把结果给模型 → 再调……
│   │                               还管熔断（防死循环）和会话恢复。
│   ├── context.py                  拼 System Prompt：人设、时间、工作区、记忆、技能。
│   ├── events.py                   定义"agent 会往外说什么"（思考/正文/工具/错误）。
│   │                               终端怎么显示由别人决定，这里只负责产生事件。
│   ├── memory.py                   历史压缩：聊太长了就把中间那段摘要成一句话。
│   ├── skills.py                   技能加载：读 skills/ 下的 SKILL.md，生成摘要。
│   └── tools/                  【工具箱：agent 能动手干的所有事】
│       ├── base.py                 工具基类。写新工具照它来（name/description/execute）。
│       ├── registry.py             工具注册表：登记、导出给模型、按名字执行。
│       ├── filesystem.py           读文件 / 写文件 / 列目录（带工作区沙箱）。
│       ├── shell.py                执行命令（超时、截断、拦危险命令）。
│       ├── web_search.py           联网搜索（博查）。
│       ├── web_fetch.py            抓网页正文（带防内网攻击）。
│       ├── memory.py               写长期记忆（MEMORY.md）。
│       ├── spawn.py                派子智能体干活。
│       └── mcp.py                  连接外部 MCP Server，把人家的工具接进来。
│
├── bus/                        【消息总线：一条进、一条出】
│   └── queue.py                    两个数据类 + 两条 asyncio 队列。
│                                   渠道和 agent 谁都不认识谁，只认这两条队列。
│
├── channels/                   【感官：用户从哪说话】
│   ├── base.py                     渠道基类。加新渠道照它写三个方法即可。
│   ├── cli.py                      终端渠道 + 本地命令（/help /clear /tools /exit）。
│   └── qq.py                       QQ 机器人渠道（群聊 + 单聊）。
│
├── providers/                  【嘴：怎么跟模型说话】
│   ├── base.py                     Provider 抽象接口 + 统一响应结构。
│   └── openai_compat.py            实际实现。所有 OpenAI 兼容接口都走它。
│
├── session/                    【记事本：对话存哪】
│   └── manager.py                  会话的读写（JSONL 文件）。
│
├── mcp_servers/                【自己写的 MCP 工具进程】
│   ├── README.md                   怎么加别人的 MCP Server + 排查表。
│   └── poetry_server.py            示例：古诗词查询，三个工具。
│
├── skills/                     【技能：给模型看的操作手册】
│   ├── code-review/SKILL.md
│   └── weather/SKILL.md
│
└── workspace/                  【运行时数据 + 文件工具的读写范围】
    ├── sessions/                   每个会话一个 .jsonl（对话历史）。
    └── memory/
        ├── MEMORY.md               长期记忆（agent 自己往里写）。
        └── HISTORY.md              被压缩掉的旧对话原文。
```
### 4.2 最该先读的 5 个文件（按顺序）

| 顺序 | 文件 | 读完你会明白 |
|---|---|---|
| 1 | `bus/queue.py` | 消息长什么样、渠道和 agent 之间怎么传（只有 176 行，先读它） |
| 2 | `gateway.py` | 消息怎么从渠道流到 agent、回复怎么流回去 |
| 3 | `agent/loop.py` | agent 一轮到底干了什么（这是最核心也最长的文件） |
| 4 | `main.py` | 所有零件是怎么被拼起来的（跳过 UI 相关函数看装配部分） |
| 5 | `agent/tools/base.py` + `registry.py` | 怎么加一个自己的工具（两个文件加起来不到 250 行） |
---
### 4.3 功能 ↔ 文件 映射总表（改需求先查这张表）

> 用法：先在"我想改什么"这一列找到你的需求，右边就是**必须打开的文件**，不会漏。

| 我想改什么 | 主要文件（括号里是具体位置） |
|---|---|
| **改 agent 怎么思考、怎么循环、最多几步** | `agent/loop.py`（`AgentLoop.run`、`max_steps`） |
| **改 System Prompt 里放什么** | `agent/context.py`（`build_system_prompt`） |
| **改 agent 的性格 / 规矩 / 何时该问用户** | `identity.md`（⚠️ 有加载路径坑，见 6.1） |
| **加 / 改一个工具** | 新建或改 `agent/tools/xxx.py`（照 `base.py` 写）→ 在 `main.py` 的 `_build_registry` 注册 |
| **改工具的超时 / 输出上限** | `agent/tools/shell.py`（`COMMAND_TIMEOUT`、`MAX_OUTPUT_CHARS`）、`agent/tools/filesystem.py`（`MAX_READ_CHARS`）、`agent/tools/web_search.py`（`MAX_OUTPUT_CHARS`）、`agent/tools/web_fetch.py`（`MAX_BYTES`、`MAX_CONTENT_CHARS`） |
| **改命令黑名单 / 危险命令拦截** | `agent/tools/shell.py`（`_rule(...)` 调用那一串） |
| **改联网搜索的关键词、条数、超时** | `agent/tools/web_search.py`（`API_URL`、`DEFAULT_MAX_RESULTS`、`SEARCH_TIMEOUT`） |
| **改抓网页的行为（哪些站不让抓、正文怎么提取）** | `agent/tools/web_fetch.py`（`_BLOCKED_NETWORKS`、`_BLOCKED_HOSTNAMES`、`_TextExtractor`） |
| **改长期记忆的分类、格式、上限** | `agent/tools/memory.py`（`_parse`、`_render`、`MAX_MEMORY_CHARS`）+ `agent/context.py`（`MAX_MEMORY_CHARS`，两处要同步改） |
| **改子智能体的行为（提示词、步数、深度）** | `agent/tools/spawn.py`（`SUBAGENT_IDENTITY`、`SUBAGENT_MAX_STEPS`、`max_depth` 参数） |
| **改历史压缩的阈值 / 保留条数 / 摘要提示词** | `agent/memory.py`（`DEFAULT_TOKEN_BUDGET`、`KEEP_RECENT`、`SUMMARY_PROMPT`） |
| **改支持哪些模型厂商 / 加新 Provider** | `providers/openai_compat.py`（改它）或照 `providers/base.py` 新写一个 |
| **改模型的请求参数（温度、思考开关等）** | `config.json` 的 `extra_body`（透传）→ 代码在 `providers/openai_compat.py`（`_build_extra_body`） |
| **加一个新渠道（飞书 / 微信 / Web…）** | 照 `channels/base.py` 写 `channels/xxx.py` → 在 `main.py` 的 `_assemble_and_serve` 里 `channels.append(...)` |
| **改终端命令（/help、加新命令）** | `channels/cli.py`（`_handle_command`、`HELP_TEXT`、`EXIT_COMMANDS` 等常量） |
| **改终端上回答/过程信息的显示样式** | 回答前缀：`channels/cli.py`（`REPLY_PREFIX`）；思考工具的颜色：`main.py`（`TerminalSink`、`C` 类） |
| **改启动横幅长什么样** | `main.py`（`_print_banner`、`_display_width`） |
| **改会话存哪 / 文件格式** | `session/manager.py`（`SessionManager`、`_get_session_path`） |
| **改会话怎么分组（同一个人在群里共用一个会话还是分开）** | `gateway.py`（`Gateway._session_key`，把 `sender_id` 换成 `chat_id` 即可） |
| **改消息路由规则 / 加消息类型** | `bus/queue.py`（`InboundMessage`、`OutboundMessage`）+ `gateway.py` |
| **改多渠道的启停规则、退出逻辑** | `gateway.py`（`Gateway.run`、`shutdown`） |
| **改配置项 / 加一个新配置** | `config.py`（`Config` 字段 + `from_env` 里加一行）+ `config.example.json` |
| **改模型别名机制** | `config.py`（`Config.resolve_model`、`_resolve_models`）+ `agent/tools/spawn.py` |
| **改 MCP 连接行为（超时、名称前缀、断开顺序）** | `agent/tools/mcp.py`（`DEFAULT_CONNECT_TIMEOUT`、`TOOL_NAME_SEPARATOR`、`shutdown`） |
| **改日志级别 / 压掉哪些第三方日志** | `main.py`（`_setup_logging`、`noisy` 列表）+ `config.json`（`log_level`） |
| **改人设 / 技能的加载规则** | `agent/skills.py`（`SkillsLoader`、`SKILL_FILENAME`） |
| **改入口的行为（退出码、启动提示）** | `main.py`（`main`、`_run`） |

---
## 5. 环境部署与运行

### 5.1 环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | Windows / macOS / Linux 都行。**当前开发机是 Windows 10 + Python 3.14.3**，代码里有几处专门照顾 Windows 的处理（控制台编码、路径分隔符） |
| Python | 建议 3.11+（实际只用 3.14 验证过；理论下限 3.10） |
| 网络 | 需要能访问模型接口；用 `web_search` / `web_fetch` 还需要能出网 |
| 其他 | 用 MCP 的 `npx` 型 server 才需要 Node；纯本地 MCP（Python 写的）不需要 |

### 5.2 装依赖（注意：项目没有 requirements.txt）

```bash
# 必需：不装项目起不来（openai 在模块顶层被导入）
pip install python-dotenv openai httpx PyYAML

# 可选：用 QQ 渠道才需要
pip install qq-botpy

# 可选：要接 MCP Server 才需要
pip install mcp
```
> 交接提醒：**建议顺手补一个 `requirements.txt`**，现在新同事只能照着 README 或本文档装。

### 5.3 配置

在项目根目录建 `config.json`（复制 `config.example.json` 改）：

```json
{
  "api_key": "你的模型密钥",
  "base_url": "https://api.deepseek.com/v1",
  "models": { "main": "deepseek-chat" }
}
```
只写这三行就能跑。其余配置项全都有默认值，需要时再补（字段说明见 `config.example.json` 的注释和 README）。

**配置优先级**（这条必须记住，排查配置问题全靠它）：

```text
环境变量  >  .env  >  config.json  >  代码里的默认值
```
- 是**逐项**合并，不是整份替换：`config.json` 里没写的项会继续从 `.env` 或默认值取；
- `.env` 是**历史遗留**（项目早期用它，现在推荐只用 config.json），但仍然会被读取，且优先级比 json 高；
- 所以"我明明在 config.json 里改了却不生效"十有八九是**环境里有同名变量**或 `.env` 里有旧值。

### 5.4 数据库怎么处理

**没有数据库，不需要初始化，不需要迁移。**

所有数据都是文件，程序第一次运行时会自动创建需要的目录和文件：

```text
workspace/
├── sessions/            每个会话一个 .jsonl（自动创建）
└── memory/
    ├── MEMORY.md        长期记忆（不存在时自动视为空）
    └── HISTORY.md       被压缩掉的旧对话（只在触发压缩后才出现）
```
几个日常操作：

| 想做什么 | 怎么做 |
|---|---|
| 清空某个会话 | 对话里敲 `/clear`，或直接删 `workspace/sessions/<会话键>.jsonl` |
| 重置全部对话 | 删掉 `workspace/sessions/` 目录 |
| 清空长期记忆 | 删掉或清空 `workspace/memory/MEMORY.md` |
| 备份 | 拷走整个 `workspace/` 目录即可 |

### 5.5 启动与运行

```bash
# 交互模式（最常用）
python main.py

# 一次性提问（问完就退）
echo "帮我看看这个项目是干什么的" | python main.py

# 临时换模型做实验（环境变量优先级最高）
LLM_MODEL=deepseek-reasoner python main.py

# 看详细日志（含每次请求的调试信息）
# 改 config.json 的 "log_level": "DEBUG"，或临时用环境变量
```
> **程序不接受命令行参数**。带了参数它只会提示一句然后忽略，不会报错退出——这是刻意的。

**退出码**（脚本化调用时会用到）：

| 码 | 含义 |
|---|---|
| 0 | 正常退出（敲了 `/exit`、或 Ctrl+D 结束输入） |
| 1 | 配置错误（密钥缺失、config.json 不是合法 JSON 等） |
| 130 | 被 Ctrl+C 打断 |

**退出方式的区别**（这一条容易踩）：

- `/exit` 或 `/quit`：**整个程序退出**，包括 QQ 渠道一起停；
- `Ctrl+C`：也是整体退出，但走的是中断路径（会打印「已中断」）。

如果你希望"关掉终端但 QQ 机器人继续在线"，现在的代码**做不到**——渠道退出就是整体收工。

### 5.6 第一次跑起来的验证清单

1. `python main.py` 能看到启动横幅（有模型名、工具数、配置文件路径）；
2. 横幅里「配置」那一行指向你的 `config.json`，不是「未使用 config.json」；
3. 敲 `/tools` 能列出工具（默认 7 个，配了子智能体密钥就是 8 个）；
4. 随便问一句，能看到 `AI >` 开头的回答；
5. 如果配了 MCP，启动时能看到 `[MCP] 已连接 xxx：N 个工具`。

---
## 6. 已知坑点与注意事项

> 这一节是本文档**最该仔细看的部分**。每条都写了：现象 → 原因 → 怎么办。

---

### 6.1 【严重】人设文件 `identity.md` 实际上没有被加载

**现象**：你在 `identity.md` 里写了人设、规矩、"什么时候该派子智能体"，但 agent 的行为完全不符——
它不认识你写的内容。

**原因**：人设文件的路径是**相对 workspace 解析**的，不是相对项目根目录。

```text
代码里的解析逻辑（agent/context.py 的 _load_identity）：
    os.path.join(self.workspace, self.identity_file)

实际取值：
    workspace      = <项目根>/workspace
    identity_file  = "identity.md"
    → 去找 <项目根>/workspace/identity.md   ← 这里没有这个文件！

而文件实际在 <项目根>/identity.md
```
找不到时的兜底是 `agent/context.py` 里的 `DEFAULT_IDENTITY`（一段内置的通用人设），
所以**程序不会报错**，只是静默用了默认人设——这一类"静默降级"最难发现。

**验证方式**（一行代码就能确认）：

```bash
python -c "from agent.context import ContextBuilder; \
           print(ContextBuilder('workspace', identity_file='identity.md').build_system_prompt()[:80])"
# 如果开头是"你是一个务实的编程助手…"= 用的是内置默认人设（不是你的 identity.md）
```
**三种改法**（任选，推荐第一种）：

| 改法 | 怎么做 | 影响 |
|---|---|---|
| ① 改配置（最省事） | `config.json` 里 `"identity_file": "../identity.md"` | 只改配置，代码不动 |
| ② 改代码默认值 | `main.py` 的 `_build_agent` 里把 `identity_file` 的默认值改掉 | 影响所有部署 |
| ③ 把文件挪过去 | 把 `identity.md` 移进 `workspace/` 目录 | ⚠️ workspace 是文件工具的可读写范围，agent 能改自己的"性格" |

> 交接建议：这是个**真 bug**（配置与实现约定不一致），建议修掉并顺手加个日志——
> 人设文件没找到时打一条 WARNING，而不是静默用默认值。

---

### 6.2 `config.json` 里写死了绝对路径（换机器会挂）

**现象**：把项目拷到另一台机器，MCP 的诗词 server 连不上，日志报 `[MCP] [!] poetry 连接失败`。

**原因**：生成的 `config.json` 里 MCP 命令用的是**当前机器的解释器绝对路径**：

```json
"mcp_servers": {
  "poetry": { "command": "C:\\Program Files\\Python314\\python.exe", ... }
}
```
**怎么办**：新机器上改成 `"python"`（前提是它在 PATH 里），或改成那台机器上的真实路径。
路径含空格时**必须加引号**，而且命令是**直接执行、不经过 shell** 的。

---

### 6.3 环境变量会静默盖掉 `config.json`

**现象**：改了 `config.json` 却没生效。

**原因**：优先级是 `环境变量 > .env > config.json > 默认值`，而且是**逐项**合并。
常见来源：

- 系统里残留的 `LLM_MODEL` / `LLM_API_KEY` 等变量；
- 项目根目录还有一个历史 `.env` 文件（本项目已经从 `.env` 迁到 `config.json`，但 `.env` 仍会被读取）；
- 你在同一个 shell 里 `set` 过、忘了清。

**怎么办**：先看启动横幅的「配置」那一行（它显示本次真正读到的是哪个文件），
再检查环境：

```bash
# Windows PowerShell
Get-ChildItem Env: | Where-Object Name -match "LLM_|AGENT_|QQ_|SUBAGENT_|MCP_|BOCHA_"
# Linux / macOS
env | grep -E "LLM_|AGENT_|QQ_|SUBAGENT_|MCP_|BOCHA_"
```
---

### 6.4 MCP：连接与断开必须在同一个任务里

**现象**：改代码后出现 `Attempted to exit cancel scope in a different task than it was entered in`，
或者程序退出时卡住不退。

**原因**：MCP 的 `ClientSession` 是**手动 `__aenter__`** 进去的（不能用 `with` 语法糖，否则初始化就死等），
而 anyio 的 cancel scope **按任务记账**——手动进入的上下文，必须在**同一个 asyncio 任务**里退出。

**怎么办**：如果你要动 MCP 的生命周期，记住这两条（`main.py` 的 `_run` 里有正确示范）：

```python
# 正确：connect_all 和 shutdown 在同一个协程里成对出现
mcp_manager = MCPClientManager(cfg.mcp_servers)
await mcp_manager.connect_all()
try:
    return await _assemble_and_serve(cfg, mcp_manager)
finally:
    await mcp_manager.shutdown()   # ← 必须在这里，不能挪进别的任务
```

外加一条：**服务器之间要按连接的逆序断开**（`agent/tools/mcp.py` 的 `shutdown` 里用的 `reversed`）。
顺序错了的表现很吓人：**一个 server 完全正常，配上第二个就必炸**。

---

### 6.5 启动横幅在重定向时只有 80 列宽

**现象**：把输出重定向到文件时，横幅里的工具清单被截断成 `…、spawn_subagent`。

**原因**：横幅宽度来自 `shutil.get_terminal_size()`，重定向时拿不到真实终端宽度，会退回 80 列。
这是**已知行为，不是 bug**——宁可窄一点，也不要为了猜宽度把边框画歪。
在真实终端里宽度是准的。

---

### 6.6 `/exit` 会连带停掉所有渠道

**现象**：在终端敲 `/exit`，正在挂着的 QQ 机器人也下线了。

**原因**：网关的退出规则是「**任一渠道正常退出 → 整体收工**」（`gateway.py` 的 `Gateway.run`）。
这条规则本身是合理的（没有渠道可服务了就该停），但如果你想让机器人长期挂机，就别敲 `/exit`。

**注意区别**：渠道**崩了**（抛异常）只摘掉它自己，其他渠道继续跑——这两条规则不一样，别搞混。

---

### 6.7 会话是按"发送者"分的，不是按"会话/群"

**现象**：同一个人在 A 群和 B 群里跟机器人说话，上下文是**同一个会话**（历史互相串）。

**原因**：`gateway.py` 的 `_session_key` 用的是 `sender_id`：

```python
return f"{msg.channel}:{msg.sender_id}"   # 例如 qq:用户openid
```
**怎么办**：想改成"某人在某群"各算一个会话，把 `sender_id` 换成 `chat_id` 即可（一行）。
README 里也记了这一条。

---

### 6.8 QQ 群聊必须 @ 机器人才会收到消息

**现象**：群里别人说话，机器人没反应。

**原因**：这是 **QQ 平台自己的规则**，不是代码问题。群消息只在被 @ 时才会推给机器人。
代码在 `channels/qq.py` 的 `on_group_at_message_create` 里，并且会自动去掉消息里的 `<@!号码>` 前缀。

---

### 6.9 配置里有 feishu / web，但功能没实现

`config.py` 已经能解析 `channels.feishu` 与 `channels.web` 两个配置段，但：

- `channels/feishu.py` **不存在**；
- `channels/web.py` **不存在**；
- 配了这两个段**不会有任何效果**（不会报错，也不会启用）。

它们是"配置先行、实现待补"的状态。要接飞书的话，照 `channels/base.py` 写一个新文件，
然后在 `main.py` 的 `_assemble_and_serve` 里加进 `channels` 列表即可。

---
### 6.10 代码里的死代码与过时注释（别被误导）

接手时看到这些，不用怀疑自己读错了：

| 位置 | 情况 | 怎么处理 |
|---|---|---|
| `main.py` 的 `_setup_logging_done()` | **完全没被调用的空函数**（历史遗留占位） | 可以直接删 |
| `main.py` 的 `_print_banner` 文档里提到"见 `_print_banner` 的说明" | 有个别注释里的交叉引用已经过时 | 顺手修 |
| `config.py` 模块文档与 `_pick` 的说明 | 大段在讲 `.env`（历史遗留），现在主配置是 `config.json` | 阅读时以代码为准 |
| `workspace\workspace\sessions\` | 会话文件**嵌了两层 workspace** 目录（`_build_session_manager` 把 `"workspace/sessions"` 拼在了 `cfg.workspace` 后面） | 功能正常，但路径反直觉；要改就改 `main.py` 的 `_build_session_manager` |
| `README.md` 里写的 "8 个工具" | 不精确：内置是 **7 个**（`read_file` / `write_file` / `list_dir` / `exec` / `web_search` / `web_fetch` / `save_memory`），第 8 个 `spawn_subagent` 需要配密钥才注册，MCP 工具数量另算 | 以 `/tools` 的实际输出为准 |
| `identity.md` 里的"用了 `Self` 等新语法"之类说法 | 项目里并没有用到 `Self` | 已在本次交接中更正 |

---

### 6.11 【严重】没有任何自动化测试

全项目**没有一个测试文件**（`agent/tools/web_fetch.py` 里倒是留了"便于注入 MockTransport 测试"的钩子，
但没人写测试）。根目录那个 `.pytest_cache/` 是历史遗留，不是测试覆盖度的证据。

**这意味着**：你改任何代码，都得**手工验证**。建议的验证清单：

```bash
# 1. 语法与导入
python -m compileall -q .
python -c "import main; print('OK')"

# 2. 配置是否读对（看横幅的"配置"行 + 工具数）
echo "/tools" | python main.py

# 3. 真跑一轮对话（必须有模型密钥）
echo "1+1等于几" | python main.py
```
**建议**：接手后优先补三类测试——配置合并优先级、工具沙箱（路径逃逸）、会话读写往返。
这三处一旦回归，症状都很隐蔽。

---

### 6.12 其他小坑（不影响跑，但会绊你一下）

| 坑 | 说明 |
|---|---|
| **`exec` 的超时是 60 秒** | `agent/tools/shell.py` 的 `COMMAND_TIMEOUT`。装依赖、跑大编译很容易超。 |
| **`exec` 的 stdin 接的是空设备** | 需要交互输入的命令（`npm init`、`git commit` 不带 `-m`）会**直接结束或报错**，不是卡住。 |
| **读文件上限 16000 字符** | `agent/tools/filesystem.py` 的 `MAX_READ_CHARS`，超出截断并在结尾标注。 |
| **抓网页上限 2MB / 12000 字符** | `agent/tools/web_fetch.py` 的 `MAX_BYTES` / `MAX_CONTENT_CHARS`。 |
| **`web_fetch` 不走系统代理** | 代码里是 `trust_env=False`（`_build_transport`）。内网需要代理才能出网的环境里，这个工具会失败。 |
| **会话文件会一直变大** | 没有自动清理。聊很久的会话 `.jsonl` 可能到几 MB；`/clear` 或直接删文件即可。 |
| **会话键里的 `:` 会变成 `_`** | `cli:local` → `cli_local.jsonl`。这个替换**不可逆**（`cli:local` 和 `cli_local` 会落到同一个文件），代码注释里写了这个已知取舍。 |
| **MCP 子进程可能变孤儿** | 主进程被强杀（`taskkill /F`）时，MCP 子进程不会自动退出，要手工清。正常退出（含 Ctrl+C）不会。 |
| **默认日志级别** | 不配 `log_level` 时默认是 **DEBUG**（`config.py` 的 `_get_log_level`）。线上部署建议显式设成 `INFO`。 |
| **群聊成员标识不通用** | QQ 单聊用 `user_openid`，群里用 `member_openid`，两者不是一套 ID，同一个人的两个身份无法自动关联。 |
| **`workspace` 是文件工具的读写范围** | 不只是"数据目录"。你把 `workspace` 指到某个代码仓库，agent 就能改那个仓库的文件。 |
---

## 7. 后续开发的建议顺序

如果让你接手后从头理一遍，建议按这个顺序：

1. **先修 6.1（人设没加载）**——一行配置就能修，但影响所有行为；
2. **补 `requirements.txt`**——现在装依赖全靠文档；
3. **给关键路径补测试**——优先配置合并、沙箱、会话读写（见 6.11）；
4. **清死代码**——`_setup_logging_done()`、过时注释、`README` 里不准的数字（见 6.10）；
5. **想清楚要不要修 `workspace/workspace` 的双层路径**——现在能用，但反直觉；
6. **再接新渠道**（飞书 / Web）——渠道是插件，照着 `channels/base.py` 和 `channels/qq.py` 写即可。

---

## 8. 附录：常见操作速查

| 我要… | 命令 / 位置 |
|---|---|
| 启动对话 | `python main.py` |
| 一次性提问 | `echo "问题" \| python main.py` |
| 看有哪些工具 | 对话里敲 `/tools` |
| 看本地命令帮助 | 对话里敲 `/help` |
| 清空当前会话 | 对话里敲 `/clear` |
| 退出 | `/exit` 或 Ctrl+C |
| 看配置从哪来 | 启动横幅的「配置」那一行 |
| 换模型 | 改 `config.json` 的 `models.main` |
| 加一个模型别名（给子智能体用） | `config.json` 的 `models` 里加键值，如 `"cheap": "qwen-turbo"` |
| 加一个 MCP Server | `config.json` 的 `mcp_servers` 里加一项（详见 `mcp_servers/README.md`） |
| 加一个工具 | 照 `agent/tools/base.py` 写类 → `main.py` 的 `_build_registry` 里注册 |
| 加一个渠道 | 照 `channels/base.py` 写类 → `main.py` 的 `_assemble_and_serve` 里 append |
| 看 AI 说了什么（纯净输出） | `python main.py > answer.txt`（stderr 是过程信息） |
| 看思考过程 / 工具调用 | `python main.py 2> think.txt`（它们都走 stderr） |
| 找会话文件 | `workspace/workspace/sessions/*.jsonl` |
| 找长期记忆 | `workspace/memory/MEMORY.md` |
| 看被压缩掉的旧对话 | `workspace/memory/HISTORY.md` |

---

## 9. 写在最后

这个项目的代码组织是**分层清晰**的：渠道 → 总线 → 网关 → agent → 工具 / Provider。
想改哪一层，改哪一层就行，其他层基本不受影响。

每个模块顶部的文档字符串都写了"为什么这么设计"，**遇到看不懂的地方先读那段**，
大部分坑前人都踩过并记在里面了（比如 MCP 为什么要手动 `__aenter__`、为什么断开要逆序）。

本文档里标注【严重】的三条（6.1 人设未加载、6.4 MCP 任务边界、6.11 没有测试），建议接手第一周就处理掉。
