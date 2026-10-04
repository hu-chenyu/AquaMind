"""replay 模块单元测试。

覆盖：Cassette 契约、request_hash 稳定性与版本校验、record 落盘、
find_match 精确匹配（无匹配/损坏/指纹变更）、play 还原、ReplayedResponse
读取面（text/content/json）、异常消息脱敏。

所有用例只用 tmp_path 构造临时 cassette 目录，不触达任何真实 API（CI 零 key）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from aquamind.exceptions import ReplayError
from aquamind.replay import (
    Cassette,
    ReplayedResponse,
    RequestInfo,
    ResponseInfo,
    find_match,
    play,
    record,
    replay_request,
)

# 出现在请求体里的敏感样本：用于验证它不会随异常字符串进入日志
_SECRET_BODY = "用户隐私数据-身份证110101199001011234"


def _make_request(
    url: str = "https://api.example.com/v1/chat/completions",
    method: str = "POST",
    body: str | None = '{"model": "m", "messages": []}',
) -> RequestInfo:
    """构造一个稳定的模拟请求。

    Args:
        url: 请求 URL。
        method: 请求方法。
        body: 请求体原文，None 表示无请求体。

    Returns:
        RequestInfo: 模拟请求对象。
    """
    return RequestInfo(
        method=method,
        url=url,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        body=body,
    )


def _make_response(
    status_code: int = 200,
    body: str | bytes | None = '{"id": "chatcmpl-1", "choices": []}',
) -> ResponseInfo:
    """构造一个稳定的模拟响应。

    Args:
        status_code: 状态码。
        body: 响应体。

    Returns:
        ResponseInfo: 模拟响应对象。
    """
    return ResponseInfo(
        status_code=status_code,
        headers={"Content-Type": "application/json"},
        body=body,
    )


def _write_raw(path: Path, payload: str) -> None:
    """把原始文本覆盖写入 cassette 文件（用于构造坏数据）。

    Args:
        path: 目标文件路径。
        payload: 文件内容。
    """
    path.write_text(payload, encoding="utf-8")


class TestRequestHash:
    """测试请求指纹的稳定性与区分度。"""

    def test_hash_is_stable_across_calls(self) -> None:
        """同一请求多次计算必须得到同一 hash（否则回放永远匹配不上）。"""
        request = _make_request()
        first = Cassette.compute_request_hash(request)
        second = Cassette.compute_request_hash(request)
        assert first == second

    def test_hash_shape_is_16_hex(self) -> None:
        """hash 固定为 16 位十六进制：它同时用作 cassette 文件名，形状必须可预测。"""
        value = Cassette.compute_request_hash(_make_request())
        assert len(value) == 16
        assert all(char in "0123456789abcdef" for char in value)

    def test_hash_differs_across_requests(self) -> None:
        """URL / 方法 / 请求体任一变化都必须改变 hash。"""
        base = Cassette.compute_request_hash(_make_request())
        assert base != Cassette.compute_request_hash(_make_request(url="https://other/v1"))
        assert base != Cassette.compute_request_hash(_make_request(method="GET"))
        assert base != Cassette.compute_request_hash(_make_request(body='{"model": "other"}'))

    def test_hash_ignores_header_case_and_order(self) -> None:
        """请求头大小写与书写顺序不参与指纹（HTTP 头名大小写不敏感）。"""
        first = Cassette.compute_request_hash(
            RequestInfo(
                method="GET",
                url="https://api.example.com/x",
                headers={"Content-Type": "text/plain", "Accept": "application/json"},
            )
        )
        second = Cassette.compute_request_hash(
            RequestInfo(
                method="GET",
                url="https://api.example.com/x",
                headers={"accept": "application/json", "content-type": "text/plain"},
            )
        )
        assert first == second

    def test_hash_ignores_noise_headers(self) -> None:
        """User-Agent 等非关键头不参与指纹，避免客户端升级让 cassette 失效。"""
        without_noise = RequestInfo(
            method="GET", url="https://api.example.com/x", headers={"Accept": "application/json"}
        )
        with_noise = RequestInfo(
            method="GET",
            url="https://api.example.com/x",
            headers={"Accept": "application/json", "User-Agent": "httpx/9.9.9"},
        )
        assert Cassette.compute_request_hash(without_noise) == Cassette.compute_request_hash(
            with_noise
        )

    def test_hash_changes_when_credential_changes(self) -> None:
        """换了密钥必须改变 hash（否则会回放出旧凭据下录的内容）。"""
        first = Cassette.compute_request_hash(
            RequestInfo(method="GET", url="https://x/y", headers={"Authorization": "Bearer k1"})
        )
        second = Cassette.compute_request_hash(
            RequestInfo(method="GET", url="https://x/y", headers={"Authorization": "Bearer k2"})
        )
        assert first != second

    def test_method_case_and_body_none_normalized(self) -> None:
        """方法大小写、请求体 None 与空串归一为同一指纹。"""
        lower = Cassette.compute_request_hash(RequestInfo(method="post", url="https://x/y"))
        upper = Cassette.compute_request_hash(RequestInfo(method="POST", url="https://x/y"))
        empty = Cassette.compute_request_hash(RequestInfo(method="POST", url="https://x/y", body=""))
        assert lower == upper == empty


class TestCassetteContract:
    """测试 Cassette 数据结构的契约校验。"""

    def test_recorded_cassette_fields(self, tmp_path: Path) -> None:
        """录制产出的 cassette 应带全指纹字段与 hash 版本字段。"""
        request = _make_request()
        path = Path(record(request, _make_response(), tmp_path))
        cassette = json.loads(path.read_text(encoding="utf-8"))
        assert cassette["request_method"] == "POST"
        assert cassette["request_url"] == request.url
        assert cassette["request_headers"] == {
            "content-type": "application/json",
            "accept": "application/json",
        }
        assert cassette["response_status_code"] == 200
        assert cassette["request_hash"] == Cassette.compute_request_hash(request)
        assert cassette["recorded_at"].startswith("20")

    def test_credential_stored_as_digest_not_plaintext(self, tmp_path: Path) -> None:
        """Authorization 不得以明文落盘（cassette 会被提交进版本库）。"""
        request = RequestInfo(
            method="GET",
            url="https://api.example.com/x",
            headers={"Authorization": "Bearer super-secret-credential"},
        )
        path = Path(record(request, _make_response(), tmp_path))
        content = path.read_text(encoding="utf-8")
        assert "super-secret-credential" not in content
        assert cassette_header(path, "authorization").startswith("sha256:")

    def test_rejects_malformed_request_hash(self) -> None:
        """非 16 位十六进制的 request_hash 必须在契约层被拒绝。"""
        with pytest.raises(ValidationError, match="request_hash"):
            Cassette(
                request_method="GET",
                request_url="https://x/y",
                response_status_code=200,
                request_hash="not-a-hex-hash",
                recorded_at="2026-10-04T00:00:00+00:00",
            )

    def test_rejects_invalid_status_code(self) -> None:
        """非 1xx-5xx 的状态码必须被拒绝。"""
        with pytest.raises(ValidationError, match="response_status_code"):
            Cassette(
                request_method="GET",
                request_url="https://x/y",
                response_status_code=99,
                request_hash="0123456789abcdef",
                recorded_at="2026-10-04T00:00:00+00:00",
            )


class TestRecord:
    """测试录制落盘。"""

    def test_record_returns_valid_json_path(self, tmp_path: Path) -> None:
        """record 返回的路径必须存在、可读，且内容是合法 JSON。"""
        request = _make_request()
        target = tmp_path / "nested" / "cassettes"
        path = Path(record(request, _make_response(), target))
        assert path.is_file()
        assert path.name == f"{Cassette.compute_request_hash(request)}.json"
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)

    def test_record_creates_missing_directory(self, tmp_path: Path) -> None:
        """目标目录不存在时应自动创建。"""
        target = tmp_path / "a" / "b" / "c"
        assert not target.exists()
        record(_make_request(), _make_response(), target)
        assert target.is_dir()

    def test_record_writes_utf8_chinese_without_escape(self, tmp_path: Path) -> None:
        """中文响应体以 UTF-8 原样落盘（不被转成 \\uXXXX，便于人工比对）。"""
        path = Path(record(_make_request(), _make_response(body='{"reply": "是"}'), tmp_path))
        assert "是" in path.read_text(encoding="utf-8")

    def test_record_normalizes_bytes_body(self, tmp_path: Path) -> None:
        """bytes 响应体在边界按 UTF-8 解码，保证 cassette 是合法 UTF-8 JSON。"""
        path = Path(record(_make_request(), _make_response(body="字节".encode()), tmp_path))
        assert json.loads(path.read_text(encoding="utf-8"))["response_body"] == "字节"

    def test_record_normalizes_none_body(self, tmp_path: Path) -> None:
        """无响应体（如 204）归一为空串，回放后 text 为空串而非 None。"""
        request = _make_request(body=None)
        path = Path(record(request, _make_response(status_code=204, body=None), tmp_path))
        assert json.loads(path.read_text(encoding="utf-8"))["response_body"] == ""
        replayed = replay_request(request, tmp_path)
        assert replayed.status_code == 204
        assert replayed.text == ""

    def test_record_rejects_non_utf8_bytes_body(self, tmp_path: Path) -> None:
        """非 UTF-8 的 bytes 响应体必须显式报错，而不是写入损坏文件。"""
        with pytest.raises(ReplayError, match="UTF-8"):
            record(_make_request(), _make_response(body=b"\xff\xfe\xfa"), tmp_path)

    def test_record_reports_write_failure(self, tmp_path: Path) -> None:
        """目录不可用时抛 ReplayError，而不是把 OSError 泄漏给调用方。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir", encoding="utf-8")
        with pytest.raises(ReplayError, match="落盘失败") as excinfo:
            record(_make_request(), _make_response(), blocker)
        assert "error_type" in excinfo.value.context

    def test_record_uses_configured_dir_by_default(self, tmp_path: Path, monkeypatch) -> None:
        """未显式传目录时应落到全局配置 replay_dir（可用 AQ_REPLAY_DIR 覆盖）。"""
        monkeypatch.setenv("AQ_REPLAY_DIR", str(tmp_path / "configured"))
        path = Path(record(_make_request(), _make_response()))
        assert path.parent.samefile(tmp_path / "configured")


