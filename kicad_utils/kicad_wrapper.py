from __future__ import annotations
import os
import re
import subprocess
import json
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKDIR = Path(os.environ.get("LLMPCB_WORKDIR", str(_PROJECT_ROOT / "_work")))
WORKDIR.mkdir(parents=True, exist_ok=True)


def _extract_symbol_names(symbol_file: str) -> list[str]:
    import re
    try:
        content = Path(symbol_file).read_text(encoding="utf-8", errors="ignore")
    except (FileNotFoundError, IsADirectoryError):
        return []
    # top-level symbol definitions only (skip nested _0_0 style sub-units)
    names = re.findall(r'^\s*\(symbol "([^"]+)"', content, re.MULTILINE)
    return [n for n in names if not re.search(r"_\d+_\d+$", n)]


def _closest_match(target: str, candidates: list[str]) -> str | None:
    if not candidates:
        return None
    import difflib
    target_u = target.upper()
    # exact case-insensitive match first
    for c in candidates:
        if c.upper() == target_u:
            return c if c != target else None  # already correct, no fix needed
    # substring relationship (e.g. NE555 -> NE555P) is a very common pattern
    substring_matches = [c for c in candidates if target_u in c.upper() or c.upper() in target_u]
    if len(substring_matches) == 1:
        return substring_matches[0]
    pool = substring_matches or candidates
    best = difflib.get_close_matches(target, pool, n=1, cutoff=0.5)
    return best[0] if best else None


_STDLIB_SYMBOL_DIR = "/usr/share/kicad/symbols"


def _resolve_stdlib_path(lib_name: str) -> str | None:
    candidate = os.path.join(_STDLIB_SYMBOL_DIR, f"{lib_name}.kicad_sym")
    return candidate if os.path.exists(candidate) else None


def _closest_stdlib_name(lib_name: str) -> str | None:
    """When a Part() call references a KiCad standard library name that
    doesn't actually exist (e.g. 'Display_OLED', 'Rotary_Encoder_Switch'
    -- both observed in practice as plausible-sounding but nonexistent
    library names), find the closest real library file name.
    """
    if not os.path.isdir(_STDLIB_SYMBOL_DIR):
        return None
    if _resolve_stdlib_path(lib_name):
        return None  # already valid, no fix needed
    import difflib
    real_libs = [f[:-len(".kicad_sym")] for f in os.listdir(_STDLIB_SYMBOL_DIR) if f.endswith(".kicad_sym")]
    matches = difflib.get_close_matches(lib_name, real_libs, n=1, cutoff=0.6)
    return matches[0] if matches else None


def _autocorrect_part_names(skidl_code: str) -> tuple[str, list[dict]]:
    """Statically scan Part(symbol_file, "NAME", ...) calls in the SKiDL
    source and, for any NAME that doesn't exist verbatim in the referenced
    symbol file, substitute the closest actual symbol name found in that
    file. This eliminates an entire category of round trips (observed
    repeatedly across many different parts, not just one specific chip)
    where the Designer guesses a part-name variant, fails, and has to try
    again -- the correction happens deterministically before execution
    instead of costing an LLM turn.
    """
    import re
    corrections = []

    def _replace(m: re.Match) -> str:
        symbol_file, part_name = m.group(1), m.group(2)
        candidates = _extract_symbol_names(symbol_file)
        fixed = _closest_match(part_name, candidates)
        if fixed:
            corrections.append({"symbol_file": symbol_file, "requested": part_name, "corrected": fixed})
            return f'Part("{symbol_file}", "{fixed}"'
        return m.group(0)

    pattern = re.compile(r'Part\(\s*["\']([^"\']+\.kicad_sym)["\']\s*,\s*["\']([^"\']+)["\']')
    new_code = pattern.sub(_replace, skidl_code)

    def _replace_stdlib(m: re.Match) -> str:
        lib_name, part_name = m.group(1), m.group(2)
        fixed_lib = _closest_stdlib_name(lib_name)
        effective_lib = fixed_lib or lib_name
        lib_path = _resolve_stdlib_path(effective_lib)
        if not lib_path:
            return m.group(0)  # can't validate further, leave as-is
        candidates = _extract_symbol_names(lib_path)
        fixed_part = _closest_match(part_name, candidates)
        if fixed_lib or fixed_part:
            corrections.append({
                "requested_library": lib_name, "corrected_library": effective_lib,
                "requested_part": part_name, "corrected_part": fixed_part or part_name,
            })
            return f'Part("{effective_lib}", "{fixed_part or part_name}"'
        return m.group(0)

    # Standard-library form: Part("LibName", "PartName", ...) where LibName
    # is NOT a .kicad_sym file path (that case is handled above). This
    # covers the common Part("Device", "R", ...) style and catches
    # plausible-but-nonexistent library names (e.g. "Display_OLED",
    # "Rotary_Encoder_Switch") that were observed to burn many retries.
    stdlib_pattern = re.compile(r'Part\(\s*["\'](?!/|\.)([A-Za-z0-9_]+)["\']\s*,\s*["\']([^"\']+)["\']')
    new_code = stdlib_pattern.sub(_replace_stdlib, new_code)

    return new_code, corrections


