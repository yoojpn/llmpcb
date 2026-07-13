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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import calculators, research
from kicad_utils import kicad_wrapper
from agents.gemini_client import LLMPCBGeminiClient

MAX_ITERATIONS = 15

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
            "description": "Place components on a PCB of the given size and run DRC.",
            "parameters": {
                "type": "object",
                "properties": {
                    "netlist_path": {"type": "string"},
                    "board_width_mm": {"type": "number"},
                    "board_height_mm": {"type": "number"},
                },
                "required": ["netlist_path", "board_width_mm", "board_height_mm"],
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

SKiDL connection syntax: `net += part[pin_number]` connects a Net to a
specific pin by its NUMBER (an int or str like 1, "1", "VBUS") -- always
index into the Part with [ ] to get a Pin object first. Never write
`net += some_number` or `net += some_float` directly; that is not a pin
reference and will crash with "TypeError: 'float' object is not iterable".
Check a connector's actual pin numbers/names from its footprint search
result before wiring it, rather than guessing pin numbers.

If build_and_check_pcb reports clearance violations, call it again with a
LARGER board_width_mm/board_height_mm (e.g. +10mm) to give components more
room -- do not just describe the problem in text, take the corrective
action yourself.

If search_footprint_library returns symbol_file: null (footprint found but
no KiCad symbol/pin data) for a common/generic part (connector, header,
etc), first check whether it already exists in KiCad's own standard
libraries (Connector, Device, etc) before searching the web for a
datasheet -- KiCad ships symbols for most standard connectors with
correct, documented pins. If search_footprint_library DID return a
datasheet_url (from LCSC, alongside a real symbol), you already have both
pin data (from the symbol) and the datasheet if you need electrical specs
-- no separate search_reference_design call is needed for that same part.

For board_width_mm/board_height_mm on the first build_and_check_pcb call,
estimate a roughly SQUARE board (width close to height, not a long thin
strip) sized for the number and size of components -- e.g. for ~10 small
THT/SMD passives plus one IC and one connector, try something like 40x40mm
as a starting point, not a narrow strip. A board that's much longer than
it is wide usually means the width was set too small for the parts.

If the user specified a maximum board size and the actual required size
(from board_too_small/required_width_mm/required_height_mm) exceeds it
even after a couple of retries, do NOT keep silently retrying the same
size forever. State clearly in your response that the requested size
cannot physically fit all components, report the actual minimum size
needed, and either use smaller-footprint part variants (if available) or
proceed with the smallest size that actually fits, explaining the
tradeoff -- don't loop on an impossible constraint."""


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
            size_text = "基板サイズの指定なし。部品構成から適切なサイズ(できるだけ正方形に近い形)を提案すること。"
        print(f"[LLMPCB] 基板サイズ: {size_text}\n")
        user_request = f"{user_request} {size_text}"

    return user_request


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


def _verify_requirements(requirements: str, final_components: list[dict], client) -> tuple[bool, str]:
    """One-shot final check: does the ACTUAL component list (from the real
    generated netlist, not the model's memory of the conversation) satisfy
    the requirements extracted at the start? Returns (passed, explanation).
    """
    prompt = (
        "Compare this list of functional requirements against the ACTUAL "
        "components in the final board (from the real generated netlist, "
        "not memory). Reply with a short explanation, then end with exactly "
        "one line: 'VERDICT: PASS' if every requirement is satisfied by an "
        "actual component present, or 'VERDICT: FAIL' if anything is missing."
    )
    content = (
        f"Requirements:\n{requirements}\n\n"
        f"Actual final components: {json.dumps(final_components, ensure_ascii=False)}"
    )
    resp = client.call_interviewer(prompt, [{"role": "user", "content": content}])
    text = resp.get("content") or ""
    import re
    m = re.search(r"VERDICT:\s*(PASS|FAIL)", text, re.IGNORECASE)
    passed = bool(m and m.group(1).upper() == "PASS")
    return passed, text


def run(user_request: str) -> dict:
    client = LLMPCBGeminiClient()
    user_request = _interview_if_needed(user_request, client)
    requirements = _extract_requirements(user_request, client)
    print(f"[LLMPCB] 機能要件チェックリスト:\n{requirements}\n")
    conversation = [{
        "role": "user",
        "content": f"Design this circuit: {user_request}\n\nFunctional requirements to satisfy:\n{requirements}",
    }]
    history = []

    for i in range(1, MAX_ITERATIONS + 1):
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

        conversation.append({"role": "assistant", "content": resp["content"] or ""})

        if not resp["tool_calls"]:
            history.append({"iteration": i, "note": "no tool call", "content": resp["content"]})
            last_drc = next((h for h in reversed(history) if "drc_clean" in h), None)
            if last_drc and last_drc["drc_clean"]:
                final_components = last_drc.get("components", [])
                passed, explanation = _verify_requirements(requirements, final_components, client)
                print(f"[LLMPCB] 機能要件の最終確認: {'PASS' if passed else 'FAIL'}")
                history.append({"iteration": i, "requirement_check": passed, "explanation": explanation})
                if passed:
                    return {"resolved": True, "iterations": i, "history": history}
                conversation.append({
                    "role": "user",
                    "content": (
                        f"DRC is clean, but the design does not satisfy the stated functional "
                        f"requirements: {explanation}\nFix the SKiDL code to add what's missing, "
                        f"then rebuild the schematic and PCB."
                    )
                })
                continue
            conversation.append({"role": "user", "content": "Continue -- call the next tool needed."})
            continue

        results = []
        for tc in resp["tool_calls"]:
            fn = DISPATCH.get(tc["name"])
            try:
                out = fn(**tc["arguments"]) if fn else {"error": f"unknown tool {tc['name']}"}
            except Exception as e:
                out = {"error": str(e)}
            results.append({"name": tc["name"], "output": out})

            if tc["name"] == "build_and_check_pcb":
                drc = out.get("drc") or {}
                layout = out.get("layout") or {}
                netlist_path = tc["arguments"].get("netlist_path")
                ref_values = kicad_wrapper.get_netlist_ref_values(netlist_path) if netlist_path else []
                history.append({
                    "iteration": i,
                    "drc_clean": drc.get("violation_count") == 0 and layout.get("success"),
                    "components": ref_values,
                })

        conversation.append({"role": "user", "content": f"Tool results:\n{json.dumps(results, ensure_ascii=False, default=str)[:3000]}"})
        history.append({"iteration": i, "tools": [r["name"] for r in results], "results": results})

    return {"resolved": False, "iterations": MAX_ITERATIONS, "history": history}


if __name__ == "__main__":
    request = sys.argv[1] if len(sys.argv) > 1 else "Design a simple LED blinker circuit (Lチカ)."
    result = run(request)
    print(json.dumps({"resolved": result["resolved"], "iterations": result["iterations"]}, indent=2))
    with open("_work/minimal_run_log.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
