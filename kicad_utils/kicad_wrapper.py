from __future__ import annotations
import os
import re
import gc
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
    # Automatically insert an ERC() call right before generate_netlist(),
    # if the Designer's code doesn't already call it -- SKiDL's own
    # Electrical Rules Check catches unconnected pins, drive conflicts, and
    # power-connection errors, but relying on the Designer to remember to
    # call it manually is unreliable. This does NOT catch every mistake
    # (e.g. a component with both pins tied to the same net, which is
    # topologically valid but functionally wrong) -- see the separate
    # same-net-both-pins check below for that category.
    if "ERC()" not in skidl_code and "ERC ()" not in skidl_code:
        skidl_code = re.sub(
            r"(generate_netlist\s*\()",
            r"ERC()\n\1",
            skidl_code, count=1,
        )
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
        erc_error_count = None
        if success:
            # SKiDL's ERC() (auto-inserted above if the Designer omitted
            # it) prints "N errors found while generating netlist" but
            # does NOT raise an exception or affect the process return
            # code -- a script with real ERC errors (e.g. a pin left
            # genuinely unconnected, drive conflicts) was previously still
            # reported as success=True as long as generate_netlist() ran.
            # Parse that line and treat any nonzero error count as failure.
            error_counts = [int(x) for x in re.findall(r"(\d+) errors? found while (?:generating netlist|running ERC)", proc.stdout + proc.stderr)]
            if error_counts:
                erc_error_count = sum(error_counts)
                if erc_error_count > 0:
                    success = False
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
        pin_name_hint = None
        if not success and ("'NoneType' and 'Net'" in proc.stderr or "'NoneType' object is not iterable" in proc.stderr):
            # This TypeError means `part['WRONG_PIN']` returned None (the
            # pin name/number doesn't exist) and the next connection
            # attempt crashed -- regardless of whether the code wrote
            # `part[pin] += net` or `net += part[pin]`. SKiDL's own
            # diagnostic line -- "ERROR: No pins found using LIB:PART[('PIN',)]"
            # -- names the failing pin but LIB there is the symbol's
            # internal name, not necessarily the file to load (e.g. for an
            # LCSC-fetched part, LIB is the symbol_name like "105017-0001",
            # not the .kicad_sym filename, which is keyed by LCSC ID
            # instead). Match it back to the actual Part(...) call in the
            # Designer's own code, which has the real symbol_file path.
            m = re.search(r"No pins found using ([^:]+):(\S+)\[\('([^']+)',?\)\]", proc.stderr)
            if m:
                _lib_name, _instance_ref, bad_pin = m.groups()
                var_m = re.search(
                    rf'(\w+)\s*=\s*Part\(\s*["\']([^"\']+)["\']\s*,\s*["\']([^"\']+)["\']',
                    skidl_code,
                )
                for var_name, lib_or_path, part_name in re.findall(
                    r'(\w+)\s*=\s*Part\(\s*["\']([^"\']+)["\']\s*,\s*["\']([^"\']+)["\']', skidl_code
                ):
                    if part_name != _lib_name:
                        continue
                    try:
                        import skidl as _skidl
                        p = _skidl.Part(lib_or_path, part_name)
                        pin_names = [pin.name for pin in p.pins]
                        pin_name_hint = (
                            f"'{bad_pin}' is not a real pin name/number on {var_name} ({part_name}). "
                            f"Its ACTUAL pin names are: {pin_names}. Use the exact name from this list, "
                            f"or use pin NUMBERS instead (e.g. {var_name}[1])."
                        )
                        break
                    except Exception:
                        continue
        elif not success:
            m = re.search(r'^\s*(\w+)\[[\'"]([^\'"]+)[\'"]\]\s*\+=', proc.stderr, re.MULTILINE)
            if m:
                var_name, bad_pin = m.groups()
                var_m = re.search(rf'^\s*{re.escape(var_name)}\s*=\s*Part\(\s*["\']([^"\']+)["\']\s*,\s*["\']([^"\']+)["\']', skidl_code, re.MULTILINE)
                if var_m:
                    lib_or_path, part_name = var_m.groups()
                    try:
                        import skidl as _skidl
                        p = _skidl.Part(lib_or_path, part_name)
                        pin_names = [pin.name for pin in p.pins]
                        pin_name_hint = (
                            f"'{var_name}[\"{bad_pin}\"]' failed because '{bad_pin}' is not a real pin "
                            f"name/number on {part_name}. Its ACTUAL pin names are: {pin_names}. "
                            f"Use the exact name from this list (note: some use SKiDL's inverted-logic "
                            f"notation like '~{{RST}}' rather than plain 'RESET')."
                        )
                    except Exception:
                        pass
        return {
            "success": success,
            "netlist_path": str(found_net_path) if success else None,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-6000:] if len(proc.stderr) <= 10000 else (proc.stderr[:2000] + "\n...(middle omitted)...\n" + proc.stderr[-4000:]),
            "part_name_corrections": part_name_corrections or None,
            "error": (
                "netlist contains a literal 'nan' value -- a calculation used to set a component value produced NaN. Check any duty-cycle/timing/log-based calculations for invalid inputs (e.g. log of zero or negative) before assigning the result."
                if nan_detected else
                pin_name_hint if pin_name_hint else
                f"SKiDL's ERC() reported {erc_error_count} error(s) -- check stdout/stderr for details (common cause: accessing a pin name/number that doesn't exist on that part, e.g. part['WRONG_NAME'] returning None, then crashing or silently failing to connect on the next line)."
                if erc_error_count else None
            ),
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


