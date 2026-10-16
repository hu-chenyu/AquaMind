# AquaMind 评分契约示例（M2-D01 / Day19）

本目录是 AquaMind 评分输出契约（`Score` / `ScoreStep` / `ScoreTrace`）的最小可运行示例，
展示**一次评分的结论与证据如何成对落盘**：

1. **构造** —— 一条 `TestCase` 经两步评分（字段级打分 → 加权聚合）产出 `Score`（结论）
   与 `ScoreTrace`（追溯链路）；
2. **校验** —— 坏数据在构造期被拦住：分数越界、理由全空白、未知字段、追溯链终值与最终分
   不一致、时间戳缺时区，逐条定位到具体字段；
3. **不可变** —— 评分结果构造后即为终态（`frozen=True`），赋值被拒；
4. **序列化** —— `model_dump_json` / `model_validate_json` 往返无损，产物可直接落盘当证据。

完整的取舍论证见 [`docs/adr/0012-score-data-contract.md`](../../docs/adr/0012-score-data-contract.md)。

## 目录内容

| 文件 | 说明 |
| --- | --- |
| `demo.py` | 可运行示例：构造 / 校验 / 不可变 / 序列化四段演示，无需任何输入 |
| `README.md` | 本文件 |
| `../../scripts/score_contract_example.py` | 示例 JSON 生成脚本（本示例所依据的形态样板来源） |

## 运行命令

在**仓库根目录**执行：

```powershell
.venv\Scripts\python.exe examples/score_contract/demo.py
```

一键生成示例 JSON（三个场景：通过 / 失败 / 边界）：

```powershell
.venv\Scripts\python.exe scripts/score_contract_example.py
```

## 预期输出

`demo.py` 退出码 **0**，四个段落依次打印（节选）：

```text
1. 构造：一条用例 → 两步评分 → Score（结论）+ ScoreTrace（证据）
  评分结果           : score=0.5 passed=False
  追溯链步骤数       : 2
  链尾中间分/最终分  : 0.5 / 0.5
2. 校验：坏数据在构造期被拦住，错误定位到具体字段
  分数越界 1.5         -> 被拒，定位字段=['score']，错误类型=['less_than_equal']
  最终分≠链尾      -> 被拒：Value error, final_score 必须等于最后一步的 partial_score...
3. 不可变：评分结果是终态产物，构造后赋值一律被拒
  改写 score.score -> 被拒，字段=('score',)，类型=frozen_instance
4. 序列化：model_dump_json → model_validate_json 往返无损
  往返前后相等     : True
```

`score_contract_example.py` 输出单个 JSON 对象到 stdout，关键数字：

| 项目 | 值 |
| --- | --- |
| `schema` | `score-contract.v1` |
| 场景数 | 3（`pass` / `fail` / `boundary`） |
| 各场景分数 | 1.0（上界） / 0.0（下界） / 0.5（中间值） |
| 各场景追溯链步骤数 | 1 / 2 / 2 |
| 时间戳 | 固定值 `2026-10-17T09:00:00Z`（与运行时刻无关） |

## 注意事项

- **不触真实 API，不需要任何 API 密钥**（CI 零 key）：全部数据在脚本内构造，零网络依赖。
- **输出可复现**：示例中的时间戳是固定值而非当前时钟，且 JSON 键有序，因此同一条命令
  的输出逐字节一致——这样它才能被当作比对基线；一份随运行变化的产物没有这个用途。
- **Windows 重定向编码**：输出是 UTF-8 文本。若要写进文件，先执行
  `$env:PYTHONIOENCODING="utf-8"`，否则默认代码页（cp936）会把中文写成另一种编码，
  下游按 UTF-8 读取时解析失败。
- **本目录与脚本都不含评分逻辑**：它们只演示契约的形态。精确匹配、rubric、Judge 等
  评分器分别在 M2-D03 / D05 / D07 实现，输出统一收敛为这里的模型。
