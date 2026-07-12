from __future__ import annotations
import os
import json
from typing import Optional

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None


GEMMA_CANDIDATES = ["gemma-4-31b-it", "gemma-4-26b-a4b-it"]  # try both; stability flips unpredictably between them
MODEL_DESIGNER_LIGHT = "gemini-3.1-flash-lite"  # simple research/lookup work: Flash-Lite handles this fine
MODEL_DESIGNER_ESCALATE = "gemini-3.5-flash"      # paid fallback when Gemma is unavailable or for high-stakes calls
MODEL_CRITIC_ESCALATE = "gemini-3.5-flash"
MODEL_INTERVIEWER = "gemini-3.1-flash-lite"
# NOTE: gemma-4-31b-it and gemma-4-26b-a4b-it were tried (free, and
# gemma-4-31b-it benchmarks above gemini-3.1-flash-lite per public
# comparisons) but both showed intermittent 500 INTERNAL errors in live
# testing (which model is more stable flips over time -- 26B failed 5/5 in
# one run, 31B failed 3/5 minutes later), matching widely-reported
# instability on Google's own developer forum as of this writing. See
# call_designer_gemma_attempt() for an opportunistic fallback path that
# tries both before giving up -- not currently wired into the main loop by
# default since gemini-3.5-flash proved more reliable overall.


def _to_gemini_schema(openai_tool: dict) -> dict:
    fn = openai_tool["function"]
    params = fn.get("parameters", {"type": "object", "properties": {}})

    def _convert_types(node):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "type" and isinstance(v, str):
                    out[k] = v.upper()
                else:
                    out[k] = _convert_types(v)
            return out
        if isinstance(node, list):
            return [_convert_types(x) for x in node]
        return node

    return {
        "name": fn["name"],
        "description": fn.get("description", ""),
        "parameters": _convert_types(params),
    }


