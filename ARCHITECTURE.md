# DeepsClaw 项目分析报告（代码结构 · 架构 · 流程 · 技术栈 · 坑点）

> 生成方式：逐文件通读全部源码（35 个提交、9 个 Python 包）+ 本机实测验证（编译、配置加载、工具装配、横幅渲染、MCP 握手、依赖版本）。
> 配套图表（三份，用 draw.io / VSCode Draw.io 插件打开）：`DeepsClaw-框架图.drawio`、`DeepsClaw-业务流程图.drawio`、`DeepsClaw-技术栈.drawio`。
> 实测环境：Windows 11 + Python 3.14.3 + openai 3.16.2 + mcp 1.29.0 + qq-botpy 1.2.1。

---

## 0. 一句话结论

**DeepsClaw 是一个「全零件自制」的多渠道 AI Agent 框架**：不依赖 LangChain / LlamaIndex 等任何 Agent 框架，自己实现了 Agent 主循环、工具系统、消息总线、网关路由、渠道适配、Provider 抽象、会话持久化、上下文压缩、子智能体、MCP 客户端。

它真正解决的问题是：**把「会聊天的模型」变成「能在多个渠道长期挂着、能动手改文件跑命令、能加载外部工具」的服务。**

定位是**参考实现**：每一层刻意拆开，每个取舍都写在模块文档字符串里 —— 所以它的价值不只在「能用」，更在「能改」（换掉其中一层不必动其他层）。

| 维度 | 结论 |
|---|---|
| 规模 | 约 6500 行 Python（9 个包 / 35 个 .py 文件），无前端、无数据库 |
| 架构 | 5 层单向依赖：渠道 → 总线 → 网关 → Agent → 工具/Provider |
| 装配 | 唯一装配点 `main.py`；唯一路由点 `gateway.py`；唯一循环点 `agent/loop.py` |
| 安全 | 文件沙箱 + 9 类命令拦截 + 四道防 SSRF 闸门 |
| 测试 | **0 个测试文件**（`.pytest_cache/` 是历史遗留） |
| 状态 | 可用；飞书/Web 渠道仅配置项；技能目录因路径 bug 实际未加载 |

---

## 1. 配套图表说明（三份 drawio 文件）

### 1.1 `DeepsClaw-框架图.drawio`（2 页）

| 页签 | 内容 |
|---|---|
| `01-系统架构` | 8 层架构图：接入层 / 总线 / 网关 / Agent 大脑 / 模型接入 / 安全机制 / 存储 / 外部服务，含依赖方向与装配要点 |
| `05-技术栈` | 核心技术栈六大块：语言运行时 / 第三方依赖 / 核心机制 / 安全机制 / 持久化 / 外部服务 |

### 1.2 `DeepsClaw-业务流程图.drawio`（4 页）

| 页签 | 内容 |
|---|---|
| `02-消息时序` | 六泳道时序图：用户 → 渠道 → 总线 → 网关 → Agent 循环 → 模型/工具 → 回复回渠道 |
| `03a-工具循环与压缩` | ① 工具调用循环与死循环熔断（按轮计指纹、10 警告 / 20 熔断）；② 历史压缩与尾部吸附 |
| `03b-MCP与子智能体` | ③ MCP 连接/断开生命周期（手动 __aenter__、逆序断开、同任务约束）；④ 子智能体派生与上下文隔离 |
| `04-启动退出` | 启动装配阶段 + 运行/退出阶段（谁掌握子任务谁判断收工、退出码语义） |

### 1.3 `DeepsClaw-技术栈.drawio`（1 页）

| 页签 | 内容 |
|---|---|
| `05-技术栈` | 核心技术栈六大块（与框架图第 2 页内容一致，单独成文件便于分享） |

---

## 2. 技术栈（实测确认）

### 2.1 语言与运行时

| 项 | 值 | 证据 / 说明 |
|---|---|---|
| 语言 | Python（唯一语言） | 35 个 `.py` 文件，无其他语言 |
| 实际版本 | **3.14.3** | 实测 `C:/Program Files/Python314/python.exe` |
| 语法下限 | **3.10+** | 全项目 `from __future__ import annotations` + PEP 604 的 `X | None` |
| 并发模型 | **asyncio 单进程单事件循环** | 渠道、网关、Agent、工具全在同一 loop |
| 类型风格 | `@dataclass` + `ABC` 抽象基类 | 无 pydantic / attrs |
| 包管理 | pip，**无 requirements.txt / pyproject.toml** | 依赖必须手装 |
| 代码检查 | 无 lint 配置，**无任何测试** | `.pytest_cache/` 为历史遗留 |

### 2.2 第三方依赖（6 个，实测全部已安装）

| 包 | 用途 | 用在哪 | 必需性 |
|---|---|---|---|
| `openai` 3.16.2 | 所有模型调用 | `providers/openai_compat.py`（**顶层导入**） | **硬必需**，不装整个项目起不来 |
| `httpx` | 联网搜索、抓网页 | `web_search.py` / `web_fetch.py` | **硬必需**（openai 也依赖） |
| `python-dotenv` | 读 `.env`（遗留兼容） | `config.py`（**顶层导入**） | **硬必需** |
| `PyYAML` | 解析技能 frontmatter | `agent/skills.py`（**顶层导入**） | **硬必需** |
| `mcp` | MCP 客户端 | `agent/tools/mcp.py` | 可选（函数内延迟导入） |
| `qq-botpy` | QQ 官方机器人 SDK | `channels/qq.py` | 可选（函数内延迟导入） |

> **刻意的依赖分层**：顶层导入不装即启动失败；延迟导入不装不影响 CLI 场景。
> `main.py::_assemble_and_serve` 只在配了 QQ 凭据时才 `from channels.qq import QQChannel`。

### 2.3 外部服务

| 服务 | 用途 | 配置项 | 当前状态 |
|---|---|---|---|
| DeepSeek（OpenAI 兼容） | 主力模型 | `api_key` / `base_url` / `models.main` | 已配，模型 `deepseek-flash` |
| 阿里云百炼 | 子智能体模型 | `subagent.api_key` / `subagent.base_url` | 已配，模型 `qwen3.8-flash` |
| 博查 Web Search | `web_search` 工具 | `tools.bocha_api_key` | 已配 |
| QQ 开放平台 | QQ 渠道 | `channels.qq.app_id` / `app_secret` | 已配，appid=1905751786 |
| MCP Server | 外部工具进程 | `mcp_servers` | 已配 poetry（实测握手成功，3 个工具） |

### 2.4 存储（无数据库）

| 数据 | 形式 | 实际路径（实测） |
|---|---|---|
| 会话历史 | JSONL 一行一条 | `workspace/workspace/sessions/<key>.jsonl` |
| 长期记忆 | Markdown | `workspace/memory/MEMORY.md`（尚未生成） |
| 压缩归档 | Markdown 追加 | `workspace/workspace/memory/HISTORY.md` |
| 人设 | Markdown | `identity.md`（项目根） |
| 技能 | Markdown + YAML frontmatter | `skills/*/SKILL.md` |
| 配置 | JSON | `config.json`（项目根，已 gitignore，实测未被 git 跟踪） |

### 2.5 前端

**没有前端**。用户界面只有两处，都在 Python 内：
- 终端界面：`channels/cli.py` + `main.py::TerminalSink`（纯 ANSI 转义，无第三方 UI 库）；
- QQ 机器人：`channels/qq.py`（官方 SDK）。
- `channels.web` / `channels.feishu` 配置段存在，但**适配器未实现**（配置先行、实现待补）。

---

## 3. 代码结构

### 3.1 目录树与职责（含实测行数）

```text
DeepsClaw/
├── main.py                737 行  【唯一装配点】入口 + 总装 + 终端呈现(TerminalSink) + 启动横幅
├── gateway.py             366 行  【路由层】按 session_key 分消息，按 channel 分回复
├── config.py              646 行  【配置】config.json / .env / 环境变量 三级逐项合并
├── identity.md             87 行  【人设】写进 System Prompt，决定 agent 性格与纪律
├── config.example.json           【模板】不含密钥，可提交
├── config.json                   【主配置】含真实密钥，已 gitignore（实测未被跟踪）
├── README.md / HANDOVER.md       【文档】README 面向上手，HANDOVER 面向接手
│
├── agent/                  【大脑：思考与决策】
│   ├── loop.py            577 行  AgentLoop：调模型→执行工具→再调模型；熔断；历史修复
│   ├── context.py         205 行  ContextBuilder：拼 System Prompt 与 messages
│   ├── events.py          218 行  6 类输出事件 + OutputSink 抽象（前端接口契约）
│   ├── memory.py          282 行  MemoryConsolidator：历史压缩 + 归档 HISTORY.md
│   ├── skills.py          237 行  SkillsLoader：扫 skills/*/SKILL.md，生成摘要/按需取正文
│   └── tools/            【工具箱：agent 能动手干的所有事】
│       ├── base.py        131 行  Tool 抽象基类：name/description/parameters/execute
│       ├── registry.py    112 行  ToolRegistry：注册、导出定义、按名分发
│       ├── filesystem.py  293 行  read_file / write_file / list_dir（沙箱校验）
│       ├── shell.py       333 行  exec（超时/截断/9 类危险命令拦截）
│       ├── web_search.py  315 行  web_search（博查 API）
│       ├── web_fetch.py   668 行  web_fetch（HTML→文本，防 SSRF）
│       ├── memory.py      289 行  save_memory（长期记忆 upsert/remove）
│       ├── spawn.py       290 行  spawn_subagent（临时子智能体）
│       └── mcp.py         418 行  MCP 客户端：把外部进程的工具接进来
│
├── bus/queue.py           176 行  【神经】InboundMessage/OutboundMessage + 两条 asyncio 队列
│
├── channels/               【感官：用户从哪说话】
│   ├── base.py            132 行  Channel 抽象基类（start/send/stop）+ NullChannel
│   ├── cli.py             283 行  终端渠道 + 本地命令
│   └── qq.py              280 行  QQ 官方机器人（群聊 + 单聊）
│
├── providers/              【嘴：怎么跟模型说话】
│   ├── base.py            153 行  LLMProvider 抽象 + LLMResponse/ToolCallRequest/StreamDelta
│   └── openai_compat.py   466 行  OpenAI 兼容实现（强制流式 + 分片累积）
│
├── session/manager.py     194 行  【记事本】JSONL 会话读写
│
├── mcp_servers/            【自己写的 MCP Server】
│   ├── poetry_server.py   202 行  示例：古诗词查询（3 个工具，实测握手成功）
│   └── README.md                 接入说明与排查表
│
├── skills/                 【技能：给模型看的操作手册】
│   ├── code-review/SKILL.md      （实测：★ 当前不会被加载，见 §7.1-B2）
│   └── weather/SKILL.md
│
└── workspace/              【运行时数据 + 文件工具读写范围】
    ├── sessions/                  ← 空（实际未使用）
    ├── memory/                    ← 空（MEMORY.md 尚未生成）
    └── workspace/sessions/        ← 真实落盘位置（双层 workspace）
        └── qq_BE73512C92CE06213E2BF8033B5EC41A.jsonl
```

