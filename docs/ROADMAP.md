# AquaMind 项目路线图（公开版）

> 公开路线图：项目定位、分层架构、方法学与里程碑安排，与 [README](../README.md) 同源维护。

## 项目定位

**LLM 应用在并发阶梯负载下的质量变化判定工具**——回答"这个下降是不是真的"。

AquaMind 的定位是**结论可辩护性层**，用三层证据回答这个问题（与 README 同序）：先证明量尺没坏，再谈结论：

1. **测量自证**：先排除工具自身在并发下引入的四类污染源——循环滞后（GIL 计时污染）、协调遗漏、截尾、冷启动——再谈结论；低于噪声底一律不报；
2. **预注册冻结**：方法、阈值、降级链与采集协议在看到任何真实数据之前锁定并哈希入库，杜绝事后调参（Day46，早于 Day47 唯一一次 live）；
3. **统计推断**：配对 BCa 置信区间、效应量、配对置换检验、趋势检验与多重比较校正，回答"变化是否显著"。

被测对象为 LLM 应用（对话、RAG、智能体等），不做推理引擎层优化。

## 分层架构

```
cli.py                     # 命令行入口（typer）
adapters/                  # 适配层：OpenAI 兼容协议 + 本地函数兜底（base / callable / openai）
sse.py                     # 采集层：SSE 帧解析（四方言 + 截断容错）
replay.py                  # 采集层：VCR 录制与时序回放（绝对时刻对齐，偏差 <1ms）
retry.py / budget.py       # 韧性层：错误分类与退避重试 / 预算账本
config.py / exceptions.py  # 基础设施：配置（AQ_ 前缀）与异常体系
models/ / loaders.py       # 契约层：用例契约与 YAML/JSON 加载

# 随里程碑新增：
scorers/                   # M2：评分器与 Judge（精确匹配 / rubric / 结构化裁判）
load_engine/ + metrics/    # M3：并发阶梯调度、LoadProfile、AIMD 429 自整定、参数矩阵
statistics.py              # M4：S1-S4 统计推断（BCa / 效应量 / 置换 / 趋势 + Holm）
measurement_validity.py    # M4：S5-S8 测量有效性（循环滞后 / 协调遗漏 / 截尾 / 冷启动）
gates.py + report/         # M4：五态四码门禁与自包含 HTML 报告
storage.py + compare.py    # M5：SQLite 最小元数据表与两次运行对比
pytest_plugin.py           # M6：pytest 插件（退出码接线）
```

## 方法学

- **开环并发阶梯**：恒定到达率 / Poisson 调度，不掩盖排队；LoadProfile 按思考时间与输入输出长度抽样，参数矩阵快照可追溯；
- **统计判定（S1-S4）**：配对 BCa 置信区间、噪声基线、效应量、配对置换 + Jonckheere-Terpstra 趋势检验 + Holm 校正；
- **测量有效性（S5-S8）**：循环滞后自校准、协调遗漏报告、截尾分离、冷启动分离——低于底噪不报告；
- **五态四码门禁**：PASS/STABLE=0、FAIL=1、WARN=2、INCONCLUSIVE=3，可直接接入 CI；`UNJUDGEABLE` 是「该条判定不可判」的状态码，`UNVERIFIABLE` 是与之配套的原因码族（两者不混用：`UNJUDGEABLE` 说发生了什么，`UNVERIFIABLE` 说为什么）；
- **三层自证**：scipy 对拍（6 位小数）、蒙特卡洛覆盖率回归、meta 故障注入验证；
- **时序保真回放**：chunk 级录制与调度回放，CI 全程零 API key。

## 里程碑

