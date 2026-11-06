# DeepsClaw agent/ 五个核心模块实现精读

## 0. 总览

| 文件 | 行数 | 一句话职责 |
|---|---|---|
| agent/loop.py | 577 | 把 provider + ToolRegistry + ContextBuilder 串成"模型→工具→模型"主循环，唯一入口 `AgentLoop.run()`，对内做事件发射、防爆熔断、历史压缩接入与落盘 |
| agent/context.py | 205 | 每轮按"人设→时间→工作区→长期记忆→技能摘要"拼 System Prompt，并组装 `[system]+history+[user]` |
| agent/events.py | 218 | 定义 7 种输出事件与其 JSON 契约、5 个 `turn_end.reason` 常量、`OutputSink` 抽象与 `NullSink`/`CallbackSink` |
| agent/memory.py | 282 | 历史压缩器：超 token 预算时把"中间旧消息"摘要成一条 system 消息，保留首条 System Prompt + 最近 KEEP_RECENT 条，并归档 HISTORY.md |
| agent/skills.py | 237 | 技能发现（扫子目录 + 解析 SKILL.md frontmatter）与按需取正文（"目录进 prompt、正文用 read_file 读"） |

---

## 1. 逐文件清单

### 1.1 agent/loop.py

| 名称 | 作用 | 行号 |
|---|---|---|
| 模块文档 | 两条 ASCII 流程图 + 说明两类"防爆"措施目的不同 | 1-36 |
| `TOOL_RESULT_PREVIEW_LIMIT = 2000` | 工具结果**事件预览**上限（模型仍拿全文） | 71 |
| `class LoopCheck(Enum)` | OK / WARN / BREAK 三态判定 | 74-84 |
| `class AgentLoop` | 有状态会话主体（一个会话一个实例） | 87-515 |
| `CIRCUIT_BREAK_THRESHOLD = 20` | 同一"整轮调用组合"重复 20 次即熔断 | 102 |
| `WARN_THRESHOLD = 10` | 重复 10 次开始给模型提示 | 104 |
| `LOOP_WINDOW_SIZE = 30` | 滑动窗口 `deque(maxlen=30)` | 106 |
| `__init__` | 装配 provider/registry/context；history 三分支（显式传入按引用持有 → 从 session_manager 恢复并 `_repair_history` → 空）；初始化 `_step=0`、`_tool_window` | 108-165（history 150-159，`_sink` 160，`_step` 163，窗口 165） |
| `run(user_input="") -> str` | 一轮完整交互；返回最终文本，异常分支 re-raise | 167-253 |
| `_compact_for_model(messages)` | 送给模型前压缩；三条约束写在 docstring | 255-286 |
| `_emit(event)` | 唯一投递口，**永不抛异常**（只记 warning） | 288-298 |
| `_on_delta(delta)` | reasoning→`ThinkingDelta`、content→`ContentDelta`（各发一条） | 300-309 |
| `_run_tool_calls(response, messages, turn)` | 执行本条 assistant 的全部工具调用；熔断时返回说明文本，正常返回 None | 311-396 |
| `_build_assistant_message(response)` | LLMResponse → 可回传的 assistant 消息（arguments 反序列化回字符串；reasoning_content 放顶层） | 398-435 |
| `_check_tool_loop(tool_calls)` | 计算"整轮调用组合"指纹并计数，返回 `(LoopCheck, repeats, label)` | 437-475 |
| `_persist(message)` | 单条写会话文件（无 session_manager 时空操作） | 477-480 |
| `_remember(turn, message)` | turn.append + `_persist`（"内存里有的磁盘上也有"的唯一实现点） | 482-497 |
| `clear_history()` | 内存 + 磁盘一起清 | 499-507 |
| `_save_to_history(messages)` | 本轮消息整批 extend 进 `self.history` | 509-515 |
| `_repair_history(history)` | 模块级函数：丢弃末尾"有 tool_calls 无结果"的残段 | 518-550 |
| `_tool_result_event(tool_call, result, step)` | 包装结果事件并按 2000 字符截断预览 | 553-577 |

### 1.2 agent/context.py

