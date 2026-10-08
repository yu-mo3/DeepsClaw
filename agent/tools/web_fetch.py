"""网页抓取工具。

给 Agent 补上联网搜索缺的那一半：搜索只给摘要和链接，要看到正文就得把页面抓
下来。本模块把一个 URL 变成一段干净的纯文本，交给模型直接读。

整条链路按顺序做四件事，每件事都有独立的失败出口：

1. **URL 安全检查**：只放行公网的 http / https。这一步不是形式主义——模型能
   从网页内容里"读到"新的 URL 再来抓，等于把外部输入接进了工具调用，也就是
   典型的 SSRF 入口。``file://`` / ``data://`` 能读本机文件，``http://169.254.169.254/``
   能读云主机元数据，``http://127.0.0.1:11434/`` 能戳内网服务，所以在**发请求之前**
   就按 IP 段拦死，并且**每一次跳转都重新校验**，不给 302 绕过检查的机会。
2. **发 GET**：httpx 流式读取，边读边计数，超过 MAX_BYTES 就断开，避免一个
   "无限下载"的地址把内存吃光。
3. **HTML 转纯文本**：用标准库 html.parser 按标签语义换行（标题、段落、列表、
   表格各自成型），丢掉 script/style/svg 这些永远不会是正文的块。
4. **清理输出**：压掉重复空行与行首尾空白，剔除只剩花括号分号的水印噪声行，
   最后按 MAX_CONTENT_CHARS 截断。

全程不抛异常：任何失败都翻译成一句人话回给模型（见 agent.tools.base 的实现约定），
让它在"这个页面打不开"之后还能换个地址继续干，而不是把整轮任务打断。

两个刻意的取舍：

- **不跟随系统代理**（trust_env=False）。代理会让连接目标变成代理服务器，内网
  判定随之失真；而且本地代理常是 127.0.0.1，一旦跟着环境变量走，就没法在
  "允许内网"和"禁止内网"之间保持一致。抓取需要走代理时请在代码里显式配置。
- **不解析 PDF / 图片**。非文本内容一律拒绝并说明原因，硬转只会给模型一堆乱码。

典型用法::

    registry = ToolRegistry()
    registry.register(WebFetchTool())
    print(await registry.execute("web_fetch", {"url": "https://example.com"}))
"""

import asyncio
import ipaddress
import logging
import re
import socket
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpcore
import httpx

from agent.tools.base import Tool

logger = logging.getLogger(__name__)

#: 单次抓取的超时时间（秒），管的是整条请求而不是两块数据之间的间隔。
FETCH_TIMEOUT = 20.0

#: 最多读取的响应体字节数。网页正文通常几十 KB，2MB 足够覆盖长文与内联数据。
MAX_BYTES = 2 * 1024 * 1024

#: 交给模型的纯文本上限（字符），超出部分截断并注明。
MAX_CONTENT_CHARS = 12000

#: 最多跟随几次跳转。超过就报错，避免跳转环把一次调用拖到超时。
MAX_REDIRECTS = 5

#: 抓取时使用的 UA。带上标识是基本礼貌，也让对方在日志里认得出这是谁。
USER_AGENT = "dshclaw/1.0 (+https://github.com/; web_fetch tool)"

#: 声明的可接受类型：网页优先，纯文本兜底，其余一律按 '*/*' 让服务端照常回，
#: 由 Content-Type 检查负责拒绝二进制（这里写死 pdf/image 反而会让个别站点直接 406）。
ACCEPT_HEADER = "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"

#: 只接受这些 Content-Type 前缀。其余（PDF、图片、压缩包）转文本没有意义。
ALLOWED_CONTENT_TYPES = (
    "text/html",
    "application/xhtml",
    "text/plain",
    "text/xml",
    "application/xml",
)

