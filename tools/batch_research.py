from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import research, spice_models


def batch_research_parts(parts: list[dict]) -> dict:
    """Take a list of {part_number, manufacturer, needs_spice_model,
    needs_datasheet, datasheet_sections} entries decided by the Designer in
    a single turn, and run footprint search / SPICE model search /
    datasheet search AND section extraction for ALL of them here in Python
    (in parallel, no further LLM round trips) before returning one combined
    result.

    This replaces what was previously N-to-3N separate LLM turns (one per
    tool call, one per part, one per datasheet section) with exactly one
    LLM turn for the whole discovery phase -- the dominant source of
    iteration count on multi-part boards (e.g. RP2040 + OLED + encoder +
    regulator + flash routinely needed 15+ separate search/extract turns
    beforehand).

    `datasheet_sections` (optional): list of section names to extract from
    the found datasheet in the same batched call, e.g.
    ["Pin Configuration", "Minimum system requirements"].
    """
    results = {}

    def _do_one(entry: dict) -> tuple[str, dict]:
        part_number = entry["part_number"]
        manufacturer = entry.get("manufacturer", "")
        out = {}
        out["footprint"] = research.search_footprint_library(part_number, manufacturer)
        if entry.get("needs_spice_model"):
            out["spice_model"] = spice_models.search_spice_model(part_number, manufacturer)
        if entry.get("needs_datasheet") or entry.get("datasheet_sections"):
            ref = research.search_reference_design(part_number, manufacturer, "datasheet")
            out["reference_design"] = ref
            sections = entry.get("datasheet_sections") or []
            if sections and ref.get("best_match", {}).get("url"):
                url = ref["best_match"]["url"]
                out["datasheet_sections"] = {}
                for section in sections:
                    out["datasheet_sections"][section] = research.fetch_and_extract_schematic_data(url, section)
        return part_number, out

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(parts)))) as executor:
        futures = {executor.submit(_do_one, entry): entry for entry in parts}
        for future in as_completed(futures):
            entry = futures[future]
            try:
                part_number, out = future.result()
            except Exception as e:
                part_number, out = entry["part_number"], {"error": str(e)}
            results[part_number] = out

    return results


if __name__ == "__main__":
    import json
    r = batch_research_parts([
        {"part_number": "NE555P", "manufacturer": "Texas Instruments", "needs_spice_model": True, "needs_datasheet": True},
        {"part_number": "ATtiny85-20P", "manufacturer": "Microchip", "needs_spice_model": False, "needs_datasheet": False},
    ])
    print(json.dumps(r, indent=2, default=str)[:2000])
