# AquaMind 适配器示例（M1-D04 / M1-D05）

本目录是 AquaMind `adapters` 模块的最小可运行示例，展示**后端如何被归一成同一个契约**：

1. **`CallableAdapter`**——把一个本地同步函数包装成统一异步适配器（CI 零 key 的主力）；
2. **`OpenAIAdapter`**——展示 `base_url` / `api_key` / `model` / `timeout` 的配置方式，
  并通过注入 `httpx.MockTransport` 在零网络的前提下跑通完整链路；
3. **错误分类**——429 / 401 / 5xx / 超时在适配器层一律收敛为 `AdapterError`；
4. **并发语义**——同步 SUT 被 `asyncio.to_thread` 卸载到线程池，并发调用不被串行化。

## 目录内容

| 文件 | 说明 |
| --- | --- |
| `adapter_demo.py` | 全部演示逻辑；被测对象与 mock 端点都在脚本内构造 |
| `README.md` | 本文件 |

## 运行命令

在**仓库根目录**执行：

```powershell
.venv\Scripts\python.exe examples/adapter_demo/adapter_demo.py
```

## 注意事项

- **本示例不触真实 API，不需要任何 API 密钥**（CI 零 key）。OpenAI 适配器一侧通过注入
 `httpx.MockTransport` 替换传输层，**真实执行**构造请求 → 发请求 → 解析响应 → 归一这条链路，
 只是在最后一跳不落网络。换成真实端点时只需去掉 `transport` 参数，其余调用代码不变。
- 脚本里的 `_PLACEHOLDER_KEY` 是**占位串**，不是真实凭据。

## 一条容易踩的坑：错误消息安全 ≠ context 安全

`AquaMindError` 的 `__str__` / `__repr__` **只渲染 `context` 的键名**，所以直接
`print(exc)` 或把异常丢进 logging 是安全的：

```text
str = 'OpenAI API 返回 HTTP 500（响应体 17 字符，详见 context.response_body）
    (context keys: [adapter, url, status_code, response_body])'
```

但 **`exc.context` 本身带着取值**（上面的 `response_body` 就是端点回显的正文）。
把 `exc.context` 整个打进日志——例如 `logging.error("失败: %s", exc.context)`——
等于绕过这层保护，端点回显的用户 prompt 会随异常进入日志与报告。

**正确做法**：要某个键就按需取（`exc.context["status_code"]`），不要整包输出。
这层保护的作用是「让你默认打错不了」，不是「让你可以随便打」。
