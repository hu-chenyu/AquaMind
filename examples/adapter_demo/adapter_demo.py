"""AquaMind 适配器最小示例（adapter_demo）。

本示例演示三件事：

1. ``CallableAdapter``：把一个本地同步函数包装成统一异步适配器契约（CI 零 key 的主力）；
2. ``OpenAIAdapter``：展示 base_url / model / api_key 的配置方式，并通过注入
   ``httpx.MockTransport`` 在**不发起任何网络请求**的前提下跑通完整的适配器链路；
3. **错误分类**：429 / 5xx / 超时 / AUTH 在适配器层的统一表现——
   全部收敛为 ``AdapterError``，且错误消息只渲染 ``context`` 的键名、不渲染取值。

为什么 OpenAI 适配器用 MockTransport 而不是本地起服务：适配器的价值在于"把后端差异
归一成 AdapterResponse"，这段链路（构造请求 → 发请求 → 解析响应 → 归一）完全可以在
传输层被替换的情况下真实执行。这样既展示了真实代码路径，又保证零网络依赖。

本示例不发起任何网络请求，不需要任何 API 密钥（CI 零 key）。
脚本内出现的一切密钥形态都是**占位串**，不是真实凭据。

运行方式（在**仓库根目录**执行）::

    .venv\\Scripts\\python.exe examples/adapter_demo/adapter_demo.py
"""

from __future__ import annotations

import asyncio
import json

import httpx

from aquamind.adapters import CallableAdapter, OpenAIAdapter
from aquamind.exceptions import AdapterError

# 占位密钥：仅用于演示"配置了 api_key 时请求会带上 Authorization 头"，
# 不是真实凭据，固定字符串以保证示例可离线重复运行
_PLACEHOLDER_KEY = "not-a-real-key"


def _echo(messages: list[dict[str, str]]) -> str:
    """一个最简单的本地被测函数：把收到的最后一条消息原样回显。

    Args:
        messages: OpenAI 格式的消息列表。

    Returns:
        str: 最后一条消息的内容（即"模型输出"）。
    """
    return messages[-1]["content"]


def _boom(messages: list[dict[str, str]]) -> str:
    """一个会失败的本地被测函数，用于演示异常被适配器包装。

    Args:
        messages: OpenAI 格式的消息列表（不使用）。

    Returns:
        str: 永远走不到返回，因为总是先抛异常。

    Raises:
        RuntimeError: 模拟被测后端内部故障。
    """
    raise RuntimeError("被测后端内部故障")


