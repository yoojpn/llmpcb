from __future__ import annotations
import os
import sys
import json
import re
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import calculators, research, safety_checker, spice_models, batch_research
from kicad_utils import kicad_wrapper
from agents.gemini_client import LLMPCBGeminiClient
from agents.tool_definitions import (
    ALL_DESIGNER_TOOLS, ALL_CRITIC_TOOLS, CALCULATOR_TOOLS, RESEARCH_TOOLS, KICAD_TOOLS,
)

MAX_LOOP_ITERATIONS = 30

CRITICAL_FIXED_LIST = {
    "short_circuit",
    "reverse_voltage",
    "current_overload",
    "thermal_overload",
    "copper_edge_violation",
}

TOOL_DISPATCH = {
    "calc_led_resistor": calculators.calc_led_resistor,
    "calc_voltage_divider": calculators.calc_voltage_divider,
    "calc_trace_width": calculators.calc_trace_width,
    "calc_battery_life": calculators.calc_battery_life,
    "unit_convert": calculators.unit_convert,
    "search_reference_design": research.search_reference_design,
    "fetch_and_extract_schematic_data": research.fetch_and_extract_schematic_data,
    "search_footprint_library": research.search_footprint_library,
    "reject_component_no_footprint": research.reject_component_no_footprint,
    "search_spice_model": spice_models.search_spice_model,
    "batch_research_parts": batch_research.batch_research_parts,
    "generate_schematic": kicad_wrapper.generate_schematic,
    "run_spice_simulation": kicad_wrapper.run_spice_simulation,
    "generate_pcb_layout": kicad_wrapper.generate_pcb_layout,
    "run_drc_check": kicad_wrapper.run_drc_check,
    "build_and_check_pcb": kicad_wrapper.build_and_check_pcb,
    "build_and_simulate_schematic": kicad_wrapper.build_and_simulate_schematic,
}
# read_offloaded_file is defined further down (it's specific to this
# module's offload mechanism) and registered into the dispatch table here,
# after its definition, to avoid a forward-reference at module load time.


@dataclass
class LoopState:
    iteration: int = 0
    resolved: bool = False
    escalated_to_human: bool = False
    history: list[dict] = field(default_factory=list)
    unresolved_reason: Optional[str] = None
    spice_verified: Optional[bool] = None  # None=not attempted, True=verified, False=unavailable
    # internal state needed to resume the loop after a human-in-the-loop pause
    conversation: list[dict] = field(default_factory=list)
    schematic_generated: bool = False
    spice_status: Optional[str] = None
    last_verified_netlist_hash: Optional[str] = None
    recent_call_signatures: list[str] = field(default_factory=list)
    cleared_warning_types: list[str] = field(default_factory=list)
    total_413_count: int = 0
    no_tool_call_streak: int = 0


def _run_tool_calls(tool_calls: list[dict]) -> list[dict]:
    results = []
    for tc in tool_calls:
        name = tc["name"]
        args = tc["arguments"]
        if name in ("build_and_check_pcb", "generate_pcb_layout") and "netlist_path" in args:
            # Deterministically correct a nonexistent netlist_path to the
            # most recently generated real netlist file, rather than
            # letting a wrong-filename guess turn into a failed tool call
            # that (in practice) confused the Designer into regenerating
            # the schematic from scratch over and over instead of just
            # using the right path.
            if not os.path.exists(args["netlist_path"]):
                candidates = sorted(kicad_wrapper.WORKDIR.glob("*.net"), key=lambda p: p.stat().st_mtime, reverse=True)
                if candidates:
                    args["netlist_path"] = str(candidates[0])
        fn = TOOL_DISPATCH.get(name)
        if fn is None:
            results.append({"tool_call_id": tc["id"], "name": name, "args": args, "error": f"unknown tool: {name}"})
            continue
        try:
            output = fn(**args)
        except Exception as e:
            output = {"error": str(e)}
        results.append({"tool_call_id": tc["id"], "name": name, "args": args, "output": output})
    return results


COMPRESS_TOKEN_THRESHOLD = 4000  # trigger compression once estimated tokens exceed this
KEEP_RECENT_MESSAGES = 3           # try to keep this many most-recent messages verbatim
MAX_SINGLE_MESSAGE_CHARS = 4000    # hard cap per message even when "kept recent" -- a single


def _estimate_tokens(text: str) -> int:
    return len(text) // 4  # rough heuristic, consistent with the rest of this file's estimates


def _total_conversation_tokens(conversation: list[dict]) -> int:
    return sum(_estimate_tokens(m.get("content") or "") for m in conversation)