#: 明确拒绝的内网/保留网段。命中任意一段即拒绝，宁可误伤也不要放行。
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        # IPv4
        "0.0.0.0/8",          # 本机/无效地址
        "10.0.0.0/8",         # 私网 A
        "100.64.0.0/10",      # CGNAT，运营商内网
        "127.0.0.0/8",        # 环回
        "169.254.0.0/16",     # 链路本地，云元数据 169.254.169.254 在这里
        "172.16.0.0/12",      # 私网 B
        "192.0.0.0/24",       # IETF 协议专用
        "192.168.0.0/16",     # 私网 C
        "198.18.0.0/15",      # 基准测试网段
        "224.0.0.0/4",        # 组播
        "240.0.0.0/4",        # 保留，含 255.255.255.255
        # IPv6
        "::/128",             # 未指定
        "::1/128",            # 环回
        "fc00::/7",           # 唯一本地地址
        "fe80::/10",          # 链路本地
        "ff00::/8",           # 组播
        "64:ff9b::/96",       # NAT64，可能把内网地址映射进来
        "2001:db8::/32",      # 文档用网段
    )
)

#: 无论解析成什么 IP 都拒绝的主机名。写死这几条是因为它们要么特殊，
#: 要么在部分系统的解析里压根拿不到可判定的 IP。
_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "metadata",
    "metadata.google.internal",
})

#: 直接丢弃的标签内容：这些块里的文字永远不是正文，留着只会污染上下文。
#:
#: **只放有闭合标签的容器在这里**。meta / link / input / br 这类空元素永远等不到
#: 自己的结束标签，一旦进了这张表就会把"跳过深度"永远顶住，整页正文全被吞掉
#: ——这类元素本来也不含文本，根本不需要过滤。
_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "template", "iframe", "svg", "canvas",
    "head", "title", "object", "video", "audio", "map", "form", "select",
    "option", "textarea", "button", "label", "figure", "figcaption",
})

#: 渲染成"块"的标签：进入和退出各补一个换行，正文才能按段落分开。
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "header", "footer", "main", "aside",
    "nav", "blockquote", "pre", "table", "thead", "tbody", "tfoot", "tr",
    "ul", "ol", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6",
    "address", "fieldset", "details", "summary",
})

#: 空元素：只补一个换行，不存在"退出"事件。
_VOID_TAGS = frozenset({"br", "hr"})

#: 单元格分隔符，让表格转文本后还能看出一行里有几列。
_CELL_SEPARATOR = " | "

#: 只有这些字符的行视为水印/装饰噪声，整行丢掉。
_JUNK_LINE_RE = re.compile(r"^[\s\{\}\(\)\[\]:;,.=+\-*/|_~`'\"<>\\&%$#@!?\^0-9]*$")

#: 判"这行像代码"：花括号、分号、等号、反斜杠扎堆。
_CODE_HINT_RE = re.compile(r"[{};\\]")

#: 行内连续空白（含全角空格、不换行空格）压成一个空格。
_INLINE_SPACE_RE = re.compile(r"[ \t\u00a0\u3000\u200b]+")


class _UnsafeURL(Exception):
    """URL 未通过安全检查（协议、主机名或解析出的 IP 落在禁止范围内）。"""


