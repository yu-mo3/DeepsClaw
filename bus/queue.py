"""渠道与 agent 之间的消息总线。

进程里跑着的其实是三拨人：**渠道适配器**（飞书 / QQ / Web / CLI，负责收消息、发
回复）、**agent 循环**（负责想）、以及**入口**（把两边接起来）。这个模块定义它们
之间传的话（两个 dataclass）和传话的管道（MessageBus）。

    feishu ─┐                     ┌─▶ feishu
    qq     ─┼─▶ inbound_queue ─▶ agent ─▶ outbound_queue ─┼─▶ qq
    web    ─┘                     └─▶ web

把收发双方隔在一条队列后面，而不是让渠道直接调用 agent，好处有三点：

1. **方向只有一个**：渠道只管把消息扔进 inbound，agent 只管把回复扔进 outbound，
   谁都不必持有对方的引用，也不必知道对方有几个实例；
2. **两个方向互不阻塞**：进和出各占一条队列，发回复不会卡住收消息，反过来也一样
   ——合成一条队列的话，agent 发回复时若一时没人取，后面的用户消息就全堵住了；
3. **将来要加东西有地方挂**：多进程、落盘、限流都可以接在队列两端，agent 一行不改。

队列里的消息是**值对象**：不持有渠道客户端的引用，也不带回调。于是同一条消息能被
打日志、被落盘、被测试直接断言，而不必先造一个假渠道出来。

典型用法::

    bus = MessageBus()

    # 渠道侧：收到用户的飞书消息
    await bus.publish_inbound(InboundMessage(
        channel="feishu", sender_id="u_123", chat_id="oc_abc", content="你好",
    ))

    # agent 侧：取一条，想完把回复放回去
    msg = await bus.consume_inbound()
    await bus.publish_outbound(OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="你好，我是 DeepsClaw",
    ))

    # 渠道侧：取回复并真正发出去
    reply = await bus.consume_outbound()
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class InboundMessage:
    """一条从渠道进来、要交给 agent 处理的消息。

    字段只留处理这条消息必需的那些：渠道适配器拿到的原始报文（飞书的事件体、QQ 的
    上报 JSON）形状各不相同，不该污染上层——适配器负责把它拍平成下面这几个字段，
    原文另存进 raw 备查。

    Attributes:
        channel: 来源渠道名称，如 "feishu"、"qq"、"web"、"cli"。回复要原路返回，
            所以这个值必须和渠道注册时用的名字一致。
        sender_id: 发送者标识，渠道内的用户 ID。用来区分"谁在说话"，也用于权限判断。
        chat_id: 会话标识，群聊或私聊的会话 ID。它同时决定两件事：回复发到哪里，以及
            这段对话在磁盘上归入哪个会话文件（形如 CHANNEL:CHAT 的 session_key）。
        content: 消息正文，已剥掉 @机器人、命令前缀之类与渠道有关的包装，是 agent
            真正要读的那段文本。
        raw: 原始消息（渠道返回的完整事件对象），只作调试与排查用。正常流程不读它，
            因此允许为 None，也不保证任何结构。
    """

    channel: str
    sender_id: str
    chat_id: str
    content: str
    raw: dict[str, Any] | None = None


@dataclass
class OutboundMessage:
    """一条 agent 产出、要由渠道发出去的消息。

    和 InboundMessage 不对称是故意的：回复只需要"发到哪、发什么"，不需要知道用户是
    谁（渠道按 chat_id 自己就能定位），也不需要原始报文。字段集合保持最小，将来换
    别的渠道实现时才好照搬。

    Attributes:
        channel: 目标渠道，取 inbound 消息里的同名值即可，不必做映射。
        chat_id: 目标会话。把收到时的那个值原样发回去，回复就落在原来那个会话里，
            无论群聊还是私聊。
        content: 回复正文，已是可直接展示给用户的文本（分片、Markdown 渲染这些属于
            渠道侧的事）。
        reply_to: 引用的消息 ID，为 None 时作为普通消息发出。带引用的回复用于把回答
            和问题对上——多人群聊里不引用，用户看不出这句话回的是谁。
    """

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None


class MessageBus:
    """收发两端共用的两条队列。

    实例本身不带业务逻辑，只是**谁都能拿到的那两条队列**的持有者：装配时建一个，
    渠道和 agent 各拿同一个实例，接线就完成了。

    队列是 asyncio.Queue，即同一事件循环内的队列。本项目的收发协程都跑在一个循环里
    （渠道的轮询协程与 agent 协程），因此不需要线程安全的结构；若将来把某个渠道拆
    到独立线程上跑，得换成 queue.Queue 并配合 call_soon_threadsafe。

    队列**不设上限**：渠道产生消息的速率由真人手速决定，消费端又一直等在 get 上，
    正常情况下队列几乎总是空的。拍一个偏小的 maxsize 反而会在 agent 忙一轮的时候
    把渠道堵住，凭空多出"消息发不出去"的失败模式。真接上高频自动来源要限流时，该
    在 publish 上做背压，而不是在这里先猜一个数字。
    """

    def __init__(self) -> None:
        """建好两条空队列。

        队列在构造时就建出来，而不是等第一次 publish 再懒加载，是为了让"随时都能
        拿到队列对象"成立——渠道常常在自己启动的时候就先持有队列引用。
        """
        self.inbound_queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound_queue: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """把一条用户消息放进 inbound_queue，等 agent 来取。

        Args:
            msg: 渠道适配器拍平后的消息。
        """
        logger.debug(
            "收到消息 %s/%s 来自 %s（%d 字）",
            msg.channel,
            msg.chat_id,
            msg.sender_id,
            len(msg.content),
        )
        await self.inbound_queue.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """从 inbound_queue 取一条消息，队列空时挂起等待。

        队列空**不是错误**，而是常态：开着的会话大部分时间都在等用户开口。所以这里
        用 await queue.get() 一直等下去，而不是抛 Empty 或超时轮询——那种写法会逼着
        调用方处理一个本来就不该发生的分支。

        Returns:
            最早放进队列、还没被取走的那条消息（先进先出）。
        """
        msg = await self.inbound_queue.get()
        logger.debug("交给 agent %s/%s（%d 字）", msg.channel, msg.chat_id, len(msg.content))
        return msg

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """把一条回复放进 outbound_queue，等渠道发出去。

        Args:
            msg: agent 产出的回复。
        """
        logger.debug("产出回复 %s/%s（%d 字）", msg.channel, msg.chat_id, len(msg.content))
        await self.outbound_queue.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """从 outbound_queue 取一条回复，队列空时挂起等待。

        与 consume_inbound 一样用 await queue.get()：渠道的发送协程可以一直挂在这里，
        来了就发、没来就睡着，不必自己写循环加 sleep。

        Returns:
            最早放进队列、还没被取走的那条回复。
        """
        msg = await self.outbound_queue.get()
        logger.debug("待发送 %s/%s（%d 字）", msg.channel, msg.chat_id, len(msg.content))
        return msg
