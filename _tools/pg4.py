from dxlib import Page, E, EB, EG, ER

def build():
    p = Page("04-启动退出", "启动 / 运行 / 退出 生命周期流程图",
             "谁创建子任务，谁负责判断收工 —— 退出判断在 Gateway.run() 内部，不在 main。")
    p.head()
    p.A(40, 84, 580, 600, "zone", "启动阶段  main.py")
    l1 = p.A(70, 116, 520, 40, "proc", "_setup_console()  Windows 管道切 UTF-8")
    l2 = p.A(70, 168, 520, 40, "proc", "Config.from_env()  config.json + .env + 环境变量")
    l3 = p.A(70, 220, 520, 40, "red", "配置错误 → 打印并 return 1（只拦 ConfigError）")
    l4 = p.A(70, 272, 520, 40, "proc", "_setup_logging(level)  httpx / openai / botpy 降噪")
    l5 = p.A(70, 324, 520, 48, "orange", "asyncio.Runner().run(_run(cfg))", "连接池绑在创建它的事件循环上")
    l6 = p.A(70, 384, 520, 48, "red", "MCPClientManager.connect_all()  30s/个，失败只跳过")
    l7 = p.A(70, 444, 520, 48, "red", "try: _assemble_and_serve()", "finally: mcp_manager.shutdown()  ★ 同一任务 · 逆序断开")
    l8 = p.A(70, 504, 520, 48, "proc", "_build_registry → Provider → SessionManager → MessageBus",
             "→ channels → Gateway → 注入 tool_names / _banner / _clear_callback")
    l9 = p.A(70, 564, 520, 48, "green", "横幅交给渠道，由渠道在就绪时打印", "（早打会被启动日志顶掉提示符）")
    l10 = p.A(70, 624, 520, 48, "orange", "Gateway.run()：create_task 各渠道 + 两条消费循环")
    p.L(l1, l2); p.L(l2, l3, "失败", ER); p.L(l2, l4, "成功"); p.L(l4, l5); p.L(l5, l6)
    p.L(l6, l7); p.L(l7, l8); p.L(l8, l9); p.L(l9, l10)
    p.A(660, 84, 900, 600, "zone", "运行与退出阶段  gateway.py::run()")
    r1 = p.A(930, 116, 280, 48, "orange", "asyncio.wait(channel_tasks,", "FIRST_COMPLETED)")
    r2 = p.A(680, 200, 220, 74, "red", "渠道抛异常", "→ 记 error，摘掉它自己", "其余渠道继续跑")
    r3 = p.A(950, 200, 220, 74, "green", "渠道正常返回", "→ return 它的退出码", "整体收工")
    r4 = p.A(1220, 200, 220, 74, "red", "全部渠道都异常退出", "→ return 1")
    r5 = p.A(930, 320, 280, 74, "red", "finally: _cancel_all(tasks)", "cancel + wait(超时 5s)",
             "★ 等不到就放弃，绝不卡住退出")
    r6 = p.A(930, 430, 280, 74, "proc", "finally: shutdown()", "逆序 stop 各渠道；清空 _agents",
             "（历史已在磁盘，丢缓存不丢对话）")
    r7 = p.A(680, 430, 220, 74, "red", "Ctrl+C", "→ CancelledError 原样上抛", "Runner 转 KeyboardInterrupt")
    r8 = p.A(1220, 430, 220, 74, "orange", "退出码", "0 正常 / 1 配置错或全崩", "130 Ctrl+C")
    p.L(r1, r2, "异常", ER); p.L(r1, r3, "正常", EG); p.L(r1, r4, "全部异常", ER)
    p.L(r3, r5); p.L(r2, r5); p.L(r4, r5); p.L(r5, r6); p.L(r7, r5, "", ER); p.L(r6, r8)
    p.A(660, 700, 900, 100, "note", "★ 已修的历史坑：",
        "① 早期用 asyncio.wait 等“网关任务先完成”会永久卡死（两条消费循环是 while True，永不完成）；",
        "② Gateway.run 不能用 asyncio.TaskGroup 取代 gather（TaskGroup 正常退出时会等所有子任务结束）；",
        "③ 不给 MCP 单个退出设 asyncio.wait_for 超时（超时靠取消实现，取消会污染 cancel scope 栈）。")
    p.A(40, 700, 580, 100, "note", "★ MCP 三条铁律：",
        "手动 __aenter__；__aexit__ 必须同一 asyncio 任务；",
        "server 之间逆序断开 —— 一个 server 时完全正常，配上第二个就必炸。")
    return p