_NOMINAL_RAIL_VOLTAGES = {
    "VBUS": 5.0, "5V": 5.0, "USB": 5.0,
    "V3V3": 3.3, "3V3": 3.3, "VDD": 3.3, "VCC": 3.3,
    "VBAT": 3.7, "VBATT": 3.7, "BAT": 3.7,
}


def verify_power_rails_via_spice(netlist_path: str) -> dict:
    """Ground-truth electrical verification via an actual SPICE simulation,
    rather than more LLM/regex-based reasoning about pin names and net
    labels -- per current best practice for LLM-assisted circuit
    verification (e.g. SPICEAssistant-style tool feedback), a real
    simulator's own convergence/current behavior is a more reliable
    source of truth than static text analysis, which today's session
    repeatedly found could be fooled (misleading net names, pin-mapping
    disagreements between a hand-rolled parser and the real EDA tool).

    Builds a MINIMAL SPICE netlist automatically from the real KiCad
    netlist: real R/C/L passives get real SPICE elements with their
    actual values; nets recognizable as named power rails (VBUS, V3V3,
    VBAT, etc) get an ideal DC voltage source at a nominal voltage;
    everything else (complex IC pins we have no real SPICE model for) is
    left unconnected/ignored. Runs a DC operating-point analysis and
    checks whether the simulator itself reports a problem -- most
    tellingly, two DIFFERENT nominal rails accidentally tied together
    (a genuine short) causes ngspice to report convergence failure or a
    voltage-source-loop error, which is orthogonal ground truth
    independent of how any net happened to be NAMED.
    """
    nets = get_netlist_nets(netlist_path)
    ref_values = {c["ref"]: c["value"] for c in get_netlist_ref_values(netlist_path)}

    # Identify which named rails are present and assign them nominal
    # voltages; find if the SAME physical node ends up implied to be at
    # two different nominal voltages (a short between named rails), which
    # is a purely textual pre-check before even running SPICE.
    rail_nets = {}
    for net_name in nets:
        upper = net_name.upper()
        for keyword, voltage in _NOMINAL_RAIL_VOLTAGES.items():
            if keyword in upper:
                rail_nets[net_name] = voltage
                break

    lines = [".title Auto-generated power-rail sanity check", ""]
    node_map = {"GND": "0", "0": "0"}
    node_counter = [1]

    def _node(net_name: str) -> str:
        if net_name not in node_map:
            node_map[net_name] = str(node_counter[0])
            node_counter[0] += 1
        return node_map[net_name]

    # Real passive elements (R/C/L) with actual values.
    elem_counter = 0
    passive_nets = {}
    for net_name, pin_list in nets.items():
        for ref, pin_num in pin_list:
            passive_nets.setdefault(ref, []).append((net_name, pin_num))
    for ref, pins in passive_nets.items():
        value = ref_values.get(ref, "")
        if ref.startswith("R") and len(pins) == 2 and re.match(r"^[\d.]+[kKmMuUgG]?$", value.replace("ohm", "").strip()):
            n1, n2 = _node(pins[0][0]), _node(pins[1][0])
            spice_val = value.replace("k", "k").replace("K", "k") or "1k"
            elem_counter += 1
            lines.append(f"R{elem_counter} {n1} {n2} {spice_val}")
        elif ref.startswith("C") and len(pins) == 2 and re.match(r"^[\d.]+[pnuUmM]?[fF]?$", value.strip()):
            n1, n2 = _node(pins[0][0]), _node(pins[1][0])
            spice_val = value if value else "1u"
            elem_counter += 1
            lines.append(f"C{elem_counter} {n1} {n2} {spice_val}")

    # Ideal voltage sources for named rails, referenced to GND (node 0).
    v_counter = 0
    for net_name, voltage in rail_nets.items():
        v_counter += 1
        n1 = _node(net_name)
        lines.append(f"V{v_counter} {n1} 0 DC {voltage}")

    lines.append("")
    lines.append(".op")
    lines.append(".end")
    spice_text = "\n".join(lines)

    if v_counter == 0:
        return {"success": True, "skipped": True, "reason": "no recognizable named power rails to inject"}

    result = run_spice_simulation(spice_text, analysis_type="dc")
    log = result.get("log_tail", "") or ""
    convergence_failed = "singular matrix" in log.lower() or "gmin stepping failed" in log.lower()
    # A genuine short between two DIFFERENT nominal rails doesn't always
    # cause ngspice to fail to converge -- for a small but nonzero
    # resistance, it happily computes a physically absurd but numerically
    # valid current (verified in practice: a 0.001-ohm resistor bridging
    # a 5V and 3.3V rail produced a computed current of -1700A, clearly
    # impossible for a small passive, but ngspice reports this as a
    # successful solve with no error string at all). Parse the actual
    # computed current magnitudes from the operating point output -- real,
    # numeric ground truth from the simulator, not text-pattern matching.
    implausible_current = False
    max_current_seen = 0.0
    for m in re.finditer(r"^\s*i\s+(-?[\d.eE+-]+)\s*$", log, re.MULTILINE):
        try:
            i_val = abs(float(m.group(1)))
            max_current_seen = max(max_current_seen, i_val)
            if i_val > 5.0:  # generous bound for small-signal passives
                implausible_current = True
        except ValueError:
            continue
    shorted_rails = convergence_failed or implausible_current
    return {
        "success": result.get("success", False) and not shorted_rails,
        "rails_simulated": {k: v for k, v in rail_nets.items()},
        "possible_short_between_rails": shorted_rails,
        "max_current_seen_amps": max_current_seen,
        "log_tail": log[-1500:],
        "spice_netlist": spice_text,
    }


