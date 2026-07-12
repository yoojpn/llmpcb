from __future__ import annotations
import math
from dataclasses import dataclass, asdict


E12_SERIES = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]


def _nearest_e12(value: float) -> float:
    if value <= 0:
        raise ValueError("value must be positive")
    exponent = math.floor(math.log10(value))
    base = value / (10 ** exponent)
    nearest = min(E12_SERIES, key=lambda x: abs(x - base))
    return round(nearest * (10 ** exponent), 6)


@dataclass
class LedResistorResult:
    resistor_ohm_ideal: float
    resistor_ohm_e12: float
    power_dissipation_w: float
    current_a: float


def calc_led_resistor(supply_voltage_v: float, led_forward_voltage_v: float,
                       led_forward_current_ma: float) -> dict:
    if supply_voltage_v <= led_forward_voltage_v:
        raise ValueError(
            f"supply_voltage_v ({supply_voltage_v}V) must exceed led_forward_voltage_v ({led_forward_voltage_v}V)"
        )
    current_a = led_forward_current_ma / 1000.0
    r_ideal = (supply_voltage_v - led_forward_voltage_v) / current_a
    r_e12 = _nearest_e12(r_ideal)
    actual_current_a = (supply_voltage_v - led_forward_voltage_v) / r_e12
    power_w = (supply_voltage_v - led_forward_voltage_v) * actual_current_a
    return asdict(LedResistorResult(
        resistor_ohm_ideal=round(r_ideal, 3),
        resistor_ohm_e12=r_e12,
        power_dissipation_w=round(power_w, 4),
        current_a=round(actual_current_a, 5),
    ))


def calc_voltage_divider(v_in: float, r1_ohm: float, r2_ohm: float) -> dict:
    if r1_ohm <= 0 or r2_ohm <= 0:
        raise ValueError("resistor values must be positive")
    v_out = v_in * r2_ohm / (r1_ohm + r2_ohm)
    current_a = v_in / (r1_ohm + r2_ohm)
    return {
        "v_out": round(v_out, 4),
        "current_a": round(current_a, 6),
        "power_total_w": round(v_in * current_a, 5),
    }


def calc_trace_width(current_a: float, temp_rise_c: float = 10.0,
                      copper_thickness_oz: float = 1.0, layer: str = "external") -> dict:
    if current_a <= 0:
        raise ValueError("current_a must be positive")
    k = 0.048 if layer == "external" else 0.024
    thickness_mil = 1.37 * copper_thickness_oz
    area_mil2 = (current_a / (k * (temp_rise_c ** 0.44))) ** (1 / 0.725)
    width_mil = area_mil2 / thickness_mil
    width_mm = width_mil * 0.0254
    return {
        "layer": layer,
        "required_width_mil": round(width_mil, 2),
        "required_width_mm": round(width_mm, 3),
        "recommended_width_mm": round(max(width_mm * 1.2, 0.2), 3),
        "assumptions": {
            "temp_rise_c": temp_rise_c,
            "copper_thickness_oz": copper_thickness_oz,
        },
    }


def calc_battery_life(battery_capacity_mah: float, avg_current_ma: float,
                       efficiency_factor: float = 0.8) -> dict:
    if avg_current_ma <= 0:
        raise ValueError("avg_current_ma must be positive")
    effective_capacity = battery_capacity_mah * efficiency_factor
    hours = effective_capacity / avg_current_ma
    return {
        "estimated_hours": round(hours, 1),
        "estimated_days": round(hours / 24, 2),
        "effective_capacity_mah": round(effective_capacity, 1),
        "efficiency_factor_used": efficiency_factor,
    }


def calc_decoupling_cap_check(claimed_uf: float, recommended_uf: float,
                               tolerance_ratio: float = 0.5) -> dict:
    ratio = claimed_uf / recommended_uf if recommended_uf > 0 else float("inf")
    within_tolerance = (1 - tolerance_ratio) <= ratio <= (1 + tolerance_ratio) * 2
    return {
        "claimed_uf": claimed_uf,
        "recommended_uf": recommended_uf,
        "ratio": round(ratio, 3),
        "within_tolerance": within_tolerance,
    }


def unit_convert(value: float, from_unit: str, to_unit: str) -> dict:
    factors = {
        "F": 1.0, "mF": 1e-3, "uF": 1e-6, "nF": 1e-9, "pF": 1e-12,
        "A": 1.0, "mA": 1e-3, "uA": 1e-6,
        "V": 1.0, "mV": 1e-3, "kV": 1e3,
        "ohm": 1.0, "kohm": 1e3, "Mohm": 1e6,
    }
    if from_unit not in factors or to_unit not in factors:
        raise ValueError(f"unsupported unit: {from_unit} -> {to_unit}")
    base = value * factors[from_unit]
    result = base / factors[to_unit]
    return {"value": result, "from": f"{value}{from_unit}", "to": f"{result}{to_unit}"}


if __name__ == "__main__":
    print(calc_led_resistor(5.0, 2.0, 20))
    print(calc_trace_width(1.5))
    print(calc_battery_life(2000, 15))
    print(unit_convert(0.1, "uF", "nF"))
