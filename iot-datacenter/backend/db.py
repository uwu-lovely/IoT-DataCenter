"""SQLite 时序存储与查询。单连接 + 锁，满足单机仿真演示规模。"""

import json
import sqlite3
import threading
import time

from config import DB_PATH, DEVICES

_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    ts REAL NOT NULL, room TEXT NOT NULL, device TEXT NOT NULL,
    metric TEXT NOT NULL, value REAL NOT NULL,
    is_test INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tel_device ON telemetry(device, metric, ts);
CREATE INDEX IF NOT EXISTS idx_tel_room ON telemetry(room, metric, ts);

CREATE TABLE IF NOT EXISTS devices (
    device TEXT PRIMARY KEY, room TEXT NOT NULL, type TEXT NOT NULL,
    name TEXT NOT NULL, last_seen REAL DEFAULT 0, online INTEGER DEFAULT 1,
    paused INTEGER DEFAULT 0, ac_level INTEGER DEFAULT 0, fan INTEGER DEFAULT 0,
    topic TEXT NOT NULL DEFAULT '', gateway TEXT NOT NULL DEFAULT 'mqtt'
);

CREATE TABLE IF NOT EXISTS alarms (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, room TEXT NOT NULL,
    device TEXT NOT NULL, rule TEXT NOT NULL, level TEXT NOT NULL,
    message TEXT NOT NULL, status TEXT DEFAULT 'open', resolved_ts REAL,
    acked_ts REAL, acked_by TEXT, note TEXT
);

CREATE TABLE IF NOT EXISTS room_state (
    ts REAL NOT NULL, room TEXT NOT NULL, mode TEXT NOT NULL,
    temp REAL, humidity REAL, ac_level INTEGER, fan INTEGER, power_w REAL
);
CREATE INDEX IF NOT EXISTS idx_state_room ON room_state(room, ts);

CREATE TABLE IF NOT EXISTS control_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, room TEXT NOT NULL,
    mode TEXT NOT NULL, target TEXT NOT NULL, action TEXT NOT NULL,
    reason TEXT NOT NULL, source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, exp_id TEXT NOT NULL, mode TEXT NOT NULL,
    started REAL NOT NULL, finished REAL NOT NULL, duration REAL,
    avg_kw REAL, over_ratio REAL, max_temp REAL, avg_temp REAL,
    kwh REAL, samples INTEGER
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""

SCHEMA_VERSION = 6


def init_db() -> None:
    with _lock:
        version = _conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 3:
            # 3 -> 4：为设备表补充自定义上报主题，保留已有数据
            columns = [row["name"] for row in _conn.execute("PRAGMA table_info(devices)")]
            if "topic" not in columns:
                _conn.execute("ALTER TABLE devices ADD COLUMN topic TEXT NOT NULL DEFAULT ''")
            version = 4
        if version == 4:
            # 4 -> 5：标记测试报文，避免混入历史与统计
            columns = [row["name"] for row in _conn.execute("PRAGMA table_info(telemetry)")]
            if "is_test" not in columns:
                _conn.execute(
                    "ALTER TABLE telemetry ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0"
                )
            version = 5
        if version == 5:
            # 5 -> 6：为设备表补充下行网关类型（mqtt/ir/modbus），默认 mqtt
            columns = [row["name"] for row in _conn.execute("PRAGMA table_info(devices)")]
            if "gateway" not in columns:
                _conn.execute(
                    "ALTER TABLE devices ADD COLUMN gateway TEXT NOT NULL DEFAULT 'mqtt'"
                )
            version = 6
        if version not in (0, SCHEMA_VERSION):
            # 更旧或未知的结构直接重建
            for table in (
                "telemetry",
                "devices",
                "alarms",
                "room_state",
                "control_actions",
                "experiment_runs",
                "settings",
            ):
                _conn.execute(f"DROP TABLE IF EXISTS {table}")
        _conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        _conn.executescript(SCHEMA)
        _conn.commit()


