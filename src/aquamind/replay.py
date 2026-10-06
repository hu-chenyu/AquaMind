"""VCR 录制回放：Cassette 数据结构 + record/find_match/play + chunk 时序录制与调度。

本模块覆盖**文本层面**与**chunk 到达时序**两个层面：

- M1-D06a：把一次请求与其响应落成一个 cassette 文件，回放时按请求指纹找到
  同一份 cassette 并还原响应对象（``play()``：一次性给全完整响应）。
- M1-D06b：流式响应的每个 chunk 及其相对到达时刻一并录进 cassette 的
  ``timing`` 字段（**只录**）。
- M1-D06c：把录下的时刻用于**调度**——``play_timed()`` 按 ``arrival_ms`` 逐片
  产出、支持加速/减速倍率，并给出可测量的时序偏差；无时序可调度时（``timing``
  为 ``None`` 或空）退化为与 ``play()`` 一致的瞬时回放。

``play()`` 与 ``play_timed()`` 是两条**并存**的口径而非新旧替代：前者是「拿到完整
响应」的读取面（读取方式与 ``httpx.Response`` 对齐），后者是「复现流式到达节奏」
的行为面。分两个入口而不是给 ``play()`` 挂 ``timed`` 开关，是为了让「这次回放要不要
时序」在调用点就必须表态——隐式开关会让同一次回放在不同调用点悄悄给出不同的 chunk
边界，而 chunk 边界正是流式使用方（打字机、进度条）唯一关心的事。

结构上的扩展约定：后续追加的字段一律是**带默认值的可选字段**，旧 cassette
缺该字段时按缺省语义处理（``timing`` 缺省为 None，即「非流式/旧格式，按瞬时
回放」），因此不用任何写死的字段清单或「多字段一张表」的封闭结构。

版本兼容由 ``format_version`` 字段承担：当前为 v2（v2 相对 v1 新增 ``timing``）。
该字段缺失时按 v1 解析（向后兼容），高于 ``CURRENT_FORMAT_VERSION`` 时直接报
「版本过新」并提示升级（向前不兼容），从而把「文件太新」与「文件损坏」这两类
根因区分开。

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
import math
import statistics
import time
from collections.abc import Iterator
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
# arrival_ms 保留的小数位：毫秒级留 3 位即微秒精度，既足以表达 chunk 之间的
# 到达间隔，又能把浮点表示的尾差（如 0.1+0.2=0.30000000000000004）挡在落盘前，
# 避免同一段流在不同机器上录出肉眼相同、数值不同的 arrival_ms
_ARRIVAL_PRECISION = 3
# 时序偏差统计的小数位：与 arrival_ms 同口径（毫秒保留 3 位 = 微秒精度）。
# 偏差是两个毫秒值相减的结果，不设上限会把浮点尾差（如 0.1+0.2 产生的
# 2.7755575615628914e-17）放大成报告里刺眼的长尾小数
_DEVIATION_PRECISION = 3
# 单次 sleep 的下界（秒）：比平台定时器粒度更小的等待没有意义——Windows 传统
# 定时器粒度约 15.6ms、Linux 通常 1ms，睡这样一个量只会多一次系统调用，还可能
# 因为刚好错过下一个时间片而睡过头。低于它就不睡，直接继续逐片产出
_MIN_SLEEP_SECONDS = 0.001
# 本模块支持的 cassette 格式版本。v2 相对 v1 新增 timing 字段。
# 向后兼容：新字段一律带默认值，旧 cassette 缺少该字段时按 v1 解析；
# 向前不兼容：文件版本高于此值时明确报「版本过新」，而不是笼统的
# 「字段不符合契约」——后者会把「用旧代码读新文件」误导成「文件损坏」，
# 排障方向从一开始就错。
CURRENT_FORMAT_VERSION = 2


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


class ChunkTiming(BaseModel):
    """单个 chunk 的到达时序记录（流式响应专有）。

    一个流式响应的「内容」与「节奏」是两个正交的事实：文本录制只还原了前者，
    而真实调用方（前端打字机、进度条、逐字播报）体验到的是后者。本模型承载
    每个 chunk 的文本与它相对第一个 chunk 的到达时刻，使 M1-D06c 能够按录下
    的时间调度 chunk 输出。

    时刻存**相对偏移**而非绝对时间：录制的绝对时刻受发起机器时钟与运行时刻
    影响，既不可复现也无回放价值，调用方真正需要的是「chunk 之间隔多久」。

    Attributes:
        index: chunk 序号，从 0 开始，与 ``arrival_ms`` 归一化后的起点对齐。
        arrival_ms: 相对第一个 chunk 的到达时间，单位毫秒，保留 3 位小数；
            第一个 chunk 恒为 0.0。浮点而非整数是为了容纳亚毫秒级的首包抖动。
        text: 该 chunk 的文本内容（增量文本，拼接后等于完整响应体）。
    """

    # 严格模式：禁止未知字段，防止调用方拼错字段名后被静默忽略
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0, description="chunk 序号，从 0 开始")
    arrival_ms: float = Field(
        description="相对第一个 chunk 的到达时间（毫秒，保留 3 位小数），第一个恒为 0.0",
        # 非有限值（inf/NaN）必须在契约层就拒：pydantic 序列化会把它们写成 null，
        # 于是 record() 成功返回路径、文件却读不回来（加载时报「字段不符合契约」），
        # 「录制输入非法」被说成「文件损坏」，与 D6a 确立的区分根因原则相悖
        allow_inf_nan=False,
    )
    text: str = Field(description="该 chunk 的文本内容")


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
        format_version: cassette 格式版本号，用于前向兼容检测。缺该字段的旧
            cassette 按 v1 解析，故默认值取 1 而非当前版本号。
        timing: 流式响应的 chunk 到达时序记录；非流式响应或旧格式 cassette 为 None。
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
    format_version: int = Field(
        default=1,
        ge=1,
        description="cassette 格式版本号，用于前向兼容检测；缺失时按 v1 解析",
    )
    # 带默认值的可选字段：M1-D06a 录制的旧 cassette 无此键，加载后为 None，
    # 语义是「非流式响应 / 旧格式」，M1-D06c 据此按瞬时回放处理
    timing: list[ChunkTiming] | None = Field(
        default=None,
        description="流式响应的 chunk 到达时序记录；非流式响应或旧格式 cassette 为 None",
    )

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


@dataclass(frozen=True)
class TimingDeviation:
    """时序回放的偏差统计：各 chunk 实际到达时刻相对**计划**到达时刻的偏移。

    偏差的定义是「实际到达时刻 − 计划到达时刻」，正数表示比计划晚、负数表示比
    计划早。计划到达时刻 = 录下的 ``arrival_ms`` **先除以 speed**（加速回放时
    计划时刻同样前移），因此不同倍率下的偏差是可比的：它衡量的是「调度本身准不准」，
    而不是「和原速差多少」——后者只是倍率的定义，不是回放的缺陷。

    瞬时回放（``timing`` 为 ``None`` 或空）不 sleep，故没有可调度的时刻，
    偏差恒为 0.0（详见 ``TimedReplay``）。

    Attributes:
        speed: 本次回放使用的倍率。
        indexes: 已产出 chunk 的序号，按产出顺序。
        expected_ms: 各 chunk 的计划到达时刻（毫秒，首片恒为 0.0）。
        actual_ms: 各 chunk 的实际到达时刻（毫秒，相对首片）。
    """

    speed: float
    indexes: tuple[int, ...]
    expected_ms: tuple[float, ...]
    actual_ms: tuple[float, ...]

    @property
    def chunk_count(self) -> int:
        """已产出的 chunk 数（瞬时回放时为 1，那 1 项是整段响应文本）。

        Returns:
            int: 参与统计的 chunk 数量。
        """
        return len(self.indexes)

    @property
    def deviations_ms(self) -> tuple[float, ...]:
        """逐片的**带符号**偏差（毫秒）：实际到达时刻 − 计划到达时刻。

        Returns:
            tuple[float, ...]: 与 ``indexes`` 一一对应的偏差序列，保留 3 位小数。
        """
        return tuple(
            round(actual - expected, _DEVIATION_PRECISION)
            for actual, expected in zip(self.actual_ms, self.expected_ms, strict=True)
        )

    @property
    def max_deviation_ms(self) -> float:
        """最大偏差幅度（毫秒，对带符号偏差取绝对值）。

        回放的目的是复现节奏，「提前 20ms」与「滞后 20ms」对使用者的体感是对称的，
        故汇总指标一律用绝对值；需要看方向时读 :attr:`deviations_ms`。

        Returns:
            float: 最大偏差幅度（毫秒）；无产出时为 0.0。
        """
        deviations = self.deviations_ms
        if not deviations:
            return 0.0
        return round(max(abs(value) for value in deviations), _DEVIATION_PRECISION)

    @property
    def mean_deviation_ms(self) -> float:
        """平均偏差幅度（毫秒，对带符号偏差取绝对值后求均值）。

        Returns:
            float: 平均偏差幅度（毫秒）；无产出时为 0.0。
        """
        deviations = self.deviations_ms
        if not deviations:
            return 0.0
        mean = statistics.fmean(abs(value) for value in deviations)
        return round(mean, _DEVIATION_PRECISION)


class TimedReplay:
    """时序回放流：按录下的时刻逐片产出 chunk 文本，并累计时序偏差。

    语义上它**就是**一个文本迭代器（``iter(stream) is stream``），
    ``for chunk in play_timed(cassette)`` 与消费一个生成器的写法完全一致；在此
    之上多挂一个 ``deviation`` 属性，让「调度得准不准」成为可测量的数字，而不是
    只能靠肉眼看输出节奏。设计取舍见 ``docs/adr/0008-timing-replay.md``。

    偏差统计随迭代推进：**生成器未耗尽时只反映已产出的部分**。

    Attributes:
        speed: 回放倍率。
        instant: 是否走瞬时回放（``timing`` 为 ``None`` 或空）。
        deviation: 截至当前的时序偏差统计。
        text: 已产出 chunk 的拼接文本。
    """

    def __init__(self, cassette: Cassette, speed: float = 1.0) -> None:
        """按 ``speed`` 建立回放流（构造时即校验 ``speed``，不推迟到第一次迭代）。

        Args:
            cassette: 命中的 cassette；``timing`` 为 ``None`` 或空时按瞬时回放。
            speed: 回放倍率，>1 加速、<1 减速；必须为正有限数。

        Raises:
            ReplayError: ``speed`` 不是正有限数时抛出。
        """
        _validate_speed(speed, cassette.request_hash)
        self._speed = speed
        self._instant = not cassette.timing
        self._stream = _iter_timed_chunks(cassette, speed)
        self._indexes: list[int] = []
        self._expected_ms: list[float] = []
        self._actual_ms: list[float] = []
        self._texts: list[str] = []

    def __iter__(self) -> Iterator[str]:
        """返回自身（``TimedReplay`` 自身即迭代器）。

        Returns:
            Iterator[str]: 产出 chunk 文本的迭代器。
        """
        return self

    def __next__(self) -> str:
        """产出下一个 chunk 文本并记录它的计划/实际到达时刻。

        Returns:
            str: 下一个 chunk 的文本；时序回放为该片增量文本，瞬时回放为完整响应
                文本（一次性给全）。

        Raises:
            StopIteration: 回放已耗尽时抛出（生成器耗尽的自然终止）。
        """
        index, expected_ms, actual_ms, text = next(self._stream)
        self._indexes.append(index)
        self._expected_ms.append(expected_ms)
        self._actual_ms.append(actual_ms)
        self._texts.append(text)
        return text

    @property
    def speed(self) -> float:
        """本次回放使用的倍率。

        Returns:
            float: speed 参数值。
        """
        return self._speed

    @property
    def instant(self) -> bool:
        """本次回放是否为瞬时回放（无时序可调度）。

        Returns:
            bool: ``timing`` 为 ``None``（旧格式 / 非流式）或空列表时为 True。
        """
        return self._instant

    @property
    def deviation(self) -> TimingDeviation:
        """截至当前的时序偏差统计。

        Returns:
            TimingDeviation: 含逐片计划/实际时刻与偏差汇总；生成器耗尽后为完整统计。
        """
        return TimingDeviation(
            speed=self._speed,
            indexes=tuple(self._indexes),
            expected_ms=tuple(self._expected_ms),
            actual_ms=tuple(self._actual_ms),
        )

    @property
    def text(self) -> str:
        """已产出 chunk 的拼接文本（未耗尽时只含已产出部分）。

        Returns:
            str: 拼接结果；时序回放下等于录制时各 chunk 增量文本的拼接。
        """
        return "".join(self._texts)


def record(
    request_info: RequestInfo,
    response_info: ResponseInfo,
    cassette_dir: str | Path | None = None,
    chunks: list[tuple[float, str]] | None = None,
) -> str:
    """录制一次请求-响应并落盘为 cassette 文件。

    文件名为 ``{request_hash}.json``：同一请求重复录制会**覆盖**旧文件
    （最新一次录制生效），避免同一指纹下堆积多个文件导致匹配歧义。

    流式响应额外传入 ``chunks``，逐 chunk 的到达时刻与文本一并录进
    ``timing``。本函数**只录不播**：``timing`` 被完整落盘并可回放读取，但
    ``play()`` 仍一次性返回完整响应（时序调度留给 M1-D06c）。

    Args:
        request_info: 请求信息，其指纹由 :meth:`Cassette.compute_request_hash` 决定。
        response_info: 响应信息，bytes 响应体在边界按 UTF-8 解码为文本。
        cassette_dir: cassette 存储目录，目录不存在时自动创建；None 时取全局配置
            ``Settings.replay_dir``（可用 ``AQ_REPLAY_DIR`` 覆盖）。
        chunks: 流式响应的 chunk 列表，每个元素为 ``(arrival_ms, text)``；
            ``arrival_ms`` 是该 chunk 的到达时刻（毫秒），函数内部以第一个
            chunk 为原点归一化。None 表示非流式响应（``timing`` 保持 None）；
            空列表表示流式响应但未采集到 chunk（``timing`` 为空列表）。

    Returns:
        str: 落盘文件的路径字符串。

    Raises:
        ReplayError: 响应体 bytes 无法按 UTF-8 解码，或目录/文件创建写入失败时抛出。
        ConfigError: cassette_dir 为 None 且全局配置本身非法时由 config 层抛出，
            本模块不吞配置层异常。
        ValidationError: chunks 中的到达时刻不是有限数（inf/NaN）时由 pydantic
            抛出，本函数不捕获——与 ``load_cases()`` 对用例契约违规的同一口径：
            调用方输入非法应在录制边界当场暴露，而不是落盘后变成「文件损坏」。
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
        format_version=CURRENT_FORMAT_VERSION,
        timing=None if chunks is None else _build_timing(chunks),
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
        ReplayError: cassette 文件读取失败、JSON 损坏、字段不符合契约，文件声明的
            格式版本高于本模块支持的上限（见 ``CURRENT_FORMAT_VERSION``），或文件内
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

    M1-D06b 边界（仍然成立）：``play()`` 仍**一次性给全**响应体，不按录下的时刻
    调度 chunk。需要时序保真请改用 :func:`play_timed`——两者并存而非替代。

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


