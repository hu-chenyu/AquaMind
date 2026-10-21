# κ 校准集（冻结交付物）

本文件说明同目录 `kappa_calibration_set.jsonl` 的用途、结构与冻结纪律。校准集是
**κ 标定（D26-D27）的基准数据**：Judge 适配器给出的评分与确定性匹配器的结论在这批
样本上比对，得到一致性系数。基准数据一旦漂移，历次 κ 值就失去可比性，因此本文件
入库后**不再修改**。

## 用途

- D26-D27：作为 κ 标定的固定样本集，比较确定性匹配器结论与 Judge 判定；
- D30：作为离线批量处理链路的冒烟数据（加载 → `score_batch` → `aggregate_scores`）；
- D23 端到端验证：部分用例带 `weights` / `score_tags`，用于验证权重归一化与
  `matched_tags` 登记口径在**真实文件加载**路径上同样成立。

## 结构

每行一个 JSON 对象，对应一条 `TestCase`（jsonl 不支持注释，故说明集中在本文件）。
字段：`input`（非空）、`expected`（`type` + `value`）、可选 `weights` / `score_tags` /
`context`。

`context` 里携带本校准集的**自描述样例**（`TestCase.context` 是自由映射，不改变契约）：

- `pass_response`：对应该用例应判定为**通过**的输出文本；
- `fail_response`：应判定为**不通过**的输出文本；
- `fail_response_judgeable`：可选，缺省为 `true`；写 `false` 表示该 `fail_response`
  属于**输出不可判**（例如期望 JSON 却给了非 JSON 文本），评分时应抛 `ValueError`
  而不是得出「不通过」——这正是 `all` 类的不可判传播口径。

这样测试与后续工具无需在代码里硬编码用例内容，直接从文件读取样例响应即可。

## 覆盖分布（共 36 条）

| 类型 | 条数 | 覆盖要点 |
| --- | --- | --- |
| exact | 6 | 数值期望、大小写敏感、多行文本、空字符串边界 |
| regex | 5 | 锚点、分组与非捕获组、`(?i)`、字符类、失败分支 |
| contains | 5 | 单子串、含空格子串、大小写敏感、连续空格保留 |
| excludes | 4 | 命中即失败、中英文排除词、未命中通过 |
| keywords | 4 | 列表与逗号分隔两种写法、AND 语义、顺序无关 |
| json | 5 | 顶层路径、嵌套数组下标、整数/字符串类型严格、数组整体比较、缺失路径 |
| all | 4 | 全通过、首条失败、JSON 子断言失败、不可判传播 |
| agent | 3 | 单工具参数层、多工具、多余参数视为不匹配 |

合计 36 条，落在 PROJECT_PLAN D24 要求的 30-50 条区间内。

## 冻结纪律

1. **入库后不修改**：任何对用例内容（`input` / `expected` / 样例响应）的改动都会
   使已标定的 κ 值不可比。需要新增样本时另建文件（如 `kappa_calibration_set_v2.jsonl`），
   而不是原地编辑；
2. 本文件**不是**随任务演进的测试夹具：测试只读校验其可加载、格式合法、八类覆盖与
   批量评分可用，不把它当作可随手调整的参数；
3. 若发现某条用例本身写错（例如正则非法导致加载即抛），属于**必须热修**的缺陷；
   热修需在 commit message 里注明「校准集热修 + 原因」，并在 D26 标定记录里说明
   修复前后的样本差异。

## 加载与校验

```python
from aquamind.dataset import validate_dataset
from aquamind.loaders import load_cases

cases = load_cases("tests/fixtures/kappa_calibration_set.jsonl")  # list[TestCase]，36 条
result = validate_dataset("tests/fixtures/kappa_calibration_set.jsonl")  # result.ok 为 True
```

`load_cases` 遇到第一个错误即抛；`validate_dataset` 逐行收集全部错误并返回
`ValidationResult`，CI 门禁使用后者。
