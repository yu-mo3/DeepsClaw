"""联网搜索工具（博查 Web Search API）。

给 Agent 开一个通向外网的只读窗口：模型提出关键词，工具拿回若干条网页结果。
底层走博查 AI 开放平台的 Web Search 接口——国内直连、无需代理，返回的是为
大模型整理过的摘要而不是网页原文，正好是 agent 需要的那种输入。

设计上刻意保守三件事：

- **只读**：只发查询，不下载、不提交、不写任何本地状态，可并发调用；
- **降级成文本**：搜索这行的失败是常态（没搜到、超时、欠费、限流、密钥错），
  这些统统翻译成一句人话回给模型，而不是抛异常——按 Tool 的约定，模型看到
  "没搜到"可以换个关键词再试，看到"密钥无效"则会直接告诉用户去处理，
  而异常会一下子打断整个 agent 循环；
- **截断**：结果拼完超过 MAX_OUTPUT_CHARS 就砍掉。博查开了 summary 后单条
  摘要动辄上千字，几条就够把上下文撑出一个大窟窿。

关于密钥：它只从配置里来（.env 的 BOCHA_API_KEY），不进代码、不进日志。
缺密钥时工具**不抛异常**，而是返回一句"未配置"的说明——让 agent 能正常起
来把话说完，比启动即崩更符合"工具失败是可读文本"的约定。

典型用法::

    registry = ToolRegistry()
    registry.register(WebSearchTool(cfg.bocha_api_key))
    print(await registry.execute("web_search", {"query": "Python asyncio 最佳实践"}))
"""

import logging
from typing import Any

import httpx

from agent.tools.base import Tool

logger = logging.getLogger(__name__)

#: 博查 Web Search 接口地址。
API_URL = "https://api.bocha.cn/v1/web-search"

#: 单次请求的超时时间（秒）。搜索接口正常 1~3 秒返回，留到 15 秒足够宽裕。
SEARCH_TIMEOUT = 15.0

#: 单次返回给模型的字符上限，超出部分按条截断。
MAX_OUTPUT_CHARS = 8000

#: max_results 的上下限：接口本身只接受 1~50，越界会被它直接拒掉。
MIN_RESULTS = 1
MAX_RESULTS = 50

#: 模型通常只需要前几条结果，默认 5 条既能覆盖答案，又不会一次塞满上下文。
DEFAULT_MAX_RESULTS = 5