### 3.2 分层依赖（只允许向下）

```text
        ┌──────────────────────────────────────────────────┐
        │  channels/ (cli, qq)      providers/ (openai)     │  ← 最外层：平台/厂商耦合
        └───────────┬───────────────────────┬──────────────┘
                    │ 只认 MessageBus        │
        ┌───────────▼───────────────────────┴──────────────┐
        │  bus/queue.py   ← 唯一的层间契约（值对象 + 队列）   │
        └───────────┬──────────────────────────────────────┘
                    │
        ┌───────────▼──────────────────────────────────────┐
        │  gateway.py   ← 唯一的路由者（session_key → agent）│
        └───────────┬──────────────────────────────────────┘
                    │
        ┌───────────▼──────────────────────────────────────┐
        │  agent/loop.py → context / memory / skills        │  ← 核心大脑
        │       └─▶ agent/tools/*  →  session/manager.py    │
        └──────────────────────────────────────────────────┘
                    ▲
        ┌───────────┴──────────────────────────────────────┐
        │  main.py  ← 唯一知道「谁接谁」的地方（装配者）        │
        └──────────────────────────────────────────────────┘
```

**关键推论**：`channels` 不 import `agent`，`agent` 不 import `channels`，两者只通过 `bus.queue` 通信 →
**加渠道不碰 agent、换模型不碰渠道、加工具不碰任何一层**。

### 3.3 核心符号表（改需求先查这张表）

| 我想改什么 | 主要文件（括号里是具体位置） |
|---|---|
| agent 怎么循环、最多几步 | `agent/loop.py`（`AgentLoop.run`、`max_steps`） |
| System Prompt 里放什么 | `agent/context.py`（`build_system_prompt`、`build_messages`） |
| agent 的性格 / 规矩 | `identity.md`（⚠ 路径规则见 §5.2） |
| 加 / 改一个工具 | 新建 `agent/tools/xxx.py`（照 `base.py` 写）→ `main.py::_build_registry` 注册 |
| 工具的超时 / 输出上限 | `shell.py`、`filesystem.py`、`web_search.py`、`web_fetch.py` 各自的常量 |
| 命令黑名单 | `agent/tools/shell.py`（`ExecTool.deny_patterns`） |
| 长期记忆的分类 / 上限 | `agent/tools/memory.py` + `agent/context.py`（**两处 MAX_MEMORY_CHARS 要同步**） |
| 子智能体行为 | `agent/tools/spawn.py`（`SUBAGENT_IDENTITY`、`SUBAGENT_MAX_STEPS`、`max_depth`） |
| 历史压缩阈值 / 摘要提示词 | `agent/memory.py`（`DEFAULT_TOKEN_BUDGET`、`KEEP_RECENT`、`SUMMARY_PROMPT`） |
| 支持哪些模型厂商 | 改 `providers/openai_compat.py` 或照 `providers/base.py` 新写 |
| 模型请求参数（思考开关等） | `config.json` 的 `extra_body` → `openai_compat.py::_build_extra_body` |
| 加一个新渠道 | 照 `channels/base.py` 写类 → `main.py::_assemble_and_serve` 的 channels 列表加一行 |
| 会话怎么存 / 存在哪 | `session/manager.py` + `main.py::_build_session_manager` |
| 消息路由规则（谁算一个会话） | `gateway.py::_session_key` |

---

## 4. 框架图（文字版，同 `DeepsClaw-框架图.drawio` 第 1 页）

### 4.1 八层架构

```text
① 用户接入层  channels/
   CLIChannel(cli.py)   QQChannel(qq.py)   FeishuChannel[未实现]   WebChannel[未实现]
   Channel 抽象基类(base.py): name / start / send / stop
   ＋ 启动横幅与本地命令 /help /clear /reset /tools /exit（由 main.py 注入）
        │ publish_inbound                 ▲ send()
        ▼                                 │
② 消息总线层  bus/queue.py（唯一层间契约）
   inbound_queue:  Queue[InboundMessage]     outbound_queue: Queue[OutboundMessage]
   InboundMessage  = channel / sender_id / chat_id / content / raw
   OutboundMessage = channel / chat_id / content / reply_to
        │                                 ▲
        ▼                                 │
③ 网关层  gateway.py（只搬运不干活）
   _process_inbound：session_key = 渠道:发送者 → 查 _agents 缓存 → agent.run()
   _dispatch_outbound：按 channel 名反查渠道 → channel.send()
   _agents / _channel_map；退出规则见 §5.4
        │                                 ▲
        ▼                                 │
④ Agent 大脑层  agent/
   AgentLoop(loop.py) ── 调模型 → 执行工具 → 再调模型（最多 50 步）+ 熔断 + 历史修复
   ContextBuilder(context.py)   人设+时间+工作区+长期记忆+技能摘要 → [system]+history+[user]
   MemoryConsolidator(memory.py) 超 token 预算折叠旧历史，归档 HISTORY.md
   Events/OutputSink(events.py)  6 类事件 + 5 条顺序契约（接前端的唯一入口）
   SkillsLoader(skills.py)       SKILL.md 目录 + 按需加载
   SpawnSubagentTool(spawn.py)   临时子智能体（深度 2）
   MCPClientManager(tools/mcp.py) Server__Tool 包装与转发
        │ registry.execute(name, args)
        ▼
   内置工具集（8 个 + MCP N 个）
   read_file / write_file / list_dir / exec / web_search / web_fetch / save_memory
   spawn_subagent（配密钥才注册）  server__tool（MCP 外部工具）
        │
        ▼
⑤ 模型接入层  providers/
   LLMProvider 抽象(base.py) → OpenAICompatProvider(openai_compat.py)
   强制流式 + _StreamAccumulator 分片累积；失败归一为 finish_reason=error（不抛异常）

⑥ 安全机制（横切）  文件沙箱 / 9 类命令拦截 / 防 SSRF 四道闸 / 记忆路径固定
⑦ 会话与存储层     SessionManager(JSONL) · MEMORY.md · HISTORY.md · config.json · identity.md · skills/
⑧ 外部服务         DeepSeek · 阿里云百炼 · 博查 · QQ 开放平台 · MCP Server（子进程 stdio）
```

### 4.2 运行时对象图（实测装配结果）

```text
main.main()
 ├─ _setup_console()                     Windows 管道切 UTF-8
 ├─ Config.from_env()                    config.json + .env + os.environ
 ├─ _setup_logging(level)                httpx / httpx2 / openai / botpy 降噪
 └─ asyncio.Runner().run(_run(cfg))      ← 连接池绑在事件循环上
     ├─ MCPClientManager(cfg.mcp_servers).connect_all()   30s/个，失败只跳过
     ├─ try: _assemble_and_serve(cfg, mcp)</*
     │    ├─ _build_registry(cfg, mcp.get_tools())   【整进程唯一】
     │    │    read_file · write_file · list_dir · exec · web_search · web_fetch · save_memory
     │    │    + poetry__*（MCP）  + spawn_subagent（配密钥才注册）
     │    ├─ OpenAICompatProvider(主模型)            【整进程共用，连接池共享】
     │    ├─ SessionManager(workspace/workspace/sessions)
     │    ├─ MessageBus()
     │    ├─ channels = [CLIChannel(bus)]  (+ QQChannel(bus) 凭据齐全才加)
     │    ├─ Gateway(bus, channels, factory)
     │    └─ 注入：channel.tool_names / channel._banner / channel._clear_callback
     └─ finally: mcp_manager.shutdown()     ★ 必须与 connect_all 同一 asyncio 任务
```

### 4.3 Gateway.run() 并发结构

```text
Gateway.run()
 ├─ create_task(channel.start())     ×N        ┐
 ├─ create_task(_process_inbound)               ├ gather 并发
 ├─ create_task(_dispatch_outbound)             ┘
 └─ while alive:                                ← 只等渠道任务
       done = await asyncio.wait(alive, FIRST_COMPLETED)
       异常退出 → 记 error，摘掉这一条，其余继续跑
       正常返回 → return 它的退出码（整体收工）
       全部异常退出 → return 1
    finally:
      _cancel_all(tasks)   cancel + wait(timeout=5s)，等不到就放弃（绝不卡住退出）
      shutdown()           逆序 stop 各渠道；清空 _agents（历史已在磁盘）
```

---

## 5. 业务工作流程图

### 5.1 一条消息的完整生命周期（同 `DeepsClaw-业务流程图.drawio` 第 1 页）