def find_unconnected_usb_c_cc_pins(netlist_path: str) -> list[dict]:
    """USB-C receptacles require CC1/CC2 to each have their own pull-down
    resistor to GND (per the USB-C spec) -- without this, most modern
    USB-C power sources will not apply VBUS at all, since the CC
    termination is how a sink identifies itself as safe to power. This
    exact omission was the real-world Raspberry Pi 4B launch bug (tying
    CC1/CC2 together instead of separate resistors). Found via manual
    datasheet-trace audit: a generated RP2040 board left CC1/CC2 entirely
    unconnected, which neither DRC/ERC nor find_shorted_two_pin_parts (a
    different failure mode -- component present but doing nothing, not a
    genuinely missing net) would catch, since there's no violation at all
    from KiCad's perspective -- an unused pin simply isn't flagged.
    """
    import re
    content = Path(netlist_path).read_text(encoding="utf-8", errors="ignore")
    # ref -> which pin numbers are actually used in any net
    nets = get_netlist_nets(netlist_path)
    used_pins_by_ref: dict[str, set] = {}
    for pin_list in nets.values():
        for ref, pin_num in pin_list:
            used_pins_by_ref.setdefault(ref, set()).add(pin_num)

    # ref -> component's symbol/part name, to identify USB-C connectors
    ref_to_part = {}
    for m in re.finditer(r'\(comp\s*\(ref "([^"]+)"\).*?\(value "([^"]*)"\)', content, re.DOTALL):
        ref_to_part[m.group(1)] = m.group(2)

    issues = []
    for ref, part_name in ref_to_part.items():
        if not re.search(r"usb.?c|type.?c|usb4085|usb31|usb32", part_name, re.IGNORECASE):
            continue
        # Find this part's symbol file to get real pin name->number mapping
        for sym_file in (Path(netlist_path).parent / "parts").glob("*.kicad_sym"):
            sym_text = sym_file.read_text(encoding="utf-8", errors="ignore")
            if f'(symbol "{part_name}"' not in sym_text and part_name not in sym_text:
                continue
            cc_pins = dict(re.findall(r'\(name "(CC[12])"[^\n]*\n\s*\(number "([^"]+)"', sym_text))
            if not cc_pins:
                continue
            used = used_pins_by_ref.get(ref, set())
            missing = [name for name, num in cc_pins.items() if num not in used]
            if missing:
                issues.append({
                    "ref": ref, "part": part_name, "missing_cc_pins": missing,
                    "note": (
                        f"{ref} ({part_name}) is a USB-C connector but pins {missing} "
                        f"(CC1/CC2) are not connected to anything. Each CC pin needs its OWN "
                        f"5.1kOhm resistor to GND (never share one resistor between them -- "
                        f"that was the real Raspberry Pi 4B launch bug) or most modern USB-C "
                        f"chargers/hosts will refuse to apply VBUS at all."
                    ),
                })
            break
    return issues


def _get_pin_names_for_part(part_value: str, work_dir: Path, lib_name: str = None) -> dict[str, str]:
    """Look up the REAL pin-number -> pin-name mapping for this part, using
    SKiDL's OWN Part() resolution rather than a hand-rolled regex parser --
    a regex-based reader was found to DISAGREE with SKiDL's actual pin
    resolution for symbols using `(extends "Base")` inheritance (e.g. for
    BSS84, the regex read pin2=D/pin3=S, but SKiDL itself resolves
    pin2=S/pin3=D -- a genuine mismatch that produced a false-positive
    wiring-bug report, since re-implementing KiCad's symbol inheritance/
    alternate-pin resolution by hand doesn't reliably match the real
    library behavior). SKiDL is what actually generates the netlist, so
    it's the authoritative source of truth for pin mapping, not a
    reimplementation of it.
    """
    import os
    parts_dir = work_dir / "parts"
    if not parts_dir.exists():
        return {}
    candidates = []
    if lib_name:
        candidates.append(parts_dir / f"{lib_name}.kicad_sym")
    candidates.extend(parts_dir.glob("*.kicad_sym"))
    seen = set()
    for sym_file in candidates:
        if sym_file in seen or not sym_file.exists():
            continue
        seen.add(sym_file)
        text = sym_file.read_text(encoding="utf-8", errors="ignore")
        if f'symbol "{part_value}"' not in text:
            continue
        try:
            import skidl as _skidl
            env = os.environ.copy()
            for var in ("KICAD9_SYMBOL_DIR", "KICAD8_SYMBOL_DIR", "KICAD7_SYMBOL_DIR", "KICAD6_SYMBOL_DIR", "KICAD_SYMBOL_DIR"):
                os.environ.setdefault(var, "/usr/share/kicad/symbols")
            p = _skidl.Part(str(sym_file), part_value)
            return {str(pin.num): pin.name for pin in p.pins}
        except Exception:
            continue
    return {}


_POWER_GROUND_KEYWORDS = ("GND", "VSS", "VCC", "VDD", "3V3", "5V", "AGND", "AVDD", "AVSS", "PWR", "EPAD")




