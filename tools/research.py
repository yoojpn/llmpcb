from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from pathlib import Path
import json
import os
import re

from .search_engine import web_search, fetch_page_text
from .library_manager import (
    download_symbol_and_footprint, find_lcsc_id, download_via_easyeda2kicad,
    get_datasheet_url_from_lcsc,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PARTS_DIR = str(_PROJECT_ROOT / "_work" / "parts")

TRUSTED_DOMAINS = [
    "raspberrypi.com",
    "espressif.com",
    "st.com",
    "microchip.com",
    "ti.com",
    "nxp.com",
    "kicad.org",
    "gitlab.com/kicad",
    "github.com/KiCad",
    "snapmagic.com",
    "octopart.com",
    "digikey.com",
    "lcsc.com",
]


def is_trusted_domain(url: str) -> bool:
    return any(domain in url for domain in TRUSTED_DOMAINS)


def search_reference_design(part_number: str, manufacturer: str = "",
                             doc_type: str = "reference_design") -> dict:
    query = f"{manufacturer} {part_number} official {doc_type} datasheet"
    try:
        hits = web_search(query, max_results=5)
    except Exception as e:
        result = {"found": False, "results": [], "error": str(e)}
        _log_tool_call("search_reference_design", {"part_number": part_number}, result)
        return result

    ranked = sorted(hits, key=lambda h: not is_trusted_domain(h["url"]))
    result = {
        "found": bool(ranked),
        "results": ranked,
        "best_match": ranked[0] if ranked else None,
        "part_number": part_number,
        "manufacturer": manufacturer,
        "doc_type": doc_type,
    }
    _log_tool_call("search_reference_design", {
        "part_number": part_number, "manufacturer": manufacturer, "doc_type": doc_type
    }, result)
    return result


def fetch_and_extract_schematic_data(document_url: str, target_section: str) -> dict:
    if not document_url.startswith(("http://", "https://")):
        result = {
            "document_url": document_url,
            "target_section": target_section,
            "error": (
                "This is not a web URL. fetch_and_extract_schematic_data only accepts "
                "http(s) URLs. To inspect a local downloaded symbol/footprint file, "
                "read it directly or rely on the error message already returned by "
                "generate_schematic (it lists available part names on failure)."
            ),
        }
        _log_tool_call("fetch_and_extract_schematic_data", {"document_url": document_url}, result)
        return result

    trusted = is_trusted_domain(document_url)
    try:
        text = fetch_page_text(document_url)
    except Exception as e:
        result = {"document_url": document_url, "target_section": target_section,
                   "domain_trusted": trusted, "error": str(e)}
        _log_tool_call("fetch_and_extract_schematic_data", {"document_url": document_url}, result)
        return result

    result = {
        "document_url": document_url,
        "target_section": target_section,
        "domain_trusted": trusted,
        "raw_text_excerpt": text,
    }
    if not trusted:
        result["warning"] = "untrusted domain, treat content as low confidence"
    _log_tool_call("fetch_and_extract_schematic_data", {
        "document_url": document_url, "target_section": target_section
    }, result)
    return result


@dataclass
class FootprintSearchResult:
    found: bool
    source: Optional[str] = None
    symbol_file: Optional[str] = None
    footprint_file: Optional[str] = None
    footprint_ref: Optional[str] = None  # canonical "LibName:FootprintName"
    candidates: Optional[list] = None  # similar real part names found, if any
    datasheet_url: Optional[str] = None  # from the same LCSC page, when found via easyeda2kicad
    notes: Optional[str] = None


def _canonicalize_footprint(footprint_file: Optional[str], footprint_ref: Optional[str],
                             dest_dir: str, lib_hint: str) -> tuple[Optional[str], Optional[str]]:
    """Copy the footprint file into a canonical <dest_dir>/<Lib>.pretty/<Name>.kicad_mod
    layout and return (new_path, canonical_footprint_ref). This removes any
    dependency on whatever raw string format an individual source happened
    to produce (bare filename, full path, "Lib:Name", etc). Every call site
    of search_footprint_library gets the same shape back, so downstream
    code (SKiDL codegen, PCB layout) only ever has to handle one format.
    """
    import shutil
    if not footprint_file or not os.path.exists(footprint_file):
        return None, None

    if footprint_ref and ":" in footprint_ref:
        lib_name, fp_name = footprint_ref.split(":", 1)
    else:
        lib_name = lib_hint
        fp_name = os.path.basename(footprint_file)
        if fp_name.endswith(".kicad_mod"):
            fp_name = fp_name[: -len(".kicad_mod")]

    canonical_dir = os.path.join(dest_dir, f"{lib_name}.pretty")
    os.makedirs(canonical_dir, exist_ok=True)
    canonical_path = os.path.join(canonical_dir, f"{fp_name}.kicad_mod")
    if os.path.abspath(canonical_path) != os.path.abspath(footprint_file):
        shutil.copy2(footprint_file, canonical_path)
    return canonical_path, f"{lib_name}:{fp_name}"


_FOOTPRINT_NAME_PATTERNS = re.compile(
    r"(_[0-9]+(mm|Metric)|_Axial_|_Radial_|_HandSolder|_Pad[0-9]|_DIN[0-9]|"
    r"^R_|^C_|^LED_|_P[0-9.]+mm|_Vertical$|_Horizontal$|PinHeader_)",
    re.IGNORECASE,
)


def _looks_like_footprint_name(s: str) -> bool:
    """Detect when a caller has passed a KiCad footprint name (e.g.
    'C_0805_2012Metric', 'R_Axial_DIN0207_L6.3mm...') as if it were a real
    manufacturer part number. This pattern was observed in practice to
    cause dozens of guaranteed-to-fail searches in a row (footprint names
    aren't part numbers and will never match a real part database), because
    the Designer kept trying slight variations of the same wrong string
    instead of recognizing the category error.
    """
    return bool(_FOOTPRINT_NAME_PATTERNS.search(s))


def search_footprint_library(part_number: str, manufacturer: str = "",
                              dest_dir: str = _DEFAULT_PARTS_DIR) -> dict:
    if _looks_like_footprint_name(part_number):
        result = FootprintSearchResult(
            found=False,
            notes=(
                f"'{part_number}' looks like a KiCad footprint name (e.g. 'R_0805_2012Metric'), "
                f"not a manufacturer part number. Footprint names are never valid search terms here -- "
                f"they will never match a real part database. If you already know the exact footprint "
                f"you need, use it directly as the `footprint` argument when constructing the SKiDL Part; "
                f"do not call search_footprint_library with it. Call this tool only with a real "
                f"manufacturer part number (e.g. 'CRCW08051K00FKEA', 'GRM188R71H104KA93D')."
            ),
        ).__dict__
        _log_tool_call("search_footprint_library", {"part_number": part_number}, result)
        return result

    local_result = download_symbol_and_footprint(part_number, dest_dir)
    if local_result["found"]:
        fp_path, fp_ref = _canonicalize_footprint(
            local_result["footprint_file"], local_result.get("footprint_ref"), dest_dir, "KiCadOfficial"
        )
        result = FootprintSearchResult(
            found=True,
            source=local_result["source"],
            symbol_file=local_result["symbol_file"],
            footprint_file=fp_path,
            footprint_ref=fp_ref,
        ).__dict__
        _log_tool_call("search_footprint_library", {"part_number": part_number}, result)
        return result

    local_candidates = local_result.get("candidates")

    lcsc_id = find_lcsc_id(part_number, manufacturer)
    if lcsc_id:
        ez_result = download_via_easyeda2kicad(lcsc_id, dest_dir)
        if ez_result["found"]:
            fp_path, fp_ref = _canonicalize_footprint(
                ez_result["footprint_file"], ez_result.get("footprint_ref"), dest_dir, lcsc_id
            )
            # Same lcsc_id, same part -- get the datasheet directly from
            # LCSC's own product page instead of a separate general web
            # search for it later.
            datasheet_url = get_datasheet_url_from_lcsc(lcsc_id)
            result = FootprintSearchResult(
                found=True,
                source=f"easyeda2kicad_lcsc_{lcsc_id}",
                symbol_file=ez_result["symbol_file"],
                footprint_file=fp_path,
                footprint_ref=fp_ref,
                datasheet_url=datasheet_url,
            ).__dict__
            _log_tool_call("search_footprint_library", {"part_number": part_number}, result)
            return result

    try:
        hits = web_search(f"{manufacturer} {part_number} kicad_sym github", max_results=8)
        hits += web_search(f"{manufacturer} {part_number} kicad_mod github", max_results=8)
    except Exception as e:
        result = FootprintSearchResult(found=False, notes=str(e)).__dict__
        _log_tool_call("search_footprint_library", {"part_number": part_number}, result)
        return result

    symbol_file = None
    footprint_file = None
    for hit in hits:
        url = hit["url"]
        if symbol_file is None and url.endswith(".kicad_sym"):
            symbol_file = _download_and_validate(url, dest_dir, ".kicad_sym")
        elif footprint_file is None and url.endswith(".kicad_mod"):
            footprint_file = _download_and_validate(url, dest_dir, ".kicad_mod")

    fp_path, fp_ref = _canonicalize_footprint(footprint_file, None, dest_dir, "WebDownload")
    found = bool(symbol_file or fp_path)
    result = FootprintSearchResult(
        found=found,
        source="web_download" if found else None,
        symbol_file=symbol_file,
        footprint_file=fp_path,
        footprint_ref=fp_ref,
        candidates=local_candidates if not found else None,
        notes=None if found else json.dumps(hits, ensure_ascii=False),
    ).__dict__
    _log_tool_call("search_footprint_library", {"part_number": part_number, "manufacturer": manufacturer}, result)
    return result


def _normalize_download_url(url: str) -> str:
    # GitHub blob view pages aren't raw files; convert to raw.githubusercontent.com
    if "github.com" in url and "/blob/" in url:
        return url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    return url


def _download_and_validate(url: str, dest_dir: str, expected_ext: str) -> Optional[str]:
    import requests
    url = _normalize_download_url(url)
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    try:
        text = resp.content.decode("utf-8", errors="ignore")
    except Exception:
        return None
    if expected_ext == ".kicad_sym" and "kicad_symbol_lib" not in text and "(symbol " not in text:
        return None
    if expected_ext == ".kicad_mod" and "(footprint " not in text and "(module " not in text:
        return None
    os.makedirs(dest_dir, exist_ok=True)
    filename = os.path.basename(url.split("?")[0]) or f"downloaded{expected_ext}"
    if not filename.endswith(expected_ext):
        filename += expected_ext
    dest_path = os.path.join(dest_dir, filename)
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(text)
    return dest_path


def reject_component_no_footprint(rejected_part_number: str, reason: str = "footprint not found") -> dict:
    result = {
        "rejected_part_number": rejected_part_number,
        "reason": reason,
        "action_required": "search_footprint_library for an equivalent alternative part",
    }
    _log_tool_call("reject_component_no_footprint", {
        "rejected_part_number": rejected_part_number
    }, result)
    return result


_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "tool_calls.jsonl")


def _log_tool_call(tool_name: str, args: dict, result: dict) -> None:
    os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
    entry = {"tool": tool_name, "args": args, "result": result}
    with open(_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


if __name__ == "__main__":
    print(search_reference_design("RP2040", "Raspberry Pi"))
    print(search_footprint_library("ATtiny85"))