def reset_runtime_tables(seed_devices: bool = True) -> None:
    """清空运行期数据；仿真模式重新写入内置设备，实际监测模式由设备自动注册。"""
    with _lock:
        for table in (
            "telemetry",
            "alarms",
            "room_state",
            "control_actions",
            "experiment_runs",
            "devices",
        ):
            _conn.execute(f"DELETE FROM {table}")
        if seed_devices:
            _conn.executemany(
                "INSERT INTO devices(device, room, type, name) VALUES (?,?,?,?)",
                [(device, room, dtype, name) for room, device, dtype, name in DEVICES],
            )
        _conn.commit()


def get_setting(key: str, default=None):
    with _lock:
        row = _conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        _conn.commit()


def save_real_devices(devices) -> None:
    """持久化实际监测设备登记，切换模式或重启后恢复（在线状态不保存）。"""
    fields = ("device", "room", "type", "name", "topic", "gateway")
    rows = [{k: str(d.get(k) or "") for k in fields} for d in devices]
    set_setting("real_devices", json.dumps(rows, ensure_ascii=False))


def load_real_devices() -> list[dict]:
    raw = get_setting("real_devices")
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return rows if isinstance(rows, list) else []


def ensure_device(device: str, room: str, kind: str = "env", name: str = "", gateway: str = "mqtt") -> None:
    """实际监测模式下自动登记首次上报的设备。"""
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO devices(device, room, type, name, online, gateway)"
            " VALUES (?,?,?,?,?,?)",
            (device, room, kind, name or device, 1, gateway),
        )
        _conn.commit()


def get_device(device: str):
    with _lock:
        row = _conn.execute("SELECT * FROM devices WHERE device=?", (device,)).fetchone()
        return dict(row) if row else None


def register_device(
    device: str, room: str, kind: str, name: str = "", topic: str = "", gateway: str = "mqtt"
) -> None:
    """手动接入设备：不存在则新建，已存在则更新名称/类型/机房/主题/网关。"""
    with _lock:
        _conn.execute(
            "INSERT INTO devices(device, room, type, name, online, topic, gateway)"
            " VALUES (?,?,?,?,0,?,?)"
            " ON CONFLICT(device) DO UPDATE SET room=excluded.room, type=excluded.type,"
            " name=excluded.name, topic=excluded.topic, gateway=excluded.gateway",
            (device, room, kind, name or device, topic, gateway),
        )
        _conn.commit()


def update_device_gateway(device: str, gateway: str) -> None:
    with _lock:
        _conn.execute("UPDATE devices SET gateway=? WHERE device=?", (gateway, device))
        _conn.commit()


def update_device_topic(device: str, topic: str) -> None:
    with _lock:
        _conn.execute("UPDATE devices SET topic=? WHERE device=?", (topic, device))
        _conn.commit()


def remove_device(device: str) -> None:
    """移除设备及其遥测与告警，机房快照会在下一个周期重新聚合。"""
    with _lock:
        _conn.execute("DELETE FROM telemetry WHERE device=?", (device,))
        _conn.execute("DELETE FROM alarms WHERE device=?", (device,))
        _conn.execute("DELETE FROM devices WHERE device=?", (device,))
        _conn.commit()


def insert_telemetry(rows, is_test: int = 0) -> None:
    """rows 为 (ts, room, device, metric, value)；is_test=1 的报文只用于链路验证。"""
    if not rows:
        return
    payload = [
        (ts, room, device, metric, value, is_test)
        for ts, room, device, metric, value in rows
    ]
    with _lock:
        _conn.executemany(
            "INSERT INTO telemetry(ts, room, device, metric, value, is_test)"
            " VALUES (?,?,?,?,?,?)",
            payload,
        )
        _conn.commit()


def touch_device(device: str, ts: float) -> None:
    with _lock:
        _conn.execute("UPDATE devices SET last_seen=?, online=1 WHERE device=?", (ts, device))
        _conn.commit()


def set_device_state(device: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        _conn.execute(f"UPDATE devices SET {cols} WHERE device=?", (*fields.values(), device))
        _conn.commit()


def set_online(device: str, online: bool) -> None:
    with _lock:
        _conn.execute("UPDATE devices SET online=? WHERE device=?", (1 if online else 0, device))
        _conn.commit()


def all_devices():
    with _lock:
        return [dict(r) for r in _conn.execute("SELECT * FROM devices ORDER BY room, device")]


def add_alarm(ts: float, room: str, device: str, rule: str, level: str, message: str) -> int:
    with _lock:
        cur = _conn.execute(
            "INSERT INTO alarms(ts, room, device, rule, level, message, status)"
            " VALUES (?,?,?,?,?,?, 'open')",
            (ts, room, device, rule, level, message),
        )
        _conn.commit()
        return int(cur.lastrowid)


def has_open_alarm(device: str, rule: str) -> bool:
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM alarms WHERE device=? AND rule=? AND status!='resolved' LIMIT 1",
            (device, rule),
        ).fetchone()
        return row is not None


