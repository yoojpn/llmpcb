CALCULATOR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calc_led_resistor",
            "description": "Calculate series resistor value for an LED using Ohm's law. Always use this instead of manual calculation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "supply_voltage_v": {"type": "number"},
                    "led_forward_voltage_v": {"type": "number", "description": "from datasheet"},
                    "led_forward_current_ma": {"type": "number", "description": "from datasheet"},
                },
                "required": ["supply_voltage_v", "led_forward_voltage_v", "led_forward_current_ma"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calc_voltage_divider",
            "description": "Calculate output voltage, current, and power for a resistive voltage divider.",
            "parameters": {
                "type": "object",
                "properties": {
                    "v_in": {"type": "number"},
                    "r1_ohm": {"type": "number"},
                    "r2_ohm": {"type": "number"},
                },
                "required": ["v_in", "r1_ohm", "r2_ohm"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calc_trace_width",
            "description": "Calculate minimum PCB trace width for a given current per IPC-2221.",
            "parameters": {
                "type": "object",
                "properties": {
                    "current_a": {"type": "number"},
                    "temp_rise_c": {"type": "number", "default": 10.0},
                    "copper_thickness_oz": {"type": "number", "default": 1.0},
                    "layer": {"type": "string", "enum": ["internal", "external"], "default": "external"},
                },
                "required": ["current_a"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calc_battery_life",
            "description": "Estimate runtime from battery capacity and average current draw.",
            "parameters": {
                "type": "object",
                "properties": {
                    "battery_capacity_mah": {"type": "number"},
                    "avg_current_ma": {"type": "number"},
                    "efficiency_factor": {"type": "number", "default": 0.8},
                },
                "required": ["battery_capacity_mah", "avg_current_ma"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unit_convert",
            "description": "Convert between electrical units (uF/nF/pF, mA/A, etc). Required for any unit conversion.",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"type": "number"},
                    "from_unit": {"type": "string"},
                    "to_unit": {"type": "string"},
                },
                "required": ["value", "from_unit", "to_unit"],
            },
        },
    },
]

RESEARCH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_design_script",
            "description": "STRONGLY PREFERRED for any sequence of 2+ tool calls. Write a Python script that calls multiple tools yourself (they're available as plain functions by name: batch_research_parts(...), calc_led_resistor(...), search_footprint_library(...), build_and_simulate_schematic(...), build_and_check_pcb(...), etc). This executes the WHOLE sequence in one turn instead of one round trip per tool call. Use print() to show only what you actually need to see in the result -- e.g. print(footprint_result['footprint_ref']) rather than print(footprint_result) if that's all you need. Example: `parts = batch_research_parts([{'part_number': 'NE555P', 'needs_datasheet': True}]); print(parts['NE555P']['footprint'])`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source code. Tools are pre-available as functions by name. Use print() for anything you need back."},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_offloaded_file",
            "description": "Recover the full content of a tool result that was too large to keep inline and was written to disk (you'll see a message like '_offloaded_to_file: <path>' with a short preview in a prior tool result). Only call this if the preview genuinely isn't enough to proceed -- most of the time it is.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string"},
                },
                "required": ["filepath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_research_parts",
            "description": "STRONGLY PREFERRED over calling search_footprint_library/search_spice_model/search_reference_design separately for each part. Given the full list of parts this design needs, look up footprints (and optionally SPICE models and datasheets) for ALL of them in one call, executed in parallel. This replaces many separate search turns with a single turn -- always use this first, at the start of the design, for every part you already know you need (e.g. the MCU, display, connector, regulator, memory chip). Only fall back to individual search_footprint_library calls afterward, for parts discovered later (e.g. an alternate part after a rejection).",
            "parameters": {
                "type": "object",
                "properties": {
                    "parts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "part_number": {"type": "string"},
                                "manufacturer": {"type": "string"},
                                "needs_spice_model": {"type": "boolean", "description": "true for transistors/diodes/ICs that need .model params for SPICE"},
                                "needs_datasheet": {"type": "boolean", "description": "true if you'll need electrical specs from the datasheet"},
                                "datasheet_sections": {"type": "array", "items": {"type": "string"}, "description": "section names to extract from the datasheet in this same call, e.g. ['Pin Configuration', 'Electrical Characteristics'] -- avoids separate fetch_and_extract_schematic_data turns"},
                            },
                            "required": ["part_number"],
                        },
                    },
                },
                "required": ["parts"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_reference_design",
            "description": "Search for the official reference design, datasheet, or application note for a part. Call before using any numeric value or topology.",
            "parameters": {
                "type": "object",
                "properties": {
                    "part_number": {"type": "string"},
                    "manufacturer": {"type": "string"},
                    "doc_type": {"type": "string", "enum": ["datasheet", "reference_design", "application_note", "evaluation_board"]},
                },
                "required": ["part_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_and_extract_schematic_data",
            "description": "Extract circuit topology and component values from a found document/URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_url": {"type": "string"},
                    "target_section": {"type": "string"},
                },
                "required": ["document_url", "target_section"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_footprint_library",
            "description": "Search KiCad/SnapMagic libraries for a part's symbol and footprint. If not found, call reject_component_no_footprint. Never generate a footprint via AI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "part_number": {"type": "string"},
                    "manufacturer": {"type": "string"},
                },
                "required": ["part_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reject_component_no_footprint",
            "description": "Exclude a part from candidates because no footprint was found.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rejected_part_number": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["rejected_part_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_spice_model",
            "description": "Search for and download a real, ngspice-compatible SPICE model (.model/.subckt) for a part. Never write .model parameters from memory -- always call this first and use the returned file_path via .include and model_name in your netlist. If found=False, no compatible model exists; fall back to the datasheet+calculator verification path instead of guessing parameters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "part_number": {"type": "string"},
                    "manufacturer": {"type": "string"},
                },
                "required": ["part_number"],
            },
        },
    },
]

