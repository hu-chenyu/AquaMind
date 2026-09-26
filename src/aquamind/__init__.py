"""AquaMind：LLM 应用在并发阶梯负载下的质量退化测试工具（库/CLI 形态）。

本模块是整个包的公共入口，对外暴露包的元信息。

包的分层结构与对应里程碑如下（各层将在后续里程碑逐步落地）：

    aquamind.models     —— 数据契约层：pydantic 模型定义（M1）
    aquamind.adapters   —— 被测对象接入层：OpenAI 兼容协议优先（M1）
    aquamind.scorers    —— 评分层：精确匹配与 LLM-as-Judge（M2）
    aquamind.load_engine —— 负载引擎层：并发阶梯编排（M3）
    aquamind.metrics    —— 指标计算层：TTFT/ITL、百分位、TPS、goodput（M3 起）
    aquamind.report     —— 报告渲染层：jinja2 输出单文件 HTML（M4-D09）
    aquamind.cli        —— 命令行入口（M1）
"""

# 包版本号。此处与 pyproject.toml 的 [project].version 保持手工双写：
# 采用双写而非运行时动态读取元数据，是为了让 import aquamind 不触发
# 任何文件系统解析，保证导入零副作用、零耗时抖动。
__version__ = "0.0.3"

# 显式声明对外导出符号清单，避免 `from aquamind import *` 污染调用方命名空间。
__all__ = ["__version__"]