| 名称 | 作用 | 行号 |
|---|---|---|
| 模块文档 | 五块上下文的来源/缺失行为表，说明为何独立成层 | 1-17 |
| `MAX_MEMORY_CHARS = 4000` | 注入 prompt 的记忆字符上限 | 27 |
| `DEFAULT_IDENTITY` | 内置人设兜底（含"严禁读取 .env 隐私文件"条款） | 31-36 |
| `class ContextBuilder` | 类文档给出五块来源表格 | 39-57 |
| `__init__(workspace, identity_file="identity.md", skills_summary="", memory_file="")` | `workspace` 走 `os.path.realpath` 固化 | 59-81（realpath 78） |
| `_load_identity()` | 读 `{workspace}/{identity_file}`；FileNotFound/OSError/空白一律回落 DEFAULT_IDENTITY | 83-105 |
| `_load_memory()` | 读记忆文件；不存在返回 ""；超 4000 字符截断并附"可用 read_file 读全文"提示 | 107-137（截断 130-136） |
| `_resolve_memory_path()` | 绝对路径原样用，相对路径按 **workspace** 解析 | 139-147 |
| `build_system_prompt()` | 段落拼接：人设→`## 当前时间`（`%Y-%m-%d %H:%M (%A)`）→`## 工作区`→`## 长期记忆`→`## 可用技能` | 149-177（时间 162-163） |
| `build_messages(history=None, current_message="")` | `[system]+history+[user]`；`current_message` 为空**不追加** user 消息；返回新 list 不改入参 | 179-205 |

### 1.3 agent/events.py

| 名称 | 作用 | 行号 |
|---|---|---|
| 模块文档 | 事件契约样例、字段含义表、5 条顺序保证、两条约定 | 1-54 |
| `REASON_FINISHED/ERROR/LOOP_BREAK/MAX_STEPS/CANCELLED` | `finished/error/loop_break/max_steps/cancelled` | 62-71 |
| `class AgentEvent` | 基类；`type: ClassVar`、`ts: field(default_factory=time.time, init=False, compare=False)` | 74-97 |
| `to_dict()` | `type` 放第一个键，其后按 `fields(self)` 展开（含 ts） | 89-97 |
| `ThinkingDelta` / `ContentDelta` | 思考/正文增量（text, step） | 100-115 |
| `ToolCallEvent` | id/name/arguments/step，执行前发出 | 118-126 |
| `ToolResultEvent` | result 是**预览**，带 truncated/length | 129-144 |
| `ErrorEvent` | 模型调用层失败（已归一） | 147-153 |
| `TurnEndEvent` | reply + reason，必为最后一条 | 156-167 |
| `OutputSink(ABC)` | 两条硬约定：不得并发发送；emit 抛异常允许 | 170-187 |
| `NullSink` | 空实现，作为默认 sink | 190-197 |
| `CallbackSink` | 把事件交给 async 回调；回调本身 await，不必再包后台任务 | 200-218 |

### 1.4 agent/memory.py

| 名称 | 作用 | 行号 |
|---|---|---|
| 模块文档 | 保留"第一条 + 最近"的理由；两处非教科书实现；chars÷2 估 token 的取舍 | 1-31 |
| `DEFAULT_TOKEN_BUDGET = 24000` | 默认 token 预算 | 45 |
| `KEEP_RECENT = 6` | 压缩后保留的最近消息条数 | 48 |
| `MIN_MESSAGES_TO_COMPRESS = KEEP_RECENT + 2`（=8） | 低于此条数不压缩 | 51 |
| `SUMMARY_PROMPT` | "3-5 句话概括…只输出摘要" | 54-57 |
| `SUMMARY_FAILED` | `（摘要生成失败，旧消息已丢弃）` | 61 |
| `SUMMARY_PREFIX = "[历史摘要]: "` | 摘要 system 消息前缀 | 65 |
| `class MemoryConsolidator` | 无状态压缩器；`history_file` 默认 `{workspace}/workspace/memory/HISTORY.md` | 68-97（默认路径 95-97） |
| `estimate_tokens(messages)` | 每条消息 `json.dumps(ensure_ascii=False)` 长度累加后 `//2`；序列化失败退 `len(str(m))` | 99-121 |
| `maybe_consolidate(messages)` | 未超预算/条数不足/旧段为空 → 原样返回；否则摘要 + 重组 `[first, summary, *tail]` + 归档 | 123-173 |
| `_summarize(messages)` | 跳过带 tool_calls/tool_call_id 的消息；全工具则免调用返回占位；异常/error finish/空内容 → SUMMARY_FAILED | 175-224 |
| `_save_to_history(summary, original_count)` | 追加写 HISTORY.md（时间戳、"压缩了 N 条旧消息"、`---` 分隔） | 226-248 |
| `_align_tail_start(messages, start)` | 模块级：把尾部起点向前吸附，绝不切开一组工具调用 | 251-282 |

