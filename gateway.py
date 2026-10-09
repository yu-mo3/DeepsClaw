"""把总线、渠道和 agent 接起来的装配层。

总线、渠道、agent 三层各自都不认识对方：渠道只认 MessageBus，agent 只认自己的
输入输出，于是总得有个地方把它们插到一起，并且把"谁的消息该交给谁"这件事定下来。
Gateway 就是这个转接台，它同时是**全项目唯一按会话路由的地方**：

    channel.start() ──┐                                      ┌── _process_inbound
                      ├─▶ inbound_queue ─▶ _process_inbound ─┤      （取消息、选 agent）
    其它渠道 ─────────┘                                      │
                                                             ▼
                                                   session_key = 渠道 + 发送者
                                                             │
                                    _agents 缓存命中就用，没命中才 agent_factory
                                                             ▼
                                                       agent.run(正文)
                                                             │
    channel.send() ◀── _dispatch_outbound ◀── outbound_queue ◀┘

三个职责，各自对应一个协程，由 run() 并发拉起：

1. **收**：_process_inbound 从总线取消息，认出它属于哪个会话，转给那个会话的 agent；
2. **发**：_dispatch_outbound 从总线取回复，按渠道名找回渠道，交给它发出去；
3. **活**：各渠道自己的 start() 负责听各自的输入源（终端、WebSocket、飞书长连接）。

为什么要有 _agents 缓存：agent 的价值大半在它手里的历史。如果每条消息都新建一个
AgentLoop，历史就断在每句话上，用户会得到一个"每次都失忆"的机器人。所以按
session_key 缓存——同一个人的第二句话必须回到同一个 agent 实例上，才能接上上下文。
也正因为如此，缓存里的实例**不能**用完就丢，它就是这个会话的常驻状态。

典型用法::

    bus = MessageBus()
    gateway = Gateway(
        bus=bus,
        channels=[CLIChannel(bus)],
        agent_factory=lambda key: build_agent(key),   # 按 key 建一个 AgentLoop
    )
    await gateway.run()      # 跑到被取消为止（Ctrl+C），期间可 await gateway.shutdown()

**本模块只做路由，不做业务**：它不拼 System Prompt、不决定用哪个模型、不碰会话文件，
这些都留在 agent_factory 返回的那个 agent 里。于是换模型、换工具、换人设都只改
factory，这一层一行不动。

由此有一条**硬约束，渠道实现方必须遵守**：

    渠道只能往 inbound_queue 里放消息、只能被 send() 投喂；
    **绝不可以在自己的 start() 里调 bus.consume_outbound()。**

asyncio.Queue 是**单消费者**的：一条回复只会被一个 get() 取走。网关这两条循环是
总线上唯一的消费者，渠道若自己去取，就是两个消费者抢同一条队列——回复会被随机地
要么进网关（正常投递）、要么被渠道自己吞掉（永远发不出去），且时机不同表现不同，
最难排查的那类 bug。渠道要发消息，唯一的路是 send()；它不需要知道回复从哪来。

尚未处理的取舍（当前形态是刻意的最小实现，真要多渠道长期跑时再补）：

- **退出信号来自渠道**。run() 在"任意一条渠道的 start() 返回"时就整体收工（见 run
  里的说明）：这在当前形态下是对的（渠道没了就没人可服务），但多个渠道并存时，
  一个渠道的退出会带走所有人。将来要么改成"全部渠道都退出才收工"，要么让装配处
  显式调用 shutdown；
- **轮内串行**。_process_inbound 是一个协程，消息严格按到达顺序一条条处理，某个
  会话跑一轮要 30 秒时，其他人的消息会排在它后面。要并发就得按 session_key 分派
  到各自的 worker，同时给 _agents 的读写加上锁。

另外记一条踩过的坑：run() 里**不能用 asyncio.TaskGroup 取代 gather**。TaskGroup 的
async with 在正常退出时会等**所有**子任务结束，而这两个消费循环按设计永不结束，于是
run() 永远不返回、外部的取消也传不进来；外层若按"任一渠道结束就叫停"，就会卡死在
等输入上（渠道已经返回了，网关却退不出来）。gather 在被取消时会把取消传播给子任务，
这才是这个结构需要的语义——上面那个 except CancelledError 就是配合它做手工收尾的。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable

from bus.queue import OutboundMessage
from channels.base import Channel

if TYPE_CHECKING:  # 只为类型检查而导入，避免 agent/渠道侧反向引用时绕成循环导入
    from agent.loop import AgentLoop
    from bus.queue import InboundMessage, MessageBus

logger = logging.getLogger(__name__)

#: 收尾时等各任务响应取消的宽限时间（秒）。见 Gateway._cancel_all 的说明：
#: 超过它就放弃等待、继续往下走，绝不让"某个任务不听话"变成"进程退不出去"。
CANCEL_GRACE = 5.0

#: agent 整轮失败时发给用户的话。带上异常类型，用户报障时"哪种错"往往比"什么错"
#: 更有用；详细堆栈只进日志，不往聊天窗口里倒。
ERROR_REPLY = "抱歉，处理这条消息时出错了，请稍后再试。（{kind}）"

#: agent 返回空文本时的兜底。渠道那边是直接打印 content 的，空的会让用户看到
#: 一个光秃秃的 "🤖 "，像是程序坏了。
EMPTY_REPLY = "（模型没有返回内容）"


class Gateway:
    """消息转接台：把总线上的消息分给对的会话，把回复分给对的渠道。

    它不干活，只搬运：进来的是 InboundMessage，出去的是 OutboundMessage，中间那句
    "这一条该谁管"由 session_key 决定。

    状态只有两块，都是缓存，都可以随时清掉：

    - _agents：session_key -> AgentLoop。**它的存在本身就是为了留住上下文**；
    - _channel_map：渠道名 -> Channel。回复要原路返回，靠它做反向查找。

    "每个会话一个 agent"这个模型有个直接推论，值得写在这里：同一个人开两个窗口，
    也只是同一个 agent 在轮流答两边的消息，历史是一条。要按窗口分开，就把 chat_id
    编进 session_key（见 _session_key）。
    """

    def __init__(
        self,
        bus: MessageBus,
        channels: list[Channel],
        agent_factory: Callable[[str], AgentLoop],
    ) -> None:
        """初始化。

        Args:
            bus: 收发共用的消息总线，必须与各渠道拿到的是**同一个实例**——渠道推进
                去的消息、本类取出来的消息，只有同一条队列才对得上。
            channels: 要跑起来的渠道列表。顺序无所谓，但渠道名必须互不相同：回复靠
                名字找回渠道，重名会让其中一路的消息永远发不出去（这里只记警告，
                仍按"后者胜"继续装配，不因为一个配置笔误就起不来）。
            agent_factory: 按 session_key 造 agent 的工厂函数，签名 ``(session_key)
                -> AgentLoop``。做成工厂而不是直接传 agent 列表，是因为会话是**遇到
                才知道有几个**的——第一个人发来消息之前，没谁知道要建哪些会话。
        """
        self.bus = bus
        self.channels = list(channels)
        self.agent_factory = agent_factory

        #: session_key -> agent。会话的上下文就靠它续着，见类文档。
        self._agents: dict[str, AgentLoop] = {}
        #: 渠道名 -> 渠道。渠道列表是给"启动"用的，这张表是给"投递"用的。
        self._channel_map: dict[str, Channel] = {}
        for channel in self.channels:
            if channel.name in self._channel_map:
                logger.warning(
                    "渠道名 %s 重复，前一个实例将被覆盖，它的回复将无法投递",
                    channel.name,
                )
            self._channel_map[channel.name] = channel

    async def run(self) -> None:
        """并发跑起所有渠道与两个消费循环，直到整体被取消。

        这几件事必须**同时**发生：渠道在等人输入、入站循环在等消息、出站循环在等
        回复。任何一个先 await 完再轮到下一个，都会让另外两个彻底没机会执行——比如
        先 await 渠道的 start()，agent 就永远拿不到消息。asyncio.gather 正是把
        "同时"这件事写出来的最直接方式。

        退出规则（读一遍能省掉一次线上排查）：

        - **渠道正常返回**（CLI 敲了 /exit、WebSocket 服务被停）说明"用户要收工了"，
          本方法据此返回它给出的退出码，并把其余渠道与消费循环一起收干净；
        - **渠道抛异常**（连不上、鉴权失败、网络断了）只把那条渠道从服务中摘掉，
          **其余渠道继续跑**。一个渠道崩掉不该让整台机器人都失联——那是把局部故障
          放大成全局故障。全部渠道都退出后本方法才返回。

        Raises:
            asyncio.CancelledError: 被取消时不吞掉，交给上层。渠道仍会收到一次
                stop()（except 分支负责），不会留下没关的连接。
            Exception: 任何一条子任务抛出的异常都会原样冒到这里（gather 不做兜底），
                由装配处决定是重试还是退出。
        """
        logger.info(
            "网关启动：%d 个渠道（%s）",
            len(self.channels),
            "、".join(self._channel_map) or "无",
        )
        # 渠道任务单独攥着，不直接塞进一个 gather 了事——理由见下面对 wait 的说明。
        channel_tasks = [
            asyncio.create_task(channel.start(), name=f"channel:{channel.name}")
            for channel in self.channels
        ]
        tasks = [
            *channel_tasks,
            asyncio.create_task(self._process_inbound(), name="gateway:inbound"),
            asyncio.create_task(self._dispatch_outbound(), name="gateway:outbound"),
        ]
        try:
            # 等渠道结束。这里必须是"等渠道"而不是"等所有任务"：两个消费循环是 while
            # True，永远不会自己结束，若对全部任务 await，本方法就永远不返回，外层
            # （main）也就永远等不到退出信号——现象正是敲了 /exit 却卡死。
            # 还要逐条看**为什么**结束：正常返回才是"用户要收工"，抛异常只是这条渠道坏了。
            alive: set[asyncio.Task] = set(channel_tasks)
            while alive:
                done, _ = await asyncio.wait(
                    list(alive), return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    alive.discard(task)
                    exc = task.exception()
                    if exc is not None:
                        # 只摘掉这一条，别的不受影响：连不上 QQ 不该连带把 CLI 也停了。
                        logger.error(
                            "渠道 %s 异常退出，将从服务中摘除：%s: %s",
                            task.get_name(),
                            type(exc).__name__,
                            exc,
                        )
                        continue
                    code = task.result()
                    logger.info("渠道 %s 正常结束，网关准备收工（退出码 %s）", task.get_name(), code)
                    return code if isinstance(code, int) else 0
            # 走到这里说明所有渠道都是"异常退出"的。没有可用渠道的网关没有意义，
            # 让它返回（finally 会清干净），比原地空转更诚实。
            logger.warning("所有渠道都已异常退出，网关停止")
            return 1
        except asyncio.CancelledError:
            # 被取消（Ctrl+C）。在 await 点收到的取消只作用于**本协程**，上面那些任务
            # 不受影响，它们会各自继续挂在队列上，而 finally 里的 shutdown() 又要 await，
            # 于是这个协程永远退不出去。所以必须自己把子任务收掉再往上抛。
            raise
        finally:
            # 正常收工与 Ctrl+C 都走这里：先停协程，再关渠道。
            await self._cancel_all(tasks)
            await self.shutdown()

    @staticmethod
    async def _cancel_all(tasks: list[asyncio.Task[Any]]) -> None:
        """取消一组任务并等它们真的结束，**最多等 CANCEL_GRACE 秒**。

        为什么要超时：一条取消只保证"在下一个 await 点抛出 CancelledError"，对**当前
        正卡在不可取消的等待里**的任务是无效的。收尾时若傻等它们，就等于把整个退出流程
        押在"所有任务都能立刻响应取消"这个假设上——一旦某条任务挂在别处（阻塞的线程
        调用、第三方库内部的等待），进程就再也退不出去，用户看到的是敲了 /exit 却没反应。

        等不到就放弃它、继续往下走：这类任务只涉及进程内的内存对象，进程退出时自然消失；
        而**渠道的连接是 stop() 负责关的**，不依赖任务退出。宁可留一条僵尸协程，也不要
        卡住退出——这正是之前踩过的坑。

        Args:
            tasks: 要收掉的任务列表。
        """
        for task in tasks:
            task.cancel()
        done, pending = await asyncio.wait(tasks, timeout=CANCEL_GRACE)
        if pending:
            # 只记日志不抛：这是"某条任务没响应取消"，属于要排查、但不该阻塞退出的情况。
            logger.warning(
                "有 %d 个任务未在 %s 秒内响应取消：%s",
                len(pending),
                CANCEL_GRACE,
                "、".join(sorted(t.get_name() for t in pending)),
            )

    async def _process_inbound(self) -> None:
        """入站循环：取一条消息，交给它所属会话的 agent，把回复放回总线。

        一条消息走完四步才回到循环顶部，因此**同一时刻只有一个会话在跑**（见模块
        文档的"轮内串行"）。

        异常处理有两层，是因为"出错"发生在完全不同的地方：

        - agent.run() 内部已经把网络失败、熔断、步数耗尽这些兜成了文本返回，正常
          情况下这里拿到的是字符串，不会走到 except。留这一层是为了接住漏出来的
          编程错误——把它变成一句给用户的道歉，比让整个消费循环带着异常死掉好，
          后者会让机器人从此不回话且毫无提示；
        - asyncio.CancelledError 不是 Exception（Py3.8 起是 BaseException 的子类），
          所以不会被上面那句 except 吞掉，Ctrl+C 仍然能真的停下来。
        """
        while True:
            msg = await self.bus.consume_inbound()
            session_key = self._session_key(msg)
            try:
                agent = self._get_agent(session_key)
                reply = await agent.run(msg.content)
            except Exception as exc:  # noqa: BLE001 - 一条消息的失败不该拖垮整个循环
                logger.exception("会话 %s 处理消息失败", session_key)
                reply = ERROR_REPLY.format(kind=type(exc).__name__)
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=reply or EMPTY_REPLY,
                )
            )

    async def _dispatch_outbound(self) -> None:
        """出站循环：取一条回复，找到它该走的渠道，投出去。

        本方法是 outbound_queue 的**唯一消费者**，渠道不得自行 consume（见模块文档的
        硬约束）：队列是单消费者的，多一个消费者就会随机丢回复。

        找不到渠道只记警告、**不抛异常**：一条回复投不出去，不该把分发循环弄死——
        那样后面所有渠道的回复都会一起停摆，故障范围从"一个渠道"扩大到"全部"。
        能用名字查不到渠道，说明这条回复的 channel 从来没被装配过，属于配置问题，
        日志里看得到即可。
        """
        while True:
            msg = await self.bus.consume_outbound()
            channel = self._channel_map.get(msg.channel)
            if channel is None:
                logger.warning(
                    "没有名为 %s 的渠道，丢弃发给 %s 的回复（%d 字）",
                    msg.channel,
                    msg.chat_id,
                    len(msg.content),
                )
                continue
            try:
                await channel.send(msg)
            except Exception:  # noqa: BLE001 - 同上：单个渠道发送失败不拖垮分发
                logger.exception("渠道 %s 发送回复失败", msg.channel)

    async def shutdown(self) -> None:
        """关停：停掉所有渠道，丢掉所有会话。

        按 channels 的**相反顺序**停：装配顺序通常是"先建的先启动"，反序停近似于
        按依赖倒着拆，对将来有上下游关系的渠道（比如先连网关再连推送）更稳妥。

        清空 _agents 是**故意丢状态**：历史已经由 AgentLoop 自己落到会话文件里了，
        缓存清掉不会丢对话，下次同一个会话再来会重新建实例、重新从磁盘恢复。反倒是
        留着旧实例有风险——provider 的连接池可能已经关了，再 run 就会在奇怪的
        地方报连接错误。

        单个渠道 stop 失败只记日志、继续停下一个：关停是收尾动作，不能因为其中一个
        出错就留下其余的连接不关。
        """
        logger.info("网关关停：停止 %d 个渠道，清空 %d 个会话", len(self.channels), len(self._agents))
        for channel in reversed(self.channels):
            try:
                await channel.stop()
            except Exception:  # noqa: BLE001 - 收尾阶段：能停几个是几个
                logger.exception("渠道 %s 停止失败", channel.name)
        self._agents.clear()

    def _session_key(self, msg: InboundMessage) -> str:
        """算出这条消息属于哪个会话。

        形如 ``feishu:u_123``：渠道名在前，发送者在后。渠道名是前缀，是为了让同一个人
        在飞书和 QQ 上有各自的上下文——按外部账号合并会话，得先有一张身份映射表，
        那是另一个功能。

        当前用 sender_id 归并，**群里所有人的消息都会进同一个会话**（群聊里一个
        会话里混着几个人的话）。想让"谁在哪个群里"各算一个会话，把下面换成
        ``f"{msg.channel}:{msg.chat_id}"`` 即可；想让整群共用一个会话、每个人都
        连到同一段上下文，那就用 chat_id 而不用 sender_id。
        """
        return f"{msg.channel}:{msg.sender_id}"

    def _get_agent(self, session_key: str) -> AgentLoop:
        """取这个会话的 agent，没有就现造一个并缓存起来。

        工厂调用放在**没有并发**的位置：_process_inbound 是单协程串行消费的，所以
        同一 key 不会有两个协程同时走到这里，不需要为缓存加锁。将来改成按会话并发
        时要补锁，否则同一个会话会被建出两个 agent，历史分成互不相见的两半。

        Args:
            session_key: 会话标识，会被原样传给 agent_factory。

        Returns:
            该会话的 agent 实例（缓存命中时是同一个对象）。
        """
        agent = self._agents.get(session_key)
        if agent is None:
            logger.debug("新建会话 %s", session_key)
            agent = self.agent_factory(session_key)
            self._agents[session_key] = agent
        return agent
