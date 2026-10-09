"""渠道适配器的抽象基类。

一个 Channel 就是一个"收发口"：对外替用户收发消息，对内只跟 MessageBus 打交道。
CLIChannel、FeishuChannel、WebChannel 都只是它的不同实现，装配处（main.py）拿到的
永远是 Channel 这个抽象，因此新增一个渠道不必改动 agent 一行代码。

渠道与 agent 的耦合被严格限制在总线这一条缝上，刻意不提供两样东西：

- **不持有 AgentLoop 的引用**。渠道拿到的是 MessageBus，不是 agent。于是渠道能被
  单独测试、能在 agent 之外被复用（比如只做转发），也不会出现"渠道→agent→渠道"
  这种绕回去的依赖；
- **不直接拼 InboundMessage 以外的输入**。渠道负责把所有平台差异（签名校验、事件
  解析、@机器人剥离）吃掉，吐出来的第一手东西就是拍平后的消息。

典型用法::

    bus = MessageBus()
    channel = CLIChannel(bus)
    await channel.start()   # 占住当前协程，直到用户退出
    await channel.stop()

两个约定，实现方必须遵守，否则装配处会出错：

1. **name 全局唯一且固定**。它既是消息的路由键（回复按它找回渠道），也是会话键
   （session_key）的前半段。同一个 name 出现两次，回复就会落到错误的渠道上；
2. **start 占住它所在的协程**。接收循环写在 start 里、在循环里 await 到退出为止，
   而不是内部 create_task 后立刻返回：这样"start 返回 = 渠道已退出"成立，装配处
   不必再等一个后台任务。代价是一个渠道要占一个协程，要同时起多个渠道时用
   asyncio.gather 并排跑，或者把 start 改成建后台任务。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只为类型检查而导入：放进运行时导入会让 channels 与 bus 相互引用
    from bus.queue import MessageBus, OutboundMessage

logger = logging.getLogger(__name__)


class Channel(ABC):
    """渠道的统一抽象：一个名字、一条总线、三个动作。

    基类只做两件事：把 name 与 bus 存下来（子类不必各写一遍），以及把"能停"作为
    默认行为提供出去。真正的收发在子类里，因为各家渠道的差异全在那两处。
    """

    def __init__(self, name: str, bus: MessageBus) -> None:
        """初始化。

        Args:
            name: 渠道名称，如 "cli"、"feishu"。必须是全局唯一的标识（见类文档的
                两条约定），空名字一律拒绝——它没法路由，也没法生成会话键，放过去
                只会在收到第一条消息时才炸，那时已经看不出是谁注册错了。
            bus: 收发共用的消息总线。渠道把用户消息推进它的 inbound_queue，从
                outbound_queue 取走要发的回复。

        Raises:
            ValueError: name 为空或只含空白。
        """
        if not name or not name.strip():
            raise ValueError("渠道名不能为空：它既是消息的路由键，也是会话键的前半段")
        self.name = name
        self.bus = bus

    @abstractmethod
    async def start(self) -> None:
        """开始接收，独占当前协程直到渠道退出。

        实现要点：
        - 把收到的消息拍平成 InboundMessage 交给 self.bus.publish_inbound()；
        - 循环里每轮都要有 await（等消息、等下一次轮询、等待发送），否则会饿死
          同循环里的 agent；
        - 退出时**正常返回**，不抛异常——用户退出、连接关闭都是预期内的结局。
        """
        raise NotImplementedError

    @abstractmethod
    async def send(self, message: OutboundMessage) -> None:
        """把一条回复交给用户。

        Args:
            message: 要发送的回复，channel / chat_id 已经由 agent 按收到的消息填好，
                实现方据此选连接或会话，不必自己再维护一份"这人从哪来"的表。

        实现约定：
        - **必须 await 到真正发完才返回**，不要在内部 create_task 后立刻返回。上层
          用这个 await 判断"这一轮结束了"（CLI 就靠它放行下一行输入），提前返回会让
          两轮回复叠在一起；
        - 发送失败应当记日志后返回，而不是抛出去：一条回复发不出去，不该把正在跑的
          会话连带弄崩。
        """
        raise NotImplementedError

    async def stop(self) -> None:
        """停止渠道，释放它占的资源（连接、轮询任务、监听端口等）。

        基类默认什么都不做，因此只靠协程退出就能收场的渠道（比如 CLI）不必实现它；
        有连接要关、有后台任务要取消的渠道覆盖这个方法即可。

        与 start 的配合：stop 是**给 start 一个退出的理由**，通常在 start 返回之后、
        或要主动关停时调用，不保证能被并发调用。
        """
        logger.debug("渠道 %s 无需清理", self.name)


class NullChannel(Channel):
    """什么都不做的渠道。

    看起来没用，其实是装配处的"空档"：没配任何渠道时放一个进去，调用点就不必到处
    判空，测试里也能拿来当占位。做法与 agent.events.NullSink 一致。
    """

    def __init__(self, bus: MessageBus, name: str = "null") -> None:
        """初始化。

        Args:
            bus: 消息总线，本实现不会用它，但保持与基类一致的构造形状。
            name: 渠道名，默认 "null"。
        """
        super().__init__(name, bus)

    async def start(self) -> None:
        """立刻返回：没有输入源可收。"""
        return None

    async def send(self, message: OutboundMessage) -> None:
        """丢弃回复，只留一条 debug 日志。"""
        logger.debug("丢弃发给 %s/%s 的回复（%d 字）", message.channel, message.chat_id, len(message.content))