def resolve_alarm(device: str, rule: str, ts: float) -> None:
    with _lock:
        _conn.execute(
            "UPDATE alarms SET status='resolved', resolved_ts=?"
            " WHERE device=? AND rule=? AND status!='resolved'",
            (ts, device, rule),
        )
        _conn.commit()


def has_open_room_alarm(room: str, rule: str) -> bool:
    """同机房环境类告警按 room+rule 去重，避免多传感器重复上报。"""
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM alarms WHERE room=? AND rule=? AND status!='resolved' LIMIT 1",
            (room, rule),
        ).fetchone()
        return row is not None


def resolve_room_alarm(room: str, rule: str, ts: float) -> None:
    with _lock:
        _conn.execute(
            "UPDATE alarms SET status='resolved', resolved_ts=?"
            " WHERE room=? AND rule=? AND status!='resolved'",
            (ts, room, rule),
        )
        _conn.commit()


def ack_alarm(alarm_id: int, ts: float, operator: str, note: str = "") -> bool:
    with _lock:
        cur = _conn.execute(
            "UPDATE alarms SET status='acked', acked_ts=?, acked_by=?, note=?"
            " WHERE id=? AND status='open'",
            (ts, operator or "值班员", note, alarm_id),
        )
        _conn.commit()
        return cur.rowcount > 0


def resolve_all_open(ts: float) -> int:
    with _lock:
        cur = _conn.execute(
            "UPDATE alarms SET status='resolved', resolved_ts=? WHERE status!='resolved'", (ts,)
        )
        _conn.commit()
        return cur.rowcount


def open_alarms(limit: int = 50):
    with _lock:
        return [
            dict(r)
            for r in _conn.execute(
                "SELECT * FROM alarms WHERE status!='resolved' ORDER BY ts DESC LIMIT ?", (limit,)
            )
        ]


def recent_alarms(limit: int = 20):
    with _lock:
        return [
            dict(r)
            for r in _conn.execute("SELECT * FROM alarms ORDER BY ts DESC LIMIT ?", (limit,))
        ]


def insert_room_state(ts, room, mode, temp, humidity, ac_level, fan, power_w) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO room_state(ts, room, mode, temp, humidity, ac_level, fan, power_w)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (ts, room, mode, temp, humidity, ac_level, fan, power_w),
        )
        _conn.commit()


def latest_room_state(room: str):
    with _lock:
        row = _conn.execute(
            "SELECT * FROM room_state WHERE room=? ORDER BY ts DESC LIMIT 1", (room,)
        ).fetchone()
        return dict(row) if row else None


def history(room: str, metric: str, minutes: float = 30.0):
    """返回 [{ts, device, value}]。metric 为 temperature/humidity/power。"""
    since = time.time() - minutes * 60
    with _lock:
        rows = _conn.execute(
            "SELECT ts, device, value FROM telemetry"
            " WHERE room=? AND metric=? AND ts>=? AND is_test=0 ORDER BY ts",
            (room, metric, since),
        ).fetchall()
        return [dict(r) for r in rows]


def recent_metric_latest(minutes: float = 1440.0):
    """重启后恢复：返回时间窗内每台设备每项指标的最新一条真实遥测。"""
    since = time.time() - minutes * 60
    with _lock:
        rows = _conn.execute(
            """
            SELECT t.room, t.device, t.metric, t.value, t.ts
            FROM telemetry t
            JOIN (
                SELECT device, metric, MAX(ts) AS mts FROM telemetry
                WHERE ts >= ? AND is_test = 0 GROUP BY device, metric
            ) m ON t.device = m.device AND t.metric = m.metric AND t.ts = m.mts
            """,
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]