def _condense_conversation(conversation: list[dict], client, spec: dict, base_overhead_tokens: int = 0) -> list[dict]:
    """Condenser pattern (as used in OpenHands / LangChain Deep Agents):
    once the conversation grows past a threshold, replace the OLDER portion
    with a single LLM-generated summary that preserves decisions made,
    constraints established, and important tool-output facts (part numbers
    found, footprint paths, calculated values, DRC violations already
    resolved) -- while keeping the most recent turns verbatim so immediate
    context isn't lost. This is the standard technique for bounding
    context growth in long-running tool-use agents; naive truncation loses
    the same information without the benefit of an explicit, structured
    summary the model can still reason from.

    `base_overhead_tokens` must include the system prompt and active tool
    definitions -- a 413 can happen even with a SHORT conversation if the
    fixed overhead (system prompt + tools) already eats most of the model's
    TPM budget. Comparing only the conversation's own token count against a
    fixed threshold (as an earlier version of this function did) missed
    that case entirely: it kept returning "already small enough, nothing to
    compress" while the request kept failing with 413 for the same reason
    every time, because the actual over-budget contributor (the fixed
    overhead) was invisible to this function.
    """
    effective_budget = max(COMPRESS_TOKEN_THRESHOLD - base_overhead_tokens, 500)
    if _total_conversation_tokens(conversation) <= effective_budget and len(conversation) > KEEP_RECENT_MESSAGES:
        return conversation
    if len(conversation) <= KEEP_RECENT_MESSAGES:
        # Nothing left to summarize away -- the overhead itself (system
        # prompt + tools) is the problem, which this function cannot fix.
        # The caller must reduce the tool set or shorten the prompt instead.
        return conversation

    to_summarize = conversation[:-KEEP_RECENT_MESSAGES]
    recent = conversation[-KEEP_RECENT_MESSAGES:]
    # Even "kept recent" messages get a hard per-message cap -- a single
    # huge message (e.g. a tool result that somehow bypassed offloading)
    # among the "recent" ones would otherwise defeat the whole point of
    # condensing everything else.
    recent = [
        {**m, "content": (m.get("content") or "")[:MAX_SINGLE_MESSAGE_CHARS]}
        for m in recent
    ]

    summary_prompt = (
        "Summarize the following design-agent conversation history. Preserve, "
        "verbatim where possible: (1) every part number, footprint reference, "
        "and file path that was found or confirmed, (2) every calculated "
        "numeric value (resistor values, currents, voltages) and which tool "
        "produced it, (3) any DRC violations already resolved or dismissed "
        "and why, (4) the current state of the design (what's been generated, "
        "what SPICE/DRC status is). Be specific -- do not paraphrase numbers "
        "or identifiers away. This summary replaces the raw history for an "
        "ongoing task, so omitting a concrete fact means it is lost."
    )
    summary_resp = client.call_designer(
        summary_prompt,
        to_summarize + [{"role": "user", "content": "Produce the summary now."}],
        tools=[],
        phase="light",
    )
    summary_text = summary_resp.get("content") or "(summary generation failed; history compressed with no summary available)"

    condensed = [{
        "role": "user",
        "content": f"[CONVERSATION SUMMARY replacing {len(to_summarize)} earlier messages]\n{summary_text}",
    }]
    return condensed + recent


_TOOL_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_work", "tool_outputs")
_OFFLOAD_THRESHOLD_CHARS = 1500  # ~375 tokens; below this, keep inline (no point offloading tiny results)


def _offload_large_tool_results(tool_results: list[dict], iteration: int) -> list[dict]:
    """Memory Pointer / offload pattern, as used by Claude Code, OpenHands,
    LangChain Deep Agents, and Google ADK (Artifacts): rather than pasting
    a large tool result into the LLM's context (where it counts against
    the TPM budget on every subsequent turn until compressed away), write
    it to disk and give the model a short pointer + preview instead. This
    is the actual fix for context bloat from big individual tool outputs
    (e.g. a single batch_research_parts or fetch_and_extract_schematic_data
    call already running several KB) -- character truncation or waiting
    for a message-count threshold to trigger summarization (the Condenser)
    cannot help here, because the bloat comes from ONE large message, not
    from many small ones accumulating. IBM's published benchmark for this
    pattern: a workflow that used 20M tokens and failed dropped to 1,234
    tokens and succeeded once outputs were offloaded to pointers instead
    of being pasted inline.
    """
    os.makedirs(_TOOL_OUTPUT_DIR, exist_ok=True)
    result = []
    for idx, tr in enumerate(tool_results):
        if tr["name"] == "read_offloaded_file":
            # Never re-offload the result of reading an offloaded file --
            # that would create pointer-to-pointer chains (observed in
            # practice: read_offloaded_file's own ~6000-char result
            # exceeded the offload threshold, got offloaded itself, and the
            # Designer spent 10+ turns chasing an ever-deeper chain of
            # "read the file that tells you where the file is" without ever
            # reaching the actual data). This tool's job IS to bring data
            # back into context; defeating that purpose here would make it
            # useless. Its result is already explicitly capped in
            # read_offloaded_file() itself, so it can't grow the request
            # size unboundedly.
            result.append(tr)
            continue
        full_json = json.dumps(tr.get("output"), ensure_ascii=False, default=str)
        if len(full_json) <= _OFFLOAD_THRESHOLD_CHARS:
            result.append(tr)
            continue
        filename = f"iter{iteration}_call{idx}_{tr['name']}.json"
        filepath = os.path.join(_TOOL_OUTPUT_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(full_json)
        preview = full_json[:1400]
        result.append({
            **{k: v for k, v in tr.items() if k != "output"},
            "output": {
                "_offloaded_to_file": filepath,
                "_preview": preview + ("..." if len(full_json) > 300 else ""),
                "_note": (
                    f"Full result ({len(full_json)} chars) was too large to keep in "
                    f"conversation and was written to {filepath}. Use read_offloaded_file "
                    f"to load it if you need details beyond this preview; otherwise the "
                    f"preview above plus this tool's own success/found indicators is "
                    f"usually enough to proceed."
                ),
            },
        })
    return result


def read_offloaded_file(filepath: str) -> dict:
    """Tool for the Designer to recover a full tool result that was
    offloaded to disk by _offload_large_tool_results, if the preview
    wasn't enough detail to proceed."""
    if not os.path.abspath(filepath).startswith(os.path.abspath(_TOOL_OUTPUT_DIR)):
        return {"error": "refused: path is outside the tool-output directory"}
    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
        return {"content": content[:600], "truncated": len(content) > 600}
    except FileNotFoundError:
        return {"error": f"file not found: {filepath}"}


TOOL_DISPATCH["read_offloaded_file"] = read_offloaded_file


_SCRIPT_OUTPUT_MAX_CHARS = 3000


def execute_design_script(code: str) -> dict:
    """Programmatic Tool Calling pattern (as used by the Anthropic API's
    code_execution + tool orchestration feature, GA Feb 2026): instead of
    calling one tool per model round trip, the Designer writes a Python
    script that calls MULTIPLE tools in sequence/combination itself, all
    executed here in one shot. Only the script's print() output returns to
    the conversation -- not each individual intermediate tool result. This
    collapses what used to be N round trips (research -> calc -> schematic
    -> pcb, each its own turn) into a single turn, which is the actual
    fix for "why does a simple board take 10+ round trips" rather than
    another guard/nudge layered on top of the existing one-tool-per-turn
    loop.

    All existing tools (batch_research_parts, calc_*, search_*,
    build_and_simulate_schematic, build_and_check_pcb, etc.) are available
    as plain functions by their tool name inside the script. Use print()
    to surface whatever you need to see in the result -- everything else
    stays out of the conversation entirely (an even more direct win than
    the offload/pointer mechanism, since it never enters context at all).
    """
    import io
    import contextlib
    import concurrent.futures

    script_globals = dict(TOOL_DISPATCH)  # tool_name -> callable, usable directly by name
    script_globals["__builtins__"] = __builtins__

    stdout_buf = io.StringIO()

    def _run():
        with contextlib.redirect_stdout(stdout_buf):
            exec(code, script_globals)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_run)
    try:
        future.result(timeout=60)
        error = None
    except concurrent.futures.TimeoutError:
        error = "script exceeded 60s execution limit"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    finally:
        executor.shutdown(wait=False)

    output = stdout_buf.getvalue()
    truncated = len(output) > _SCRIPT_OUTPUT_MAX_CHARS
    if truncated:
        output = output[:_SCRIPT_OUTPUT_MAX_CHARS] + f"\n...({len(stdout_buf.getvalue())} chars total, truncated -- print less, or offload large intermediate results to a file yourself)"

    return {"success": error is None, "stdout": output, "error": error}


