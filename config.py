"""运行配置。

所有可变参数都放在项目根目录的 .env 里，代码只读不写。密钥不进代码、不进版本库，
换环境（换 key、换模型、换工作区）只需要改文件，不用动代码。

配置在 Config.from_env() 里**一次性读取并校验**，之后整个进程用的都是这个对象。
缺 key、步数写成汉字这类问题会在这里当场报错并说清楚是哪一项，而不是等到第一次
调用模型时冒出一句听不懂的 401。

典型用法::

    from config import Config
    cfg = Config.from_env()
    provider = OpenAICompatProvider(
        api_key=cfg.api_key, base_url=cfg.base_url, model=cfg.model,
        timeout=cfg.timeout, extra_body=cfg.extra_body,
    )
"""

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

#: 项目根目录，即本文件所在目录。.env 和相对路径都以它为基准。
PROJECT_ROOT = Path(__file__).resolve().parent

#: agent 运行数据的落点（会话历史、长期记忆等都放这里），相对项目根目录。
#: 与代码、文档分开：迁移或备份时拷走这一个目录就够了，仓库根也不会被运行时文件污染。
DATA_DIR = "workspace"

#: 长期记忆文件的默认位置，相对项目根目录。放在数据目录下，agent 能通过文件工具
#: 读到它、也能由 save_memory 工具写入它。
DATA_MEMORY_FILE = f"{DATA_DIR}/memory/MEMORY.md"

#: 默认接 DeepSeek，换兼容接口只改 .env，不用改代码。
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

#: 子智能体（spawn_subagent）默认走阿里云百炼。它天然是另一个服务商，所以有自己的
#: 三个配置项，不与主模型共用——"主模型用谁"和"子任务用谁"是两件事，混在一起就没法
#: 单独把子任务换到更便宜的模型上。百炼同样提供 OpenAI 兼容接口，协议不用改。
DEFAULT_SUBAGENT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_SUBAGENT_MODEL = "qwen3.8-flash"

_ENV_FILE = PROJECT_ROOT / ".env"

#: 提示用的常用日志级别，按严重程度递减
_COMMON_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _get(*names: str) -> str | None:
    """按顺序取第一个非空环境变量，全空则返回 None。

    支持多个名字是为了让 .env 里写 LLM_API_KEY 或 DEEPSEEK_API_KEY 都能生效，
    按传入顺序优先。
    """
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


