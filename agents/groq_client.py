from __future__ import annotations
import os
import json
import time
import concurrent.futures
from typing import Optional

try:
    from groq import Groq
except ImportError:
    Groq = None


MODEL_MAIN = "openai/gpt-oss-120b"  # higher tool-calling accuracy than Llama 4 Scout (Scout
# passed numeric params as quoted strings repeatedly, failing schema
# validation even after corrective feedback). GPT-OSS 120B's smaller 8K TPM
# is mitigated by the Condenser (see orchestrator._condense_conversation)
# rather than traded away for a less accurate model.
MODEL_INTERVIEWER = "openai/gpt-oss-120b"


class LLMPCBGroqClient:
    def __init__(self, api_key: Optional[str] = None):
        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise RuntimeError("GROQ_API_KEY is not set")
        if Groq is None:
            raise RuntimeError("groq package not installed")
        self._client = Groq(api_key=key)

    def _to_groq_tools(self, tools: list[dict]) -> list[dict]:
        # tool_definitions.py is already in OpenAI-compatible format, which
        # Groq's API uses natively -- no conversion needed.
        return tools

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
                kwargs["tools"] = self._to_groq_tools(tools)
                kwargs["tool_choice"] = "auto"
            return self._client.chat.completions.create(**kwargs)

        for candidate_model in model_candidates:
            candidate_retries = max_retries if not multi_candidate else 1
            for attempt in range(candidate_retries):
                try:
                    # Do NOT use `with ThreadPoolExecutor(...) as executor:`
                    # here -- its __exit__ calls shutdown(wait=True), which
                    # blocks until the worker thread actually finishes, even
                    # after future.result(timeout=30) has already given up.
                    # In practice the underlying groq SDK does its own
                    # internal retries/backoff on a stalled connection, so
                    # the worker could keep running for a long time after
                    # our timeout fires -- turning a "30s timeout" into an
                    # effective multi-minute stall while we wait for the
                    # thread pool to clean up. shutdown(wait=False) lets us
                    # move on immediately; the orphaned thread is reclaimed
                    # by the OS when it eventually finishes or the process
                    # exits.
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
                        continue  # try again immediately -- a stalled connection is often transient
                    break
                except Exception as e:
                    last_error = str(e)
                    is_quota_exhausted = "429" in last_error and (
                        "rate_limit" in last_error.lower() or "quota" in last_error.lower()
                    )
                    if attempt < candidate_retries - 1 and not is_quota_exhausted:
                        if "429" in last_error:
                            time.sleep(15 * (attempt + 1))
                            continue
                        if "500" in last_error or "503" in last_error or "internal" in last_error.lower():
                            time.sleep(2 * (attempt + 1))
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
        model = MODEL_MAIN
        return self.call(model, system_prompt, messages, tools=tools, temperature=0.1)

    def call_critic(self, system_prompt: str, messages: list[dict], tools: list[dict],
                     phase: str = "normal") -> dict:
        return self.call(MODEL_MAIN, system_prompt, messages, tools=tools, temperature=0.0)

    def call_interviewer(self, system_prompt: str, messages: list[dict]) -> dict:
        return self.call(MODEL_INTERVIEWER, system_prompt, messages, temperature=0.4)


if __name__ == "__main__":
    client = LLMPCBGroqClient()
    result = client.call_interviewer(
        system_prompt="You are a concise assistant.",
        messages=[{"role": "user", "content": "Hello, this is a test. Reply briefly."}],
    )
    print(result["content"])