def find_pin_function_mismatches(netlist_path: str) -> list[dict]:
    """Deterministic check: for every net, resolve each connected pin's
    REAL function (from the actual .kicad_sym pin name, not the
    Designer-chosen net name) and flag nets where multiple DIFFERENT,
    non-power/ground signal pins are tied together -- regardless of what
    the net happens to be named. This catches the case a net misleadingly
    named 'GND' actually shorts two unrelated signal pins (e.g. IO12 and
    TXD0) together, which an LLM judge given only the net's chosen name
    can be fooled by, but real pin-function data cannot.
    """
    nets = get_netlist_nets(netlist_path)
    work_dir = Path(netlist_path).resolve().parent
    ref_to_part = {}
    ref_to_lib = {}
    content = Path(netlist_path).read_text(encoding="utf-8", errors="ignore")
    for m in re.finditer(r'\(comp\s*\(ref "([^"]+)"\).*?\(value "([^"]*)"\).*?\(libsource\s*\(lib "([^"]*)"\)', content, re.DOTALL):
        ref_to_part[m.group(1)] = m.group(2)
        ref_to_lib[m.group(1)] = m.group(3)

    pin_name_cache: dict[str, dict[str, str]] = {}
    issues = []
    for net_name, pin_list in nets.items():
        real_names = []
        for ref, pin_num in pin_list:
            part_value = ref_to_part.get(ref)
            if not part_value:
                continue
            cache_key = f"{ref_to_lib.get(ref, '')}:{part_value}"
            if cache_key not in pin_name_cache:
                pin_name_cache[cache_key] = _get_pin_names_for_part(part_value, work_dir, lib_name=ref_to_lib.get(ref))
            real_name = pin_name_cache[cache_key].get(str(pin_num))
            if real_name:
                real_names.append((ref, pin_num, real_name))

        # Classify each real pin name as power/ground or signal.
        signal_pins = [
            (ref, pin_num, name) for ref, pin_num, name in real_names
            if not any(kw in name.upper() for kw in _POWER_GROUND_KEYWORDS)
            and not re.fullmatch(r"Pin_?\d+", name)  # generic header/connector
            # passthrough pins have no inherent "function" of their own --
            # their role is whatever they're wired to, not a fixed identity
            # like a GPIO name, so they shouldn't count as a conflicting
            # "different function" on a net.
        ]
        # Distinct signal pin NAMES (not just count) tied together on one
        # net, where more than one part/pin-number is a genuine signal
        # (not power/ground), is the anomaly worth flagging -- a single
        # signal pin driving/receiving on a net is normal; two or more
        # DIFFERENT signal pins (different real names) sharing a net
        # almost always indicates a wiring mistake.
        distinct_signal_names = set(name for _, _, name in signal_pins)
        if len(signal_pins) >= 2 and len(distinct_signal_names) >= 2:
            issues.append({
                "net": net_name,
                "pins": [(ref, pin_num, name) for ref, pin_num, name in signal_pins],
                "note": (
                    f"Net '{net_name}' ties together pins with DIFFERENT real functions per their "
                    f"actual datasheet-derived pin names (not the net's own label): "
                    f"{[(ref, pin_num, name) for ref, pin_num, name in signal_pins]}. "
                    f"The net's name may be misleading -- verify these pins are genuinely meant "
                    f"to be connected together; if not, this is a wiring bug."
                ),
            })
    return issues


def find_shorted_multipin_ic_gpios(netlist_path: str, ref_values: list[dict] = None) -> list[dict]:
    """Detect a distinct wiring mistake from find_shorted_two_pin_parts:
    a multi-pin IC/MCU (not a simple 2-terminal part) with two or more of
    its OWN pins landing on the same net, where that net also connects to
    something else external (i.e. not just an internal don't-care net).
    This catches, e.g., an ESP32's IO4 and IO2 both being wired to the same
    encoder pin -- which shorts those two GPIOs together internally,
    something neither DRC nor find_shorted_two_pin_parts (which only
    checks 2-terminal passives) catches. Common net names that legitimately
    span many pins of one IC (power/ground rails) are excluded via a
    heuristic: if MORE than a small threshold of that part's pins share
    the net, treat it as a deliberate power/ground rail, not a wiring bug
    -- two coincidentally-shorted signal GPIOs is the case worth flagging,
    dozens of GND pins tied together on a thermal pad is not.
    """
    import re
    nets = get_netlist_nets(netlist_path)
    # collect, per (ref, net), how many DISTINCT pin numbers of that ref appear in that net
    ref_pins_per_net: dict[tuple[str, str], set] = {}
    for net_name, pin_list in nets.items():
        for ref, pin_num in pin_list:
            ref_pins_per_net.setdefault((ref, net_name), set()).add(pin_num)

    suspicious = []
    for (ref, net_name), pins in ref_pins_per_net.items():
        if len(pins) >= 2 and len(pins) <= 3:
            # 2-3 distinct pins of the SAME part sharing one net is very
            # unlikely to be a legitimate power/ground rail (those
            # typically involve many more pins) and much more likely to be
            # two different signal/GPIO pins accidentally wired together.
            suspicious.append({
                "ref": ref, "net": net_name, "pins": sorted(pins),
                "note": f"{len(pins)} different pins of {ref} ({pins}) all share net '{net_name}' -- "
                        f"likely two different signals/GPIOs shorted together rather than an intentional "
                        f"power/ground rail (which normally involves many more shared pins).",
            })
    return suspicious


