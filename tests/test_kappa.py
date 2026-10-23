"""Cohen's kappa 点估计单元测试（M2-D08）。

覆盖范围：
- 完全一致：两类都出现时 κ=1.0；只出现单一类别（全 True / 全 False）时 Pe=1、公式
  分母为零，按约定返回 1.0——这是本函数唯一需要约定的退化情形；
- 完全不一致：边际均匀（5 正 5 负对调）时 κ=-1.0，是 κ 的理论下界；
- 随机一致：观测一致率恰等于随机期望一致率（Po=Pe=0.5）时 κ=0，即「与随机猜测无差别」；
- 部分一致手算：10 条中 8 条一致、两类边际均 5/5，κ=(0.8-0.5)/(1-0.5)=0.6；
- 单条样本：一致返回 1.0（Pe=1 分支），不一致返回 0.0（无信息量，但不报错）；
- 入参拦截：空序列、长度不等抛 ValueError；非布尔值（1 / 0 / 字符串）抛 TypeError，
  且指明出错的下标与位置（reference 侧 / prediction 侧）；
- 取值域：所有场景的结果都落在 [-1.0, 1.0] 内。

本模块是纯函数测试，不触网络、不读环境变量、零 key。
"""

from __future__ import annotations

import pytest

from aquamind.kappa import cohens_kappa


class TestAgreementExtremes:
  """一致度的两端：完全一致与完全不一致。"""

  def test_perfect_agreement_returns_one(self) -> None:
    """两类都出现且逐条相同时 κ=1.0（Pe<1，公式与约定两条路径都给出 1.0）。"""
    reference = [True, True, False, False]
    assert cohens_kappa(reference, list(reference)) == 1.0

  @pytest.mark.parametrize("value", [True, False])
  def test_single_class_perfect_agreement_returns_one(self, value: bool) -> None:
    """只出现单一类别时 Pe=1、分母为零，按「完全一致」约定返回 1.0。

    这一条单独钉住退化分支：若实现直接做 (Po-Pe)/(1-Pe)，此处会抛
    ``ZeroDivisionError`` 而不是返回一个可参与聚合的系数。
    """
    labels = [value] * 5
    assert cohens_kappa(labels, labels) == 1.0

  def test_perfect_disagreement_returns_minus_one(self) -> None:
    """边际均匀且逐条相反时 κ=-1.0：Po=0、Pe=0.5，公式给出理论下界。"""
    reference = [True, True, True, True, True, False, False, False, False, False]
    prediction = [not label for label in reference]
    assert cohens_kappa(reference, prediction) == pytest.approx(-1.0)


class TestHandComputedCases:
  """可手算的中段：随机一致与部分一致。"""

  def test_chance_level_agreement_returns_zero(self) -> None:
    """Po=Pe=0.5 时 κ=0：一致率完全可以用随机猜测解释，系数不含任何信息。

    基准侧 5 正 5 负时 Pe 恒为 0.5（与预测侧边际无关），因此只要观测到 5 条一致
    就落在随机水平。
    """
    reference = [True, True, True, True, True, False, False, False, False, False]
    prediction = [True, True, False, False, False, True, True, False, False, False]
    assert cohens_kappa(reference, prediction) == pytest.approx(0.0, abs=1e-12)

  def test_partial_agreement_hand_computed(self) -> None:
    """10 条中 8 条一致：Po=0.8、Pe=0.5，κ=(0.8-0.5)/(1-0.5)=0.6。

    构造：前 4 条 true 对齐、第 5 条 true 被判成 false；第 6 条 false 被判成 true、
    后 4 条 false 对齐。两侧边际都是 5/5，因此 Pe 恰为 0.5。
    """
    reference = [True, True, True, True, True, False, False, False, False, False]
    prediction = [True, True, True, True, False, True, False, False, False, False]
    assert cohens_kappa(reference, prediction) == pytest.approx(0.6)

  @pytest.mark.parametrize(
    ("reference", "prediction", "expected"),
    [
      ([True], [True], 1.0),
      ([False], [False], 1.0),
      ([True], [False], 0.0),
    ],
  )
  def test_single_sample(
    self, reference: list[bool], prediction: list[bool], expected: float
  ) -> None:
    """单条样本不报错：一致时走 Pe=1 约定返回 1.0，不一致时 Po=Pe=0 给出 0.0。"""
    assert cohens_kappa(reference, prediction) == pytest.approx(expected)


class TestInputValidation:
  """入参拦截：空序列 / 长度不等 / 非布尔值。"""

  def test_empty_sequences_rejected(self) -> None:
    """空序列的一致率无定义，必须报错而不是返回 0 或 1。"""
    with pytest.raises(ValueError, match="不能为空"):
      cohens_kappa([], [])

  def test_length_mismatch_rejected(self) -> None:
    """长度不等时报错而不是截断对齐：对不上号时任何对齐方式都是猜测。"""
    with pytest.raises(ValueError, match="长度必须相同"):
      cohens_kappa([True, False], [True])

  @pytest.mark.parametrize(
    ("reference", "prediction", "bad_side"),
    [
      # 0/1 是标注文件里最常见的格式漂移：静默换算会把格式错误洗成有效样本
      ([True, 1], [True, True], "reference"),
      ([True, True], [True, "true"], "prediction"),
      ([True, None], [True, False], "reference"),
    ],
  )
  def test_non_bool_rejected(
    self,
    reference: list[bool],
    prediction: list[bool],
    bad_side: str,
  ) -> None:
    """非布尔取值抛 TypeError，消息里指明位置与下标，便于直接定位标注文件。"""
    with pytest.raises(TypeError) as excinfo:
      cohens_kappa(reference, prediction)
    assert "布尔" in str(excinfo.value), bad_side
    assert "第 1 项" in str(excinfo.value), bad_side


class TestValueRange:
  """取值域：任何合法输入的结果都必须落在 [-1.0, 1.0]。"""

  def test_result_within_bounds(self) -> None:
    """高度失衡的边际（9 正 1 负）下，结果仍不得越出理论范围。"""
    reference = [True] * 9 + [False]
    prediction = [True] * 8 + [False, False]
    kappa = cohens_kappa(reference, prediction)
    assert -1.0 <= kappa <= 1.0