def _resolve_path(raw: str) -> Path:
    """把配置里的路径解析成绝对路径。

    相对路径以**项目根目录**为基准而不是当前工作目录——否则从别的目录执行
    ``python DeepsClaw/main.py``，workspace 会静默变成那个目录，工具沙箱的范围
    跟着变，行为看起来就像随机。
    """
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass
class Config:
    """一份完整的运行配置。

    Attributes:
        api_key: 模型服务密钥，必填。
        base_url: 接口地址。
        model: 默认模型名。
        workspace: agent 的工作区根目录，文件工具只能在这个范围内读写。
        identity_file: 人设文件名，相对 workspace。
        memory_file: 长期记忆文件（MEMORY.md）的路径。默认落在 workspace 数据目录下，
            每次拼 System Prompt 时读取；为空字符串表示不使用长期记忆。
        token_budget: 单次请求的历史 token 预算（粗估）。超过它时自动把中间那段旧消息
            摘要成一条 system 消息，只保留 System Prompt 与最近的对话。为 None 表示关闭。
        bocha_api_key: 博查搜索 API key，供联网搜索工具使用；为空则联网搜索不可用，
            但不影响 agent 启动。
        qq_app_id: QQ 官方机器人的 AppID。与 qq_app_secret 一起为空时**不启用 QQ 渠道**，
            程序只跑 CLI；这也是没装 qq-botpy 的人不受影响的原因（渠道是懒加载的）。
        qq_app_secret: QQ 官方机器人的 AppSecret。属于密钥，只在本进程内使用，不进日志。
        subagent_api_key: 子智能体（spawn_subagent 派生的临时 agent）用的密钥，默认接
            阿里云百炼。**为空则不注册 spawn_subagent 工具**——主 agent 少了这个能力，
            其余一切照常，不需要它的人不必配。
        subagent_base_url: 子智能体的接口地址，默认百炼的 OpenAI 兼容入口。
        subagent_model: 子智能体默认模型名。spawn_subagent 的 model 参数可以按次覆盖它。
        max_steps: 单轮对话最多调用模型的次数，防止任务不收敛时无限跑。
        timeout: 单次模型请求超时（秒）。
        extra_body: 透传给接口的额外请求体，如 DeepSeek 思考模式开关。
        log_level: 日志级别，如 INFO / DEBUG。
    """

    api_key: str
    base_url: str
    model: str
    workspace: Path
    identity_file: str
    memory_file: Path
    token_budget: int | None
    bocha_api_key: str | None
    qq_app_id: str | None
    qq_app_secret: str | None
    subagent_api_key: str | None
    subagent_base_url: str
    subagent_model: str
    max_steps: int
    timeout: float
    extra_body: dict | None
    log_level: str

    def __repr__(self) -> str:
        """打印配置时遮住密钥，避免它随日志或报错信息泄漏出去。"""
        return (
            f"Config(api_key={_mask(self.api_key)!r}, base_url={self.base_url!r}, "
            f"model={self.model!r}, workspace={str(self.workspace)!r}, "
            f"identity_file={self.identity_file!r}, memory_file={str(self.memory_file)!r}, "
            f"token_budget={self.token_budget!r}, "
            f"bocha_api_key={_mask(self.bocha_api_key)!r}, "
            f"qq_app_id={self.qq_app_id!r}, qq_app_secret={_mask(self.qq_app_secret)!r}, "
            f"subagent_api_key={_mask(self.subagent_api_key)!r}, "
            f"subagent_base_url={self.subagent_base_url!r}, "
            f"subagent_model={self.subagent_model!r}, "
            f"max_steps={self.max_steps}, timeout={self.timeout}, "
            f"extra_body={self.extra_body!r}, log_level={self.log_level!r})"
        )

    @classmethod
    def from_env(cls, env_file: Path | str | None = None) -> "Config":
        """读取 .env 与环境变量，构造配置。

        .env 只是补充：**已存在的真实环境变量优先**，不会被文件覆盖，方便临时用
        ``LLM_MODEL=xxx python main.py`` 覆盖单项做实验。

        Args:
            env_file: 指定 .env 路径，默认项目根目录下的 .env。主要给测试用。

        Returns:
            校验通过的配置对象。

        Raises:
            ValueError: 缺少必填项，或某项格式不对（如步数不是正整数）。
        """
        load_dotenv(dotenv_path=env_file or _ENV_FILE, override=False)

        api_key = _get("LLM_API_KEY", "DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError(
                f"未配置 API key。请在 {_ENV_FILE} 中设置 LLM_API_KEY=你的密钥"
                "（可参照同目录下的 .env.example），或直接设置同名环境变量。"
            )

        return cls(
            api_key=api_key,
            base_url=_get("LLM_BASE_URL", "DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL,
            model=_get("LLM_MODEL", "DEEPSEEK_MODEL") or DEFAULT_MODEL,
            workspace=_resolve_path(_get("AGENT_WORKSPACE") or "."),
            identity_file=_get("AGENT_IDENTITY_FILE") or "identity.md",
            # 记忆文件不跟随 workspace：它属于"这个项目的数据"，无论 agent 去哪个目录
            # 干活都该读到同一份记忆，所以固定按项目根目录解析。
            memory_file=_resolve_path(_get("AGENT_MEMORY_FILE") or DATA_MEMORY_FILE),
            # 0 表示"未配置"，转成 None 关闭自动压缩：它会改写发给模型的历史，
            # 属于"改变行为"的功能，交给用户显式开启（如 AGENT_TOKEN_BUDGET=8000）。
            token_budget=_get_int("AGENT_TOKEN_BUDGET", 0, minimum=0) or None,
            bocha_api_key=_get("BOCHA_API_KEY"),
            # QQ 渠道是可选功能：两个值都配了才启用。只配一半属于笔误，起不来但又不
            # 报错最难查，所以这里对"配了一半"明确告警，然后按未启用处理。
            qq_app_id=_get("QQ_APP_ID"),
            qq_app_secret=_get("QQ_APP_SECRET"),
            # 子智能体：密钥缺失时功能整体关闭（见 SUBAGENT_TOOL 的装配判断），
            # 地址与模型名给了默认值，所以只要填一个 key 就能用。
            subagent_api_key=_get("SUBAGENT_API_KEY"),
            subagent_base_url=_get("SUBAGENT_BASE_URL") or DEFAULT_SUBAGENT_BASE_URL,
            subagent_model=_get("SUBAGENT_MODEL") or DEFAULT_SUBAGENT_MODEL,
            max_steps=_get_int("AGENT_MAX_STEPS", 50, minimum=1),
            timeout=_get_float("LLM_TIMEOUT", 120.0, minimum=0.1),
            extra_body=_get_json("LLM_EXTRA_BODY"),
            log_level=_get_log_level(),
        )


def _mask(secret: str | None) -> str:
    """把密钥裁成可安全打印的形式，未配置时返回固定文案。

    配置对象会被 debug 日志和报错信息带出去，密钥必须在这里就被遮住，
    而不是指望调用方记得别打。
    """
    if not secret:
        return "(未配置)"
    return f"{secret[:6]}…" if len(secret) > 12 else "***"


def _get_log_level() -> str:
    """读取日志级别，取值不合法时退回 INFO 并提示。

    这里不抛异常：日志级别写错不该让整个程序起不来。但不校验也不行——
    ``logging.basicConfig(level="VERBOSE")`` 会直接抛 ValueError: Unknown level，
    报错还发生在启动流程中段，看起来像是别的地方坏了。
    """
    raw = _get("LOG_LEVEL")
    if raw is None:
        return "INFO"
    level = raw.upper()
    if level not in logging.getLevelNamesMapping():
        print(
            f"警告：LOG_LEVEL={raw!r} 不是合法日志级别，已按 INFO 处理。"
            f"可选值：{'/'.join(_COMMON_LOG_LEVELS)}",
            file=sys.stderr,
        )
        return "INFO"
    return level


def _get_int(name: str, default: int, minimum: int | None = None) -> int:
    """读取整数配置项，非法值直接报错并指明是哪个变量。"""
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"配置项 {name} 必须是整数，当前值为 {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ValueError(f"配置项 {name} 不能小于 {minimum}，当前值为 {value}")
    return value


def _get_float(name: str, default: float, minimum: float | None = None) -> float:
    """读取浮点配置项，非法值直接报错并指明是哪个变量。"""
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"配置项 {name} 必须是数字，当前值为 {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ValueError(f"配置项 {name} 不能小于 {minimum}，当前值为 {value}")
    return value


def _get_json(name: str) -> dict | None:
    """读取 JSON 对象配置项，未设置时返回 None。

    解析失败不在启动时兜住的话，错误会一路带到请求里变成一个看不懂的参数错误，
    所以这里直接失败并回显原值。
    """
    raw = _get(name)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"配置项 {name} 不是合法 JSON（{exc}），当前值为 {raw!r}") from None
    if not isinstance(value, dict):
        raise ValueError(f"配置项 {name} 必须是 JSON 对象，当前值为 {raw!r}")
    return value
