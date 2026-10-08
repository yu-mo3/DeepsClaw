"""会话持久化。

把一轮轮对话落成磁盘上的 JSONL 文件，让"关掉终端再打开"不会丢掉上下文。

存储形态选 JSONL（每行一条 JSON）而不是一个大 JSON 数组，是为了配合对话的写入方式：
对话是**追加**出来的，每轮往文件尾添一条即可，不必把整个历史读出来、改完、再整体写
回去——那种"读-改-写"一旦被 Ctrl+C 打断，会把历史截断成半截文件；追加写最坏也只是
丢掉最后一条。

目录结构（所有数据都落在 workspace 下，不放项目根目录）::

    workspace/sessions/
    ├── cli_direct.jsonl
    └── feishu_user123.jsonl

session_key 里的 ":" 在文件名里换成 "_"（Windows 文件名不允许冒号），读取时再换回来，
所以 list_sessions 拿到的仍是 ``cli:direct`` 这种能直接用的 key。

已知取舍：这个替换**不可逆**——"cli:direct" 与 "cli_direct" 会落到同一个文件上。
本项目的 key 由自己的 channel 层生成（形如 ``cli:direct``），不会撞名；若将来要把
外部输入直接当 key 用，需要先换成转义编码。

本模块只做存取，不持有任何状态，也不做并发控制：单进程 CLI 顺序调用即可，真要多进程
同时写同一个会话文件时得另加文件锁。

典型用法::

    manager = SessionManager("workspace/sessions")
    manager.save_message("cli:direct", {"role": "user", "content": "你好"})
    history = manager.get_history("cli:direct")
"""

import json
import logging
import os
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

#: 会话文件的扩展名。list_sessions 按它筛选，写入时也用它。
SESSION_SUFFIX = ".jsonl"

#: session_key 里要替换掉的字符 -> 文件名里能用的字符。
#: Windows 文件名不允许 ":"，而本项目的 key 恰好用它分隔 channel 与用户。
_KEY_TO_FILE = {":": "_"}

#: 落盘时才写、读历史时要摘掉的字段。OpenAI 的 messages 不认这个键，
#: 原样回传给接口会被拒。
TIMESTAMP_FIELD = "timestamp"


