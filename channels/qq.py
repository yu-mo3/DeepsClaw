"""QQ 官方机器人（QQ 开放平台）渠道实现。

用官方 SDK qq-botpy 接单聊与群聊：机器人连着 QQ 的 WebSocket 网关，用户发消息由
SDK 回调进来，回复走 REST 接口发回去。装上 botpy 之后，本模块与飞书那套的区别只有
两点——**回调本身就是 async 的**（且运行在主事件循环里，所以直接 await，不需要
飞书那种 run_coroutine_threadsafe 绕线程），以及**发送接口按会话类型分成两个**
（群 / 单聊），而不是一个统一的 send。

两个类各管一头，中间只通过 MessageBus 打交道：

    QQBotClient（botpy 回调）──publish_inbound──▶ bus ──▶ Gateway ──▶ agent
                                                                      │
    QQChannel.send（调 REST 接口）◀──bus.consume_outbound──────────────┘

    QQChannel.start()
      └─ async with QQBotClient: await bot.start(appid, secret)   # 阻塞到连接关闭

三条容易踩的坑，写在这里免得下次再查一遍：

1. **单聊用的是 C2CMessage，不是 DirectMessage**。前者是 QQ 单聊
   （`on_c2c_message_create`，用户 openid 在 `author.user_openid`），
   后者是**频道私信**（`on_direct_message_create`，需要 direct_message intent）
   ——两者完全不是一回事，用错了会表现为"机器人收不到单聊消息"；
2. **intent 用 public_messages**。它同时覆盖群消息与 C2C 单聊事件；频道消息才要
   public_guild_messages。另外单聊的 openid 与群里的 member_openid **不是同一套标识**，
   所以单聊的 chat_id 只能取 user_openid，也没法与同一个人在同一群里的身份互通；
3. **不用 bot.run()**。它内部是 `loop.run_until_complete()`，放进已经在跑的
   事件循环里会报"循环已在运行"。本模块用 `async with` + `await start()`：
   前者负责建好连接期需要的 asyncio 对象（loop / ready 事件），后者才是真正的登录与连接。

依赖：`pip install qq-botpy`。没装的话导入本模块会立刻报 ModuleNotFoundError——
这是刻意的：main.py 只在配了 QQ 凭据时才导入它，不用 QQ 的人完全不受影响。

典型用法（由 main.py 装配，这里只示形）::

    channel = QQChannel(bus, app_id="123", app_secret="***")
    await channel.start()      # 占住协程，直到连接关闭
    await channel.stop()
"""

from __future__ import annotations

import logging
import re
from typing import Any

import botpy
from botpy.message import C2CMessage, GroupMessage

from bus.queue import InboundMessage, OutboundMessage
from channels.base import Channel

logger = logging.getLogger(__name__)

_AT_BOT = re.compile(r"<@!\d+>")

#: 会话类型标记，会写进 InboundMessage.raw 供排查。
MSG_TYPE_GROUP = "group"
MSG_TYPE_C2C = "c2c"

#: 文本消息：0=文本 1=图文混排 2=markdown 3=ark 4=embed 7=富媒体。本渠道只发文本。
MSG_TYPE_TEXT = 0