def play_timed(cassette: Cassette, speed: float = 1.0) -> TimedReplay:
    """按录下的时刻调度回放：逐片产出 chunk，并给出时序偏差统计（M1-D06c）。

    三种 cassette 形态对应三条行为，且都只走 ``play_timed()`` 这一个入口：

    1. ``timing`` 非空：按时序调度。首片立即产出，其余各片按
       ``(arrival_ms − 首片 arrival_ms) / speed`` 的间隔 sleep 后产出；
    2. ``timing is None``（v1 旧格式或非流式响应）：瞬时回放，一次性产出完整响应
       文本，不 sleep；
    3. ``timing == []``（流式但未采集到 chunk）：同 2，瞬时回放。

    时序回放**只改变 chunk 的到达时间，不改动任何文本内容**：各片文本按录制顺序
    原样产出，拼接结果与录制时一致。指纹校验不在本函数内——它由 ``find_match()``
    完成（:func:`replay_request_timed` 是把两者串起来的入口）。

    Args:
        cassette: 命中的 cassette。
        speed: 回放倍率。>1 加速（间隔变短）、<1 减速（间隔变长）、1.0 原速。
            极大倍率下间隔趋近 0，此时退化为「不 sleep 但仍逐片产出」，而不是
            一次性给全——chunk 边界对流式使用方仍然可见。

    Returns:
        TimedReplay: 产出 chunk 文本的迭代器，附带 :attr:`TimedReplay.deviation`
            时序偏差统计。

    Raises:
        ReplayError: ``speed`` 不是正有限数（<=0、NaN、±inf）时抛出，不静默回退成
            原速。
    """
    return TimedReplay(cassette, speed)


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


