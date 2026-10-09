"""派生一个临时子 agent 去干一件具体的活。

有些任务塞进主对话里会污染上下文：读完十几个文件只为了回答一个问题、试三次才跑通的
一段命令、要翻一大堆搜索结果才能挑出两条。这类"过程很长、结论很短"的活适合交给一个
**临时 agent**：它有自己的历史、自己的循环，跑完把结论交回来，那一堆中间过程不会留在
主对话里。

    SpawnSubagentTool.execute(task)
        └─ 现造一个 AgentLoop（自己的 Provider、工具集、一次性历史）
             └─ await agent.run(task)
                  └─ 返回结论文本 → 主 agent 当成一次普通工具结果收下

四个刻意的设计，都是为了让"子 agent 只是工具，不是第二个主 agent"：

1. **系统提示只有一句话**（见 SUBAGENT_IDENTITY）。它不带主 agent 的人设、记忆与
   技能摘要——那些是"跟用户长期相处"才需要的东西。子 agent 只知道"把这件事做完"，
   目标越单一，行为越可控，也越省 token；
2. **不写磁盘**。子 agent 用 DummySessionManager，历史只活在这一次调用里，跑完即散。
   它是临时工，不该在 workspace/sessions 里留下一堆一次性会话文件；
3. **拿不到主对话的历史**。历史是空的，只把 task 这一句话交给它——这正是本工具的
   价值所在：不继承上下文，才不会把主对话的噪声带进子任务；
4. **深度受限**。子 agent 自己也能有 spawn_subagent（取决于 max_depth），但不能无限
   套娃：每层都新开一个完整的模型循环，层层派发会迅速烧完 token 而什么也没产出。

代价写在明处：每派生一次就新建一个 Provider（一份连接池）外加多次模型调用。所以它适合
"值得多花一次调用换一个干净上下文"的任务，不适合替主 agent 做顺手就能做完的小事——
description 里写了这条，模型也该照此判断。

典型用法（由 main.py 装配）::

    registry.register(
        SpawnSubagentTool(
            provider_factory=lambda model: OpenAICompatProvider(
                api_key=cfg.api_key, base_url=cfg.base_url, model=model or cfg.model)
            ,tools_registry=registry,
            workspace=str(cfg.workspace),
        )
    )
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Optional

from agent.context import ContextBuilder
from agent.loop import AgentLoop
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from providers.base import LLMProvider

logger = logging.getLogger(__name__)

#: 子 agent 的系统提示，全文就这一句。
#:
#: 短是刻意的：人设、记忆、技能都是"长期相处"才需要的东西，子 agent 只需要知道
#: "把活干完、直接给结果"。附加要求（别寒暄、别反问、把结论说清楚）都压在这一个句子
#: 里，免得提示词膨胀成第二份人设。
SUBAGENT_IDENTITY = "你是任务专员，完成任务直接输出结果"

#: 本工具在注册表里的名字。复制工具时按它跳过自己。
TOOL_NAME = "spawn_subagent"

#: 子 agent 单轮最多调用几次模型。
#:
#: 比主 agent 的默认值（50）小得多：子任务只有一件事，跑到二十步还没结论说明方向就
#: 错了；而每多一层派生的代价都是实打实的 token，宁可让它早点带着半成品回来。
SUBAGENT_MAX_STEPS = 20


class DummySessionManager:
    """给子 agent 用的空会话管理器：不读、不写、不删。

    存在的意义是**满足 AgentLoop 的构造形状**：它接收 session_manager 参数，为 None
    时本就退化成纯内存模式（正是我们要的），但显式传一个空实现把意图写成了代码事实——
    "子 agent 不落盘"从此有人看得见。将来谁想给子 agent 加持久化，必须先删掉这个类，
    也就无法在不知不觉中往 sessions 目录里倒一次性文件。

    三个方法对应 SessionManager 的三件事：恢复历史（空）、追加消息（丢弃）、清空会话
    （无事可做）。
    """

    def get_history(self, session_key: str, limit: int | None = None) -> list[dict[str, Any]]:
        """永远返回空历史：子 agent 不继承上下文，只带着 task 开场。"""
        return []

    def save_message(self, session_key: str, message: dict[str, Any]) -> None:
        """什么都不做：子 agent 的对话不留档。"""
        return None

    def clear(self, session_key: str) -> None:
        """什么都不做：没有落盘的东西可清。"""
        return None


class SubagentContext(ContextBuilder):
    """子 agent 的上下文构造器：System Prompt 只有任务专员那一句。

    继承而非另写一个类，是为了白拿父类 build_messages 的组装逻辑（"system + 历史 + 本轮
    输入"这种拼接规则只该有一处实现）。

    而且**只覆盖 build_system_prompt 这一个方法**：父类的 _load_identity / _load_memory
    / skills_summary 全部不参与拼装，于是子 agent 天然拿不到人设、长期记忆和技能摘要。
    这比"先建一个普通 ContextBuilder 再把字段清空"稳：那种写法会在父类将来新增上下文块
    时悄悄多带一块本不该有的东西，而这里不会——父类怎么改都只影响自己那一句。
    """

    def __init__(self, workspace: str) -> None:
        """初始化。

        Args:
            workspace: 工作区根目录，照常交给父类——子 agent 的文件工具作用范围与主 agent
                一致：它只是换了脑子，不是换了场地。
        """
        super().__init__(workspace=workspace)
        self._workspace = workspace

    def build_system_prompt(self) -> str:
        """返回那一句话，外加时间与工作区。

        只补这两样"环境事实"：不知道自己在哪个目录，文件工具给出的相对路径它用不了；
        不知道当前时间，涉及"今天/最近"的任务只能靠猜。人设、记忆、技能一律不带。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        return (
            f"{SUBAGENT_IDENTITY}\n\n"
            f"## 当前时间\n{now}\n\n"
            f"## 工作区\n{self._workspace}"
        )