### 1.5 agent/skills.py

| 名称 | 作用 | 行号 |
|---|---|---|
| 模块文档 | "目录 + 按需加载"拆法的理由、目录结构、frontmatter 格式、失败一律不抛 | 1-40 |
| `SKILL_FILENAME = "SKILL.md"` | 固定定义文件名 | 52 |
| `_FRONTMATTER_FENCE` | `re.compile(r"^---[ \t]*\r?$", re.MULTILINE)`，整行匹配（容忍尾随空白与 \r） | 56 |
| `_GUIDE` | 引导语，明确"正文要自己 read_file 读"，且**不自带**标题 | 62-65 |
| `class SkillsLoader` | 构造时 `os.path.abspath(skills_dir)` 固化；不做缓存 | 68-237（abspath 86） |
| `_parse_frontmatter(content)` | 返回 `(meta, body)`；<2 个 fence / 首个不在 0 位置 / YAML 坏 / 非 dict → `({}, content)`；空 frontmatter（None）→ `({}, body)` | 88-131 |
| `_discover()` | `sorted(os.listdir)` 扫子目录，逐个读 frontmatter，产出 name/description/path/meta；单文件失败 continue | 133-178 |
| `build_skills_summary()` | `_GUIDE` + 空行 + 每行 `- {name} ({path})：{description}`；无技能返回 "" | 180-198 |
| `load_skill(name)` | 按**目录名**拼路径读正文并去 frontmatter；不存在/失败返回 None | 200-225 |
| `list_skills()` | 调试用，返回 `[{name, description, path}]` | 227-237 |

---

## 2. `run()` 一轮的完整执行流程（含出口/兜底/熔断/落盘/事件点）

```
run(user_input)  loop.py:167
├─ 181-183 重置：self._tool_window.clear()；self._step = 0
│           （注释：防爆只针对"本次任务"，上一轮不该算进来）
├─ 185     messages = context.build_messages(self.history, user_input)
│           —— System Prompt 每轮只构建 1 次（不是每步重建）
├─ 186-188 turn = []；若 user_input 非空 → _remember(turn, user_msg)
│           —— 【落盘点 1】user 消息立刻 append 到 JSONL（_persist 497）
├─ 190-191 reply="" / reason=REASON_FINISHED
└─ try: 192
   └─ for _ in range(self.max_steps)  193        ← 默认 50
      ├─ 194-199 response = provider.chat(await _compact_for_model(messages),
      │            tools=registry.get_definitions(), model=self.model, on_delta=_on_delta)
      │   ├─ 【事件】流式增量在 provider 内部回调 _on_delta（300-309）：
      │   │        reasoning→ThinkingDelta(step=self._step)；content→ContentDelta(step)
      │   └─ _compact_for_model（255-286）：consolidator 为 None 直接返回原表；
      │            否则 maybe_consolidate；若 compacted is messages 或
      │            len(compacted) >= len(messages) 就仍用原表（"必须真的变短才采用"）
      │            ——**压缩结果不回写 messages**，只影响本次请求
      ├─ 201-207 【出口 A｜模型层错误】finish_reason == "error"
      │        logger.error；reason=REASON_ERROR；reply = content or "错误：模型调用失败"
      │        【事件】ErrorEvent(message=reply, step)（206）→ return reply
      │        （注释 202：不写历史，避免"用户提问 + 无回复"污染上下文）
      ├─ 209-211 assistant_message = _build_assistant_message(response)
      │        messages.append(...) + _remember(turn, ...)   —— 【落盘点 2】
      ├─ 213-216 【出口 B｜正常收敛】not response.has_tool_calls
      │        _save_to_history(turn)（整批进 self.history）→ reply = content or "" → return
      ├─ 218-222 aborted = await _run_tool_calls(response, messages, turn)
      │        └─ 返回非 None 即熔断：【出口 C】reason=REASON_LOOP_BREAK；reply=aborted → return
      └─ 225     self._step += 1（工具跑完要再问一次模型，算新一步）
   ├─ 227-239 【出口 D｜步数耗尽】notice = "已达到单轮最大步数（N 步）仍未完成任务…"
   │        turn.append(assistant notice)；self._persist(turn[-1])【落盘点 5】；
   │        _save_to_history(turn)；logger.warning；reason=REASON_MAX_STEPS → return notice
   ├─ 240-243 【隐形出口 E｜取消】except asyncio.CancelledError：reason=REASON_CANCELLED；raise
   │        （注释 241：取消不写历史，这是 main.py 敢承诺"中途取消不留半截消息"的原因）
   ├─ 244-249 【隐形出口 F｜任意异常】except Exception：reason=REASON_ERROR；raise
   │        （注释 245-247：不额外发 ErrorEvent，调用方手里有异常对象更全）
   └─ finally: 250-253 await self._emit(TurnEndEvent(reply=reply, reason=reason))
            —— 唯一收尾点，覆盖 4 个显式出口 + 取消/异常 2 条隐形出口
```