```text
用户输入 "帮我看看这个目录"
   │
   ▼
① CLIChannel.start()
   await asyncio.to_thread(input, "你 > ")     # 阻塞调用丢线程，不钉死事件循环
   strip → 非空 → 不以 "/" 开头
   _response_event.clear()
   bus.publish_inbound(InboundMessage(channel=cli, sender_id=local, chat_id=direct, content=...))
   await _response_event.wait()               # ★ 接力式：等回复打完才读下一行
   │
   ▼  inbound_queue
② Gateway._process_inbound()
   session_key = "cli:local"                  # f"{msg.channel}:{msg.sender_id}"
   agent = _agents.get(key) or agent_factory(key)   # 缓存命中即复用（保留上下文）
   reply = await agent.run(msg.content)
       │
       │  ┌────────────────── AgentLoop.run(user_input) ──────────────────┐
       │  │ _tool_window.clear(); _step = 0                               │
       │  │ messages = context.build_messages(history, user_input)        │
       │  │ _remember(turn, {user})  → turn.append + 立刻落盘 JSONL         │
       │  │                                                               │
       │  │ for step in range(max_steps=50):                              │
       │  │   response = await provider.chat(messages, tools, on_delta)   │
       │  │      └ 强制流式；_StreamAccumulator 边收边攒                     │
       │  │        content / reasoning / tool_calls(按 index 归并)          │
       │  │        on_delta → ThinkingDelta / ContentDelta 事件            │
       │  │   if finish_reason == "error":  → 不写历史，直接返回错误文本     │
       │  │   assistant_msg = _build_assistant_message(response)          │
       │  │      （tool_calls.arguments 再序列化成 JSON 字符串；             │
       │  │        reasoning_content 放消息顶层，DeepSeek 要求回传）          │
       │  │   if not has_tool_calls: _save_to_history(turn); return content│
       │  │   _run_tool_calls(...)                                        │
       │  │      ├ _check_tool_loop：整轮指纹 → OK / WARN(10) / BREAK(20)   │
       │  │      ├ BREAK：逐个补占位 tool 结果（否则下次请求 400）→ 熔断说明  │
       │  │      ├ WARN：首个结果后追加 [系统提示] 提醒换路                  │
       │  │      └ OK：emit ToolCallEvent → registry.execute →             │
       │  │             emit ToolResultEvent（预览截断 2000 字）→            │
       │  │             tool 消息写回 messages + 落盘                       │
       │  │   _step += 1                                                  │
       │  │ else: 步数耗尽 → 追加 assistant 说明，reason=max_steps          │
       │  │ finally: emit TurnEndEvent(reply, reason)  ← 恰好一次，必为最后  │
       │  └───────────────────────────────────────────────────────────────┘
       │
   except Exception → reply = "抱歉，处理这条消息时出错了…（{kind}）"
   bus.publish_outbound(OutboundMessage(channel=cli, chat_id=direct, content=reply))
   │
   ▼  outbound_queue
③ Gateway._dispatch_outbound()
   channel = _channel_map["cli"]              # 按渠道名反向查找
   await channel.send(msg)
   │
   ▼
④ CLIChannel.send()
   print("AI > " + content, file=stdout)      # 只有回答进 stdout
   _response_event.set()                      # ★ set 在打印之后
   │
   ▼  用户看到回答 → start() 从 wait 醒来 → 回到 ①
```

### 5.2 启动 / 运行 / 退出（同 `DeepsClaw-业务流程图.drawio` 第 4 页）

```text
启动：main() → Config.from_env() → asyncio.Runner
        └─ MCP connect_all → try: 装配并服务 → finally: MCP shutdown
             └─ Gateway.run()
                  ├─ 渠道异常 → 只摘掉它自己（局部故障不放大成全局故障）
                  ├─ 渠道正常返回 → 整体收工（return 它的退出码）
                  └─ 全部渠道异常 → return 1
退出码：0 正常（/exit、/quit、Ctrl+D） / 1 配置错或全渠道崩 / 130 Ctrl+C
```

**关键设计**：谁掌握子任务，谁负责判断「该收工了」—— 退出判断在 `Gateway.run()` 内部，不在 `main`。
（早期在 `_serve` 用 `asyncio.wait` 等「网关任务先完成」是错的：两条消费循环是 `while True`，永不完成，等的人永远等不到 → 敲了 `/exit` 却卡死。）

### 5.3 会话恢复与历史修复

```text
Gateway._get_agent(session_key)
  └─ agent_factory(session_key) → main.factory 里的闭包
       └─ _build_agent(...)
            ├─ SkillsLoader(workspace/skills).build_skills_summary()   ← 扫盘一次
            ├─ ContextBuilder(workspace, identity_file, skills_summary, memory_file)
            ├─ MemoryConsolidator(provider, workspace, token_budget)   ← 0 则 None
            └─ AgentLoop(...)
                 └─ __init__: history = _repair_history(session_manager.get_history(key))
                      └─ 从末尾往前找「有 tool_calls 但结果不全」的 assistant → 丢弃该残段
                         （崩溃/中断留下的半截历史原样回传会被接口 400，会话从此打不开）
                    print("会话 cli:local：已恢复 N 条历史消息 / 新会话")  → stderr
```

### 5.4 上下文压缩（同 `DeepsClaw-业务流程图.drawio` 第 2 页）

```text
AgentLoop.run 每次调模型之前：_compact_for_model(messages)
  └─ consolidator.maybe_consolidate(messages)
       ├─ estimate_tokens: Σ len(json.dumps(msg, ensure_ascii=False)) // 2
       ├─ ≤ 24000 → 原样返回
       ├─ 条数 < 8（KEEP_RECENT+2）→ 压无可压，如实记日志后返回
       ├─ tail_start = _align_tail_start(messages, len-KEEP_RECENT)
       │     └─ 向前吸附：绝不把 assistant(tool_calls) 与其 tool 结果切散（切散即 400）
       ├─ _summarize(old)：跳过全部 tool 消息 → provider.chat(不带工具、不流式)
       │     └─ 失败/空/finish_reason=error → SUMMARY_FAILED 占位（绝不抛）
       ├─ compressed = [system, {system: "[历史摘要]: …"}, *messages[tail_start:]]
       └─ _save_to_history(summary, n) → 追加 workspace/workspace/memory/HISTORY.md
  ★ 压缩结果只用于「这一次请求」；messages 才是循环自己的账本（tool 结果要按 id 配对）
  ★ 压缩后不比原来短 → 直接沿用原表
```

### 5.5 MCP 接入（同 `DeepsClaw-业务流程图.drawio` 第 3 页）

```text
启动：connect_all(timeout=30s) 逐个连、不并发
  for server in enabled:
      try: await asyncio.wait_for(_connect_one(server), 30)
      except TimeoutError → "[MCP] [!] X 连接超时，已跳过"
      except Exception   → "[MCP] [!] X 连接失败，已跳过"
  _connect_one:
      params = StdioServerParameters(command, args, env, cwd)
      read, write = await stdio_client(params).__aenter__()      ★ 手动进入
      session = await ClientSession(read, write).__aenter__()
      await session.initialize()                                ← 握手
      tools = (await session.list_tools()).tools
      逐个包成 MCPTool(server__tool)；重名只跳过该工具（不 raise）

运行：agent 调 poetry__search_poetry({keyword:"月"})
      → MCPTool.execute(**kwargs) → MCPClientManager.call_tool(...)
      → session.call_tool(...) → 取文本块拼成 str（isError → "错误：…"）

退出：shutdown()  for server in reversed(连接顺序): _dispose(context, session)
      ★ server 之间逆序（一个 server 正常、两个必炸）
      ★ server 内部 session 先、stdio 后
      ★ 不能 gather 并发退；不能给单个退出加 wait_for 超时
```

### 5.6 子智能体派生（同 `DeepsClaw-业务流程图.drawio` 第 3 页）

```text
主 agent 调 spawn_subagent({task, model?})
  ├─ task 为空 → 错误文本
  ├─ provider = provider_factory(model)    独立实例（默认百炼，extra_body 刻意不透传）
  ├─ registry = 复制父工具集，跳过父辈的 spawn_subagent
  ├─ if current_depth+1 < max_depth(=2): 注册新的 spawn（传复制好的那份注册表）
  ├─ agent = AgentLoop(provider, registry, SubagentContext(workspace),
  │                    max_steps=20, session_manager=DummySessionManager(), sink=None)
  │     ├─ SubagentContext 只覆盖 build_system_prompt → 一句话 + 时间 + 工作区
  │     │    （拿不到人设 / 长期记忆 / 技能摘要）
  │     └─ 不写磁盘、不挂 sink（过程不打到用户终端）
  ├─ result = await agent.run(task)
  └─ finally: await provider.aclose()      ← 不关就每派生一次漏一份连接池
```

### 5.7 事件契约（前端接入依据，`agent/events.py`）

```json
{"type": "thinking",    "text": "…", "step": 0, "ts": 1759900000.12}
{"type": "content",     "text": "…", "step": 1, "ts": …}
{"type": "tool_call",   "id": "call_1", "name": "list_dir", "arguments": {…}, "step": 0, "ts": …}
{"type": "tool_result", "id": "call_1", "name": "list_dir", "result": "…",
                            "truncated": false, "length": 128, "step": 0, "ts": …}
{"type": "error",       "message": "…", "step": 0, "ts": …}
{"type": "turn_end",    "reply": "…", "reason": "finished", "ts": …}
```

**5 条顺序保证**（AgentLoop 串行 inline await 投递，sink 不得重排、不得并发发送）：
1. 同一 step 的 thinking/content 增量严格按模型吐字顺序到达；
2. 某 step 的 tool_call 出现在该 step 最后一个增量之后；
3. tool_result 紧跟它对应的 tool_call；
4. error 一定在 turn_end 之前；
5. turn_end 恰好一次，且一定是最后一条。

