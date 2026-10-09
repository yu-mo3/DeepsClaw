"""渠道层。

按渠道各写一个适配器（CLI、飞书、QQ、Web），它们只做两件事：把收到的消息拍平成
InboundMessage 推进总线，以及把总线上的 OutboundMessage 发出去。agent 不认渠道，
渠道也不认 agent，两边唯一的接口就是 MessageBus。
"""
