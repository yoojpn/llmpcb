"""
Minimal audit loop. Deliberately stripped of every guard/nudge/compression/
offload mechanism accumulated in orchestrator.py -- those, layered one on
top of another over a long debugging session, made the system slower and
more failure-prone, not more reliable. This is the opposite bet: the
smallest possible loop, a low hard iteration cap, and nothing else. Accept
lower success/quality for now; add back exactly one guard at a time, only
once this baseline is confirmed to work.
"""
from __future__ import annotations
import os
import sys
import json
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import calculators, research
from kicad_utils import kicad_wrapper
from agents.gemini_client import LLMPCBGeminiClient

MAX_ITERATIONS = 20

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_reference_design",
            "description": "Find a part's datasheet URL and reference design info.",
            "parameters": {
                "type": "object",
                "properties": {"part_number": {"type": "string"}, "manufacturer": {"type": "string"}, "doc_type": {"type": "string"}},
                "required": ["part_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_and_extract_schematic_data",
            "description": "Fetch a specific section (e.g. pin configuration, pinout) from a datasheet URL.",
            "parameters": {
                "type": "object",
                "properties": {"document_url": {"type": "string"}, "target_section": {"type": "string"}},
                "required": ["document_url", "target_section"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_footprint_library",
            "description": "Find a real footprint/symbol for a specific manufacturer part number.",
            "parameters": {
                "type": "object",
                "properties": {"part_number": {"type": "string"}, "manufacturer": {"type": "string"}},
                "required": ["part_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calc_led_resistor",
            "description": "Calculate the series resistor for an LED.",
            "parameters": {
                "type": "object",
                "properties": {
                    "supply_voltage_v": {"type": "number"},
                    "led_forward_voltage_v": {"type": "number"},
                    "led_forward_current_ma": {"type": "number"},
                },
                "required": ["supply_voltage_v", "led_forward_voltage_v", "led_forward_current_ma"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_and_simulate_schematic",
            "description": "Generate the schematic/netlist from SKiDL code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skidl_code": {"type": "string"},
                    "output_name": {"type": "string"},
                },
                "required": ["skidl_code", "output_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_and_check_pcb",
            "description": "Place components on a PCB of the given size and run DRC. If DRC reports a spacing-related violation between DIFFERENT components (clearance, solder_mask_bridge, courtyards_overlap) -- NOT a schematic/netlist issue -- increase part_clearance_mm and call this again; editing the SKiDL schematic code cannot fix physical placement spacing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "netlist_path": {"type": "string"},
                    "board_width_mm": {"type": "number"},
                    "board_height_mm": {"type": "number"},
                    "part_clearance_mm": {"type": "number", "description": "Minimum gap between placed components, default 3.0mm. Increase this (e.g. to 4-5mm) if DRC reports clearance/solder-mask/courtyard violations between different components."},
                },
                "required": ["netlist_path"],
            },
        },
    },
]

DISPATCH = {
    "search_footprint_library": research.search_footprint_library,
    "search_reference_design": research.search_reference_design,
    "fetch_and_extract_schematic_data": research.fetch_and_extract_schematic_data,
    "calc_led_resistor": calculators.calc_led_resistor,
    "build_and_simulate_schematic": kicad_wrapper.build_and_simulate_schematic,
    "build_and_check_pcb": kicad_wrapper.build_and_check_pcb,
}

SYSTEM_PROMPT = """You design a KiCad PCB from a natural-language request using SKiDL.
Tools: search_footprint_library (real part footprints), calc_led_resistor,
build_and_simulate_schematic (SKiDL code -> netlist), build_and_check_pcb
(place components + DRC). Search real part numbers, never guess footprints.
Use KiCad standard library (Part("Device", "R", footprint=...)) for plain
resistors/capacitors/LEDs -- no search needed for those. Work efficiently:
batch independent searches in one turn when possible. Aim to finish in as
few turns as possible.

When calling Part(symbol_file, ..., footprint=...), the second positional
argument must be search_footprint_library's `symbol_name` field (the
actual part name defined inside the .kicad_sym file), NOT `footprint_ref`
or any part of it -- these are different things (footprint_ref is a
"library:footprint" string describing the physical pad layout, symbol_name
is the schematic symbol's name) and using the wrong one causes "Unable to
find part X in library Y" even though the search itself succeeded.

If using a USB-C connector, CC1 and CC2 pins each need their OWN 5.1kOhm
resistor to GND (as separate, independent resistors -- never share one
resistor between them, and never leave them unconnected). Without this,
most modern USB-C power sources will not apply VBUS at all, since CC
termination is how a sink identifies itself as safe to power (this exact
omission was the real Raspberry Pi 4B launch bug).

SKiDL connection syntax: `net += part[pin_number]` connects a Net to a
specific pin by its NUMBER (an int or str like 1, "1", "VBUS") -- always
index into the Part with [ ] to get a Pin object first. Never write
`net += some_number` or `net += some_float` directly; that is not a pin
reference and will crash with "TypeError: 'float' object is not iterable".
Check a connector's actual pin numbers/names from its footprint search
result before wiring it, rather than guessing pin numbers.

For simple series chains (e.g. power -> resistor -> LED -> ground), PREFER
SKiDL's chain operator over manual pin-by-pin +=: `vcc & r1 & led1 & gnd`
connects them in series automatically and is much harder to get wrong than
`r1[1] += vcc; r1[2] += led1[1]; led1[2] += gnd` by hand -- manual
pin-by-pin wiring has caused a real bug where a resistor ended up with
BOTH pins on the same net, silently doing nothing electrically while
still passing DRC. A 2-pin component with both pins on the same net is
always a bug -- double check any manual wiring for this before finishing.
Include ERC() in your script before generate_netlist (auto-inserted if
you forget, but read its output yourself for unconnected-pin warnings).

If build_and_check_pcb reports clearance violations, call it again with a
LARGER board_width_mm/board_height_mm (e.g. +10mm) to give components more
room -- do not just describe the problem in text, take the corrective
action yourself.

IMPORTANT: DRC violations between DIFFERENT components -- clearance,
solder_mask_bridge, courtyards_overlap -- are PHYSICAL PLACEMENT issues,
not schematic/netlist issues. Rewriting the SKiDL code and regenerating
the schematic does NOT change where components are physically placed on
the board and cannot fix these. Instead, call build_and_check_pcb again
with a larger part_clearance_mm (e.g. 4-5mm instead of the 3mm default).
Only edit the SKiDL schematic for genuinely electrical/connectivity
problems (wrong pins, missing parts, wrong values).

If search_footprint_library returns symbol_file: null (footprint found but
no KiCad symbol/pin data) for a common/generic part (connector, header,
etc), first check whether it already exists in KiCad's own standard
libraries (Connector, Device, etc) before searching the web for a
datasheet -- KiCad ships symbols for most standard connectors with
correct, documented pins. If search_footprint_library DID return a
datasheet_url (from LCSC, alongside a real symbol), you already have both
pin data (from the symbol) and the datasheet if you need electrical specs
-- no separate search_reference_design call is needed for that same part.

board_width_mm/board_height_mm are OPTIONAL on build_and_check_pcb -- if
you omit them, the tool computes the actual required size itself and uses
a reasonably-sized board automatically. Only pass explicit dimensions if
the user specified a maximum board/enclosure size to check the design
against. If the user's specified max size doesn't fit (compare against
the returned required_width_mm/required_height_mm) even after a retry or
two, do NOT keep looping -- state clearly that it doesn't fit, report the
actual minimum size needed, and either use smaller-footprint part variants
or proceed with the smallest size that actually fits, explaining the
tradeoff.

If build_and_check_pcb reports minor clearance violations (a fraction of a
mm short of the required spacing) even when board size was auto-computed,
call it again but this time pass EXPLICIT board_width_mm/board_height_mm
a few mm larger than the required_width_mm/required_height_mm the
previous call reported -- the auto-sizer's estimate can be right at the
edge of what fits. Do not just describe the violation in text without
taking this corrective action."""


def _interview_if_needed(user_request: str, client) -> str:
    """LLMPCB itself (not the person testing/developing it) checks whether
    the request is missing critical information the user should decide --
    power source and board size -- and asks the actual end user running
    this CLI tool via real terminal prompts. Each item is checked and
    asked independently, so specifying one doesn't skip asking the other.
    If the request already specifies an item, that question is skipped.
    """
    def _is_missing(topic: str, description: str) -> bool:
        check_prompt = (
            f"You are checking whether a circuit design request specifies {description}. "
            f"Reply with exactly one word: 'SPECIFIED' if the request already states this, "
            f"or 'MISSING' if it does not."
        )
        resp = client.call_interviewer(check_prompt, [{"role": "user", "content": user_request}])
        return "MISSING" in (resp.get("content") or "").strip().upper()

    if _is_missing("power", "a power source (USB, battery type, etc)"):
        print("\n[LLMPCB] この回路の電源方式が指定されていません。")
        print("  1) USB給電(5V)")
        print("  2) コイン電池(3V)")
        print("  3) 単三電池2本(3V)")
        print("  4) おまかせ(USB給電)")
        choice = input("番号を選んでください [1-4]: ").strip()
        power_map = {
            "1": "USB給電(5V)", "2": "コイン電池(3V)", "3": "単三電池2本(3V)", "4": "USB給電(5V、おまかせ)",
        }
        power = power_map.get(choice, "USB給電(5V、おまかせ)")
        print(f"[LLMPCB] 電源方式: {power} で設計します。\n")
        user_request = f"{user_request} 電源は{power}を使用すること。"

    if _is_missing("board size", "a target board/enclosure size (dimensions in mm)"):
        print("[LLMPCB] 基板サイズの希望はありますか?")
        print("  1) 指定なし(部品構成に応じてAIが提案)")
        print("  2) 30x30mm以内")
        print("  3) 50x50mm以内")
        print("  4) 自分でmm数を入力")
        size_choice = input("番号を選んでください [1-4]: ").strip()
        if size_choice == "2":
            size_text = "基板サイズは30x30mm以内に収めること。"
        elif size_choice == "3":
            size_text = "基板サイズは50x50mm以内に収めること。"
        elif size_choice == "4":
            w = input("横幅(mm): ").strip()
            h = input("縦幅(mm): ").strip()
            size_text = f"基板サイズは{w}x{h}mm以内に収めること。"
        else:
            size_text = "基板サイズの指定なし。部品構成から必要な最小サイズを自動算出すること。"
        print(f"[LLMPCB] 基板サイズ: {size_text}\n")
        user_request = f"{user_request} {size_text}"

    return user_request


def _verify_against_datasheets(final_components: list[dict], nets_info: dict, client) -> tuple[bool, str]:
    """Generic (not connector-type-specific) wiring verification: for each
    non-trivial component (skip simple R/C/L/LED passives), re-fetch its
    real datasheet and ask the model to check the ACTUAL net connections
    against that SPECIFIC part's documented requirements -- mandatory
    pull resistors, decoupling, enable-pin handling, unused-pin treatment,
    etc. This generalizes the USB-C CC1/CC2 case (found via manual audit)
    to any part with its own special wiring requirements, without needing
    to hand-write a check for every connector/IC family in existence --
    the datasheet text itself is the source of truth each time, not
    baked-in domain knowledge about one specific connector type.
    """
    import re
    findings = []
    for comp in final_components:
        ref, value = comp.get("ref", ""), comp.get("value", "")
        if re.fullmatch(r"R|C|L|LED|Fuse|D", value or "") or not value:
            continue  # skip simple passives with no generic part number to look up
        try:
            search_result = research.search_footprint_library(value, "")
        except Exception:
            continue
        datasheet_url = search_result.get("datasheet_url") if search_result else None
        if not datasheet_url:
            continue
        try:
            extracted = research.fetch_and_extract_schematic_data(
                datasheet_url, "typical application circuit, pin description, unused pin handling"
            )
        except Exception:
            continue
        excerpt = (extracted or {}).get("raw_text_excerpt") or (extracted or {}).get("content") or ""
        if not excerpt:
            continue
        actual_conn = {net: pins for net, pins in nets_info.items() if any(r == ref for r, _ in pins)}
        findings.append({"ref": ref, "part": value, "actual_connections": actual_conn, "datasheet_excerpt": excerpt[:2000]})

    if not findings:
        return True, "No non-passive parts with fetchable datasheets to cross-check; nothing to verify here."

    prompt = (
        "For each component below, compare its ACTUAL net connections against what its OWN "
        "datasheet excerpt says is required (mandatory pull resistors/terminations, decoupling "
        "capacitors, how to handle an enable/unused pin, etc). Flag anything the datasheet "
        "says is required but is missing or wired incorrectly in the actual connections -- be "
        "specific about which pin and which requirement. If everything checks out, say so. "
        "End with exactly one line: 'VERDICT: PASS' or 'VERDICT: FAIL'."
    )
    resp = client.call_interviewer(prompt, [{"role": "user", "content": json.dumps(findings, ensure_ascii=False, default=str)[:8000]}])
    text = resp.get("content") or ""
    m = re.search(r"VERDICT:\s*(PASS|FAIL)", text, re.IGNORECASE)
    passed = bool(m and m.group(1).upper() == "PASS")
    return passed, text


def _extract_requirements(user_request: str, client) -> str:
    """Before design starts, think through what "done" actually requires --
    not just power source (already handled by the interview step) but the
    functional behavior itself. E.g. "LED blinker" implies an oscillator
    (555 timer, microcontroller, astable circuit) is REQUIRED -- a steady-on
    LED with just a resistor satisfies zero DRC violations but does not
    satisfy the actual request. This checklist is injected into the design
    prompt so the Designer keeps it in view throughout, and is checked again
    against the ACTUAL final component list before declaring success (see
    _verify_requirements).
    """
    prompt = (
        "Given this circuit request, list the SHORT set of concrete functional "
        "requirements a correct design must satisfy -- especially anything "
        "implied but not stated outright (e.g. 'blinker'/'blinking' implies "
        "an oscillator component like a 555 timer or microcontroller is "
        "required; a resistor+LED alone would just be steady-on, not "
        "blinking). Be concrete about what component TYPE would satisfy each "
        "requirement. Keep it to 2-4 bullet points, no more."
    )
    resp = client.call_interviewer(prompt, [{"role": "user", "content": user_request}])
    return resp.get("content") or ""


def _verify_requirements(requirements: str, final_components: list[dict], client,
                          netlist_path: str = None, advisory_warnings: list = None) -> tuple[bool, str]:
    """One-shot final check: does the ACTUAL component list (from the real
    generated netlist, not the model's memory of the conversation) satisfy
    the requirements extracted at the start? Also passes the actual net
    connections and any advisory wiring warnings, so this check can catch
    real wiring mistakes -- e.g. a rotary encoder's channel A accidentally
    tied to two different MCU GPIOs simultaneously (shorting them), or a
    required push-button function left completely unwired -- not just
    "is the right component type present" (a part can be present and still
    be wired wrong, as found via manual datasheet-trace audit in practice).
    Returns (passed, explanation).
    """
    prompt = (
        "Compare this list of functional requirements against the ACTUAL "
        "components AND their actual net connections in the final board "
        "(from the real generated netlist, not memory). Check not just "
        "that the right component TYPES are present, but that they are "
        "actually wired correctly for their stated purpose -- e.g. if a "
        "rotary encoder is required, check its A/B/common pins each go to "
        "a DIFFERENT net (not the same net as each other, and not shorted "
        "to two different MCU pins simultaneously), and that any required "
        "button/switch function has its pins actually connected to "
        "something, not left floating. Advisory warnings (if any) flag "
        "specific pins that may be shorted together -- use your judgment "
        "on whether each is a real bug or a legitimate design pattern "
        "(e.g. tying an LDO's EN pin to VIN to keep it always-enabled is "
        "correct, not a bug). Reply with a short explanation, then end "
        "with exactly one line: 'VERDICT: PASS' if everything is correct, "
        "or 'VERDICT: FAIL' if a real problem is found."
    )
    nets_info = kicad_wrapper.get_netlist_nets(netlist_path) if netlist_path else {}
    content = (
        f"Requirements:\n{requirements}\n\n"
        f"Actual final components: {json.dumps(final_components, ensure_ascii=False)}\n\n"
        f"Actual net connections: {json.dumps(nets_info, ensure_ascii=False)}\n\n"
        f"Advisory warnings (may or may not be real bugs, use judgment): "
        f"{json.dumps(advisory_warnings or [], ensure_ascii=False)}"
    )
    resp = client.call_interviewer(prompt, [{"role": "user", "content": content}])
    text = resp.get("content") or ""
    import re
    m = re.search(r"VERDICT:\s*(PASS|FAIL)", text, re.IGNORECASE)
    passed = bool(m and m.group(1).upper() == "PASS")
    return passed, text


def _run_batch(user_request: str, client, conversation: list, requirements: str,
                start_iteration: int, batch_size: int) -> dict:
    history = []

    for i in range(start_iteration, start_iteration + batch_size):
        print(f"[iteration {i}]", flush=True)
        resp = client.call_designer(SYSTEM_PROMPT, conversation, TOOLS, phase="light")
        if resp.get("error"):
            error_text = resp["error"]
            if "429" in error_text or "RESOURCE_EXHAUSTED" in error_text:
                # This is a simple RPM (requests-per-minute) limit, not a
                # request-too-large problem -- the error message itself
                # tells us exactly how long to wait. Honor it and retry the
                # SAME turn rather than burning an iteration on a doomed call.
                import re
                import time
                m = re.search(r"retry in ([\d.]+)s", error_text)
                wait_s = float(m.group(1)) + 2 if m else 15
                print(f"  RPM limit hit, waiting {wait_s:.0f}s before retry (not counted as an iteration)", flush=True)
                time.sleep(wait_s)
                resp = client.call_designer(SYSTEM_PROMPT, conversation, TOOLS, phase="light")
            if resp.get("error"):
                history.append({"iteration": i, "error": resp["error"]})
                conversation.append({"role": "assistant", "content": ""})
                conversation.append({"role": "user", "content": f"Tool error: {resp['error']}. Try again."})
                continue

        # Cap the Designer's own text response before storing it in
        # conversation -- unbounded explanatory text (e.g. restating SKiDL
        # code, long reasoning) added every single turn was found to be a
        # second, independent contributor to the OOM growth alongside the
        # uncapped `history` results (fixed separately). 3000 chars mirrors
        # the cap already used for tool results below.
        assistant_text = (resp["content"] or "")[:3000]
        conversation.append({"role": "assistant", "content": assistant_text})

        if not resp["tool_calls"]:
            history.append({"iteration": i, "note": "no tool call", "content": resp["content"]})
            last_drc = next((h for h in reversed(history) if "drc_clean" in h), None)
            if last_drc and last_drc["drc_clean"]:
                final_components = last_drc.get("components", [])
                passed, explanation = _verify_requirements(
                    requirements, final_components, client,
                    netlist_path=last_drc.get("netlist_path"),
                    advisory_warnings=last_drc.get("advisory_warnings"),
                )
                print(f"[LLMPCB] 機能要件の最終確認: {'PASS' if passed else 'FAIL'}")
                history.append({"iteration": i, "requirement_check": passed, "explanation": explanation})
                if passed:
                    nets_info = kicad_wrapper.get_netlist_nets(last_drc.get("netlist_path")) if last_drc.get("netlist_path") else {}
                    ds_passed, ds_explanation = _verify_against_datasheets(final_components, nets_info, client)
                    print(f"[LLMPCB] データシート照合検証: {'PASS' if ds_passed else 'FAIL'}")
                    history.append({"iteration": i, "datasheet_check": ds_passed, "explanation": ds_explanation})
                    if ds_passed:
                        return {"resolved": True, "iterations": i, "history": history, "conversation": conversation}
                    conversation.append({
                        "role": "user",
                        "content": (
                            f"Functional requirements are satisfied, but a datasheet cross-check on the "
                            f"actual wiring found a problem: {ds_explanation}\nFix the SKiDL code accordingly, "
                            f"then rebuild the schematic and PCB."
                        )
                    })
                    continue
                conversation.append({
                    "role": "user",
                    "content": (
                        f"DRC is clean, but the design does not satisfy the stated functional "
                        f"requirements: {explanation}\nFix the SKiDL code to add what's missing, "
                        f"then rebuild the schematic and PCB."
                    )
                })
                continue
            if last_drc and last_drc.get("missing_footprint"):
                conversation.append({
                    "role": "user",
                    "content": (
                        f"The following components have no footprint and were NOT physically "
                        f"placed on the board, even though DRC may show 0 violations (DRC only "
                        f"checks what IS placed): {last_drc['missing_footprint']}. Search for a "
                        f"real footprint for each of these parts (search_footprint_library or "
                        f"KiCad standard library), fix the SKiDL code, and rebuild."
                    )
                })
                continue
            if last_drc and last_drc.get("routing_failed"):
                conversation.append({
                    "role": "user",
                    "content": (
                        "The board was placed and DRC-checked, but auto-routing (copper trace "
                        "generation) did not complete -- the board has NO actual electrical "
                        "connections between components yet, even though DRC reported 0 "
                        "violations (there was nothing to check connectivity on). Call "
                        "build_and_check_pcb again on the same netlist to retry routing."
                    )
                })
                continue
            conversation.append({"role": "user", "content": "Continue -- call the next tool needed."})
            continue

        results = []
        for tc in resp["tool_calls"]:
            if tc["name"] == "build_and_check_pcb" and "netlist_path" in tc["arguments"]:
                # Always use the MOST RECENTLY generated netlist file,
                # regardless of what path the Designer specified -- this
                # closes a real bug: the Designer fixed a wiring mistake in
                # the schematic (regenerating a newer netlist), but then
                # called build_and_check_pcb with the OLD netlist_path from
                # an earlier turn, so DRC/shorted-pin checks silently
                # verified stale data while the LATEST board (checked
                # separately after the run) still had the original bug.
                candidates = sorted(Path("_work").glob("*.net"), key=lambda p: p.stat().st_mtime, reverse=True)
                if candidates:
                    tc["arguments"]["netlist_path"] = str(candidates[0])
            fn = DISPATCH.get(tc["name"])
            try:
                out = fn(**tc["arguments"]) if fn else {"error": f"unknown tool {tc['name']}"}
            except Exception as e:
                out = {"error": str(e)}
            results.append({"name": tc["name"], "output": out, "args": tc["arguments"]})

            if tc["name"] == "build_and_check_pcb":
                drc = out.get("drc") or {}
                layout = out.get("layout") or {}
                routing = out.get("routing") or {}
                netlist_path = tc["arguments"].get("netlist_path")
                ref_values = kicad_wrapper.get_netlist_ref_values(netlist_path) if netlist_path else []
                missing_fp = layout.get("components_missing_footprint") or []
                # Purely cosmetic/manufacturing-appearance DRC types that
                # don't affect whether the board is electrically correct
                # or physically assemblable -- treated as non-blocking so
                # the Designer isn't stuck re-submitting the same layout
                # forever chasing a warning that generally can't be fully
                # eliminated by the auto-placement algorithm anyway.
                COSMETIC_DRC_TYPES = {"silk_over_copper", "silk_overlap"}
                blocking_violations = [
                    v for v in drc.get("violations", [])
                    if v.get("type") not in COSMETIC_DRC_TYPES
                ]
                # A previous run reported "resolved: true" with a board
                # that had ZERO copper traces and 40 unconnected items --
                # routing had timed out (freerouting didn't finish within
                # 90s), but DRC still reported violation_count=0 because
                # nothing was routed yet for DRC to check connectivity on.
                # routing.success must be true for the board to actually
                # be considered done, not just DRC's violation count.
                routing_ok = routing.get("success", False)
                history.append({
                    "iteration": i,
                    "drc_clean": (
                        len(blocking_violations) == 0
                        and layout.get("success")
                        and not missing_fp
                        and routing_ok
                    ),
                    "routing_failed": (not routing_ok),
                    "missing_footprint": missing_fp,
                    "components": ref_values,
                    "netlist_path": netlist_path,
                    "advisory_warnings": drc.get("advisory_warnings", []),
                })

        conversation.append({"role": "user", "content": f"Tool results:\n{json.dumps(results, ensure_ascii=False, default=str)[:3000]}"})
        # Store a SIZE-BOUNDED summary of results in history, not the raw
        # output -- a run that hit an out-of-memory kill at iteration 55
        # (process RSS grew from ~700MB at iteration 8 to 3.7GB, exceeding
        # the 3.9GB system limit) traced back to this line: `results`
        # includes full datasheet excerpts and other large tool outputs,
        # appended to `history` (a plain Python list kept in memory for
        # the whole run) on every single iteration with no size cap at
        # all, unlike the full orchestrator which has offload/compression
        # mechanisms. This keeps history usable for debugging without the
        # unbounded growth.
        def _bound_result_sizes(obj, max_len=500):
            if isinstance(obj, str):
                return obj if len(obj) <= max_len else obj[:max_len] + f"...({len(obj)} chars total)"
            if isinstance(obj, dict):
                return {k: _bound_result_sizes(v, max_len) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_bound_result_sizes(v, max_len) for v in obj]
            return obj

        history.append({
            "iteration": i, "tools": [r["name"] for r in results],
            "results": _bound_result_sizes(results),
        })

        # Bound the conversation's message COUNT, not just per-message
        # size -- 55+ iterations each adding several messages (assistant
        # text, tool results, nudges) adds up even with per-message caps
        # in place, and this is a long-running process with no other
        # memory-management mechanism (unlike the full orchestrator's
        # Condenser/offload system). Keep the first message (original
        # request + requirements, needed for context) plus the most
        # recent messages; drop the middle once it gets long.
        MAX_CONVERSATION_MESSAGES = 40
        if len(conversation) > MAX_CONVERSATION_MESSAGES:
            conversation[:] = conversation[:1] + conversation[-(MAX_CONVERSATION_MESSAGES - 1):]

    return {
        "resolved": False,
        "iterations": start_iteration + batch_size - 1,
        "history": history,
        "conversation": conversation,
    }


def run(user_request: str) -> dict:
    client = LLMPCBGeminiClient()
    user_request = _interview_if_needed(user_request, client)
    requirements = _extract_requirements(user_request, client)
    print(f"[LLMPCB] 機能要件チェックリスト:\n{requirements}\n")
    conversation = [{
        "role": "user",
        "content": f"Design this circuit: {user_request}\n\nFunctional requirements to satisfy:\n{requirements}",
    }]

    all_history = []
    next_iteration = 1
    batch_count = 0
    MAX_BATCHES_NONINTERACTIVE = 3  # safety cap when auto-continuing unattended (60 iterations total)
    while True:
        batch_count += 1
        result = _run_batch(user_request, client, conversation, requirements, next_iteration, MAX_ITERATIONS)
        all_history.extend(result["history"])
        conversation = result["conversation"]
        if result["resolved"]:
            return {"resolved": True, "iterations": result["iterations"], "history": all_history}

        next_iteration = result["iterations"] + 1
        print(f"\n[LLMPCB] {result['iterations']}回で完成しませんでした。さらに{MAX_ITERATIONS}回続けますか?")
        if os.environ.get("LLMPCB_NONINTERACTIVE_CONTINUE"):
            if batch_count >= MAX_BATCHES_NONINTERACTIVE:
                print(f"[LLMPCB] 非対話モードの上限({MAX_BATCHES_NONINTERACTIVE}バッチ)に達したため終了します。")
                return {"resolved": False, "iterations": result["iterations"], "history": all_history}
            choice = ""
        else:
            choice = input("続ける場合は何か入力、終了する場合は 'q' [続ける/q]: ").strip().lower()
        if choice == "q":
            return {"resolved": False, "iterations": result["iterations"], "history": all_history}
        print(f"[LLMPCB] さらに{MAX_ITERATIONS}回、設計を続けます。\n")


if __name__ == "__main__":
    request = sys.argv[1] if len(sys.argv) > 1 else "Design a simple LED blinker circuit (Lチカ)."
    result = run(request)
    print(json.dumps({"resolved": result["resolved"], "iterations": result["iterations"]}, indent=2))
    with open("_work/minimal_run_log.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