**reason 取值**：`finished` / `error` / `loop_break` / `max_steps` / `cancelled`。
`turn_end` 就是「会话空闲」信号；`reason=error` 表示本 turn 已渲染的流式内容作废。

---

## 6. 核心机制与关键数值

### 6.1 关键常量一览（实测自源码）

| 常量 | 值 | 位置 | 为什么是这个数 |
|---|---|---|---|
| `CIRCUIT_BREAK_THRESHOLD` | 20 | loop.py | 同一组调用重复 20 轮即熔断 |
| `WARN_THRESHOLD` | 10 | loop.py | 10 轮开始给模型提示 |
| `LOOP_WINDOW_SIZE` | 30 | loop.py | 滑动窗口大小（按「轮」计指纹） |
| `max_steps`（主 agent） | 50 | config `max_iterations` | 单轮最多 50 次模型调用 |
| `SUBAGENT_MAX_STEPS` | 20 | spawn.py | 子任务只干一件事 |
| `max_depth`（子 agent） | 2 | spawn.py | 主 → 子 → 孙（孙不能再派） |
| `TOOL_RESULT_PREVIEW_LIMIT` | 2000 | loop.py | 推给前端的预览上限（模型拿全文） |
| `MAX_READ_CHARS` | 16000 | filesystem.py | 单次读文件上限 |
| `MAX_MEMORY_CHARS`（读侧 / 写侧） | 4000 / 4000 | context.py / tools/memory.py | 注入 Prompt 与写入把关，**两处需同步改** |
| `MAX_ITEM_CHARS` | 200 | tools/memory.py | 单条记忆上限 |
| `COMMAND_TIMEOUT` | 60.0 s | shell.py | exec 超时 |
| `MAX_OUTPUT_CHARS`（exec） | 10000 | shell.py | exec 输出截断 |
| `SEARCH_TIMEOUT` | 15.0 s | web_search.py | 博查搜索超时 |
| `MAX_OUTPUT_CHARS`（search） | 8000 | web_search.py | 搜索结果截断（**按条**切） |
| `MIN/MAX/DEFAULT_RESULTS` | 1 / 50 / 5 | web_search.py | 接口只接受 1~50 |
| `FETCH_TIMEOUT` | 20.0 s | web_fetch.py | 抓网页超时（整条请求） |
| `MAX_BYTES` | 2 MB | web_fetch.py | 响应体上限（流式边读边数） |
| `MAX_CONTENT_CHARS` | 12000 | web_fetch.py | 正文截断 |
| `MAX_REDIRECTS` | 5 | web_fetch.py | 跳转次数硬上限（最多 6 次请求） |
| `DEFAULT_TOKEN_BUDGET` | 24000 | memory.py | 压缩触发阈值 |
| `KEEP_RECENT` / `MIN_MESSAGES_TO_COMPRESS` | 6 / 8 | memory.py | 压缩后保留最近 6 条 |
| `DEFAULT_CONNECT_TIMEOUT` | 30.0 s | mcp.py | 留给「装依赖 / 下载包」 |
| `CANCEL_GRACE` | 5.0 s | gateway.py | 取消子任务的宽限时间 |
| `timeout`（模型请求） | 120 s | config | SDK 默认 600 太慢；流式下管的是**分片间隔** |
| `log_level` 默认 | **DEBUG** | config.py | ⚠ 线上应显式设 INFO（见 §7.1-B6） |

### 6.2 沙箱与安全（三层 + 四道闸）

| 层 | 机制 | 实现位置 |
|---|---|---|
| **文件工具** | `os.path.realpath` 解符号链接 + `os.path.normcase`（Windows 转小写）+ `os.path.commonpath` 判归属（**不是 startswith**，否则 `/ws-evil` 会被判在 `/ws` 内） | `filesystem.py::_WorkspaceTool._resolve` |
| **命令执行** | **先拦后跑**：9 类正则黑名单，命中就不 fork（无生效窗口）；`cwd` 限定工作区；`stdin=DEVNULL`；60s 超时；输出截断 | `shell.py::ExecTool.deny_patterns` |
| **网页抓取** | 协议白名单 + 主机名黑名单 + IP 字面量判段 + **DNS 解析处校验**（防 rebinding）+ 每跳重定向重新校验 + 流式限量 | `web_fetch.py::_SafeNetworkBackend` / `_validate_url` |
| **长期记忆** | 路径只来自配置构造参数，模型**无法指定**写到别处 | `tools/memory.py::MemoryTool` |

**`exec` 的 9 类危险命令拦截表**（`shell.py::deny_patterns`）：

| # | 类别 | 覆盖要点 |
|---|---|---|
| 1 | 递归或批量删除 | `rm -r/-f/-rf`、`rm *`、`del /f/q/s/a`、`del *`、`Remove-Item -Recurse`、`rmdir/rd`、`shred/sdelete`、`find -delete`、`truncate -s 0` |
| 2 | 格式化磁盘或分区 | `mkfs*`、`format C:`、`fdisk/parted/diskpart/wipefs` |
| 3 | 权限升级 | `sudo/sudoedit/doas/pkexec/gsudo/runas`、`su`、`Start-Process -Verb RunAs` |
| 4 | 关机重启 | `shutdown/reboot/poweroff/logoff/halt`、`init 0/6`、`Stop/Restart-Computer` |
| 5 | 危险权限修改 | `chmod 777/666/0000/a+rwx/+s/o+w`、`chown -R`、`takeown/cacls/icacls`、`umask 000` |
| 6 | 网络后门 | `nc/ncat/socat/telnetd`、`/dev/tcp/`、`curl x \| bash`、`ssh -R`、`netsh advfirewall`、`iptables -j ACCEPT`、`msfvenom`、`powershell -enc`、`base64 -d \|` |
| 7 | 覆写设备 / 磁盘镜像 | `dd of=/dev/…`、`> /dev/sda`、`\\.\PhysicalDrive`、`hdparm --write`、`blkdiscard` |
| 8 | 覆写系统关键文件 | `> /etc/passwd\|shadow\|sudoers\|fstab\|hosts`、`> /boot\|/sys\|/proc`、`> C:\Windows\System32` |
| 9 | 进程炸弹 | bash fork bomb、`%0\|%0`、`kill -1`、`killall5` |

> 设计取向：**宁枉勿纵** —— `echo "别用 rm -rf"` 也会被拦。误报代价 = 换个说法重试；漏报代价 = 用户数据。
> 唯一例外：单个文件的普通 `rm a.txt` / `del a.txt` 默认放行。

**防 SSRF 的四道闸**（`web_fetch.py`）：

```text
① _validate_url（发请求前，静态）
     scheme ∈ {http, https}；hostname 非空；不含 user:password；
     hostname ∉ {localhost, localhost.localdomain, ip6-localhost, metadata,
                  metadata.google.internal} 且不以 .local/.internal/.localhost 结尾；
     IP 字面量直接判 _blocked_reason
② _SafeNetworkBackend.connect_tcp（真正建连时，动态 —— 防 DNS rebinding）
     重写 httpcore 后端：await loop.getaddrinfo(host, port)
     ★ 所有候选 IP 只要有一个落在禁止段 → 整体拒绝（A 记录公网 + AAAA 内网也不行）
     然后用同一个 IP 去 super().connect_tcp（不再让 httpx 解析第二遍）
③ _get_with_redirects（follow_redirects=False，每跳重新 _validate_url，上限 5 跳）
④ _read_body（流式 aiter_bytes，边读边数，超 2MB 断开）
```

禁止网段：IPv4 共 11 段（含 `169.254.0.0/16` 云元数据、`100.64.0.0/10` CGNAT）+ IPv6 共 7 段；
`_blocked_reason` 还会把 `::ffff:127.0.0.1` 这类 IPv4 映射地址**换算回 IPv4 再判一次**。

### 6.3 流式解析机制（`providers/openai_compat.py`）

**为什么强制流式**：不是「顺手支持流式」，而是让流式与非流式**只有一套解析逻辑**，
避免「带回调和不带回调结果不一致」这类只在边角发作的 bug。

```text
chat(messages, tools, model, on_delta)
  params = {model, messages, stream=True, extra_body}
           ↑ stream 必须作显式 kwarg（只写进 extra_body 会让 SDK 按普通 JSON 解析 SSE 流）
  tools 为空则不传该字段（有些服务收到 tools=[] 直接报参数错）
  _build_extra_body():
     ★ stream / stream_options 由本模块最后写入
       （extra_body 的值会覆盖显式 kwarg，用户写 "stream": false 会把请求悄悄变回非流式）
  try: _stream_once(params, on_delta)
  except BadRequestError: _retry_without_usage(...)   ← 去掉 stream_options 重试一次
  except Exception: → _error_response(exc)            ← 转成 finish_reason=error，不抛

_StreamAccumulator.feed(chunk):
  usage 每片都看一眼（有服务把它塞在带 finish_reason 的那片里）
  choices 为空 → None（角色片 / usage 尾片不产生增量）
  tool_calls → _merge_tool_call_deltas（按 index 归并：id/name 取首个非空，arguments 往后拼）
  content / reasoning_content 分别 append；二者都空 → None

_StreamInterrupted：请求成功、内容收了一半、连接没了
  → 错误信息里带上已收到的字符数（「看到的那半截不作数」）
_parse_arguments：JSON 非法 → 返回 {} 而非抛异常（工具层报「缺少参数」，模型通常能自我纠正）
```

### 6.4 四种失败降级（全项目统一约定）

| 失败点 | 降级方式 | 位置 |
|---|---|---|
| 模型调用失败（网络/鉴权/限流/断流） | 转成 `finish_reason="error"` 的响应，**不抛异常** | `openai_compat.py` |
| 工具执行抛异常 | 捕获 → 返回 `错误：工具 'x' 执行失败 - TypeError: …` 交回模型 | `registry.py::execute` |
| Agent 循环异常/熔断/步数耗尽 | 统一变成**文本返回值**（否则渠道什么都收不到） | `loop.py` |
| 渠道发送失败 | 记日志后返回，不抛（不拖垮分发循环） | `qq.py` / `gateway.py` |