### 2.1 `_run_tool_calls` 内部（311-396）

1. **327** `verdict, repeats, label = self._check_tool_loop(response.tool_calls)`
   - 指纹算法（460-466）：每个调用 `f"{name}({json.dumps(arguments, sort_keys=True, ensure_ascii=False)})"` → 一轮内 `sorted` → `'|'.join` → `self._tool_window.append(signature)` → `repeats = self._tool_window.count(signature)`
   - 判定（471-475）：repeats >= 20 → BREAK；>= 10 → WARN；否则 OK。`label` 只取去重后的工具名（469）
2. **BREAK 分支 329-360**：
   - 对**每个** tool_call 发 `ToolCallEvent`（338-345），但**不发 ToolResultEvent**（335-337 注释：占位是给接口的结构补丁，不是真结果）
   - 往 `messages` 与 `turn` 各补一条 `{"role":"tool","tool_call_id":...,"content":"[未执行] 本次调用因检测到重复循环被中止。"}`（346-352，经 `_remember` → 【落盘点 3】）
   - notice（354-357）→ `self._save_to_history([*turn, {assistant notice}])`（359，**未落盘**）→ return notice
3. **WARN 分支 362-368**：构造 `[系统提示] 你已经原样重复了 N 轮相同的工具调用…`，不单独发事件
4. **正常分支 370-394**（逐调用，调用与结果**交错**发出）：
   - `ToolCallEvent`（373-380）→ `registry.execute(name, arguments)`（381）→ index==0 时把 hint 追加到 result（382-384，只挂第一个结果）→ `_emit(_tool_result_event(...))`（386）→ `messages.append(tool_msg)` + `_remember`（388-394，【落盘点 4】）
5. **396** return None

### 2.2 sink 事件发射点汇总

| 事件 | 位置 | 时机 |
|---|---|---|
| ThinkingDelta / ContentDelta | 307 / 309 | provider 流式回调期间（带当步 step） |
| ToolCallEvent | 338（熔断占位）/ 373（正常） | 执行**之前** |
| ToolResultEvent | 386 | 每次执行之后（经 `_tool_result_event` 截断） |
| ErrorEvent | 206 | 仅 `finish_reason=="error"` 这一条已归一失败路径 |
| TurnEndEvent | 253（finally） | 恰好一次、必为最后一条 |

### 2.3 落盘（JSONL）时机汇总

`_persist`（477-480）只被 `_remember`（497）与 max_steps 分支（234）调用：user 消息（188）、每条 assistant 消息（211）、每个工具结果/熔断占位（352/394）、max_steps 说明（234）。**熔断说明（359）与 error/取消路径的 assistant 内容都不落盘**。

---

## 3. 魔法数字 / 常量全表

| 常量 | 值 | 位置 |
|---|---|---|
| TOOL_RESULT_PREVIEW_LIMIT | 2000（字符，事件预览；模型拿全文） | loop.py:71 |
| AgentLoop.CIRCUIT_BREAK_THRESHOLD | 20（轮） | loop.py:102 |
| AgentLoop.WARN_THRESHOLD | 10（轮） | loop.py:104 |
| AgentLoop.LOOP_WINDOW_SIZE | 30（deque maxlen） | loop.py:106, 165 |
| max_steps 默认 | 50（config.py:390 的 AGENT_MAX_STEPS / max_iterations 同值；子 agent SUBAGENT_MAX_STEPS = 20，spawn.py:69） | loop.py:114, 193 |
| session_key 默认 | "cli:direct" | loop.py:118 |
| _step 初值 | 0 | loop.py:163 |
| reason 取值 | finished / error / loop_break / max_steps / cancelled | events.py:62-71 |
| MAX_MEMORY_CHARS | 4000 | context.py:27 |
| identity_file 默认 / 时间格式 | "identity.md" / "%Y-%m-%d %H:%M (%A)" | context.py:62, 162 |
| DEFAULT_TOKEN_BUDGET | 24000（config.py:43 的 memory.token_budget 默认 24000；AGENT_TOKEN_BUDGET=0 → None → 关闭压缩） | memory.py:45 |
| KEEP_RECENT | 6 | memory.py:48 |
| MIN_MESSAGES_TO_COMPRESS | 8 | memory.py:51 |
| token 估算系数 | 字符数 // 2 | memory.py:121 |
| 默认 HISTORY.md | {workspace}/workspace/memory/HISTORY.md | memory.py:95-97 |
| SKILL_FILENAME | "SKILL.md" | skills.py:52 |
| description / name 兜底 | "无描述" / 回退目录名 | skills.py:171-172 |