class QQBotClient(botpy.Client):
    """botpy 的事件回调入口。

    只做一件事：把 SDK 递进来的事件翻译成 InboundMessage 推进总线。不碰 agent、
    不碰会话文件，所以这个类里没有一行业务逻辑——排查"消息到底有没有进来"时，
    只需要看这里。

    持有 QQChannel 的引用（而不是 bus）：消息与回复都挂在同一对象上，装配处只需要
    记住渠道这一个东西。
    """

    def __init__(self, channel: QQChannel, **kwargs: Any) -> None:
        """初始化。

        Args:
            channel: 所属渠道，回调通过它拿到总线。
            **kwargs: 原样透传给 botpy.Client（intents 等）。
        """
        super().__init__(**kwargs)
        self._channel = channel

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        """群里被 @ 时收到消息。

        Args:
            message: SDK 解析好的群消息。
        """
        sender_id = message.author.member_openid
        chat_id = message.group_openid
        content = self._strip_at(message.content)
        logger.info("QQ 群消息 %s ← %s（%d 字）", chat_id, sender_id, len(content))
        await self._channel.publish(sender_id, chat_id, content, MSG_TYPE_GROUP, message)

    async def on_c2c_message_create(self, message: C2CMessage) -> None:
        """收到单聊（私聊）消息。

        **注意是 C2CMessage 而不是 DirectMessage**：后者是频道私信，属于另一套接口，
        与 QQ 单聊完全不相干。单聊的用户标识取 `author.user_openid`（不是
        `author.openid`，那是频道侧的字段）。

        Args:
            message: SDK 解析好的单聊消息。
        """
        sender_id = message.author.user_openid
        # 单聊没有"群"这种容器，chat_id 就是对方本人，回复时按它找收件人。
        chat_id = sender_id
        content = self._strip_at(message.content)
        logger.info("QQ 单聊来消息（%d 字）", len(content))
        await self._channel.publish(sender_id, chat_id, content, MSG_TYPE_C2C, message)

    @staticmethod
    def _strip_at(content: str | None) -> str:
        """剥掉 @机器人占位符与首尾空白。

        用 sub 而不是 lstrip：@ 通常在开头，但也可能夹在正文中间（用户先打字再 @），
        统一按"删掉占位符"处理比按位置切更稳。
        """
        return _AT_BOT.sub("", content or "").strip()


