"""运行配置：**一个 config.json 管全部**。

以前配置散在 `.env` 的一堆变量里（模型一套、子智能体一套、QQ 一套、MCP 再一套），
接第二个模型或第二个渠道就要在几十行注释里找该加哪个前缀。现在主配置集中在项目根目录的
**config.json**：模型、渠道、子智能体、MCP Server、工具与运行参数都在一处，改完一眼能看全。

## 三级优先：环境变量 > .env > config.json > 内置默认值

三条来源同时生效，查找顺序为：

1. **环境变量**（最高）——`LLM_MODEL=xxx python main.py` 可以临时压掉文件里的配置，
   容器/CI 里也不必改文件；
2. **.env**——老配置继续有效，升级不用一次性搬家；密钥也可以只放这里（.env 已进
   .gitignore，config.json 未必）；
3. **config.json**——日常改配置的地方；
4. **内置默认值**——最后兜底，保证"什么都不配"也能跑起来（除了密钥）。

合并是**逐项**的：config.json 里没写的项照样能从 .env 或默认值取到，所以可以一边迁移
一边用，不必一次改完。

## config.json 长什么样

最小可用（只要密钥，其余全用默认值）::

    {"api_key": "sk-xxx", "base_url": "https://api.deepseek.com/v1",
     "models": {"main": "deepseek-chat"}}

完整一点::

    {
      "api_key": "sk-xxx",
      "base_url": "https://api.deepseek.com/v1",
      "models": {
        "main": "deepseek-chat",
        "subagent": "qwen3.8-flash",
        "cheap": "gpt-4o-mini"
      },
      "workspace": ".",
      "identity_file": "identity.md",
      "max_iterations": 50,
      "timeout": 120,
      "log_level": "INFO",
      "memory": {"file": "workspace/memory/MEMORY.md", "token_budget": 24000},
      "tools": {"bocha_api_key": "sk-xxx"},
      "subagent": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
      "channels": {
        "qq":  {"app_id": "102xxx", "app_secret": "xxx"},
        "web": {"enabled": false, "host": "0.0.0.0", "port": 8080}
      },
      "mcp_servers": {
        "poetry": {"command": "python", "args": ["mcp_servers/poetry_server.py"],
                   "description": "古诗词查询"}
      }
    }

**models 里除了 main / subagent，其余键都是"给 spawn_subagent 按次指定用的别名"**：
模型调 `spawn_subagent(task, model="cheap")` 时，"cheap" 会被翻译成这里配的真实模型名，
于是"用便宜模型干杂活"不必让模型自己记一串模型 ID。

也兼容只写一个模型名的写法（老配置抄过来能直接用）::

    {"api_key": "sk-xxx", "base_url": "https://api.siliconflow.cn/v1",
     "model": "Pro/zai-org/GLM-5"}

## 典型用法::

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
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

#: 项目根目录，即本文件所在目录。config.json、.env 与相对路径都以它为基准。
PROJECT_ROOT = Path(__file__).resolve().parent

#: agent 运行数据的落点（会话历史、长期记忆等都放这里），相对项目根目录。
DATA_DIR = "workspace"

#: 长期记忆文件的默认位置，相对项目根目录。
DATA_MEMORY_FILE = f"{DATA_DIR}/memory/MEMORY.md"

#: 主配置文件。写成 json 而不是继续堆 .env：配置项一多，"哪几项属于同一个功能"在
#: 扁平的环境变量里根本看不出来。
CONFIG_FILE = PROJECT_ROOT / "config.json"

#: 兼容用：老配置仍从这里读，且优先级高于 config.json（方便"文件写通用值、.env 放密钥"）。
_ENV_FILE = PROJECT_ROOT / ".env"

#: 默认接 DeepSeek，换兼容接口只改配置，不用改代码。
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

#: 子智能体（spawn_subagent）默认走阿里云百炼：它天然是另一个服务商，所以有自己的一组
#: 默认值，不与主模型共用——"主模型用谁"和"子任务用谁"是两件事。
DEFAULT_SUBAGENT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_SUBAGENT_MODEL = "qwen3.8-flash"

#: 提示用的常用日志级别，按严重程度递减
_COMMON_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(ValueError):
    """配置有问题，且问题**出在用户写的文件里**。

    单独一个类型是为了让报错分得清责任：捕获 ConfigError 就是"让用户去改配置"，
    而其他 ValueError 多半是程序自己的 bug。main() 只拦这一类，把它变成人话。
    """


def load_config_file(path: Path | str | None = None) -> dict:
    """读取 config.json，返回一个 dict；文件不存在就是空 dict。

    两种失败分别对待：

    - **文件不存在**：正常情况（有人就是只用 .env），静默返回空 dict；
    - **文件存在但不是合法 JSON**：这里必须报错。悄悄忽略一个写错的文件，会让用户面对
      "我明明配了却不生效"这种最难查的现象——宁可当场起不来，也不要在几小时后才发现。

    Args:
        path: 配置文件路径，默认项目根目录下的 config.json。

    Returns:
        配置内容；文件不存在时为空 dict。

    Raises:
        ConfigError: 文件存在但读不动，或内容不是 JSON 对象。
    """
    target = Path(path) if path is not None else CONFIG_FILE
    if not target.is_file():
        return {}
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"读取配置文件 {target} 失败：{exc}") from None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"配置文件 {target} 不是合法 JSON：{exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件 {target} 的顶层必须是一个 JSON 对象")
    return data


def _get_json_value(data: dict, key: str) -> str | None:
    """从 config.json 里取一个值并转成字符串，取不到返回 None。

    数字与布尔也转字符串，是为了让"从 json 取"和"从环境变量取"两条路产出同一种类型，
    下游的 _get_int / _get_float 不必分两套。

    Args:
        data: config.json 的内容。
        key: 键名，支持 "memory.file" 这样的点号路径。

    Returns:
        字符串形式的值；键不存在或值为 null 时返回 None。
    """
    node: object = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if node is None or isinstance(node, (dict, list)):
        return None
    if isinstance(node, bool):
        return "true" if node else "false"
    return str(node)


def _pick(env_names: tuple[str, ...], data: dict, json_key: str) -> str | None:
    """按"环境变量 > .env > config.json"取一个字符串值。

    三级优先集中在这一个函数里，是为了让每一处配置的取值规则**长得一样**：
    读代码的人不必逐个字段去回想"这一项到底会不会被环境变量盖掉"。

    Args:
        env_names: 环境变量名（.env 已经由 load_dotenv 灌进环境里，所以两者同一来源）。
        data: config.json 的内容。
        json_key: config.json 里的键名，支持点号路径。

    Returns:
        第一个取到的非空值；三处都没有则返回 None。
    """
    value = _get(*env_names)
    if value is not None:
        return value
    return _get_json_value(data, json_key)

#: 本次运行实际用到的配置文件路径（None 表示没有用 config.json）。由 from_env 填充，
#: 供启动横幅展示"配置从哪来"——排障时第一个要问的就是这个。
_loaded_config_path: Path | None = None


@dataclass
class Config:
    """一份完整的运行配置。

    字段按"干什么用的"分组：模型 → agent 行为 → 记忆 → 渠道 → 子智能体 → MCP。

    Attributes:
        api_key: 主模型密钥，必填（三处都没有就起不来）。
        base_url: 主模型接口地址。
        model: 主模型名。
        models: 模型别名表，形如 {"main": ..., "subagent": ..., "cheap": ...}。
            main / subagent 有固定含义，其余键供 spawn_subagent 的 model 参数按名引用。
        workspace: agent 的工作区根目录，文件工具只能在这个范围内读写。
        identity_file: 人设文件名，相对 workspace。
        memory_file: 长期记忆文件（MEMORY.md）路径。为空字符串表示不使用长期记忆。
        token_budget: 单次请求的历史 token 预算（粗估）。为 None 表示关闭自动压缩。
        bocha_api_key: 博查搜索 API key；为空则联网搜索工具不可用，但不影响启动。
        qq_app_id: QQ 官方机器人 AppID。与 qq_app_secret 一起为空时不启用 QQ 渠道。
        qq_app_secret: QQ 机器人 AppSecret，密钥，不进日志。
        feishu_app_id: 飞书机器人 AppID。两项都配了才启用飞书渠道（渠道实现待接入）。
        feishu_app_secret: 飞书机器人 AppSecret，密钥。
        web_enabled: 是否启用 Web 渠道（渠道实现待接入）。
        web_host: Web 渠道监听地址。
        web_port: Web 渠道监听端口。
        subagent_api_key: 子智能体用的密钥。为空则**不注册 spawn_subagent 工具**。
        subagent_base_url: 子智能体接口地址，默认百炼的 OpenAI 兼容入口。
        subagent_model: 子智能体默认模型名，可被 spawn_subagent 的 model 参数按次覆盖。
        mcp_servers: 要连接的 MCP Server，形如 {"名字": {"command": ..., "args": [...]}}。
        max_steps: 单轮对话最多调用模型的次数，防止任务不收敛时无限跑。
        timeout: 单次模型请求超时（秒）。
        extra_body: 透传给接口的额外请求体，如思考模式开关。
        log_level: 日志级别，如 INFO / DEBUG。
        config_file: 本次实际读取的 config.json 路径，没用到就是 None（横幅会显示它）。
    """

    api_key: str
    base_url: str
    model: str
    models: dict[str, str]
    workspace: Path
    identity_file: str
    memory_file: Path
    token_budget: int | None
    bocha_api_key: str | None
    qq_app_id: str | None
    qq_app_secret: str | None
    feishu_app_id: str | None
    feishu_app_secret: str | None
    web_enabled: bool
    web_host: str
    web_port: int
    subagent_api_key: str | None
    subagent_base_url: str
    subagent_model: str
    mcp_servers: dict[str, dict]
    max_steps: int
    timeout: float
    extra_body: dict | None
    log_level: str
    config_file: Path | None

    @property
    def has_subagent(self) -> bool:
        """子智能体能力是否可用（配了密钥才算）。"""
        return bool(self.subagent_api_key)

    def resolve_model(self, alias: str | None) -> str | None:
        """把模型别名翻译成真实模型名。

        模型给的参数是给人看的短名（"cheap"、"fast"），而不是一串厂商模型 ID；这里把
        `models` 里的别名翻成真名。找不到别名时**原样返回**——用户也可能直接写完整
        模型名，而那样更明确，不该被拦。

        Args:
            alias: 别名或完整模型名；None 表示"不指定"。

        Returns:
            真实模型名；alias 为 None 时返回 None。
        """
        if alias is None:
            return None
        return self.models.get(alias, alias)

    def __repr__(self) -> str:
        """打印配置时遮住密钥，避免它随日志或报错信息泄漏出去。"""
        return (
            f"Config(api_key={_mask(self.api_key)!r}, base_url={self.base_url!r}, "
            f"model={self.model!r}, models={sorted(self.models)!r}, "
            f"workspace={str(self.workspace)!r}, "
            f"identity_file={self.identity_file!r}, memory_file={str(self.memory_file)!r}, "
            f"token_budget={self.token_budget!r}, "
            f"bocha_api_key={_mask(self.bocha_api_key)!r}, "
            f"qq_app_id={self.qq_app_id!r}, qq_app_secret={_mask(self.qq_app_secret)!r}, "
            f"feishu_app_id={self.feishu_app_id!r}, "
            f"feishu_app_secret={_mask(self.feishu_app_secret)!r}, "
            f"web_enabled={self.web_enabled!r}, web_host={self.web_host!r}, "
            f"web_port={self.web_port!r}, "
            f"subagent_api_key={_mask(self.subagent_api_key)!r}, "
            f"subagent_base_url={self.subagent_base_url!r}, "
            f"subagent_model={self.subagent_model!r}, "
            f"mcp_servers={sorted(self.mcp_servers)!r}, "
            f"max_steps={self.max_steps}, timeout={self.timeout}, "
            f"extra_body={self.extra_body!r}, log_level={self.log_level!r}, "
            f"config_file={str(self.config_file) if self.config_file else None!r})"
        )

    @classmethod
    def from_env(cls, env_file: Path | str | None = None, config_file: Path | str | None = None) -> "Config":
        """读取 config.json / .env / 环境变量，合成一份配置。

        查找顺序见模块文档：**环境变量 > .env > config.json > 内置默认值**，逐项合并。
        .env 由 load_dotenv 灌进环境（override=False，即已存在的真实环境变量优先），
        所以前两级在代码里就是同一次 os.environ 查询。

        Args:
            env_file: 指定 .env 路径，默认项目根目录下的 .env。主要给测试用。
            config_file: 指定 config.json 路径，默认项目根目录下的 config.json。

        Returns:
            校验通过的配置对象。

        Raises:
            ConfigError: 缺少主模型密钥，或配置文件读不动 / 不是合法 JSON。
        """
        global _loaded_config_path

        data = load_config_file(config_file)
        # config_file 的语义是"实际读到了哪个文件"，所以文件不存在时必须是 None——
        # 否则横幅会显示一个根本没被读取的来源，排障时反而把人带偏。
        candidate = Path(config_file) if config_file is not None else CONFIG_FILE
        _loaded_config_path = candidate if candidate.is_file() else None
        load_dotenv(dotenv_path=env_file or _ENV_FILE, override=False)

        # 主模型密钥：环境变量/.env 优先，其次 config.json 顶层。
        api_key = _get("LLM_API_KEY", "DEEPSEEK_API_KEY") or _get_json_value(data, "api_key")
        if not api_key:
            raise ConfigError(
                f"未配置模型密钥。两种配法（选一种即可）：\n"
                f"  1. 在 {CONFIG_FILE} 里写 {{\"api_key\": \"你的密钥\", "
                f"\"base_url\": \"https://api.deepseek.com/v1\", "
                f"\"models\": {{\"main\": \"deepseek-chat\"}}}}；\n"
                f"  2. 或在 {_ENV_FILE} 里设置 LLM_API_KEY=你的密钥。"
            )

        # models 段支持对象与字符串两种写法，_resolve_models 已经把它归一到"别名 -> 模型名"。
        # 主模型在这里从别名表取（models.main），**不能**用 _pick(data, "models.main")：
        # 当 models 写成字符串时，"models.main" 这个点号路径在 JSON 里并不存在，
        # 会静默取不到、悄悄退回默认模型——配了却不生效是最难查的一类问题。
        models = _resolve_models(data)
        subagent_model = (
            _get("SUBAGENT_MODEL") or models.get("subagent") or DEFAULT_SUBAGENT_MODEL
        )

        web_enabled = (
            _pick_bool(("WEB_ENABLED",), data, "channels.web.enabled") or False
        )

        return cls(
            api_key=api_key,
            base_url=_pick(("LLM_BASE_URL", "DEEPSEEK_BASE_URL"), data, "base_url")
            or DEFAULT_BASE_URL,
            model=
            _get("LLM_MODEL", "DEEPSEEK_MODEL") or models.get("main") or DEFAULT_MODEL,
            models=models,
            workspace=_resolve_path(_pick(("AGENT_WORKSPACE",), data, "workspace") or "."),
            identity_file=_pick(("AGENT_IDENTITY_FILE",), data, "identity_file") or "identity.md",
            memory_file=_resolve_path(
                _pick(("AGENT_MEMORY_FILE",), data, "memory.file") or DATA_MEMORY_FILE
            ),
            token_budget=_pick_int(("AGENT_TOKEN_BUDGET",), data, "memory.token_budget", 0, minimum=0)
            or None,
            bocha_api_key=_pick(("BOCHA_API_KEY",), data, "tools.bocha_api_key"),
            qq_app_id=_pick(("QQ_APP_ID",), data, "channels.qq.app_id"),
            qq_app_secret=_pick(("QQ_APP_SECRET",), data, "channels.qq.app_secret"),
            feishu_app_id=_pick(("FEISHU_APP_ID",), data, "channels.feishu.app_id"),
            feishu_app_secret=_pick(("FEISHU_APP_SECRET",), data, "channels.feishu.app_secret"),
            web_enabled=web_enabled,
            web_host=_pick(("WEB_HOST",), data, "channels.web.host") or "127.0.0.1",
            web_port=_pick_int(("WEB_PORT",), data, "channels.web.port", 8080, minimum=1),
            # 子智能体：密钥缺失时功能整体关闭；地址与模型名给了默认值，所以填一个 key 就能用。
            subagent_api_key=_pick(("SUBAGENT_API_KEY",), data, "subagent.api_key") or api_key,
            subagent_base_url=_pick(("SUBAGENT_BASE_URL",), data, "subagent.base_url")
            or DEFAULT_SUBAGENT_BASE_URL,
            subagent_model=subagent_model,
            mcp_servers=_resolve_mcp_servers(data),
            max_steps=_pick_int(("AGENT_MAX_STEPS",), data, "max_iterations", 50, minimum=1),
            timeout=_pick_float(("LLM_TIMEOUT",), data, "timeout", 120.0, minimum=0.1),
            extra_body=_pick_json(("LLM_EXTRA_BODY",), data, "extra_body"),
            log_level=_get_log_level(_get_json_value(data, "log_level")),
            config_file=_loaded_config_path,
        )


def _resolve_models(data: dict) -> dict[str, str]:
    """把 config.json 里的 models 段整理成"别名 -> 模型名"。

    支持两种形状：

    - 对象：`{"main": "deepseek-chat", "cheap": "gpt-4o-mini"}` —— 别名就是键名；
    - 字符串：`"models": "Pro/zai-org/GLM-5"` —— 视为只配了 main，等价于
      `{"main": "Pro/zai-org/GLM-5"}`。允许这种简写是因为"只用一个模型"是最常见的
      情况，逼用户写一层对象纯属添乱。

    Args:
        data: config.json 的内容。

    Returns:
        别名 -> 模型名；没配任何模型时为空 dict。
    """
    raw = data.get("models")
    if isinstance(raw, str):
        text = raw.strip()
        return {"main": text} if text else {}
    if isinstance(raw, dict):
        return {
            str(alias): str(name)
            for alias, name in raw.items()
            if name is not None and str(name).strip()
        }
    return {}

def _get(*names: str) -> str | None:
    """按顺序取第一个非空环境变量，全空则返回 None。

    支持多个名字是为了让 .env 里写 LLM_API_KEY 或 DEEPSEEK_API_KEY 都能生效，按传入顺序优先。
    """
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


def _resolve_path(raw: str) -> Path:
    """把配置里的路径解析成绝对路径。

    相对路径以**项目根目录**为基准而不是当前工作目录——否则从别的目录执行
    `python DeepsClaw/main.py`，workspace 会静默变成那个目录，工具沙箱的范围跟着变，
    行为看起来就像随机。
    """
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _mask(secret: str | None) -> str:
    """把密钥裁成可安全打印的形式，未配置时返回固定文案。

    配置对象会被 debug 日志和报错信息带出去，密钥必须在这里就被遮住，
    而不是指望调用方记得别打。
    """
    if not secret:
        return "(未配置)"
    return f"{secret[:6]}…" if len(secret) > 12 else "***"


def _pick_int(
    env_names: tuple[str, ...], data: dict, json_key: str, default: int,
    minimum: int | None = None,
) -> int:
    """按三级优先取一个整数配置项，非法值直接报错并指明是哪一项。"""
    raw = _pick(env_names, data, json_key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"配置项 {env_names[0]} / {json_key} 必须是整数，当前值为 {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"配置项 {env_names[0]} / {json_key} 不能小于 {minimum}，当前值为 {value}")
    return value


def _pick_float(
    env_names: tuple[str, ...], data: dict, json_key: str, default: float,
    minimum: float | None = None,
) -> float:
    """按三级优先取一个浮点配置项，非法值直接报错并指明是哪一项。"""
    raw = _pick(env_names, data, json_key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"配置项 {env_names[0]} / {json_key} 必须是数字，当前值为 {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"配置项 {env_names[0]} / {json_key} 不能小于 {minimum}，当前值为 {value}")
    return value


def _pick_bool(env_names: tuple[str, ...], data: dict, json_key: str) -> bool | None:
    """按三级优先取一个布尔配置项；三处都没有时返回 None。

    JSON 里可以直接写 true / false（_get_json_value 会转成 "true"/"false"），环境变量里
    常见的 1/0、yes/no、on/off 一并认，免得用户为了写个"开"去查该写哪个词。
    """
    raw = _pick(env_names, data, json_key)
    if raw is None:
        return None
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


def _pick_json(env_names: tuple[str, ...], data: dict, json_key: str) -> dict | None:
    """按三级优先取一个 JSON 对象配置项。

    两条来源的形状不一样，所以这里不能复用 _pick：

    - **环境变量 / .env** 里那一份是**字符串**（形如 {"thinking": {"type": "enabled"}}），要解析；
    - **config.json** 里那一份已经是对象，直接用。

    遍历 config.json 的工具函数（_get_json_value）只认标量，遇到对象会返回 None，
    因此这一项在 json 侧必须单独取——否则表现是"json 里明明写了 extra_body，
    运行时却是 None"，即"配了不生效"，最难查的那一类。
    """
    raw = _get(*env_names)
    if raw is not None:
        try:
            parsed = json.loads(raw)
        except ValueError:
            print(f"警告：{env_names[0]} 不是合法 JSON，已忽略该项。", file=sys.stderr)
            return None
        if isinstance(parsed, dict):
            return parsed
        print(f"警告：{env_names[0]} 必须是 JSON 对象，已忽略该项。", file=sys.stderr)
        return None

    value = data
    for part in json_key.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    if value is None:
        return None
    if not isinstance(value, dict):
        print(f"警告：config.json 的 {json_key} 必须是对象，已忽略该项。", file=sys.stderr)
        return None
    return value


def _get_log_level(from_json: str | None = None) -> str:
    """读取日志级别，取值不合法时退回 INFO 并提示。

    这里不抛异常：日志级别写错不该让整个程序起不来。但不校验也不行——
    `logging.basicConfig(level="VERBOSE")` 会直接抛 ValueError: Unknown level，报错还发生在
    启动流程中段，看起来像是别的地方坏了。
    """
    raw = _get("LOG_LEVEL") or (from_json or "").strip() or None
    if raw is None:
        return "DEBUG"
    level = raw.upper()
    if level not in logging.getLevelNamesMapping():
        print(
            f"警告：日志级别 {raw!r} 不合法，已按 INFO 处理。"
            f"可选值：{'/'.join(_COMMON_LOG_LEVELS)}",
            file=sys.stderr,
        )
        return "INFO"
    return level


def _resolve_mcp_servers(data: dict) -> dict[str, dict]:
    """整理要连接的 MCP Server，config.json 里的优先。

    config.json 里就是现成的对象，直接用（还会顺手补一个空的 args）：:

        "mcp_servers": {
          "poetry": {"command": "python", "args": ["mcp_servers/poetry_server.py"],
                     "description": "古诗词查询"}
        }

    `description` 是给人看的备注，MCP 客户端会忽略它——写在这里是为了让配置自解释。

    环境变量 MCP_SERVERS 仍支持（老配置不改也能用），两种写法：
    简单式 `MCP_SERVERS=poetry=python mcp_servers/poetry_server.py`（多个用 ; 分隔），
    或完整 JSON。**环境变量优先于 config.json**，与其它配置项一致。

    Returns:
        server 名 -> 配置（至少含 command / args）；没配时为空 dict。
    """
    from_env_text = _get("MCP_SERVERS")
    if from_env_text:
        parsed = _parse_mcp_env(from_env_text)
        if parsed:
            return parsed
        # 环境变量写了但解析不出来：继续用 config.json 的，别让一个笔误把功能整个关掉。

    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}
    result: dict[str, dict] = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            print(f"警告：mcp_servers.{name} 必须是对象，已跳过。", file=sys.stderr)
            continue
        command = cfg.get("command")
        if not command:
            print(f"警告：mcp_servers.{name} 缺少 command，已跳过。", file=sys.stderr)
            continue
        result[str(name)] = {
            **cfg,
            "command": str(command),
            "args": [str(a) for a in cfg.get("args") or []],
        }
    return result


def _parse_mcp_env(text: str) -> dict[str, dict]:
    """解析 MCP_SERVERS 环境变量（简单式或 JSON），解析不出来就返回空 dict。"""
    text = text.strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            print(f"警告：MCP_SERVERS 不是合法 JSON（{exc}），已忽略该项。", file=sys.stderr)
            return {}
        if not isinstance(parsed, dict):
            print("警告：MCP_SERVERS 的 JSON 顶层必须是对象，已忽略该项。", file=sys.stderr)
            return {}
        return {str(k): dict(v) for k, v in parsed.items() if isinstance(v, dict)}

    servers: dict[str, dict] = {}
    for entry in text.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            print(f"警告：MCP_SERVERS 的条目 {entry!r} 缺少 '='（应形如 名字=命令），已跳过。",
                  file=sys.stderr)
            continue
        name, _, command = entry.partition("=")
        name, command = name.strip(), command.strip()
        if not name or not command:
            print(f"警告：MCP_SERVERS 的条目 {entry!r} 名字或命令为空，已跳过。", file=sys.stderr)
            continue
        # posix=True：只有这个模式会**剥掉引号**。用 posix=False 的话
        # "C:\\Program Files\\python.exe" 会被切成 ['"C:\\Program', 'Files\\...']，
        # 带空格的路径直接连不起来——而 Windows 上解释器路径几乎必然带空格。
        parts = shlex.split(command)
        if not parts:
            print(f"警告：MCP_SERVERS 的条目 {entry!r} 解析后为空，已跳过。", file=sys.stderr)
            continue
        servers[name] = {"command": parts[0], "args": parts[1:]}
    return servers
