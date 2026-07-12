from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Optional

from .search_engine import web_search

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_MODEL_DIR = str(_PROJECT_ROOT / "_work" / "spice_models")


def _validate_spice_model(text: str) -> bool:
    return (".model" in text.lower()) or (".subckt" in text.lower())


def _download_model(url: str, dest_dir: str) -> Optional[str]:
    import requests
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    text = resp.content.decode("utf-8", errors="ignore")
    if not _validate_spice_model(text):
        return None
    os.makedirs(dest_dir, exist_ok=True)
    filename = os.path.basename(url.split("?")[0]) or "model.lib"
    dest_path = os.path.join(dest_dir, filename)
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(text)
    return dest_path


def _extract_model_name(text: str, part_number: str = "") -> Optional[str]:
    # Prefer a .subckt/.model whose name relates to the requested part number
    # (files with multiple internal subcircuits, e.g. IC macromodels, can
    # otherwise match an unrelated internal helper subckt like "D_D").
    if part_number:
        core = re.sub(r"[^A-Za-z0-9]", "", part_number).upper()
        for directive in (r"\.subckt", r"\.model"):
            for m in re.finditer(directive + r"\s+(\S+)", text, re.IGNORECASE):
                name = m.group(1)
                if core and core in re.sub(r"[^A-Za-z0-9]", "", name).upper():
                    return name
    m = re.search(r"\.model\s+(\S+)\s+(NPN|PNP|NMOS|PMOS|D)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"\.subckt\s+(\S+)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def search_spice_model(part_number: str, manufacturer: str = "",
                        dest_dir: str = _DEFAULT_MODEL_DIR) -> dict:
    """Search the web for a real, ngspice-compatible SPICE model file
    (.lib/.mod/.sub containing an actual .model or .subckt directive) for
    a part, download and validate it, and return the local path plus the
    .model/.subckt name to use in a netlist .include. Never fabricates
    parameters -- if nothing valid is found, returns found=False.
    """
    try:
        hits = web_search(f"{manufacturer} {part_number} spice model .lib ngspice", max_results=8)
    except Exception as e:
        return {"found": False, "error": str(e)}

    for hit in hits:
        url = hit["url"]
        if not any(url.lower().endswith(ext) for ext in (".lib", ".mod", ".sub", ".cir", ".txt")):
            continue
        if "github.com" in url and "/blob/" in url:
            url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        path = _download_model(url, dest_dir)
        if path:
            with open(path, encoding="utf-8", errors="ignore") as f:
                text = f.read()
            model_name = _extract_model_name(text, part_number)
            return {
                "found": True,
                "file_path": path,
                "model_name": model_name,
                "source_url": url,
            }

    return {"found": False, "hits_checked": [h["url"] for h in hits]}


if __name__ == "__main__":
    print(search_spice_model("2N3904"))