| 里程碑 | Day 范围 | 日期 | 核心交付物 |
| --- | --- | --- | --- |
| M1 底座 | Day1-18 | 09-29~10-16 | 多模型适配层 + SSE 流式采集骨架 + CLI + VCR 时序回放 + 预算熔断 |
| M2 功能基线 | Day19-32 | 10-17~10-30 | 评分器 + Judge + 用例管理 + 批量执行 + Agent 用例 + 质量基线 |
| M3 负载引擎 | Day33-52 | 10-31~11-19 | 负载模型 + 性能指标（百分位 / TPS / goodput）+ AIMD 429 自整定 + 参数矩阵 + 退化曲线 v1 + 预注册冻结（D46）+ 唯一 live（D47） |
| M4 联动分析 | Day53-76 | 11-20~12-13 | S1-S8 统计 / 测量校验 + 质量 × 性能联动 + 双门禁 + 五态四码 + 自包含 HTML 报告 |
| M5 持久化与复核 | Day77-86 | 12-14~12-23 | SQLite 最小元数据表 + compare 两次运行对比 + cassette 回放复核（零 API）+ 校准夹具 |
| M6 打磨交付 | Day87-94 | 12-24~12-31 | 覆盖率 ≥90% + 中文文档 + ADR 补齐 + rc1（D91）+ v1.0 PyPI 发布（OIDC，D94） |

## 关键口径

- **预注册冻结**：Day46，S1-S8 全部超参与阈值 + 方法降级链 + 采集协议冻结，早于 Day47 live；
- **live 观测唯一化**：Day47 全项目唯一一次 live（3 档 ×1，纯描述性）；Day84/85 为 cassette 时序回放复核（零 API）；
- **覆盖率**：各里程碑验收日全仓 ≥90%；
- **休息日**：Day38 / Day52 / Day73。

## 非目标

- **推理引擎调优**：不做 GPU / KV Cache / vLLM 等推理框架层优化；
- **平台化服务**：不做账号、权限、多人协作与集中式调度——库 + 命令行 + pytest 插件形态（见 ADR 0001）；
- **论文级人类对齐校准专项**：不做大规模多人双标注 IAA、ECE 校准曲线、κ 显著性研究——工程级最小校准（人工校准集 + κ 置信区间）与预注册冻结照常执行；
- **红队 / 攻击语料库**：不做提示注入攻防产品化；
- **多模态**：本期只覆盖文本类 LLM 应用；
- **语义相似度评分**：不引入 embedding 重依赖，功能评分以精确匹配 + Judge 为限；
- **外部观测后端集成**：不做 OTel 导出（观测后端由使用者自行建设）；
- **全文检索**：不做 FTS5 全文检索（持久化采用普通索引最小元数据表）；
- **持久化质量趋势线**：不做独立趋势线模块（两次运行对比已覆盖）；
- **报告交互 / 主题切换**：不做报告交互与主题切换（报告为自包含静态 HTML，见 ADR 0007）。

## ADR 索引

| 编号 | 决策 |
| --- | --- |
| [0001](adr/0001-library-not-platform.md) | 选择纯 Python 库形态，而非 Web 平台形态 |
| [0002](adr/0002-asyncio-concurrency.md) | 评估执行以 asyncio 单线程事件循环为主 |
| [0003](adr/0003-openai-compatible-protocol.md) | 以 OpenAI 兼容接口作为唯一硬承诺协议 |
| [0004](adr/0004-pydantic-v2.md) | 以 pydantic v2 作为数据模型与边界校验层 |
| [0005](adr/0005-judge-structured-output.md) | 裁判模型强制结构化输出，不解析自由文本 |
| [0006](adr/0006-typer-cli.md) | 以 typer 实现命令行入口 |
| [0007](adr/0007-single-file-html-report.md) | 报告以 jinja2 渲染为自包含单文件 HTML |
| [0008](adr/0008-timing-replay.md) | 时序保真回放采用同步生成器 + 绝对时刻对齐 + 可测偏差 |
| [0010](adr/0010-why-not-sse-standalone-package.md) | 不把 SSE 帧解析器抽为独立包发布 |

## 开发状态

- **当前进度**：Day1-12 已完成；D12（CLI run 骨架，原计划 Day15）于 2026-10-10 提前交付。当前为 **Day13**，D10 流式采集骨架 + 五时间戳契约是当日任务、尚未交付；D11（Day14）与 D13（Day16）尚未开始。94 天计划至 **2026-12-31**；
- **当前版本**：0.0.4（占位版）；v1.0 于 Day94（12-31）经 OIDC Trusted Publishing 发布至 PyPI；
- 使用说明见 [README](../README.md)。
