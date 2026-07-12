from __future__ import annotations
import os
import subprocess
import shutil
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("LLMPCB_LIB_CACHE", str(_PROJECT_ROOT / "_lib_cache")))

KICAD_SYMBOL_REPO = "https://gitlab.com/kicad/libraries/kicad-symbols.git"
KICAD_FOOTPRINT_REPO = "https://gitlab.com/kicad/libraries/kicad-footprints.git"


import re
import subprocess as _subprocess


def find_lcsc_id(part_number: str, manufacturer: str = "") -> str | None:
    """Search the web for a part's LCSC product page and extract its LCSC ID
    (e.g. 'C5446'), which easyeda2kicad can then use to fetch real KiCad files.
    """
    from .search_engine import web_search
    try:
        hits = web_search(f"{manufacturer} {part_number} LCSC", max_results=5)
    except Exception:
        return None
    for hit in hits:
        m = re.search(r"[/_](C\d+)(?:\.html|$|_)", hit["url"])
        if m:
            return m.group(1)
    return None


def download_via_easyeda2kicad(lcsc_id: str, dest_dir: str) -> dict:
    """Fetch symbol/footprint/3D model for a part via its LCSC ID using the
    easyeda2kicad CLI tool. Works without any authentication.
    """
    os.makedirs(dest_dir, exist_ok=True)
    output_prefix = os.path.join(dest_dir, lcsc_id)
    try:
        proc = _subprocess.run(
            ["easyeda2kicad", "--full", f"--lcsc_id={lcsc_id}", "--output", output_prefix],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        return {"found": False, "error": "easyeda2kicad not installed"}
    except _subprocess.TimeoutExpired:
        return {"found": False, "error": "easyeda2kicad timed out"}

    symbol_file = output_prefix + ".kicad_sym"
    footprint_dir = output_prefix + ".pretty"
    footprint_file = None
    footprint_ref = None
    if os.path.isdir(footprint_dir):
        mods = [f for f in os.listdir(footprint_dir) if f.endswith(".kicad_mod")]
        if mods:
            footprint_file = os.path.join(footprint_dir, mods[0])
            fp_name = mods[0][: -len(".kicad_mod")]
            footprint_ref = f"{lcsc_id}:{fp_name}"  # canonical "Lib:Name" form

    found = os.path.exists(symbol_file) or bool(footprint_file)
    return {
        "found": found,
        "symbol_file": symbol_file if os.path.exists(symbol_file) else None,
        "footprint_file": footprint_file,
        "footprint_ref": footprint_ref,
        "stdout": proc.stdout[-1000:],
        "stderr": proc.stderr[-500:] if not found else None,
    }


def _detect_kicad_version() -> str | None:
    """Return the installed kicad-cli version string (e.g. '9.0.9'), or None."""
    try:
        proc = subprocess.run(["kicad-cli", "--version"], capture_output=True, text=True, timeout=10)
        return proc.stdout.strip() or None
    except Exception:
        return None


def ensure_kicad_libraries(timeout: int = 300) -> dict:
    """Clone official KiCad symbol/footprint libraries into a local cache
    (shallow clone) if not already present. Idempotent.

    Clones the tag matching the installed KiCad version when possible --
    the repository's default branch tracks the next unreleased KiCad
    version and its file format is not backward compatible with older
    kicad-cli/pcbnew installs.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    symbols_path = CACHE_DIR / "kicad-symbols"
    footprints_path = CACHE_DIR / "kicad-footprints"
    results = {}
    version = _detect_kicad_version()

    for name, repo, path in (
        ("symbols", KICAD_SYMBOL_REPO, symbols_path),
        ("footprints", KICAD_FOOTPRINT_REPO, footprints_path),
    ):
        if path.exists() and any(path.iterdir()):
            results[name] = {"status": "cached", "path": str(path)}
            continue
        clone_cmd = ["git", "clone", "--depth", "1"]
        if version:
            clone_cmd += ["--branch", version]
        clone_cmd += [repo, str(path)]
        try:
            proc = subprocess.run(clone_cmd, capture_output=True, text=True, timeout=timeout)
            if proc.returncode != 0 and version:
                # tag not found (e.g. patch version not tagged) -- fall back to default branch
                proc = subprocess.run(
                    ["git", "clone", "--depth", "1", repo, str(path)],
                    capture_output=True, text=True, timeout=timeout,
                )
            results[name] = {
                "status": "cloned" if proc.returncode == 0 else "failed",
                "path": str(path),
                "kicad_version_matched": version,
                "stderr": proc.stderr[-1000:],
            }
        except subprocess.TimeoutExpired:
            results[name] = {"status": "timeout", "path": str(path)}
    return results


def _merge_symdir_to_single_lib(symdir_path: str, dest_path: str) -> bool:
    """KiCad 9's split-file symbol format (one .kicad_sym per part inside a
    .kicad_symdir folder) uses `extends` to reference sibling parts in the
    same folder. Copying a single file breaks that reference. This merges
    every symbol in the folder into one valid kicad_symbol_lib file so
    `extends` resolves correctly.
    """
    import re
    symbol_blocks = []
    header = None
    for fname in sorted(os.listdir(symdir_path)):
        if not fname.endswith(".kicad_sym"):
            continue
        content = Path(symdir_path, fname).read_text(encoding="utf-8", errors="ignore")
        if header is None:
            m = re.match(r"(\(kicad_symbol_lib\s*\(version [^\)]*\)\s*\(generator[^\)]*\)\s*(?:\(generator_version[^\)]*\)\s*)?)", content)
            header = m.group(1) if m else '(kicad_symbol_lib (version 20241209) (generator "llmpcb")\n'
        # extract each (symbol "...") ... block (top-level only) by balanced parens
        idx = content.find('(symbol "')
        if idx == -1:
            continue
        depth = 0
        start = idx
        for i in range(idx, len(content)):
            if content[i] == "(":
                depth += 1
            elif content[i] == ")":
                depth -= 1
                if depth == 0:
                    symbol_blocks.append(content[start:i + 1])
                    break
    if not symbol_blocks:
        return False
    merged = header + "\n" + "\n".join(symbol_blocks) + "\n)\n"
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    Path(dest_path).write_text(merged, encoding="utf-8")
    return True


def find_symbol_files(part_number: str) -> list[str]:
    symbols_path = CACHE_DIR / "kicad-symbols"
    if not symbols_path.exists():
        return []
    try:
        proc = subprocess.run(
            ["grep", "-rl", part_number, str(symbols_path), "--include=*.kicad_sym"],
            capture_output=True, text=True, timeout=15,
        )
        return [line for line in proc.stdout.strip().split("\n") if line]
    except subprocess.TimeoutExpired:
        return []


def _extract_symbol_block(content: str, symbol_name: str) -> str | None:
    marker = f'(symbol "{symbol_name}"'
    idx = content.find(marker)
    if idx == -1:
        return None
    depth = 0
    for i in range(idx, len(content)):
        if content[i] == "(":
            depth += 1
        elif content[i] == ")":
            depth -= 1
            if depth == 0:
                return content[idx:i + 1]
    return None


def get_footprint_ref_from_symbol(symbol_file: str, symbol_name: str | None = None) -> str | None:
    """Parse the 'Footprint' property for a specific symbol out of a
    .kicad_sym file. If the symbol uses `extends`, follow the chain to the
    parent symbol, since footprint/pins are inherited and often only
    defined on the base symbol.
    """
    try:
        content = Path(symbol_file).read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        return None

    if symbol_name is None:
        # fall back to old behavior: first Footprint property in the file
        marker = '(property "Footprint" "'
        idx = content.find(marker)
        if idx == -1:
            return None
        start = idx + len(marker)
        end = content.find('"', start)
        return content[start:end] if end != -1 else None

    seen = set()
    current = symbol_name
    for _ in range(5):  # bounded extends-chain traversal
        if current in seen:
            break
        seen.add(current)
        block = _extract_symbol_block(content, current)
        if block is None:
            return None
        fp_match = block.find('(property "Footprint" "')
        if fp_match != -1:
            start = fp_match + len('(property "Footprint" "')
            end = block.find('"', start)
            return block[start:end] if end != -1 else None
        ext_match = block.find('(extends "')
        if ext_match == -1:
            return None
        start = ext_match + len('(extends "')
        end = block.find('"', start)
        current = block[start:end]
    return None


def find_footprint_by_ref(footprint_ref: str) -> str | None:
    """footprint_ref is 'LibName:FootprintName'. Returns the .kicad_mod path."""
    footprints_path = CACHE_DIR / "kicad-footprints"
    if ":" not in footprint_ref or not footprints_path.exists():
        return None
    lib_name, fp_name = footprint_ref.split(":", 1)
    candidate = footprints_path / f"{lib_name}.pretty" / f"{fp_name}.kicad_mod"
    return str(candidate) if candidate.exists() else None


def find_footprint_dirs(part_number: str) -> list[str]:
    """Fallback: naive filename search by part number (rarely matches,
    since KiCad footprints are named by package, not part number).
    """
    footprints_path = CACHE_DIR / "kicad-footprints"
    if not footprints_path.exists():
        return []
    try:
        proc = subprocess.run(
            ["find", str(footprints_path), "-iname", f"*{part_number}*"],
            capture_output=True, text=True, timeout=15,
        )
        return [line for line in proc.stdout.strip().split("\n") if line]
    except subprocess.TimeoutExpired:
        return []


def _symbol_exists_in_file(symbol_file: str, symbol_name: str) -> bool:
    try:
        content = Path(symbol_file).read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        return False
    return f'(symbol "{symbol_name}"' in content


def download_symbol_and_footprint(part_number: str, dest_dir: str) -> dict:
    """Search local library cache and copy matching symbol/footprint files
    into dest_dir. Footprint is resolved via the symbol's Footprint property
    (KiCad footprints are named by package, not part number).

    `found` is only True if a symbol with a name *exactly* matching
    part_number exists in the merged file. A file matching by substring
    (e.g. searching "ATtiny85" hits a file that only contains
    "ATtiny85-20P") is not sufficient -- Part() in SKiDL will fail if the
    exact name is absent, so we must not report a false positive here.
    Returns found=False if nothing matches (caller must then call
    reject_component_no_footprint and search an alternative part).
    """
    ensure_kicad_libraries()
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    symbol_matches = find_symbol_files(part_number)

    copied_symbol = None
    copied_footprint = None
    footprint_ref = None
    exact_symbol_found = False

    if symbol_matches:
        for candidate_src in symbol_matches:
            src = Path(candidate_src)
            dst = dest / src.name
            symdir = src.parent
            candidate_copied = None
            if symdir.name.endswith(".kicad_symdir"):
                # merge the whole folder so `extends` references resolve
                if _merge_symdir_to_single_lib(str(symdir), str(dst)):
                    candidate_copied = str(dst)
            if candidate_copied is None:
                shutil.copy2(src, dst)
                candidate_copied = str(dst)

            if _symbol_exists_in_file(candidate_copied, part_number):
                copied_symbol = candidate_copied
                exact_symbol_found = True
                break
            # not this file -- clean up the speculative copy and try the next candidate
            copied_symbol = candidate_copied  # keep the last-tried copy for the fallback/candidates path below

        if exact_symbol_found:
            footprint_ref = get_footprint_ref_from_symbol(copied_symbol, symbol_name=part_number)
            if footprint_ref:
                fp_path = find_footprint_by_ref(footprint_ref)
                if fp_path:
                    fp_src = Path(fp_path)
                    fp_dst = dest / fp_src.name
                    shutil.copy2(fp_src, fp_dst)
                    copied_footprint = str(fp_dst)

    if not exact_symbol_found:
        # Don't just fail outright, and don't silently guess either. Like a
        # human searching LCSC/DigiKey, offer the real candidate names found
        # in the library so the caller can pick the correct one explicitly
        # in its next call -- this is faster and safer than either an
        # auto-guess (risk of silently picking the wrong part) or a bare
        # "not found" (forces blind guessing across multiple turns).
        candidates: list[str] = []
        if symbol_matches:
            for path in symbol_matches[:3]:  # cap: avoid scanning huge merged files repeatedly
                content = Path(path).read_text(encoding="utf-8", errors="ignore")
                all_names = re.findall(r'^\s*\(symbol "([^"]+)"', content, re.MULTILINE)
                all_names = [n for n in all_names if not re.search(r"_\d+_\d+$", n)]
                part_u = part_number.upper()
                candidates.extend(n for n in all_names if part_u in n.upper())
        candidates = sorted(set(candidates))[:10]
        return {
            "found": False,
            "symbol_file": None,
            "footprint_file": None,
            "footprint_ref": None,
            "source": None,
            "candidates": candidates or None,
            "note": (
                f"'{part_number}' is not an exact symbol name in the local cache. "
                + (f"Similar real part names found: {candidates}. Call again with one of "
                   f"these exact names." if candidates else
                   "No similar names found locally either; try a web/LCSC search instead.")
            ),
        }

    if not copied_footprint:
        # fallback naive search, low confidence
        fallback = find_footprint_dirs(part_number)
        if fallback:
            src = Path(fallback[0])
            dst = dest / src.name
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            copied_footprint = str(dst)

    return {
        "found": bool(copied_footprint),
        "symbol_file": copied_symbol,
        "footprint_file": copied_footprint,
        "footprint_ref": footprint_ref,
        "source": "kicad_official_local_cache",
    }


if __name__ == "__main__":
    print(ensure_kicad_libraries())
    print(download_symbol_and_footprint("ATtiny85", "/tmp/llmpcb_test_download"))
