"""VCR 文本录制回放：Cassette 数据结构 + record/find_match/play。

本模块（M1-D06a）只覆盖**文本层面**的录制与回放：把一次请求与其响应落成
一个 cassette 文件，回放时按请求指纹找到同一份 cassette 并还原响应对象。

结构上的扩展预留：chunk 到达时间（``timing``，M1-D06b 录制）与时序调度回放
（M1-D06c）以**带默认值的可选字段**形式追加在 Cassette 上，字段一旦出现即被
结构接收；未携带新字段的旧 cassette 仍按瞬时回放处理。因此这里不用任何
写死的字段清单或「多字段一张表」的封闭结构。

指纹与脱敏约定：
    ``request_hash`` 由「方法 + URL + 关键请求头（排序后）+ 请求体」规范化后的
    SHA-256 前 16 位十六进制串构成，是判定「同一个请求」的稳定依据，同时充当
    版本字段——回放时重算的 hash 与 cassette 中记录的不一致，就说明请求已变更
    （改了模型名、改了 body、换了密钥……），必须重新录制，否则回放出来的将是
    一个与当前请求无关的响应。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .config import Settings
from .exceptions import ReplayError

# request_hash 的十六进制长度：16 位约 64 bit，对本地 cassette 库碰撞概率足够，
# 同时让文件名短到肉眼可比对
_HASH_LENGTH = 16
# 十六进制字符集，用于 request_hash 形状校验
_HEX_DIGITS = frozenset("0123456789abcdef")
# 计入请求指纹的普通关键请求头（统一小写比较）
_KEY_HEADERS: frozenset[str] = frozenset({"content-type", "accept"})
# 敏感请求头：参与指纹，但**不以明文落盘**（落盘时替换为短摘要）
_SENSITIVE_HEADERS: frozenset[str] = frozenset({"authorization", "x-api-key"})
# 敏感请求头落盘时的占位前缀，便于人工辨认「这里是摘要不是真值」
_MASK_PREFIX = "sha256:"
# cassette 文件扩展名
_CASSETTE_SUFFIX = ".json"


class RequestInfo(BaseModel):
    """一次请求的输入信息：录制与回放两侧共用的请求指纹来源。

    Attributes:
        method: HTTP 方法，构造指纹时统一去空白并转大写。
        url: 完整请求 URL。
        headers: 请求头全量输入。只有关键头（content-type/accept）与敏感头
            （authorization/x-api-key）进入指纹与 cassette，其余头（如
            User-Agent、Trace 标识）不参与，避免客户端版本或链路抖动让
            cassette 莫名失效。
        body: 请求体原文，由调用方自行序列化。JSON 载荷建议用
            ``sort_keys=True`` 序列化，使键序不同的等价请求得到同一指纹。
    """

    # 严格模式：禁止未知字段，防止调用方拼错字段名后被静默忽略
    model_config = ConfigDict(extra="forbid")

    method: str = Field(description="HTTP 方法，如 GET/POST")
    url: str = Field(description="完整请求 URL")
    headers: dict[str, str] = Field(default_factory=dict, description="请求头全量输入")
    body: str | None = Field(default=None, description="请求体原文，无请求体时为 None")


class ResponseInfo(BaseModel):
    """一次响应的输入信息：录制时写入 cassette 的响应侧内容。

    Attributes:
        status_code: HTTP 状态码。
        headers: 响应头全量输入，原样落盘。
        body: 响应体。str 原样落盘；bytes 在 record 边界按 UTF-8 解码为文本，
            使 cassette 始终是合法 UTF-8 JSON。
    """

    # 严格模式：禁止未知字段，防止调用方拼错字段名后被静默忽略
    model_config = ConfigDict(extra="forbid")

    status_code: int = Field(description="HTTP 状态码")
    headers: dict[str, str] = Field(default_factory=dict, description="响应头全量输入")
    body: str | bytes | None = Field(default=None, description="响应体，无响应体时为 None")


class Cassette(BaseModel):
    """cassette：一次「请求 + 响应」录制的完整落盘结构。

    一个 cassette 文件 = 一个请求指纹下的最新一份响应录制；文件名为
    ``{request_hash}.json``，故同一请求重复录制即覆盖（最新一次生效）。

    Attributes:
        request_method: 请求方法（大写）。
        request_url: 请求 URL。
        request_headers: 归一化后的关键请求头（敏感头值已替换为短摘要）。
        request_body: 请求体原文。
        response_status_code: 响应状态码。
        response_headers: 响应头。
        response_body: 响应体文本。
        request_hash: 请求指纹的稳定 hash，兼作版本字段。
        recorded_at: 录制时刻（UTC，ISO 8601）。
    """

    # 严格模式：禁止未知字段，保证回放侧不会把不认识的键当成有效录制内容
    model_config = ConfigDict(extra="forbid")

    request_method: str = Field(description="请求方法（大写）")
    request_url: str = Field(description="请求 URL")
    request_headers: dict[str, str] = Field(
        default_factory=dict,
        description="归一化关键请求头，敏感头值为短摘要",
    )
    request_body: str | None = Field(default=None, description="请求体原文")
    response_status_code: int = Field(ge=100, le=599, description="响应状态码")
    response_headers: dict[str, str] = Field(default_factory=dict, description="响应头")
    response_body: str = Field(default="", description="响应体文本")
    request_hash: str = Field(description="请求指纹的稳定 hash（版本字段）")
    recorded_at: str = Field(description="录制时刻（UTC，ISO 8601）")

    @staticmethod
    def compute_request_hash(request: RequestInfo) -> str:
        """计算请求指纹的稳定 hash。

        规范化内容为「方法（大写）+ URL + 关键请求头（键名排序）+ 请求体」，
        以 JSON 紧凑序列化后取 SHA-256 前 16 位十六进制。同一请求无论调用
        多少次、请求头书写顺序如何，结果都一致；任一要素变化则结果变化。

        Args:
            request: 请求信息。

        Returns:
            str: 16 位十六进制的请求指纹。
        """
        canonical = json.dumps(
            {
                "method": request.method.strip().upper(),
                "url": request.url,
                "headers": _canonical_headers(request.headers),
                # None 与空串归一为同一个指纹：「没有请求体」与「请求体为空串」
                # 对服务端而言是同一次请求
                "body": request.body or "",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_HASH_LENGTH]

    @field_validator("request_hash")
    @classmethod
    def _check_request_hash_shape(cls, value: str) -> str:
        """校验 request_hash 是 16 位十六进制串。

        校验意图：hash 是「查文件 + 验版本」的唯一依据。若它被手改成空串、截断串
        或非十六进制串，find_match 会拿它拼出错误的文件名而永远匹配不上，
        排障时只会看到「无匹配 cassette」这种无效线索。放在契约层挡住，可在
        加载 cassette 的当下就报出字段级错误。

        Args:
            value: 待校验的 request_hash。

        Returns:
            str: 校验通过的 request_hash（原样返回）。

        Raises:
            ValueError: 长度或字符集不符合要求时抛出。
        """
        if len(value) != _HASH_LENGTH or any(c not in _HEX_DIGITS for c in value):
            raise ValueError(f"request_hash 必须是 {_HASH_LENGTH} 位十六进制串，实际: {value!r}")
        return value


@dataclass(frozen=True)
class ReplayedResponse:
    """回放还原出的轻量响应对象。

    读取面（status_code / headers / text / content / json）与 httpx.Response
    对齐，使适配器层读取响应的方式（见 ``adapters/openai.py`` 依次读
    status_code、text、json）无需为回放场景改写。

    选用 frozen dataclass 而非 pydantic 模型：其内容全部来自已通过契约校验的
    Cassette，无需二次校验；不可变保证回放结果不会被下游意外改写；附带字段
    （recorded_at / request_hash / source）随结构演进，不影响现有读取面。

    Attributes:
        status_code: 响应状态码。
        headers: 响应头。
        body: 响应体原文（str 或 bytes）。
        recorded_at: 原录制的时刻（UTC，ISO 8601）。
        request_hash: 命中的请求指纹，便于日志溯源到具体 cassette。
        source: 响应来源标识，恒为 "cassette"。
    """

    status_code: int
    headers: dict[str, str]
    body: str | bytes
    recorded_at: str
    request_hash: str
    source: str = "cassette"

    @property
    def text(self) -> str:
        """响应体文本。

        Returns:
            str: body 为 str 时原样返回；为 bytes 时按 UTF-8 解码。

        Raises:
            ReplayError: body 为 bytes 且不是合法 UTF-8 时抛出。
        """
        if isinstance(self.body, bytes):
            try:
                return self.body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ReplayError(
                    message="回放响应体不是合法的 UTF-8 文本",
                    context={"request_hash": self.request_hash, "body_length": len(self.body)},
                ) from exc
        return self.body

    @property
    def content(self) -> bytes:
        """响应体字节。

        Returns:
            bytes: 与 httpx.Response.content 同义的 UTF-8 字节串。
        """
        return self.text.encode("utf-8")

    def json(self) -> Any:
        """把响应体解析为 JSON。

        Returns:
            Any: 解析后的 JSON 值（顶层通常是对象）。

        Raises:
            ReplayError: 响应体不是合法 JSON 时抛出。刻意不把 json.JSONDecodeError
                透给调用方——适配器层只需 except ReplayError 即可覆盖全部回放异常。
        """
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as exc:
            raise ReplayError(
                message="回放响应体不是合法 JSON",
                context={
                    "request_hash": self.request_hash,
                    "status_code": self.status_code,
                    "line": exc.lineno,
                },
            ) from exc


def record(
    request_info: RequestInfo,
    response_info: ResponseInfo,
    cassette_dir: str | Path | None = None,
) -> str:
    """录制一次请求-响应并落盘为 cassette 文件。

    文件名为 ``{request_hash}.json``：同一请求重复录制会**覆盖**旧文件
    （最新一次录制生效），避免同一指纹下堆积多个文件导致匹配歧义。

    Args:
        request_info: 请求信息，其指纹由 :meth:`Cassette.compute_request_hash` 决定。
        response_info: 响应信息，bytes 响应体在边界按 UTF-8 解码为文本。
        cassette_dir: cassette 存储目录，目录不存在时自动创建；None 时取全局配置
            ``Settings.replay_dir``（可用 ``AQ_REPLAY_DIR`` 覆盖）。

    Returns:
        str: 落盘文件的路径字符串。

    Raises:
        ReplayError: 响应体 bytes 无法按 UTF-8 解码，或目录/文件创建写入失败时抛出。
        ConfigError: cassette_dir 为 None 且全局配置本身非法时由 config 层抛出，
            本模块不吞配置层异常。
    """
    cassette = Cassette(
        request_method=request_info.method.strip().upper(),
        request_url=request_info.url,
        request_headers=_canonical_headers(request_info.headers),
        request_body=request_info.body,
        response_status_code=response_info.status_code,
        response_headers=dict(response_info.headers),
        response_body=_normalize_response_body(response_info.body),
        request_hash=Cassette.compute_request_hash(request_info),
        recorded_at=datetime.now(UTC).isoformat(),
    )
    directory = _resolve_cassette_dir(cassette_dir)
    path = directory / f"{cassette.request_hash}{_CASSETTE_SUFFIX}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # pydantic 序列化本身不转义非 ASCII，中文直接以 UTF-8 写入，人工可读
        path.write_text(cassette.model_dump_json(indent=2), encoding="utf-8")
    except OSError as exc:
        raise ReplayError(
            message="cassette 落盘失败",
            context={"cassette": str(path), "error_type": type(exc).__name__},
        ) from exc
    return str(path)


def find_match(
    request_info: RequestInfo,
    cassette_dir: str | Path | None = None,
) -> Cassette | None:
    """在 cassette 目录中查找与请求指纹匹配的录制。

    匹配策略为**精确匹配**（方法 + URL + 关键请求头 + 请求体派生的同一 hash），
    不做模糊匹配：一条用例要复现的必须是当时那一次确定的响应，宽松匹配会把
    「回放到了别的响应却以为成功」变成静默的错误。找不到即返回 None，
    由调用方决定报错还是转去录制。

    Args:
        request_info: 请求信息。
        cassette_dir: cassette 存储目录；None 时取全局配置 ``Settings.replay_dir``。

    Returns:
        Cassette | None: 命中的 cassette；目录或文件不存在时返回 None。

    Raises:
        ReplayError: cassette 文件读取失败、JSON 损坏、字段不符合契约，或文件内
            记录的 hash 与重算 hash 不一致（请求已变更）时抛出。
    """
    expected_hash = Cassette.compute_request_hash(request_info)
    path = _resolve_cassette_dir(cassette_dir) / f"{expected_hash}{_CASSETTE_SUFFIX}"
    if not path.is_file():
        return None
    cassette = _load_cassette(path)
    if cassette.request_hash != expected_hash:
        # 文件名对得上、内容里的 hash 对不上：只能是被手改过，或文件被改名/复制
        # 自另一请求。两条路径都会让回放结果失真，必须显式拦截而不是照常回放。
        raise ReplayError(
            message="cassette 请求指纹已变更，请重新录制",
            context={
                "cassette": str(path),
                "recorded_request_hash": cassette.request_hash,
                "current_request_hash": expected_hash,
            },
        )
    return cassette


def play(cassette: Cassette) -> ReplayedResponse:
    """把 cassette 还原为响应对象。

    只做还原，不做重试、归一化或状态码判断——那些属于调用方（适配器层）的职责。

    Args:
        cassette: 命中的 cassette。

    Returns:
        ReplayedResponse: 还原出的响应对象，字段与录制时一一对应。
    """
    return ReplayedResponse(
        status_code=cassette.response_status_code,
        headers=dict(cassette.response_headers),
        body=cassette.response_body,
        recorded_at=cassette.recorded_at,
        request_hash=cassette.request_hash,
    )


def replay_request(
    request_info: RequestInfo,
    cassette_dir: str | Path | None = None,
) -> ReplayedResponse:
    """查找并回放一次请求，无匹配时显式报错。

    这是「只回放」模式的入口：与 :func:`find_match` 的区别在于，找不到 cassette
    时以异常形式暴露，而不是把 None 交给调用方——静默返回空响应会让一次本该
    校验录制数据真伪的离线回归，跑成「什么都没校验」。

    Args:
        request_info: 请求信息。
        cassette_dir: cassette 存储目录；None 时取全局配置 ``Settings.replay_dir``。

    Returns:
        ReplayedResponse: 还原出的响应对象。

    Raises:
        ReplayError: 无匹配 cassette，或 cassette 损坏、指纹已变更时抛出。
    """
    cassette = find_match(request_info, cassette_dir)
    if cassette is None:
        raise ReplayError(
            message="未找到匹配的 cassette（首次运行或请求已变更），请先执行录制",
            context={
                "request_hash": Cassette.compute_request_hash(request_info),
                "method": request_info.method,
                "url": request_info.url,
                "cassette_dir": str(_resolve_cassette_dir(cassette_dir)),
            },
        )
    return play(cassette)


def _canonical_headers(headers: dict[str, str]) -> dict[str, str]:
    """归一化请求头：只保留关键头，敏感头值替换为短摘要。

    两点约定：

    1. 头名统一去空白并转小写。HTTP 头名大小写不敏感，若不归一，
       ``Content-Type`` 与 ``content-type`` 会算出两个不同指纹，让 cassette
       因调用方书写习惯不同而莫名失效。
    2. 敏感头（authorization/x-api-key）**不以明文落盘**，改存该值的 SHA-256
       前 16 位。这样既能让「换了密钥」被指纹察觉、触发重新录制，又不会把凭据
       写进会被提交进版本库的 cassette 文件。摘要只用于比对，不用于还原。

    Args:
        headers: 请求头全量输入。

    Returns:
        dict[str, str]: 归一化后的关键请求头（敏感头为摘要值）。
    """
    canonical: dict[str, str] = {}
    for name, value in headers.items():
        key = name.strip().lower()
        if key in _SENSITIVE_HEADERS:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
            canonical[key] = f"{_MASK_PREFIX}{digest}"
        elif key in _KEY_HEADERS:
            canonical[key] = value
    return canonical


def _normalize_response_body(body: str | bytes | None) -> str:
    """把响应体归一为可落盘的 UTF-8 文本。

    cassette 是 JSON 文件，bytes 无法直接写入；统一在 record 边界解码，使落盘
    格式单一、回放时不必再猜编码。

    Args:
        body: 响应体原文。

    Returns:
        str: 归一后的文本；None 归一为空串。

    Raises:
        ReplayError: body 为 bytes 且不是合法 UTF-8 时抛出。
    """
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReplayError(
            message="响应体 bytes 不是合法的 UTF-8，无法录制为 cassette",
            context={"body_length": len(body)},
        ) from exc


def _resolve_cassette_dir(cassette_dir: str | Path | None) -> Path:
    """解析 cassette 存储目录。

    Args:
        cassette_dir: 显式目录；None 时回落到全局配置 ``Settings.replay_dir``。

    Returns:
        Path: 存储目录路径。
    """
    if cassette_dir is not None:
        return Path(cassette_dir)
    return Settings().replay_dir


def _load_cassette(path: Path) -> Cassette:
    """从磁盘加载 cassette 并完成契约校验。

    Args:
        path: cassette 文件路径。

    Returns:
        Cassette: 通过契约校验的 cassette。

    Raises:
        ReplayError: 文件不可读、非 UTF-8、JSON 解析失败，或字段不符合契约时抛出。
            三类失败都在此包装成 ReplayError：调用方只需 except ReplayError，
            不会被 json.JSONDecodeError / pydantic ValidationError 击穿。
    """
    try:
        # utf-8-sig 兼容「有 BOM」与「无 BOM」两种文件（与 loaders 口径一致）
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReplayError(
            message="cassette 文件读取失败",
            context={"cassette": str(path), "error_type": type(exc).__name__},
        ) from exc
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ReplayError(
            message="cassette 文件损坏：JSON 解析失败",
            context={"cassette": str(path), "line": exc.lineno},
        ) from exc
    try:
        return Cassette.model_validate(data)
    except ValidationError as exc:
        raise ReplayError(
            message="cassette 文件损坏：字段不符合 Cassette 契约",
            context={
                "cassette": str(path),
                "error_count": exc.error_count(),
                "error_types": sorted({error["type"] for error in exc.errors()}),
            },
        ) from exc