> 这是项目最一致的架构约定：**失败要降级、不要抛出**。抛异常会让整轮任务断在半路，渠道那边什么都收不到。

### 6.5 路径语义（最容易搞混的一块，实测梳理）

| 配置 / 代码 | 值 | 实际解析结果 |
|---|---|---|
| `PROJECT_ROOT` | — | `…/DeepsClaw/`（`config.py` 文件所在目录） |
| `cfg.workspace` | `"workspace"` | `<root>/workspace`（相对路径按 **PROJECT_ROOT** 解析，与 cwd 无关） |
| `cfg.identity_file` | `"../identity.md"` | 相对 **workspace** 解析 → `<root>/identity.md` ✔ **实测人设已正确加载** |
| `cfg.memory_file` | `"workspace/memory/MEMORY.md"` | 绝对路径（`_resolve_path`）→ `<root>/workspace/memory/MEMORY.md` |
| 会话文件 | `os.path.join(cfg.workspace, "workspace", "sessions")` | `<root>/workspace/workspace/sessions/` ← **双层 workspace** |
| `HISTORY.md` | `os.path.join(workspace, "workspace", "memory", …)` | `<root>/workspace/workspace/memory/HISTORY.md`（与 `memory_file` **不同目录**） |
| 技能目录 | `os.path.join(cfg.workspace, "skills")` | `<root>/workspace/skills/` ← ❌ **不存在，技能实际未被加载**（§7.1-B2） |
| 文件工具沙箱 | `cfg.workspace` | `<root>/workspace/`（agent 只能在这里读写） |

> ⚠ **三套「相对谁」的规则并存**：人设相对 workspace、记忆相对 PROJECT_ROOT、技能相对 workspace。改路径相关代码前务必先对照此表。

### 6.6 内置工具清单（实测 `/tools` 输出）

| 工具 | 作用 | 关键约束 |
|---|---|---|
| `read_file` | 读文本文件 | 路径必须在工作区内；超 16000 字符截断 |
| `write_file` | 写文件 | 自动建父目录；**整文件覆盖**；写入前校验沙箱 |
| `list_dir` | 列目录 | 显示大小与类型；无条数上限（隐患，见 §7.1-B9） |
| `exec` | 执行命令 | 9 类拦截 + 60s 超时 + 输出截断 + stdin 接空设备 |
| `web_search` | 联网搜索（博查） | 需 key，否则返回「未配置」；默认 5 条 |
| `web_fetch` | 抓网页正文 | 防 SSRF；不走系统代理；不解析 PDF/图片 |
| `save_memory` | 写长期记忆 | 路径只来自配置；写满 4000 字符拒写 |
| `spawn_subagent` | 派临时子智能体 | **配了密钥才注册**；深度上限 2 |
| `server__tool` | MCP 来的外部工具 | 名字带 server 前缀；数量取决于连上了哪些 server |

实测装配结果：内置 7 个 + `spawn_subagent` = **8 个**；启动横幅显示「共 7 个」（`spawn_subagent` 单独一行显示，故不计入）。

---

## 7. 碰到过的问题（分三类）

### A. 代码注释里记录的历史坑（已修，但改代码时别踩回去）

| # | 现象 | 原因 | 现在的做法 |
|---|---|---|---|
| A1 | MCP 用 `with` 语法糖时 `initialize()` **直接死等** | `with` 退出即拆掉 anyio 任务组，而握手响应需要一个活着的后台循环来读 | **手动 `await ctx.__aenter__()`**，把生命周期攥在自己手里（`mcp.py` 模块文档） |
| A2 | 一个 MCP server 完全正常，**配上第二个就必炸** | 每次连接往当前任务压一对 anyio cancel scope 形成栈；正序退出等于从栈底抽 → anyio 直接取消任务 | `shutdown()` 用 **`reversed()`** 逆序断开 |
| A3 | `Attempted to exit cancel scope in a different task than it was entered in` | anyio 的 cancel scope **按任务记账** | `connect_all` / `shutdown` 必须同一 asyncio 任务；**不能 gather 并发退**；**不能加 wait_for 超时**（取消会污染作用域栈） |
| A4 | 敲 `/exit` 却**卡死**退不出去 | 早期在 `_serve` 用 `asyncio.wait` 等「网关任务先完成」，而两条消费循环是 `while True`，永不完成 | **谁掌握子任务，谁负责判断收工**：退出判断搬进 `Gateway.run()` 内部 |
| A5 | 收尾时进程退不出去 | 一条取消只保证「下一个 await 点抛 CancelledError」，对卡在不可取消等待里的任务无效 | `_cancel_all` 加 `timeout=5s`，等不到就放弃（**宁可留僵尸协程，也不要卡住退出**） |
| A6 | `Gateway.run` 不能用 `asyncio.TaskGroup` 替代 `gather` | TaskGroup 正常退出时会等**所有**子任务结束，而消费循环永不结束 → `run()` 永不返回 | 用 `asyncio.gather` + 手工 `except CancelledError` 收尾 |
| A7 | 会话文件被中断后**这个会话再也打不开**（接口 400） | 进程死在「assistant 已发 tool_calls、工具结果还没写上」的瞬间 | `_repair_history`：从末尾往前找不完整的组，**丢弃该残段** |
| A8 | 历史压缩后**每轮请求都 400** | 切入点落在 `assistant(tool_calls)` 与其 `tool` 结果中间 | `_align_tail_start` **向前吸附**到最近的组起点（「向前多留几条不会出错，向后切错一条就出事」） |
| A9 | 压缩后接口 400 | 追加新消息后开头是 tool 消息 | 压缩结果必须以 user 开头；压缩只作用于**发出去的那份**，循环自己的 `messages` 账本不动 |
| A10 | 中文历史**每轮都触发压缩**（费用暴涨） | `json.dumps` 默认 `ensure_ascii=True`，一个汉字变 6 个转义字符 | `ensure_ascii=False` |
| A11 | DeepSeek 思考模式下一轮**直接 400** | 工具调用轮次的 `reasoning_content` 必须随 tool_calls 原样回传，且**位置在消息顶层** | `_build_assistant_message` 把 reasoning 放 `message["reasoning_content"]`；无工具调用时**不带** `tool_calls` 字段 |
| A12 | 熔断后**下一次请求还是 400** | assistant 消息已进 `messages`，但它的 tool_call 没有对应 tool 结果 | 熔断时**逐个补占位结果**，不能直接跳出 |
| A13 | 多调用死循环检测**有盲区** | 按「单次调用」计数时，窗口 30 内每轮 3 个调用最多凑到 10 次，永远够不到 20 的熔断线 | 改为**按「轮」计数整轮调用组合的指纹**（参数 sort_keys 排序，防换键序绕过） |
| A14 | 老服务拒绝 `stream_options` → 400 | 少数 OpenAI 兼容服务（老 vLLM/Ollama）不认这个参数 | `_retry_without_usage` 去掉它重试一次；**只有重试确实成功才记住降级** |
| A15 | `extra_body` 里写 `"stream": false` 把请求悄悄变回非流式 | SDK 里 `extra_body` 的值会**覆盖**显式 kwarg | `_build_extra_body` 最后写入 stream/stream_options（字典后写者胜） |
| A16 | 同一句话被打印两遍 | agent 挂了写 stdout 的 sink，同时 Gateway 又把返回值交给渠道打印 | `TerminalSink` 只管思考与工具（走 stderr），**回复不归它管** |
| A17 | 提示符被日志吃掉（`你 > 10:42:20 [INFO] QQ 渠道启动中…`） | 启动横幅打得太早 | **横幅先生成、后打印**，由 `CLIChannel` 在**就绪时**、打印第一个提示符之前打出 |
| A18 | 横幅右边框参差不齐 | `len()` 把中文算 1 列，实际占 2 列 | `_display_width` 按 `unicodedata.east_asian_width` 在 (W, F) 时算 2 列 |
| A19 | 横幅永远显示「可用工具：」后面空着，而 `/tools` 报「没有装配任何工具」 | 早期在 `_print_banner` 里就地新建了一个**空注册表** | 必须把**真正装好工具的那个注册表**传进来（「打印的和实际加载的不是同一批，比不打印更误导」） |
| A20 | 窄终端下看不到最新加的工具 | 从头截断会吃掉末尾几项（正是用户最想确认「装上没有」的） | 工具清单**从末尾往前铺**，铺不下在最前面用省略号收尾 |
| A21 | 用户敲了第二句，两次提问的回复叠成一团 | agent 跑一轮要几十秒，期间终端仍在读输入 | `_response_event` **接力式**：`start()` 在等回复时根本不读键盘 |
| A22 | 第二次提问不等回复 | `asyncio.Event` 一旦 set 就一直是 set 状态 | 发消息前 `clear()`，`send()` 里再 `set()`（且 set 放在 print 之后） |
| A23 | `/clear` 后历史「没真清」 | 只清内存或只清磁盘 | `clear_history` 内存 + 磁盘一起清；取的是**缓存里那个 agent** |
| A24 | `python main.py` 把事件循环钉住，用户永远等不到回复 | `input()` 是阻塞调用 | `await asyncio.to_thread(self._read_line, PROMPT)` |
| A25 | SDK 在**当前目录**写 `botpy.log` | botpy 默认 file handler 落到 `os.getcwd()` | `QQBotClient(..., bot_log=False)` |
| A26 | Ctrl+C 时 `_serve` 协程退不出去 | 在 await 点收到的取消只作用于本协程，而 finally 里的 shutdown 还要 await | `except asyncio.CancelledError: raise` + finally 里 `_cancel_all` 先收子任务 |
| A27 | 中文 Windows 下命令输出整段乱码 | `cmd.exe` 跟随控制台代码页输出 GBK | `_decode_output`：先 UTF-8，失败退回 `locale.getpreferredencoding(False)` |
| A28 | 抓网页的两次 DNS 解析之间可被换答案（DNS rebinding） | 「先解析判 IP、再让 httpx 自己解析一次」是经典 SSRF 漏洞 | 重写 httpcore 后端的 `connect_tcp`，校验与实际连接用**同一个 IP** |
| A29 | 一个公网地址用 302 就能把请求带进内网 | `follow_redirects=True` 时跳转目标完全绕过校验 | `follow_redirects=False` + 手动逐跳 `_validate_url` + 上限 5 跳 |
| A30 | 页面正文全被吞掉 | 把 `meta/link/input/br` 这类**空元素**放进了 `_SKIP_TAGS`，它们的「跳过深度」永远等不到结束标签 | `_SKIP_TAGS` **只放有闭合标签的容器** |
| A31 | 声明 `MCP_SERVERS` 却连不上（Windows） | `shlex.split` 用 `posix=False` 时带空格的路径被切成两半 | **`posix=True`** 才剥引号 |
| A32 | MCP 工具重名导致**整个进程起不来** | `ToolRegistry.register()` 遇重名直接 `raise` | MCP 侧**先检查再收**，重名只跳过该工具并告警 |
| A33 | `config.json` 里配了 `extra_body` 却读到 `None` | 遍历 JSON 的 `_get_json_value` 只认标量，遇对象返回 None | `extra_body` 在 json 侧**单独取**（`_pick_json`） |
| A34 | json 里 `"models": "xxx"` 时主模型静默退回默认值 | `_pick(data, "models.main")` 的点号路径在字符串形态下不存在 | `_resolve_models` 先归一，主模型从 `models.get("main")` 取 |
| A35 | `logging.basicConfig(level="VERBOSE")` 直接抛 `ValueError` | 非法级别在启动流程中段炸开 | `_get_log_level` 校验合法性，非法则退回 INFO 并提示 |
| A36 | 「配置错误」类异常被伪装成程序 bug | 混在一起无法区分责任 | 单独 `ConfigError(ValueError)` 类型，`main()` **只拦这一类** |