def replay_request_timed(
    request_info: RequestInfo,
    cassette_dir: str | Path | None = None,
    speed: float = 1.0,
) -> TimedReplay:
    """查找并按时序调度回放一次请求，指纹不符/无匹配时显式报错。

    这是「时序回放」模式的入口，等价于 :func:`find_match` + :func:`play_timed`：
    与 :func:`replay_request` 一样，找不到 cassette 或 cassette 内指纹与重算值
    不一致时以异常暴露——时序回放同样必须先证明「回放的是这一次请求」，否则按
    录下节奏调度出来的是一段**别人的**响应，比文本错更隐蔽。

    Args:
        request_info: 请求信息。
        cassette_dir: cassette 存储目录；None 时取全局配置 ``Settings.replay_dir``。
        speed: 回放倍率，语义同 :func:`play_timed`。

    Returns:
        TimedReplay: 产出 chunk 文本的迭代器，附带时序偏差统计。

    Raises:
        ReplayError: ``speed`` 不是正有限数、无匹配 cassette，或 cassette 损坏、
            指纹已变更时抛出。
    """
    # 先校验调用参数再看磁盘：倍率非法是调用方自己的错，不该被「文件恰好不存在」
    # 这类数据问题掩盖掉，报错指向错误的一方会让人多绕一圈才找到真因
    _validate_speed(speed, Cassette.compute_request_hash(request_info))
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
    return play_timed(cassette, speed)


