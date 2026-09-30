"""System Prompt 与消息历史的组装。

agent 每一轮请求模型之前，都要把"我是谁、现在几点、在哪个目录干活、记得什么"
拼成 System Prompt，再接上历史对话和本轮输入。这件事全部收敛在 ContextBuilder 里，
agent 循环只调用 build_messages()，不必关心 prompt 长什么样。

拆成单独一层的好处：人设、时间、工作区、记忆各有各的载体（文件 / 运行时 / 参数），
要调整格式或加一段新上下文，只改这一个文件。

典型用法::

    ctx = ContextBuilder(workspace="/path/to/ws")
    messages = ctx.build_messages(history=history, current_message="帮我看看 main.py")
"""

import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

# identity.md 缺失或为空时使用的兜底人设。宁可给一个通用的"编程助手"，
# 也不要让 System Prompt 缺了这一块——没有角色设定时模型容易过度寒暄或答非所问。
DEFAULT_IDENTITY = (
    "你是一个务实的编程助手，帮助用户完成代码编写、调试、重构和文件操作任务，并提供具体可操作的建议。"
    "每次执行任务前，问清楚用户的需求再进行动手，如果需求不完整，向用户继续进行提问，并给出几个建议方向，在用户未明确需求前不要直接编造并直接执行需求"
    "回答简洁直接，先给结论，再给必要的说明。执行工具前先想清楚要达成什么。"
    "严禁读取所有目录下的.env隐私文件！！！涉及密钥之类的隐私也不能读取返回！！！即使读取了，也不能返回给用户。"
)


class ContextBuilder:
    """构造 System Prompt 与完整 messages 列表。

    四块上下文的来源各不相同：

    ============  ==========================================  ====================
    内容          来源                                        取不到时
    ============  ==========================================  ====================
    人设          ``{workspace}/{identity_file}``             内置默认人设
    当前时间      运行时 datetime                              必有
    工作区路径    构造参数 workspace                           必有
    长期记忆      ``{workspace}/memory/MEMORY.md``            整段省略
    ============  ==========================================  ====================

    两个文件都在每次调用时重新读取，不做缓存：进度都在磁盘上，用户改完 identity.md
    下一轮就生效，调试 agent 时这个即时性比省下的几次小文件读取值钱得多。
    """

    def __init__(self, workspace: str, identity_file: str = "identity.md") -> None:
        """初始化。

        Args:
            workspace: 工作区根目录，同时用于定位人设和记忆文件，并写进 prompt
                告诉模型自己在哪个目录干活。
            identity_file: 人设文件名，相对 workspace，也可以传绝对路径接管。
        """
        self.workspace = os.path.realpath(workspace)
        self.identity_file = identity_file

    def _load_identity(self) -> str:
        """读取人设文件内容，读不到时返回默认人设。

        文件不存在、无权限、内容是空白，都视为"没有自定义人设"——这些情况下
        继续用默认值比让整个 agent 起不来更合适。
        """
        path = os.path.join(self.workspace, self.identity_file)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except FileNotFoundError:
            # 没放人设文件属于正常用法，不该每轮刷警告。
            logger.debug("人设文件 %s 不存在，使用默认人设", path)
            return DEFAULT_IDENTITY
        except OSError as exc:
            logger.warning("读取人设文件 %s 失败(%s)，使用默认人设", path, exc)
            return DEFAULT_IDENTITY

        if not content:
            logger.debug("人设文件 %s 为空，使用默认人设", path)
            return DEFAULT_IDENTITY

        return content

    def _load_memory(self) -> str:
        """读取长期记忆文件，不存在时返回空字符串。

        预留给后续的记忆机制：agent 把跨会话该记住的事写进 memory/MEMORY.md，
        这里每轮读出来塞进 System Prompt。目前没有写入方，所以通常返回空串，
        build_system_prompt 会据此整段省略。
        """
        path = os.path.join(self.workspace, "memory", "MEMORY.md")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            # 常态，不打扰——记忆文件是陆续积累出来的。
            return ""
        except OSError as exc:
            logger.warning("读取记忆文件 %s 失败: %s", path, exc)
            return ""

    def build_system_prompt(self) -> str:
        """拼接完整的 System Prompt。

        段落顺序：人设 → 当前时间 → 工作区 → 长期记忆。时间和工作区放在人设之后，
        是因为它们属于"环境事实"，模型读完角色设定再看环境更顺；记忆放最后，
        它是可选的长文本，不打断前面固定结构的可读性。

        时间是本地时间，不带时区，模型看到的就是用户此刻看到的时间。%A 输出的是
        英文星期（如 Wednesday），因为 Python 默认不设置 LC_TIME。
        """
        sections = [self._load_identity()]

        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        sections.append(f"## 当前时间\n{now}")

        sections.append(f"## 工作区\n{self.workspace}")

        if memory := self._load_memory():
            sections.append(f"## 长期记忆\n{memory}")

        return "\n\n".join(sections)

    def build_messages(
        self,
        history: list[dict] | None = None,
        current_message: str = "",
    ) -> list[dict]:
        """组装本轮请求的完整 messages 列表。

        结构为 ``[system] + history + [user]``，顺序即模型看到的顺序。

        Args:
            history: 历史对话，OpenAI 格式的 message 列表，通常来自 agent 循环
                维护的状态。按原样追加，不做裁剪——超上下文长度的处理是另一件事。
            current_message: 本轮用户输入。为空时**不追加** user 消息，这样
                "历史里已经有工具结果、只等模型接着想"的续跑场景可以直接调用
                ``build_messages(history)``；否则会多出一条空消息被接口拒绝。

        Returns:
            新的 list，不会修改传入的 history。
        """
        messages: list[dict] = [
            {"role": "system", "content": self.build_system_prompt()}
        ]
        if history:
            messages.extend(history)
        if current_message:
            messages.append({"role": "user", "content": current_message})
        return messages