### B. 当前仍未修的问题（实测确认，按严重度排序）

#### 🔴 安全相关（优先处理）

**B1｜命令黑名单可被 `write_file` + `exec` 完全绕过**
- 证据：`shell.py:82-192` 的黑名单是**文本正则**，而 `write_file` 能任意写文件、`exec` 能执行任意脚本。
- 绕过示例：`write_file("x.sh", "rm -rf ~")` → `exec("bash x.sh")`；或 `python -c "import shutil;shutil.rmtree('.')"`、`git clean -fdx`、`xargs rm`、`find -exec rm`、`mv a /dev/null`。
- 结论：**黑名单只能防「手滑的模型」，防不住「被注入的模型」**。
- 建议：接受这个事实并在文档里写明；真要收紧应做「允许清单 + 命令确认」，而不是继续加正则。

**B2｜`exec` 子进程继承父进程完整环境变量（密钥可被读走）**
- 证据：`shell.py:253-260` 的 `create_subprocess_shell` 未传 `env=`，于是 `os.environ` 全量继承；而密钥就在环境里（`config.py` 读环境变量优先）。
- 影响：模型执行 `env` / `set` / `Get-ChildItem Env:` 即可把 `LLM_API_KEY`、`BOCHA_API_KEY`、`SUBAGENT_API_KEY` 打印进自己的上下文。
- 建议：显式传一份白名单 `env=`（PATH / COMSPEC / SystemRoot / LANG 等），或至少注入前先把密钥从子进程环境里剔除。

**B3｜`web_fetch` 的 DNS 层校验会静默降级**
- 证据：`web_fetch.py:306-310` 探测 httpx 私有属性 `_pool._network_backend`，拿不到时**只 warning 就继续**（「DNS 层校验已跳过」）。
- 影响：SSRF 防线从四道闸退化成「静态 URL 校验 + IP 字面量」，DNS rebinding 与「域名解析到内网」重新可行。
- 建议：改为 fail-closed（探测失败就拒绝抓取并明确报错），或在工具描述里降级说明。

**B4｜`identity.md` 里的「禁止读 .env」只是提示词约束**
- 现状：`ReadFileTool` 的沙箱只限制「在工作区内」。当前 `workspace` 指向 `<root>/workspace`，`.env` 与 `config.json` 在项目根，**恰好被沙箱挡在外面** ✔。
- 风险：一旦把 `workspace` 改成 `"."`（模板 `config.example.json` 就是这个值），`config.json`（含全部真实密钥）与 `.env` 立刻落进可读范围。
- 建议：把「敏感文件黑名单」做进 `ReadFileTool`，或在文档里明确警告不要用 `"."`。

#### 🟠 功能缺陷（影响可用性）

**B5｜`skills/` 目录实际从未被加载（实测确认）**
- 证据：`main.py:366` 用 `os.path.join(workspace, "skills")` = `workspace/skills`，该目录**不存在**；技能实际在项目根 `skills/`。
- 实测：`SkillsLoader('workspace/skills').list_skills()` → `[]`；启动横幅显示「技能 无」。
- 影响：`identity.md` 与 README 里描述的「技能」能力实际不生效。
- 建议（与 `identity_file` 同一思路）：`config.json` 加 `skills_dir` 字段，默认 `../skills`。

**B6｜`config.json` 里 MCP 用的是机器绑定的绝对路径 + 相对 args**
- 证据：`"command": "C:/Program Files/Python314/python.exe"`（换机器即失效），而 `"args": ["mcp_servers/poetry_server.py"]` 是**相对路径**，命令**不经过 shell**，按**进程 cwd** 解析。
- 影响：从别的目录启动（`python DeepsClaw/main.py`）或换机器 → `[MCP] [!] poetry 连接失败`。
- 建议：MCP 配置支持 `cwd` 字段并默认 PROJECT_ROOT；或 `command` 写 `python`（PATH 里的话）。

**B7｜MCP 工具调用没有任何超时**
- 证据：`mcp.py:274` `await session.call_tool(...)` 裸调用；对比 `exec` 60s、`web_fetch` 20s、MCP 连接 30s。
- 影响：一个卡住的 MCP 工具会永久挂住主 agent；因 `gateway._process_inbound` 是**轮内串行**，所有会话一起卡死。
- 建议：`asyncio.wait_for(session.call_tool(...), 60)` 并把超时转成可读错误文本。

**B8｜`spawn_subagent` 事实上总是被注册（与文档不符）**
- 证据：`config.py:385` `subagent_api_key = _pick(...) or api_key` —— 缺失时**回退主密钥**，而 `api_key` 必填 → `has_subagent` 恒真。
- 影响：没配子智能体密钥时依然注册该工具，运行时用**主模型密钥**去请求百炼 base_url → 第一次派活才 401；与 HANDOVER「配了密钥才是 8 个工具」不符。
- 建议：`_pick(...)` 不加 `or api_key`（或改成显式 `subagent.enabled` 开关）。

**B9｜`list_dir` 没有条数上限**
- 证据：`filesystem.py:252-264` 一次性返回全部条目；`read_file` / `exec` / `web_fetch` 都有截断，唯独它没有。
- 影响：`node_modules`、`venv`、十万级目录会一次性把上下文顶爆。
- 建议：加 `MAX_ENTRIES`（如 500）并在结尾标注「还有 N 项未列出」。

**B10｜熔断那一轮的收尾说明不落盘，重启后「用户看到的」与文件里的不一致**
- 证据：熔断分支只调 `_save_to_history([*turn, notice])`（`loop.py:359`，只进内存）；而 max_steps 分支是 `_persist` + `_save_to_history`（`loop.py:233-235`）。
- 建议：熔断分支补 `self._persist(...)`。

**B11｜`input(PROMPT)` 的提示符写进 stdout，破坏「纯净回答」承诺**
- 证据：`cli.py:190` `input(PROMPT)`（默认写 stdout）；而 `cli.py:114-118`、`main.py:19-22`、`HANDOVER.md:700` 都承诺 `python main.py > answer.txt` 拿到干净回答。同现象：命令回显（`/help` `/tools` `/clear`）也走 stdout。
- 影响：一次性提问（管道输入）时 `你 > ` 会混进 `answer.txt`。
- 建议：读入前先 `print(PROMPT, file=sys.stderr, end="", flush=True)`，再调 `input()` 不传提示符；或把命令回显也改走 stderr。

**B12｜`_repair_history` 在某些路径下会误删正常对话**
- 证据链：若工具执行路径出现异常且 assistant 消息已在 `loop.py:211` 落盘而 tool 结果永不写 → 残段夹在**中间**；`_repair_history`（`loop.py:538-549`）一旦命中就 `return history[:index]`，**把残段之后的全部合法轮次一起丢掉**，且不回写磁盘 → 每次启动重放同一次截断。
- 前提：函数假设「残段只可能出现在末尾」，而同进程继续对话会把新消息追加到同一文件、把残段夹到中间。
- 建议：改为逐组校验并只丢最后一段，或把修复结果回写磁盘。

**B13｜子智能体没有墙钟超时**
- 证据：`spawn.py:261-276` 唯一约束是 `max_steps=20`；单步最长可能 60s(exec) + 120s(LLM)。
- 影响：最坏情况主 agent 被阻塞十几分钟，轮内串行会让所有会话一起卡住。
- 建议：给子 agent 包一层 `asyncio.wait_for`（如 300s），超时返回可读文本。

