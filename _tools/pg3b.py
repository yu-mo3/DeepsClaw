from dxlib import Page, E, EB, EG, ER

def build():
    p = Page("03b-MCP与子智能体", "核心机制流程图（MCP 生命周期 / 子智能体派生）",
             "③ MCP 连接与断开  ④ 子智能体派生与上下文隔离。")
    p.head()
    p.A(40, 84, 760, 520, "zone", "③ MCP 连接生命周期（agent/tools/mcp.py）")
    c1 = p.A(80, 116, 220, 50, "proc", "connect_all(timeout=30s)", "逐个连、不并发")
    c2 = p.A(80, 184, 220, 50, "red", "stdio_client().__aenter__()", "★ 必须手动进入，不能用 with")
    c3 = p.A(80, 252, 220, 50, "red", "ClientSession().__aenter__()", "initialize() 握手")
    c4 = p.A(80, 320, 220, 50, "proc", "list_tools()", "包成 server__tool")
    c5 = p.A(80, 388, 220, 50, "green", "注册进 ToolRegistry", "agent 可直接调用")
    c6 = p.A(370, 116, 240, 50, "orange", "失败 / 超时 → 只跳过该 server", "[!] 已跳过（agent 照常启动）")
    c7 = p.A(370, 184, 240, 50, "red", "shutdown()：逆序断开", "★ 一个 server 正常、两个必炸")
    c8 = p.A(370, 252, 240, 66, "red", "server 内部：session 先、stdio 后", "★ 不得 gather 并发退", "★ 不得加 wait_for 超时")
    c9 = p.A(370, 336, 240, 50, "red", "connect_all 与 shutdown", "必须在同一个 asyncio 任务里")
    c10 = p.A(370, 404, 240, 50, "orange", "重名工具只跳过不 raise", "否则整个进程起不来")
    p.L(c1, c2); p.L(c2, c3); p.L(c3, c4); p.L(c4, c5)
    p.L(c1, c6, "任一步失败", ER); p.L(c9, c7, "", E); p.L(c7, c8); p.L(c8, c10, "", E)
    p.A(80, 456, 530, 130, "note", "为什么必须手动 __aenter__：stdio_client 与 ClientSession 内部各起一个 anyio 任务组；",
        "with 的语义是退出即拆任务组，而这里要的正好相反——连上之后那些后台任务必须一直活着，",
        "否则 initialize() 的请求没有人在读回音，直接死等。",
        "另：子进程 stdout 是协议通道，自己写 server 时不能往 stdout 打日志；日志不要用 emoji（Windows GBK 会崩）。")
    p.A(830, 84, 760, 520, "zone", "④ 子智能体派生（agent/tools/spawn.py）")
    s1 = p.A(1080, 116, 240, 44, "purple", "主 agent 调 spawn_subagent", "{task, model?}")
    s2 = p.A(1080, 178, 240, 58, "blue", "provider_factory(model)", "独立 Provider（默认百炼）", "extra_body 刻意不透传")
    s3 = p.A(1080, 254, 240, 50, "proc", "复制工具集", "跳过父辈的 spawn_subagent")
    s4 = p.A(1080, 322, 240, 50, "dec", "depth+1 < 2 ?", "注册新的 spawn（深度+1）")
    s5 = p.A(860, 400, 220, 62, "green", "SubagentContext", "只一句“任务专员”", "+ 当前时间 + 工作区")
    s6 = p.A(1090, 400, 240, 62, "green", "DummySessionManager", "不写磁盘 · 不挂 sink", "max_steps = 20")
    s7 = p.A(1340, 400, 220, 62, "fin", "await agent.run(task)", "结论交回主 agent", "finally: provider.aclose()")
    p.L(s1, s2); p.L(s2, s3); p.L(s3, s4); p.L(s4, s5, "到上限"); p.L(s4, s6, "未到上限"); p.L(s6, s7)
    p.A(860, 140, 200, 240, "note", "设计要点：", "· 不继承主对话历史", "  （task 就是它知道的全部）",
        "· 拿不到人设 / 长期记忆 / 技能摘要", "· 不写磁盘、不挂 sink", "· 子 agent 自己也能派活，", "  但最多再深一层", "",
        "⚠ 工具集含 exec / write_file，", "  是一个拥有完整 shell 的", "  临时执行者。")
    return p