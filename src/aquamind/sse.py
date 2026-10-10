"""SSE（Server-Sent Events）流解析器：LLM 流式响应的归一化解析层。

OpenAI 兼容端点的流式响应采用 SSE 协议：每个事件以空行（\\n\\n）分隔，
事件内每行格式为 ``field: value``，其中 ``data`` 字段承载 JSON 载荷，
``data: [DONE]`` 表示流正常结束。

本模块解决三个核心问题：
1. **分片边界处理**：TCP 流可能在任意位置切包，一个 SSE 事件可能被拆成
   多个 ``aiter_bytes()`` 分片；解析器必须维护内部缓冲，在事件完整时才产出。
2. **协议归一**：兼容 ``\\n`` / ``\\r\\n`` 两种行尾、注释行（``: ping``）、
   多 ``data`` 行拼接、``event`` / ``id`` / ``retry`` 字段。
3. **错误识别**：流中可能混入错误事件（``data: {"error": {...}}``），
   解析器在聚合阶段识别并抛 ``AdapterError``，避免调用方拿到半截文本。

使用方式（异步迭代）::

    async with client.stream("POST", url, json=payload) as resp:
        async for delta in iter_stream_deltas(resp):
            print(delta, end="")

或一次性聚合::

    content = await collect_stream_content(resp)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from .exceptions import AdapterError

# SSE 协议中表示流正常结束的标记，OpenAI / vLLM / Ollama 均采用此约定
_SSE_DONE_MARKER = "[DONE]"
# 单个 data 字段值的最大长度（字符），防止恶意端点构造超长行导致内存耗尽
# 正常 LLM 流式响应的单个 chunk JSON 通常 < 4KB，1MB 留有充足余量
_MAX_DATA_LENGTH = 1_000_000
# 内部缓冲的最大字节数，防止端点不发空行导致缓冲无限增长
_MAX_BUFFER_SIZE = 2_000_000


class SSEEvent:
    """解析后的单个 SSE 事件。

    Attributes:
        data: ``data`` 字段的原始文本（未 JSON 解析）；多 data 行时以 ``\\n`` 拼接。
            若事件无 data 字段（如纯注释事件），则为 None。
        event_type: ``event`` 字段值；未指定时为 None（SSE 规范中默认事件名为 "message"，
            但 OpenAI 流式响应几乎不使用 event 字段，保留 None 更贴近实际）。
        event_id: ``id`` 字段值；用于 Last-Event-ID 重连，本阶段不消费但保留。
        retry: ``retry`` 字段值（毫秒）；端点建议的重连间隔，本阶段不消费。
        raw: 事件的原始文本（含所有行），用于调试与错误上下文。
    """

    __slots__ = ("data", "event_id", "event_type", "raw", "retry")

    def __init__(
        self,
        data: str | None = None,
        event_type: str | None = None,
        event_id: str | None = None,
        retry: int | None = None,
        raw: str = "",
    ) -> None:
        """初始化 SSE 事件。

        Args:
            data: data 字段原始文本，无 data 时为 None。
            event_type: event 字段值，未指定为 None。
            event_id: id 字段值。
            retry: retry 字段值（毫秒），非数字时忽略。
            raw: 事件原始文本。
        """
        self.data = data
        self.event_type = event_type
        self.event_id = event_id
        self.retry = retry
        self.raw = raw

    @property
    def is_done(self) -> bool:
        """判断是否为 ``[DONE]`` 终止事件。

        OpenAI 流式协议中，服务端发送 ``data: [DONE]`` 表示所有 chunk 已发送完毕。
        解析器遇到此事件后应停止迭代，不再产出后续内容。

        Returns:
            bool: data 字段去除首尾空白后等于 ``[DONE]`` 时返回 True。
        """
        return self.data is not None and self.data.strip() == _SSE_DONE_MARKER

    def parse_json(self) -> Any:
        """将 data 字段解析为 JSON 对象。

        Returns:
            Any: 解析后的 JSON 值（通常是 dict）。

        Raises:
            AdapterError: data 为 None 或不是合法 JSON 时抛出。
                context 中携带原始 data 片段（截断到 500 字符），便于排障。
        """
        # data 为 None 说明这是一个纯注释或纯 event/id/retry 事件，没有可解析的载荷
        if self.data is None:
            raise AdapterError(
                message="SSE 事件无 data 字段，无法解析 JSON",
                context={"event_type": self.event_type, "raw": self.raw[:500]},
            )
        # 延迟导入 json：本模块被 import 时不需要 json，仅在实际解析时才加载，
        # 保持包导入零副作用的设计原则（与 __init__.py 的双写版本号同一理念）
        import json

        try:
            return json.loads(self.data)
        except ValueError as exc:
            # JSON 解析失败时，原始 data 可能包含端点返回的 HTML 错误页或乱码，
            # 截断到 500 字符放入 context（不放入 message，避免 traceback 泄露）
            raise AdapterError(
                message=f"SSE 事件 data 不是合法 JSON: {type(exc).__name__}",
                context={"data_preview": self.data[:500], "error_type": type(exc).__name__},
            ) from exc


def parse_sse_events(text: str) -> list[SSEEvent]:
    """从一段完整文本中解析所有 SSE 事件（同步，用于测试与 cassette 回放）。

    文本必须包含完整的事件（以空行结尾），不处理分片边界。
    生产环境的流式解析应使用 ``SSEStreamParser``。

    Args:
        text: 完整的 SSE 文本，事件之间以空行分隔。

    Returns:
        list[SSEEvent]: 解析出的事件列表（不含纯注释事件，但包含 [DONE] 事件）。

    Raises:
        AdapterError: 单行 data 超过 _MAX_DATA_LENGTH 时抛出（防内存耗尽）。
    """
    events: list[SSEEvent] = []
    # 按空行分割事件：兼容 \\n\\n 和 \\r\\n\\r\\n 以及混合行尾
    # 先统一替换 \\r\\n 为 \\n，再按 \\n\\n 分割，这样能处理所有行尾组合
    normalized = text.replace("\r\n", "\n")
    # 按空行分割；注意最后一个事件后可能没有空行，split 会把它也包含进来
    raw_events = normalized.split("\n\n")

    for raw_event in raw_events:
        # 跳过空字符串（文本末尾的空行导致）和纯注释事件（整段以 : 开头）
        if not raw_event.strip():
            continue
        event = _parse_single_event(raw_event)
        # 纯注释事件（没有任何 data/event/id/retry 字段）不产出
        if (
            event.data is None
            and event.event_type is None
            and event.event_id is None
            and event.retry is None
        ):
            continue
        events.append(event)

    return events


def _parse_single_event(raw_event: str) -> SSEEvent:
    """解析单个 SSE 事件的原始文本（不含尾部空行）。

    SSE 事件解析规则（W3C Server-Sent Events 规范）：
    1. 按行分割，每行格式为 ``field: value`` 或 ``field:value``（冒号后空格可选）
    2. 以 ``:`` 开头的行是注释，忽略
    3. 没有冒号的行，整行作为 field 名，value 为空串
    4. 多个 ``data`` 行的 value 以 ``\\n`` 拼接
    5. ``event`` / ``id`` 字段取最后一个值（后出现的覆盖先出现的）
    6. ``retry`` 字段必须是数字，否则忽略

    Args:
        raw_event: 单个事件的原始文本（多行，不含尾部空行）。

    Returns:
        SSEEvent: 解析后的事件对象。

    Raises:
        AdapterError: data 行长度超过 _MAX_DATA_LENGTH 时抛出。
    """
    data_lines: list[str] = []
    event_type: str | None = None
    event_id: str | None = None
    retry: int | None = None

    for line in raw_event.split("\n"):
        # 空行不应出现在单个事件内部（已被 split("\\n\\n") 分割），但防御性跳过
        if not line:
            continue
        # 注释行：以冒号开头，整行忽略（SSE 规范用于保活心跳 ``: ping``）
        if line.startswith(":"):
            continue

        # 解析 field: value 格式
        colon_index = line.find(":")
        if colon_index == -1:
            # 没有冒号的行：整行作为 field 名，value 为空串（SSE 规范）
            field = line
            value = ""
        else:
            field = line[:colon_index]
            value = line[colon_index + 1:]
            # 冒号后如果有一个前导空格，按规范移除（仅移除一个，不是全部）
            value = value.removeprefix(" ")

        # 按字段名分发处理
        if field == "data":
            # 防内存耗尽：单个 data 行超过上限直接报错，而非静默截断
            if len(value) > _MAX_DATA_LENGTH:
                raise AdapterError(
                    message=f"SSE data 行超过最大长度限制 ({_MAX_DATA_LENGTH} 字符)",
                    context={"actual_length": len(value)},
                )
            data_lines.append(value)
        elif field == "event":
            # event 字段取最后出现的值（后覆盖前）
            event_type = value
        elif field == "id":
            # id 字段取最后出现的值；包含空字符 \\0 的 id 按规范应忽略，这里不做过滤
            # 因为 OpenAI 流式响应几乎不使用 id 字段，保持简单
            event_id = value
        elif field == "retry":
            # retry 必须是非负整数；解析失败则忽略该行（不覆盖之前的有效值）
            try:
                retry = int(value)
                if retry < 0:
                    retry = None
            except ValueError:
                pass
        # 其他字段（如 OpenAI 扩展字段）按规范忽略

    # 多个 data 行以 \\n 拼接；如果没有 data 行则 data 为 None
    data = "\n".join(data_lines) if data_lines else None

    return SSEEvent(
        data=data,
        event_type=event_type,
        event_id=event_id,
        retry=retry,
        raw=raw_event,
    )


class SSEStreamParser:
    """异步 SSE 流解析器：处理 TCP 分片边界，产出完整事件。

    生产环境中，``httpx.Response.aiter_bytes()`` 返回的字节分片可能在任意位置
    切断（包括在一个 SSE 事件的中间、在一行的中间、甚至在一个多字节 UTF-8 字符
    的中间）。解析器维护内部字节缓冲，每次喂入新分片后，只产出已经完整的事件，
    不完整的尾部留到下一次喂入。

    使用方式::

        parser = SSEStreamParser()
        async for chunk in response.aiter_bytes():
            for event in parser.feed(chunk):
                if event.is_done:
                    break
                process(event)
        # 流结束后处理缓冲中可能残留的最后一个事件（端点没发尾部空行时）
        for event in parser.finish():
            process(event)
    """

    def __init__(self) -> None:
        """初始化解析器，创建空的内部字节缓冲。"""
        # 内部缓冲：累积尚未形成完整事件的字节
        # 使用 bytearray 而非 bytes，因为 feed 时需要频繁追加，bytearray 是可变的
        self._buffer: bytearray = bytearray()

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        """喂入一个字节分片，返回其中包含的完整事件。

        分片可能在任意位置切断，解析器会把不完整的尾部留在缓冲中，
        等下一次 feed 时拼接后再解析。

        Args:
            chunk: 从 TCP 流读取的字节分片。

        Returns:
            list[SSEEvent]: 本分片中完整的事件列表（可能为空）。

        Raises:
            AdapterError: 缓冲超过 _MAX_BUFFER_SIZE（端点不发空行）时抛出。
        """
        # 把新分片追加到缓冲
        self._buffer.extend(chunk)

        # 防内存耗尽：缓冲超过上限说明端点一直不发空行（可能是错误响应或攻击），
        # 直接报错而非继续累积
        if len(self._buffer) > _MAX_BUFFER_SIZE:
            raise AdapterError(
                message=f"SSE 缓冲超过最大限制 ({_MAX_BUFFER_SIZE} 字节)，端点未发送事件分隔符",
                context={"buffer_size": len(self._buffer)},
            )

        # 尝试从缓冲中提取完整事件
        return self._extract_events()

    def finish(self) -> list[SSEEvent]:
        """流结束时调用，处理缓冲中可能残留的最后一个事件。

        有些端点在发送最后一个事件后不发尾部空行（虽然不符合 SSE 规范，
        但实际中 vLLM 等端点偶尔出现）。此时缓冲中会残留一个完整但没有
        尾部空行的事件，finish() 会把它解析出来。

        Returns:
            list[SSEEvent]: 缓冲中残留的事件列表（可能为空）。
        """
        # 如果缓冲为空，直接返回空列表
        if not self._buffer:
            return []

        # 把缓冲中剩余的内容当作一个完整事件处理
        # 先解码为文本（UTF-8），然后解析
        try:
            text = self._buffer.decode("utf-8")
        except UnicodeDecodeError as exc:
            # 流结束时缓冲中仍有不完整的多字节字符，说明流被异常截断
            raise AdapterError(
                message=f"SSE 流异常截断：缓冲末尾不是完整的 UTF-8 序列: {type(exc).__name__}",
                context={"buffer_size": len(self._buffer), "error_type": type(exc).__name__},
            ) from exc

        # 清空缓冲
        self._buffer.clear()

        # 解析残留文本中的事件（可能包含一个或多个）
        return parse_sse_events(text)

    def _extract_events(self) -> list[SSEEvent]:
        """从当前缓冲中提取所有完整事件，不完整的尾部留在缓冲中。

        核心逻辑：
        1. 在缓冲中查找事件分隔符（\\n\\n 或 \\r\\n\\r\\n）
        2. 把分隔符之前的内容解码为文本，解析为事件
        3. 把分隔符之后的内容留在缓冲中
        4. 重复直到缓冲中没有完整分隔符

        Returns:
            list[SSEEvent]: 提取出的完整事件列表。
        """
        events: list[SSEEvent] = []

        while True:
            # 查找事件分隔符：优先查找 \\n\\n（最常见），同时考虑 \\r\\n\\r\\n
            # 策略：先找 \\n\\n，如果找到的位置前面是 \\r（即 \\r\\n\\n），
            # 说明实际是 \\r\\n\\r\\n 的一部分，需要调整分隔符长度
            sep_pos = self._buffer.find(b"\n\n")

            if sep_pos == -1:
                # 缓冲中没有完整分隔符，停止提取，剩余内容留到下一次 feed
                break

            # 确定分隔符的实际长度：检查 sep_pos 位置是否是 \\r\\n\\n 的一部分
            # 即 sep_pos-1 是否是 \\r（此时分隔符是 \\r\\n\\n，长度 3）
            # 或者更准确地说，事件文本以 \\r\\n 结尾，然后又一个 \\r\\n 作为空行
            # 简化处理：如果 sep_pos > 0 且 buffer[sep_pos-1] == ord('\\r')，
            # 说明事件行尾是 \\r\\n，分隔符从 \\r 开始算（\\r\\n\\n）
            if sep_pos > 0 and self._buffer[sep_pos - 1] == ord("\r"):
                # 事件文本包含末尾的 \\r，分隔符是 \\r\\n\\n（从 \\r 开始）
                event_end = sep_pos - 1  # 事件文本的结束位置（不含 \\r）
                separator_length = 3  # \\r\\n\\n
            else:
                # 事件行尾是 \\n，分隔符就是 \\n\\n
                event_end = sep_pos  # 事件文本的结束位置（不含第一个 \\n）
                separator_length = 2  # \\n\\n

            # 提取事件文本（分隔符之前的部分）
            event_bytes = bytes(self._buffer[:event_end])

            # 从缓冲中移除事件文本和分隔符
            del self._buffer[: event_end + separator_length]

            # 解码为 UTF-8 文本
            try:
                event_text = event_bytes.decode("utf-8")
            except UnicodeDecodeError:
                # 事件文本不是合法 UTF-8，跳过此事件（不抛出，因为后续事件可能正常）
                # 这种情况在实际中极少见（TCP 流不会在字符中间切出完整事件），
                # 但防御性处理比崩溃好
                continue

            # 解析单个事件
            event = _parse_single_event(event_text)

            # 纯注释事件不产出（与 parse_sse_events 保持一致）
            if (
                event.data is None
                and event.event_type is None
                and event.event_id is None
                and event.retry is None
            ):
                continue

            events.append(event)

        return events


async def iter_stream_deltas(response: httpx.Response) -> AsyncIterator[str]:
    """从 httpx 流式响应中异步迭代每个 chunk 的 delta 文本。

    逐个产出 ``choices[0].delta.content`` 的文本片段，调用方可以实时拼接
    或逐段处理。遇到 ``[DONE]`` 事件时正常结束迭代。

    Args:
        response: 已发起的 httpx 流式响应（必须是 ``client.stream()`` 上下文内的响应）。

    Yields:
        str: 每个 chunk 的 delta 文本（可能是空串，当 chunk 只有 role/finish_reason 时）。

    Raises:
        AdapterError: HTTP 非 2xx、SSE 解析失败、流中出现错误事件、delta 结构异常时抛出。
    """
    # 先检查 HTTP 状态码：流式响应的状态码在响应头返回时就已确定，
    # 不需要等 body 读完。非 2xx 时，body 可能是 JSON 错误或 HTML 错误页，
    # 统一包装为 AdapterError
    if not 200 <= response.status_code < 300:
        # 读取错误响应体（流式响应也需要读完 body 才能释放连接）
        error_body = await response.aread()
        try:
            error_text = error_body.decode("utf-8", errors="replace")
        except (AttributeError, TypeError):
            # 响应体不是 bytes 形态（自定义 transport / 桩实现）时没有 decode 方法；
            # errors="replace" 已保证合法字节不会解码失败，故此处只兜住形态异常
            error_text = f"<{len(error_body)} bytes>"
        raise AdapterError(
            message=f"流式请求返回 HTTP {response.status_code}",
            context={
                "status_code": response.status_code,
                "response_body": error_text[:500],
            },
        )

    # 创建 SSE 流解析器，处理 TCP 分片边界
    parser = SSEStreamParser()

    # 异步迭代响应体的字节分片
    async for chunk in response.aiter_bytes():
        # 喂入分片，获取完整事件
        events = parser.feed(chunk)

        for event in events:
            # 遇到 [DONE] 事件，正常结束迭代
            if event.is_done:
                return

            # 解析事件的 JSON 载荷
            payload = event.parse_json()

            # 从 payload 中提取 delta content
            delta = _extract_delta_content(payload)
            if delta:
                yield delta

    # 流正常结束（端点关闭连接）后，处理缓冲中可能残留的最后一个事件
    # （有些端点在 [DONE] 后直接关闭连接，不发尾部空行）
    for event in parser.finish():
        if event.is_done:
            continue
        payload = event.parse_json()
        delta = _extract_delta_content(payload)
        if delta:
            yield delta


async def collect_stream_content(response: httpx.Response) -> str:
    """从 httpx 流式响应中聚合所有 delta，返回完整文本。

    这是 ``iter_stream_deltas`` 的便捷封装：迭代所有 delta 并拼接。
    适用于不需要实时处理、只需要最终文本的场景。

    Args:
        response: 已发起的 httpx 流式响应。

    Returns:
        str: 所有 chunk 的 delta 拼接后的完整文本。

    Raises:
        AdapterError: 同 ``iter_stream_deltas``。
    """
    # 使用列表收集所有 delta，最后用 join 拼接
    # 列表 append + 最终 join 比反复字符串拼接（+=）效率高得多，
    # 因为字符串是不可变的，每次 += 都会创建新对象
    deltas: list[str] = []
    async for delta in iter_stream_deltas(response):
        deltas.append(delta)
    return "".join(deltas)


def _extract_delta_content(payload: Any) -> str:
    """从 OpenAI 流式 chunk JSON 中提取 ``choices[0].delta.content``。

    同时做防御性结构校验和错误事件识别：
    - 如果 payload 包含 ``error`` 字段，说明这是一个错误事件，抛 AdapterError
    - 如果 choices 缺失/为空/结构异常，抛 AdapterError
    - 如果 delta 缺失或 content 不是字符串，返回空串（正常 chunk 可能只有
      role 或 finish_reason，没有 content）

    Args:
        payload: 已解析的 chunk JSON（通常是 dict）。

    Returns:
        str: delta content 文本；无 content 时返回空串。

    Raises:
        AdapterError: payload 是错误事件或结构不符合预期时抛出。
    """
    # 错误事件识别：OpenAI 错误响应的格式是 {"error": {"message": "...", "type": "..."}}
    # 流式响应中如果发生错误，端点会发送一个包含 error 字段的 data 事件
    if isinstance(payload, dict) and "error" in payload:
        error_obj = payload["error"]
        # error 可能是字符串（少数端点）或对象（标准格式）
        if isinstance(error_obj, dict):
            error_message = error_obj.get("message", str(error_obj))
            error_type = error_obj.get("type")
        else:
            error_message = str(error_obj)
            error_type = None
        raise AdapterError(
            message=f"流式响应中收到错误事件: {error_message}",
            context={
                "error_type": error_type,
                "error": error_obj if isinstance(error_obj, dict) else None,
            },
        )

    # 防御性校验：payload 必须是 dict
    if not isinstance(payload, dict):
        raise AdapterError(
            message="流式 chunk 顶层不是 JSON 对象",
            context={"actual_type": type(payload).__name__},
        )

    # 提取 choices
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        # choices 缺失或为空：某些端点的中间 chunk 可能不带 choices（如纯 usage 事件），
        # 这种情况返回空串而非报错，因为流中可能夹杂 usage 统计事件
        return ""

    first = choices[0]
    if not isinstance(first, dict):
        raise AdapterError(
            message="流式 chunk choices[0] 不是对象",
            context={"actual_type": type(first).__name__},
        )

    # 提取 delta（流式响应中是 delta，非流式是 message）
    delta = first.get("delta")
    if not isinstance(delta, dict):
        # delta 缺失可能是因为这是一个只有 finish_reason 的最终 chunk，
        # 或者端点使用了非标准格式。返回空串。
        return ""

    # 提取 content
    content = delta.get("content")
    if content is None:
        # content 为 None 是正常的：第一个 chunk 通常只有 role，没有 content；
        # 中间 chunk 可能只有 content；最后一个 chunk 可能只有 finish_reason
        return ""
    if not isinstance(content, str):
        # content 不是字符串（如结构化输出中的 tool_calls），本阶段只处理文本，
        # 返回空串而非报错
        return ""

    return content
