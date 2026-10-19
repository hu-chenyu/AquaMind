"""全仓 BaseModel 的 ``model_copy`` 统一收口测试（D21 收尾）。

覆盖范围：
- ``models/score.py`` 与 ``models/testcase.py`` 之外的 6 个模型（AdapterResponse /
  RunSpec / RequestInfo / ResponseInfo / ChunkTiming / Cassette）：带 update 时坏值
  被拒、合法 update 生效、空 update 保持官方纯复制语义；
- ``models.score.validated_model_copy`` 对**嵌套模型实例**的处理：列表里的模型实例
  会先降级为 dict 再重建（Cassette.timing / ScoreTrace.steps），而 dict 扩展口袋里
  的实例原样保留（metadata 是 ``Any`` 承载的自由数据，不得被静默改型）。

全部为纯数据契约测试，不发起任何网络调用（零 key）。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import BaseModel, ValidationError

from aquamind.adapters.base import AdapterResponse
from aquamind.cli import RunSpec
from aquamind.models import Score, ScoreStep, ScoreTrace
from aquamind.replay import Cassette, ChunkTiming, RequestInfo, ResponseInfo


def _cassette_payload(**overrides: object) -> dict[str, object]:
  """构造一份最小合法 cassette 载荷（request_hash 须为 16 位十六进制）。"""
  payload: dict[str, object] = {
    "request_method": "POST",
    "request_url": "https://api.example.com/v1/chat/completions",
    "response_status_code": 200,
    "request_hash": "0123456789abcdef",
    "recorded_at": "2026-10-19T00:00:00+00:00",
  }
  payload.update(overrides)
  return payload


def _step() -> ScoreStep:
  """构造一个最小合法评分步骤，供追溯链测试复用。"""
  return ScoreStep(
    scorer="exact",
    input_digest="in",
    output_digest="out",
    partial_score=1.0,
    reasoning="ok",
  )


def _adapter_response() -> AdapterResponse:
  """构造一个最小合法适配器响应。"""
  return AdapterResponse(content="x")


def _run_spec() -> RunSpec:
  """构造一份最小合法运行配置。"""
  return RunSpec(base_url="https://api.example.com/v1")


def _request_info() -> RequestInfo:
  """构造一个最小合法请求信息。"""
  return RequestInfo(method="GET", url="https://x/y")


def _response_info() -> ResponseInfo:
  """构造一个最小合法响应信息。"""
  return ResponseInfo(status_code=200)


def _chunk_timing() -> ChunkTiming:
  """构造一个最小合法 chunk 时序记录。"""
  return ChunkTiming(index=0, arrival_ms=0.0, text="a")


def _cassette() -> Cassette:
  """构造一份最小合法 cassette。"""
  return Cassette(**_cassette_payload())  # type: ignore[arg-type]


# 六个「非模型契约」模型的统一用例表：构造工厂 / 合法 update / 非法 update。
# 非法 update 一律取该模型自身字段约束或 extra="forbid" 能拦下的取值，
# 保证「被拒」不是因为写错了字段名之外的偶然原因。
_FACTORIES: list[tuple[str, Callable[[], BaseModel]]] = [
  ("adapter_response", _adapter_response),
  ("run_spec", _run_spec),
  ("request_info", _request_info),
  ("response_info", _response_info),
  ("chunk_timing", _chunk_timing),
  ("cassette", _cassette),
]


@pytest.mark.parametrize(("label", "factory"), _FACTORIES, ids=[item[0] for item in _FACTORIES])
def test_model_copy_without_update_is_pure_copy(
  label: str, factory: Callable[[], BaseModel]
) -> None:
  """不带 update（含空字典）时保持官方语义：值相等、对象不同。"""
  model = factory()
  for copied in (model.model_copy(), model.model_copy(update={})):
    assert copied == model, f"{label} 的纯复制改变了取值"
    assert copied is not model, f"{label} 的纯复制返回了同一对象"


@pytest.mark.parametrize(
  ("label", "factory", "legal_update", "bad_update"),
  [
    ("adapter_response", _adapter_response, {"content": "y"}, {"bogus": 1}),
    ("run_spec", _run_spec, {"timeout": 30.0}, {"base_url": "ftp://x"}),
    ("request_info", _request_info, {"method": "POST"}, {"bogus": 1}),
    ("response_info", _response_info, {"status_code": 201}, {"status_code": "abc"}),
    ("chunk_timing", _chunk_timing, {"text": "b"}, {"index": -1}),
    ("cassette", _cassette, {"response_status_code": 201}, {"response_status_code": 999}),
  ],
)
def test_model_copy_rejects_bad_update_and_applies_legal_update(
  label: str,
  factory: Callable[[], BaseModel],
  legal_update: dict[str, object],
  bad_update: dict[str, object],
) -> None:
  """6 个模型带 update 时都必须重跑校验：坏值被拒、合法值真的生效。"""
  model = factory()
  with pytest.raises(ValidationError):
    model.model_copy(update=bad_update)
  copied = model.model_copy(update=legal_update)
  assert copied is not model, f"{label} 的 update 副本与原件是同一对象"
  assert copied.model_dump() != model.model_dump(), f"{label} 的合法 update 未生效"


def test_cassette_copy_rejects_bad_nested_timing_instance() -> None:
  """timing 是 list[ChunkTiming]：坏实例经 update 注入必须被拒（修复前会静默收下）。"""
  bad = ChunkTiming.model_construct(index=-1, arrival_ms=0.0, text="a")
  with pytest.raises(ValidationError):
    _cassette().model_copy(update={"timing": [bad]})


def test_cassette_copy_accepts_legal_nested_timing() -> None:
  """合法嵌套实例经 update 注入应通过，并被重建为契约模型。"""
  copied = _cassette().model_copy(
    update={"timing": [{"index": 0, "arrival_ms": 0.0, "text": "hi"}]}
  )
  assert copied.timing is not None
  assert copied.timing[0].text == "hi"
  assert isinstance(copied.timing[0], ChunkTiming)


def test_score_trace_copy_rejects_bad_step_instance() -> None:
  """输出侧 ScoreTrace.steps 同样是 list[模型]：坏实例不得经副本混入追溯链。"""
  trace = ScoreTrace(
    case_id="c1",
    steps=[_step()],
    final_score=1.0,
    scorer_version="v1",
    timestamp="2026-10-19T00:00:00+00:00",
  )
  bad = ScoreStep.model_construct(
    scorer="exact",
    input_digest="in",
    output_digest="out",
    partial_score=5.0,
    reasoning="ok",
  )
  with pytest.raises(ValidationError):
    trace.model_copy(update={"steps": [bad]})


def test_model_copy_keeps_dict_extension_pocket_untouched() -> None:
  """metadata 是 ``Any`` 承载的扩展口袋：dict 内的模型实例原样保留，不被降级改型。"""
  extension = _step()
  score = Score(
    score=1.0,
    reason="ok",
    scorer="exact",
    passed=True,
    metadata={"detail": extension},
  )
  copied = score.model_copy(update={"metadata": {"detail": extension}})
  assert copied.metadata["detail"] is extension
