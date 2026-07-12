import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import calculators
from tools import safety_checker


class TestCalculators:
    def test_led_resistor_basic(self):
        result = calculators.calc_led_resistor(5.0, 2.0, 20)
        assert result["resistor_ohm_e12"] == 150.0
        assert 0 < result["current_a"] <= 0.021

    def test_led_resistor_invalid_supply(self):
        with pytest.raises(ValueError):
            calculators.calc_led_resistor(1.5, 2.0, 20)

    def test_trace_width_positive(self):
        result = calculators.calc_trace_width(2.0)
        assert result["required_width_mm"] > 0
        assert result["recommended_width_mm"] >= result["required_width_mm"]

    def test_battery_life(self):
        result = calculators.calc_battery_life(2000, 20)
        assert result["estimated_hours"] > 0

    def test_unit_convert_uf_to_nf(self):
        result = calculators.unit_convert(0.1, "uF", "nF")
        assert abs(result["value"] - 100.0) < 1e-6

    def test_unit_convert_invalid_unit(self):
        with pytest.raises(ValueError):
            calculators.unit_convert(1.0, "banana", "nF")


class TestSafetyChecker:
    def test_normal_node_passes(self):
        node = safety_checker.NodeResult(
            name="LED1", voltage_v=3.3, current_a=0.015,
            component_rated_current_a=0.02, component_area_mm2=4.0, power_w=0.05
        )
        result = safety_checker.run_deterministic_safety_check([node])
        assert result["pass"] is True
        assert result["critical_count"] == 0

    def test_current_overload_fails(self):
        node = safety_checker.NodeResult(
            name="R1", voltage_v=5.0, current_a=0.19,
            component_rated_current_a=0.2, component_area_mm2=1.0, power_w=0.9
        )
        result = safety_checker.run_deterministic_safety_check([node])
        assert result["pass"] is False
        assert result["critical_count"] >= 1

    def test_reverse_voltage_detected(self):
        node = safety_checker.NodeResult(
            name="D1", voltage_v=-1.0, current_a=0.001,
            component_rated_current_a=0.02, reverse_voltage_allowed=False
        )
        result = safety_checker.run_deterministic_safety_check([node])
        assert result["pass"] is False

    def test_short_circuit_detected(self):
        node = safety_checker.NodeResult(
            name="OK_NODE", voltage_v=3.3, current_a=0.01, component_rated_current_a=0.1
        )
        result = safety_checker.run_deterministic_safety_check(
            [node], short_circuit_pairs=[("VCC", "GND", 0.05)]
        )
        assert result["pass"] is False

    def test_result_is_json_serializable(self):
        import json
        node = safety_checker.NodeResult(
            name="R1", voltage_v=5.0, current_a=0.19,
            component_rated_current_a=0.2, component_area_mm2=1.0, power_w=0.9
        )
        result = safety_checker.run_deterministic_safety_check([node])
        json.dumps(result)  # must not raise (severity enum serialization)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