---

## 4. 代码中记录的设计取舍与"踩过的坑"（原文要点）

**loop.py**
- 两类防爆目的不同、缺一不可：`_check_tool_loop` 治"完全相同的调用转圈"，`max_steps` 治"工具各不相同但任务没边"（23-29）。
- 按**轮**计数而非按次计数：窗口 30 时，每轮固定 3 个调用同一指纹最多凑 10 次，够不到 20 的熔断线；每轮 4 个连 10 的警告线都够不着；而"每轮读一个文件、改一处、再读"恰是最常见的死循环形态（446-450）。
- 参数必须 sort_keys 后序列化，否则模型换个键序就能绕过检测（443）。
- 熔断后**必须补占位 tool 结果**：assistant 消息已进 messages，每个 tool_call 都要有对应 tool 结果，否则下次请求被接口以结构非法拒（400）——所以整轮不执行也要逐个补，不能直接跳出（331-333）。
- 熔断时只发 ToolCallEvent 不发 ToolResultEvent：占位是给接口的结构补丁，不是真结果（335-337）。
- WARN 提示只挂在整轮第一个结果上，多个调用重复挂同样文字是噪声（382）。
- reasoning_content 必须在 assistant 消息**顶层**：DeepSeek 思考模式要求随 tool_calls 一起回传，位置放错直接返 400；无工具调用时**不带** tool_calls 字段而非空列表，因为部分服务端对空数组处理不一致（405-410）。
- 压缩三条硬约束（259-270）：①只压缩"发出去的那份"，就地替换会让账本与真实发生的事脱节（工具结果靠 tool_call_id 配对）；②压缩结果必须以 user 开头且尾部对齐，否则新 assistant 会接在"找不到发起者的工具结果"后面被 400；③必须真的变短才采用，且**不能**反过来要求"不得短于本轮起点条数"——历史越长起点越高，那等于让压缩永不生效。
- finish_reason="error" 时不写历史：模型什么都没说过，记进去会凭空多出"用户提问+无回复"，重试时还可能出现连续两条相同提问（96-98）。
- `_emit` 永不抛异常：sink 是外接的（未来是 WebSocket），它崩了不能带崩 agent 任务（288-298）。
- 取消不写历史，这正是 main.py 敢承诺"中途取消不留半截消息"的原因（241）。
- 异常路径不额外发 ErrorEvent：调用方手里有异常对象，比一段字符串全（245-247）。
- finally 统一收尾的理由：4 个显式出口 + 取消/异常 2 条隐形出口，逐个 return 前手写 emit 一定会漏（251-252）。
- `_repair_history` 的理由：残缺历史原样回传会 400，而且**这个会话从此再也打不开**，比丢一轮对话严重得多；"历史上会出现这种情况的唯一入口就是崩溃"，所以从末尾往前丢就够（518-550）。
- 截断只针对订阅者：模型拿的始终是全文，read_file 读几 MB 文件原样推到前端会打死页面（553-558）。
- `turn` 与 session 文件用途不同：turn 是本轮新增、结束时整批进 history（熔断/出错时不进），文件则从第一条起落盘（489-490）。
- clear_history 必须两处都清，否则"用户按这个命令时的预期"被破坏（499-503）。

**events.py**
- ts 用 init=False：一让子类字段不必迁就它，二顺手避免子类必填字段触发 "non-default argument follows default argument"（78-81）。
- step 是前端**开新块**的依据：一次 run() 里模型可能被调用多次，只按类型聚合会把多段思考糊成一团、多轮回答粘成同一气泡（23-25）。
- ts 只用于展示（如首字延迟），**不要用于排序**（26-27）。
- turn_end 就是"会话空闲"信号，前端"收到它之前禁用发送键"由契约直接提供（40-43）。
- turn_end.reason == "error" 表示本 turn 已渲染的流式内容作废：流式失败可能发生在收到一半时，推出去的字收不回来（44-46）。
- sink 两条硬约束：不得在 emit 里 create_task 转发（会乱序），"慢就让它慢，AgentLoop 是 inline await，背压天然成立"（176-181）。
- reply 兜底的理由：错误提示、熔断说明不走流式增量，前端只拼 content 拼不出来（161-163）。

