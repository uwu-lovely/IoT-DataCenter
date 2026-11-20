"""时序库与告警存储：测试数据隔离、告警流转、实验统计。"""

import time

import db


def test_history_and_latest_ignore_test_rows():
    now = time.time()
    db.insert_telemetry([(now - 2, "roomA", "TH-01", "temperature", 25.0)], is_test=0)
    db.insert_telemetry([(now - 1, "roomA", "TH-01", "temperature", 99.0)], is_test=1)

    history = db.history("roomA", "temperature", minutes=5)
    latest = db.recent_metric_latest(minutes=5)

    assert [row["value"] for row in history] == [25.0]
    assert len(latest) == 1
    assert latest[0]["value"] == 25.0


def test_alarm_ack_and_resolve_flow():
    alarm_id = db.add_alarm(1000.0, "roomA", "TH-01", "temp_warn", "warning", "温度偏高")

    assert len(db.open_alarms()) == 1
    assert db.ack_alarm(alarm_id, 1001.0, "值班员", "已现场确认")
    assert db.open_alarms()[0]["status"] == "acked"
    # 重复确认应失败
    assert not db.ack_alarm(alarm_id, 1002.0, "值班员", "再次确认")

    db.resolve_alarm("TH-01", "temp_warn", 1003.0)
    assert db.open_alarms() == []
    assert db.recent_alarms()[0]["status"] == "resolved"


def test_room_alarm_helpers_deduplicate_by_room():
    db.add_alarm(1000.0, "roomA", "TH-01", "temp_warn", "warning", "温度偏高")
    db.add_alarm(1000.5, "roomA", "TH-02", "temp_warn", "warning", "温度偏高")
    db.add_alarm(1000.0, "roomB", "TH-03", "temp_warn", "warning", "温度偏高")

    assert db.has_open_room_alarm("roomA", "temp_warn")
    db.resolve_room_alarm("roomA", "temp_warn", 1001.0)

    assert not db.has_open_room_alarm("roomA", "temp_warn")
    assert db.has_open_room_alarm("roomB", "temp_warn")


def test_run_stats_and_experiment_record():
    now = time.time()
    db.insert_room_state(now - 10, "roomA", "threshold", 27.0, 50.0, 50, 1, 1000.0)
    db.insert_room_state(now - 5, "roomA", "threshold", 29.0, 51.0, 50, 1, 1000.0)

    stats = db.run_stats(now - 20, now)

    assert stats["samples"] == 2
    assert stats["avg_kw"] == 3.6  # ac 50% -> 3.0kW + 排风 0.6kW
    assert stats["over_ratio"] == 50.0
    assert stats["max_temp"] == 29.0

    db.insert_experiment_run("exp-1", "threshold", now - 20, now, stats)
    runs = db.recent_experiment_runs()
    assert len(runs) == 1
    assert runs[0]["mode"] == "threshold"
    assert runs[0]["kwh"] == stats["kwh"]


def test_purge_removes_only_expired_rows():
    now = time.time()
    db.insert_telemetry(
        [
            (now - 3600, "roomA", "TH-01", "temperature", 25.0),
            (now - 10, "roomA", "TH-01", "temperature", 26.0),
        ]
    )
    db.purge_old(keep_minutes=30, state_keep_minutes=1440)
    rows = db.history("roomA", "temperature", minutes=120)
    assert len(rows) == 1
    assert rows[0]["value"] == 26.0
