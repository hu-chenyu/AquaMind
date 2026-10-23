"""AquaMind Judge 适配器最小示例（judge_demo）。

本示例演示 D25 交付的三件事：

1. **异族校验通过**：被测模型 ``gpt-4o`` + 裁判模型 ``claude-3-opus`` 构造成功，
   随后对一条输出做结构化判定，打印 ``JudgeOutput`` 的三个字段；
2. **同族被拒**：被测与裁判同为 ``gpt-*`` 时，构造期即抛 ``JudgeError``——同族自评
   会让裁判与被测方共享训练偏好与失效模式，判定失去独立性；
3. **不可判不等于不通过**：裁判返回自由文本（不是 JSON）时同样抛 ``JudgeError``，
   而不是被猜成「通过」或「不通过」。

裁判响应由 ``CallableAdapter`` 本地注入，**不发起任何网络请求**、不需要任何 API 密钥
（CI 零 key）；输出不含时间戳，因此每次运行逐字相同。

运行方式（在**仓库根目录**执行）::

  .venv\\Scripts\\python.exe examples/judge_demo/demo.py
"""

from __future__ import annotations

import asyncio

from aquamind.adapters import CallableAdapter
from aquamind.judge import JudgeAdapter, JudgeError

# 演示用的模型对：异族（gpt-* 与 claude-* 属不同厂商前缀）
_SUT_MODEL = "gpt-4o"
_JUDGE_MODEL = "claude-3-opus"

# 模拟裁判的结构化返回：三字段齐全，理由说明分数为什么不是满分
_JUDGE_REPLY = '{"passed": true, "score": 0.82, "reason": "结论正确，但推导缺少中间步骤"}'


def _mock_judge(messages: list[dict[str, str]]) -> str:
  """模拟裁判模型：忽略输入，恒定返回预置的结构化 JSON。

  Args:
    messages: 裁判端收到的 OpenAI 格式消息（真实裁判会据此作答，mock 不用）。

  Returns:
    str: 固定的结构化 JSON 文本。
  """
  return _JUDGE_REPLY


async def main() -> int:
  """依次演示异族放行、结构化判定、同族拒绝与不可判出口。

  Returns:
    int: 恒为 0；演示过程中的异常路径都由示例自己捕获并打印。
  """
  print("1. 异族校验：被测 gpt-4o / 裁判 claude-3-opus")
  judge_adapter = JudgeAdapter(CallableAdapter(_mock_judge), _SUT_MODEL, _JUDGE_MODEL)
  print(f"  构造成功   : sut_model={judge_adapter.sut_model} judge_model={judge_adapter.judge_model}")

  print("2. 结构化判定：裁判返回 JSON → JudgeOutput")
  output = await judge_adapter.aevaluate("2+2=? 并说明理由", "4")
  print(f"  passed     : {output.passed}")
  print(f"  score      : {output.score}")
  print(f"  reason     : {output.reason}")

  print("3. 同族被拒：被测 gpt-4o / 裁判 gpt-4o-mini")
  try:
    JudgeAdapter(CallableAdapter(_mock_judge), "gpt-4o", "gpt-4o-mini")
  except JudgeError as exc:
    print(f"  构造被拒   : {exc.message}")

  print("4. 不可判出口：裁判返回自由文本，不猜结论")
  free_text_judge = JudgeAdapter(
    CallableAdapter(lambda messages: "这个回答不错，给 0.9 分"),
    _SUT_MODEL,
    "qwen-max",
  )
  try:
    await free_text_judge.aevaluate("2+2=? 并说明理由", "4")
  except JudgeError as exc:
    print(f"  判为不可判 : {exc.message}")
  return 0


if __name__ == "__main__":
  raise SystemExit(asyncio.run(main()))
