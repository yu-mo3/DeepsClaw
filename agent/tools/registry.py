"""工具注册表。

集中保管所有可被 Agent 调用的 Tool 实例，并向两个方向提供接口：

- 对模型：get_definitions 导出全部工具的 function calling 定义，拼进请求体；
- 对 agent 循环：execute 按模型给出的工具名和参数分发到对应工具并取回结果。

典型用法::

    registry = ToolRegistry()
    registry.register(WeatherTool())
    registry.register(ReadFileTool())

    # 请求模型时
    tools = registry.get_definitions()

    # 收到 tool_calls 后
    for call in tool_calls:
        result = await registry.execute(call.name, call.arguments)
"""

import logging
from typing import Any, Iterator

from agent.tools.base import Tool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Tool 实例的注册与分发中心。

    持有 name -> Tool 的映射，负责三件事：

    1. **注册**：register 收编工具实例，重名会被拒绝，避免"看似注册成功、
       实际被覆盖"这类难查的问题。
    2. **暴露定义**：get_definitions 汇总所有工具的 OpenAI tools JSON，
       上层每次请求前调用即可，新增工具无需改动请求拼装代码。
    3. **分发执行**：execute 按名字查找并 await 工具，同时兜住所有异常——
       工具内部报错不应中断整个 agent 循环，而应把错误文本交回给模型，
       让模型有机会换个参数或换条路重试。

    实例本身不是线程安全的，注册应在启动阶段一次性完成；注册完成后只读使用。
    """

    def __init__(self) -> None:
        """初始化空注册表。"""
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具实例。

        Args:
            tool: Tool 子类实例，其 name 属性作为映射键。

        Raises:
            ValueError: 已存在同名工具时抛出。重名会让后注册的一方静默顶掉
                先注册的一方，而模型的 tools 列表里只留一份定义，排查起来很绕，
                因此在注册阶段就直接拦下。
        """
        if tool.name in self._tools:
            raise ValueError(f"工具名重复: {tool.name!r} 已注册，请检查 name 是否唯一")
        self._tools[tool.name] = tool

    def iter_tools(self) -> Iterator[Tool]:
        """遍历已注册的工具实例。

        与 list_tools() 的区别是它给的是**对象**而不是名字。需要它的场景是"把工具
        复制到另一个注册表"——spawn_subagent 给子 agent 装配工具集时就得这么做。少了
        这个方法，调用方只能去读私有的 _tools，那是把内部结构当成契约。

        返回迭代器而不是列表：调用方多为"筛一遍再注册"，迭代器能一路过、不必先物化
        一份中间列表。注册表在启动阶段一次性建好后只读，因此遍历期间不会被改动。
        """
        return iter(self._tools.values())

    def get_definitions(self) -> list[dict[str, Any]]:
        """导出所有工具的 function calling 定义。

        Returns:
            可直接放入请求体 tools 字段的列表，每项形如
            ``{"type": "function", "function": {...}}``。空注册表返回空列表。
        """
        return [tool.to_function_definition() for tool in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """按名字找到工具并执行。

        Args:
            name: 工具名，来自模型的 tool_call。
            arguments: 工具参数，已由上层从 JSON 解析为 dict。

        Returns:
            工具返回的结果字符串；工具不存在或执行抛异常时，返回可读的错误
            描述（而非抛出），使调用方总能拿到一段能回传给模型的文本。
        """
        tool = self._tools.get(name)
        if tool is None:
            available = ", ".join(self._tools) or "无"
            return f"错误：工具 {name!r} 不存在。当前可用工具：{available}"

        try:
            return await tool.execute(**arguments)
        except Exception as exc:
            # 捕获 Exception 而非 BaseException，保证 CancelledError / KeyboardInterrupt
            # 等控制流异常仍能正常向上传播，不会被当成工具错误吞掉。
            logger.exception("工具 %s 执行失败，参数: %s", name, arguments)
            return f"错误：工具 {name!r} 执行失败 - {type(exc).__name__}: {exc}"

    def list_tools(self) -> list[str]:
        """返回所有已注册工具的名称，顺序与注册顺序一致。"""
        return list(self._tools)
