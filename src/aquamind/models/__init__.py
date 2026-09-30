"""AquaMind 数据契约层：用例与期望输出的 pydantic 模型。

对外导出加载器、批量执行器与评分器共用的三个核心模型。
"""

from __future__ import annotations

from .testcase import ExpectedSpec, ScoreTag, TestCase

__all__ = ["ExpectedSpec", "ScoreTag", "TestCase"]