KICAD_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "build_and_check_pcb",
            "description": "Place components on the PCB AND run DRC in one call (these are always used together; prefer this over calling generate_pcb_layout and run_drc_check separately). Component/hole overlap is always prevented regardless of clearance settings -- adjust part_clearance_mm to make the board denser (tight enclosure) or looser (easier hand assembly) as the user's request implies.",
            "parameters": {
                "type": "object",
                "properties": {
                    "netlist_path": {"type": "string"},
                    "board_width_mm": {"type": "number"},
                    "board_height_mm": {"type": "number"},
                    "mounting_holes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "x_mm": {"type": "number"},
                                "y_mm": {"type": "number"},
                                "diameter_mm": {"type": "number"},
                            },
                        },
                    },
                    "part_clearance_mm": {"type": "number", "default": 3.0, "description": "extra spacing between adjacent component bounding boxes; lower for a compact/small enclosure, higher for easier hand-soldering or thermal spacing"},
                    "hole_keepout_margin_mm": {"type": "number", "default": 2.0, "description": "extra radius kept clear around each mounting hole beyond its own diameter"},
                },
                "required": ["netlist_path", "board_width_mm", "board_height_mm"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_and_simulate_schematic",
            "description": "Generate the KiCad schematic from SKiDL code AND run SPICE verification in one call (prefer this over calling generate_schematic and run_spice_simulation separately). Pass a raw SPICE netlist string in `netlist` to also verify electrically; omit it to only generate the schematic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skidl_code": {"type": "string"},
                    "output_name": {"type": "string"},
                    "netlist": {"type": "string", "description": "raw SPICE netlist text for ngspice, optional"},
                    "analysis_type": {"type": "string", "enum": ["transient", "dc", "ac"]},
                    "check_points": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["skidl_code", "output_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_schematic",
            "description": "Run SKiDL code and generate a KiCad netlist. Prefer build_and_simulate_schematic instead when SPICE verification will also be needed.",
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
            "name": "run_spice_simulation",
            "description": "Run ngspice on a netlist and return voltage/current results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "netlist": {"type": "string"},
                    "analysis_type": {"type": "string", "enum": ["transient", "dc", "ac"]},
                    "check_points": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["netlist", "analysis_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_pcb_layout",
            "description": "Place components and mounting holes via pcbnew.",
            "parameters": {
                "type": "object",
                "properties": {
                    "netlist_path": {"type": "string"},
                    "board_width_mm": {"type": "number"},
                    "board_height_mm": {"type": "number"},
                    "mounting_holes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "x_mm": {"type": "number"},
                                "y_mm": {"type": "number"},
                                "diameter_mm": {"type": "number"},
                            },
                        },
                    },
                },
                "required": ["netlist_path", "board_width_mm", "board_height_mm"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_drc_check",
            "description": "Run design rule check via kicad-cli.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pcb_path": {"type": "string"},
                },
                "required": ["pcb_path"],
            },
        },
    },
]

CRITIC_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "audit_grounding",
            "description": "Verify whether a design decision is backed by a cited source.",
            "parameters": {
                "type": "object",
                "properties": {
                    "design_decision": {"type": "string"},
                    "cited_source": {"type": "string"},
                },
                "required": ["design_decision"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cross_check_against_datasheet",
            "description": "Cross-check a claimed spec against actual circuit usage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "component_ref": {"type": "string"},
                    "claimed_spec": {"type": "string"},
                    "circuit_usage": {"type": "string"},
                },
                "required": ["component_ref", "claimed_spec", "circuit_usage"],
            },
        },
    },
]

ALL_DESIGNER_TOOLS = CALCULATOR_TOOLS + RESEARCH_TOOLS + KICAD_TOOLS
ALL_CRITIC_TOOLS = CALCULATOR_TOOLS + RESEARCH_TOOLS + CRITIC_TOOLS + [
    t for t in KICAD_TOOLS if t["function"]["name"] == "run_drc_check"
]
