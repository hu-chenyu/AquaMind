# API 参考

> **本文为占位文档，内容尚未补充。**
> 本站计划在 **M2** 起启用 `mkdocs-material` + `mkdocstrings`，从源码的 docstring
> **自动生成** API 参考页面。届时本文将成为自动生成的入口索引，而非手工维护的清单。

## 为什么自动生成

API 参考与源码一旦由人手工维护两份，二者的偏差只是时间问题。本项目选择由 docstring
单向生成 API 页面：源码是唯一事实来源，参考文档是其派生产物。因此在本文所列能力真正
落地之前，此处保留占位，避免出现与实际代码不一致的描述。

## 当前可用

顶层导出由 `aquamind.__all__` 承诺，当前**仅含版本号**——这是刻意的最小承诺面设计，
避免 `from aquamind import *` 污染调用方命名空间：

```python
import aquamind

aquamind.__version__  # "0.0.4"
```

子模块 API 不受 `__all__` 限制，已可直接导入使用。M1 已交付的公开符号如下
（可用 `dir()` 与 `help()` 自行核对）：

| 模块 | 公开符号 |
|---|---|
| `aquamind.models` | `TestCase` / `ExpectedSpec` / `ScoreTag` |
| `aquamind.loaders` | `load_cases` / `LoaderError` |
| `aquamind.adapters` | `BaseAdapter` / `AdapterResponse` / `CallableAdapter` / `OpenAIAdapter` |
| `aquamind.exceptions` | `AquaMindError` 及其 `Config` / `Loader` / `Adapter` / `Replay` / `Budget` 子类 |
| `aquamind.config` | `Settings` / `load_config` |
| `aquamind.retry` | `ErrorKind` / `classify_error` / `is_retryable` / `with_retry` |
| `aquamind.budget` | `Budget` / `BudgetError` / `BudgetExceeded` |
| `aquamind.sse` | `SSEEvent` / `SSEStreamParser` / `parse_sse_events` / `iter_stream_deltas` |
| `aquamind.replay` | `Cassette` / `record` / `find_match` / `play` / `play_timed` / `TimingDeviation` / `ReplayError` |
| `aquamind.cli` | `app` / `run` / `version` |

子模块的**稳定面自 M2 起承诺**（接口随评分层与批量执行接入而扩展）；
M1 阶段这些符号可用但不承诺跨版本稳定。

## 计划中的模块结构（待补充）

以下模块按分层设计，将随里程碑逐步落地。**注意：这是既定的分层方案，不是当前可用清单。**

| 层 | 模块 | 职责 | 里程碑 | 当前状态 |
|---|---|---|---|---|
| 契约层 | `aquamind.models` | pydantic 模型定义 | M1 | 已交付 |
| 接入层 | `aquamind.adapters` | 被测对象接入 | M1 | 已交付 |
| 评分层 | `aquamind.scorers` | 精确匹配与 LLM-as-Judge 裁判 | M2 | 规划中 |
| 引擎层 | `aquamind.load_engine` | 并发阶梯编排 | M3 | 规划中 |
| 指标层 | `aquamind.metrics` | 流式指标（TTFT/ITL/百分位/TPS/goodput） | M3 起 | 规划中 |
| 报告层 | `aquamind.report` | 报告渲染 | M4-D09 | 规划中 |
| 命令行 | `aquamind.cli` | 命令行入口 | M1 | 已交付（`run` 子命令在 M1-D12 接入） |

各层之间的调用关系，见 [开发计划](./ROADMAP.md) 中的分层架构图。

## 生成方式（待补充）

- **待补充**：本地预览 API 站点的启动命令
- **待补充**：docstring 书写规范（摘要行、参数、返回值、抛出）
- **待补充**：公共接口与内部接口的划分约定

---

相关文档：[文档首页](./index.md) ｜ [快速上手](./quickstart.md) ｜ [完整教程](./tutorial.md) ｜ [架构决策记录](./adr/)