**B14｜MCP 工具名无字符集 / 长度校验**
- 证据：`mcp.py:151,382` 直接 `server__tool` 拼名。OpenAI 兼容接口要求 function name 匹配 `^[a-zA-Z0-9_-]{1,64}$`。
- 影响：server 名 + 工具名超过 64 字符，或含点 / 空格 / 非 ASCII → **整个 tools 请求被拒**（不是只坏一个工具）。
- 建议：包装时做字符替换与长度截断，并检查与内置工具重名。

**B15｜MCP 连接失败时可能泄漏子进程与 anyio 任务组**
- 证据：`mcp.py:129-144` 的 `try/except BaseException` 覆盖了 `__aenter__` 与 `initialize()`，但 `session.list_tools()`（141 行）在 **try 之外**。若它抛异常或被 `wait_for` 取消（超时点常常正落在这里），已进入的两层上下文**既没退、也没记进 `_context_managers`** → `shutdown()` 也找不到它们。
- 影响：这是「MCP 子进程变孤儿」的代码级成因。
- 建议：把 `list_tools()` 移进 try，或先登记 dict 再列工具。
#### 🟡 文档 / 一致性 / 维护性

| # | 问题 | 证据 | 建议 |
|---|---|---|---|
| B16 | `HANDOVER.md §6.7` 说 `_session_key` 用 `chat_id`，**实际代码用的是 `sender_id`** | `gateway.py:346` | 代码是对的（与 README / identity 一致），修文档即可。注意：用 sender_id 意味着 QQ **群里所有人的消息进同一个会话** |
| B17 | README 说「Python 3.14+」，实际语法下限是 3.10+ | HANDOVER §3 已更正，README 未同步 | 统一为 3.10+（实测在 3.14.3 上运行） |
| B18 | README 说「8 个内置工具」，实测内置 7 个（横幅显示「共 7 个」） | `main.py:486` 特意把 `spawn_subagent` 排除在清单外单独显示 | 改文档表述 |
| B19 | 全项目**没有任何自动化测试** | 35 个提交、6500 行、0 个测试文件；`.pytest_cache/` 是历史遗留 | 优先补三类：**配置合并优先级、工具沙箱路径逃逸、会话读写往返** |
| B20 | 无 `requirements.txt` / `pyproject.toml` | 依赖全靠 README 手装 | 补一个并标注可选依赖 |
| B21 | 默认日志级别是 **DEBUG** | `config.py::_get_log_level` 三处都取不到时返回 DEBUG | 默认改 INFO |
| B22 | `main.py::_setup_logging_done()` 是**完全没被调用的空函数** | `main.py:238-240` | 删 |
| B23 | `poetry_server.py` 文档字符串残留 `@@BT@@` 占位符 | 第 14 / 19 行 | 换成反引号 |
| B24 | `poetry_server.py::search_poetry` 的 docstring 与实现相反 | docstring 说空关键词「返回全部」，实现返回「未找到」 | 改 docstring 或改实现 |
| B25 | `cli.py:81-82` 注释说会话键是 `cli:direct`，实际是 `cli:local` | `gateway.py:346` + `cli.py:86` | 改注释 |
| B26 | `mcp_servers/README.md` 只讲 `.env` 配法，漏了 `config.json` 这条路 | 实际仓库的 `config.json` 正是用 `mcp_servers` 段配的 | 补上 config.json 写法 |
| B27 | `.gitignore` 尾部混入**上一个项目的残留**，且中文注释是 GBK 乱码 | `data/index/`、`frontend/node_modules/`、`scripts/build_index.py`、LangGraph checkpoint 等本项目全无 | 清理并统一 UTF-8 |
| B28 | 仓库没有 `LICENSE` 文件 | README 声明 MIT 但 GitHub 检测不到 | 补标准 MIT 全文 |
| B29 | 双层 `workspace/` 路径反直觉 | `main.py:249` 拼 `workspace/workspace/sessions`；仓库里 `workspace/sessions/`、`workspace/memory/` 两个空目录在误导人 | 明确两套路径语义，或统一到一层 |
| B30 | `memory.py` 的 docstring 仍写「默认 6000」，实际是 24000 | `memory.py:29,87` vs `memory.py:45` | 按 docstring 调参的人会拿到 4 倍预算 |
| B31 | 记忆按**追加**写入，但读侧截断保留**开头** | `context.py:130` 保留前 4000 字符 | 记忆越长，被截掉的是**最新**的事实 |
| B32 | `session/manager.py` 的残行拼接会把「丢 1 条」放大成「丢 2 条」 | `manager.py:110-112` 直接写一行加换行；若上次被强杀留下无换行残行，下一次追加会拼在残行尾部 | 写前检查文件末尾是否有换行 |
| B33 | `qq.py` 的 `msg_id` 永远为空串 | `qq.py:222,232` 传 `msg_id=message.reply_to or ""`，但全项目没有任何地方给 `OutboundMessage.reply_to` 赋值 | 若要用被动消息需把入站 msg id 透传上来 |
| B34 | `qq.py::_AT_BOT` 只认数字 openid | `qq.py:55` 的正则只匹配数字，而本部署 openid 是十六进制（会话文件名 `qq_BE7351…`） | 需用真实群消息确认是否影响 @ 剥离 |
| B35 | 入站处理是**串行**的 | `gateway._process_inbound` 单协程消费 | 要并发得按 session_key 分派 worker + 给 `_agents` 加锁 |
| B36 | `web_search` 的 `int(max_results)` 在 try 之外 | `web_search.py:132`，模型传非数字字符串会抛 ValueError 冒泡到 registry 兜底 | 移进 try 或做类型校验 |

### C. 使用 / 环境层坑

| # | 坑 | 说明 |
|---|---|---|
| C1 | **环境变量会静默盖掉 `config.json`** | 优先级 `环境变量 > .env > config.json > 默认值`，且**逐项**合并。残留的 `LLM_MODEL` / `LLM_API_KEY` 会让文件里的配置失效。排查看启动横幅的「配置」行 |
| C2 | 程序**不接受命令行参数** | 带了参数只提示一句然后忽略（`main.py:707`）。一次性提问用 `echo "问题" | python main.py` |
| C3 | `/exit` 会**连带停掉所有渠道** | 网关规则是「任一渠道正常返回 → 整体收工」。想长期挂机就别敲 `/exit`，用 Ctrl+C 同样是优雅退出 |
| C4 | 渠道**抛异常**只摘掉它自己，**正常返回**才整体收工 | 两条规则不一样，别搞混 |
| C5 | `exec` 的 stdin 接的是空设备 | 需要交互输入的命令（`npm init`、不带 `-m` 的 `git commit`）会**直接结束**，不是卡住 |
| C6 | `exec` 超时 60 秒 | 装依赖、跑大编译很容易超 |
| C7 | `web_fetch` **不走系统代理** | `trust_env=False` 是刻意的（代理会让内网判定失真）。需要代理的环境这个工具会失败 |
| C8 | 重定向到文件时横幅很窄 | `shutil.get_terminal_size()` 探不到真实宽度，退回 **72 列**（HANDOVER 写的是 80，实现是 72）。是已知行为，不是 bug |
| C9 | 会话键里的冒号换成下划线**不可逆** | `cli:local` → `cli_local.jsonl`；`cli:local` 与 `cli_local` 会落到同一个文件。`list_sessions` 反向还原会把真实下划线也换成冒号 |
| C10 | QQ 群聊**必须 @ 机器人才推送** | QQ 平台规则，不是代码问题。代码会自动剥 `<@!号码>` 前缀 |
| C11 | QQ 单聊用 **C2CMessage** 而非 DirectMessage | 后者是**频道私信**，需要 `direct_message` intent，用错会表现为「收不到单聊消息」。单聊 openid 与群内 member_openid **不是同一套标识**，无法自动关联同一个人 |
| C12 | 不用 `bot.run()`，用 `async with` + `await start()` | `run()` 内部是 `loop.run_until_complete()`，在已跑着的循环里调用会报「循环已在运行」 |
| C13 | `npx` 拉起的 MCP server 首次要下载包 | 实测约 8 秒，30 秒超时内一般能完成；重跑一次通常会命中缓存 |
| C14 | **MCP server 的 stdout 是协议通道** | 自己写 server 时绝不能往 stdout 打日志（会破坏 JSON-RPC），要打就写 stderr |
| C15 | **不要用 emoji 打日志** | Windows 默认代码页是 GBK，会让进程直接崩掉；用 `[MCP]` / `[!]` 这类 ASCII 前缀 |
| C16 | MCP 子进程可能变孤儿 | 主进程被强杀（`taskkill /F`）时 MCP 子进程不会自动退出；正常退出（含 Ctrl+C）不会 |
| C17 | 会话文件会一直变大 | 没有自动清理；`/clear` 或直接删文件 |
| C18 | 记忆文件位置与压缩归档位置**不是同一个目录** | `MEMORY.md` 在 `workspace/memory/`，`HISTORY.md` 在 `workspace/workspace/memory/`（见 §6.5） |

---


## 8. 注意事项与接手重点

### 8.1 修改前必读的「契约点」（写错一处就会出隐蔽 bug）

