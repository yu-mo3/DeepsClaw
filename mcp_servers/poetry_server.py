"""古诗词查询 MCP Server。

用 MCP 官方 SDK 的 FastMCP 简化模式暴露三个工具：按关键词搜诗、随机来一首、列出所有诗人。
诗词数据**硬编码在文件里**，不连数据库也不联网——它是一个自包含的示例服务：
进程一起来就能用，没有任何外部依赖，适合用来验证"agent 通过 MCP 拿到新能力"这条链路。

为什么选 stdio 而不是 HTTP：MCP 的 stdio 传输是**由宿主进程把 server 当子进程拉起来**的，
双方用标准输入输出上的 JSON-RPC 通信。好处是零端口、零网络配置、进程退出即释放，
最贴合"本地给 agent 加一个能力"的场景；缺点是只能本机用，且 server 不能在 stdout 上
打印任何非协议内容（见下面的注意事项）。

三条实现约束，改这个文件时别踩：

1. **stdout 是协议通道**。stdio 模式下，任何 @@BT@@print()@@BT@@ 都会被宿主当成 JSON-RPC 报文而
   解析失败。要打日志请写 stderr，或干脆用 logging（它的默认去向就是 stderr）；
2. **工具函数只认类型注解**。FastMCP 从函数签名和 docstring 自动生成 MCP 工具定义，
   所以参数要带类型、docstring 要写清用途——那里写的内容就是模型看到的"工具说明"；
3. **返回值必须是字符串**。本文件统一返回给模型看的文本，不是结构化数据；
   要返回结构化结果该用 @@BT@@-> dict@@BT@@ 并按 MCP 的输出 schema 声明。

单独调试（不接 agent，直接用 SDK 的客户端连它）::

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command="python", args=["mcp_servers/poetry_server.py"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print(await session.list_tools())
            print(await session.call_tool("random_poetry", {}))
"""

from __future__ import annotations

import random
from typing import Any

from mcp.server.fastmcp import FastMCP

#: 诗词库。字段固定为 标题 / 作者 / 朝代 / 正文 四项，正文用全角标点、单行存放。
#:
#: 硬编码而不是读文件：这是示例服务，"自包含"比"可扩展"更重要——换机器、换目录都
#: 不用带数据文件。真要扩成正经的诗库，把它换成读 JSON/CSV 即可，工具层的代码不用动。
POEMS: list[dict[str, str]] = [
    {
        "title": "静夜思",
        "author": "李白",
        "dynasty": "唐",
        "content": "床前明月光，疑是地上霜。举头望明月，低头思故乡。",
    },
    {
        "title": "春晓",
        "author": "孟浩然",
        "dynasty": "唐",
        "content": "春眠不觉晓，处处闻啼鸟。夜来风雨声，花落知多少。",
    },
    {
        "title": "登鹳雀楼",
        "author": "王之涣",
        "dynasty": "唐",
        "content": "白日依山尽，黄河入海流。欲穷千里目，更上一层楼。",
    },
    {
        "title": "相思",
        "author": "王维",
        "dynasty": "唐",
        "content": "红豆生南国，春来发几枝。愿君多采撷，此物最相思。",
    },
    {
        "title": "江雪",
        "author": "柳宗元",
        "dynasty": "唐",
        "content": "千山鸟飞绝，万径人踪灭。孤舟蓑笠翁，独钓寒江雪。",
    },
    {
        "title": "悯农",
        "author": "李绅",
        "dynasty": "唐",
        "content": "锄禾日当午，汗滴禾下土。谁知盘中餐，粒粒皆辛苦。",
    },
    {
        "title": "登乐游原",
        "author": "李商隐",
        "dynasty": "唐",
        "content": "向晚意不适，驱车登古原。夕阳无限好，只是近黄昏。",
    },
    {
        "title": "望庐山瀑布",
        "author": "李白",
        "dynasty": "唐",
        "content": "日照香炉生紫烟，遥看瀑布挂前川。飞流直下三千尺，疑是银河落九天。",
    },
    {
        "title": "早发白帝城",
        "author": "李白",
        "dynasty": "唐",
        "content": "朝辞白帝彩云间，千里江陵一日还。两岸猿声啼不住，轻舟已过万重山。",
    },
    {
        "title": "水调歌头",
        "author": "苏轼",
        "dynasty": "宋",
        "content": (
            "明月几时有？把酒问青天。不知天上宫阙，今夕是何年。"
            "我欲乘风归去，又恐琼楼玉宇，高处不胜寒。起舞弄清影，何似在人间。"
            "转朱阁，低绮户，照无眠。不应有恨，何事长向别时圆？"
            "人有悲欢离合，月有阴晴圆缺，此事古难全。但愿人长久，千里共婵娟。"
        ),
    },
    {
        "title": "如梦令",
        "author": "李清照",
        "dynasty": "宋",
        "content": "昨夜雨疏风骤，浓睡不消残酒。试问卷帘人，却道海棠依旧。知否，知否？应是绿肥红瘦。",
    },
    {
        "title": "青玉案·元夕",
        "author": "辛弃疾",
        "dynasty": "宋",
        "content": (
            "东风夜放花千树。更吹落、星如雨。宝马雕车香满路。凤箫声动，玉壶光转，一夜鱼龙舞。"
            "蛾儿雪柳黄金缕。笑语盈盈暗香去。众里寻他千百度。蓦然回首，那人却在，灯火阑珊处。"
        ),
    },
]

