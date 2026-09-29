"""工具系统的抽象基类。

所有可被 Agent 调用的工具都必须继承本模块的 Tool，并实现 name / description /
parameters 三个属性和 execute 方法。基类负责把工具自身的元信息组装成 OpenAI
兼容的 function calling 定义，供上层在请求模型时直接塞进 tools 字段。

典型用法::

    class WeatherTool(Tool):
        @property
        def name(self) -> str:
            return "get_weather"

        @property
        def description(self) -> str:
            return "查询指定城市的实时天气"

        @property
        def parameters(self) -> dict:
            return {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名称"},
                },
                "required": ["city"],
            }

        async def execute(self, city: str) -> str:
            return f"{city} 今天晴，25℃"
"""

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """Agent 工具的统一抽象基类。

    职责有三块：

    1. **自描述**：通过 name / description / parameters 向模型暴露"我是谁、
       能干什么、需要哪些参数"，这三者直接决定模型能否正确地选择并调用该工具，
       因此 description 要写清楚使用场景，parameters 要写清楚每个字段的含义。
    2. **执行**：execute 是工具的实际逻辑入口，统一声明为 async，这样耗时操作
       （网络请求、磁盘 IO）不会阻塞事件循环；纯同步的实现直接返回结果即可。
    3. **协议适配**：to_function_definition 把上述元信息翻译成 OpenAI 的
       tools JSON 格式，使新增工具无需改动上层的请求拼装代码。

    子类需要实现全部标的为 abstractmethod 的成员，否则实例化时会抛 TypeError。
    工具实例应当是无状态或只持有只读配置的，同一次会话中可能被并发调用。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称，模型据此发起调用。

        必须是全局唯一的标识符，建议用小写字母加下划线（如 read_file），
        同一份 tools 列表里出现重名会导致调用结果不确定。
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def description(self) -> str:
        """工具功能描述，是模型判断"什么时候该用这个工具"的主要依据。

        写清楚用途、适用场景和边界（比如"仅支持读取文本文件，不支持二进制"），
        描述含糊会直接导致模型误选或漏选工具。
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """参数的 JSON Schema 定义，描述 execute 接收的关键字参数。

        顶层固定为 object 类型，形如::

            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                },
                "required": ["path"],
            }

        无参数的工具返回 {"type": "object", "properties": {}}。
        属性名必须与 execute 的形参名一一对应，否则调用时会因关键字不匹配而报错。
        """
        raise NotImplementedError

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """执行工具逻辑，返回给模型的字符串结果。

        kwargs 由模型根据 parameters 定义生成，参数字段名与 parameters 中的
        properties 保持一致。

        实现约定：
        - 返回值统一为 str，结构化数据自行序列化成 JSON 字符串；
        - 业务层面的失败（文件不存在、接口报错等）应作为可读的错误信息返回，
          而不是抛异常——模型看到错误描述后可以自行纠正并重试；
        - 不要在此处吞掉编程错误（如 TypeError），让它们冒泡以便定位问题。
        """
        raise NotImplementedError

    def to_function_definition(self) -> dict[str, Any]:
        """组装成 OpenAI function calling 的 tools 定义。

        返回值通常整体放进请求体的 tools 数组::

            {
                "type": "function",
                "function": {
                    "name": ...,
                    "description": ...,
                    "parameters": {...},
                },
            }

        直接在子类实例上调用，无需参数。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