class QQChannel(Channel):
    """QQ 渠道：把 QQ 的消息接进总线，把回复发回 QQ。

    start() 会一直连在 QQ 的 WebSocket 网关上，直到连接关闭或被取消——与其他渠道
    一样，它占住自己的协程，由 Gateway 统一拉起与收停。

    渠道自己记着"哪些 chat_id 是群、哪些是单聊"（_session_types）。这是被出站接口的
    形状逼出来的：发群消息要 group_openid 参数、发单聊要 openid 参数，是两个不同的
    方法，而 OutboundMessage 只带一个 chat_id、不带类型。记下来还有一个好处：不必对
    每条回复都先去试探一个注定失败的接口。
    """

    def __init__(self, bus: Any, app_id: str, app_secret: str) -> None:
        """初始化。

        Args:
            bus: 消息总线。
            app_id: QQ 开放平台的机器人 AppID。
            app_secret: 机器人 AppSecret。密钥只在这里用一次，绝不进日志。
        """
        super().__init__("qq", bus)
        self.app_id = app_id
        self.app_secret = app_secret

        #: botpy 客户端。start() 里才创建；stop() 之后置空，于是 send() 靠"引用没了"
        #: 就能判断出渠道已经关了，不必再维护一个 is_running 标志。
        self._bot: QQBotClient | None = None

        #: chat_id -> 会话类型（group / c2c）。见类文档。
        self._session_types: dict[str, str] = {}

    async def start(self) -> None:
        """连上 QQ 网关并开始收消息，直到连接关闭或被取消。

        用 `async with` 而不是 `bot.run()`：run() 内部是
        loop.run_until_complete()，在已经跑着的事件循环里调用会直接报错——而本项目的
        所有渠道都跑在同一个循环里。`__aenter__` 负责建好连接期需要的 asyncio
        对象，`start` 才做登录与长连接。

        参数名是 **secret** 而不是 token：botpy 拿 appid + secret 去换 access_token，
        签名用的也是 secret。

        本方法会一直挂着（长连接），因此它天然占住自己的协程——正是 Channel 基类约定
        的形态。
        """
        intents = botpy.Intents(public_messages=True)
        # bot_log=False：不让 SDK 在**当前目录**写 botpy.log。它的默认文件 handler 会把
        # 日志落到 os.getcwd()（跑一次项目根就多一个文件），而同样的内容它本来也会
        # 经 logging 输出——main.py 配好的 handler 会让它走 stderr，不丢信息也不留垃圾。
        self._bot = QQBotClient(channel=self, intents=intents, bot_log=False)
        logger.info("QQ 渠道启动中（appid=%s）", self.app_id)
        async with self._bot:
            await self._bot.start(appid=self.app_id, secret=self.app_secret)
        logger.info("QQ 渠道连接已结束")

    async def send(self, message: OutboundMessage) -> None:
        """把一条回复发回 QQ。

        群和单聊是两个不同的 REST 接口，而 OutboundMessage 不携带会话类型，所以：
        查得到类型就直接走对的那个接口；查不到（渠道刚起、或回复对应的入站记录已丢）
        就按"先单聊、失败再群"试探——两个接口的入参名字不同，试探本身也就确认了
        chat_id 属于哪一侧。

        发送失败**不抛出**，只记日志：一条回复发不出去不该把分发循环拖垮，这个约定
        写在 Channel.send 的文档里。

        Args:
            message: 要发送的回复。chat_id 是群的 group_openid，或单聊的用户 openid。
        """
        bot = self._bot
        if bot is None or bot.api is None:
            logger.warning("QQ 渠道尚未连接（或已关闭），丢弃发给 %s 的回复", message.chat_id)
            return

        kind = self._session_types.get(message.chat_id)
        try:
            if kind == MSG_TYPE_GROUP:
                await self._send_group(bot, message)
                return
            if kind == MSG_TYPE_C2C:
                await self._send_c2c(bot, message)
                return
            try:
                await self._send_c2c(bot, message)
                self._session_types[message.chat_id] = MSG_TYPE_C2C
            except Exception:  # noqa: BLE001 - 试错路径：失败的尝试是预期内的
                logger.debug("按单聊发送失败，改按群发送：%s", message.chat_id)
                await self._send_group(bot, message)
                self._session_types[message.chat_id] = MSG_TYPE_GROUP
        except Exception:  # noqa: BLE001 - 见 docstring：发送失败只记日志
            logger.exception("QQ 发送失败（chat_id=%s）", message.chat_id)

    async def _send_c2c(self, bot: QQBotClient, message: OutboundMessage) -> None:
        """按单聊发送。openid 就是收件人本人。"""
        await bot.api.post_c2c_message(
            openid=message.chat_id,
            msg_type=MSG_TYPE_TEXT,
            msg_id=message.reply_to or "",
            content=message.content,
        )
        logger.debug("QQ 单聊已发送 → %s", message.chat_id)

    async def _send_group(self, bot: QQBotClient, message: OutboundMessage) -> None:
        """按群发送。group_openid 是群标识。"""
        await bot.api.post_group_message(
            group_openid=message.chat_id,
            msg_type=MSG_TYPE_TEXT,
            msg_id=message.reply_to or "",
            content=message.content,
        )
        logger.debug("QQ 群消息已发送 → %s", message.chat_id)

    async def stop(self) -> None:
        """停掉渠道：断开引用，让后续发送立刻失败得清楚。

        把 _bot 置空是这里的实际动作。**不主动跟 botpy 断连**：长连接与 HTTP 会话由
        SDK 自己持有，进程退出时随之释放；而引用一旦为空，send() 立刻拒绝发送，不会再
        往一条已关闭的连接上写——那才是关停之后最容易冒出来的一类错误。

        **不主动清 _session_types**：重建渠道是装配处的事，留着这张表不影响正确性，
        却能让"关停后再发一次"这类边界情况仍然找对接口。
        """
        self._bot = None
        logger.info("QQ 渠道已停止")

    async def publish(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        msg_type: str,
        raw: Any,
    ) -> None:
        """把一条 QQ 消息拍平后推进总线（供 QQBotClient 调用）。

        顺手记住 chat_id 的会话类型，出站时按它选接口（见类文档）。类型信息留在渠道
        内部而不是塞进 InboundMessage：它是纯粹的渠道细节，agent 与网关都不该看见。

        Args:
            sender_id: 发送者标识（群里的 member_openid 或单聊的 user_openid）。
            chat_id: 会话标识（群的 group_openid 或单聊的用户 openid）。
            content: 已剥掉 @ 的正文。
            msg_type: MSG_TYPE_GROUP 或 MSG_TYPE_C2C。
            raw: SDK 原始消息对象。
        """
        self._session_types[chat_id] = msg_type
        await self.bus.publish_inbound(
            InboundMessage(
                channel=self.name,
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                # raw 里带类型标记：SDK 对象不便序列化，排查时看这一行就够了。
                raw={"msg_type": msg_type, "message": raw},
            )
        )