def _validate_speed(speed: float, request_hash: str) -> None:
    """校验回放倍率必须是正有限数。

    非法倍率一律显式报错而不静默回退成原速，理由是「回放得慢/快」与「回放的
    根本不是这段时序」在结果里长得一样：一次 0.5x 被悄悄当成 1.0x 的回放，会让
    依赖节奏的下游测试通过，却验证了错误的条件。

    Args:
        speed: 待校验的回放倍率。
        request_hash: 相关请求指纹，仅用于错误上下文定位。

    Returns:
        None: ``speed`` 为正有限数时正常返回。

    Raises:
        ReplayError: ``speed`` <= 0、NaN 或 ±inf 时抛出。这三类值会让
            「间隔 ÷ speed」得到负数或 NaN，而 ``time.sleep(负数)`` 在 Windows 上
            抛 ValueError、sleep(NaN) 静默不睡——都不该由回放层兜着。
    """
    if speed > 0 and math.isfinite(speed):
        return
    raise ReplayError(
        message=f"speed 必须为正数（0 速与非有限数都没有调度意义），实际: {speed}",
        context={"speed": speed, "request_hash": request_hash},
    )


def _iter_timed_chunks(
    cassette: Cassette, speed: float
) -> Iterator[tuple[int, float, float, str]]:
    """按 ``speed`` 调度产出 chunk，同时给出每片的计划与实际到达时刻。

    调度采用**绝对时刻对齐**而非「逐片 sleep 间隔」：以开始时刻为基准算出每片的
    目标时刻，每片只 sleep「目标时刻 − 当前时刻」的差额。这样某一片睡过头不会
    把误差累积到后面每一片——逐片 sleep 间隔时第 20 片的实际时刻偏差是前 19 片
    偏差之和，一次 15ms 的调度抖动会被放大成整段流的尾端漂移。

    计划时刻以**首个 chunk 的 arrival_ms 为原点**（与 ``record()`` 的归一化口径
    一致），并先除以 speed：加速回放时计划时刻同样前移，偏差因而衡量的是调度
    精度而不是倍率本身。

    Args:
        cassette: 命中的 cassette；调用方已校验过 speed。
        speed: 回放倍率，已确保为正有限数。

    Yields:
        tuple[int, float, float, str]: ``(chunk 序号, 计划到达毫秒, 实际到达毫秒,
            chunk 文本)``。瞬时回放时只产出一项 ``(0, 0.0, 0.0, 完整响应文本)``：
        不 sleep 意味着等待偏差为零，故 actual 记 0.0 而非混入「构造响应对象的
        耗时」这类与调度无关的量。
    """
    timing = cassette.timing
    if not timing:
        # 无时序可调度：timing 为 None（v1 旧格式 / 非流式响应）或空列表
        # （流式但未采集到 chunk）。两者都走瞬时回放，且文本口径与 play() 完全
        # 一致——直接复用 play()，不给响应构造留第二份实现
        yield (0, 0.0, 0.0, play(cassette).text)
        return

    base_ms = timing[0].arrival_ms
    started = time.perf_counter()
    first_at: float | None = None
    for index, chunk in enumerate(timing):
        expected_ms = (chunk.arrival_ms - base_ms) / speed
        # 剩余等待为负（首片、或录到乱序到达的负间隔）时直接跳过：绝不把负数或
        # NaN 交给 time.sleep；小到低于定时器粒度的等待同样跳过（见 _MIN_SLEEP_SECONDS）
        remaining = started + expected_ms / 1000 - time.perf_counter()
        if remaining >= _MIN_SLEEP_SECONDS:
            time.sleep(remaining)
        now = time.perf_counter()
        if first_at is None:
            first_at = now
        yield (
            index,
            round(expected_ms, _DEVIATION_PRECISION),
            round((now - first_at) * 1000, _DEVIATION_PRECISION),
            chunk.text,
        )


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