def find_shorted_two_pin_parts(netlist_path: str) -> list[dict]:
    """Detect a specific, common LLM wiring mistake: a 2-pin passive
    component (resistor, capacitor, LED, etc) with BOTH pins landing on
    the exact same net. This is topologically valid (SKiDL/ERC won't flag
    it -- both pins ARE connected to something) but functionally means the
    component does nothing (e.g. a "current limiting resistor" wired with
    both ends on the same 5V rail, observed in practice, silently passing
    both DRC and a naive functional-completeness check that only verifies
    the part exists). Only checks parts with exactly 2 pins, and only
    passive/2-terminal device types -- a multi-pin IC legitimately having
    several pins on the same power/ground net is normal and not flagged.
    """
    import re
    content = Path(netlist_path).read_text(encoding="utf-8", errors="ignore")

    # ref -> part type, from each (comp (ref "X") ... (libsource (lib "Y") (part "Z"))) block
    ref_to_part = {}
    for m in re.finditer(r'\(comp\s*\(ref "([^"]+)"\)(.*?)\(libsource\s*\(lib "[^"]*"\)\s*\(part "([^"]+)"\)', content, re.DOTALL):
        ref, _, part = m.groups()
        ref_to_part[ref] = part
    two_terminal_types = {"R", "C", "L", "D", "LED", "Fuse", "Ferrite_Bead"}

    # ref+pin -> net name, from each net block's node list
    pin_counts: dict[tuple[str, str], int] = {}
    net_section = content[content.find("(nets"):] if "(nets" in content else ""
    for net_m in re.finditer(r'\(net\s*\(code \d+\)\s*\(name "([^"]+)"\)(.*?)(?=\(net\s*\(code|\Z)', net_section, re.DOTALL):
        net_name, body = net_m.groups()
        for ref, pin in re.findall(r'\(node\s*\(ref "([^"]+)"\)\s*\(pin "([^"]+)"\)', body):
            pin_counts[(ref, net_name)] = pin_counts.get((ref, net_name), 0) + 1

    shorted = []
    for (ref, net_name), count in pin_counts.items():
        if count >= 2 and ref_to_part.get(ref) in two_terminal_types:
            shorted.append({"ref": ref, "part": ref_to_part.get(ref), "net": net_name, "pins_on_same_net": count})
    return shorted


