"""规则引擎：阈值滞回、机房级去重与自动恢复。"""

import time

import db
from rules import RuleEngine


def _alarms(events):
    return [e for e in events if e["type"] == "alarm"]


def _resolves(events):
    return [e for e in events if e["type"] == "resolve"]


def test_same_room_sensors_share_one_alarm():
    eng = RuleEngine()
    first = eng.evaluate(1000.0, "roomA", "TH-01", "temperature", 30.0)
    second = eng.evaluate(1001.0, "roomA", "TH-02", "temperature", 29.5)

    assert len(_alarms(first)) == 1
    assert _alarms(second) == []
    assert len(db.open_alarms()) == 1


def test_room_alarm_resolves_only_after_all_sensors_recover():
    eng = RuleEngine()
    eng.evaluate(1000.0, "roomA", "TH-01", "temperature", 30.0)
    eng.evaluate(1001.0, "roomA", "TH-02", "temperature", 29.0)

    # 第一路恢复，但第二路仍高于“阈值-滞回”，告警应保持
    still_open = eng.evaluate(1002.0, "roomA", "TH-01", "temperature", 26.5)
    assert _resolves(still_open) == []
    assert len(db.open_alarms()) == 1

    # 两路都低于 27.5°C 后才自动恢复
    recovered = eng.evaluate(1003.0, "roomA", "TH-02", "temperature", 26.4)
    assert len(_resolves(recovered)) == 1
    assert db.open_alarms() == []


def test_temperature_hysteresis_prevents_flapping():
    eng = RuleEngine()
    assert len(_alarms(eng.evaluate(1000.0, "roomA", "TH-01", "temperature", 28.4))) == 1
    # 滞回区间内（27.5~28）不恢复
    assert _resolves(eng.evaluate(1001.0, "roomA", "TH-01", "temperature", 27.8)) == []
    # 低于阈值-滞回后恢复
    assert len(_resolves(eng.evaluate(1002.0, "roomA", "TH-01", "temperature", 27.4))) == 1


def test_humidity_alarm_is_deduplicated_per_room():
    eng = RuleEngine()
    first = eng.evaluate(1000.0, "roomA", "TH-01", "humidity", 75.0)
    second = eng.evaluate(1001.0, "roomA", "TH-02", "humidity", 76.0)

    assert [a["rule"] for a in _alarms(first)] == ["humidity_warn"]
    assert _alarms(second) == []
    assert len(db.open_alarms()) == 1


def test_power_alarm_stays_per_device():
    eng = RuleEngine()
    eng.evaluate(1000.0, "roomA", "PDU-01", "power", 2700.0)
    eng.evaluate(1001.0, "roomA", "PDU-02", "power", 2800.0)

    # 单路 PDU 过载是设备级问题，两路应各自产生告警
    assert len(db.open_alarms()) == 2


def test_offline_sensor_does_not_block_recovery():
    eng = RuleEngine()
    eng.evaluate(1000.0, "roomA", "TH-01", "temperature", 30.0)
    # TH-02 只在 1001s 上报过一次高温，超过新鲜窗口后不再参与判定
    eng.evaluate(1001.0, "roomA", "TH-02", "temperature", 30.0)
    events = eng.evaluate(1100.0, "roomA", "TH-01", "temperature", 26.0)

    assert len(_resolves(events)) == 1
    assert db.open_alarms() == []


def test_temp_rate_alarm_is_room_scoped():
    eng = RuleEngine()
    # 先各上报一次建立基线，再分别给出 30°C/min 的升温
    eng.evaluate(1000.0, "roomA", "TH-01", "temperature", 25.0)
    eng.evaluate(1000.0, "roomA", "TH-02", "temperature", 25.0)
    eng.evaluate(1000.5, "roomA", "TH-01", "temperature", 25.2)
    eng.evaluate(1000.5, "roomA", "TH-02", "temperature", 25.2)
    eng.evaluate(1031.0, "roomA", "TH-01", "temperature", 41.0)
    eng.evaluate(1031.0, "roomA", "TH-02", "temperature", 41.0)

    rate_alarms = [a for a in db.open_alarms() if a["rule"] == "temp_rate"]
    assert len(rate_alarms) == 1


def test_temp_rate_supports_slow_sampling():
    eng = RuleEngine()
    # 真实设备 60 秒采样：第三次上报时按 60 秒窗口计算斜率
    eng.evaluate(0.0, "roomA", "TH-01", "temperature", 25.0)
    eng.evaluate(60.0, "roomA", "TH-01", "temperature", 30.0)
    eng.evaluate(120.0, "roomA", "TH-01", "temperature", 32.0)

    rate_alarms = [a for a in db.open_alarms() if a["rule"] == "temp_rate"]
    assert len(rate_alarms) == 1


def test_ups_output_voltage_is_monitored():
    eng = RuleEngine()
    low = eng.evaluate(1000.0, "roomA", "UPS-01", "output_voltage", 190.0)
    assert [a["rule"] for a in _alarms(low)] == ["ups_voltage_low_warn"]

    recovered = eng.evaluate(1001.0, "roomA", "UPS-01", "output_voltage", 225.0)
    assert len(_resolves(recovered)) == 1
    assert db.open_alarms() == []