def temp_series(room: str, seconds: float = 120.0):
    since = time.time() - seconds
    with _lock:
        rows = _conn.execute(
            "SELECT ts, value FROM telemetry"
            " WHERE room=? AND metric='temperature' AND ts>=? AND is_test=0 ORDER BY ts",
            (room, since),
        ).fetchall()
        return [(r["ts"], r["value"]) for r in rows]


def insert_action(ts, room, mode, target, action, reason, source) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO control_actions(ts, room, mode, target, action, reason, source)"
            " VALUES (?,?,?,?,?,?,?)",
            (ts, room, mode, target, action, reason, source),
        )
        _conn.commit()


def recent_actions(limit: int = 10):
    with _lock:
        return [
            dict(r)
            for r in _conn.execute("SELECT * FROM control_actions ORDER BY ts DESC LIMIT ?", (limit,))
        ]


def mode_metrics():
    """按调控模式聚合：平均制冷能耗、超温占比、最高温度、运行时长。"""
    with _lock:
        rows = _conn.execute(
            """
            SELECT mode,
                   COUNT(*) AS samples,
                   AVG(ac_level / 100.0 * 6.0 + fan * 0.6) AS avg_kw,
                   AVG(CASE WHEN temp > 28.0 THEN 1.0 ELSE 0 END) AS over_ratio,
                   MAX(temp) AS max_temp,
                   AVG(temp) AS avg_temp
            FROM room_state GROUP BY mode
            """
        ).fetchall()
        return [dict(r) for r in rows]


def purge_old(keep_minutes: float = 120.0, state_keep_minutes: float = 1440.0) -> None:
    """遥测保留 2 小时；机房状态保留 24 小时，保证实验明细仍可导出。"""
    now = time.time()
    since = now - keep_minutes * 60
    state_since = now - state_keep_minutes * 60
    with _lock:
        _conn.execute("DELETE FROM telemetry WHERE ts < ?", (since,))
        _conn.execute("DELETE FROM room_state WHERE ts < ?", (state_since,))
        _conn.commit()


# ---------------- 对照实验 ----------------
def run_stats(start: float, end: float) -> dict:
    """统计指定时间窗内两机房的制冷功率、超温占比、温度极值。"""
    with _lock:
        row = _conn.execute(
            """
            SELECT COUNT(*) AS samples,
                   AVG(ac_level / 100.0 * 6.0 + fan * 0.6) AS avg_kw,
                   AVG(CASE WHEN temp > 28.0 THEN 1.0 ELSE 0 END) AS over_ratio,
                   MAX(temp) AS max_temp,
                   AVG(temp) AS avg_temp
            FROM room_state WHERE ts >= ? AND ts <= ?
            """,
            (start, end),
        ).fetchone()

    duration = max(0.0, end - start)
    avg_kw = row["avg_kw"] or 0.0
    return {
        "avg_kw": round(avg_kw, 3),
        "over_ratio": round((row["over_ratio"] or 0.0) * 100, 2),
        "max_temp": round(row["max_temp"] or 0.0, 2),
        "avg_temp": round(row["avg_temp"] or 0.0, 2),
        "kwh": round(avg_kw * duration / 3600.0, 4),
        "samples": row["samples"] or 0,
        "duration": round(duration, 1),
    }


def insert_experiment_run(exp_id: str, mode: str, started: float, finished: float, stats: dict) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO experiment_runs(exp_id, mode, started, finished, duration,"
            " avg_kw, over_ratio, max_temp, avg_temp, kwh, samples)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                exp_id,
                mode,
                started,
                finished,
                stats["duration"],
                stats["avg_kw"],
                stats["over_ratio"],
                stats["max_temp"],
                stats["avg_temp"],
                stats["kwh"],
                stats["samples"],
            ),
        )
        _conn.commit()


def recent_experiment_runs(limit: int = 20):
    with _lock:
        return [
            dict(r)
            for r in _conn.execute(
                "SELECT * FROM experiment_runs ORDER BY id DESC LIMIT ?", (limit,)
            )
        ]


def experiment_samples(start: float, end: float):
    with _lock:
        return [
            dict(r)
            for r in _conn.execute(
                "SELECT ts, room, mode, temp, humidity, ac_level, fan, power_w"
                " FROM room_state WHERE ts >= ? AND ts <= ? ORDER BY ts, room",
                (start, end),
            )
        ]