def _mock_openai_transport(content: str) -> httpx.AsyncBaseTransport:
    """构造一个返回 OpenAI 兼容响应的 mock 传输层。

    Args:
        content: 要放进响应体 ``choices[0].message.content`` 的文本。

    Returns:
        httpx.AsyncBaseTransport: 可直接注入 ``OpenAIAdapter`` 的传输层实例。
    """
    payload = {
        "id": "chatcmpl-demo",
        "object": "chat.completion",
        "model": "demo-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        """对任意请求返回预置的成功响应。"""
        del request  # 请求内容与本示例的断言无关，此处显式标记未使用
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def _failing_transport(status_code: int, body: str) -> httpx.AsyncBaseTransport:
    """构造一个返回指定状态码的 mock 传输层。

    Args:
        status_code: 要返回的 HTTP 状态码（如 429 / 401 / 500）。
        body: 响应体文本，模拟端点回显的正文。

    Returns:
        httpx.AsyncBaseTransport: 可直接注入 ``OpenAIAdapter`` 的传输层实例。
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        """返回预置的错误响应。"""
        del request
        return httpx.Response(status_code, text=body)

    return httpx.MockTransport(handler)


async def _demo_callable() -> None:
    """演示 CallableAdapter 的正常调用、异常包装与流式限制。"""
    print("=" * 68)
    print("1. CallableAdapter：本地函数接入统一适配器契约")
    print("=" * 68)
    adapter = CallableAdapter(_echo)
    messages = [
        {"role": "system", "content": "你是一个助手"},
        {"role": "user", "content": "你好，AquaMind"},
    ]
    response = await adapter.acomplete(messages)
    print(f"  content        = {response.content!r}")
    print(f"  raw            = {response.raw!r}")
    print(f"  metadata       = {response.metadata}")
    print("  说明：同步函数被 asyncio.to_thread 卸载到线程池，不阻塞事件循环，")
    print("        因此并发调用不会被串行化（这一点对压测结果的可信度是前提）。")

    print()
    print("  1b. 本地函数抛异常时，统一收敛为 AdapterError：")
    try:
        await CallableAdapter(_boom).acomplete(messages)
    except AdapterError as exc:
        print(f"    AdapterError: {exc}")
        print(f"    context: {exc.context}")
        print(f"    原始异常保留在 __cause__：{type(exc.__cause__).__name__}")

    print()
    print("  1c. 流式在本地函数适配器上暂不支持，显式报错而非静默降级：")
    try:
        await adapter.acomplete(messages, stream=True)
    except AdapterError as exc:
        print(f"    AdapterError: {exc}")


async def _demo_openai_ok() -> None:
    """演示 OpenAIAdapter 的配置方式与响应归一（不触真实 API）。"""
    print()
    print("=" * 68)
    print("2. OpenAIAdapter：配置方式与响应归一（注入 MockTransport，零网络）")
    print("=" * 68)
    adapter = OpenAIAdapter(
        base_url="https://api.example.com/v1",
        api_key=_PLACEHOLDER_KEY,
        model="demo-model",
        transport=_mock_openai_transport("这是 mock 端点返回的模型输出"),
    )
    print("  构造参数：base_url / api_key / model / timeout / transport")
    response = await adapter.acomplete([{"role": "user", "content": "你好"}])
    print(f"  content  = {response.content!r}")
    print(f"  metadata = {response.metadata}")
    print("  说明：换成真实端点时，只需去掉 transport 参数并填入真实 base_url，")
    print("        其余调用代码完全不用改——这正是适配器层要提供的后端无关性。")

    print()
    print("  2b. base_url 非法时在构造期就失败，不推迟到请求期：")
    try:
        OpenAIAdapter(base_url="ftp://not-supported")
    except AdapterError as exc:
        print(f"    AdapterError: {exc}")


async def _demo_error_classification() -> None:
    """演示 429 / 5xx / AUTH / 超时在适配器层的统一表现。"""
    print()
    print("=" * 68)
    print("3. 错误分类：429 / 5xx / AUTH / 超时一律收敛为 AdapterError")
    print("=" * 68)
    cases: list[tuple[str, httpx.AsyncBaseTransport]] = [
        ("429 限流", _failing_transport(429, '{"error":"rate limited"}')),
        ("401 鉴权失败", _failing_transport(401, '{"error":"bad key"}')),
        ("500 端点故障", _failing_transport(500, "upstream exploded")),
    ]
    last_error: AdapterError | None = None
    for label, transport in cases:
        adapter = OpenAIAdapter(
            base_url="https://api.example.com/v1",
            transport=transport,
        )
        try:
            await adapter.acomplete([{"role": "user", "content": "hi"}])
        except AdapterError as exc:
            print(f"  {label:12s} -> AdapterError，context={exc.context}")
            last_error = exc

    print()
    print("  超时（由传输层直接抛出 httpx 异常）：")

    async def timeout_handler(request: httpx.Request) -> httpx.Response:
        """模拟连接超时。"""
        del request
        raise httpx.ConnectTimeout("connection timed out")

    adapter = OpenAIAdapter(
        base_url="https://api.example.com/v1",
        transport=httpx.MockTransport(timeout_handler),
    )
    try:
        await adapter.acomplete([{"role": "user", "content": "hi"}])
    except AdapterError as exc:
        print(f"    超时        -> AdapterError，context={exc.context}")

    print()
    print("  注意上面的 context 里**确实带着 response_body 取值**。安全性来自")
    print("  AquaMindError 的 __str__ / __repr__ 只渲染 context 的**键名**：")
    print("    打印 str(exc)     -> 值不外泄（下面是最后一条 500 错误的实际 str）")
    print(f"      str = {str(last_error)!r}")
    print("    打印 exc.context -> 值可见（上面已打印过，含端点回显正文）")
    print("  所以**不要**把 exc.context 整个打进日志；要按需取键再输出，")
    print("  否则端点回显的用户 prompt 会随异常进入日志与报告。")


async def _demo_concurrency() -> None:
    """演示多个适配器调用并发执行，说明同步 SUT 不被串行化。"""
    print()
    print("=" * 68)
    print("4. 并发：同步 SUT 被卸载到线程池，asyncio.gather 不退化为串行")
    print("=" * 68)
    adapter = CallableAdapter(_echo)
    messages = [[{"role": "user", "content": f"第 {i} 条消息"}] for i in range(1, 6)]
    responses = await asyncio.gather(*(adapter.acomplete(m) for m in messages))
    print(f"  并发完成 {len(responses)} 条，输出顺序：")
    for response in responses:
        print(f"    {response.content}")


async def _main_async() -> None:
    """按顺序跑完适配器的全部演示段落。"""
    await _demo_callable()
    await _demo_openai_ok()
    await _demo_error_classification()
    await _demo_concurrency()
    print()
    print("小结：无论后端是本地函数、OpenAI 兼容端点还是 cassette 回放，上层只依赖")
    print("      BaseAdapter.acomplete 这一条契约，返回类型恒为 AdapterResponse。")
    print()
    print("序列化形态示例（AdapterResponse 是 pydantic 模型，可直接 dump）：")
    adapter = CallableAdapter(_echo)
    response = await adapter.acomplete([{"role": "user", "content": "序列化一下"}])
    print("  " + json.dumps(response.model_dump(), ensure_ascii=False))


def main() -> None:
    """脚本入口。"""
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