**context.py**
- 前四块每轮重读、不做缓存：进度都在磁盘上，用户改完 identity.md 下一轮就生效，"调试 agent 时这个即时性比省下的几次小文件读取值钱得多"（54-56）。
- 技能摘要例外，由调用方启动时加载一次（10-11、172-173）。
- 段落顺序理由：时间/工作区属"环境事实"，读完角色设定再看环境更顺；技能排最后，因为它是"额外能力清单"（151-155）。
- %A 输出英文星期，因为 Python 默认不设置 LC_TIME（157-158）。
- current_message 为空时不追加 user 消息，供"历史里已有工具结果、只等模型接着想"的续跑场景使用；否则会多出一条空消息被接口拒绝（191-193）。
- 记忆截断保留开头，理由是记忆每轮都带、封顶防止把上下文挤干（114-116）。

**memory.py**
- 保留"第一条 + 最近 KEEP_RECENT"的形状：第一条 System Prompt 含人设/工作区/技能/记忆，丢了 agent 就失忆；最近对话是当前任务上下文；中间旧消息价值已被后续轮次消化过一遍（8-12）。
- 两处与"教科书实现"不同，都是踩过才知道疼的：①结构安全——切入位置不能落在"assistant 带 tool_calls"和它对应 tool 结果之间，否则剩下的 tool 消息找不到发起者，接口 400，所以尾部要向前吸附到组起点；②压缩结果也要落盘到 workspace/memory/HISTORY.md，"日后想追查模型当时为什么这么说，这是唯一线索"（14-21）。
- token 估算用"字符数÷2"，图的是够快够保守；精确分词要多跑一遍；估算偏大只会早压缩不会漏压；中文被高估（一个汉字往往不到 0.5 token）属于有意保守（23-25）。
- ensure_ascii=False：否则中文历史被高估到离谱、每轮都触发压缩（114-115）。
- 消息里混进不可序列化对象时按 str() 粗算，估算偏一点无所谓，压缩判断不该因一条脏数据停摆（118-119）。
- 摘要时**整条跳过**工具调用与工具结果：又长又偏过程，塞进去既费钱又把结论淹掉（178-179）。
- 只有 tool_calls 没有正文的 assistant 消息同样没有摘要价值（194）。
- provider 层把失败归一成 finish_reason="error" 而不抛，所以这里必须显式判它，否则"错误：模型调用失败"会被当成摘要写进历史（214-215）。
- 归档用追加而非覆盖：覆盖等于把被丢弃的旧消息彻底抹去；写失败只记日志，压缩本身已成功不该因归档失败回滚（229-230）。
- `_align_tail_start`："向前多留几条不会出错，向后切错一条就出事"（259）。
- 压缩是"顺手做的保养"，它失败不该让正在跑的一轮对话崩掉（185-186）。
- 单条消息过长（如刚 read_file 一个大文件）时压无可压，只能如实告知（139-140）。

**skills.py**
- "目录 + 按需加载"是刻意的：一个项目攒十几个技能全塞 System Prompt 就是几千 token 常驻开销，而这些内容多数轮次压根用不到（10-12）。
- frontmatter 用**整行**匹配而不是搜子串 "---"，否则正文里的 --- 或 ------ 会被误判成结束标记（54-55）。
- `_GUIDE` 里**不再自带** "可用技能：" 标题：System Prompt 已由 ContextBuilder 加 `## 可用技能` 段头，两处都写会连着出现两行标题（58-61）。
- 不做缓存：技能文件是用户手写的，改完下一轮就该生效，为省几次几 KB 读盘去维护失效策略不划算（71-73）。
- 元信息坏了不连带正文一起丢：YAML 写坏、解析出来不是 dict 都按"没有 frontmatter"处理（92-93、112-129）。
- YAML 写错是手写技能文件最常见的手误，只 warning 不抛（112-114）。
- errors="replace"：技能文件被别的编辑器写成 GBK 也不该让整份技能表消失，"乱码总好过没有这个技能"（159-160）。
- 单个技能读不动就跳过，不能让一个坏文件把整份技能表抹掉（164-165）。
- 不给引导语，模型会以为看到的摘要就是技能全部内容，然后照着摘要硬答（184-186）。
- `load_skill` 的 name 是**子目录名**不是 frontmatter 的 name 字段，两者不一致时以目录名为准（203-204）。
- 目录名排序保证输出一致，也便于前缀缓存命中（140）。

