"""技能（Skill）加载器。

把磁盘上的技能目录变成两样东西：

- **一段给模型看的目录**（build_skills_summary）：只列"有哪些技能、分别能干什么"，
  塞进 System Prompt；
- **一份按需取用的正文**（load_skill）：模型决定要用某个技能时，再用 read_file 把
  对应的 SKILL.md 读进来。

这个"目录 + 按需加载"的拆法是刻意的：技能正文动辄几百上千字，一个项目攒上十几个
技能，全塞进 System Prompt 就是几千 token 的常驻开销，而这些内容多数轮次压根用不到。
只放目录，模型在需要时才付那份 token，同时又能知道自己"有这个能力"。

技能目录结构（每个技能一个子目录，定义文件固定叫 SKILL.md）::

    skills/
    ├── weather/
    │   └── SKILL.md
    └── code-review/
        └── SKILL.md

SKILL.md 的 frontmatter 是可选的，写了就必须是文件开头的第一个块::

    ---
    name: weather
    description: 查询指定城市的实时天气和温度
    ---

    ## 使用方法
    ...

解析失败、文件缺失、目录不存在这些情况一律不抛异常：技能是"锦上添花"的能力，
它坏了不该让整个 agent 起不来。拿不到就当作没有这个技能。

典型用法::

    loader = SkillsLoader("skills")
    prompt += loader.build_skills_summary()      # 拼进 System Prompt
    content = loader.load_skill("weather")       # 模型要用时取正文
"""

import logging
import os
import re
from typing import Any

import yaml

logger = logging.getLogger(__name__)

#: 技能目录下的定义文件名，固定不变。
SKILL_FILENAME = "SKILL.md"

#: frontmatter 的分隔行：整行恰好是 ---（允许尾随空白与 Windows 的 \r）。
#: 用整行匹配而不是搜子串"---"，否则正文里的 --- 或 ------ 会被误判成结束标记。
_FRONTMATTER_FENCE = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)

#: 摘要的引导语。模型看到这段才会知道：技能正文不在眼前，得自己去读。
_GUIDE = (
    "你有以下技能可用。当你需要使用某项技能时，"
    "请先用 read_file 工具读取对应的 SKILL.md 文件获取详细指南。"
    "\n\n可用技能："
)


