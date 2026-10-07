"""M1-P2 集中修复的回归测试。

覆盖 8 项修复：
- 修复1 weights 拒绝 NaN/inf 且 JSON 往返自洽；ScoreTag.weight、tolerance 的 NaN 回归
- 修复2 Settings/TestCase/ExpectedSpec/ScoreTag 构造后赋值同样受约束
- 修复3 ExpectedSpec.type 只接受 4 个合法枚举值
- 修复4 OpenAIAdapter 把请求体序列化 TypeError 包装为 AdapterError
- 修复5 LocalProtocolError 原生路径与名字路径结论一致且不可重试
- 修复6 repr(exc) 不再渲染 context 取值
- 修复7 AQ_REPLAY_DIR 空串/纯空白被拒
- 修复8 load_config() 存在且可调用（文档陷阱回归）

所有 HTTP 交互由 httpx.MockTransport 模拟，配置用例用 monkeypatch.setenv，
不触真实 API（零 key）、不依赖 CWD 下的 .env。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from aquamind.adapters import OpenAIAdapter
from aquamind.config import Settings, load_config
from aquamind.exceptions import AdapterError
from aquamind.models import ExpectedSpec, ScoreTag, TestCase
from aquamind.retry import ErrorKind, classify_error, is_retryable

# 修复3 确认的 4 个合法 type 取值（与 ExpectedSpec.type 的 Literal 一一对应）
_LEGAL_TYPES = ("exact", "regex", "contains", "judge")


def _case(**overrides: object) -> TestCase:
    """构造一条最小合法用例，省去各测试重复 expected/input。"""
    payload: dict[str, object] = {
        "input": "x",
        "expected": ExpectedSpec(type="exact", value="y"),
    }
    payload.update(overrides)
    return TestCase(**payload)  # type: ignore[arg-type]


def _adapter(transport: httpx.AsyncBaseTransport) -> OpenAIAdapter:
    """构造指向 mock 端点的适配器。"""
    return OpenAIAdapter("https://api.example.com/v1", transport=transport)


def _ok_transport() -> httpx.MockTransport:
    """返回 HTTP 200 的 MockTransport（本组用例根本走不到响应，仅为构造合法适配器）。"""
    payload = {"choices": [{"message": {"content": "ok"}}]}
    return httpx.MockTransport(lambda request: httpx.Response(200, json=payload))


class TestP2Fix1WeightsFinite:
    """修复1：weights 必须是有限值，且 JSON 往返自洽。"""

    def test_weights_nan_rejected(self) -> None:
        """NaN 权重必须在构造期被拒。"""
        with pytest.raises(ValidationError):
            _case(weights={"accuracy": float("nan")})

    def test_weights_inf_rejected(self) -> None:
        """+inf 同样被拒：ge=0.0 拦不住它，但它会污染成本/加权聚合。"""
        with pytest.raises(ValidationError):
            _case(weights={"accuracy": float("inf")})

    def test_weights_negative_still_rejected(self) -> None:
        """原有负值校验不得被新约束削弱。"""
        with pytest.raises(ValidationError):
            _case(weights={"accuracy": -1.0})

    def test_weights_json_roundtrip(self) -> None:
        """构造 → dump_json → validate_json 必须往返成功。

        这是修复1 的核心回归：修复前 NaN 权重能构造成功，却会被序列化成
        ``null``，再读回时直接 ValidationError——即「自己写出的 JSON 自己读不回来」。
        """
        original = _case(weights={"accuracy": 0.5, "consistency": 1.0})
        restored = TestCase.model_validate_json(original.model_dump_json())
        assert restored.weights == original.weights
        assert restored.expected.type == "exact"

    def test_weights_roundtrip_does_not_degrade_to_null(self) -> None:
        """落盘 JSON 里权重必须是数值而非 null（防御性断言，防止约束被误删）。"""
        dumped = json.loads(_case(weights={"accuracy": 0.5}).model_dump_json())
        assert dumped["weights"] == {"accuracy": 0.5}
        assert dumped["weights"]["accuracy"] is not None

    def test_score_tag_weight_nan_rejected(self) -> None:
        """回归：ScoreTag.weight 本就应拒绝 NaN（ge=0.0 已覆盖）。"""
        with pytest.raises(ValidationError):
            ScoreTag(name="accuracy", weight=float("nan"))

    def test_tolerance_nan_rejected(self) -> None:
        """回归：ExpectedSpec.tolerance 本就应拒绝 NaN。"""
        with pytest.raises(ValidationError):
            ExpectedSpec(type="exact", value=1, tolerance=float("nan"))


class TestP2Fix2ValidateAssignment:
    """修复2：字段约束在构造后的赋值路径上同样成立。"""

    def test_settings_assignment_violates_constraint(self) -> None:
        """赋值绕过 ge=1.0 是修复前的真实漏洞（s.default_timeout = 0.001 被保留）。"""
        settings = Settings()
        with pytest.raises(ValidationError):
            settings.default_timeout = 0.001

    def test_settings_assignment_log_level_literal(self) -> None:
        """赋值绕过 Literal 枚举同样是修复前的漏洞。"""
        settings = Settings()
        with pytest.raises(ValidationError):
            settings.log_level = "NOT_A_LEVEL"

    def test_testcase_assignment_negative_weight(self) -> None:
        """TestCase 构造后赋非法权重必须被拒。"""
        case = _case()
        with pytest.raises(ValidationError):
            case.weights = {"evil": -999.0}

    def test_testcase_assignment_nan_weight(self) -> None:
        """赋值路径同样要拦 NaN，而不只是构造路径。"""
        case = _case()
        with pytest.raises(ValidationError):
            case.weights = {"evil": float("nan")}

    def test_expected_spec_assignment_negative_tolerance(self) -> None:
        """ExpectedSpec 赋值绕过 ge=0.0 应被拒。"""
        spec = ExpectedSpec(type="exact", value="y")
        with pytest.raises(ValidationError):
            spec.tolerance = -5.0

    def test_score_tag_assignment_negative_weight(self) -> None:
        """ScoreTag 赋值绕过 ge=0.0 应被拒。"""
        tag = ScoreTag(name="accuracy")
        with pytest.raises(ValidationError):
            tag.weight = -1.0

    def test_valid_assignment_still_allowed(self) -> None:
        """合法赋值不得被误伤（修复不是把模型改成只读）。"""
        case = _case()
        case.weights = {"accuracy": 0.75}
        assert case.weights == {"accuracy": 0.75}
        settings = Settings()
        settings.default_timeout = 30.0
        assert settings.default_timeout == 30.0


class TestP2Fix3ExpectedType:
    """修复3：ExpectedSpec.type 只接受有限枚举值。"""

    @pytest.mark.parametrize("bad_type", ["excat", "", "totally_bogus", "EXACT"])
    def test_invalid_type_rejected(self, bad_type: str) -> None:
        """拼错或非法 type 必须被拒，不能飘到 M2 打分器静默产出错误分数。"""
        with pytest.raises(ValidationError):
            ExpectedSpec(type=bad_type, value="y")

    @pytest.mark.parametrize("good_type", _LEGAL_TYPES)
    def test_legal_types_accepted(self, good_type: str) -> None:
        """4 个合法取值全部可构造（取值集合以实况为准：docstring + 仓库实际用法）。"""
        assert ExpectedSpec(type=good_type, value="y").type == good_type

    def test_type_used_by_existing_fixtures_still_works(self) -> None:
        """仓库内实际使用的 exact/contains 不得被新约束误伤。"""
        assert _case(expected={"type": "exact", "value": "2"}).expected.type == "exact"
        assert _case(expected={"type": "contains", "value": "蓝"}).expected.type == "contains"


class TestP2Fix4AdapterTypeError:
    """修复4：不可 JSON 序列化的 messages 必须包装为 AdapterError。"""

    @pytest.mark.parametrize(
        ("label", "messages"),
        [
            ("set_in_content", [{"role": "user", "content": {1, 2, 3}}]),
            ("custom_object", [{"role": "user", "content": object()}]),
            ("set_as_message", [{"role": "user", "content": "x", "meta": {4, 5}}]),
        ],
    )
    def test_unserializable_messages_wrapped(
        self, label: str, messages: list[dict[str, object]]
    ) -> None:
        """裸 TypeError 会击穿「调用方只需 except AdapterError」的契约，必须被包装。"""
        adapter = _adapter(_ok_transport())
        with pytest.raises(AdapterError) as excinfo:
            asyncio.run(adapter.acomplete(messages))  # type: ignore[arg-type]
        assert "序列化" in excinfo.value.message, f"{label} 未被正确包装"

    def test_context_carries_original_error_type(self) -> None:
        """context 须带原始异常类型名，便于排障时定位到 messages 序列化。"""
        adapter = _adapter(_ok_transport())
        with pytest.raises(AdapterError) as excinfo:
            asyncio.run(adapter.acomplete([{"role": "user", "content": {1, 2}}]))
        assert excinfo.value.context["error_type"] == "TypeError"
        assert excinfo.value.context["adapter"] == "openai"

    def test_exception_chain_preserved(self) -> None:
        """原始 TypeError 必须留在 __cause__ 里，不能被包装吞掉。"""
        adapter = _adapter(_ok_transport())
        with pytest.raises(AdapterError) as excinfo:
            asyncio.run(adapter.acomplete([{"role": "user", "content": object()}]))
        assert isinstance(excinfo.value.__cause__, TypeError)

    def test_network_error_still_wrapped_separately(self) -> None:
        """网络异常仍走原捕获族，消息不得被新的序列化文案污染。"""

        def _boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("模拟连接失败")

        adapter = _adapter(httpx.MockTransport(_boom))
        with pytest.raises(AdapterError) as excinfo:
            asyncio.run(adapter.acomplete([{"role": "user", "content": "x"}]))
        assert "序列化" not in excinfo.value.message
        assert excinfo.value.context["error_type"] == "ConnectError"


class TestP2Fix5LocalProtocolError:
    """修复5：LocalProtocolError 两条路径结论一致，且不可重试。"""

    def test_native_path_not_retried(self) -> None:
        """原生 httpx.LocalProtocolError 归 CLIENT_ERROR，不重试。"""
        exc = httpx.LocalProtocolError("客户端发送了畸变的 HTTP 方法")
        assert classify_error(exc) is ErrorKind.CLIENT_ERROR
        assert is_retryable(exc) is False

    def test_name_path_matches_native_path(self) -> None:
        """经 openai.py 包装后走 error_type 名字路径，结论必须与原生路径一致。

        修复前这是同因异果：原生路径 NETWORK_ERROR（可重试）、名字路径 UNKNOWN
        （不重试），同一个故障因调用方式不同得到相反的重试结论。
        """
        native = classify_error(httpx.LocalProtocolError("x"))
        named = classify_error(AdapterError("x", {"error_type": "LocalProtocolError"}))
        assert named is native
        assert named is ErrorKind.CLIENT_ERROR

    def test_remote_protocol_error_still_retried(self) -> None:
        """回归：服务端侧 RemoteProtocolError 必须仍可重试（别误伤同族）。"""
        exc = httpx.RemoteProtocolError("服务端中途断开")
        assert classify_error(exc) is ErrorKind.NETWORK_ERROR
        assert is_retryable(exc) is True

    def test_timeout_still_wins_over_transport_branch(self) -> None:
        """回归：超时判定仍优先于 TransportError 分支。"""
        exc = httpx.ConnectTimeout("连接超时")
        assert classify_error(exc) is ErrorKind.TIMEOUT

    def test_other_protocol_family_still_retried(self) -> None:
        """回归：ProxyError / UnsupportedProtocol 仍归 NETWORK_ERROR。"""
        for exc in (httpx.ProxyError("代理不可用"), httpx.UnsupportedProtocol("协议不支持")):
            assert classify_error(exc) is ErrorKind.NETWORK_ERROR

    def test_local_protocol_error_via_name_path_is_retryable(self) -> None:
        """名字路径下 is_retryable 与原生路径一致（同为 False）。"""
        assert is_retryable(AdapterError("x", {"error_type": "LocalProtocolError"})) is False


class TestP2Fix6ReprSafety:
    """修复6：repr 与 str 都不渲染 context 取值。"""

    def test_repr_does_not_leak_context_values(self) -> None:
        """response_body 可能含服务端回显的 prompt，repr 不得把它带出来。"""
        exc = AdapterError(
            "请求失败",
            {"status_code": 500, "response_body": "SENSITIVE_PROMPT_CONTENT"},
        )
        assert "SENSITIVE_PROMPT_CONTENT" not in repr(exc)

    def test_str_does_not_leak_context_values(self) -> None:
        """str 侧的行为不得因本次改动回退。"""
        exc = AdapterError("请求失败", {"response_body": "SENSITIVE_PROMPT_CONTENT"})
        assert "SENSITIVE_PROMPT_CONTENT" not in str(exc)

    def test_repr_keeps_key_names_for_debuggability(self) -> None:
        """加固不等于丢信息：键名仍要保留，排障得知道有哪些上下文可用。"""
        exc = AdapterError("请求失败", {"status_code": 500, "error_type": "ConnectError"})
        text = repr(exc)
        assert "status_code" in text
        assert "error_type" in text

    def test_repr_without_context(self) -> None:
        """空 context 的 repr 不得报错，且同样不含取值。"""
        text = repr(AdapterError("裸异常"))
        assert "裸异常" in text

    def test_repr_does_not_leak_via_fstring(self) -> None:
        """repr 会被 f-string 隐式调用，必须堵住这条间接泄露路径。"""
        exc = AdapterError("x", {"response_body": "SENSITIVE_PROMPT_CONTENT"})
        assert "SENSITIVE_PROMPT_CONTENT" not in f"{exc!r}"


class TestP2Fix7ReplayDirBlank:
    """修复7：AQ_REPLAY_DIR 空串/纯空白必须被拒，不得静默落到 CWD。"""

    @pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
    def test_blank_replay_dir_rejected(
        self, monkeypatch: pytest.MonkeyPatch, blank: str
    ) -> None:
        """Path('') 会被解析成 Path('.') 再 resolve 成 CWD——必须提前拦。"""
        monkeypatch.setenv("AQ_REPLAY_DIR", blank)
        with pytest.raises(ValidationError):
            Settings()

    def test_blank_replay_dir_rejected_through_load_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """经 load_config() 走时应包装为 ConfigError，与既有契约一致。"""
        from aquamind.exceptions import ConfigError

        monkeypatch.setenv("AQ_REPLAY_DIR", "")
        with pytest.raises(ConfigError):
            load_config()

    def test_valid_replay_dir_still_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """回归：合法路径仍应正常展开为绝对路径。"""
        monkeypatch.setenv("AQ_REPLAY_DIR", "./some_cache_dir")
        resolved = Settings().replay_dir
        assert resolved.is_absolute()
        assert resolved.name == "some_cache_dir"

    def test_default_replay_dir_unaffected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """回归：未设置环境变量时默认目录行为不变。"""
        monkeypatch.delenv("AQ_REPLAY_DIR", raising=False)
        assert Settings().replay_dir.name == ".aquamind_cache"


class TestP2Fix8DocTrap:
    """修复8：PROJECT-PLAN.md 曾把 load_config() 写成 Settings.load()。"""

    def test_load_config_is_module_level_function(self) -> None:
        """实际 API 是模块级 load_config()；Settings 上并没有 load 方法。"""
        assert callable(load_config)
        assert not hasattr(Settings, "load")

    def test_load_config_returns_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """load_config() 必须可无参调用并返回 Settings 实例。"""
        monkeypatch.delenv("AQ_REPLAY_DIR", raising=False)
        assert isinstance(load_config(), Settings)

    def test_project_plan_no_longer_references_settings_load(self) -> None:
        """计划文档里不得再出现 `Settings.load()` 这个不存在的 API。"""
        plan = Path(__file__).resolve().parent.parent / "docs" / "PROJECT-PLAN.md"
        assert "Settings.load()" not in plan.read_text(encoding="utf-8")