def _build_timing(chunks: list[tuple[float, str]]) -> list[ChunkTiming]:
    """把「(到达时刻毫秒, 文本)」列表转换为可落盘的 ChunkTiming 列表。

    两点归一化约定：

    1. **以第一个 chunk 为原点**。录制方给的是绝对到达时刻，但录制的绝对时刻
        取决于发起机器的时钟与运行时刻，既不可复现也对回放无意义；调用方真正
        需要的是「chunk 之间隔多久」。归一化后第一个 chunk 恒为 0.0。
    2. **保序即事实**。chunk 的先后顺序由列表顺序决定（``index`` 即位置），
        不按 ``arrival_ms`` 排序：录制方给出的顺序才是服务端真实下发的顺序，
        贸然排序会掩盖真实的乱序到达。允许 ``arrival_ms`` 为负，它表达的是
        「该 chunk 比第一个 chunk 还早到达」这一可观测事实。

    Args:
        chunks: 流式响应的 chunk 列表，每个元素为 (到达时刻毫秒, 文本)；
            可为空列表（流式响应但未采集到 chunk）。

    Returns:
        list[ChunkTiming]: 按输入顺序编号的时序记录列表。
    """
    first_arrival = chunks[0][0] if chunks else 0.0
    return [
        ChunkTiming(
            index=index,
            arrival_ms=round(arrival - first_arrival, _ARRIVAL_PRECISION),
            text=text,
        )
        for index, (arrival, text) in enumerate(chunks)
    ]


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