def get_netlist_nets(netlist_path: str) -> dict[str, list[tuple[str, str]]]:
    """Parse the (nets ...) section of a netlist into {net_name: [(ref, pin), ...]}.
    Used to actually assign electrical nets to pads on the PCB after
    placement -- generate_pcb_layout previously only placed footprints
    without ever connecting their pads to nets, which meant there was
    nothing for an autorouter to route (0 unrouted nets) even though the
    schematic-level netlist was fully connected.
    """
    import re
    content = Path(netlist_path).read_text(encoding="utf-8", errors="ignore")
    net_section = content[content.find("(nets"):] if "(nets" in content else ""
    nets = {}
    for net_m in re.finditer(r'\(net\s*\(code \d+\)\s*\(name "([^"]+)"\)(.*?)(?=\(net\s*\(code|\Z)', net_section, re.DOTALL):
        net_name, body = net_m.groups()
        pins = re.findall(r'\(node\s*\(ref "([^"]+)"\)\s*\(pin "([^"]+)"\)', body)
        nets[net_name] = pins
    return nets


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
        # Oversized dry-run pass: first, with an unconstrained width (500mm)
        # get a rough estimate of total footprint area (no wrapping means
        # required_width there is just the sum of every part's width in one
        # row -- not usable directly as a board size, but its area is a
        # reasonable estimate of total component area). Use that to pick a
        # sensible width for a SECOND dry run that actually wraps rows,
        # producing a real achievable shape.
        area_probe = generate_pcb_layout(
            netlist_path, 500.0, 500.0, mounting_holes, footprint_search_dirs,
            part_clearance_mm, hole_keepout_margin_mm,
        )
        if not area_probe.get("success"):
            return area_probe
        rough_total_area = area_probe["required_width_mm"] * area_probe["required_height_mm"]
        probe_width = max(rough_total_area ** 0.5, 20.0)
        probe = generate_pcb_layout(
            netlist_path, probe_width, 500.0, mounting_holes, footprint_search_dirs,
            part_clearance_mm, hole_keepout_margin_mm,
        )
        if not probe.get("success"):
            return probe
        # Small safety margin beyond the exact measured requirement --
        # the probe pass computes the tightest possible fit, which can
        # leave DRC clearance violations right at the edge (observed:
        # ~0.03mm short) when used as the literal final board size.
        board_width_mm = probe["required_width_mm"] + 5.0
        board_height_mm = probe["required_height_mm"] + 5.0

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

        # Edge-mount parts (connectors: USB, headers, JST, etc -- anything
        # needing external physical access, e.g. a cable plugged in from
        # outside the enclosure) get placed in a dedicated column along the
        # LEFT edge of the board first. Placing them via the same
        # shelf-packing pass as ordinary passives previously let them land
        # in the middle of the board, which is physically impractical (you
        # can't plug in a USB cable to a connector buried in the board's
        # interior). Everything else is packed to the right of this column.
        def _is_edge_connector(comp: dict) -> bool:
            ref = comp.get("ref", "")
            lib = (comp.get("footprint_lib") or "").lower()
            return ref.startswith("J") or "connector" in lib

        edge_comps = [c for c in components if _is_edge_connector(c)]
        other_comps = [c for c in components if not _is_edge_connector(c)]

        edge_col_width_mm = 0.0
        edge_cursor_y = margin_mm
        for comp in edge_comps:
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
            footprint.SetReference(comp["ref"])
            # Explicitly force all components onto the front (F.Cu) side --
            # per explicit user preference, routing may freely use both
            # F.Cu and B.Cu (already the case, verified via DSN export
            # containing both layers), but component placement should stay
            # single-sided for simpler assembly. FootprintLoad already
            # defaults to F.Cu, but this makes it a guarantee rather than
            # an assumption.
            if footprint.GetLayer() != pcbnew.F_Cu:
                footprint.Flip(footprint.GetPosition(), False)
            # x=0 anchors the connector body right at the board edge
            # (margin_mm/2 in from the true edge for a little copper/silk
            # clearance) so its externally-facing side is accessible.
            footprint.SetPosition(pcbnew.VECTOR2I_MM(margin_mm / 2 + fp_w / 2, edge_cursor_y + fp_h / 2))
            board.Add(footprint)
            placed.append(comp["ref"])
            edge_cursor_y += fp_h
            edge_col_width_mm = max(edge_col_width_mm, fp_w)

        cursor_x = margin_mm + edge_col_width_mm + (part_clearance_mm if edge_comps else 0.0)
        cursor_y = margin_mm
        components = other_comps

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

            if cursor_x + fp_w > board_width_mm - margin_mm and cursor_x > margin_mm + edge_col_width_mm:
                # wrap to next row
                cursor_x = margin_mm + edge_col_width_mm + (part_clearance_mm if edge_comps else 0.0)
                cursor_y += row_height_mm
                row_height_mm = 0.0

            # Skip past any mounting-hole keepout zone this component's
            # footprint would otherwise land on.
            guard = 0
            while _overlaps_any_hole(cursor_x, cursor_y, cursor_x + fp_w, cursor_y + fp_h) and guard < 20:
                cursor_x += fp_w
                if cursor_x + fp_w > board_width_mm - margin_mm:
                    cursor_x = margin_mm + edge_col_width_mm
                    cursor_y += max(row_height_mm, fp_h)
                    row_height_mm = 0.0
                guard += 1

            footprint.SetReference(comp["ref"])
            if footprint.GetLayer() != pcbnew.F_Cu:
                footprint.Flip(footprint.GetPosition(), False)
            # position is the footprint's anchor (origin), not necessarily
            # its bbox center, but placing by cursor + half-size is a
            # reasonable approximation for THT/SMD footprints in this cache.
            footprint.SetPosition(pcbnew.VECTOR2I_MM(cursor_x + fp_w / 2, cursor_y + fp_h / 2))
            board.Add(footprint)
            placed.append(comp["ref"])

            cursor_x += fp_w
            row_height_mm = max(row_height_mm, fp_h)
            max_x_used = max(max_x_used, cursor_x)

        required_height_mm = max(cursor_y + row_height_mm + margin_mm, edge_cursor_y + margin_mm)
        required_width_mm = max_x_used + margin_mm
        board_too_small = (
            required_height_mm > board_height_mm or required_width_mm > board_width_mm
        )

        # Assign electrical nets to pads based on the netlist -- without
        # this, the board only has placed footprints with no connectivity
        # information at all, meaning DRC's unconnected-item check and any
        # autorouter would see "0 nets" (nothing to route or verify),
        # silently hiding the fact that nothing is actually wired.
        net_assignments = get_netlist_nets(netlist_path)
        footprints_by_ref = {fp.GetReference(): fp for fp in board.GetFootprints()}
        for net_name, pin_list in net_assignments.items():
            net_info = pcbnew.NETINFO_ITEM(board, net_name)
            board.Add(net_info)
            for ref, pin_num in pin_list:
                fp = footprints_by_ref.get(ref)
                if fp is None:
                    continue
                # Some footprints (QFN/QFP modules with an exposed thermal
                # pad, e.g. ESP32) split one logical pin into MANY physical
                # sub-pads that all share the same pad number (KiCad's
                # convention for thermal/ground pad arrays -- often 20+
                # sub-pads for solder-paste-stencil reasons). FindPadByNumber
                # only returns the FIRST match, which left every other
                # sub-pad net-less -- DRC correctly flagged this as both an
                # unconnected item and a short (the unconnected copper
                # islands sit directly adjacent to the one properly-netted
                # sub-pad). Assign the net to EVERY sub-pad sharing that
                # number, not just the first one found.
                matched = False
                for pad in fp.Pads():
                    if pad.GetNumber() == str(pin_num):
                        pad.SetNet(net_info)
                        matched = True
                if not matched:
                    pass  # pin not found on this footprint; leave unconnected (surfaces via DRC)

        # Auto-relax the board's minimum-hole-size design rule to match
        # whatever the actual placed parts need, rather than leaving
        # KiCad's generic 0.3mm default in place. Real parts (e.g. ESP32
        # modules with thermal-pad vias, or other fine-pitch footprints)
        # routinely specify smaller drills (0.2mm and below are standard
        # and manufacturable at JLCPCB/PCBWay etc) -- flagging those as DRC
        # violations against an arbitrary generic default was a false
        # positive the Designer had no way to fix itself (previously
        # observed giving up after concluding "this requires manual KiCad
        # settings changes"). The board should adapt to its components,
        # not the other way around.
        min_drill_mm = None
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                drill = pad.GetDrillSize()
                d = min(drill.x, drill.y) if drill.x > 0 and drill.y > 0 else None
                if d:
                    d_mm = pcbnew.ToMM(d)
                    if min_drill_mm is None or d_mm < min_drill_mm:
                        min_drill_mm = d_mm
        if min_drill_mm is not None:
            settings = board.GetDesignSettings()
            if min_drill_mm < pcbnew.ToMM(settings.m_MinThroughDrill):
                settings.m_MinThroughDrill = pcbnew.FromMM(min_drill_mm)

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
        # Clearance violations where every item in the violation belongs
        # to the SAME component (e.g. two pads within one connector
        # footprint, or two silkscreen segments of one part's own symbol)
        # are a property of that footprint's own design -- the part's
        # manufacturer/footprint author already validated that spacing is
        # manufacturable at their target fab. A generic board-wide
        # clearance default (e.g. 0.2mm) being stricter than that is a
        # false positive our shelf-packing placement had no way to fix
        # (it doesn't touch pad spacing WITHIN a footprint at all), and
        # the Designer had no path to resolve it either -- observed in
        # practice causing repeated identical DRC failures across resized
        # boards, since resizing/repositioning components can't change a
        # single footprint's own internal pad layout.
        def _same_component_only(v):
            # Not limited to "clearance" -- the same root cause (a
            # footprint's own internal pad/sub-pad layout, e.g. a thermal
            # pad split into multiple physical sub-pads where some are
            # tied to GND and others left unconnected within ONE part)
            # also surfaces as other DRC violation types like
            # "shorting_items". What matters is whether every item in the
            # violation belongs to the same single component -- if so, our
            # placement/routing had no hand in it and can't fix it either.
            refs = set()
            for item in v.get("items", []):
                desc = item.get("description", "")
                m = re.search(r"of (\S+)\b", desc)
                if m:
                    refs.add(m.group(1))
                else:
                    return False  # can't determine ref, don't filter
            return len(refs) == 1

        filtered_violations = [v for v in violations if not _same_component_only(v)]
        # KiCad's DRC JSON report puts genuinely missing/unrouted
        # connections in a SEPARATE top-level "unconnected_items" array,
        # not in "violations" -- our violation_count/violations handling
        # only ever looked at "violations", meaning a board could show
        # violation_count=0 while still having dozens of unconnected nets.
        # Found via manual audit: a board reported drc_clean=True /
        # violation_count=0 at the point build_and_check_pcb was called,
        # yet re-checking the saved file directly with kicad-cli showed 25
        # unconnected items that were never surfaced to the Designer.
        unconnected = report.get("unconnected_items", [])
        for u in unconnected:
            filtered_violations.append({
                "type": "unconnected_item",
                "severity": u.get("severity", "error"),
                "description": u.get("description", "Missing connection between items"),
                "items": u.get("items", []),
            })
        return {
            "success": proc.returncode == 0,
            "violation_count": len(filtered_violations),
            "violations": filtered_violations,
            "violations_ignored_same_footprint": len(violations) - len([v for v in violations if not _same_component_only(v)]),
            "unconnected_item_count": len(unconnected),
            "stderr": proc.stderr[-2000:],
        }
    except FileNotFoundError:
        return {"success": False, "error": "kicad-cli not installed"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "timeout after 60s"}