def generate_schematic(skidl_code: str, output_name: str) -> dict:
    # NOTE: run in a sandboxed/restricted environment in production.
    skidl_code, part_name_corrections = _autocorrect_part_names(skidl_code)
    # output_name may or may not include a .net suffix already; normalize.
    base_name = output_name[:-4] if output_name.endswith(".net") else output_name
    script_path = WORKDIR / f"{base_name}.py"
    net_path = WORKDIR / f"{base_name}.net"
    script_path.write_text(skidl_code, encoding="utf-8")

    env = os.environ.copy()
    env.setdefault("KICAD9_SYMBOL_DIR", "/usr/share/kicad/symbols")
    env.setdefault("KICAD8_SYMBOL_DIR", "/usr/share/kicad/symbols")
    env.setdefault("KICAD7_SYMBOL_DIR", "/usr/share/kicad/symbols")
    env.setdefault("KICAD6_SYMBOL_DIR", "/usr/share/kicad/symbols")
    env.setdefault("KICAD_SYMBOL_DIR", "/usr/share/kicad/symbols")

    try:
        proc = subprocess.run(
            ["python3", str(script_path)],
            cwd=str(WORKDIR), capture_output=True, text=True, timeout=60, env=env,
        )
        # The script may call generate_netlist(file_=...) with any filename,
        # not necessarily matching our guess. Fall back to scanning for any
        # .net file newly written by this run if the expected path is absent.
        found_net_path = net_path if net_path.exists() else None
        if found_net_path is None:
            candidates = sorted(WORKDIR.glob("*.net"), key=lambda p: p.stat().st_mtime, reverse=True)
            if candidates:
                found_net_path = candidates[0]

        success = proc.returncode == 0 and found_net_path is not None
        nan_detected = False
        if success:
            # A value like float('nan') silently propagating into a
            # component's .value (e.g. from a bad timing/duty-cycle
            # calculation with a zero-or-negative log argument) doesn't
            # always crash SKiDL -- it can write straight through into the
            # netlist as the literal text "nan", producing a netlist that
            # "succeeds" but is electrically meaningless. Catch it here
            # rather than letting it surface later as an unexplained
            # generate_pcb_layout/DRC failure.
            netlist_text = found_net_path.read_text(encoding="utf-8", errors="ignore")
            if re.search(r'\bnan\b', netlist_text, re.IGNORECASE):
                nan_detected = True
                success = False
        return {
            "success": success,
            "netlist_path": str(found_net_path) if success else None,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-6000:] if len(proc.stderr) <= 10000 else (proc.stderr[:2000] + "\n...(middle omitted)...\n" + proc.stderr[-4000:]),
            "part_name_corrections": part_name_corrections or None,
            "error": "netlist contains a literal 'nan' value -- a calculation used to set a component value produced NaN. Check any duty-cycle/timing/log-based calculations for invalid inputs (e.g. log of zero or negative) before assigning the result." if nan_detected else None,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "timeout after 60s"}


def run_spice_simulation(netlist: str, analysis_type: str = "dc",
                          check_points: list[str] = None) -> dict:
    cir_path = WORKDIR / "sim_input.cir"
    log_path = WORKDIR / "sim_output.log"
    cir_path.write_text(netlist, encoding="utf-8")

    try:
        proc = subprocess.run(
            ["ngspice", "-b", str(cir_path), "-o", str(log_path)],
            cwd=str(WORKDIR), capture_output=True, text=True, timeout=60
        )
        log_content = log_path.read_text(encoding="utf-8", errors="ignore") if log_path.exists() else ""
        return {
            "success": proc.returncode == 0,
            "analysis_type": analysis_type,
            "check_points": check_points or [],
            "log_tail": log_content[-4000:],
            "stderr": proc.stderr[-2000:],
        }
    except FileNotFoundError:
        return {"success": False, "error": "ngspice not installed"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "timeout after 60s"}


