from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    OK = "OK"


@dataclass
class SafetyFlag:
    severity: Severity
    node: str
    reason: str
    measured_value: float
    limit_value: float
    auto_block: bool


@dataclass
class NodeResult:
    name: str
    voltage_v: float
    current_a: float
    component_rated_current_a: float
    component_area_mm2: float = 1.0
    power_w: float = 0.0
    reverse_voltage_allowed: bool = False


CURRENT_UTILIZATION_LIMIT = 0.8
THERMAL_LIMIT_W_PER_MM2 = 0.5


def check_current_overload(node: NodeResult) -> SafetyFlag | None:
    if node.component_rated_current_a <= 0:
        return None
    utilization = node.current_a / node.component_rated_current_a
    if utilization > CURRENT_UTILIZATION_LIMIT:
        return SafetyFlag(
            severity=Severity.CRITICAL,
            node=node.name,
            reason=f"current at {utilization * 100:.0f}% of rated (limit {CURRENT_UTILIZATION_LIMIT * 100:.0f}%)",
            measured_value=utilization,
            limit_value=CURRENT_UTILIZATION_LIMIT,
            auto_block=True,
        )
    return None


def check_reverse_voltage(node: NodeResult) -> SafetyFlag | None:
    if node.voltage_v < 0 and not node.reverse_voltage_allowed:
        return SafetyFlag(
            severity=Severity.CRITICAL,
            node=node.name,
            reason=f"unexpected reverse voltage detected ({node.voltage_v}V)",
            measured_value=node.voltage_v,
            limit_value=0.0,
            auto_block=True,
        )
    return None


def check_thermal(node: NodeResult) -> SafetyFlag | None:
    if node.component_area_mm2 <= 0:
        return None
    density = node.power_w / node.component_area_mm2
    if density > THERMAL_LIMIT_W_PER_MM2:
        return SafetyFlag(
            severity=Severity.CRITICAL,
            node=node.name,
            reason=f"power density {density:.3f} W/mm^2 exceeds limit {THERMAL_LIMIT_W_PER_MM2} W/mm^2",
            measured_value=density,
            limit_value=THERMAL_LIMIT_W_PER_MM2,
            auto_block=True,
        )
    return None


def check_short_circuit(node_pairs: list[tuple[str, str, float]]) -> list[SafetyFlag]:
    flags = []
    SHORT_THRESHOLD_OHM = 0.5
    for a, b, r in node_pairs:
        if r < SHORT_THRESHOLD_OHM:
            flags.append(SafetyFlag(
                severity=Severity.CRITICAL,
                node=f"{a}-{b}",
                reason=f"resistance between {a} and {b} is {r} ohm, possible short",
                measured_value=r,
                limit_value=SHORT_THRESHOLD_OHM,
                auto_block=True,
            ))
    return flags


def run_deterministic_safety_check(nodes: list[NodeResult],
                                    short_circuit_pairs: list[tuple[str, str, float]] = None) -> dict:
    flags: list[SafetyFlag] = []
    for node in nodes:
        for check in (check_current_overload, check_reverse_voltage, check_thermal):
            flag = check(node)
            if flag:
                flags.append(flag)

    if short_circuit_pairs:
        flags.extend(check_short_circuit(short_circuit_pairs))

    critical_flags = [f for f in flags if f.severity == Severity.CRITICAL]
    passed = len(critical_flags) == 0

    def _serialize(f: SafetyFlag) -> dict:
        d = f.__dict__.copy()
        d["severity"] = f.severity.value
        return d

    return {
        "pass": passed,
        "flags": [_serialize(f) for f in flags],
        "critical_count": len(critical_flags),
        "summary": "PASS" if passed else f"FAIL: {len(critical_flags)} critical flag(s)",
    }


if __name__ == "__main__":
    normal_node = NodeResult(name="LED_anode", voltage_v=3.3, current_a=0.015,
                              component_rated_current_a=0.02, component_area_mm2=4.0, power_w=0.05)
    overload_node = NodeResult(name="R1", voltage_v=5.0, current_a=0.19,
                                component_rated_current_a=0.2, component_area_mm2=1.0, power_w=0.9)

    print(run_deterministic_safety_check([normal_node]))
    print(run_deterministic_safety_check([overload_node]))
    print(run_deterministic_safety_check(
        [normal_node], short_circuit_pairs=[("VCC", "GND", 0.1)]
    ))