---

## 5. 潜在缺陷 / 隐患（附证据行号）

**H1｜熔断说明只进内存、不进磁盘 → 重启后"用户看到的"和"会话文件里的"不一致**
熔断分支只调 `_save_to_history([*turn, notice])`（loop.py:359），**没有** `_persist`；而 max_steps 分支是 `self._persist(turn[-1])` + `_save_to_history(turn)`（loop.py:233-235）。结果：熔断那一轮的收尾说明在 self.history 里有、JSONL 里没有，重启后模型不记得熔断发生过。

**H2｜error / 取消路径的 user 消息已经落盘，恰好制造了类注释想避免的"用户提问 + 无回复"**
run() 在 188 行就把 user 消息通过 `_remember → _persist` 写进 JSONL，而模型层错误分支（201-207）与取消分支（240-243）都不写 self.history。下一次启动 `session_manager.get_history`（session/manager.py:116-161）会把这条"孤儿提问"读回来——正是 loop.py:96-98 与 241 两处注释声称要避免的组合（"重试时还可能出现连续两条相同提问"）。

**H3｜`_repair_history` 在"残段之后又聊了几轮"时会误删正常对话（数据丢失）**
证据链：
- 残段的产生路径：工具执行处 `registry.execute` 未做 try/except（loop.py:381），抛异常时 assistant 消息**已经**在 211 行落盘，而 tool 结果永远不会写 → 文件里留下"assistant(tool_calls) 无结果"。
- 同进程继续对话时，新消息**追加**到同一个 JSONL（session/manager.py:110-112 的 `open(path, "a")`），残段被夹在中间。
- `_repair_history` 的 results 对全量历史按 tool_call_id 建索引（535-537），随后从末尾往前找第一个"未完成的组"（538-549），一旦命中就 `return history[:index]`——**把残段之后的全部合法轮次一并丢掉**。
- 且修复结果只赋给 self.history（loop.py:153），**不回写磁盘**，所以每次启动都会重放同一次截断。
- 函数 docstring 自称"历史上会出现这种情况的唯一入口就是崩溃，从末尾往前丢足够用"（525-526），但上面的路径（工具异常 + 同进程继续对话）并不满足这个前提。

**H4｜工具执行异常没有任何兜底，直接冒泡终止整轮**
loop.py:381 `result = await self.registry.execute(...)` 裸调用，无 try/except。一个工具抛异常会走 run() 的 244-249 → re-raise 给调用方（gateway.py:274 兜成 ERROR_REPLY），本轮已产生的其它工具结果/正文全部丢弃，并且按 H3 在磁盘留下残段。对比：`SpawnSubagentTool.execute` 自己把异常兜成文本返回（agent/tools/spawn.py:279-281），项目内两层策略不一致。

**H5｜压缩结果不复用导致"每步重复摘要"的成本放大**
`_compact_for_model`（loop.py:255-286）在 for 循环体内**每一步**都被调用（195），而它只用压缩结果作为本次请求参数，从**不回写** messages（279-286 直接 return compacted）。只要历史持续超预算，每一步都会再触发一次 maybe_consolidate → `_summarize` → 一次额外的非流式 `provider.chat`（memory.py:209），token 与延迟随步数线性放大（单轮最多 50 步）。

**H6｜取消时的 TurnEndEvent 可能发不出去，且 `_emit` 只兜 Exception**
`_emit` 捕获的是 Exception（loop.py:297），而 asyncio.CancelledError 自 Py3.8 起是 BaseException 子类（gateway.py:265-266 自己也写明了这点）。取消路径先在 242 行标 reason 再抛出，异常传播时执行 finally 中的 `await self._emit(TurnEndEvent(...))`（253）——任务已处于取消状态，这里的 await 仍可能立刻再抛 CancelledError，于是 turn_end（前端唯一的"会话空闲"信号）到不了 sink；同理 sink 抛 CancelledError/KeyboardInterrupt 不会被 297 兜住。

**H7｜AgentLoop 有状态且无重入保护，并发/复用会串台**
`_step`（163、182）与 `_tool_window`（165、182）都是实例状态，run() 全程就地修改，没有任何锁或"正在运行"标志。当前只靠网关"单协程串行消费"隐式保证（gateway.py:255-257、351-353 的注释明确承认"将来改成按会话并发时要补锁"），而 gateway.py:61 的注释已在讨论按 session_key 并发。此外 session_key 默认值是常量 "cli:direct"（loop.py:118）：任何用默认值构造的实例都会共用同一个 JSONL，历史串台（main.py:347-348 特意强调过"缺了它就会所有渠道的人共用一个历史"）。