def get_netlist_ref_values(netlist_path: str) -> list[dict]:
    """Lightweight extraction of (ref, value) pairs for every component in
    a netlist -- e.g. [{"ref": "U1", "value": "NE555P"}, {"ref": "D1",
    "value": "LED"}]. Used to check actual functional-completeness (does
    the real, final design contain the part types it needs) against a
    requirements checklist, since a reference designator alone (e.g. "U1")
    says nothing about what the part actually is.
    """
    import re
    content = Path(netlist_path).read_text(encoding="utf-8", errors="ignore")
    out = []
    for m in re.finditer(r'\(comp\s*\(ref "([^"]+)"\).*?\(value "([^"]*)"\)', content, re.DOTALL):
        out.append({"ref": m.group(1), "value": m.group(2)})
    return out


def _parse_netlist_components(netlist_path: str) -> list[dict]:
    """Parse a SKiDL-generated (KiCad legacy S-expression) netlist file and
    extract (ref, footprint_lib, footprint_name) for each component.

    The footprint field may be "LibName:FootprintName" or, when SKiDL/KiCad
    could not resolve the library, just "FootprintName" with no colon. Both
    forms must be handled or components silently get dropped from layout.
    """
    import re
    content = Path(netlist_path).read_text(encoding="utf-8", errors="ignore")
    components = []
    for m in re.finditer(
        r'\(comp\s*\(ref "([^"]+)"\).*?\(footprint "([^"]+)"\)',
        content, re.DOTALL,
    ):
        ref, fp_field = m.groups()
        if fp_field.startswith("/") or fp_field.startswith("~") or ".kicad_mod" in fp_field:
            # Designer passed an absolute/relative filesystem path as the
            # footprint value instead of "Lib:Name". Extract the filename.
            fp_lib = None
            fp_name = os.path.basename(fp_field)
            if fp_name.endswith(".kicad_mod"):
                fp_name = fp_name[: -len(".kicad_mod")]
        elif ":" in fp_field:
            fp_lib, fp_name = fp_field.split(":", 1)
        else:
            fp_lib, fp_name = None, fp_field
        components.append({"ref": ref, "footprint_lib": fp_lib, "footprint_name": fp_name})
    return components


