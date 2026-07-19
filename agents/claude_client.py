from __future__ import annotations
import os
import json
import time
from typing import Optional

try:
    import anthropic
except ImportError:
    anthropic = None


# Claude Haiku 4.5: matches Sonnet 4 on coding/tool-use, but with notably
# higher first-attempt tool-calling accuracy (per Anthropic's own release
# notes) -- directly targets today's recurring pain point of wasted
# iterations from wrong/repeated tool calls, at 2-4x the speed and a
# fraction of the cost of a larger model.
MODEL = "claude-haiku-4-5-20251001"


class LLMPCBClaudeClient:
    def __init__(self, api_key: Optional[str] = None):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        if anthropic is None:
            raise RuntimeError("anthropic package not installed")
        self._client = anthropic.Anthropic(api_key=key)

    def _to_anthropic_tools(self, openai_tools: list[dict]) -> list[dict]:
        out = []
        for t in openai_tools:
            fn = t["function"]
            out.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return out

    def call(self, model: str, system_prompt: str, messages: list[dict],
              tools: Optional[list[dict]] = None, temperature: float = 0.2,
              max_retries: int = 3) -> dict:
        anthropic_messages = []
        for m in messages:
            role = m["role"]
            if role not in ("user", "assistant"):
                role = "user"
            content = m.get("content") or ""
            if content:
                anthropic_messages.append({"role": role, "content": content})

        kwargs = dict(
            model=model, max_tokens=4096,
            # NOTE: prompt caching (cache_control below) requires a
            # minimum of 4,096 tokens for Haiku 4.5 specifically (higher
            # than Sonnet/Opus's 1024-2048 minimum) -- verified via direct
            # testing: SYSTEM_PROMPT is only ~1.5-2K tokens, so caching
            # does NOT activate here (cache_creation_input_tokens stayed
            # 0 across repeated identical calls). Left in place as a
            # no-op safety net in case the prompt grows past 4096 tokens
            # later, but the REAL cost lever for Claude right now is the
            # tighter MAX_CONVERSATION_MESSAGES/MAX_ITERATIONS caps
            # already applied above in orchestrator_minimal.py, not caching.
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=anthropic_messages, temperature=temperature,
        )
        if tools:
            kwargs["tools"] = self._to_anthropic_tools(tools)

        last_error = None
        for attempt in range(max_retries):
            try:
                resp = self._client.messages.create(**kwargs)
                last_error = None
                break
            except Exception as e:
                last_error = str(e)
                if attempt < max_retries - 1:
                    if "429" in last_error or "rate_limit" in last_error.lower() or "overloaded" in last_error.lower():
                        time.sleep(5 * (attempt + 1))
                        continue
                    if "500" in last_error or "529" in last_error:
                        time.sleep(2 * (attempt + 1))
                        continue
                break
        else:
            resp = None

        if last_error is not None:
            return {"content": None, "tool_calls": [], "error": last_error}

        text_parts = []
        tool_calls = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "arguments": block.input})

        return {
            "content": "\n".join(text_parts) if text_parts else None,
            "tool_calls": tool_calls,
            "finish_reason": resp.stop_reason,
            "raw": resp,
        }

    def call_designer(self, system_prompt: str, messages: list[dict], tools: list[dict],
                       phase: str = "normal") -> dict:
        return self.call(MODEL, system_prompt, messages, tools=tools, temperature=0.1)

    def call_critic(self, system_prompt: str, messages: list[dict], tools: list[dict],
                     phase: str = "normal") -> dict:
        return self.call(MODEL, system_prompt, messages, tools=tools, temperature=0.0)

    def call_interviewer(self, system_prompt: str, messages: list[dict]) -> dict:
        return self.call(MODEL, system_prompt, messages, temperature=0.4)


if __name__ == "__main__":
    client = LLMPCBClaudeClient()
    result = client.call_interviewer(
        system_prompt="You are a concise assistant.",
        messages=[{"role": "user", "content": "Hello, this is a test. Reply briefly."}],
    )
    print(result["content"])