class SkillsLoader:
    """从技能目录发现并加载技能。

    只做两件事：**发现**（扫子目录、读 frontmatter）和**取用**（读正文）。
    不做缓存——技能文件是用户手写的，改完下一轮就该生效，为省几次几 KB 的磁盘
    读取去维护一套失效策略并不划算。

    skills_dir 可以是相对路径（相对进程当前目录）或绝对路径；构造时 abspath 固化，
    之后不受进程切换工作目录的影响。
    """

    def __init__(self, skills_dir: str = "skills") -> None:
        """初始化。

        Args:
            skills_dir: 技能根目录，默认 "skills"。目录不存在不算错误，
                此时所有查询都返回空值（见 build_skills_summary）。
        """
        self.skills_dir = os.path.abspath(skills_dir)

    def _parse_frontmatter(self, content: str) -> tuple[dict[str, Any], str]:
        """解析 SKILL.md 的 frontmatter，返回 (元数据, 正文)。

        只有"整行 --- 出现在文件开头"才认为有 frontmatter；找不到结束行、YAML 写坏、
        或者解析出来根本不是字典（比如整段是个字符串列表），统统按"没有 frontmatter"
        处理并原样返回内容——元信息坏了不该连带正文一起丢掉。

        Args:
            content: SKILL.md 的完整内容。

        Returns:
            (元数据字典, 去掉 frontmatter 的正文)；无 frontmatter 时返回
            (空字典, 原文)。
        """
        fences = list(_FRONTMATTER_FENCE.finditer(content))
        # 至少要有一对 ---，且第一个必须落在文件开头。
        if len(fences) < 2 or fences[0].start() != 0:
            return {}, content

        raw_yaml = content[fences[0].end():fences[1].start()]
        body = content[fences[1].end():].strip()

        try:
            metadata = yaml.safe_load(raw_yaml)
        except yaml.YAMLError as exc:
            # 只记 warning 不抛：YAML 写错是手写技能文件最常见的手误，
            # 让 agent 继续跑、用户下次改正即可。
            logger.warning("SKILL.md 的 frontmatter 不是合法 YAML（%s），已按无元信息处理", exc)
            return {}, content

        if not isinstance(metadata, dict):
            # 空 frontmatter 会解析成 None，这是合法写法，只是没有元信息——
            # 正文已经解析出来了，照常返回（丢弃它会让"有 frontmatter 但没写
            # name/description"的技能整段失效）。写成列表或字符串的则告警，
            # 后面所有取用都按字典来，那种文件多半是写错了。
            if metadata is not None:
                logger.warning(
                    "SKILL.md 的 frontmatter 不是键值对（%s），已按无元信息处理",
                    type(metadata).__name__,
                )
                return {}, content
            return {}, body

        return metadata, body

    def _discover(self) -> list[dict[str, Any]]:
        """扫描技能目录，返回 [{name, description, path, meta}]。

        path 与 skills_dir 保持同一形态拼接，所以当 skills_dir 是相对路径时，摘要里
        给出的 `skills/weather/SKILL.md` 能直接喂给 read_file——那个工具正是按
        工作区相对路径解析的。

        顺序按目录名排序，保证同样的技能集合每次输出一致（也便于前缀缓存命中）。
        """
        if not os.path.isdir(self.skills_dir):
            logger.debug("技能目录 %s 不存在，视为没有技能", self.skills_dir)
            return []

        try:
            entries = sorted(os.listdir(self.skills_dir))
        except OSError as exc:
            logger.warning("读取技能目录 %s 失败: %s", self.skills_dir, exc)
            return []

        found: list[dict[str, Any]] = []
        for entry in entries:
            skill_file = os.path.join(self.skills_dir, entry, SKILL_FILENAME)
            if not os.path.isfile(skill_file):
                continue  # 不是技能子目录（README、临时文件等）

            try:
                # errors="replace"：技能文件被别的编辑器写成 GBK 也不该让整份
                # 技能表消失，乱码总好过"没有这个技能"。
                with open(skill_file, "r", encoding="utf-8", errors="replace") as f:
                    metadata, _ = self._parse_frontmatter(f.read())
            except OSError as exc:
                # 单个技能读不动就跳过它，不能让一个坏文件把整份技能表抹掉。
                logger.warning("读取技能文件 %s 失败: %s", skill_file, exc)
                continue

            found.append(
                {
                    # name 缺失时退回目录名：目录名本来就是最适合当标识的东西。
                    "name": str(metadata.get("name") or entry),
                    "description": str(metadata.get("description") or "无描述"),
                    "path": skill_file,
                    "meta": metadata,
                }
            )

        return found

    def build_skills_summary(self) -> str:
        """拼出给 System Prompt 用的技能目录。

        每行一条：``- {name} ({相对路径}/SKILL.md)：{description}``，前面加一段引导语
        说明"正文要自己用 read_file 去读"——不给这句，模型会以为看到的这几行摘要
        就是技能的全部内容，然后照着摘要硬答。

        Returns:
            技能目录字符串；目录不存在或一个技能都没有时返回空字符串，调用方可以
            直接拼接（空串拼进 prompt 等于没加）。
        """
        skills = self._discover()
        if not skills:
            return ""

        lines = [_GUIDE, ""]
        for skill in skills:
            lines.append(f"- {skill['name']} ({skill['path']})：{skill['description']}")
        return "\n".join(lines)

    def load_skill(self, name: str) -> str | None:
        """按技能名取正文（已去掉 frontmatter）。

        name 是**子目录名**，不是 frontmatter 里的 name 字段：目录名是路径的一部分，
        用它才拼得出文件位置；两者不一致时以目录名为准（建目录时顺手写对即可）。

        Args:
            name: 技能目录名，如 "weather"。

        Returns:
            技能正文；目录或文件不存在、读取失败时返回 None。
        """
        skill_file = os.path.join(self.skills_dir, name, SKILL_FILENAME)
        if not os.path.isfile(skill_file):
            logger.debug("技能 %r 不存在（%s 不是文件）", name, skill_file)
            return None

        try:
            with open(skill_file, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as exc:
            logger.warning("读取技能 %r 失败: %s", name, exc)
            return None

        _, body = self._parse_frontmatter(content)
        return body

    def list_skills(self) -> list[dict[str, Any]]:
        """列出全部已发现的技能，供调试与管理使用。

        Returns:
            [{name, description, path}, ...]；没有技能时返回空列表（不是 None，
            调用方可以直接遍历）。
        """
        return [
            {"name": s["name"], "description": s["description"], "path": s["path"]}
            for s in self._discover()
        ]
