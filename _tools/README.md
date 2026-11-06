# _tools —— 图表生成 / 校验脚本（分析产物，非项目运行时代码）

本目录是**本次代码分析时生成的辅助脚本**，不参与 DeepsClaw 运行。可以直接删除，
或留着以后改图：改完重跑 `python _tools/build.py` 就会重新生成根目录下的**三份 drawio**。

## 生成的三份图表（项目根目录）

| 文件 | 页签 | 内容 |
|---|---|---|
| `../DeepsClaw-框架图.drawio` | `01-系统架构` | 8 层架构 + 依赖方向 + 装配要点 |
| | `05-技术栈` | 核心技术栈六大块 |
| `../DeepsClaw-业务流程图.drawio` | `02-消息时序` | 六泳道时序图（用户→渠道→总线→网关→Agent→模型/工具→回复） |
| | `03a-工具循环与压缩` | 工具调用循环与死循环熔断 / 历史压缩 |
| | `03b-MCP与子智能体` | MCP 连接生命周期 / 子智能体派生 |
| | `04-启动退出` | 启动装配 + 运行/退出（退出码语义） |
| `../DeepsClaw-技术栈.drawio` | `05-技术栈` | 核心技术栈（单页，单独成文件便于分享） |

## 脚本清单

| 文件 | 作用 |
|---|---|
| `dxlib.py` | drawio XML 生成库：样式表、`Page.A()/L()`、文本框自动缩字号、`write()` |
| `pg1.py` | 第 1 页：系统架构（8 层 + 依赖方向） |
| `pg2.py` | 第 2 页：消息时序（六泳道） |
| `pg3.py` | 第 3a 页：工具调用循环与熔断 / 历史压缩 |
| `pg3b.py` | 第 3b 页：MCP 连接生命周期 / 子智能体派生 |
| `pg4.py` | 第 4 页：启动 / 运行 / 退出生命周期 |
| `pg5.py` | 第 5 页：技术栈（6 大块） |
| `build.py` | 按三组生成三份 drawio，并做**几何重叠校验**与**文本溢出校验** |
| `verify_drawio.py` | 校验 XML 结构：每页有根 cell、边的 source/target 不悬空 |
| `render_preview.py` | 用 Pillow 把每页渲染成缩略图（仅版式自检，中文字体缺失会显示方块） |
| `preview/` | 上一步产出的预览 PNG |
| `agent-modules-deep-read.md` | `agent/` 五个核心模块的逐行精读记录（行号级清单 + 隐患清单），作为 `../ARCHITECTURE.md` 的支撑材料 |

## 用法

```bash
cd <项目根>
python _tools/build.py            # 重新生成三份 drawio 并自检
python _tools/verify_drawio.py    # 校验 XML 结构
python _tools/render_preview.py   # 渲染版式预览到 _tools/preview/
```

## 自检结果（最后一次运行）

```text
written: DeepsClaw-框架图.drawio         pages=2 bytes=39124
written: DeepsClaw-业务流程图.drawio       pages=4 bytes=50729
written: DeepsClaw-技术栈.drawio         pages=1 bytes=17310
groups=3 pages=7 cells=253 problems=0        # 无几何重叠、无越界、无文字溢出
每页均有根 cell，边无悬空引用 → XML structure OK
```

> 说明：`render_preview.py` 的缩略图里中文会显示成方块，那是 **PIL 找不到中文字体**导致的，
> 不是 drawio 文件的问题 —— 用 draw.io 打开时中文正常。