class TestFindMatch:
    """测试查找匹配的精确性与错误拦截。"""

    def test_round_trip_consistency(self, tmp_path: Path) -> None:
        """record → find_match → play 全链路：响应三要素与录制时一致。"""
        request = _make_request()
        response = _make_response(status_code=200, body='{"id": "chatcmpl-9", "choices": []}')
        record(request, response, tmp_path)

        cassette = find_match(request, tmp_path)
        assert cassette is not None
        replayed = play(cassette)
        assert replayed.status_code == 200
        assert replayed.body == response.body
        assert replayed.text == '{"id": "chatcmpl-9", "choices": []}'
        assert replayed.json() == {"id": "chatcmpl-9", "choices": []}

    def test_no_match_returns_none(self, tmp_path: Path) -> None:
        """未录制的请求返回 None（不做模糊匹配）。"""
        record(_make_request(), _make_response(), tmp_path)
        assert find_match(_make_request(url="https://api.example.com/v1/other"), tmp_path) is None

    def test_empty_dir_returns_none(self, tmp_path: Path) -> None:
        """空目录下查找返回 None。"""
        assert find_match(_make_request(), tmp_path) is None

    def test_changed_request_hash_raises(self, tmp_path: Path) -> None:
        """cassette 内 hash 与重算值不一致时必须报错（请求指纹已变更）。"""
        request = _make_request()
        path = Path(record(request, _make_response(), tmp_path))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["request_hash"] = "0123456789abcdef"
        _write_raw(path, json.dumps(payload, ensure_ascii=False))

        with pytest.raises(ReplayError, match="请求指纹已变更，请重新录制") as excinfo:
            find_match(request, tmp_path)
        # 排障需要的两个 hash 以键名形式留在 context 里供显式读取
        assert excinfo.value.context["recorded_request_hash"] == "0123456789abcdef"
        assert excinfo.value.context["current_request_hash"] == (
            Cassette.compute_request_hash(request)
        )

    def test_corrupted_json_raises(self, tmp_path: Path) -> None:
        """非法 JSON 必须报「文件损坏」，而不是把 JSONDecodeError 泄漏出去。"""
        request = _make_request()
        path = Path(record(request, _make_response(), tmp_path))
        _write_raw(path, "{not-json,,,")
        with pytest.raises(ReplayError, match="损坏") as excinfo:
            find_match(request, tmp_path)
        assert excinfo.value.context["line"] is not None

    def test_contract_violation_raises(self, tmp_path: Path) -> None:
        """字段不符合契约（含手改坏 hash 形状）时报「文件损坏」。"""
        request = _make_request()
        path = Path(record(request, _make_response(), tmp_path))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["request_hash"] = "short"
        _write_raw(path, json.dumps(payload, ensure_ascii=False))
        with pytest.raises(ReplayError, match="字段不符合 Cassette 契约"):
            find_match(request, tmp_path)

    def test_non_utf8_file_raises(self, tmp_path: Path) -> None:
        """非 UTF-8 编码的 cassette（如 GBK 保存）必须显式报错。"""
        request = _make_request()
        path = Path(record(request, _make_response(), tmp_path))
        # 必须含非 ASCII 字符：纯 ASCII 文本在 GBK 与 UTF-8 下字节相同，
        # 那样测的是"正常读取"而非编码不符
        path.write_bytes(json.dumps({"request_body": "中文"}, ensure_ascii=False).encode("gbk"))
        with pytest.raises(ReplayError, match="读取失败") as excinfo:
            find_match(request, tmp_path)
        assert excinfo.value.context["error_type"] == "UnicodeDecodeError"

    def test_unreadable_file_raises(self, tmp_path: Path, monkeypatch) -> None:
        """文件不可读时包装成 ReplayError（OSError 不穿透契约）。"""

        def _boom(self: Path, *args: object, **kwargs: object) -> str:
            raise PermissionError("denied")

        request = _make_request()
        record(request, _make_response(), tmp_path)
        monkeypatch.setattr(Path, "read_text", _boom)
        with pytest.raises(ReplayError, match="读取失败") as excinfo:
            find_match(request, tmp_path)
        assert excinfo.value.context["error_type"] == "PermissionError"

    def test_credential_change_invalidates_cassette(self, tmp_path: Path) -> None:
        """录制后换密钥：指纹变化 → 无匹配，而非回放出旧凭据下的响应。"""
        first = RequestInfo(
            method="GET", url="https://x/y", headers={"Authorization": "Bearer k1"}
        )
        second = RequestInfo(
            method="GET", url="https://x/y", headers={"Authorization": "Bearer k2"}
        )
        record(first, _make_response(), tmp_path)
        assert find_match(second, tmp_path) is None