| 契约 | 内容 | 位置 |
|---|---|---|
| **渠道两条硬约束** | ① `name` 全局唯一且固定（既是路由键，又是 session_key 前缀）；② `start()` 占住自己的协程，返回即表示已退出 | `channels/base.py` 模块文档 |
| **总线单消费者** | 渠道**绝不可以**在自己的 `start()` 里调 `bus.consume_outbound()` —— asyncio.Queue 是单消费者的，两个消费者会**随机丢回复**，且时机不同表现不同（最难排查的那类 bug） | `gateway.py` 模块文档 |
| **事件顺序保证** | 5 条（见 §5.7）；sink **不得重排、不得并发发送**（不要在 emit 里 `create_task`）；emit 抛异常是允许的（AgentLoop 会兜住） | `agent/events.py` |
| **工具实现约定** | 返回值统一 `str`；业务失败**返回可读错误文本而非抛异常**；不吞编程错误（TypeError 等让它冒泡）；实例必须**无状态**（可能被并发调用） | `agent/tools/base.py` |
| **Provider 异常边界** | 调用层面失败（网络 / 鉴权 / 限流）→ 转 `finish_reason="error"`；模型正常返回但内容异常（arguments 是坏 JSON）→ **不抛**，置空字典 | `providers/base.py` |
| **MCP 生命周期铁律** | ① 必须手动 `__aenter__`；② `__aexit__` 必须在**同一个 asyncio 任务**里；③ server 之间**逆序**断开；④ server 内部 session 先、stdio 后；⑤ 不能 gather 并发退；⑥ 不能加 `wait_for` 超时 | `agent/tools/mcp.py` 模块文档 |
| **失败降级约定** | 工具与渠道的失败都应变成可读文本交回模型或记日志，**不抛异常** | 全项目 |
| **stdout / stderr 分流** | **stdout 只有模型回答**（`AI > `），思考 / 工具 / 错误 / 横幅全走 stderr（⚠ 实际有两处泄漏，见 B11） | `main.py::TerminalSink` |

### 8.2 动手前必须知道的 5 件事

1. **没有依赖声明**：装依赖前先读 README 的「安装依赖」段；`openai` / `dotenv` / `yaml` 是硬依赖（顶层导入）。
2. **改路径相关代码前先看 §6.5 那张表**：三套「相对谁」的规则并存，最容易改错。
3. **改工具循环 / 压缩相关代码，务必保证消息结构合法**：带 tool_calls 的 assistant 消息必须与 tool 结果成组出现，否则接口直接 400，而且**该会话此后每轮都失败**。
4. **不要把长任务阻塞在入站循环里**：`_process_inbound` 是串行的，轮内 30 秒 = 所有人等 30 秒。
5. **没有测试兜底**：任何改动都要手工验证（见 §8.4）。

### 8.3 接手建议顺序（按性价比排序）

| 顺序 | 做什么 | 为什么先做 |
|---|---|---|
| 1 | 修 **B5**（技能目录未加载） | 一行配置即可，但影响所有「技能」相关设计 |
| 2 | 决策 **B1 / B2**（命令黑名单绕过、子进程继承密钥） | 安全相关，属设计层面问题，需尽早定调 |
| 3 | 修 **B6 / B7**（MCP 路径与超时） | 换机器即挂 / 卡死整个 agent |
| 4 | 补 **B20**（`requirements.txt`） | 现在装依赖全靠读文档 |
| 5 | 补测试（配置合并、沙箱逃逸、会话读写往返） | 这三处回归症状最隐蔽，一写测试就能暴露 B7 / B12 / B15 |
| 6 | 文档纠错（B16-B18、B25、B26、B30） | 低成本高收益，避免下一个人被误导 |
| 7 | 清死代码与残留（B22、B23、B27、B28） | 降低阅读噪声 |
| 8 | 统一路径语义（B29）／接飞书、Web 渠道 | 结构性改进，可放最后 |

### 8.4 手工验证清单（没有测试套件时的替代方案）

```bash
# 1. 语法与导入
python -m compileall -q .
python -c "import main; print('OK')"

# 2. 配置是否读对（看横幅的「配置」行 + 工具数 + 技能数）
echo "/tools" | python main.py

# 3. 真跑一轮对话（需要模型密钥）
echo "1+1等于几" | python main.py

# 4. MCP 是否连上（看启动日志）
#    期待：[MCP] 已连接 poetry：3 个工具 / [MCP] 连接结束：1/1 个 server 可用

# 5. 纯净回答是否干净（B11 的验证）
python main.py > answer.txt    # 检查 answer.txt 里有没有提示符混进去
```

### 8.5 安全提醒（真实评估，非模板话术）

| 风险 | 现状 | 说明 |
|---|---|---|
| **密钥泄漏** | `config.json` **已 gitignore 且实测未被 git 跟踪** ✔ | 但里面是**真实可用**的 DeepSeek key、博查 key、百炼 key、QQ AppSecret。分享仓库时务必确认只发 `config.example.json` |
| **命令执行是真实执行的** | 9 类拦截 + 60s 超时，但**规则是正则黑名单** | 一定存在绕过写法（见 B1 / B2）；不要把 `exec` 开放给不可信输入 |
| **agent 会改文件** | 沙箱限定在 `workspace/` 内 ✔ | `identity.md` 的「动手前先确认」是**提示词约束**，不是沙箱。重要目录请自行备份 |
| **`.env` / `config.json` 保护** | 提示词约束 + 沙箱恰好挡住（因为 `workspace` 指向子目录） | `workspace` 改成当前目录时立刻失效（见 B4） |
| **SSRF 防护** | 四道闸（见 §6.2） | 做得比多数项目扎实，包含 DNS rebinding 与 302 绕过；但存在静默降级（B3） |
| **提示词注入** | 网页内容 / 工具结果会进入模型上下文 | `web_fetch` 抓到的页面可以内含指令诱导模型调工具 —— 目前**无任何防护** |
| **子智能体是临时执行者** | 子 agent 拿到除 spawn 外的全部工具（含 `exec`、`write_file`） | 它只有一句话 system prompt，若 task 夹带注入内容，就是一个不受控执行者 |

---

## 9. 附录

### 9.1 操作速查

| 我要… | 命令 / 位置 |
|---|---|
| 启动对话 | `python main.py` |
| 一次性提问 | `echo "问题" | python main.py` |
| 看有哪些工具 | 对话里 `/tools` |
| 清空当前会话 | `/clear`（内存 + 磁盘） |
| 退出 | `/exit` 或 Ctrl+C（退出码 130） |
| 看配置从哪来 | 启动横幅的「配置」那一行 |
| 换模型 | 改 `config.json` 的 `models.main` |
| 加模型别名（给子智能体用） | `config.json` 的 `models` 里加键值，如 `"cheap": "qwen-turbo"` |
| 加一个 MCP Server | `config.json` 的 `mcp_servers` 加一项 |
| 加一个工具 | 照 `agent/tools/base.py` 写类 → `main.py::_build_registry` 注册 |
| 加一个渠道 | 照 `channels/base.py` 写类 → `main.py::_assemble_and_serve` 的 channels 列表 append |
| 看 AI 纯净回答 | `python main.py > answer.txt` |
| 看思考过程 / 工具调用 | `python main.py 2> think.txt` |
| 找会话文件 | `workspace/workspace/sessions/` 下的 jsonl |
| 找长期记忆 | `workspace/memory/MEMORY.md` |
| 看被压缩掉的旧对话 | `workspace/workspace/memory/HISTORY.md` |

### 9.2 git 提交脉络（35 个提交，按时间）

```text
be74604 创建基础框架
6b39166 工具基类 base.py + 注册类 registry.py
29b8912 Delete .env                          ← 早期就把密钥从版本库摘掉
5e33d25 读文件 / 写文件 / 列目录
67571cf 大模型基类 base.py
794ba88 通用 OpenAI 接口
a266253 拼接人设与消息列表
fedb48c 创建大模型人设
0309d3f 智能体主循环 + 环境变量配置
2fc7190 补充人设提示词
e35e4b3 创建程序入口
53b9378 移除入口冗余代码
1b7fbc1 exec 工具（shell.py）
4972e61 流式输出 + 思考/工具内容输出，留存前端接口
2692740 补充人设
3df7d20 联网搜索工具 web_search.py
bad36f5 联网搜索改为博查 API
c7c1dce 页面抓取工具 web_fetch.py
3be79dd 技能加载 skills.py + SKILL.md 示例
ff472de SkillsLoader 接入 ContextBuilder
e9096c6 历史对话存储 manager.py
24455f9 长期记忆 + 工作区
52a5a42 项目改名 DeepsClaw
f325c82 上下文压缩（memory.py）
1de07d4 压缩接入主循环
3663de3 多渠道网关 + QQ 渠道连通
2cd36c9 QQ 机器人配置接入入口
7415722 子智能体 spawn.py + 阿里云百炼 API
4a68cef 补全主智能体人设使其自主调用子智能体
203f331 补全人设
a640398 古诗词查询 MCP Server
61fcad6 MCP 服务器配置接入入口启动
78e5b34 配置从 .env 迁移到 config.json
863afdc 完成 README 文档
64bccbd 配置文件文本修正
```

**读这条脉络能看出演进方向**：先骨架（工具 + Provider + 循环）→ 再能力（exec、联网、抓取、技能）→ 再状态（会话、记忆、压缩）→ 再扩展（多渠道、子智能体、MCP）→ 最后收敛配置与文档。

### 9.3 相关文档与图表

| 文件 | 定位 |
|---|---|
| `README.md` | 给人看的项目介绍（上手、配置、FAQ） |
| `HANDOVER.md` | 面向接手者的交接文档（功能 ↔ 文件映射表、坑点清单） |
| `ARCHITECTURE.md`（本文） | 代码结构 + 架构 + 流程 + 技术栈 + 问题清单 |
| `DeepsClaw-框架图.drawio` | 框架图：系统架构 + 核心技术栈（2 页） |
| `DeepsClaw-业务流程图.drawio` | 业务流程图：消息时序 / 工具循环与压缩 / MCP 与子智能体 / 启动退出（4 页） |
| `DeepsClaw-技术栈.drawio` | 核心技术栈（1 页，单独成文件） |
| `mcp_servers/README.md` | MCP 接入说明与排查表 |
| `identity.md` | agent 人设（**运行时真正生效的提示词**） |

---

> 本文档的所有结论均来自**源码通读 + 本机实测**；标注「实测」的条目都在当前环境复现过。
> 涉及行号的引用以本文档生成时的代码状态为准（`64bccbd`）。