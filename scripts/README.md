# scripts/

发布前检查与实测基线两类工具。两者都不是运行时依赖：`pre_release_scan.py` 只在
提交前手动运行，`replay_baseline.json` 是一次运行产出的快照数据。

| 文件 | 性质 | 何时用 |
|---|---|---|
| `pre_release_scan.py` | 可执行脚本 | 提交前检查对外材料是否含禁止口径 |
| `replay_baseline.json` | 生成物（已入库） | 需要引用时序回放的实测数值时 |

---

## pre_release_scan.py

发布前合规扫描。检查两类问题：

1. **tracked 内容**里是否出现禁止模式——对外材料不应出现的口径；
2. **未跟踪文件名**里是否含内部材料关键词——避免被 `git add -A` 误带入库。

### 运行

```bash
python scripts/pre_release_scan.py
```

### 词表格式

每行一个禁止模式，`#` 开头为注释，空行忽略：

```
# 注释行
某个词
另一个词
```

### 词表来源（三级通道，优先级由高到低）

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | `--wordlist PATH` | 命令行显式指定 |
| 2 | `AQUAMIND_SCAN_WORDLIST` | 环境变量 |
| 3 | 仓库根 `.wordlist.local` | 默认；该文件已被 `.gitignore` 排除，不随仓库分发 |

**词表不入库是有意设计**：词表本身会包含它要禁止的词，一旦提交进版本库，
扫描器就成了「禁止这些词的文件里写着这些词」。克隆仓库后需自建
`.wordlist.local` 才能运行。

### 退出码

| 码 | 含义 | 触发条件 |
|---|---|---|
| `0` | 通过 | 未命中禁止模式，无未忽略的内部材料 |
| `1` | 命中 | tracked 内容命中禁止模式，或未跟踪文件名命中内部材料关键词 |
| `2` | 配置错误 | 词表文件不存在，或词表内无有效条目（全为注释/空行） |

三种码已实测确认。**`2` 是 fail-loud 设计**：词表缺失时脚本直接失败而非静默放行，
避免「没配词表 = 扫描通过」这种假绿。

---

## replay_baseline.json

VCR 录制回放的**实测基线快照**，由 `examples/replay_demo/generate_baseline.py` 生成。

### 记录什么

- 文本层面：请求指纹稳定性、状态码/响应体/文本一致率
- 时序录制层面：chunk 到达时刻的记录精度（单调性、3 位小数、JSON 往返、拼接对齐）
- 时序调度层面：按录下时刻调度回放的偏差（1.0x / 2.0x / 0.5x 三档），以及非法倍率、
  篡改 hash 的拦截结果

### 生成 / 重跑

```bash
python examples/replay_demo/generate_baseline.py
```

约需 10 秒，**写入会覆盖本文件**。跑之前确保工作区干净，便于从 diff 看出数值变化。

### 何时需要重跑

改动 `src/aquamind/replay.py`、`examples/replay_demo/generate_baseline.py`，或影响
时序录制/调度行为时。**不重跑的后果**：文档与 ADR-0008 引用的偏差数值会指向
一次已经不存在的行为。

### 当前状态

`generated_at` 为 2026-10-06，与 `replay.py`、生成器最后一次改动同属提交 `980ac29`，
三者同步，基线未过期。

### 关于数值波动

文件中的 `max_deviation_ms` / `mean_deviation_ms` / `elapsed_ms` 是**真实测量值**，
随机器与运行波动属正常——偏差下限取决于操作系统定时器粒度，环境口径见
[ADR 0008 边界一](../docs/adr/0008-timing-replay.md)。

而**一致率与守卫类字段是确定性结果**（是否匹配、是否拦截），重跑必然相同。
区分这两类很重要：前者变化不代表回归，后者变化才是。

`examples/replay_demo/README.md` 中展示的输出块是**某一次运行的示例**，与本文件
不保证逐位相同（该 README 已在块外说明这一点）。

---

## 两者与 CI 的关系

当前 CI 流水线**不运行**这两个文件：

- `pre_release_scan.py` 依赖本地 `.wordlist.local`（不入库），CI 上不存在；
- `replay_baseline.json` 需要先 `pip install -e ".[dev]"` 再跑生成器，属于耗时操作。

因此它们属于提交前的本地门禁，不是流水线门禁。