class TestPlayAndReplayRequest:
    """测试回放入口。"""

    def test_replay_request_raises_when_no_cassette(self, tmp_path: Path) -> None:
        """无匹配时必须显式报错，不静默返回空响应。"""
        with pytest.raises(ReplayError, match="未找到匹配的 cassette") as excinfo:
            replay_request(_make_request(), tmp_path)
        assert excinfo.value.context["request_hash"] == Cassette.compute_request_hash(
            _make_request()
        )

    def test_replay_request_returns_response(self, tmp_path: Path) -> None:
        """有匹配时 replay_request 应直接返回还原的响应。"""
        request = _make_request()
        record(request, _make_response(status_code=201, body='{"ok": true}'), tmp_path)
        replayed = replay_request(request, tmp_path)
        assert replayed.status_code == 201
        assert replayed.json() == {"ok": True}
        assert replayed.source == "cassette"

    def test_chinese_body_restored(self, tmp_path: Path) -> None:
        """中文请求体与中文响应体都要在回放后原样还原。"""
        request = _make_request(body='{"messages": [{"content": "请用中文回答：你好"}]}')
        record(request, _make_response(body='{"reply": "好的，我用中文回答"}'), tmp_path)
        cassette = find_match(request, tmp_path)
        assert cassette is not None
        # 请求体逐字还原（指纹比较的就是它，中文被改一个字即失配）
        assert cassette.request_body == request.body
        assert play(cassette).text == '{"reply": "好的，我用中文回答"}'

    def test_latest_record_wins(self, tmp_path: Path) -> None:
        """同一请求重复录制：后者覆盖前者，回放返回最新响应。"""
        request = _make_request()
        record(request, _make_response(body='{"version": 1}'), tmp_path)
        record(request, _make_response(body='{"version": 2}'), tmp_path)
        cassette = find_match(request, tmp_path)
        assert cassette is not None
        assert play(cassette).json() == {"version": 2}
        # 同一指纹只有一个文件，不留歧义副本
        assert len(list(tmp_path.glob("*.json"))) == 1