class WebSearchTool(Tool):
    """用博查搜索互联网并返回结果摘要的工具。

    每条结果格式化成一片 Markdown 段落（序号 + 标题 + 链接 + 摘要），模型可以
    直接读，也可以把它当成"下一步该打开哪个网页"的索引。摘要优先取接口的
    summary（长文本，要求 summary=true），它为空时退回 snippet。

    无状态，可并发调用；每次调用新建一个 httpx.AsyncClient，用完即关，不跨调用
    共享连接与 Cookie，也就不存在"上次搜索的残留状态影响这次"的问题。
    """

    def __init__(self, api_key: str | None = None) -> None:
        """初始化。

        Args:
            api_key: 博查 API key。为 None 或空串时，execute 会返回"未配置"的
                提示而不是抛异常——这样缺密钥的 agent 仍然能启动并对话。
        """
        self.api_key = (api_key or "").strip()

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "用博查搜索互联网，返回若干条结果的标题、链接、来源和摘要。"
            "适合查询工作区里没有、且需要外部或最新信息的问题：库的用法、报错原因、"
            "版本变更、新闻动态等。参数 query 是搜索关键词，写法与搜索引擎一致；"
            f"max_results 控制返回条数，默认 {DEFAULT_MAX_RESULTS} 条（上限 {MAX_RESULTS}）。"
            f"结果过多时按 {MAX_OUTPUT_CHARS} 字符截断。"
            "结果是网页摘要而非正文，需要确认细节时请顺着链接进一步查看。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，例如 Python asyncio 超时处理",
                },
                "max_results": {
                    "type": "integer",
                    "description": f"最多返回几条结果，默认 {DEFAULT_MAX_RESULTS}",
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, max_results: int = DEFAULT_MAX_RESULTS) -> str:
        """执行一次联网搜索并返回格式化后的结果。

        Args:
            query: 搜索关键词，空白串会被直接拒绝，不发请求。
            max_results: 最多返回几条结果，会被夹到 1~50 之间。

        Returns:
            格式化后的结果文本；搜不到返回"未找到相关结果"，出错返回
            "搜索出错: …"，两者都不抛异常。
        """
        keyword = (query or "").strip()
        if not keyword:
            return "错误：搜索关键词不能为空，请提供具体的 query。"

        if not self.api_key:
            return (
                "错误：未配置博查 API key，联网搜索不可用。"
                "请在项目根目录的 .env 中设置 BOCHA_API_KEY（可参照 .env.example）；"
                "也可以先用工作区内已有的资料回答，并把这一步告诉用户。"
            )

        payload = {
            "query": keyword,
            # count 由 max_results 决定，夹在接口允许的 1~50 之内：负数会让接口
            # 直接报参数错误，而 0 条结果对模型毫无意义，所以下限取 1。
            "count": max(MIN_RESULTS, min(int(max_results), MAX_RESULTS)),
            "summary": True,
            # noLimit 是官方推荐值：指定时间范围反而可能搜出空结果。
            "freshness": "noLimit",
        }

        try:
            async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
                response = await client.post(
                    API_URL,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.TimeoutException:
            return f"搜索出错: 请求超时（超过 {SEARCH_TIMEOUT:g} 秒未返回），可以稍后重试或换更短的关键词"
        except httpx.HTTPError as exc:
            # 连不上（断网、DNS、代理设置不对）落在这里，属于环境问题；
            # 原样带上异常文本，模型才知道该请用户检查网络还是自己换个词重试。
            logger.warning("联网搜索请求失败: %r", exc)
            return f"搜索出错: 无法连接搜索服务 - {exc}"
        except Exception as exc:  # noqa: BLE001 - 工具约定：失败要变成文本而不是异常
            return _describe_error(exc)

        error = _read_error(response)
        if error is not None:
            return error

        results = _extract_results(response)
        if not results:
            return "未找到相关结果"

        return _format_results(results)


def _read_error(response: httpx.Response) -> str | None:
    """把 HTTP 状态码与接口业务码翻译成错误文本；一切正常时返回 None。

    博查有两层状态：HTTP 码和响应体里的 code，正常情况下两者都是 200。出问题
    时该看哪一层并不固定（网关 502 时响应体根本不是 JSON），所以先判 HTTP 码，
    解析得出 JSON 再判业务码。鉴权失败和欠费只能请用户处理，限流则值得重试，
    按码给出各自的说法，比笼统一句"搜索失败"更省事。
    """
    if response.status_code != httpx.codes.OK:
        detail = response.text.strip()[:200]
        hints = {
            400: "请求参数不合法",
            401: "API key 无效，请检查 .env 里的 BOCHA_API_KEY",
            403: "账户余额不足，请到 open.bochaai.com 充值",
            429: "请求频率超限，请稍后重试",
        }
        if response.status_code >= 500:
            hint = "搜索服务内部错误，稍后重试即可"
        else:
            hint = hints.get(response.status_code, "接口返回了非 200 状态")
        return f"搜索出错: HTTP {response.status_code}（{hint}）" + (f" - {detail}" if detail else "")

    body = _json_or_none(response)
    if body is None:
        return "搜索出错: 搜索服务返回的不是合法 JSON，可能被网关或代理拦截了"

    code = body.get("code")
    if code in (None, httpx.codes.OK):
        return None

    detail = body.get("msg") or body.get("message")
    return f"搜索出错: 接口返回 code={code}" + (f" - {detail}" if detail else "")


def _json_or_none(response: httpx.Response) -> dict[str, Any] | None:
    """解析响应 JSON，失败或不是对象时返回 None。

    成功路径上抛异常最省事，但这里解析失败本身就是一种要被翻译给模型的失败，
    所以让它以 None 的形式参与普通控制流，而不是再抛一层异常出来。
    """
    try:
        body = response.json()
    except ValueError:
        logger.warning("搜索响应不是合法 JSON，前 200 字符: %r", response.text[:200])
        return None
    return body if isinstance(body, dict) else None


def _extract_results(response: httpx.Response) -> list[dict[str, Any]]:
    """从响应里取出 webPages.value 列表，结构不对时返回空列表。

    路径是固定的三层嵌套（data → webPages → value），中间任何一层缺失都算
    "没有结果"——接口无匹配时会把这一串留空，把它当异常处理反而会误导模型。
    """
    body = _json_or_none(response) or {}
    data = body.get("data")
    if not isinstance(data, dict):
        return []
    web_pages = data.get("webPages")
    if not isinstance(web_pages, dict):
        return []
    value = web_pages.get("value")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _format_results(results: list[dict[str, Any]]) -> str:
    """把搜索结果拼成给模型看的文本，超过 MAX_OUTPUT_CHARS 时截断。

    单条格式为 "### {序号}. {标题}\n链接: {网址}\n来源: {出处}\n{摘要}\n"：
    Markdown 标题让模型一眼分清条与条，链接独占一行便于它原样引用，来源和日期
    帮它判断这条信息可不可信、够不够新，末尾是摘要正文。

    截断按"条"来切而不是按字符硬切：堆到上限就停，末尾注明还剩几条，模型因此
    知道信息不完整，需要时可以调小 max_results 或换更精确的关键词重搜。
    """
    blocks: list[str] = []
    length = 0
    truncated = False

    for index, item in enumerate(results, start=1):
        block = (
            f"### {index}. {_flat(item.get('name'))}\n"
            f"链接: {_flat(item.get('url'))}\n"
            f"{_attribution(item)}\n"  # 来源/日期独立成行，摘要缺失时也不会丢
            f"{_body(item)}\n"
        )

        # 第一条无条件收下：宁可略微超限，也好过因为一条超长摘要而返回空字符串。
        if blocks and length + len(block) > MAX_OUTPUT_CHARS:
            truncated = True
            break

        blocks.append(block)
        length += len(block)

    text = "\n".join(blocks)
    if truncated:
        text += (
            f"\n...[结果已截断：共 {len(results)} 条，以上为前 {len(blocks)} 条，"
            f"另有 {len(results) - len(blocks)} 条未列出]"
        )
    return text


def _body(item: dict[str, Any]) -> str:
    """取一条结果的正文摘要：优先 summary，为空时退回 snippet。

    summary 要 summary=true 才有，且个别站点仍然留空（比如纯图片页），
    这时 snippet 虽然短，也聊胜于无。
    """
    return _flat(item.get("summary")) or _flat(item.get("snippet"))


def _attribution(item: dict[str, Any]) -> str:
    """拼出"来源: {站点} · {日期}"这一行，两者都没有时返回空串。

    只取日期部分：完整时间戳精确到秒，对判断新旧没有额外帮助，
    却要在每条结果上多占二十来个字符。返回空串时这一行会变成空行，
    由调用方统一处理，不必在这里再造一个分支。
    """
    parts = [_flat(item.get("siteName")), _flat(item.get("datePublished"))[:10]]
    joined = " · ".join(part for part in parts if part)
    return f"来源: {joined}" if joined else ""


def _flat(value: Any) -> str:
    """把结果字段规整成单行文本，缺失或非字符串时返回空串。

    摘要里带换行会把"链接"那一行挤到段落中间，模型读到的结构就散了，所以统一
    压成空格；字段是 None 或数字也不该让格式化抛 AttributeError。
    """
    if value is None:
        return ""
    return " ".join(str(value).split())


def _describe_error(exc: Exception) -> str:
    """兜底：把执行期冒出来的意外异常翻译成错误文本，不让它穿给 agent 循环。

    正常路径上的失败（HTTP 码、业务码、超时、连不上）都已经有各自的出口，
    走到这里说明是没预料到的类型——原样保留异常文本，方便定位。
    """
    message = str(exc)
    logger.warning("联网搜索失败: %s: %s", type(exc).__name__, message)
    return f"搜索出错: {message}" if message else f"搜索出错: {type(exc).__name__}"
