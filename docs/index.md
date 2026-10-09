# AquaMind 文档

**AquaMind —— LLM 应用在并发阶梯负载下的质量变化判定工具（库/CLI 形态）。**

把“负载让质量掉多少”变成可统计判定、可复现、可门禁的结论：同一并发阶梯下联动测量
延迟 / 质量 / 成本三曲线，退化结论先过统计（S1-S4）与测量（S5-S8）双重校验，再由 SLO×质量双门禁裁定。

> **当前状态**：0.0.4 占位包，核心能力开发中。下方多数页面为占位，将随里程碑逐步补全。

## 文档导航

| 文档 | 内容 | 状态 |
|---|---|---|
| [快速上手](./quickstart.md) | 安装、校验、五分钟跑通第一次评测 | 占位，M2 补全 |
| [完整教程](./tutorial.md) | 按被测对象分类的系列教程索引 | 占位，M3 起逐步补全 |
| [API 参考](./api-reference.md) | 由源码 docstring 自动生成的参考 | 占位，M2 起补全 |

## 架构决策记录

技术选型与架构判断均记录在 ADR 中，每篇统一采用「决策 / 备选 / 边界」三层结构：
说明选了什么、放弃了什么及其代价、以及什么条件下这条判断不再成立。

| 编号 | 主题 |
|---|---|
| [0001](./adr/0001-library-not-platform.md) | 选择纯 Python 库形态，而非 Web 平台形态 |
| [0002](./adr/0002-asyncio-concurrency.md) | 评估执行以 asyncio 单线程事件循环为主 |
| [0003](./adr/0003-openai-compatible-protocol.md) | 以 OpenAI 兼容接口作为唯一硬承诺协议 |
| [0004](./adr/0004-pydantic-v2.md) | 以 pydantic v2 作为数据模型与边界校验层 |
| [0005](./adr/0005-judge-structured-output.md) | 裁判模型强制结构化输出，不解析自由文本 |
| [0006](./adr/0006-typer-cli.md) | 以 typer 实现命令行入口 |
| [0007](./adr/0007-single-file-html-report.md) | 报告以 jinja2 渲染为自包含单文件 HTML |
| [0008](./adr/0008-timing-replay.md) | 时序保真回放采用同步生成器 + 绝对时刻对齐 + 可测偏差 |

## 项目计划

- [开发计划](./ROADMAP.md) —— 项目定位、分层架构、方法学与里程碑安排。

## 其他

- 源码仓库：<https://github.com/hu-chenyu/AquaMind>
- 许可证：[MIT](../LICENSE)