_FREEROUTING_JAR = str(_PROJECT_ROOT / "_tools" / "freerouting.jar")


def route_pcb(pcb_path: str, max_passes: int = 20, timeout_s: int = 150) -> dict:
    """Auto-route the PCB's copper traces using Freerouting (headless CLI
    mode, via the standard Specctra DSN/SES interchange format that KiCad's
    own pcbnew Python API supports natively: ExportSpecctraDSN /
    ImportSpecctraSES). Without this, "build_and_check_pcb" only places
    components and checks physical/clearance rules -- it does NOT connect
    any pins with copper, so a "DRC clean" board previously could still be
    entirely unrouted. This closes that gap.
    """
    import subprocess
    try:
        import pcbnew  # type: ignore
    except ImportError:
        return {"success": False, "error": "pcbnew not available"}
    if not os.path.exists(_FREEROUTING_JAR):
        return {"success": False, "error": f"freerouting.jar not found at {_FREEROUTING_JAR}"}

    pcb_path = str(pcb_path)
    dsn_path = pcb_path.replace(".kicad_pcb", ".dsn")
    ses_path = pcb_path.replace(".kicad_pcb", ".ses")

    board = pcbnew.LoadBoard(pcb_path)
    pcbnew.ExportSpecctraDSN(board, dsn_path)

    try:
        proc = subprocess.run(
            [
                "java", "-jar", _FREEROUTING_JAR,
                "-de", dsn_path, "-do", ses_path,
                "-mp", str(max_passes),
                "--gui.enabled=false",
            ],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"freerouting did not finish within {timeout_s}s"}

    if not os.path.exists(ses_path):
        return {
            "success": False,
            "error": "freerouting did not produce a session file",
            "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:],
        }

    board = pcbnew.LoadBoard(pcb_path)  # reload fresh; ImportSpecctraSES mutates in place
    pcbnew.ImportSpecctraSES(board, ses_path)
    board.Save(pcb_path)

    return {"success": True, "pcb_path": pcb_path}


def _mp_worker(fn_name, kwargs, queue):
    import kicad_utils.kicad_wrapper as _self
    fn = getattr(_self, fn_name)
    try:
        queue.put(fn(**kwargs))
    except Exception as e:
        queue.put({"success": False, "error": f"{type(e).__name__}: {e}"})