**H8｜文档与常量漂移（token 预算）**
memory.py:45 实际是 `DEFAULT_TOKEN_BUDGET = 24000`，config.py:43 的 JSON 默认也是 24000，但同一文件的 docstring 两处仍写 "6000"：模块示例 `MemoryConsolidator(provider, workspace, token_budget=6000)`（29）与参数说明"默认 6000，是给中等窗口模型留足余量的保守值"（87）。按 87 行调参的人会拿到 4 倍预算。

**H9｜记忆截断保留"开头"与记忆的写入方式相冲突**
`_load_memory` 超限时保留前 4000 字符（context.py:130-136），而 MemoryTool 是**追加**式写入（agent/tools/memory.py，DEFAULT_MEMORY_PATH 46）。即记忆越长，被截掉的是**最新**的事实；prompt 里那句"可用 read_file 读取完整文件"依赖模型主动再读一次。同时截断按字符而非行边界，可能切在半行中间。

**H10｜HISTORY.md 路径会重复拼接 "workspace" 段**
`MemoryConsolidator.history_file` 默认 `os.path.join(workspace, "workspace", "memory", "HISTORY.md")`（memory.py:95-97），而 main.py:377 传的 workspace=str(cfg.workspace) 已经是项目根（config.py:369 + `_resolve_path` 438-446）。默认配置下恰好与 `DATA_MEMORY_FILE = "workspace/memory/MEMORY.md"`（config.py:92）同目录，看起来没问题；但一旦 AGENT_WORKSPACE 指向别处（如 D:/proj），归档会落到 D:/proj/workspace/memory/HISTORY.md 这个凭空多出来的嵌套目录里，与长期记忆文件 MEMORY.md 所在目录不再一致。

**H11｜`_summarize` 丢弃工具消息与压缩自身的结构约束存在张力（轻微）**
`_summarize` 跳过所有带 tool_calls/tool_call_id 的消息（memory.py:189-194），而 `_align_tail_start` 又刻意把整组工具调用留在尾部（251-282）。当"中间旧消息"里只有工具往返（没有自然语言）时，摘要退化成固定占位句（199），旧消息被整段删除而新生成的 system 消息几乎不携带信息——"压缩了但等于没压"；此时 len(compressed) < len(messages) 仍成立，loop.py:279 会照常采用它。

**H12｜技能摘要里的路径形态与 read_file 的边界校验存在耦合风险**
skills.py:86 把 skills_dir 固化成 abspath，`build_skills_summary`（197）直接把该绝对路径写进 prompt；而 `_discover` 的 docstring 声称"path 与 skills_dir 保持同一形态拼接，所以相对路径时 skills/weather/SKILL.md 能直接喂给 read_file"（135-138）。实际装配处传的是 `os.path.join(workspace, "skills")`（main.py:366），cfg.workspace 经 `_resolve_path` 已是绝对路径，所以模型看到的是绝对路径。它能否被 read_file 接受取决于 `ReadFileTool._resolve` 的 workspace 包含性检查（agent/tools/filesystem.py:57-77）：技能目录在 workspace 内则可用，不在（例如把技能放在 workspace 之外的独立目录）则模型拿到的路径会被判越界直接报错，而 docstring 的表述会让人以为这条路径是安全的。

---

## 6. 补充：跨模块契约（读这些文件时需要一起看的事实）

- provider 层把调用失败**归一成** `finish_reason="error"` 的响应而不抛异常（providers/openai_compat.py:25、434），所以 loop.py:201 与 memory.py:216 两处都必须显式判它。
- `SessionManager.save_message` 每条消息打 timestamp 字段落盘、`get_history` 读时摘掉（session/manager.py:50、96、156），因此 history 里的消息形状始终是干净的 OpenAI 格式。
- `SessionManager` **不做并发控制**（session/manager.py:23-24 明确写"真要多进程同时写同一个会话文件时得另加文件锁"），叠加 H7 的默认 session_key，多进程/多实例同时跑同一会话会互相追加同一个文件。
- 子 agent 用 `DummySessionManager` 与 `max_steps=SUBAGENT_MAX_STEPS(20)`（agent/tools/spawn.py:69、261-268），因此 loop.py 的落盘与压缩分支在子 agent 场景下分别是空操作与 None。