class _SafeNetworkBackend(httpcore.AnyIOBackend):
    """在 DNS 解析处做校验的网络后端。

    "先解析、判 IP、再让 httpx 自己解析一次"是经典 SSRF 漏洞：两次解析之间
    DNS 可以被换答案（DNS rebinding），检查通过的那次和真正连上的那次可能是两台
    完全不同的机器。所以校验必须发生在**真正要用的那个 IP 上**，这里重写
    connect_tcp 就是为了拿到这个位置——它拿到解析结果，先判，再用同一个地址连。

    放在后端层还有个好处：重定向、连接复用、每条新连接都自动过这道检查，
    不需要在每个调用点记得再验一次。
    """

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        """解析并校验目标地址，然后把**同一个** IP 交给底层去连。"""
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise _UnsafeURL(f"域名 {host!r} 解析失败，可能是地址写错了或本机 DNS 不可用") from exc

        if not infos:
            raise _UnsafeURL(f"域名 {host!r} 没有解析到任何地址")

        # 不挑"第一个能用的"：只要有一个候选落在禁止网段就整体拒绝。
        # 攻击者完全可以给一个 A 记录是公网、AAAA 记录是内网的域名。
        for info in infos:
            ip = str(info[4][0]).split("%", 1)[0]  # 去掉 IPv6 的 scope id
            blocked = _blocked_reason(ip)
            if blocked:
                raise _UnsafeURL(f"目标 {host!r} 解析到 {ip}，{blocked}")

        ip = str(infos[0][4][0]).split("%", 1)[0]
        return await super().connect_tcp(
            ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class _TextExtractor(HTMLParser):
    """把 HTML 按标签语义抽成纯文本片段。

    用标准库的 html.parser 而不是引第三方解析器：抓网页只需要"哪些字是正文、
    段落怎么分"，不需要容错恢复 DOM 树，为此背一个 lxml 依赖不划算。

    产出的是"片段列表"而不是成型的文本，换行、逐行清理、截断都留给后续步骤，
    这样每段职责单一，改清理规则不必碰解析逻辑，反之亦然。
    """

    def __init__(self) -> None:
        # convert_charrefs=True：&nbsp; &amp; &#39; 这类实体在解析阶段就还原，
        # 免得它们混在文本里被当成正文的一部分。
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        # 用计数器而不是布尔量：<div><script> 这种嵌套里，退出内层不该立刻恢复收集。
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        # 同一行里已经出过几个单元格，用来决定要不要补 " | " 分隔符。
        self._cells_in_row = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._on_start(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <br/> <img src=x /> 这类自闭合标签只出现在开始位置，不补退出事件。
        self._on_start(tag)

    def _on_start(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if tag == "title":
                self._in_title = True
            else:
                self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "tr":
            # 行结束：先给最后一格收尾（补换行），再重置计数。
            if self._cells_in_row:
                self._parts.append("\n")
            self._cells_in_row = 0
            return
        if tag in _BLOCK_TAGS or tag in _VOID_TAGS:
            self._parts.append("\n")
        elif tag == "li":
            self._parts.append("\n- ")
        elif tag in ("td", "th"):
            # 行列分隔：首格直接开始，后续格用 " | " 接在本行后面，
            # 这样一行表格落在一行文本里；结尾的换行留给 </tr>。
            if self._cells_in_row:
                self._parts.append(_CELL_SEPARATOR)
            self._cells_in_row += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if tag == "title":
                self._in_title = False
            else:
                self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        # </td> / </th> 不换行：单元格之间靠分隔符连成一行，换行由 </tr> 负责。
        if tag in _BLOCK_TAGS or tag == "li":
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._skip_depth:
            return
        self._parts.append(data)

    @property
    def title(self) -> str:
        """页面标题，取不到时返回空串。"""
        return _INLINE_SPACE_RE.sub(" ", unescape("".join(self._title_parts))).strip()

    def text(self) -> str:
        """拼接后的原始文本（尚未做逐行清理）。"""
        return "".join(self._parts)


def _build_transport() -> httpx.AsyncBaseTransport:
    """构造带安全检查的 httpx 传输层。

    两处定制：

    - ``trust_env=False``：不跟随系统代理（原因见模块文档）；
    - 换上 _SafeNetworkBackend，把"目标 IP 是否允许"的判定放在真正建立连接的
      那一刻，重定向与新建连接都会自动过这道闸。

    ``_pool`` 是 httpx 的私有属性，私有 API 有随版本变动的风险；这里做了探测，
    万一将来找不到就把安全检查降级到 _validate_url 这一层，并明确告警，而不是
    在导入或运行时炸掉。
    """
    transport = httpx.AsyncHTTPTransport(verify=True, trust_env=False, retries=0)
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        logger.warning("当前 httpx 版本不再暴露连接池网络后端，DNS 层校验已跳过")
        return transport
    pool._network_backend = _SafeNetworkBackend()
    return transport


def _build_client(**kwargs: Any) -> httpx.AsyncClient:
    """构造抓取用的 AsyncClient。

    单独抽一层是为了让测试能替换它注入 MockTransport：把每个连接都真的打到外网
    是对测试机网络的额外依赖，也不该为了测一段文本处理去真的发请求。
    """
    return httpx.AsyncClient(**kwargs)


class WebFetchTool(Tool):
    """抓取指定 URL 的网页并转换成纯文本的工具。

    与 web_search 的分工：搜索负责"去哪里找"，本工具负责"把那一页读进来"。
    返回的是正文纯文本，不含 HTML 标签、脚本和样式，模型可以直接当作资料使用。

    无状态、只读、可并发调用；每次调用新建一个 client，用完即关。
    """

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return (
            "抓取指定 URL 的网页内容并转换成纯文本返回，适合在 web_search 之后"
            "打开某一条结果看正文。参数 url 必须是完整的 http/https 地址，"
            "例如 https://example.com/doc。"
            f"单页正文最多返回 {MAX_CONTENT_CHARS} 字符，超出会截断并注明；"
            f"响应体超过 {MAX_BYTES // 1024 // 1024}MB 会停止下载。"
            "只支持网页和纯文本，PDF、图片等二进制内容会被拒绝。"
            "出于安全考虑，内网地址（localhost、127.0.0.1、10.x、192.168.x 等）"
            "一律不允许抓取；需要读取本地文件请改用 read_file。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要抓取的完整网页地址，必须以 http:// 或 https:// 开头",
                },
            },
            "required": ["url"],
        }

    async def execute(self, url: str) -> str:
        """抓取 URL 并返回网页正文的纯文本。

        Args:
            url: 完整的 http/https 地址。

        Returns:
            正文纯文本（带标题与最终地址的头部）；地址不安全、打不开、类型不支持
            等情况都返回对应的说明文本，不抛异常。
        """
        raw = (url or "").strip()
        if not raw:
            return "错误：url 不能为空，请提供完整的 http/https 地址。"

        if not _has_scheme(raw):
            # 补 https:// 比直接报错友好，模型给 "example.com" 是最常见的形态。
            raw = "https://" + raw.lstrip("/")

        try:
            async with _build_client(
                transport=_build_transport(),
                timeout=FETCH_TIMEOUT,
                follow_redirects=False,
                headers={"User-Agent": USER_AGENT, "Accept": ACCEPT_HEADER},
            ) as client:
                response, final_url = await _get_with_redirects(client, raw)
                body, truncated_bytes = await _read_body(response)
        except _UnsafeURL as exc:
            return f"错误：{exc}。出于安全考虑，只允许抓取公网地址。"
        except httpx.TimeoutException:
            return f"错误：抓取超时（超过 {FETCH_TIMEOUT:g} 秒未完成），可以稍后重试或换个地址。"
        except httpx.HTTPError as exc:
            logger.warning("抓取 %s 失败: %r", raw, exc)
            return _describe_http_error(exc)
        except Exception as exc:  # noqa: BLE001 - 工具约定：失败要变成文本而不是异常
            logger.warning("抓取 %s 时出现意外错误: %r", raw, exc)
            return f"错误：抓取失败 - {type(exc).__name__}: {exc}"


        content_type = response.headers.get("content-type", "")
        if not _is_supported_type(content_type):
            kind = content_type.split(";", 1)[0].strip() or "未知类型"
            return (
                f"错误：{final_url} 返回的是 {kind}，不是网页文本，无法转换成纯文本。"
                "如果需要该文件本身，请让用户下载后放到工作区，再用 read_file 读取。"
            )

        text = _decode(body, response)
        if not text.strip():
            return f"错误：{final_url} 返回了空内容（HTTP {response.status_code}）。"

        return _build_output(text, response.status_code, final_url, truncated_bytes)


async def _get_with_redirects(
    client: httpx.AsyncClient, url: str
) -> tuple[httpx.Response, str]:
    """发 GET 并手动跟随跳转，每一跳都重新做安全检查。

    不用 httpx 自带的 follow_redirects：那样跳转目标完全绕过我们的校验，
    一个公网地址用 302 就能把请求带进内网（经典的 SSRF 绕过）。这里自己走，
    每跳都过一遍 _validate_url，并在跳转次数上设硬上限。

    Returns:
        (最终响应, 最终地址)。响应已读完——httpx 的响应体和 client 的生命周期
        绑在一起，出 with 块再读就是空的。

    Raises:
        _UnsafeURL: 起始地址或任意一跳的目标不安全。
        httpx.HTTPError: 网络层面的失败。
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _validate_url(current)
        response = await client.get(current)
        location = response.headers.get("location")

        if not (300 <= response.status_code < 400 and location):
            return response, current

        current = str(httpx.URL(current).join(location))
        await response.aclose()

    raise _UnsafeURL(f"跳转次数超过 {MAX_REDIRECTS} 次仍未到达目标，疑似跳转环")


async def _read_body(response: httpx.Response) -> tuple[bytes, bool]:
    """流式读取响应体，超过 MAX_BYTES 就停下来。

    必须流式：``response.content`` 会先把整个响应体收进内存，遇到一个无限输出的
    地址就是无上限的内存占用。边读边数，到线即断，并把"截断了"如实告诉调用方。

    Returns:
        (响应体字节, 是否因超限被截断)。
    """
    chunks: list[bytes] = []
    total = 0
    truncated = False
    try:
        async for chunk in response.aiter_bytes():
            if total + len(chunk) > MAX_BYTES:
                chunks.append(chunk[: MAX_BYTES - total])
                truncated = True
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        await response.aclose()
    return b"".join(chunks), truncated


def _build_output(
    raw_text: str,
    status_code: int,
    final_url: str,
    truncated_bytes: bool,
) -> str:
    """把响应体变成最终交付给模型的文本。

    头部三行给出"这页讲什么、从哪来、有没有异常"，正文紧跟其后。带上标题是因为
    正文可能很长，模型需要一个先验判断值不值得细读；带上最终地址是因为跳转之后
    它常常和请求的地址不同。
    """
    extractor = _TextExtractor()
    try:
        extractor.feed(raw_text)
        extractor.close()
    except Exception:  # noqa: BLE001 - 解析器在畸形页面上出什么错都不该让抓取作废
        logger.debug("HTML 解析中途出错，保留已抽取的内容", exc_info=True)

    title = extractor.title
    text = _clean_text(extractor.text())
    if not text:
        text = "（页面没有可提取的文本内容，可能是需要 JavaScript 才能渲染的页面）"

    header = []
    if title:
        header.append(f"标题: {title}")
    header.append(f"来源: {final_url}")
    if status_code != httpx.codes.OK:
        # 4xx/5xx 的页面往往仍有正文（404 页、验证页），照样给模型看，
        # 只是如实标注状态码，免得它把错误页当成正常内容。
        header.append(f"状态: HTTP {status_code}，内容可能不是正常页面")
    if truncated_bytes:
        header.append(f"提示: 响应体超过 {MAX_BYTES // 1024 // 1024}MB，已停止下载，内容不完整")

    body = text
    if len(body) > MAX_CONTENT_CHARS:
        body = (
            body[:MAX_CONTENT_CHARS]
            + f"\n\n...[内容已截断：全文约 {len(text)} 字符，以上为前 {MAX_CONTENT_CHARS} 字符]"
        )

    return "\n".join(header) + "\n\n" + body


def _clean_text(raw: str) -> str:
    """把抽取出的原始文本清理成可读的正文。

    四步，顺序不能换：先按行去空白，再丢弃噪声行，然后压掉连续空行，最后统一
    行内空白。反过来做的话，行首的多余空格会让"这行是不是空的"判断失真。

    Returns:
        清理后的文本；全是噪声时返回空串，由调用方给出兜底说明。
    """
    lines: list[str] = []
    for line in raw.split("\n"):
        cleaned = _INLINE_SPACE_RE.sub(" ", unescape(line)).strip()
        if not cleaned:
            lines.append("")
            continue
        if _JUNK_LINE_RE.match(cleaned):
            continue  # 只剩标点/花括号/数字的行：水印、被截断的脚本残渣
        if len(cleaned) > 200 and len(_CODE_HINT_RE.findall(cleaned)) > 20:
            continue  # 长且充满 {};\ 的行，是没被 script 标签包住的脚本片段
        lines.append(cleaned)

    # 连续空行压成一个：HTML 里段落之间常有大量空白节点。
    collapsed: list[str] = []
    for line in lines:
        if not line and collapsed and not collapsed[-1]:
            continue
        collapsed.append(line)

    return "\n".join(collapsed).strip()


def _decode(body: bytes, response: httpx.Response) -> str:
    """把响应体字节解码成文本。

    编码优先用响应头里的 charset（httpx 已解析好）；头里没写时按 UTF-8 解，
    失败再退回系统本地编码——中文站点里 GBK 但没声明 charset 的不在少数，
    硬按 UTF-8 解会整页乱码。两种情况都用替换字符兜住，绝不因为编码问题报错。
    """
    encoding = response.encoding or "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        # 服务端声明了一个 Python 不认识的 charset，直接退回 UTF-8。
        return body.decode("utf-8", errors="replace")


def _has_scheme(url: str) -> bool:
    """判断字符串里是否已经写了协议头。"""
    return re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url) is not None


def _is_supported_type(content_type: str) -> bool:
    """Content-Type 是否属于能转成文本的类型。

    头缺失时按支持处理：有些站点（尤其内网工具和静态托管）压根不回这个头，
    一律拒绝会让工具在这些站上完全不可用；真要是二进制，解码与解析阶段的兜底
    也会让它变成一段没什么用的文本，而不是崩掉。
    """
    value = (content_type or "").split(";", 1)[0].strip().lower()
    if not value:
        return True
    return any(value.startswith(prefix) for prefix in ALLOWED_CONTENT_TYPES)


def _validate_url(url: str) -> None:
    """校验 URL 的协议、主机名与 IP 是否允许抓取。

    只做静态检查：真正的 IP 判定在 _SafeNetworkBackend.connect_tcp 里，
    那里才能拿到"即将连接的那个地址"。这一步负责把明显不该发的请求挡在门外，
    并给出人能看懂的理由（IP 字面量在这里就能判，不必等到连的时候）。

    Raises:
        _UnsafeURL: 任何一项不通过。
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()

    if scheme not in ("http", "https"):
        raise _UnsafeURL(f"不支持 {scheme or '空'} 协议，只能抓取 http/https 地址")

    host = parts.hostname or ""
    if not host:
        raise _UnsafeURL("地址里没有主机名")

    if parts.username or parts.password:
        raise _UnsafeURL("地址里带有用户名或密码，已拒绝")

    lowered = host.lower()
    if lowered in _BLOCKED_HOSTNAMES or lowered.endswith((".local", ".internal", ".localhost")):
        raise _UnsafeURL(f"主机名 {host!r} 属于本机或内网")

    # IP 字面量不必等 DNS：直接判段。域名交给网络后端在解析处判。
    literal = host.strip("[]")
    if _is_ip_literal(literal):
        reason = _blocked_reason(literal)
        if reason:
            raise _UnsafeURL(f"地址指向 {literal}，{reason}")


def _is_ip_literal(host: str) -> bool:
    """host 是否直接就是 IP 字面量。"""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _blocked_reason(ip: str) -> str | None:
    """IP 落在禁止网段时返回原因描述，公网地址返回 None。

    解析不出来的字符串按"可疑"处理并拒绝：能走到这儿的 IP 都来自 DNS 或字面量，
    正常的解析结果不会解不开。
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return "无法识别该地址"

    # ::ffff:127.0.0.1 这类 IPv4 映射地址会被某些解析器原样返回，
    # 换算回 IPv4 再判一次，否则 127.0.0.1 换个写法就绕过去了。
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped

    if address.is_loopback:
        return "这是本机环回地址"
    if address.is_private:
        return "这是内网地址"
    if address.is_link_local:
        return "这是链路本地地址（常见于云主机元数据服务）"

    for network in _BLOCKED_NETWORKS:
        if address.version == network.version and address in network:
            return "这属于保留网段"
    return None


def _describe_http_error(exc: httpx.HTTPError) -> str:
    """把网络异常翻译成给模型看的说明。

    连不上、证书不对、对方中途断开，处置方式各不相同：前两种要请用户检查环境，
    最后一种重试一次往往就好了，所以分开说。
    """
    if isinstance(exc, httpx.ConnectError):
        return f"无法连接到该地址 - {exc}（请确认地址可达，或稍后重试）"
    if isinstance(exc, httpx.TooManyRedirects):
        return "跳转次数过多，已放弃"
    text = str(exc)
    if "certificate" in text.lower() or "ssl" in text.lower():
        return f"TLS 证书校验失败 - {exc}（该站点的证书可能有问题，已拒绝连接）"
    return f"请求失败 - {type(exc).__name__}: {exc}"