class SpawnSubagentTool(Tool):
    """把一个子任务交给临时 agent 执行，返回它的结论。

    实例是**可重入**的：主 agent 一轮里可以发多个 tool_call，于是同一个实例可能被并发的
    execute 调用。所有一次性的东西（Provider、注册表、AgentLoop）都是方法内的局部变量，
    实例只持有配置，因此并发调用互不干扰。
    """

    def __init__(
        self,
        provider_factory: Callable[[Optional[str]], LLMProvider],
        tools_registry: ToolRegistry,
        workspace: str,
        current_depth: int = 0,
        max_depth: int = 2,
    ) -> None:
        """初始化。

        Args:
            provider_factory: 造 Provider 的工厂，签名 (model) -> LLMProvider。model 为
                None 时应返回一个用默认模型的服务方。做成工厂而不是直接传 Provider，
                是因为子 agent 可能换更便宜的模型，而且用完就要关——"怎么造、造哪个"
                留给装配处，本工具不必知道密钥与 base_url。
            tools_registry: 主 agent 的工具注册表。子 agent 从这里复制工具集，于是装配处
                不必再列一遍相同清单。
            workspace: 工作区根目录，子 agent 的文件工具以此为沙箱。
            current_depth: 当前深度。0 表示主 agent 手里那一份。
            max_depth: 允许的最大深度。current_depth + 1 >= max_depth 时子 agent 不再
                获得 spawn_subagent，套娃到此为止。
        """
        # 前提：tools_registry 里**不含本实例自己**（正常情况下装配处只 register 一次）。
        # 若有人把它自己注册进了它自己持有的注册表，复制时会跳过（按名字过滤），所以也
        # 不会无限套下去——但那种装配本身就是错的，这里不做支持。
        self._provider_factory = provider_factory
        self._tools_registry = tools_registry
        self._workspace = workspace
        self.current_depth = current_depth
        self.max_depth = max_depth

    @property
    def name(self) -> str:
        """工具名，与注册表里的键一致。"""
        return TOOL_NAME

    @property
    def description(self) -> str:
        """工具说明。重点写"什么时候值得用"，因为它每次调用都挺贵。"""
        return (
            "把一个子任务交给临时专员执行并返回结论。适合过程长、结论短的活："
            "翻多个文件才回答一个问题、连续试错、汇总一批搜索结果。"
            "子专员看不到本次对话，只能看到你写在 task 里的内容——"
            "所以 task 要自包含：把背景、目标、期望产出写全。"
            "不要用它做顺手就能完成的小事：每次派生都有额外的模型调用开销。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """参数定义：task 必填，model 可选。"""
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "交给子任务专员的完整任务描述。它看不到本次对话，"
                        "请写清楚必要的背景、要做什么、期望产出什么。"
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "指定子任务使用的模型名，省略则用默认模型。"
                        "简单归纳类任务可以指定更便宜的模型。"
                    ),
                },
            },
            "required": ["task"],
        }

    async def execute(
        self, task: str = "", model: Optional[str] = None, **kwargs: Any
    ) -> str:
        """执行子任务，返回它的最终文本。

        Args:
            task: 子任务描述。模型按 parameters 生成，理论上非空；空串仍要拦一道——
                没有任务描述的子 agent 只会对着空气开始编。
            model: 指定模型名，None 表示用工厂的默认模型。
            **kwargs: 忽略多余参数：模型的参数由它自己按 JSON Schema 生成，多给一个键
                不该让这一轮直接失败。

        Returns:
            子 agent 的最终回复文本；失败时返回**可读的错误说明**而不是抛异常——与其他
            工具一致：模型看到错误描述后可以换个思路重试。
        """
        if not task.strip():
            return "错误：task 不能为空，请把要交给子任务专员的事情写清楚。"

        provider: Optional[LLMProvider] = None
        try:
            # 1) 子 agent 有自己的 Provider：它可能换了模型，也可能按调用次数计费，
            #    用完即关，不与主 agent 的连接池混在一起。
            provider = self._provider_factory(model)

            # 2) 复制工具，跳过 spawn_subagent：注册表里那一份是"父辈"的（深度与当前
            #    不同），子 agent 要用的是下面按新深度现造的那一份。
            registry = ToolRegistry()
            for tool in self._tools_registry.iter_tools():
                if tool.name == self.name:
                    continue
                registry.register(tool)

            # 3) 还没到深度上限，就给子 agent 也配一个，它才能继续往下派活。
            if self.current_depth + 1 < self.max_depth:
                registry.register(
                    SpawnSubagentTool(
                        provider_factory=self._provider_factory,
                        # 往下传**复制好的这份**注册表，不含 spawn_subagent，
                        # 于是每深一层都只能看到自己那一份，不会回到父辈的注册表上。
                        tools_registry=registry,
                        workspace=self._workspace,
                        current_depth=self.current_depth + 1,
                        max_depth=self.max_depth,
                    )
                )

            # 4) 一次性 agent：不挂 sink（它的过程不该打到用户终端上）、不挂压缩器
            #    （历史只活这一次，没有"太长"的机会）、不写磁盘（见 DummySessionManager）。
            agent = AgentLoop(
                provider,
                registry,
                SubagentContext(self._workspace),
                max_steps=SUBAGENT_MAX_STEPS,
                session_manager=DummySessionManager(),
                session_key=f"subagent:{self.current_depth + 1}",
            )
            logger.info(
                "子任务开始（深度 %d/%d，工具 %d 个，%d 字）",
                self.current_depth + 1,
                self.max_depth,
                len(registry.list_tools()),
                len(task),
            )
            result = await agent.run(task)
            logger.info("子任务结束（返回 %d 字）", len(result))
            return result or "（子任务没有返回内容）"
        except Exception as exc:  # noqa: BLE001 - 与其他工具一致：把失败讲清楚交回模型
            logger.exception("子任务执行失败（深度 %d）", self.current_depth + 1)
            return f"错误：子任务执行失败 - {type(exc).__name__}: {exc}"
        finally:
            # 5) 关掉这个子 agent 的连接池，把连接还回去：不关的话每派生一次就漏一份，
            #    套娃几层之后进程里会攒下一堆闲置 HTTP 连接。
            closer = getattr(provider, "aclose", None) if provider is not None else None
            if callable(closer):
                try:
                    await closer()
                except Exception:  # noqa: BLE001 - 关连接失败不该盖住真正的执行结果
                    logger.debug("关闭子 agent 的 Provider 时出错", exc_info=True)