class TestReplayedResponse:
    """测试回放响应对象的读取面。"""

    def test_fields_readable(self, tmp_path: Path) -> None:
        """status_code/headers/body/recorded_at/request_hash 均可直接读取。"""
        request = _make_request()
        record(request, _make_response(status_code=202, body="payload"), tmp_path)
        cassette = find_match(request, tmp_path)
        assert cassette is not None
        replayed = play(cassette)
        assert replayed.status_code == 202
        assert replayed.headers == {"Content-Type": "application/json"}
        assert replayed.body == "payload"
        assert replayed.text == "payload"
        assert replayed.request_hash == cassette.request_hash
        assert replayed.recorded_at == cassette.recorded_at

    def test_text_decodes_bytes(self) -> None:
        """body 为 bytes 时 text 按 UTF-8 解码（供 D6b chunk 回放直接构造）。"""
        replayed = ReplayedResponse(
            status_code=200,
            headers={},
            body="分片内容".encode(),
            recorded_at="2026-10-04T00:00:00+00:00",
            request_hash="0123456789abcdef",
        )
        assert replayed.text == "分片内容"
        assert replayed.content == "分片内容".encode()

    def test_text_rejects_non_utf8_bytes(self) -> None:
        """bytes 无法解码时抛 ReplayError，不抛裸 UnicodeDecodeError。"""
        replayed = ReplayedResponse(
            status_code=200,
            headers={},
            body=b"\xff\xfe",
            recorded_at="2026-10-04T00:00:00+00:00",
            request_hash="0123456789abcdef",
        )
        with pytest.raises(ReplayError, match="UTF-8"):
            _ = replayed.text

    def test_json_invalid_raises(self) -> None:
        """响应体不是合法 JSON 时抛 ReplayError（适配器层只需 except ReplayError）。"""
        replayed = ReplayedResponse(
            status_code=200,
            headers={},
            body="<html>502</html>",
            recorded_at="2026-10-04T00:00:00+00:00",
            request_hash="0123456789abcdef",
        )
        with pytest.raises(ReplayError, match="不是合法 JSON"):
            replayed.json()

    def test_play_does_not_alias_cassette_headers(self, tmp_path: Path) -> None:
        """回放对象的 headers 是副本，改它不应污染 cassette。"""
        request = _make_request()
        record(request, _make_response(), tmp_path)
        cassette = find_match(request, tmp_path)
        assert cassette is not None
        replayed = play(cassette)
        replayed.headers["X-Injected"] = "1"
        assert "X-Injected" not in cassette.response_headers


