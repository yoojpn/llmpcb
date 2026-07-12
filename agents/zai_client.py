from __future__ import annotations
import os
import json
import time
import concurrent.futures
from typing import Optional

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


MODEL_MAIN = "glm-4.7-flash"  # free tier; concurrency=1, throttled to ~1% above 8K context
MODEL_INTERVIEWER = "glm-4.7-flash"
BASE_URL = "https://api.z.ai/api/paas/v4"


class LLMPCBZaiClient:
    def __init__(self, api_key: Optional[str] = None):
        key = api_key or os.environ.get("ZAI_API_KEY")
        if not key:
            raise RuntimeError("ZAI_API_KEY is not set")
        if OpenAI is None:
            raise RuntimeError("openai package not installed")
        self._client = OpenAI(api_key=key, base_url=BASE_URL)

    def call(self, model: str | list[str], system_prompt: str, messages: list[dict],
              tools: Optional[list[dict]] = None, temperature: float = 0.2,
              max_retries: int = 3) -> dict:
        full_messages = [{"role": "system", "content": system_prompt}]
        for m in messages:
            role = m["role"]
            if role not in ("user", "assistant", "system"):
                role = "user"
            full_messages.append({"role": role, "content": m["content"] or ""})

        model_candidates = model if isinstance(model, list) else [model]
        multi_candidate = len(model_candidates) > 1
        last_error = None
        resp = None

        def _do_request(candidate_model):
            kwargs = dict(model=candidate_model, messages=full_messages, temperature=temperature)
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            return self._client.chat.completions.create(**kwargs)

        for candidate_model in model_candidates:
            candidate_retries = max_retries if not multi_candidate else 1
            for attempt in range(candidate_retries):
                try:
                    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    future = executor.submit(_do_request, candidate_model)
                    try:
                        resp = future.result(timeout=30)
                    finally:
                        executor.shutdown(wait=False)
                    last_error = None
                    break
                except concurrent.futures.TimeoutError:
                    last_error = "LLMPCB_TIMEOUT: request exceeded 30s wall-clock limit"
                    if attempt < candidate_retries - 1:
                        continue
                    break
                except Exception as e:
                    last_error = str(e)
                    is_quota_exhausted = "429" in last_error or "concurrency" in last_error.lower()
                    if attempt < candidate_retries - 1 and not is_quota_exhausted:
                        time.sleep(3 * (attempt + 1))
                        continue
                    break
            if resp is not None and last_error is None:
                break

        if resp is None or last_error is not None:
            return {"content": None, "tool_calls": [], "error": last_error}

        choice = resp.choices[0]
        return {
            "content": choice.message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                }
                for tc in (choice.message.tool_calls or [])
            ],
            "finish_reason": choice.finish_reason,
            "raw": resp,
        }

    def call_designer(self, system_prompt: str, messages: list[dict], tools: list[dict],
                       phase: str = "normal") -> dict:
        return self.call(MODEL_MAIN, system_prompt, messages, tools=tools, temperature=0.1)

    def call_critic(self, system_prompt: str, messages: list[dict], tools: list[dict],
                     phase: str = "normal") -> dict:
        return self.call(MODEL_MAIN, system_prompt, messages, tools=tools, temperature=0.0)

    def call_interviewer(self, system_prompt: str, messages: list[dict]) -> dict:
        return self.call(MODEL_INTERVIEWER, system_prompt, messages, temperature=0.4)


if __name__ == "__main__":
    client = LLMPCBZaiClient()
    result = client.call_interviewer(
        system_prompt="You are a concise assistant.",
        messages=[{"role": "user", "content": "Hello, this is a test. Reply briefly."}],
    )
    print(result["content"])
