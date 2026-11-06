from dxlib import Page, E, EB, ER

def build():
    p = Page("02-消息时序", "一条消息的完整生命周期（时序 / 泳道图）",
             "追踪：用户输入 → 渠道 → 总线 → 网关 → Agent 循环 → 模型 → （工具）→ 回复回渠道。")
    p.head()
    lanes = [("用户 / 终端", 40), ("渠道层 Channel", 250), ("总线 MessageBus", 460),
             ("网关 Gateway", 670), ("Agent 循环 AgentLoop", 880), ("模型 / 工具", 1110)]
    for name, x in lanes:
        p.A(x, 80, 200, 1020, "zone", name + "  ▼")
    def y(i):
        return 120 + i * 68
    r1 = p.A(40, y(0), 200, 52, "begin", "用户输入问题")
    r2 = p.A(250, y(0), 200, 52, "proc", "input() 丢线程读一行", "_response_event.clear()")
    r3 = p.A(250, y(1), 200, 52, "proc", "包装 InboundMessage", "channel=cli / sender_id=local")
    r4 = p.A(460, y(1), 200, 52, "hex", "inbound_queue.put")
    r5 = p.A(670, y(2), 200, 52, "orange", "consume_inbound 取消息")
    r6 = p.A(670, y(3), 200, 52, "orange", "session_key = cli:local", "查 _agents 缓存")
    r7 = p.A(670, y(4), 200, 52, "dec", "未命中 → agent_factory()", "命中 → 复用同一 AgentLoop")
    r8 = p.A(880, y(4), 200, 52, "purple", "恢复历史 _repair_history", "build_messages()")
    r9 = p.A(880, y(5), 200, 52, "purple", "provider.chat(流式)", "吐 ThinkingDelta / ContentDelta")
    r10 = p.A(1110, y(5), 200, 52, "blue", "LLM 返回", "tool_calls 或 正文")
    r11 = p.A(1110, y(6), 200, 52, "yellow", "registry.execute", "（熔断判定通过）")
    r12 = p.A(880, y(7), 200, 52, "purple", "tool 结果写回 messages", "_step += 1 再问模型")
    r13 = p.A(880, y(8), 200, 52, "purple", "无 tool_calls", "_save_to_history(turn)")
    r14 = p.A(880, y(9), 200, 52, "fin", "finally: emit", "TurnEndEvent(reply, reason)")
    r15 = p.A(670, y(9), 200, 52, "orange", "reply 为空 → 兜底文案", "publish_outbound()")
    r16 = p.A(460, y(9), 200, 52, "hex", "outbound_queue.put")
    r17 = p.A(250, y(9), 200, 52, "green", "print(AI > content) -> stdout", "_response_event.set()")
    r18 = p.A(40, y(9), 200, 52, "fin", "用户看到回答", "回到下一个 你 > ")
    p.L(r1, r2); p.L(r2, r3); p.L(r3, r4, "", EB); p.L(r4, r5, "", EB)
    p.L(r5, r6); p.L(r6, r7); p.L(r7, r8); p.L(r8, r9); p.L(r9, r10)
    p.L(r10, r11, "有 tool_calls"); p.L(r11, r12, "结果"); p.L(r12, r9, "继续下一轮（最多 50 步）")
    p.L(r10, r13, "无 tool_calls"); p.L(r13, r14); p.L(r14, r15, "", EB)
    p.L(r15, r16, "", EB); p.L(r16, r17, "", EB); p.L(r17, r18)
    p.A(40, 830, 1270, 120, "note",
        "关键约束：",
        "① CLI 用 _response_event 接力：agent 跑一轮要几十秒，期间必须让终端空着等，否则两次提问的回复叠成一团。",
        "② send() 里 set 放在 print 之后：先从等待中醒来再去打印，用户会看到提示符比回答先出现。",
        "③ stdout 只有回答（AI > 前缀），思考/工具/错误/横幅全走 stderr，于是 python main.py > answer.txt 拿到的是干净回答。",
        "④ AgentLoop 串行 inline await 投递事件 → 天然保序、天然背压；sink 不得 create_task 转发（会乱序）。")
    return p