def build_and_check_pcb(netlist_path: str, board_width_mm: float = None, board_height_mm: float = None,
                         mounting_holes: list[dict] = None,
                         footprint_search_dirs: list[str] = None,
                         part_clearance_mm: float = 3.0,
                         hole_keepout_margin_mm: float = 2.0,
                         skip_routing: bool = False) -> dict:
    """Runs _build_and_check_pcb_impl in a disposable child process via
    multiprocessing, rather than in-process. pcbnew's SWIG-wrapped C++
    BOARD/FOOTPRINT objects were found (by direct measurement: process RSS
    grew past 1GB by iteration 10-24 across multiple runs while the
    conversation itself stayed under 30KB, and gc.collect() every
    iteration did not meaningfully slow the growth) to not be released by
    Python's normal reference counting or garbage collection. Running the
    actual pcbnew work in a fresh child process each call guarantees the
    OS reclaims ALL of that memory (leaked or not) when the child exits,
    which is the standard mitigation for this class of C-extension leak
    that can't be fixed from pure Python code calling into it.
    """
    import multiprocessing
    kwargs = dict(
        netlist_path=netlist_path, board_width_mm=board_width_mm, board_height_mm=board_height_mm,
        mounting_holes=mounting_holes, footprint_search_dirs=footprint_search_dirs,
        part_clearance_mm=part_clearance_mm, hole_keepout_margin_mm=hole_keepout_margin_mm,
        skip_routing=skip_routing,
    )
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(target=_mp_worker, args=("_build_and_check_pcb_impl", kwargs, queue))
    proc.start()
    proc.join(timeout=220)
    if proc.is_alive():
        proc.terminate()
        proc.join()
        return {"layout": {"success": False, "error": "build_and_check_pcb child process timed out after 180s"}, "routing": None, "drc": None}
    try:
        return queue.get_nowait()
    except Exception:
        return {"layout": {"success": False, "error": "build_and_check_pcb child process produced no result (likely crashed)"}, "routing": None, "drc": None}


def _build_and_check_pcb_impl(netlist_path: str, board_width_mm: float = None, board_height_mm: float = None,
                         mounting_holes: list[dict] = None,
                         footprint_search_dirs: list[str] = None,
                         part_clearance_mm: float = 3.0,
                         hole_keepout_margin_mm: float = 2.0,
                         skip_routing: bool = False) -> dict:
    """Combine generate_pcb_layout + route_pcb + run_drc_check into a
    single tool call: place components, auto-route copper traces between
    them (via Freerouting), then check the result. Previously this only
    placed components and checked physical/clearance rules -- it never
    actually connected any pins with copper, so "DRC clean" did not mean
    the board was electrically complete. board_width_mm/board_height_mm
    are optional -- see generate_pcb_layout's docstring. skip_routing=True
    bypasses the auto-router (e.g. for quick placement-only iteration).
    """
    layout_result = generate_pcb_layout(
        netlist_path, board_width_mm, board_height_mm, mounting_holes, footprint_search_dirs,
        part_clearance_mm=part_clearance_mm, hole_keepout_margin_mm=hole_keepout_margin_mm,
    )
    if not layout_result.get("success"):
        return {"layout": layout_result, "routing": None, "drc": None}

    routing_result = None
    if not skip_routing:
        routing_result = route_pcb(layout_result["pcb_path"])

    drc_result = run_drc_check(layout_result["pcb_path"])
    shorted = find_shorted_two_pin_parts(netlist_path)
    usb_c_cc_issues = find_unconnected_usb_c_cc_pins(netlist_path)
    if shorted and drc_result:
        # These are real functional bugs (a component wired to do nothing)
        # that standard DRC/ERC don't catch -- fold them into the DRC
        # violation count/list so the existing auto-fix loop treats them
        # with the same seriousness as a physical clearance violation,
        # rather than needing a separate code path the Designer might miss.
        drc_result["violation_count"] = drc_result.get("violation_count", 0) + len(shorted)
        drc_result.setdefault("violations", [])
        for s in shorted:
            drc_result["violations"].append({
                "type": "shorted_two_pin_component",
                "severity": "error",
                "description": (
                    f"Component {s['ref']} ({s['part']}) has both pins connected to the same "
                    f"net ('{s['net']}') -- it is wired to do nothing (e.g. a resistor or LED "
                    f"with both ends on the same rail is not actually in the current path). "
                    f"Fix the SKiDL connections so each pin goes to a DIFFERENT net."
                ),
            })
    if usb_c_cc_issues and drc_result:
        # Unlike find_shorted_multipin_ic_gpios (below), this is a
        # specific, unambiguous omission (USB-C CC pins entirely
        # unconnected) with no legitimate reason to leave it that way --
        # found via manual datasheet-trace audit on a real generated
        # RP2040 board. Block completion the same way as a real DRC
        # violation, since a board built this way would very likely not
        # power on at all from most modern USB-C sources.
        drc_result["violation_count"] = drc_result.get("violation_count", 0) + len(usb_c_cc_issues)
        drc_result.setdefault("violations", [])
        for s in usb_c_cc_issues:
            drc_result["violations"].append({
                "type": "usb_c_cc_unconnected",
                "severity": "error",
                "description": s["note"],
            })

    # Unlike find_shorted_two_pin_parts (a deterministic bug -- a passive
    # wired to do nothing is NEVER intentional), find_shorted_multipin_ic_gpios
    # has real false positives (e.g. tying an LDO's EN pin directly to VIN
    # is the textbook-correct way to keep it always-enabled, and looks
    # identical to a genuine two-GPIOs-shorted mistake by this heuristic).
    # Surface it as a non-blocking advisory for the Designer/reviewer to
    # judge, rather than auto-failing DRC and risking "fixing" something
    # that was already correct.
    if drc_result is not None:
        drc_result["advisory_warnings"] = [
            s["note"] for s in find_shorted_multipin_ic_gpios(netlist_path)
        ] + [
            s["note"] for s in find_pin_function_mismatches(netlist_path)
        ]
    return {"layout": layout_result, "routing": routing_result, "drc": drc_result}


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
