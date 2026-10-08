"""联网搜索工具。

给 Agent 开一个通向外网的只读窗口：模型提出关键词，工具拿回若干条网页摘要。
它是目前唯一会访问外部的工具（exec 虽然也能联网，但用法受限且要过安全策略），
因此在设计上刻意保守三件事：

- **只读**：只做查询，不下载、不提交、不写任何本地状态，可放心并发调用；
- **降级成文本**：搜索这行的失败是常态（没搜到、超时、被限流、网络不通），
  这些统统翻译成一句人话回给模型，而不是抛异常——按 Tool 的约定，模型看到
  "没搜到"可以换个关键词再试，而异常会直接打断整个 agent 循环；
- **截断**：结果拼完超过 MAX_OUTPUT_CHARS 就砍掉，一条摘要动辄几百字，
  攒够几条就够把上下文撑出一个大窟窿。

同步库跑在异步循环里的问题单独说一句：ddgs 的 DDGS().text() 是同步阻塞调用，
内部还会起线程池等网络，直接在 execute 里调用会把整个事件循环卡住——期间连
流式输出的分片都推不出去。所以它被 asyncio.to_thread 丢进线程里跑，这一步不是
可选的优化，是让工具"不把 agent 冻住"的前提。

任何异常都不会穿出去，两条出口：搜不到返回"未找到相关结果"，其余异常返回
"搜索出错: …"。超时单独判一下，因为它的处置方式和其他错误不一样——值得原样
重试，而参数写错重试多少次都没用。

典型用法::

    registry = ToolRegistry()
    registry.register(WebSearchTool())
    print(await registry.execute("web_search", {"query": "Python asyncio 最佳实践"}))
"""

import asyncio
import logging
from typing import Any

from ddgs import DDGS

from agent.tools.base import Tool

logger = logging.getLogger(__name__)

#: 单次返回给模型的字符上限，超出部分截断。
MAX_OUTPUT_CHARS = 8000

#: max_results 的下限：0 或负数交给 ddgs 也拿不到结果，不如当一次用法错误拦下。
MIN_RESULTS = 1

#: 单次搜索的超时时间（秒），透传给 ddgs 内部的 HTTP 请求。
SEARCH_TIMEOUT = 10

#: 模型通常只需要前几条结果，默认 5 条既能覆盖答案，又不会一次塞满上下文。
DEFAULT_MAX_RESULTS = 5

#: ddgs 在"没搜到"时抛异常而不是返回空列表，只能靠错误文本把它自己的
#: "没结果"和真正的故障区分开。统一转小写后按子串匹配，兼容它换措辞或换语言。
_NO_RESULT_HINTS = ("no results found", "no results", "未找到", "没有找到")


class WebSearchTool(Tool):
    """用 DuckDuckGo 搜索互联网并返回带链接的结果摘要的工具。

    走 ddgs 库的文本搜索接口，把每条结果格式化成 Markdown 段落
    （序号 + 标题 + 链接 + 摘要），模型可以直接读，也可以把它当成"下一步该
    打开哪个网页"的索引。

    无状态，可并发调用；每次调用新建一个 DDGS 实例，不共享连接与缓存，
    因此也不受上一次搜索残留状态的影响。
    """

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "搜索互联网获取最新信息。"
            "当你需要查询实时信息、最新新闻或者不确定的知识时使用。"
            "用 DuckDuckGo 搜索互联网，返回若干条结果的标题、链接和摘要。"
            "适合查询工作区里没有、且需要最新外部信息的问题：库的用法、报错原因、"
            "版本变更、新闻动态等。参数 query 是搜索关键词，写法与搜索引擎一致；"
            f"max_results 控制返回条数，默认 {DEFAULT_MAX_RESULTS} 条。"
            f"结果过多时按 {MAX_OUTPUT_CHARS} 字符截断。"
            "搜索结果只是摘要，需要看正文时请用链接进一步确认。"
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
            max_results: 最多返回几条结果，小于 1 时被抬到 1。

        Returns:
            格式化后的结果文本；搜不到返回"未找到相关结果"，出错返回
            "搜索出错: …"，两者都不抛异常。
        """
        keyword = (query or "").strip()
        if not keyword:
            return "错误：搜索关键词不能为空，请提供具体的 query。"

        try:
            # DDGS().text() 是同步阻塞调用，必须丢进线程，否则会卡死事件循环，
            # 连流式输出的分片都推不出去。max_results 取正数后再传，避免负数
            # 进去让 ddgs 内部的结果聚合逻辑直接抛错。
            results = await asyncio.to_thread(
                DDGS(timeout=SEARCH_TIMEOUT).text,
                keyword,
                max_results=max(int(max_results), MIN_RESULTS),
            )
        except Exception as exc:  # noqa: BLE001 - 工具约定：失败要变成文本而不是异常
            return _describe_error(exc)

        if not results:
            # ddgs 在没结果时走的是异常分支，这里兜的是它将来改成返回空列表的情况。
            return "未找到相关结果"

        return _format_results(results)


def _describe_error(exc: Exception) -> str:
    """把搜索异常翻译成给模型看的说明文本。

    只对"没搜到"和"超时"两种做特判，因为它们的下一步动作与普通错误不同：前者
    该换关键词，后者该原样重试。其余异常（解析失败、代理不通、被限流等）一律
    保留原始错误信息——模型读到 "TimeoutException: error sending request for
    url (…)" 这种原文，比"搜索失败，请稍后再试"有信息量得多。
    """
    message = str(exc)
    lowered = message.lower()

    # 没结果在 ddgs 里是抛 DDGSException("No results found.")，要按"没搜到"报，
    # 否则模型会把一次正常的空结果当成工具坏了。
    if any(hint in lowered for hint in _NO_RESULT_HINTS):
        return "未找到相关结果"

    if isinstance(exc, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
        return f"搜索出错: 请求超时（超过 {SEARCH_TIMEOUT:g} 秒未返回），可以稍后重试或换更短的关键词"

    logger.warning("联网搜索失败: %s: %s", type(exc).__name__, message)
    return f"搜索出错: {message}" if message else f"搜索出错: {type(exc).__name__}"


def _format_results(results: list[dict[str, Any]]) -> str:
    """把搜索结果拼成给模型看的文本，超过 MAX_OUTPUT_CHARS 时截断。

    单条格式固定为 "### {序号}. {标题}\n链接: {网址}\n{摘要}\n"：Markdown
    标题让模型一眼分清条与条，链接独占一行便于它原样引用。字段缺失时用空串
    兜住、摘要里的换行一律压成空格——一条格式异常的记录不该让整次搜索白跑。

    截断按"条"来切而不是按字符硬切：堆到上限就停，末尾注明还剩几条，模型因此
    知道信息不完整，需要时可以调小 max_results 或换更精确的关键词重搜。
    """
    blocks: list[str] = []
    length = 0
    truncated = False

    for index, item in enumerate(results, start=1):
        block = (
            f"### {index}. {_flat(item.get('title'))}\n"
            f"链接: {_flat(item.get('href'))}\n"
            f"{_flat(item.get('body'))}\n"
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


def _flat(value: Any) -> str:
    """把结果字段规整成单行文本，缺失或非字符串时返回空串。

    摘要里带换行会把"链接"那一行挤到段落中间，模型读到的结构就散了，所以统一
    压成空格；字段是 None 或数字也不该让格式化抛 AttributeError。
    """
    if value is None:
        return ""
    return " ".join(str(value).split())
