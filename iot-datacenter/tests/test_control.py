"""三种调控策略的单元测试：只依赖控制器纯逻辑。"""

from config import CONTROL, THRESHOLDS
from control import Controller


def test_threshold_turns_on_at_warn_and_off_at_target():
    ctl = Controller("threshold")
    on = ctl.decide("roomA", THRESHOLDS["temp_warn"])
    assert on and on["level"] == 88 and on["fan"] == 1
    assert ctl.decide("roomA", 27.0) is None
    off = ctl.decide("roomA", CONTROL["target_temp"])
    assert off and off["level"] == 18 and off["fan"] == 0


def test_predictive_output_is_clamped_and_topic_ready():
    ctl = Controller("predictive")
    cmd = ctl.decide("roomA", 32.0)
    assert cmd is not None
    assert 0 <= cmd["level"] <= 100
    assert cmd["target"] == "AC-01"
    assert cmd["fan_target"] == "FAN-01"
    assert 0 <= cmd["fan"] <= 1


def test_adaptive_feedforward_grows_with_power():
    low = Controller("adaptive")
    high = Controller("adaptive")
    cmd_low = low.decide("roomA", CONTROL["adaptive_target"], power_w=1000)
    cmd_high = high.decide("roomA", CONTROL["adaptive_target"], power_w=5000)
    assert cmd_low and cmd_high
    assert cmd_high["level"] > cmd_low["level"]
    assert 0 <= cmd_high["level"] <= 100


def test_adaptive_does_not_cool_before_soft_target():
    ctl = Controller("adaptive")
    # 低于软约束目标且轻载时保持空调关闭，避免过度预冷
    assert ctl.decide("roomA", 24.0, power_w=300) is None


def test_adaptive_ramps_up_when_predicted_to_cross_warning():
    ctl = Controller("adaptive")
    for i in range(12):
        ctl.observe("roomA", 1000.0 + i * 5, 26.5 + i * 0.2)
    cmd = ctl.decide("roomA", 26.5 + 11 * 0.2, power_w=3000)
    assert cmd is not None
    assert cmd["level"] >= 80


def test_reset_clears_controller_state():
    ctl = Controller("threshold")
    ctl.decide("roomA", THRESHOLDS["temp_warn"])
    ctl.reset()
    assert ctl.current("roomA") == {"level": 0, "fan": 0}