mcp = FastMCP("poetry-server")


def _format(poem: dict[str, str]) -> str:
    """把一首诗排成给模型看的文本。

    三行：标题行（书名的书名号 + 作者 + 朝代）、正文行。所有工具共用这一个排版，
    是为了让模型看到的"诗"长得一样——它按固定格式读，也好按固定格式复述给用户。
    """
    return (
        "《{title}》—— {author}（{dynasty}）\n{content}".format(
            title=poem["title"],
            author=poem["author"],
            dynasty=poem["dynasty"],
            content=poem["content"],
        )
    )


@mcp.tool()
def search_poetry(keyword: str) -> str:
    """按关键词搜索古诗词。

    在**标题、正文、作者名**三处查找：只搜标题正文的话，"李白"就搜不出东西，
    而用户问"有没有李白的诗"是最常见的问法之一。

    Args:
        keyword: 要查找的关键词，如"月"、"李白"、"明月"。空字符串会返回全部诗词——
            那不是错误，是"列出来看看"的自然用法。
    """
    word = keyword.strip()
    if not word:
        return "未找到包含该关键词的诗词"

    matched = [
        poem
        for poem in POEMS
        if word in poem["title"] or word in poem["content"] or word in poem["author"]
    ]
    if not matched:
        return "未找到包含该关键词的诗词"

    blocks = ["共找到 {} 首包含「{}」的诗词：".format(len(matched), word)]
    blocks.extend(_format(poem) for poem in matched)
    return "\n\n".join(blocks)


@mcp.tool()
def random_poetry() -> str:
    """随机返回一首古诗词。

    用途是"来一首活跃下气氛"或"随便给我看一首"——用户没有明确想要什么的时候，
    随机比让模型自己编一首靠谱，也顺带告诉模型这个诗库里都有什么。
    """
    return _format(random.choice(POEMS))


@mcp.tool()
def list_poets() -> str:
    """列出诗库里的所有诗人。

    去重后**按首次出现的顺序**返回，而不是排序：顺序反映的是诗库自身的编排，
    每次调用结果都一样，便于复现（随机排序会让同一次对话前后回答不一致）。
    """
    poets: list[str] = []
    for poem in POEMS:
        if poem["author"] not in poets:
            poets.append(poem["author"])
    return "共有诗人 {} 位：{}".format(len(poets), "、".join(poets))


if __name__ == "__main__":
    # stdio 传输：由宿主进程用子进程方式拉起本文件，双方在标准输入输出上走 JSON-RPC。
    # 这里**不能**再往 stdout 写任何东西，见模块文档的第一条约束。
    mcp.run(transport="stdio")
