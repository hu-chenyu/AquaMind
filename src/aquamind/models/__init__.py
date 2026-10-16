"""AquaMind 数据契约层：输入侧（用例与期望输出）与输出侧（评分结果）的 pydantic 模型。

对外导出加载器、批量执行器与评分器共用的模型：输入侧 ``ExpectedSpec`` / ``ScoreTag`` /
``TestCase``，输出侧 ``Score`` / ``ScoreStep`` / ``ScoreTrace``。两侧同为全链路的边界收口——
坏数据在进入系统的第一刻就被拦住，评分产物在离开评分器时就已是终态。
"""

from __future__ import annotations

from .score import Score, ScoreStep, ScoreTrace
from .testcase import ExpectedSpec, ScoreTag, TestCase

__all__ = ["ExpectedSpec", "Score", "ScoreStep", "ScoreTag", "ScoreTrace", "TestCase"]
