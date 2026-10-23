# AquaMind Judge 适配器示例（M2-D25）

本目录是 `JudgeAdapter`（`src/aquamind/judge.py`）的最小可运行示例，演示**裁判模型如何被
约束成可信的判定来源**：

1. **强制异族**——被测模型与裁判模型必须属于不同厂商前缀。同族自评时，裁判与被测方
   共享同一套训练偏好与失效模式，"独立判定"这句话就不成立了；
2. **结构化 JSON**——裁判只能输出 `{"passed": bool, "score": float, "reason": str}`，
   由 `JudgeOutput` 强校验（`extra="forbid"`、`score` 闭区间 0.0-1.0、`passed` 严格布尔）；
3. **不可判不等于不通过**——解析失败、字段缺失、类型不符、分数越界一律抛 `JudgeError`，
   绝不压成通过或不通过（否则通过率会静默失真）。

完整取舍论证见 [`docs/adr/0013-judge-adapter.md`](../../docs/adr/0013-judge-adapter.md)，
输出结构的原始决策见 [`docs/adr/0005-judge-structured-output.md`](../../docs/adr/0005-judge-structured-output.md)。

## 目录内容

| 文件 | 说明 |
| --- | --- |
| `demo.py` | 可运行示例：异族放行 → 结构化判定 → 同族拒绝 → 不可判出口，无需任何输入 |
| `README.md` | 本文件 |
| `../../scripts/judge_contract_check.py` | 量化验证脚本（异族矩阵 + JSON 契约矩阵） |

## 运行命令

在**仓库根目录**执行：

```powershell
.venv\Scripts\python.exe examples/judge_demo/demo.py
```

一条命令复跑量化数字（异族判定矩阵 10 组 + JSON 契约矩阵 8 组，退出码 0 表示全部符合预期）：

```powershell
.venv\Scripts\python.exe scripts/judge_contract_check.py
```

## 预期输出

`demo.py` 退出码 **0**，四段依次打印：

```text
1. 异族校验：被测 gpt-4o / 裁判 claude-3-opus
  构造成功   : sut_model=gpt-4o judge_model=claude-3-opus
2. 结构化判定：裁判返回 JSON → JudgeOutput
  passed     : True
  score      : 0.82
  reason     : 结论正确，但推导缺少中间步骤
3. 同族被拒：被测 gpt-4o / 裁判 gpt-4o-mini
  构造被拒   : 裁判模型与被测模型同族，强制异族校验失败：...
4. 不可判出口：裁判返回自由文本，不猜结论
  判为不可判 : 裁判输出不是合法 JSON：未找到 { 起始的 JSON 对象
```

`judge_contract_check.py` 的关键数字（与完成报告一致）：

| 项目 | 值 |
| --- | --- |
| 异族判定通过率 | `10/10 = 100.0%`（5 组同族必拒 + 5 组异族必过） |
| JSON 契约校验通过率 | `8/8 = 100.0%`（2 组合法通过 + 6 组违约判不可判） |

## 接真实裁判

示例用 `CallableAdapter` 注入固定响应，因此零 key、零网络。接到真实裁判时，**只把注入的
适配器换成一个配置好的 `OpenAIAdapter`**，其余代码不变：

```python
from aquamind.adapters import OpenAIAdapter
from aquamind.judge import JudgeAdapter

judge_adapter = JudgeAdapter(
  OpenAIAdapter(base_url="https://api.anthropic.com/v1", model="claude-3-opus"),
  sut_model="gpt-4o",
  judge_model="claude-3-opus",
)
```

`JudgeAdapter` 只依赖适配器契约（`acomplete`），不依赖具体后端；裁判 API 的 429 / 超时 /
网络故障由 `OpenAIAdapter` 统一包装为 `AdapterError`，Judge 边界处转成 `JudgeError`
（不可判），原始异常保留在 `__cause__` 中，**不重试**。

## 注意事项

- **不触真实 API，不需要任何 API 密钥**（CI 零 key）。
- **输出可复现**：示例不含时间戳、不读时钟，同一命令的输出逐字相同。
- **Windows 重定向编码**：输出是 UTF-8 文本。若要写进文件，先执行
  `$env:PYTHONIOENCODING="utf-8"`，否则默认代码页（cp936）会把中文写成另一种编码。
- **本目录不含评分逻辑的接线**：`scoring.py` 的 `_MATCHERS` 仍未注册 `judge`
  （接线留到后续任务），`JudgeAdapter` 目前是独立的评分器出口。