class SessionManager:
    """会话历史的读写器。

    一个 session_key 对应一个 JSONL 文件，文件里每一行是一条消息。实例本身不缓存
    任何内容，所有信息都在磁盘上，因此进程重启后拿同一个 key 就能接着聊。
    """

    def __init__(self, sessions_dir: str = "workspace/sessions") -> None:
        """初始化并确保目录存在。

        Args:
            sessions_dir: 会话文件目录，默认 "workspace/sessions"，相对**进程当前
                目录**解析。目录不存在会自动创建（含中间层级），这样调用方不必先建
                目录，也不必为"第一次运行"写特判。
        """
        self.sessions_dir = sessions_dir
        os.makedirs(sessions_dir, exist_ok=True)

    def _get_session_path(self, session_key: str) -> str:
        """把 session_key 翻译成会话文件路径。

        非字符串或空 key 退化成默认会话名，而不是抛异常：key 是内部生成的，走到这
        一步说明调用方传错了，写进一个固定的兜底文件总好过让整轮对话失败。
        """
        name = session_key if isinstance(session_key, str) else ""
        for bad, safe in _KEY_TO_FILE.items():
            name = name.replace(bad, safe)
        if not name:
            logger.warning("session_key 为空（%r），已退回默认会话名", session_key)
            name = "default"
        return os.path.join(self.sessions_dir, name + SESSION_SUFFIX)

    def save_message(self, session_key: str, message: dict[str, Any]) -> None:
        """追加一条消息到会话文件。

        写入前打上时间戳：JSONL 也是给人排查问题用的，"这条是什么时候写的"往往就是
        关键线索，而历史里有它并不影响回传模型（读的时候会摘掉）。

        Args:
            session_key: 会话标识，如 "cli:direct"。
            message: 一条 OpenAI 格式的消息，如 ``{"role": "user", "content": "..."}``。
        """
        record = dict(message)
        record[TIMESTAMP_FIELD] = datetime.now().isoformat()

        path = self._get_session_path(session_key)
        try:
            # ensure_ascii=False：中文按原样落盘，文件能直接看，也省下转义后的体积。
            # 整体 dumps 成一行——内容里的换行会被转义成 \n，所以一条消息永远只占
            # 一行，JSONL 的"一行一条"才成立。
            line = json.dumps(record, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            # 消息里混进了不能序列化的对象（比如工具结果里的自定义类型）。只记日志：
            # 存不下总比让正在跑的会话崩掉强。
            logger.warning("消息无法序列化，已跳过本次保存: %s", exc)
            return

        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            logger.warning("写入会话文件 %s 失败: %s", path, exc)

    def get_history(self, session_key: str, limit: int | None = None) -> list[dict[str, Any]]:
        """读回会话历史。

        返回的消息**不含 timestamp**：它是本项目落盘时的附加字段，OpenAI 的 messages
        里没有这个键，原样回传会被接口以参数非法拒掉。

        Args:
            session_key: 会话标识。
            limit: 只取最近多少条，None 表示全部。恢复长会话时可以用它给上下文封顶
                ——不然聊得越久，每轮请求带的历史越长，迟早顶到模型的窗口上限。

        Returns:
            按写入顺序排列的消息列表；文件不存在或读不动时返回空列表（调用方不必
            区分"新会话"和"读取失败"，两者都从空历史开始）。
        """
        path = self._get_session_path(session_key)
        if not os.path.isfile(path):
            return []

        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as exc:
            logger.warning("读取会话文件 %s 失败: %s", path, exc)
            return []

        messages: list[dict[str, Any]] = []
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue  # 空行跳过：文件末尾的空行、手工编辑留下的空白行
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                # 半截写入（写文件时被强杀）会让某一行不是合法 JSON。丢掉这一行、
                # 保住其余历史，比整份历史都读不出来有用得多。
                logger.warning("会话文件 %s 第 %d 行不是合法 JSON，已跳过: %s", path, number, exc)
                continue
            if not isinstance(record, dict):
                logger.warning("会话文件 %s 第 %d 行不是消息对象，已跳过", path, number)
                continue
            record.pop(TIMESTAMP_FIELD, None)
            messages.append(record)

        if limit is not None and limit >= 0:
            return messages[-limit:] if limit else []
        return messages

    def clear(self, session_key: str) -> None:
        """删除会话文件，文件不存在时什么也不做。"""
        path = self._get_session_path(session_key)
        try:
            os.remove(path)
        except FileNotFoundError:
            logger.debug("会话文件 %s 不存在，无需清理", path)
        except OSError as exc:
            logger.warning("删除会话文件 %s 失败: %s", path, exc)

    def list_sessions(self) -> list[str]:
        """列出已有的会话 key。

        文件名里的 "_" 换回 ":"，与 _get_session_path 的替换互逆，返回值的形状和存进去
        时看到的 session_key 一致，可以直接拿去调 get_history。排序后返回，保证同样
        一批会话每次输出顺序一致。
        """
        try:
            names = sorted(os.listdir(self.sessions_dir))
        except OSError as exc:
            logger.warning("读取会话目录 %s 失败: %s", self.sessions_dir, exc)
            return []

        sessions: list[str] = []
        for name in names:
            if not name.endswith(SESSION_SUFFIX):
                continue
            # 以 .jsonl 结尾的目录名不算会话，这里只收文件。
            if not os.path.isfile(os.path.join(self.sessions_dir, name)):
                continue
            sessions.append(name[: -len(SESSION_SUFFIX)].replace("_", ":"))
        return sessions
