# AquaMind

> **LLM 应用在并发阶梯负载下的质量变化判定工具**——回答"这个下降是不是真的"。

[![CI](https://github.com/hu-chenyu/AquaMind/actions/workflows/ci.yml/badge.svg)](https://github.com/hu-chenyu/AquaMind/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

---

## 它解决什么问题

给 LLM 应用做压测的工具能量出延迟与吞吐，做评测的工具能在单并发下打出质量分；但当并发升高、质量分开始下降时，两边都不回答一个关键问题：**这个下降是真实的退化，还是噪声？** 没有统计推断的"掉了 3%"，和掷硬币没有区别——样本量多大、置信区间多宽、效应量多大、测法本身有没有引入误差，都无从判断。

AquaMind 的定位是**结论可辩护性层**，用三层证据回答这个问题。**这三层是项目的目标形态，M1 只交付了采集与回放底座**：

| 证据层 | 作用 | 交付里程碑 |
|---|---|---|
| **测量自证** | 先排除工具自身在并发下引入的四类污染源——循环滞后（GIL 计时污染）、协调遗漏、截尾、冷启动——再谈结论；低于噪声底一律不报 | M3 起（循环滞后监测）／M4（S5-S8 模块） |
| **预注册冻结** | 方法、阈值、降级链与采集协议在看到任何真实数据之前锁定并哈希入库，杜绝事后调参 | M3（Day46，早于 Day47 的唯一一次正式观测运行） |
| **统计推断** | 配对 BCa 置信区间、效应量、配对置换检验、趋势检验与多重比较校正，回答"变化是否显著" | M4 |

四类污染源的含义：**循环滞后**——计时代码本身也占 CPU，测出的耗时里混进了工具自己的开销；**协调遗漏**——系统一卡，压测工具自己跟着降速，于是最该被记录的那批请求根本没发出去；**截尾**——只统计跑完的样本，失败与超时的被丢掉，从而高估成功率；**冷启动**——首次调用的初始化开销被算进稳态，污染后续测量。四者都属于"量尺坏了"，不是"被测物变了"。

三层按「先信数据、再谈显著性」的顺序排列：**测量自证排在最前**，因为统计推断再严谨，测出来的数若是工具自己污染出来的，置信区间只会把污染包装得更漂亮。预注册冻结约束的是"分析不能事后调"，统计推断回答的是"变化是否显著"，两者都建立在测量本身可信的前提上。

目标形态是在**开环并发阶梯**（恒定到达率，不掩盖排队）下联合测量延迟、质量与成本，输出五态四码门禁（可直接接入 CI）与自包含 HTML 报告。**当前（M1）已交付的是这条链路上的采集与回放底座**：SSE 帧解析、VCR 录制与时序回放、预算熔断、多模型适配与错误分类；负载引擎、指标计算、测量自证、统计层与门禁报告随 M3-M4 交付。适合需要在流水线上对 LLM 应用做"性能 × 质量"联合放行、且结论要能被审查与复算的测试、效能与平台工程师。被测对象为 LLM 应用（对话、RAG、智能体等），不做推理引擎层优化。

---

## 核心特性

### 已交付（M1）

| 能力 | 说明 |
|---|---|
| **用例契约与加载** | pydantic 数据契约（`ExpectedSpec` / `ScoreTag` / `TestCase`）与 YAML/JSON 加载器，加载失败带文件与行号报错 |
| **多模型适配与错误分类** | 以 OpenAI 兼容协议为核心接入面（覆盖多数服务商、网关与本地推理服务，含无鉴权端点），另有本地函数适配器；重试退避与错误分类：429/5xx/超时/网络抖动可重试，认证失败不重试 |
| **预算熔断** | token 与成本双维度账本：达阈值快速失败、超限不部分扣减、并发记账原子；当前为后置记账，在途超支上界 = 在途 × 单请求，两段式 reserve/settle 前置预留随 D43 交付 |
| **SSE 帧解析** | 逐 chunk 增量解析，兼容 `data:` / `event:` / `id:` / `retry:` 四类字段，带 UTF-8 容错与缓冲上限 |
| **VCR 录制与时序回放** | 录制响应内容与 chunk 到达节奏，支持加速/减速回放（绝对时刻对齐，偏差 <1ms）；CI 全程零 API key |

### 规划中（M2-M6，尚未交付）

| 能力 | 交付里程碑 | 说明 |
|---|---|---|
| 评分器与 Agent 用例三层断言 | M2 | 精确匹配评分器与 LLM-as-Judge 适配器；Agent 用例断言按参数 > 顺序 > 工具三层组织 |
| 批量执行 | M2 | 离线批量跑用例，部分失败出部分报告 |
| 指标计算与五时间戳采集 | M3 | TTFT / ITL 等五时间戳、百分位、TPS |
| 负载引擎 | M3 | 开环并发阶梯（恒定到达率 / Poisson 调度）、LoadProfile 参数抽样、AIMD 429 自整定与限流退避联动、参数矩阵快照可追溯、goodput 质量门控 |
| 测量自证与统计推断（S1-S8） | M4 | S5-S8 测量自证（本项目首要差异点，暂无同类工具覆盖）：循环滞后自校准、协调遗漏报告、截尾分离、冷启动分离，低于底噪不报告；S1-S4 统计推断：配对 BCa 置信区间、噪声基线、效应量、配对置换 + Jonckheere-Terpstra 趋势检验 + Holm 校正；方法与阈值随预注册冻结（M3 Day46）一并入库 |
| 五态四码门禁与报告 | M4 | 五态四码退出（PASS/STABLE=0、FAIL=1、WARN=2、INCONCLUSIVE=3，UNVERIFIABLE 为原因码族）；自包含 HTML 报告：三行人话层 + 11 码映射 + 内联 SVG，断网可开 |
| 轻量持久化与回放复核 | M5 | SQLite 最小元数据表 + 两次运行对比（`compare`，强制 case_id 一致）；真实端点的正式观测运行仅一次（3 档 ×1 同批，纯描述性） |
| pytest 插件 | M6 | 把质量门禁挂进既有流水线 |

---

## 快速开始

### 安装

要求 Python 3.11+。

```bash
pip install aquamind
```

从源码开发（含测试与静态检查工具链；本地测试 `pytest`、静态检查 `ruff` + `mypy`）：

```bash
git clone https://github.com/hu-chenyu/AquaMind.git
cd AquaMind
pip install -e ".[dev]"
```

### 当前可用

命令行骨架：

```bash
aquamind --help
aquamind version
aquamind run run.json          # 发起一次流式补全，实时输出到终端
```

`aquamind run` 读取一份 JSON 运行配置（字段口径见 `aquamind.cli.RunSpec`），`--speed N` 控制输出速度上限（字符/秒，`0` 为全速，默认 `0`）；配置不可读、请求失败或 SSE 解析失败时以退出码 `1` 结束。

用例文件为 YAML/JSON：顶层为数组，每条用例 `input` 与 `expected` 必填（`context` / `score_tags` / `weights` 可选）：

```yaml
# cases.yaml
- input: "水的沸点是多少摄氏度？"
  expected:
    type: contains      # exact / regex / contains / judge
    value: "100"
  score_tags:
    - name: accuracy
  weights:
    accuracy: 1.0
```

加载用例并调用一次 OpenAI 兼容端点（加载失败与契约不符会带文件与行号报错）：

```python
import asyncio

from aquamind.adapters import OpenAIAdapter
from aquamind.loaders import load_cases

cases = load_cases("cases.yaml")
adapter = OpenAIAdapter(base_url="http://localhost:8000/v1", model="my-model")


async def main() -> None:
    for case in cases:
        resp = await adapter.acomplete([{"role": "user", "content": case.input}])
        print(case.input, "->", resp.content)


asyncio.run(main())
```

### 计划中的 CLI 子命令

能力层面的规划见上文「规划中（M2-M6）」，此处只列尚未提供的子命令：

| 计划中的子命令 | 交付里程碑 |
|---|---|
| 评分器与 Judge、批量出分 | M2 功能基线 |
| 并发阶梯压测与退化曲线 | M3 负载引擎 |
| S1-S8 统计判定、双门禁报告、`gate` / `report` / `calibrate` 子命令 | M4 联动分析 |
| `aquamind compare` 两次运行对比、回放复核 | M5 持久化与复核 |
| pytest 插件、v1.0 正式发布 | M6 打磨交付 |

> 当前 0.0.4 为占位版本：除"当前可用"部分外，评测能力随里程碑逐步交付。

---

## 项目结构

```
src/aquamind/
├── __init__.py        # 包入口与版本号（__version__）
├── cli.py             # 命令行入口（typer）：aquamind --help / version / run（含 --speed）
├── config.py          # 全局配置（pydantic-settings；AQ_ 前缀环境变量 / .env）
├── exceptions.py      # 异常体系（AquaMindError → Config / Loader / Adapter / Replay / Budget）
├── loaders.py         # YAML/JSON 用例加载与契约校验（load_cases）
├── models/
│   └── testcase.py    # 用例契约：TestCase / ExpectedSpec / ScoreTag
├── adapters/
│   ├── base.py        # 适配器契约：BaseAdapter / AdapterResponse
│   ├── callable.py    # 本地函数适配器（CallableAdapter，CI 零 key）
│   └── openai.py      # OpenAI 兼容端点适配器（非流式）
├── retry.py           # 错误分类与退避重试（ErrorKind / classify_error / with_retry）
├── budget.py          # 预算账本（token / 成本双维度，并发安全）
├── sse.py             # SSE 帧解析（SSEStreamParser / iter_stream_deltas）
└── replay.py          # VCR 录制与时序回放（record / play_timed / replay_request）
```

`tests/` 为对应单元测试（pytest 全绿）；随后续里程碑将新增 `scorers/`（评分层，M2）、`load_engine/` 与 `metrics/`（负载与指标，M3）、`report/`（报告渲染，M4）等模块。

---

## 路线图

| 里程碑 | 内容 | 结束时可演示的产物 |
|---|---|---|
| **M1 底座（Day1-18）** | 多模型适配层 + SSE 流式采集骨架 + CLI | 可回放的调用底座：两种适配可切换；错误分类全绿；CLI 退出码正确；cassette 时序回放（绝对对齐偏差 <1ms）；CI 覆盖率门禁 + 零 key 回放 |
| **M2 功能基线（Day19-32）** | 评分器 + 用例管理 + 批量执行 + 质量基线 | 10 条用例出分与理由可复跑；评分器目标断言集（regex / contains / excludes / keywords / json，共五类；与当前契约层的四类 `exact` / `regex` / `contains` / `judge` 是不同集合）与 Judge 全绿；Agent 三层断言正确；坏 JSON 落不可判（UNJUDGEABLE） |
| **M3 负载引擎（Day33-52）** | 负载模型 + 性能指标 + AIMD 限流 + 参数矩阵 | 并发压测输出延迟百分位 / TPS / goodput；退化曲线 v1（mock 标定，拐点复测 ≤5%）；AIMD 升降有效；参数矩阵快照可追溯；预注册哈希入库 |
| **M4 联动分析（Day53-76）** | 质量 × 性能联动 + S1-S8 统计 / 测量校验 + 双门禁 + 报告 | "并发下的质量变化"自包含 HTML 报告（三行人话层 + 11 码 + 五态四码端到端）；S1-S4 / S5-S8 全绿；统计三层自证（对拍 / 蒙卡 / meta） |
| **M5 持久化与复核（Day77-86）** | SQLite 最小元数据表 + 回放复核（零 API） | 运行落库与两次运行对比（compare，case_id 不一致直接拒绝）；cassette 回放复核复算一致；三层数据源分区不混写 |
| **M6 打磨交付（Day87-94）** | 覆盖率 ≥90% + 中文文档 + ADR + 复盘 + v1.0 发布 | pytest 插件（退出码接线）；中文文档与 ADR 补齐；rc1 → v1.0 发布至 PyPI（OIDC） |

---

## 非目标（明确不做）

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

---

## 开发状态

- **当前进度**：M1 底座进入收口阶段；Day1-12 已完成，D12（CLI run 骨架，原计划 Day15）于 2026-10-10 提前交付。原计划落在 M1 的 D10 流式采集骨架与 D11 流式适配器经评估归入 M3 前置（Day36 / Day33），M1 余下为验收与收口工作。94 天开发计划至 **2026-12-31**（详见 [docs/ROADMAP.md](./docs/ROADMAP.md)）。
- **已交付**：配置与异常体系、用例契约与 YAML/JSON 加载器、适配器底座（本地函数 + OpenAI 兼容）、重试退避与错误分类、预算账本、SSE 帧解析器（四类协议变体：LF/CRLF 行尾、注释与心跳行、`event`/`id`/`retry` 字段、单事件多行 `data` 拼接；另含截断容错）、VCR 录制与时序回放、`aquamind run` 流式输出接线；单元测试全绿。
- **当前版本**：0.0.4（占位版）——除"快速开始"中标注可用与计划中的能力外，评测功能随 M1-M6 里程碑逐步交付。
- **已知限制**：当前版本真实存在的限制、以及预注册冻结后的变更纪律（变更 = 新哈希 + 显式 amendment），见 [KNOWN_LIMITATIONS.md](./KNOWN_LIMITATIONS.md)。

---

## 贡献

发布前合规扫描的词表不随仓库分发，需在仓库根自建 `.wordlist.local`（每行一个禁止模式，`#` 开头为注释），或用 `--wordlist PATH` / `AQUAMIND_SCAN_WORDLIST` 指定路径；词表缺失时脚本直接失败而非静默放行。

---

## License

[MIT](./LICENSE) © 2026 hu-chenyu