TOOL_DISPATCH["execute_design_script"] = execute_design_script


def _classify_violation(violation: dict) -> str:
    text = json.dumps(violation, ensure_ascii=False).lower()
    if "short" in text:
        return "short_circuit"
    if "reverse" in text:
        return "reverse_voltage"
    if "overload" in text:
        return "current_overload"
    if "thermal" in text:
        return "thermal_overload"
    if "copper" in text and ("edge" in text or "clearance" in text):
        # A copper pad overlapping or touching the board edge is a physical
        # manufacturing/reliability defect (drill breakout, reduced board
        # strength, possible short at the edge), not a cosmetic issue --
        # observed in practice being repeatedly (correctly) REJECTed by the
        # Critic while classified as "minor", which meant the auto-clear
        # dedup never applied and the same rejection repeated many times.
        return "copper_edge_violation"
    return "minor"


def _netlist_hash(netlist_path: str | None) -> str | None:
    if not netlist_path or not os.path.exists(netlist_path):
        return None
    import hashlib
    with open(netlist_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _get_designer_part_plan(client: LLMPCBGeminiClient, spec: dict, designer_system_prompt: str) -> list[dict]:
    """Phase 0 of the pipeline: ask the Designer for a structured JSON list
    of parts needed (no tool calls at all) in exactly one LLM turn, then
    the caller executes ALL research for that list mechanically in Python
    (batch_research_parts, zero further LLM turns). This replaces relying
    on prompt instructions ("batch your tool calls") -- which the model
    did not reliably follow, resulting in individual search_footprint_library/
    read_offloaded_file calls scattered across many separate turns in
    practice -- with an enforced two-phase structure where the batching is
    not optional.
    """
    plan_prompt = (
        f"{designer_system_prompt}\n\n"
        "PLANNING PHASE: Do not call any tools yet. Given the spec below, output ONLY a JSON "
        "array (no other text, no markdown fences) of every part this design will need that "
        "has a specific manufacturer part number (MCU, IC, connector, sensor, display, etc -- "
        "NOT generic passives like plain resistors/capacitors/LEDs, which need no research). "
        "Each entry: "
        '{"part_number": "...", "manufacturer": "...", "needs_spice_model": true/false, '
        '"needs_datasheet": true/false, "datasheet_sections": ["..."]}. '
        "Use real, specific manufacturer part numbers you are confident exist -- not category "
        "names. If truly no researchable parts are needed, output []."
    )
    resp = client.call_designer(
        plan_prompt,
        [{"role": "user", "content": f"Design based on this spec:\n{json.dumps(spec, ensure_ascii=False, indent=2)}"}],
        tools=[],
        phase="light",
    )
    if resp.get("error") or not resp.get("content"):
        return []
    text = resp["content"].strip()
    # strip markdown code fences if the model added them despite instructions
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        plan = json.loads(text)
        return plan if isinstance(plan, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


def run_audit_loop(spec: dict, client: LLMPCBGeminiClient,
                    designer_system_prompt: str, critic_system_prompt: str,
                    on_progress=None, resume_state: "LoopState | None" = None,
                    extra_iterations: int = MAX_LOOP_ITERATIONS,
                    human_guidance: str | None = None) -> LoopState:
    if resume_state is not None:
        state = resume_state
        state.resolved = False
        state.escalated_to_human = False
        state.unresolved_reason = None
        conversation = state.conversation
        schematic_generated = state.schematic_generated
        spice_status = state.spice_status
        last_verified_netlist_hash = state.last_verified_netlist_hash
        recent_call_signatures = state.recent_call_signatures
        cleared_warning_types = state.cleared_warning_types
        max_iteration_target = state.iteration + extra_iterations
        if human_guidance:
            conversation.append({"role": "user", "content": f"Human guidance: {human_guidance}"})
    else:
        state = LoopState()
        # Phase 0: get the full part list in one LLM turn (no tool calls),
        # then execute all research for it in Python with zero further LLM
        # turns. This is the enforced version of "batch your research" --
        # previously only a prompt instruction, which was not reliably
        # followed and resulted in many individual tool-call turns.
        part_plan = _get_designer_part_plan(client, spec, designer_system_prompt)
        initial_content = f"Design based on this spec:\n{json.dumps(spec, ensure_ascii=False, indent=2)}"
        if part_plan:
            research_results = batch_research.batch_research_parts(part_plan)
            offloaded = _offload_large_tool_results(
                [{"name": "batch_research_parts", "output": research_results, "tool_call_id": "phase0", "args": {"parts": part_plan}}],
                0,
            )
            initial_content += (
                f"\n\nThe following parts were pre-researched for you before this "
                f"conversation started (Phase 0, already done). This includes footprint "
                f"info, SPICE models, and the requested datasheet sections. "
                f"**Do NOT call batch_research_parts, search_footprint_library, "
                f"search_reference_design, fetch_and_extract_schematic_data, or "
                f"search_spice_model again for these parts -- you already have everything "
                f"needed below.** If a preview was truncated and you genuinely need more "
                f"detail, use read_offloaded_file on the exact file path shown, but do not "
                f"re-search from scratch. Proceed straight to calc_* and then "
                f"build_and_simulate_schematic.\n\n"
                f"{json.dumps(offloaded, ensure_ascii=False, indent=2)}"
            )
        conversation = [{"role": "user", "content": initial_content}]
        schematic_generated = False
        spice_status = None  # None=not attempted, "verified", "unavailable"
        last_verified_netlist_hash = None
        recent_call_signatures: list[str] = []
        cleared_warning_types: list[str] = []
        max_iteration_target = extra_iterations
    REPEAT_LIMIT = 3

    while state.iteration < max_iteration_target and not state.resolved:
        state.iteration += 1
        if on_progress:
            on_progress(state.iteration, state)
        # Lightweight diagnostic marker on every single iteration, appended
        # unconditionally regardless of which `continue` branch is hit
        # below. Several silent-loop bugs in this file (SPICE-block nudge,
        # missing-footprint block, stale-PCB block, etc) were only
        # discoverable after the fact because their `continue` paths never
        # touched state.history at all, making hundreds of iterations
        # invisible in the log. This ensures every iteration leaves at
        # least a trace of which state variables were true at the time.
        state.history.append({
            "iteration": state.iteration, "role": "_tick",
            "response": f"schematic_generated={schematic_generated} spice_status={spice_status} total_413_count={state.total_413_count}",
        })

        # Escalate to the stronger (quota-limited) model only when the free
        # model appears to be stuck -- i.e. it has repeated the same tool
        # call 2+ times already -- rather than using it by default. This
        # keeps 3.5 Flash usage reserved for genuinely difficult moments.
        near_repeat = len(recent_call_signatures) >= 2 and len(set(recent_call_signatures[-2:])) == 1
        if near_repeat:
            designer_phase = "escalate"
        elif not schematic_generated:
            designer_phase = "light"
        else:
            designer_phase = "normal"
        # Only send the tool subset relevant to the current phase. The full
        # tool list (~2300 tokens) plus the system prompt (~2200 tokens)
        # alone consumed over half of GPT-OSS 120B's 8K free-tier TPM
        # budget on Groq before any conversation history was even added --
        # this was the real, structural cause of 413 "request too large"
        # errors (not just a growing conversation). Narrowing to what's
        # actually usable before schematic generation cuts that baseline
        # roughly in half.
        active_tools = (CALCULATOR_TOOLS + RESEARCH_TOOLS) if not schematic_generated else ALL_DESIGNER_TOOLS
        designer_resp = client.call_designer(designer_system_prompt, conversation, active_tools, phase=designer_phase)
        if designer_resp.get("error"):
            error_text = designer_resp["error"]
            is_request_too_large = "413" in error_text or "too large" in error_text.lower()
            # A 429 for daily/token-per-day limits is a quota reset that
            # won't happen again until tomorrow. Gemini's 429s all use the
            # generic "RESOURCE_EXHAUSTED" label regardless of whether it's
            # a transient RPM hit (resolves in seconds) or a hard daily
            # quota exhaustion (resolves in ~24h) -- since the message text
            # alone can't distinguish them, count both via the same
            # cumulative counter as 413s rather than guessing: a merely
            # transient RPM 429 will stop recurring after a few turns and
            # never reach the threshold, while a real daily exhaustion will
            # keep failing every turn and correctly escalate.
            is_rate_or_quota_error = "429" in error_text or is_request_too_large
            if is_rate_or_quota_error:
                state.total_413_count += 1
                # A raw char/4 token estimate is not precise enough to
                # reliably detect "compression made no progress" by
                # comparing before/after token counts (observed: it
                # fluctuated by a few tokens turn to turn even while the
                # underlying 413 kept recurring identically, so that
                # comparison never triggered). Counting consecutive 413s
                # directly is a simpler, unambiguous signal: if the model's
                # own quota mechanism keeps rejecting the same request
                # shape several times in a row despite compression
                # attempts, no amount of further compression is fixing it
                # -- escalate instead of spinning indefinitely.
                if state.total_413_count >= 5:
                    state.escalated_to_human = True
                    state.unresolved_reason = (
                        "[UNRECOVERABLE] Request too large (413) recurred 5 times in a row even "
                        "after conversation compression attempts. The fixed overhead (system "
                        "prompt + tool definitions) is likely too large for this model's "
                        "TPM budget on its own -- this requires a smaller tool set, a "
                        "shorter system prompt, or a model with a larger TPM limit, not "
                        "something the auto-fix loop can resolve by itself."
                    )
                    break
                before_len = len(conversation)
                before_tokens = _total_conversation_tokens(conversation)
                base_overhead = _estimate_tokens(designer_system_prompt) + _estimate_tokens(json.dumps(active_tools))
                conversation[:] = _condense_conversation(conversation, client, spec, base_overhead_tokens=base_overhead)
                after_tokens = _total_conversation_tokens(conversation)
                state.history.append({
                    "iteration": state.iteration, "role": "conversation_compressed",
                    "response": (
                        f"request too large ({error_text[:150]}); base_overhead≈{base_overhead} tokens, "
                        f"condensed {before_len} messages ({before_tokens} tokens) down to "
                        f"{len(conversation)} messages ({after_tokens} tokens); "
                        f"total_413_count={state.total_413_count}"
                    ),
                })
                continue
            # NOTE: deliberately not reset to 0 elsewhere -- this counts
            # cumulative 413s across the whole run, not a strict streak.
            # A strict "reset on any success" streak counter was tried
            # first and never reached its threshold in practice: a stray
            # successful turn or an unrelated api_error in between 413s
            # kept resetting it, so the process spun for 300+ iterations
            # hitting the same unfixable 413 without ever escalating.
            is_bad_tool_params = "tool_use_failed" in error_text or "did not match schema" in error_text
            if is_bad_tool_params:
                # The model passed numbers as quoted strings (e.g.
                # supply_voltage_v: "5" instead of 5), which some Groq
                # models (observed with Llama 4 Scout) do more often than
                # others. This is a instruction-following issue, not a
                # transient one -- give explicit corrective feedback rather
                # than silently retrying the same mistake.
                conversation.append({
                    "role": "user",
                    "content": (
                        f"Your previous tool call failed validation: {error_text[:400]}\n"
                        "Numeric parameters (voltage, current, resistance, dimensions, etc) "
                        "must be passed as JSON numbers, not quoted strings. For example, "
                        "use 5 not \"5\". Retry the tool call with correctly-typed parameters."
                    )
                })
                state.history.append({
                    "iteration": state.iteration, "role": "tool_param_error",
                    "response": error_text[:300],
                })
                continue
            # The API call itself failed (timeout, transient server error,
            # etc) -- this is NOT the same as "the model chose not to call
            # a tool", and must not be silently treated as a normal empty
            # turn. Record it so it's visible in history/logs, and retry
            # without consuming the conversation with a blank assistant
            # turn (which was previously happening and caused hundreds of
            # silent no-op iterations that never appeared in history).
            state.history.append({
                "iteration": state.iteration, "role": "api_error",
                "response": designer_resp["error"],
            })
            continue
        conversation.append({"role": "assistant", "content": designer_resp["content"] or ""})

        if designer_resp["tool_calls"]:
            state.no_tool_call_streak = 0
            requested_names = {tc["name"] for tc in designer_resp["tool_calls"]}
            spice_related = {
                "run_spice_simulation", "build_and_simulate_schematic", "search_spice_model",
                "search_reference_design", "fetch_and_extract_schematic_data",  # allowed prep steps
            }
            if schematic_generated and spice_status is None and not (requested_names & spice_related):
                # Mechanically block: do not execute the requested tool call
                # at all if the model is trying to skip past SPICE verification
                # (e.g. jumping straight to generate_pcb_layout). An
                # instruction alone was observed to be ignored in practice.
                conversation.append({
                    "role": "user",
                    "content": (
                        f"BLOCKED: your requested tool call(s) {sorted(requested_names)} were not "
                        "executed. You must call run_spice_simulation or build_and_simulate_schematic "
                        "(with a netlist argument) before proceeding to PCB layout or DRC. If a "
                        "model is unavailable, search_spice_model will tell you so and you can then "
                        "proceed with the datasheet-based fallback."
                    )
                })
                continue

            def _normalize_for_dedup(name: str, args: dict) -> str:
                # For code-bearing tools, ignore the exact source text
                # (comments/whitespace/variable names vary trivially between
                # attempts) and instead fingerprint by which real symbols/
                # footprints/output name are referenced -- that is what
                # actually determines whether this is "the same attempt
                # again" versus a genuinely different one. A prior exact-JSON
                # comparison let near-identical retries slip through
                # indefinitely (observed: 6+ near-duplicate schematic
                # attempts in a row that differed only in whitespace).
                if name in ("build_and_simulate_schematic", "generate_schematic"):
                    code = args.get("skidl_code", "")
                    symbol_refs = tuple(sorted(re.findall(r'"([^"]+\.kicad_sym)"', code)))
                    part_names = tuple(sorted(re.findall(r'Part\(\s*"[^"]+"\s*,\s*"([^"]+)"', code)))
                    return json.dumps({"tool": name, "symbols": symbol_refs, "parts": part_names,
                                        "output": args.get("output_name")}, sort_keys=True)
                return json.dumps({"tool": name, "args": args}, sort_keys=True, default=str)

            call_sig = json.dumps(
                sorted([_normalize_for_dedup(tc["name"], tc["arguments"]) for tc in designer_resp["tool_calls"]]),
                ensure_ascii=False,
            )
            recent_call_signatures.append(call_sig)
            recent_call_signatures = recent_call_signatures[-REPEAT_LIMIT:]
            if len(recent_call_signatures) == REPEAT_LIMIT and len(set(recent_call_signatures)) == 1:
                # Same tool call with identical arguments repeated -- the model
                # is stuck (e.g. searching a category name instead of a real
                # part number). Do not keep burning iterations; force a
                # different approach instead of re-executing the same call.
                conversation.append({
                    "role": "user",
                    "content": (
                        "You have called the exact same tool with the exact same arguments "
                        f"{REPEAT_LIMIT} times in a row and gotten the same result each time. "
                        "Repeating it again will not change the outcome. If you were searching "
                        "for a generic category name (e.g. 'Comparator', 'Sensor') rather than "
                        "a specific manufacturer part number, that is likely the problem -- "
                        "search for a specific part number instead. If a part genuinely has no "
                        "footprint, call reject_component_no_footprint and pick a different "
                        "specific part."
                    )
                })
                continue

            tool_results = _run_tool_calls(designer_resp["tool_calls"])
            state.history.append({"iteration": state.iteration, "role": "designer", "tool_results": tool_results})
            for tr in tool_results:
                out = tr.get("output", {}) or {}
                if tr["name"] == "generate_schematic" and out.get("success"):
                    schematic_generated = True
                    new_hash = _netlist_hash(out.get("netlist_path"))
                    if new_hash and new_hash != last_verified_netlist_hash:
                        spice_status = None  # circuit content actually changed
                elif tr["name"] == "build_and_simulate_schematic":
                    if (out.get("schematic") or {}).get("success"):
                        schematic_generated = True
                        new_hash = _netlist_hash((out.get("schematic") or {}).get("netlist_path"))
                        if out.get("spice") is not None:
                            spice_status = "verified" if out["spice"].get("success") else "unavailable"
                            if spice_status == "verified":
                                last_verified_netlist_hash = new_hash
                        elif new_hash and new_hash != last_verified_netlist_hash:
                            # schematic regenerated with different content and no
                            # netlist passed this time -- prior "verified" no longer applies
                            spice_status = None
                elif tr["name"] == "run_spice_simulation":
                    spice_status = "verified" if out.get("success") else "unavailable"
            conversation.append({
                "role": "user",
                "content": f"Tool results:\n{json.dumps(_offload_large_tool_results(tool_results, state.iteration), ensure_ascii=False, indent=2)}"
            })
            continue

        # Designer produced no tool calls this turn. If the most recent
        state.no_tool_call_streak += 1
        if state.no_tool_call_streak >= 10:
            # The Designer has responded with text only (no tool call) 10
            # turns in a row. This is a distinct failure mode from the
            # 413/quota loops -- every nudge below (schematic missing,
            # stale PCB, etc) appends to `conversation` to prod the model
            # into action, but if the model just keeps replying with prose
            # instead of calling a tool, none of those nudges ever reach
            # their own resolution and the loop can spin indefinitely
            # without a single tool actually being invoked. (Discovered via
            # the _tick diagnostic marker: 3570+ iterations recorded with
            # no matching "designer"/"api_error" entry, because this whole
            # code path never touches state.history on its own.)
            state.escalated_to_human = True
            state.unresolved_reason = (
                "[UNRECOVERABLE] The Designer has not called any tool for 10 consecutive "
                "turns despite repeated prompts to do so. This model appears stuck "
                "producing prose instead of taking action and cannot make further "
                "progress without human intervention."
            )
            break
        # critic_final_review was a FAIL, that means real work is still
        # required (e.g. add the missing OLED/encoder parts) -- silently
        # falling through to re-review the *same* unfixed PCB with the
        # Critic again would just repeat the identical FAIL verdict forever.
        # Force the Designer to actually act instead.
        last_final_review = next(
            (h for h in reversed(state.history) if h.get("role") == "critic_final_review"), None
        )
        if last_final_review:
            verdict_m = re.search(r"VERDICT:\s*(PASS|FAIL)", last_final_review.get("response") or "", re.IGNORECASE)
            if verdict_m and verdict_m.group(1).upper() == "FAIL":
                conversation.append({
                    "role": "user",
                    "content": (
                        "You have not called any tools since the last functional completeness "
                        "FAIL. Re-reviewing the same unmodified board will produce the same "
                        "FAIL verdict again. Take concrete action now: add the missing "
                        "component(s) to the SKiDL code and call build_and_simulate_schematic "
                        "and build_and_check_pcb again."
                    )
                })
                continue

        if not schematic_generated:
            # Designer stopped calling tools but never actually generated a schematic
            # (e.g. it just wrote code as text). Force it to continue.
            conversation.append({
                "role": "user",
                "content": (
                    "You have not called generate_schematic yet. Writing SKiDL code as text "
                    "is not sufficient. Call the generate_schematic tool now with your code."
                )
            })
            state.history.append({
                "iteration": state.iteration, "role": "nudge_no_schematic_yet",
                "response": (designer_resp.get("content") or "")[:200],
            })
            continue

        if spice_status is None:
            # No tool calls were made this turn (handled above) but SPICE
            # still hasn't been attempted -- prompt again.
            conversation.append({
                "role": "user",
                "content": (
                    "You have not called run_spice_simulation (or build_and_simulate_schematic "
                    "with a netlist) yet. Call one of those now for this circuit."
                )
            })
            continue

        drc_called = any(
            tr["name"] in ("run_drc_check", "build_and_check_pcb")
            for h in state.history for tr in h.get("tool_results", [])
        )
        # Check the PCB layout was built from the CURRENT schematic, not a
        # stale one. Otherwise a schematic fix (e.g. adding a missing
        # connector) can go unreflected in the PCB the Critic actually
        # reviews, causing the same functional-completeness FAIL to repeat
        # forever even after the Designer "fixed" it.
        latest_schematic_netlist_path = None
        latest_pcb_source_netlist_path = None
        for h in state.history:
            for tr in h.get("tool_results", []):
                out = tr.get("output", {}) or {}
                if tr["name"] == "build_and_simulate_schematic" and (out.get("schematic") or {}).get("success"):
                    latest_schematic_netlist_path = (out.get("schematic") or {}).get("netlist_path")
                elif tr["name"] == "generate_schematic" and out.get("success"):
                    latest_schematic_netlist_path = out.get("netlist_path")
                elif tr["name"] == "build_and_check_pcb" and (out.get("layout") or {}).get("success"):
                    latest_pcb_source_netlist_path = tr.get("args", {}).get("netlist_path")
                elif tr["name"] == "generate_pcb_layout" and out.get("success"):
                    latest_pcb_source_netlist_path = tr.get("args", {}).get("netlist_path")

        pcb_is_stale = (
            drc_called
            and latest_schematic_netlist_path is not None
            and _netlist_hash(latest_schematic_netlist_path) != _netlist_hash(latest_pcb_source_netlist_path)
        )
        if pcb_is_stale:
            conversation.append({
                "role": "user",
                "content": (
                    "Your schematic has changed since the last PCB layout was built. "
                    "The PCB/DRC results you have are stale and do not reflect your "
                    "latest schematic changes. Call build_and_check_pcb again with the "
                    "current netlist_path before proceeding."
                )
            })
            continue

        # A component listed in components_missing_footprint was NOT placed
        # on the board at all, even if DRC otherwise reports zero violations
        # (DRC only checks what IS placed). This must block progress
        # unconditionally -- it has caused the main IC of a design (e.g. the
        # microcontroller itself) to be silently absent from a "resolved"
        # board in practice.
        latest_missing_footprint = None
        for h in reversed(state.history):
            for tr in h.get("tool_results", []):
                out = tr.get("output", {}) or {}
                if tr["name"] == "build_and_check_pcb":
                    latest_missing_footprint = (out.get("layout") or {}).get("components_missing_footprint")
                    break
                if tr["name"] == "generate_pcb_layout":
                    latest_missing_footprint = out.get("components_missing_footprint")
                    break
            if latest_missing_footprint is not None:
                break
        if latest_missing_footprint:
            conversation.append({
                "role": "user",
                "content": (
                    f"BLOCKED: the following components have no footprint and were NOT "
                    f"placed on the board: {latest_missing_footprint}. A design cannot be "
                    f"considered complete while required components are physically absent "
                    f"from the PCB. Fix the footprint reference for each of these parts "
                    f"(re-run search_footprint_library if needed) and rebuild the PCB."
                )
            })
            continue

        if not drc_called:
            if spice_status == "unavailable":
                conversation.append({
                    "role": "user",
                    "content": (
                        "run_spice_simulation could not verify this circuit (no compatible "
                        "SPICE model was found for one or more parts). Do not fabricate a "
                        "model or silently proceed. Instead, follow this order: "
                        "(1) Call search_reference_design and/or fetch_and_extract_schematic_data "
                        "for each part whose electrical parameters matter for safety (voltage, "
                        "current, power ratings) if you have not already cited a source for them "
                        "in this conversation. Do not reuse memorized datasheet values without a "
                        "citation from this session. "
                        "(2) Only after citing those values, call the relevant calc_* tools "
                        "using the values you just looked up (not memory) to verify steady-state "
                        "current/voltage/power. "
                        "(3) State explicitly in your response that SPICE verification is "
                        "unavailable for this part, and summarize which datasheet values (with "
                        "source) were used in the calculator checks. "
                        "(4) Continue with generate_pcb_layout and run_drc_check. This design "
                        "will be marked as SPICE-unverified in the final output."
                    )
                })
                continue
            conversation.append({
                "role": "user",
                "content": (
                    "The schematic was generated, but you have not run generate_pcb_layout "
                    "and run_drc_check yet. Continue the pipeline: generate the PCB layout, "
                    "then call run_drc_check before finishing."
                )
            })
            continue

        drc_result = None
        for h in reversed(state.history):
            for tr in h.get("tool_results", []):
                if tr["name"] == "run_drc_check":
                    drc_result = tr["output"]
                    break
                if tr["name"] == "build_and_check_pcb":
                    drc_result = (tr.get("output") or {}).get("drc")
                    break
            if drc_result:
                break

        if drc_result is None:
            # run_drc_check was never called -- should not reach here due to
            # the drc_called guard above, but fail safe just in case.
            conversation.append({
                "role": "user",
                "content": "run_drc_check has not been called. Call it before finishing."
            })
            continue

        if drc_result.get("success") is not True:
            # The DRC tool itself failed to execute (e.g. kicad-cli version
            # mismatch, missing binary). This is NOT the same as "zero
            # violations" and must not be silently treated as a pass.
            state.escalated_to_human = True
            state.unresolved_reason = (
                f"run_drc_check could not be executed on this system: "
                f"{drc_result.get('error', 'unknown error')}. Design cannot be "
                f"automatically verified and must be reviewed manually before use."
            )
            break

        pcb_path = None
        for h in reversed(state.history):
            for tr in h.get("tool_results", []):
                if tr["name"] == "generate_pcb_layout" and tr.get("output", {}).get("success"):
                    pcb_path = tr["output"].get("pcb_path")
                    break
                if tr["name"] == "build_and_check_pcb":
                    layout_out = (tr.get("output") or {}).get("layout") or {}
                    if layout_out.get("success"):
                        pcb_path = layout_out.get("pcb_path")
                        break
            if pcb_path:
                break

        if pcb_path and os.path.exists(pcb_path):
            with open(pcb_path, encoding="utf-8") as f:
                pcb_content = f.read()
            footprint_count = pcb_content.count("(footprint ")
            if footprint_count == 0:
                # DRC "0 violations" on a board with no placed components
                # verifies nothing. Do not treat this as a pass.
                conversation.append({
                    "role": "user",
                    "content": (
                        "The PCB has 0 placed footprints. DRC passing with zero "
                        "violations on an empty board verifies nothing. You must "
                        "place the actual components (not just the outline/holes) "
                        "before DRC can be considered meaningful."
                    )
                })
                continue

        raw_violations = drc_result.get("violations", [])
        raw_violation_types = {v.get("type", "unknown") for v in raw_violations}
        if raw_violations and raw_violation_types and all(
            _classify_violation(v) not in CRITICAL_FIXED_LIST and v.get("type", "unknown") in cleared_warning_types
            for v in raw_violations
        ):
            # Every remaining violation is a non-critical type already
            # independently reviewed and cleared earlier this session (e.g.
            # a cosmetic silkscreen-overlap warning that keeps recurring
            # after board resizes). Treat this DRC result as effectively
            # clean and fall through to the functional-completeness check
            # below, instead of bouncing the Designer back to rebuild the
            # PCB for a warning that will never actually go away and was
            # already judged safe.
            state.history.append({
                "iteration": state.iteration, "role": "auto_cleared_warning",
                "response": f"types {sorted(raw_violation_types)} previously cleared this session, treating DRC as clean",
            })
            effective_violation_count = 0
        else:
            effective_violation_count = drc_result.get("violation_count", 0)

        if effective_violation_count > 0:
            critical_violations = []
            minor_violations = []
            for v in drc_result.get("violations", []):
                category = _classify_violation(v)
                if category in CRITICAL_FIXED_LIST:
                    critical_violations.append((category, v))
                else:
                    minor_violations.append(v)

            if critical_violations:
                conversation.append({
                    "role": "user",
                    "content": (
                        f"CRITICAL violation(s) detected, must be fixed: "
                        f"{json.dumps(critical_violations, ensure_ascii=False)}"
                    ),
                })
                continue

            if minor_violations:
                minor_types = sorted({v.get("type", "unknown") for v in minor_violations})

                critic_resp = client.call_critic(
                    critic_system_prompt,
                    conversation + [{
                        "role": "user",
                        "content": (
                            f"Independently verify these minor DRC warnings ONLY: "
                            f"{json.dumps(minor_violations, ensure_ascii=False)}\n\n"
                            "Scope restriction: judge ONLY whether these specific warnings are "
                            "safe to dismiss (manufacturing cosmetic issues vs real electrical "
                            "risk). Do NOT comment on functional completeness (whether all "
                            "requested parts are present) in this response -- that is checked "
                            "separately with the authoritative component list, and any opinion "
                            "you form here from conversation memory rather than that list would "
                            "be a guess, not a verified fact.\n\n"
                            "End your response with exactly one line in this exact format "
                            "(no other text on that line): VERDICT: CLEAR or VERDICT: REJECT"
                        )
                    }],
                    ALL_CRITIC_TOOLS,
                )
                if critic_resp.get("error"):
                    state.history.append({
                        "iteration": state.iteration, "role": "api_error",
                        "response": critic_resp["error"],
                    })
                    continue
                state.history.append({"iteration": state.iteration, "role": "critic", "response": critic_resp["content"]})

                if critic_resp["tool_calls"]:
                    tool_results = _run_tool_calls(critic_resp["tool_calls"])
                    state.history.append({"iteration": state.iteration, "role": "critic_tools", "tool_results": tool_results})

                content = critic_resp["content"] or ""
                verdict_match = re.search(r"VERDICT:\s*(CLEAR|REJECT)", content, re.IGNORECASE)
                if verdict_match and verdict_match.group(1).upper() == "CLEAR":
                    # Minor warnings dismissed, but this alone does NOT mean
                    # the design is complete -- fall through to the next
                    # loop iteration, where the mandatory DRC-zero /
                    # functional-completeness check (with the authoritative
                    # component list) still has to pass before state.resolved
                    # is set. Do not set state.resolved here.
                    for t in minor_types:
                        if t not in cleared_warning_types:
                            cleared_warning_types.append(t)
                    conversation.append({
                        "role": "user",
                        "content": (
                            "The minor DRC warnings were reviewed and cleared. Re-run "
                            "build_and_check_pcb to confirm zero violations remain, so the "
                            "final functional completeness check can proceed."
                        )
                    })
                    continue
                else:
                    if not verdict_match:
                        # Critic didn't follow the required format -- treat as
                        # unresolved rather than silently looping forever on
                        # a natural-language guess that never matches.
                        content += "\n(No VERDICT line found; treated as REJECT by default.)"
                    conversation.append({
                        "role": "user",
                        "content": f"Critic rejected the warning dismissal. Fix required: {content}"
                    })
                    continue
            else:
                state.resolved = True
        else:
            # DRC found zero violations, but that only verifies physical/
            # electrical layout rules -- it says nothing about whether the
            # circuit actually contains the functional parts the user asked
            # for (e.g. an LED could be entirely missing from the netlist
            # while DRC still reports zero violations on the empty result).
            # A final Critic review is mandatory every time DRC comes back
            # clean, not just once -- otherwise a prior FAIL could be
            # silently treated as "already reviewed" while the flaw it
            # found is never actually fixed.
            actual_placed_components = None
            for h in reversed(state.history):
                for tr in h.get("tool_results", []):
                    if tr["name"] == "build_and_check_pcb":
                        actual_placed_components = (tr.get("output") or {}).get("layout", {}).get("components_placed")
                        break
                if actual_placed_components is not None:
                    break

            critic_resp = client.call_critic(
                critic_system_prompt,
                conversation + [{
                    "role": "user",
                    "content": (
                        "DRC passed with zero violations. Before declaring this design "
                        "complete, perform the functional completeness check.\n\n"
                        f"AUTHORITATIVE list of components actually placed on the board "
                        f"(from the tool output, not your memory of the conversation): "
                        f"{json.dumps(actual_placed_components, ensure_ascii=False)}\n\n"
                        "Compare this exact list against the user's original request. Do "
                        "not assume a part exists because it was discussed earlier -- if "
                        "it is not in this list, it is not on the board. Does this list "
                        "contain every functional part the user's original request implies "
                        "(e.g. an LED if the user asked for a light, a battery holder/"
                        "connector part if the user asked for battery power)? List which "
                        "required parts are present and which are missing from this exact "
                        "list, then end your response with exactly one line in this exact "
                        "format (no other text on that line): VERDICT: PASS or VERDICT: FAIL"
                    )
                }],
                ALL_CRITIC_TOOLS,
                phase="escalate",
            )
            if critic_resp.get("error"):
                state.history.append({
                    "iteration": state.iteration, "role": "api_error",
                    "response": critic_resp["error"],
                })
                continue
            state.history.append({
                "iteration": state.iteration, "role": "critic_final_review",
                "response": critic_resp["content"],
            })
            content = critic_resp["content"] or ""
            verdict_match = re.search(r"VERDICT:\s*(PASS|FAIL)", content, re.IGNORECASE)
            if verdict_match and verdict_match.group(1).upper() == "FAIL":
                conversation.append({
                    "role": "user",
                    "content": f"Critic found a functional completeness failure: {content}"
                })
                continue
            if not verdict_match:
                # No parseable verdict -- do not silently pass; ask again
                conversation.append({
                    "role": "user",
                    "content": (
                        "Your functional completeness review did not include a "
                        "'VERDICT: PASS' or 'VERDICT: FAIL' line. Please redo it and "
                        "include that exact line."
                    )
                })
                continue
            state.resolved = True

    state.spice_verified = {"verified": True, "unavailable": False, None: None}[spice_status]
    state.conversation = conversation
    state.schematic_generated = schematic_generated
    state.spice_status = spice_status
    state.last_verified_netlist_hash = last_verified_netlist_hash
    state.recent_call_signatures = recent_call_signatures
    state.cleared_warning_types = cleared_warning_types

    if not state.resolved:
        state.escalated_to_human = True
        if not state.unresolved_reason:
            # Only set the generic "ran out of iterations" message if
            # nothing more specific was already set. This was overwriting
            # the [UNRECOVERABLE] marker from the total_413_count/
            # no_tool_call_streak escalations every single time (they set
            # state.resolved=False and break, then execution fell through
            # to here unconditionally) -- which is why those escalations
            # appeared to never fire from run.py's perspective, even though
            # the underlying counters clearly show they were triggering
            # internally (confirmed via the _tick diagnostic: total_413_count
            # exceeded 700 across resumed runs because run.py kept seeing
            # the generic resumable message and auto-continuing forever).
            state.unresolved_reason = f"unresolved after {state.iteration} auto-fix iterations (resumable)"

    return state
