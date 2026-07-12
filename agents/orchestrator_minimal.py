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
few turns as possible."""


def run(user_request: str) -> dict:
    client = LLMPCBGeminiClient()
    conversation = [{"role": "user", "content": f"Design this circuit: {user_request}"}]
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
            # check if last DRC was clean
            last_drc = next((h for h in reversed(history) if "drc_clean" in h), None)
            if last_drc and last_drc["drc_clean"]:
                return {"resolved": True, "iterations": i, "history": history}
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
                history.append({"iteration": i, "drc_clean": drc.get("violation_count") == 0 and out.get("layout", {}).get("success")})

        conversation.append({"role": "user", "content": f"Tool results:\n{json.dumps(results, ensure_ascii=False, default=str)[:3000]}"})
        history.append({"iteration": i, "tools": [r["name"] for r in results], "results": results})

    return {"resolved": False, "iterations": MAX_ITERATIONS, "history": history}


if __name__ == "__main__":
    request = sys.argv[1] if len(sys.argv) > 1 else "Design a simple LED blinker circuit (Lチカ)."
    result = run(request)
    print(json.dumps({"resolved": result["resolved"], "iterations": result["iterations"]}, indent=2))
    with open("_work/minimal_run_log.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