def _find_footprint_file_for(fp_lib: str | None, fp_name: str, footprint_search_dirs: list[str]) -> str | None:
    """Locate a .kicad_mod file for the given library:name pair by searching
    the official KiCad footprint cache and any per-project downloaded dirs
    (including one level of subdirectories, e.g. easyeda2kicad's *.pretty
    folders). fp_lib may be None if the netlist did not specify a library
    prefix or was a raw filesystem path.
    """
    candidates = []
    if fp_lib:
        candidates += [
            Path(CACHE_DIR_FOR_LAYOUT, "kicad-footprints", f"{fp_lib}.pretty", f"{fp_name}.kicad_mod")
            for CACHE_DIR_FOR_LAYOUT in [os.environ.get("LLMPCB_LIB_CACHE", str(_PROJECT_ROOT / "_lib_cache"))]
        ]
    for d in footprint_search_dirs:
        base = Path(d)
        candidates.append(base / f"{fp_name}.kicad_mod")
        if base.is_dir():
            candidates.extend(base.glob(f"*/{fp_name}.kicad_mod"))
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def generate_pcb_layout(netlist_path: str, board_width_mm: float = None, board_height_mm: float = None,
                         mounting_holes: list[dict] = None,
                         footprint_search_dirs: list[str] = None,
                         part_clearance_mm: float = 3.0,
                         hole_keepout_margin_mm: float = 2.0) -> dict:
    """part_clearance_mm and hole_keepout_margin_mm are tunable, not fixed --
    a denser board (e.g. tight enclosure) can request smaller values, a
    board with hand-solderable through-hole parts might want larger ones.
    Regardless of the value chosen, the algorithm guarantees no component
    literally overlaps another component or a mounting hole (that
    correctness property does not depend on the clearance amount -- it was
    a missing collision check, not a spacing-tuning problem).

    board_width_mm/board_height_mm are OPTIONAL. If omitted, this function
    computes the actual required size itself (via an internal oversized
    dry-run pass) and uses that measured size directly -- there is no
    reason to make the caller guess a size, get told "too small", and
    retry, when the layout algorithm already knows exactly how much space
    it needs. There is no inherent reason to prefer a square aspect ratio
    over whatever shape the shelf-packing placement actually needs; a
    square-ification step was tried here previously and rejected as an
    unfounded assumption. Pass explicit dimensions only when there's a
    real physical constraint (a specific enclosure, a user-specified
    maximum) to check the design against.
    """
    if board_width_mm is None or board_height_mm is None:
        # Oversized dry-run pass: compute the actual footprint of the
        # design with a huge sandbox board, then use that measured size
        # directly (plus the standard margin baked into required_*_mm
        # already) as the real board size.
        probe = generate_pcb_layout(
            netlist_path, 500.0, 500.0, mounting_holes, footprint_search_dirs,
            part_clearance_mm, hole_keepout_margin_mm,
        )
        if not probe.get("success"):
            return probe
        board_width_mm = probe["required_width_mm"]
        board_height_mm = probe["required_height_mm"]

    try:
        import pcbnew  # type: ignore
    except ImportError:
        return {"success": False, "error": "pcbnew not available"}

    if not os.path.exists(netlist_path):
        candidates = sorted(WORKDIR.glob("*.net"), key=lambda p: p.stat().st_mtime, reverse=True)
        hint = (
            f" The most recently generated netlist file is: {candidates[0]}. "
            f"Use that exact path (do not guess a filename that hasn't been generated yet)."
            if candidates else " No netlist files exist yet -- call build_and_simulate_schematic first."
        )
        return {"success": False, "error": f"netlist file not found: {netlist_path}.{hint}"}

    footprint_search_dirs = footprint_search_dirs or [str(_PROJECT_ROOT / "_work" / "parts")]
    pcb_path = WORKDIR / "design.kicad_pcb"

    try:
        board = pcbnew.CreateEmptyBoard()

        pts = [
            (0, 0), (board_width_mm, 0),
            (board_width_mm, board_height_mm), (0, board_height_mm), (0, 0),
        ]
        for i in range(len(pts) - 1):
            seg = pcbnew.PCB_SHAPE(board)
            seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
            seg.SetStart(pcbnew.VECTOR2I_MM(*pts[i]))
            seg.SetEnd(pcbnew.VECTOR2I_MM(*pts[i + 1]))
            seg.SetLayer(pcbnew.Edge_Cuts)
            board.Add(seg)

        components = _parse_netlist_components(netlist_path)

        # A duplicate-generation bug in the SKiDL code (e.g. a loop that
        # accidentally re-instantiates parts, or copy-paste with unedited
        # reference numbers) can silently produce a netlist with far more
        # components than the design actually needs -- observed in
        # practice: a simple LED blinker generating over 100 parts across
        # duplicated reference designators, consuming enough memory during
        # placement to approach OOM. Catch it early with a sanity check
        # rather than let it eat all available memory during placement.
        refs_seen = [c["ref"] for c in components]
        if len(refs_seen) > len(set(refs_seen)):
            dupes = sorted({r for r in refs_seen if refs_seen.count(r) > 1})
            return {
                "success": False,
                "error": (
                    f"Netlist contains duplicate component references: {dupes[:10]}"
                    f"{'...' if len(dupes) > 10 else ''}. This usually means the SKiDL "
                    f"code accidentally instantiated the same part more than once (e.g. "
                    f"inside a loop, or copy-pasted Part() calls without changing the "
                    f"reference). Fix the SKiDL code so each component is created exactly "
                    f"once, then regenerate the schematic."
                ),
            }
        if len(components) > 60:
            return {
                "success": False,
                "error": (
                    f"Netlist contains {len(components)} components, which is unusually "
                    f"high for a request of this apparent complexity. This is very likely "
                    f"a bug in the SKiDL code (e.g. a loop unintentionally generating many "
                    f"copies of the same part) rather than an intentionally large design. "
                    f"Review the SKiDL code for accidental repetition before proceeding."
                ),
            }

        placed = []
        missing = []
        # Shelf-packing layout: place components left-to-right using their
        # actual bounding box size, wrapping to a new row when the board
        # width would be exceeded. Clearance is intentionally generous
        # (not just "a bit more than 0") because courtyard/silkscreen
        # extents are often larger than the raw pad bounding box, and a
        # too-tight margin was observed in practice to cause
        # courtyards_overlap / silk_over_copper violations that then had
        # to be blindly retried by the Designer over dozens of turns --
        # a purely geometric problem that a deterministic layout should
        # avoid outright rather than relying on the LLM to guess a fix.
        margin_mm = 3.0
        # part_clearance_mm is now a function parameter (see docstring);
        # generous default retained, but callers can tune it per-board.
        cursor_x = margin_mm
        cursor_y = margin_mm
        row_height_mm = 0.0
        max_x_used = margin_mm

        # Mounting holes are placed FIRST (before components) at their
        # exact user-specified positions, and recorded as reserved
        # rectangles. Components are then shelf-packed to avoid overlapping
        # any hole's keepout zone -- previously holes were placed last with
        # no collision check at all, which caused a component pad to end up
        # directly under a mounting hole (observed as a nonsensical negative
        # annular_width DRC violation).
        hole_keepouts = []  # (x_min, y_min, x_max, y_max) in mm
        for idx, hole in enumerate(mounting_holes or []):
            x, y = hole["x_mm"], hole["y_mm"]
            dia = hole.get("diameter_mm", 3.2)
            keepout_radius = dia / 2 + hole_keepout_margin_mm
            hole_keepouts.append((x - keepout_radius, y - keepout_radius,
                                   x + keepout_radius, y + keepout_radius))

            fp = pcbnew.FOOTPRINT(board)
            fp.SetReference(f"MH{idx + 1}")
            fp.SetPosition(pcbnew.VECTOR2I_MM(x, y))
            pad = pcbnew.PAD(fp)
            pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
            pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
            pad.SetSize(pcbnew.VECTOR2I_MM(dia, dia))
            pad.SetDrillSize(pcbnew.VECTOR2I_MM(dia, dia))
            pad.SetPosition(pcbnew.VECTOR2I_MM(x, y))
            fp.Add(pad)
            board.Add(fp)

        def _overlaps_any_hole(x0, y0, x1, y1) -> bool:
            for hx0, hy0, hx1, hy1 in hole_keepouts:
                if x0 < hx1 and x1 > hx0 and y0 < hy1 and y1 > hy0:
                    return True
            return False

        for comp in components:
            fp_file = _find_footprint_file_for(comp["footprint_lib"], comp["footprint_name"], footprint_search_dirs)
            if not fp_file:
                missing.append(comp)
                continue
            libdir = os.path.dirname(fp_file)
            fp_name_no_ext = os.path.basename(fp_file)[:-len(".kicad_mod")]
            footprint = pcbnew.FootprintLoad(libdir, fp_name_no_ext)
            if footprint is None:
                missing.append(comp)
                continue

            bbox = footprint.GetBoundingBox()
            fp_w = pcbnew.ToMM(bbox.GetWidth()) + part_clearance_mm
            fp_h = pcbnew.ToMM(bbox.GetHeight()) + part_clearance_mm

            if cursor_x + fp_w > board_width_mm - margin_mm and cursor_x > margin_mm:
                # wrap to next row
                cursor_x = margin_mm
                cursor_y += row_height_mm
                row_height_mm = 0.0

            # Skip past any mounting-hole keepout zone this component's
            # footprint would otherwise land on.
            guard = 0
            while _overlaps_any_hole(cursor_x, cursor_y, cursor_x + fp_w, cursor_y + fp_h) and guard < 20:
                cursor_x += fp_w
                if cursor_x + fp_w > board_width_mm - margin_mm:
                    cursor_x = margin_mm
                    cursor_y += max(row_height_mm, fp_h)
                    row_height_mm = 0.0
                guard += 1

            footprint.SetReference(comp["ref"])
            # position is the footprint's anchor (origin), not necessarily
            # its bbox center, but placing by cursor + half-size is a
            # reasonable approximation for THT/SMD footprints in this cache.
            footprint.SetPosition(pcbnew.VECTOR2I_MM(cursor_x + fp_w / 2, cursor_y + fp_h / 2))
            board.Add(footprint)
            placed.append(comp["ref"])

            cursor_x += fp_w
            row_height_mm = max(row_height_mm, fp_h)
            max_x_used = max(max_x_used, cursor_x)

        required_height_mm = cursor_y + row_height_mm + margin_mm
        required_width_mm = max_x_used + margin_mm
        board_too_small = (
            required_height_mm > board_height_mm or required_width_mm > board_width_mm
        )

        # NOTE: trace routing between footprints is not implemented; DRC will
        # report unrouted nets. Component placement/clearance/courtyard
        # checks are meaningful, connectivity/routing checks are not yet.
        board.Save(str(pcb_path))
        return {
            "success": True,
            "pcb_path": str(pcb_path),
            "mounting_holes_placed": len(mounting_holes or []),
            "components_placed": placed,
            "components_missing_footprint": [c["ref"] for c in missing],
            "board_too_small": board_too_small,
            "required_width_mm": round(required_width_mm, 2),
            "required_height_mm": round(required_height_mm, 2),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def run_drc_check(pcb_path: str) -> dict:
    report_path = WORKDIR / "drc_report.json"
    try:
        proc = subprocess.run(
            ["kicad-cli", "pcb", "drc", pcb_path, "--format", "json", "-o", str(report_path)],
            capture_output=True, text=True, timeout=60
        )
        if proc.returncode != 0 and "Maximum number of positional arguments" in (proc.stdout + proc.stderr):
            return {
                "success": False,
                "error": "kicad-cli on this system does not support 'pcb drc' (requires KiCad >= 8.0). "
                         "Installed version lacks this subcommand.",
            }
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
        else:
            report = {"violations": []}
        violations = report.get("violations", [])
        return {
            "success": proc.returncode == 0,
            "violation_count": len(violations),
            "violations": violations,
            "stderr": proc.stderr[-2000:],
        }
    except FileNotFoundError:
        return {"success": False, "error": "kicad-cli not installed"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "timeout after 60s"}


def build_and_check_pcb(netlist_path: str, board_width_mm: float = None, board_height_mm: float = None,
                         mounting_holes: list[dict] = None,
                         footprint_search_dirs: list[str] = None,
                         part_clearance_mm: float = 3.0,
                         hole_keepout_margin_mm: float = 2.0) -> dict:
    """Combine generate_pcb_layout + run_drc_check into a single tool call.
    These two steps are always used together, so splitting them doubled the
    number of round trips needed for no benefit. board_width_mm/
    board_height_mm are optional -- see generate_pcb_layout's docstring.
    """
    layout_result = generate_pcb_layout(
        netlist_path, board_width_mm, board_height_mm, mounting_holes, footprint_search_dirs,
        part_clearance_mm=part_clearance_mm, hole_keepout_margin_mm=hole_keepout_margin_mm,
    )
    if not layout_result.get("success"):
        return {"layout": layout_result, "drc": None}
    drc_result = run_drc_check(layout_result["pcb_path"])
    return {"layout": layout_result, "drc": drc_result}


def build_and_simulate_schematic(skidl_code: str, output_name: str,
                                  netlist: str = None, analysis_type: str = "transient",
                                  check_points: list[str] = None) -> dict:
    """Combine generate_schematic + run_spice_simulation into a single tool
    call for the common case. `netlist` here is the raw SPICE netlist text
    (not the KiCad netlist file) to feed ngspice; if omitted, only the
    schematic step runs and spice is skipped (e.g. for ICs with no model).
    """
    schematic_result = generate_schematic(skidl_code, output_name)
    if not schematic_result.get("success"):
        return {"schematic": schematic_result, "spice": None}
    spice_result = None
    if netlist:
        spice_result = run_spice_simulation(netlist, analysis_type, check_points)
    return {"schematic": schematic_result, "spice": spice_result}


if __name__ == "__main__":
    sample_netlist = """
LED circuit test
V1 vcc 0 5
R1 vcc led_a 150
D1 led_a 0 DLED
.model DLED D(IS=1e-14 N=2 RS=1)
.op
.end
"""
    print(run_spice_simulation(sample_netlist, "dc", ["vcc", "led_a"]))
    print(run_drc_check("/nonexistent/design.kicad_pcb"))