class TestErrorRedaction:
    """测试异常消息脱敏（遵循 exceptions.py 只渲染 context 键名的约定）。"""

    def test_replay_error_str_hides_request_body(self, tmp_path: Path) -> None:
        """含隐私的请求体不得出现在 str(exc) 中，否则会随日志与报告泄露。"""
        request = _make_request(body='{"pii": "' + _SECRET_BODY + '"}')
        path = Path(record(request, _make_response(), tmp_path))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["request_hash"] = "0123456789abcdef"
        _write_raw(path, json.dumps(payload, ensure_ascii=False))

        with pytest.raises(ReplayError) as excinfo:
            find_match(request, tmp_path)
        assert _SECRET_BODY not in str(excinfo.value)
        # 键名仍可见：排障时能知道去哪里取值
        assert "current_request_hash" in str(excinfo.value)
        # 取值本身仍可通过 context 显式读取
        assert excinfo.value.context["current_request_hash"]

    def test_replay_error_is_aquamind_error(self, tmp_path: Path) -> None:
        """ReplayError 须能被基类 AquaMindError 捕获（调用方按基类兜底）。"""
        from aquamind.exceptions import AquaMindError

        with pytest.raises(AquaMindError):
            replay_request(_make_request(), tmp_path)


def cassette_header(path: Path, name: str) -> str:
    """读取 cassette 文件中的某个请求头值（测试辅助）。

    Args:
        path: cassette 文件路径。
        name: 请求头名（小写）。

    Returns:
        str: 该请求头的落盘值。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    return str(payload["request_headers"][name])