def _check_format_version(data: Any, path: Path) -> None:
    """校验 cassette 声明的格式版本不高于当前代码支持的上限。

    检查点必须在 pydantic 契约校验**之前**：Cassette 是 extra="forbid"，
    更高版本 cassette 携带的未知字段（未来版本新增的字段）会先让
    ``model_validate`` 失败。若把版本检查放在校验之后，这条路径永远走不到，
    错误会退化成「字段不符合契约」——恰好把「旧代码读新文件」说成「文件损坏」，
    排障方向从第一步就错。

    缺字段（旧 cassette）或非整数一律放行：向后兼容优先，畸形值交由契约层
    统一报「文件损坏」。

    Args:
        data: 已解析的 JSON 值。
        path: cassette 文件路径（用于错误上下文）。

    Returns:
        None: 版本可支持时正常返回。

    Raises:
        ReplayError: 声明的 format_version 高于 ``CURRENT_FORMAT_VERSION`` 时抛出。
    """
    if not isinstance(data, dict):
        return
    version = data.get("format_version")
    if not isinstance(version, int):
        return
    if version > CURRENT_FORMAT_VERSION:
        raise ReplayError(
            message=(
                f"cassette 格式版本过新（v{version}），"
                f"当前代码支持 v{CURRENT_FORMAT_VERSION}，请升级 aquamind 后重试"
            ),
            context={
                "cassette": str(path),
                "cassette_version": version,
                "supported_version": CURRENT_FORMAT_VERSION,
            },
        )


def _load_cassette(path: Path) -> Cassette:
    """从磁盘加载 cassette 并完成契约校验。

    Args:
        path: cassette 文件路径。

    Returns:
        Cassette: 通过契约校验的 cassette。

    Raises:
        ReplayError: 文件不可读、非 UTF-8、JSON 解析失败、声明的格式版本过新，
            或字段不符合契约时抛出。
            四类失败都在此包装成 ReplayError：调用方只需 except ReplayError，
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
    # 版本闸口必须在契约校验之前：extra="forbid" 会先因新字段把 model_validate
    # 打挂，若把检查放在校验之后，这条路径永远走不到
    _check_format_version(data, path)
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
