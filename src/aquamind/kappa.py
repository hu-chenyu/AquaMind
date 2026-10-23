"""Cohen's kappa：两组二元标注（裁判 vs 人工）的一致度系数。

为什么不用裸一致率：观测一致率会把「类别严重失衡」算得虚高——一批用例里 90% 都该通过
时，裁判全判通过就能拿到 90% 的一致率，而它对「不通过」这一类毫无分辨力。κ 用随机期望
一致率扣掉这部分「白捡的一致」，因此是拿 D24 冻结的校准集做标定时要报的指标。

口径边界（本日只做点估计）：

- 只算**点估计**，不给标准误与置信区间。待补充：SE 与 CI 随 D29 的 κ 正式测量一起落地，
  届时先定「用哪个近似式」（Fleiss 近似与 Cohen 原式在样本量小、类别失衡时差异可见），
  并与本项目其他区间（BCa bootstrap）保持同一套口径，而不是在此刻拍一个公式；
- 输入必须是布尔序列，**不做静默换算**：``1`` / ``0`` / ``"true"`` 一律报错。标注文件里
  出现 0/1 往往是上游格式约定不一致，就地换算会把格式错误洗成有效样本，等 κ 偏低时
  已经无法区分「裁判不准」与「标注格式错」；
- 长度不等直接报错而不是截断对齐：两条标注对不上号时，任何对齐方式都是猜测；
- 完全一致且两类都出现过时返回 1.0；只出现单一类别（例如全 True）时 Pe=1、分母为零，
  数学上 κ 未定义，按「观测完全一致」的约定返回 1.0（这是唯一需要约定的退化情形）。
"""

from __future__ import annotations

from collections.abc import Sequence


def cohens_kappa(reference: Sequence[bool], prediction: Sequence[bool]) -> float:
  """计算两组二元标注的 Cohen's kappa 系数。

  二元特例下的公式为 κ = (Po - Pe) / (1 - Pe)：

  - Po（观测一致率）= 两组取值相同的条目数 / 总条目数；
  - Pe（随机期望一致率）= (ref_yes * pred_yes + ref_no * pred_no) / total²，
    即两次独立随机标注碰巧一致的概率。

  κ = 1 表示完全一致；κ = 0 表示与随机猜测无差别；负值表示系统性反向（一方说通过、
  另一方说不过的比例高于随机）。

  Args:
    reference: 人工标注（基准），每项必须为 ``bool``。
    prediction: 裁判判定（预测），每项必须为 ``bool``，长度须与 ``reference`` 相同。

  Returns:
    float: 一致度系数，理论范围 [-1.0, 1.0]；完全一致（含只出现单一类别）时为 1.0。

  Raises:
    ValueError: 两条序列为空，或长度不等时抛出（一致率与随机期望都无定义）。
    TypeError: 任一项不是 ``bool`` 时抛出（``1`` / ``0`` / ``"true"`` 不做静默换算）。
  """
  if len(reference) != len(prediction):
    raise ValueError(
      f"两组标注长度必须相同：reference={len(reference)} prediction={len(prediction)}"
    )
  if not reference:
    raise ValueError("标注不能为空：空序列的一致率与随机期望都无定义")
  total = len(reference)
  ref_yes = 0
  pred_yes = 0
  agreed = 0
  for index, (ref, pred) in enumerate(zip(reference, prediction)):
    if not isinstance(ref, bool) or not isinstance(pred, bool):
      # 类型错用 TypeError、值域错用 ValueError：与仓库内 scoring.py 的口径一致，
      # 调用方也能分开处理（前者是标注文件格式错，后者是配对逻辑错）。
      raise TypeError(
        f"标注必须是布尔值（True/False），第 {index} 项为 "
        f"{type(ref).__name__}/{type(pred).__name__}：1/0 与字符串不做静默换算"
      )
    ref_yes += ref
    pred_yes += pred
    if ref is pred:
      agreed += 1
  po = agreed / total
  ref_no = total - ref_yes
  pred_no = total - pred_yes
  pe = (ref_yes * pred_yes + ref_no * pred_no) / (total * total)
  if pe == 1.0:
    # 两组都只出现同一个类别。Pe 由整数比值乘加得到，此情形下精确等于 1.0，不存在
    # 浮点歧义；分母为零，数学上 κ 未定义，按「观测完全一致」的约定返回 1.0。
    return 1.0
  return (po - pe) / (1.0 - pe)