class LLMPCBGeminiClient:
    def __init__(self, api_key: Optional[str] = None):
        key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError("GOOGLE_API_KEY is not set")
        if genai is None:
            raise RuntimeError("google-genai package not installed")
        # Without an explicit timeout, a stalled server-side response hangs
        # this call indefinitely (observed in practice via py-spy: the
        # process was blocked on ssl.py socket read with no progress for
        # minutes). 30s is generous for a single generate_content call but
        # prevents the whole pipeline from stalling silently.
        self._client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=30_000))

    def call(self, model: str | list[str], system_prompt: str, messages: list[dict],
              tools: Optional[list[dict]] = None, use_search: bool = False,
              temperature: float = 0.2, max_retries: int = 3) -> dict:
        contents = []
        for m in messages:
            role = "model" if m["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": m["content"]}]})

        gemini_tools = []
        if tools:
            gemini_tools.append(types.Tool(function_declarations=[_to_gemini_schema(t) for t in tools]))
        if use_search:
            gemini_tools.append(types.Tool(google_search=types.GoogleSearch()))

        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            tools=gemini_tools or None,
        )

        import time
        import concurrent.futures
        model_candidates = model if isinstance(model, list) else [model]
        multi_candidate = len(model_candidates) > 1
        last_error = None
        resp = None

        def _do_request(candidate_model):
            return self._client.models.generate_content(model=candidate_model, contents=contents, config=config)

        for candidate_model in model_candidates:
            candidate_retries = max_retries if not multi_candidate else 1
            for attempt in range(candidate_retries):
                try:
                    # The google-genai SDK has known bugs (upstream issues
                    # #911, #1876, #1893) where HttpOptions(timeout=...) is
                    # silently not honored and a stalled server connection
                    # hangs the call indefinitely. Enforce a hard wall-clock
                    # timeout from the outside using a worker thread.
                    # Do NOT use `with ThreadPoolExecutor(...) as executor:`
                    # -- its __exit__ blocks until the orphaned worker
                    # thread actually finishes (which can be minutes if the
                    # underlying SDK is doing its own internal retries),
                    # turning a 30s timeout into a much longer effective
                    # stall. shutdown(wait=False) lets us move on immediately.
                    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    future = executor.submit(_do_request, candidate_model)
                    try:
                        resp = future.result(timeout=30)
                    finally:
                        executor.shutdown(wait=False)
                    last_error = None
                    break
                except concurrent.futures.TimeoutError:
                    last_error = "LLMPCB_TIMEOUT: request exceeded 30s wall-clock limit (SDK-level hang, see gemini_client.py)"
                    if attempt < candidate_retries - 1:
                        continue  # try again immediately -- a stalled connection is often transient
                    break
                except Exception as e:
                    last_error = str(e)
                    is_quota_exhausted = "429" in last_error and (
                        "exceeded your current quota" in last_error.lower()
                        or "resource_exhausted" in last_error.lower()
                    )
                    if attempt < candidate_retries - 1 and not is_quota_exhausted:
                        if "429" in last_error:
                            time.sleep(20 * (attempt + 1))  # backoff for transient free-tier RPM limit
                            continue
                        if "500" in last_error or "INTERNAL" in last_error or "503" in last_error:
                            time.sleep(2 * (attempt + 1))  # transient server-side error, short retry
                            continue
                    break  # exhausted retries (or quota dead) for this candidate, try the next model
            if resp is not None and last_error is None:
                break  # this candidate succeeded, stop trying further models

        if resp is None or last_error is not None:
            return {"content": None, "tool_calls": [], "error": last_error}

        candidate = resp.candidates[0]
        text_parts = []
        tool_calls = []
        for part in candidate.content.parts:
            if getattr(part, "text", None):
                text_parts.append(part.text)
            if getattr(part, "function_call", None):
                fc = part.function_call
                tool_calls.append({"id": fc.name, "name": fc.name, "arguments": dict(fc.args)})

        return {
            "content": "\n".join(text_parts) if text_parts else None,
            "tool_calls": tool_calls,
            "finish_reason": str(candidate.finish_reason),
            "raw": resp,
        }

    def call_designer(self, system_prompt: str, messages: list[dict], tools: list[dict],
                       phase: str = "normal") -> dict:
        """phase='light': simple research/lookup work -- Flash-Lite directly.
        phase='normal': try free Gemma 4 (31B then 26B) first; if both fail,
        fall back to Flash-Lite (NOT 3.5 Flash) -- most schematic/DRC work
        is still within Flash-Lite's ability, just with more retries.
        phase='escalate': Gemini 3.5 Flash, reserved ONLY for situations
        Flash-Lite has demonstrably failed at repeatedly (stuck loops) or
        judgment calls where an independent, high-quality review is
        essential (functional completeness check). Do not use 3.5 Flash by
        default -- its free quota is far smaller (RPD ~20) than Flash-Lite's
        (RPD ~500) and it must be reserved for cases Flash-Lite truly
        cannot handle.
        """
        if phase == "escalate":
            model = MODEL_DESIGNER_ESCALATE
        elif phase == "light":
            model = MODEL_DESIGNER_LIGHT
        else:
            model = GEMMA_CANDIDATES + [MODEL_DESIGNER_LIGHT]
        return self.call(model, system_prompt, messages, tools=tools, temperature=0.1)

    def call_critic(self, system_prompt: str, messages: list[dict], tools: list[dict],
                     phase: str = "normal") -> dict:
        """phase='escalate' (functional-completeness check, CRITICAL-
        adjacent judgment) goes straight to Gemini 3.5 Flash -- these are
        the cases genuinely worth spending the scarce quota on.
        phase='normal' (routine minor-warning review) tries free Gemma 4
        first, falling back to Flash-Lite (not 3.5 Flash) if both fail.
        """
        model = MODEL_CRITIC_ESCALATE if phase == "escalate" else GEMMA_CANDIDATES + [MODEL_DESIGNER_LIGHT]
        return self.call(model, system_prompt, messages, tools=tools, temperature=0.0)

    def call_interviewer(self, system_prompt: str, messages: list[dict]) -> dict:
        return self.call(MODEL_INTERVIEWER, system_prompt, messages, temperature=0.4)


if __name__ == "__main__":
    client = LLMPCBGeminiClient()
    result = client.call_interviewer(
        system_prompt="You are a concise assistant.",
        messages=[{"role": "user", "content": "Hello, this is a test. Reply briefly."}],
    )
    print(result["content"])
