"""平台主程序：MQTT 接入、规则引擎、调控闭环、REST 与 WebSocket。"""

import asyncio
import csv
import io
import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import db
from config import (
    CONTROL,
    CONTROL_MODE_CN,
    CONTROL_MODES,
    DEVICE_TYPE_CN,
    DEVICE_TYPE_PREFIX,
    FRONTEND_DIR,
    METRICS_BY_TYPE,
    MODE,
    MODES,
    MQTT_ALLOW_ANONYMOUS,
    MQTT_BIND_HOST,
    MQTT_PORT,
    MQTT_USE_TLS,
    REAL_RETENTION_MINUTES,
    REAL_TICK_SECONDS,
    ROOMS,
    ROOM_SUFFIX,
    THRESHOLDS,
    TICK_SECONDS,
    VERSION,
)
from control import Controller
from experiment import ExperimentManager
from gateway import GATEWAY_CN, GATEWAY_TYPES, device_gateway
from mqtt import MqttBridge, start_broker
from rules import RuleEngine
from simulator import Simulator


class Runtime:
    def __init__(self) -> None:
        self.mode: str = MODE if MODE in MODES else "sim"
        self.sim = Simulator()
        self.engine = RuleEngine()
        self.ctl = Controller()
        self.latest_temp: dict[str, tuple[float, float]] = {}
        self.sensors: dict[str, dict[str, tuple[float, float]]] = {}
        # 同机房多路 PDU 功率求和后作为前馈输入
        self.latest_power: dict[str, tuple[float, float]] = {}
        self.power_sensors: dict[str, dict[str, tuple[float, float]]] = {}
        # 实际监测模式：保存每台设备每项指标的最新值 (ts, value)
        self.latest_metrics: dict[tuple[str, str, str], tuple[float, float]] = {}
        # 实际监测模式：记录每台设备已上报过的指标，用于接入后的合理性检查
        self.device_metrics: dict[str, set[str]] = {}
        # 实际监测模式：最近一次测试报文时间与测试收到的指标（仅用于接入验证）
        self.device_test: dict[str, float] = {}
        self.device_test_metrics: dict[str, set[str]] = {}
        # 自定义上报主题 -> (机房, 设备)，用于非 iot/ 标准主题的消息归属
        self.topic_map: dict[str, tuple[str, str]] = {}
        self.known_devices: set[str] = set()
        self.clients: set[WebSocket] = set()
        self.bridge: MqttBridge | None = None
        self.broker = None
        self.exp: ExperimentManager | None = None
        self.tasks: list[asyncio.Task] = []

    async def broadcast(self, message: dict) -> None:
        if not self.clients:
            return
        text = json.dumps(message, ensure_ascii=False)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


rt = Runtime()


def _device_type(device: str) -> str:
    """实际监测模式下按设备号前缀推断类型。"""
    return DEVICE_TYPE_PREFIX.get(device.split("-")[0].upper(), "env")


def _default_topic(room: str, device: str) -> str:
    return f"iot/{room}/{device}/telemetry"


def _device_topic(dev: dict) -> str:
    return dev.get("topic") or _default_topic(dev["room"], dev["device"])


def _persist_real_devices() -> None:
    """把当前真实设备登记写入设置表，供模式切换后恢复。"""
    db.save_real_devices(db.all_devices())


def _restore_real_devices() -> None:
    """切回实际监测模式时恢复设备编号、名称、类型、主题与网关。"""
    for row in db.load_real_devices():
        device = str(row.get("device", "")).strip()
        room = row.get("room")
        kind = row.get("type")
        if not device or room not in ROOMS or kind not in METRICS_BY_TYPE:
            continue
        topic = str(row.get("topic") or "")
        gateway = row.get("gateway")
        if gateway not in GATEWAY_TYPES:
            gateway = "mqtt"
        db.register_device(
            device, room, kind, str(row.get("name") or device), topic, gateway
        )
        rt.known_devices.add(device)
        if rt.bridge and topic and topic != _default_topic(room, device):
            rt.bridge.subscribe_topic(topic)
            rt.topic_map[topic] = (room, device)


def _topic_error(topic: str, room: str, device: str) -> str | None:
    """校验自定义上报主题：不允许通配符、空层级、空格或指向其他设备的标准主题。"""
    if not topic:
        return "上报主题不能为空"
    if len(topic) > 128:
        return "上报主题不超过 128 个字符"
    if any(ch in topic for ch in "+#$"):
        return "上报主题不能包含 + / # / $ 等通配符"
    if any(ch.isspace() for ch in topic):
        return "上报主题不能包含空格"
    if topic.endswith("/cmd"):
        return "上报主题不能以下行命令主题 /cmd 结尾"
    parts = topic.split("/")
    if len(parts) < 2 or any(not p for p in parts):
        return "上报主题至少两级，且不能出现空层级"
    if len(parts) == 4 and parts[0] == "iot" and parts[3] == "telemetry":
        if parts[1] != room or parts[2] != device:
            return "该主题会被识别为其他设备，请与机房/设备编号保持一致"
    return None


def _topic_owner(topic: str, exclude: str | None = None):
    for d in db.all_devices():
        if d["device"] != exclude and _device_topic(d) == topic:
            return d
    return None


def get_offline_seconds() -> float:
    """运行时可配置的离线判定阈值（秒），默认 20s，真实设备采样 30~60s 时可调大。"""
    try:
        val = db.get_setting("offline_seconds", str(THRESHOLDS["offline_seconds"]))
        seconds = float(val)
    except (TypeError, ValueError):
        seconds = THRESHOLDS["offline_seconds"]
    return max(5.0, min(86400.0, seconds))


def _latest_metric(room: str, metric: str, ts: float, max_age: float = 30.0):
    best_ts = None
    best_value = None
    for (r, _device, m), (t, v) in rt.latest_metrics.items():
        if r != room or m != metric or ts - t > max_age:
            continue
        if best_ts is None or t > best_ts:
            best_ts, best_value = t, v
    return best_value


def real_room_snapshots(ts: float) -> dict[str, dict]:
    """实际监测模式：用设备遥测聚合机房快照，供大屏与入库使用。

    真实设备采样常见 30~60s，快照新鲜窗口跟随离线阈值，避免两次上报之间
    大屏反复出现“无实时数据”。
    """
    fresh = max(30.0, get_offline_seconds())
    snaps: dict[str, dict] = {}
    for room in ROOMS:
        temps = [
            v
            for (r, _d, m), (t, v) in rt.latest_metrics.items()
            if r == room and m == "temperature" and ts - t <= fresh
        ]
        if not temps:
            continue
        hums = [
            v
            for (r, _d, m), (t, v) in rt.latest_metrics.items()
            if r == room and m == "humidity" and ts - t <= fresh
        ]
        powers = [
            v
            for (r, _d, m), (t, v) in rt.latest_metrics.items()
            if r == room and m == "power" and ts - t <= fresh
        ]
        snaps[room] = {
            "temp": round(sum(temps) / len(temps), 2),
            "humidity": round(sum(hums) / len(hums), 1) if hums else 0.0,
            "ac_level": int(_latest_metric(room, "ac_level", ts, max_age=fresh) or 0),
            # 排风状态兼容 fan_state / fan 两种字段名
            "fan": int(
                _latest_metric(room, "fan_state", ts, max_age=fresh)
                or _latest_metric(room, "fan", ts, max_age=fresh)
                or 0
            ),
            "power_w": round(sum(powers), 1),
        }
    return snaps


async def handle_message(topic: str, payload: dict) -> None:
    """订阅回调：上行遥测落库 + 规则判定；仿真模式下才交给设备仿真器。"""
    parts = topic.split("/")
    mapped = rt.topic_map.get(topic)
    if mapped:
        room, device = mapped
        kind = "telemetry"
    elif len(parts) == 4 and parts[0] == "iot":
        _, room, device, kind = parts
    else:
        return

    if kind == "cmd":
        # 实际监测模式下命令由真实设备/网关执行，平台不回灌仿真器
        if rt.mode == "sim":
            rt.sim.apply_command(room, device, payload)
        return

    if kind != "telemetry":
        return

    ts = float(payload.get("ts", time.time()))
    metrics = payload.get("metrics") or {}
    if not metrics:
        return

    is_test = bool(payload.get("test"))

    if is_test:
        # 测试报文：仅验证链路与报文格式。写入遥测表时标记 is_test=1，
        # 不更新最新值、不触发告警、不影响在线状态，所有统计查询均过滤 is_test=0。
        if rt.mode != "real":
            return
        rows = [(ts, room, device, m, float(v)) for m, v in metrics.items()]
        db.insert_telemetry(rows, is_test=1)
        rt.device_test[device] = ts
        for metric in metrics:
            rt.device_test_metrics.setdefault(device, set()).add(metric)
        await rt.broadcast({"type": "device", "action": "test", "device": device})
        return

    if rt.mode == "real" and device not in rt.known_devices:
        db.ensure_device(device, room, _device_type(device))
        rt.known_devices.add(device)
        _persist_real_devices()

    rows = [(ts, room, device, m, float(v)) for m, v in metrics.items()]
    db.insert_telemetry(rows, is_test=0)
    db.touch_device(device, ts)

    for metric, value in metrics.items():
        value = float(value)
        rt.latest_metrics[(room, device, metric)] = (ts, value)
        if rt.mode == "real":
            rt.device_metrics.setdefault(device, set()).add(metric)
        if metric == "temperature":
            # 同机房多传感器取最近 5 秒内的均值，作为该机房上报温度
            bucket = rt.sensors.setdefault(room, {})
            bucket[device] = (ts, value)
            fresh = [v for t, v in bucket.values() if ts - t <= 5.0]
            if fresh:
                avg = sum(fresh) / len(fresh)
                rt.latest_temp[room] = (ts, avg)
                rt.ctl.observe(room, ts, avg)
        elif metric == "power":
            # 同机房多路 PDU 在最近 5 秒内的功率求和，供按需制冷前馈使用
            bucket = rt.power_sensors.setdefault(room, {})
            bucket[device] = (ts, value)
            fresh = [v for t, v in bucket.values() if ts - t <= 5.0]
            if fresh:
                rt.latest_power[room] = (ts, sum(fresh))
        for event in rt.engine.evaluate(ts, room, device, metric, value):
            await rt.broadcast(event)


async def ingest_loop() -> None:
    while True:
        try:
            topic, payload = await rt.queue.get()
            await handle_message(topic, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 单条报文异常不影响整体链路
            print(f"[ingest] 处理失败: {exc}")


async def tick_loop() -> None:
    """仿真模式推进热模型；实际监测模式聚合真实遥测并按周期推送大屏。"""
    while True:
        if rt.mode == "sim":
            sim = rt.sim
            started = time.time()
            readings, snapshots = await asyncio.to_thread(sim.step, TICK_SECONDS)
            # 切换模式后，旧仿真实例产生的结果直接丢弃
            if sim is not rt.sim or rt.mode != "sim":
                continue
            ts = time.time()

            for item in readings:
                if rt.bridge:
                    rt.bridge.publish(
                        f"iot/{item['room']}/{item['device']}/telemetry",
                        {"ts": ts, "metrics": item["metrics"]},
                        apply_link=True,
                    )

            for room, snap in snapshots.items():
                db.insert_room_state(
                    ts,
                    room,
                    rt.ctl.mode,
                    snap["temp"],
                    snap["humidity"],
                    snap["ac_level"],
                    snap["fan"],
                    snap["power_w"],
                )

            await rt.broadcast(
                {
                    "type": "tick",
                    "ts": ts,
                    "mode": rt.ctl.mode,
                    "system": "sim",
                    "rooms": snapshots,
                    "readings": readings,
                    "control": {room: rt.ctl.current(room) for room in ROOMS},
                    "link": rt.bridge.link_stats() if rt.bridge else None,
                }
            )
            await asyncio.sleep(max(0.0, TICK_SECONDS - (time.time() - started)))
        else:
            ts = time.time()
            snapshots = real_room_snapshots(ts)
            for room, snap in snapshots.items():
                db.insert_room_state(
                    ts,
                    room,
                    "real",
                    snap["temp"],
                    snap["humidity"],
                    snap["ac_level"],
                    snap["fan"],
                    snap["power_w"],
                )
            await rt.broadcast(
                {
                    "type": "tick",
                    "ts": ts,
                    "mode": "real",
                    "system": "real",
                    "rooms": snapshots,
                    "readings": [],
                    "control": {},
                    "link": rt.bridge.link_stats() if rt.bridge else None,
                }
            )
            await asyncio.sleep(REAL_TICK_SECONDS)


async def control_loop() -> None:
    """每 2 秒依据上报温度决策，命令走 MQTT 下发（仅仿真模式启用自动调控）。"""
    while True:
        await asyncio.sleep(2.0)
        if rt.mode != "sim":
            continue
        ts = time.time()
        for room in ROOMS:
            latest = rt.latest_temp.get(room)
            if not latest or ts - latest[0] > 10:
                continue
            power = rt.latest_power.get(room)
            power_w = power[1] if power and ts - power[0] <= 10 else 0.0
            cmd = rt.ctl.decide(room, latest[1], power_w)
            if not cmd or not rt.bridge:
                continue

            rt.bridge.publish(
                f"iot/{room}/{cmd['target']}/cmd",
                {"level": cmd["level"], "source": rt.ctl.mode},
            )
            rt.bridge.publish(
                f"iot/{room}/{cmd['fan_target']}/cmd",
                {"on": cmd["fan"], "source": rt.ctl.mode},
            )
            db.insert_action(
                ts,
                room,
                rt.ctl.mode,
                cmd["target"],
                f"AC={cmd['level']}%, FAN={'on' if cmd['fan'] else 'off'}",
                cmd["reason"],
                rt.ctl.mode,
            )
            await rt.broadcast(
                {
                    "type": "action",
                    "ts": ts,
                    "room": room,
                    "mode": rt.ctl.mode,
                    "target": cmd["target"],
                    "action": f"AC={cmd['level']}%, FAN={'on' if cmd['fan'] else 'off'}",
                    "reason": cmd["reason"],
                }
            )


async def housekeeping_loop() -> None:
    """设备离线检测 + 历史数据保留窗口。"""
    last_purge = 0.0
    while True:
        await asyncio.sleep(5.0)
        now = time.time()
        offline_seconds = get_offline_seconds()
        for dev in db.all_devices():
            last_seen = dev["last_seen"] or 0
            # 从未上报过的设备不判定离线，避免启动瞬间产生误告警
            is_offline = last_seen > 0 and now - last_seen > offline_seconds
            if is_offline:
                if dev["online"]:
                    db.set_online(dev["device"], False)
                if not db.has_open_alarm(dev["device"], "offline"):
                    db.add_alarm(
                        now, dev["room"], dev["device"], "offline", "critical", "设备心跳超时，判定离线"
                    )
                    await rt.broadcast(
                        {
                            "type": "alarm",
                            "ts": now,
                            "room": dev["room"],
                            "device": dev["device"],
                            "rule": "offline",
                            "level": "critical",
                            "message": "设备心跳超时，判定离线",
                        }
                    )
            else:
                if not dev["online"]:
                    db.set_online(dev["device"], True)
                # 遥测已恢复：即使 touch_device 已把 online 置回 1，也要收敛遗留的离线告警
                if db.has_open_alarm(dev["device"], "offline"):
                    db.resolve_alarm(dev["device"], "offline", now)

        if now - last_purge > 60:
            if rt.mode == "real":
                # 实际监测保留 30 天，便于回溯
                db.purge_old(
                    keep_minutes=REAL_RETENTION_MINUTES,
                    state_keep_minutes=REAL_RETENTION_MINUTES,
                )
            else:
                db.purge_old(keep_minutes=120)
            last_purge = now


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()

    # 运行模式：环境变量显式指定时优先，否则沿用上次持久化的选择
    persisted = db.get_setting("mode", "sim")
    initial = os.environ.get("IOT_MODE") or persisted
    rt.mode = initial if initial in MODES else "sim"
    # 仿真模式每次从干净状态开始；模式发生变化时清掉上一模式的数据，避免两路数据混在一起
    if rt.mode == "sim" or rt.mode != persisted:
        db.reset_runtime_tables(seed_devices=(rt.mode == "sim"))
    db.set_setting("mode", rt.mode)
    rt.known_devices = {d["device"] for d in db.all_devices()}
    rt.topic_map = {
        d["topic"]: (d["room"], d["device"]) for d in db.all_devices() if d.get("topic")
    }
    if rt.mode == "real":
        # 重启后从最近 24 小时遥测恢复最新值与已收指标，避免刚启动时误报“缺少指标”
        for row in db.recent_metric_latest(minutes=24 * 60):
            rt.latest_metrics[(row["room"], row["device"], row["metric"])] = (
                row["ts"],
                row["value"],
            )
            rt.device_metrics.setdefault(row["device"], set()).add(row["metric"])

    rt.queue = asyncio.Queue(maxsize=8000)
    loop = asyncio.get_running_loop()

    async def enqueue(topic: str, payload: dict) -> None:
        try:
            rt.queue.put_nowait((topic, payload))
        except asyncio.QueueFull:
            pass

    rt.broker = await start_broker()
    rt.bridge = MqttBridge(loop, enqueue)
    rt.bridge.start()
    for topic in rt.topic_map:
        rt.bridge.subscribe_topic(topic)
    rt.exp = ExperimentManager(rt.sim, rt.ctl, rt.engine, rt.broadcast)

    rt.tasks = [
        asyncio.create_task(ingest_loop()),
        asyncio.create_task(tick_loop()),
        asyncio.create_task(control_loop()),
        asyncio.create_task(housekeeping_loop()),
    ]
    print(f"[平台] MQTT Broker 已启动，运行模式：{MODES[rt.mode]}")
    if rt.mode == "real" and MQTT_BIND_HOST in ("127.0.0.1", "localhost"):
        print(
            "[平台] 提示：当前 Broker 只监听本机；接入局域网设备请设置 "
            "IOT_MQTT_BIND=0.0.0.0 后重启"
        )
    try:
        yield
    finally:
        for task in rt.tasks:
            task.cancel()
        await asyncio.gather(*rt.tasks, return_exceptions=True)
        if rt.exp and rt.exp.running:
            rt.exp.task.cancel()
            await asyncio.gather(rt.exp.task, return_exceptions=True)
        if rt.bridge:
            rt.bridge.stop()
        if rt.broker:
            await rt.broker.shutdown()


app = FastAPI(title="智慧机房物联网仿真平台", version=VERSION, lifespan=lifespan)


@app.get("/api/bootstrap")
async def bootstrap():
    metrics = db.mode_metrics()
    for row in metrics:
        # samples 是 room_state 的行数，每个采样周期会为每个机房各写一行
        row["minutes"] = round((row["samples"] or 0) / len(ROOMS) * TICK_SECONDS / 60.0, 1)
        row["avg_kw"] = round(row["avg_kw"] or 0, 2)
        row["over_ratio"] = round((row["over_ratio"] or 0) * 100, 1)
        row["max_temp"] = round(row["max_temp"] or 0, 1)
        row["avg_temp"] = round(row["avg_temp"] or 0, 1)
    return {
        "version": VERSION,
        "mode": rt.ctl.mode,
        "system": _system_info(),
        "thresholds": THRESHOLDS,
        "control_config": CONTROL,
        "rooms": [
            {
                "room": room,
                "name": cfg["name"],
                "state": db.latest_room_state(room) or {},
            }
            for room, cfg in ROOMS.items()
        ],
        "devices": db.all_devices(),
        "device_latest": db.recent_metric_latest(minutes=5),
        "alarms": db.open_alarms(30),
        "alarm_history": db.recent_alarms(20),
        "actions": db.recent_actions(12),
        "metrics": metrics,
        "alarm_stats": _alarm_stats(),
        "experiment": rt.exp.status() if rt.exp else {"phase": "idle", "runs": []},
        "experiment_history": db.recent_experiment_runs(10),
        "link": rt.bridge.link_stats() if rt.bridge else {},
    }


def _system_info() -> dict:
    return {
        "mode": rt.mode,
        "mode_cn": MODES[rt.mode],
        "modes": MODES,
        "mqtt_bind": MQTT_BIND_HOST,
        "mqtt_port": MQTT_PORT,
        "mqtt_auth": "anonymous" if MQTT_ALLOW_ANONYMOUS else "password",
        "mqtt_tls": MQTT_USE_TLS,
        # 心跳超时阈值，页面据此提示，可通过 /api/system/offline-seconds 运行时调整
        "offline_seconds": get_offline_seconds(),
    }


def _alarm_stats() -> dict:
    open_list = db.open_alarms(200)
    return {
        "active": len(open_list),
        "critical": sum(1 for a in open_list if a["level"] == "critical"),
        "warning": sum(1 for a in open_list if a["level"] == "warning"),
        "unacked": sum(1 for a in open_list if a["status"] == "open"),
    }


@app.get("/api/history")
async def history(room: str, metric: str = "temperature", minutes: float = 30.0):
    rows = db.history(room, metric, minutes)
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row["device"], []).append([round(row["ts"], 2), row["value"]])
    return {
        "room": room,
        "metric": metric,
        "series": [{"name": name, "points": pts} for name, pts in grouped.items()],
    }


@app.get("/api/alarms")
async def alarms(limit: int = 50):
    return {"open": db.open_alarms(limit), "history": db.recent_alarms(limit)}


@app.post("/api/alarms/ack")
async def ack_alarm(payload: dict):
    alarm_id = payload.get("id")
    if not alarm_id:
        return JSONResponse({"error": "缺少告警 id"}, status_code=400)
    try:
        alarm_id = int(alarm_id)
    except (TypeError, ValueError):
        return JSONResponse({"error": "告警 id 必须为整数"}, status_code=400)
    ok = db.ack_alarm(
        alarm_id, time.time(), payload.get("operator", "值班员"), payload.get("note", "")
    )
    if not ok:
        return JSONResponse({"error": "告警不存在或已确认"}, status_code=404)
    await rt.broadcast({"type": "alarm", "ts": time.time()})
    return {"ok": True}


@app.get("/api/metrics")
async def metrics():
    return {"metrics": db.mode_metrics()}


@app.post("/api/system/mode")
async def set_system_mode(payload: dict):
    """切换仿真 / 实际监测模式；切换会清空运行期数据，避免两种数据源混用。"""
    mode = payload.get("mode")
    if mode not in MODES:
        return JSONResponse({"error": "mode 仅支持 sim / real"}, status_code=400)
    if mode == rt.mode:
        return {"mode": rt.mode, "system": _system_info()}

    if rt.exp and rt.exp.running:
        rt.exp.task.cancel()
        await asyncio.gather(rt.exp.task, return_exceptions=True)

    # 离开实际监测模式前保存设备登记，避免切回仿真后丢失真实设备配置
    if rt.mode == "real":
        _persist_real_devices()
    db.reset_runtime_tables(seed_devices=(mode == "sim"))
    db.set_setting("mode", mode)
    rt.mode = mode
    rt.latest_temp.clear()
    rt.sensors.clear()
    rt.latest_power.clear()
    rt.power_sensors.clear()
    rt.latest_metrics.clear()
    rt.device_metrics.clear()
    rt.device_test.clear()
    rt.device_test_metrics.clear()
    rt.topic_map.clear()
    rt.known_devices = {d["device"] for d in db.all_devices()}
    # 重建仿真/控制/规则实例，清掉上一模式的状态
    rt.engine = RuleEngine()
    rt.ctl = Controller()
    rt.sim = Simulator()
    rt.exp = ExperimentManager(rt.sim, rt.ctl, rt.engine, rt.broadcast)
    if rt.bridge:
        for topic in list(rt.bridge.custom_topics):
            rt.bridge.unsubscribe_topic(topic)
        rt.bridge.drop_buffer()
    # 切回实际监测时恢复上次登记的真实设备与自定义上报主题
    if mode == "real":
        _restore_real_devices()

    await rt.broadcast({"type": "system", "mode": mode})
    return {"mode": mode, "system": _system_info()}


@app.post("/api/system/offline-seconds")
async def set_offline_seconds(payload: dict):
    """运行时调整设备离线判定阈值（秒）。真实设备采样 30~60s 时建议设为 90~120s。"""
    try:
        seconds = float(payload.get("seconds"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "seconds 必须为数字"}, status_code=400)
    if seconds < 5 or seconds > 86400:
        return JSONResponse({"error": "阈值需在 5 ~ 86400 秒之间"}, status_code=400)
    db.set_setting("offline_seconds", str(seconds))
    # 阈值放宽后，原本被误判离线的设备立即重新评估
    now = time.time()
    for dev in db.all_devices():
        last_seen = dev["last_seen"] or 0
        if last_seen > 0 and now - last_seen <= seconds and not dev["online"]:
            db.set_online(dev["device"], True)
            if db.has_open_alarm(dev["device"], "offline"):
                db.resolve_alarm(dev["device"], "offline", now)
    await rt.broadcast({"type": "system", "mode": rt.mode})
    return {"ok": True, "offline_seconds": seconds, "system": _system_info()}


# ---------------- 实际监测：设备接入向导 ----------------
REAL_DEVICE_RE = re.compile(r"^[A-Za-z]{2,4}-[A-Za-z0-9_]{1,16}$")
TEST_METRICS = {
    "env": {"temperature": 25.6, "humidity": 55.0},
    "power": {"power": 1800.0, "current": 8.2, "voltage": 220.0},
    "smoke": {"smoke": 0.02},
    "water": {"water_leak": 0.0},
    "door": {"door_state": 0},
    "ups": {"battery": 96.0, "ups_load": 55.0, "output_voltage": 220.0},
    "airflow": {"airflow": 0.6},
    "actuator": {"ac_level": 60, "fan_state": 0},
}


def _telemetry_template(device: str, room: str, kind: str) -> dict:
    return {
        "ts": round(time.time(), 3),
        "room": room,
        "device": device,
        "metrics": dict(TEST_METRICS.get(kind, {"temperature": 25.0})),
    }


def _real_device_info(dev: dict, now: float) -> dict:
    metrics = sorted(rt.device_metrics.get(dev["device"], set()))
    expected = METRICS_BY_TYPE.get(dev["type"], [])
    last_seen = dev["last_seen"] or 0
    last_test = rt.device_test.get(dev["device"], 0)
    offline_seconds = get_offline_seconds()
    # 真实遥测优先；只有测试报文时标记为“测试数据”，便于区分是否真的上线
    if last_seen > 0 and now - last_seen <= offline_seconds:
        status = "online"
    elif last_test > 0 and now - last_test <= offline_seconds:
        status = "test"
    elif last_seen > 0:
        status = "offline"
    else:
        status = "waiting"
    return {
        "device": dev["device"],
        "name": dev["name"],
        "room": dev["room"],
        "room_name": ROOMS.get(dev["room"], {}).get("name", dev["room"]),
        "type": dev["type"],
        "type_cn": DEVICE_TYPE_CN.get(dev["type"], dev["type"]),
        "gateway": dev.get("gateway") or "mqtt",
        "gateway_cn": GATEWAY_CN.get(dev.get("gateway") or "mqtt", dev.get("gateway") or "mqtt"),
        "last_seen": last_seen,
        "status": status,
        "metrics": metrics,
        "expected": expected,
        "missing": [m for m in expected if m not in metrics],
        "test_metrics": sorted(rt.device_test_metrics.get(dev["device"], set())),
        "last_test": last_test,
        "topic": _device_topic(dev),
    }


@app.get("/api/real/devices")
async def real_devices():
    if rt.mode != "real":
        return JSONResponse({"error": "仅实际监测模式下可管理设备"}, status_code=409)
    now = time.time()
    return {"devices": [_real_device_info(d, now) for d in db.all_devices()]}


@app.get("/api/real/devices/{device}/template")
async def real_device_template(device: str):
    """编辑设备时回填名称/类型/机房/主题与示例报文。"""
    if rt.mode != "real":
        return JSONResponse({"error": "仅实际监测模式下可管理设备"}, status_code=409)
    dev = db.get_device(device)
    if not dev:
        return JSONResponse({"error": "设备不存在"}, status_code=404)
    return {
        "device": dev["device"],
        "name": dev["name"],
        "room": dev["room"],
        "type": dev["type"],
        "topic": _device_topic(dev),
        "gateway": dev.get("gateway") or "mqtt",
        "payload": _telemetry_template(dev["device"], dev["room"], dev["type"]),
        "expected": METRICS_BY_TYPE.get(dev["type"], []),
    }


@app.post("/api/real/devices")
async def register_real_device(payload: dict):
    """校验设备信息并登记，返回主题与示例报文，设备上报后自动上线。

    同一路径同时支持「新接入」与「编辑」：设备编号已存在时为编辑模式，
    可修改名称、机房、类型与上报主题；编号前缀与类型不一致时仅作提示，
    不强制阻断（编辑场景下可能需要保留历史编号）。
    """
    if rt.mode != "real":
        return JSONResponse({"error": "请先切换到实际监测模式"}, status_code=409)

    room = payload.get("room", "roomA")
    if room not in ROOMS:
        return JSONResponse({"error": "机房仅支持 roomA / roomB"}, status_code=400)

    device = str(payload.get("device", "")).strip()
    if not REAL_DEVICE_RE.match(device):
        return JSONResponse(
            {"error": "设备编号格式应形如 TH-101：2~4 位字母前缀 + “-” + 编号"},
            status_code=400,
        )

    name = str(payload.get("name", "")).strip() or device
    if len(name) > 24:
        return JSONResponse({"error": "设备名称不超过 24 个字符"}, status_code=400)

    gateway = str(payload.get("gateway", "mqtt")).strip() or "mqtt"
    if gateway not in GATEWAY_TYPES:
        return JSONResponse({"error": f"网关类型仅支持 {' / '.join(GATEWAY_TYPES)}"}, status_code=400)

    prefix = device.split("-")[0].upper()
    inferred = DEVICE_TYPE_PREFIX.get(prefix)
    selected = payload.get("type") or inferred or "env"
    if selected not in METRICS_BY_TYPE:
        return JSONResponse({"error": "设备类型不支持"}, status_code=400)

    # 主题可自定义；留空则使用标准 iot/<机房>/<设备>/telemetry
    topic = str(payload.get("topic", "")).strip() or _default_topic(room, device)
    topic_error = _topic_error(topic, room, device)
    if topic_error:
        return JSONResponse({"error": topic_error}, status_code=400)
    owner = _topic_owner(topic, device)
    if owner:
        return JSONResponse(
            {"error": f"该主题已被设备 {owner['device']} 占用"}, status_code=409
        )

    existing = db.get_device(device)
    old_topic = _device_topic(existing) if existing else ""
    old_room = existing["room"] if existing else room

    db.register_device(device, room, selected, name, topic, gateway)
    if rt.bridge and old_topic and old_topic != topic:
        if old_topic != _default_topic(old_room, device):
            rt.bridge.unsubscribe_topic(old_topic)
        rt.topic_map.pop(old_topic, None)
    if rt.bridge and topic != _default_topic(room, device):
        rt.bridge.subscribe_topic(topic)
        rt.topic_map[topic] = (room, device)
    rt.known_devices.add(device)
    _persist_real_devices()
    now = time.time()
    info = _real_device_info(db.get_device(device), now)
    action = "update" if existing else "register"
    await rt.broadcast({"type": "device", "action": action, "device": info})
    return {
        "ok": True,
        "device": info,
        "topic": info["topic"],
        "payload": _telemetry_template(device, room, selected),
        "expected": METRICS_BY_TYPE[selected],
        "type_mismatch_warning": (
            f"编号前缀 {prefix}- 对应「{DEVICE_TYPE_CN[inferred]}」，"
            f"当前选择「{DEVICE_TYPE_CN[selected]}」"
            if inferred and selected != inferred
            else None
        ),
    }


@app.post("/api/real/devices/test")
async def test_real_device(payload: dict):
    """发布测试遥测；可携带页面编辑后的报文内容。"""
    if rt.mode != "real":
        return JSONResponse({"error": "仅实际监测模式下可用"}, status_code=409)
    device = str(payload.get("device", "")).strip()
    dev = db.get_device(device)
    if not dev:
        return JSONResponse({"error": "请先接入该设备"}, status_code=404)
    if not rt.bridge:
        return JSONResponse({"error": "链路未就绪"}, status_code=503)

    raw = payload.get("payload")
    if raw is None:
        body = _telemetry_template(device, dev["room"], dev["type"])
    else:
        if not isinstance(raw, dict):
            return JSONResponse({"error": "报文必须是 JSON 对象"}, status_code=400)
        metrics = raw.get("metrics")
        if not isinstance(metrics, dict) or not metrics:
            return JSONResponse({"error": "报文缺少 metrics 指标对象"}, status_code=400)
        clean = {}
        for key, value in metrics.items():
            key = str(key).strip()
            if not key or len(key) > 32:
                return JSONResponse({"error": "指标名不能为空且不超过 32 个字符"}, status_code=400)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return JSONResponse(
                    {"error": f"指标 {key} 的值必须是数字"}, status_code=400
                )
            clean[key] = float(value)
        body = {
            "ts": time.time(),
            "room": dev["room"],
            "device": device,
            "metrics": clean,
        }

    # 标记为测试报文，平台只校验链路与格式，不写入历史/统计，也不触发告警
    body["test"] = True
    rt.bridge.publish(_device_topic(dev), body, apply_link=False)
    return {"ok": True, "published": body, "topic": _device_topic(dev)}


@app.post("/api/real/devices/topic")
async def update_real_device_topic(payload: dict):
    """修改设备上报主题；留空恢复为标准主题。"""
    if rt.mode != "real":
        return JSONResponse({"error": "仅实际监测模式下可管理设备"}, status_code=409)
    device = str(payload.get("device", "")).strip()
    dev = db.get_device(device)
    if not dev:
        return JSONResponse({"error": "设备不存在"}, status_code=404)

    standard = _default_topic(dev["room"], device)
    topic = str(payload.get("topic", "")).strip() or standard
    topic_error = _topic_error(topic, dev["room"], device)
    if topic_error:
        return JSONResponse({"error": topic_error}, status_code=400)
    owner = _topic_owner(topic, device)
    if owner:
        return JSONResponse(
            {"error": f"该主题已被设备 {owner['device']} 占用"}, status_code=409
        )

    old = _device_topic(dev)
    if old == topic:
        return {"ok": True, "topic": topic}
    db.update_device_topic(device, "" if topic == standard else topic)
    if rt.bridge:
        if old != standard:
            rt.bridge.unsubscribe_topic(old)
        if topic != standard:
            rt.bridge.subscribe_topic(topic)
    rt.topic_map.pop(old, None)
    if topic != standard:
        rt.topic_map[topic] = (dev["room"], device)
    _persist_real_devices()
    await rt.broadcast({"type": "device", "action": "topic", "device": device})
    return {"ok": True, "topic": topic}


@app.post("/api/real/devices/gateway")
async def update_device_gateway(payload: dict):
    """修改设备下行网关类型：mqtt（直连）/ ir（红外）/ modbus（Modbus）。"""
    if rt.mode != "real":
        return JSONResponse({"error": "仅实际监测模式下可管理设备"}, status_code=409)
    device = str(payload.get("device", "")).strip()
    dev = db.get_device(device)
    if not dev:
        return JSONResponse({"error": "设备不存在"}, status_code=404)
    gateway = str(payload.get("gateway", "mqtt")).strip() or "mqtt"
    if gateway not in GATEWAY_TYPES:
        return JSONResponse({"error": f"网关类型仅支持 {' / '.join(GATEWAY_TYPES)}"}, status_code=400)
    db.update_device_gateway(device, gateway)
    _persist_real_devices()
    await rt.broadcast({"type": "device", "action": "gateway", "device": device})
    return {"ok": True, "device": device, "gateway": gateway, "gateway_cn": GATEWAY_CN[gateway]}


@app.delete("/api/real/devices/{device}")
async def delete_real_device(device: str):
    if rt.mode != "real":
        return JSONResponse({"error": "仅实际监测模式下可管理设备"}, status_code=409)
    dev = db.get_device(device)
    if not dev:
        return JSONResponse({"error": "设备不存在"}, status_code=404)
    old_topic = _device_topic(dev)
    if rt.bridge and old_topic != _default_topic(dev["room"], device):
        rt.bridge.unsubscribe_topic(old_topic)
    rt.topic_map.pop(old_topic, None)
    db.remove_device(device)
    rt.known_devices.discard(device)
    rt.device_metrics.pop(device, None)
    rt.device_test.pop(device, None)
    rt.device_test_metrics.pop(device, None)
    for key in [k for k in rt.latest_metrics if k[1] == device]:
        rt.latest_metrics.pop(key, None)
    _persist_real_devices()
    await rt.broadcast({"type": "device", "action": "remove", "device": device})
    return {"ok": True}


@app.post("/api/mode")
async def set_mode(payload: dict):
    if rt.mode != "sim":
        return JSONResponse({"error": "实际监测模式下不启用自动调控"}, status_code=409)
    mode = payload.get("mode")
    if mode not in CONTROL_MODES:
        return JSONResponse({"error": "mode 仅支持 threshold / adaptive / predictive"}, status_code=400)
    if rt.exp and rt.exp.running:
        return JSONResponse({"error": "对照实验运行中，模式由实验自动控制"}, status_code=409)
    rt.ctl.mode = mode
    await rt.broadcast({"type": "mode", "mode": mode})
    return {"mode": mode}


@app.post("/api/control")
async def manual_control(payload: dict):
    room = payload.get("room")
    if room not in ROOMS:
        return JSONResponse({"error": "未知机房"}, status_code=400)
    if rt.exp and rt.exp.running:
        return JSONResponse({"error": "对照实验运行中，不接受手动控制"}, status_code=409)
    suffix = ROOM_SUFFIX[room]
    device = payload.get("device", f"AC-{suffix}")
    try:
        level = int(payload.get("level", 0))
        fan = int(payload.get("fan", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "level / fan 必须为整数"}, status_code=400)
    # 与设备执行侧保持一致，避免日志记录越界档位而设备实际被钳制
    level = max(0, min(100, level))
    fan = 1 if fan else 0

    # 通过下行网关适配器下发命令：仿真模式用直连 MQTT，真实设备可配红外/Modbus 网关
    ac_gw = device_gateway(device, rt.bridge)
    fan_device = f"FAN-{suffix}"
    fan_gw = device_gateway(fan_device, rt.bridge)
    ac_result = ac_gw.send_ac(room, device, level, "manual")
    fan_result = fan_gw.send_fan(room, fan_device, bool(fan), "manual")

    ts = time.time()
    await rt.broadcast(
        {
            "type": "action",
            "ts": ts,
            "room": room,
            "mode": "manual",
            "target": device,
            "action": f"AC={level}%, FAN={'on' if fan else 'off'}",
            "reason": f"手动下发（{ac_result['gateway']} 网关）",
        }
    )
    return {"ok": True, "ac": ac_result, "fan": fan_result}


@app.post("/api/demo/load")
async def demo_load(payload: dict):
    if rt.mode != "sim":
        return JSONResponse({"error": "实际监测模式下不支持演示注入"}, status_code=409)
    room = payload.get("room", "roomA")
    delta = float(payload.get("delta_w", 3000))
    if room not in ROOMS:
        return JSONResponse({"error": "未知机房"}, status_code=400)
    if rt.exp and rt.exp.running:
        return JSONResponse({"error": "对照实验运行中，负载由实验脚本统一注入"}, status_code=409)
    rt.sim.inject_load(room, delta)
    await rt.broadcast({"type": "notice", "message": f"{ROOMS[room]['name']} 注入负载 {delta:.0f}W"})
    return {"ok": True, "room": room, "delta_w": delta}


@app.post("/api/demo/smoke")
async def demo_smoke(payload: dict):
    if rt.mode != "sim":
        return JSONResponse({"error": "实际监测模式下不支持演示注入"}, status_code=409)
    room = payload.get("room", "roomA")
    level = float(payload.get("level", 0.0))
    if room not in ROOMS:
        return JSONResponse({"error": "未知机房"}, status_code=400)
    rt.sim.set_smoke(room, level)
    return {"ok": True}


@app.post("/api/demo/fault")
async def demo_fault(payload: dict):
    if rt.mode != "sim":
        return JSONResponse({"error": "实际监测模式下不支持演示注入"}, status_code=409)
    device = payload.get("device")
    paused = bool(payload.get("paused", True))
    if not device:
        return JSONResponse({"error": "缺少 device"}, status_code=400)
    rt.sim.set_paused(device, paused)
    db.set_device_state(device, paused=1 if paused else 0)
    if not paused:
        db.set_online(device, True)
        db.resolve_alarm(device, "offline", time.time())
    return {"ok": True, "device": device, "paused": paused}


@app.post("/api/demo/water-leak")
async def demo_water_leak(payload: dict):
    """注入漏水事件：level=0 解除，level>0.5 触发漏水告警。"""
    if rt.mode != "sim":
        return JSONResponse({"error": "实际监测模式下不支持演示注入"}, status_code=409)
    room = payload.get("room", "roomA")
    level = float(payload.get("level", 0.8))
    if room not in ROOMS:
        return JSONResponse({"error": "未知机房"}, status_code=400)
    rt.sim.set_water_leak(room, level)
    await rt.broadcast({"type": "notice", "message": f"{ROOMS[room]['name']} 漏水注入 {level:.1f}"})
    return {"ok": True, "room": room, "level": level}


@app.post("/api/demo/door")
async def demo_door(payload: dict):
    """注入门禁事件：open=true 触发门禁打开告警。"""
    if rt.mode != "sim":
        return JSONResponse({"error": "实际监测模式下不支持演示注入"}, status_code=409)
    room = payload.get("room", "roomA")
    open_ = bool(payload.get("open", True))
    if room not in ROOMS:
        return JSONResponse({"error": "未知机房"}, status_code=400)
    rt.sim.set_door(room, open_)
    await rt.broadcast({"type": "notice", "message": f"{ROOMS[room]['name']} 门禁{'打开' if open_ else '关闭'}"})
    return {"ok": True, "room": room, "open": open_}


@app.post("/api/demo/ac-fault")
async def demo_ac_fault(payload: dict):
    """注入空调故障：制冷失效，室温会持续上升。"""
    if rt.mode != "sim":
        return JSONResponse({"error": "实际监测模式下不支持演示注入"}, status_code=409)
    room = payload.get("room", "roomA")
    fault = bool(payload.get("fault", True))
    if room not in ROOMS:
        return JSONResponse({"error": "未知机房"}, status_code=400)
    rt.sim.set_ac_fault(room, fault)
    await rt.broadcast({"type": "notice", "message": f"{ROOMS[room]['name']} 空调{'故障' if fault else '恢复'}"})
    return {"ok": True, "room": room, "fault": fault}


@app.post("/api/demo/outdoor-spike")
async def demo_outdoor_spike(payload: dict):
    """注入室外高温：持续抬升室温，观察空调调控响应。"""
    if rt.mode != "sim":
        return JSONResponse({"error": "实际监测模式下不支持演示注入"}, status_code=409)
    room = payload.get("room", "roomA")
    delta = float(payload.get("delta", 5.0))
    if room not in ROOMS:
        return JSONResponse({"error": "未知机房"}, status_code=400)
    rt.sim.set_outdoor_spike(room, delta)
    await rt.broadcast({"type": "notice", "message": f"{ROOMS[room]['name']} 室外温度 +{delta:.1f}°C"})
    return {"ok": True, "room": room, "delta": delta}


@app.post("/api/demo/humidity-spike")
async def demo_humidity_spike(payload: dict):
    """注入湿度骤升：观察湿度告警与空调除湿响应。"""
    if rt.mode != "sim":
        return JSONResponse({"error": "实际监测模式下不支持演示注入"}, status_code=409)
    room = payload.get("room", "roomA")
    delta = float(payload.get("delta", 20.0))
    if room not in ROOMS:
        return JSONResponse({"error": "未知机房"}, status_code=400)
    rt.sim.set_humidity_spike(room, delta)
    await rt.broadcast({"type": "notice", "message": f"{ROOMS[room]['name']} 湿度骤升 +{delta:.0f}%"})
    return {"ok": True, "room": room, "delta": delta}


@app.post("/api/demo/voltage-fluct")
async def demo_voltage_fluct(payload: dict):
    """注入电压波动：观察 UPS 与 PDU 电压告警。"""
    if rt.mode != "sim":
        return JSONResponse({"error": "实际监测模式下不支持演示注入"}, status_code=409)
    room = payload.get("room", "roomA")
    amplitude = float(payload.get("amplitude", 25.0))
    if room not in ROOMS:
        return JSONResponse({"error": "未知机房"}, status_code=400)
    rt.sim.set_voltage_fluct(room, amplitude)
    await rt.broadcast({"type": "notice", "message": f"{ROOMS[room]['name']} 电压波动 ±{amplitude:.0f}V"})
    return {"ok": True, "room": room, "amplitude": amplitude}


# ---------------- 对照实验 ----------------
@app.get("/api/experiment/status")
async def experiment_status():
    return rt.exp.status() if rt.exp else {"phase": "idle", "runs": []}


@app.post("/api/experiment/start")
async def experiment_start(payload: dict):
    if rt.mode != "sim":
        return JSONResponse({"error": "对照实验仅在仿真模式下可用"}, status_code=409)
    if not rt.exp:
        return JSONResponse({"error": "实验模块未初始化"}, status_code=503)
    if rt.exp.running:
        return JSONResponse({"error": "实验正在运行"}, status_code=409)
    duration = float(payload.get("duration", 180))
    if duration < 30 or duration > 1800:
        return JSONResponse({"error": "单组时长需在 30~1800 秒之间"}, status_code=400)
    rt.exp.start(duration)
    return {"ok": True, "status": rt.exp.status()}


@app.post("/api/experiment/cancel")
async def experiment_cancel():
    if rt.exp and rt.exp.running:
        rt.exp.task.cancel()
        return {"ok": True, "message": "已取消"}
    return JSONResponse({"error": "当前没有运行中的实验"}, status_code=400)


def _csv_response(rows: list[list], filename: str) -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(rows)
    # 添加 BOM，避免 Excel 打开中文乱码
    return Response(
        content=("\ufeff" + buf.getvalue()).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/experiment/export")
async def experiment_export(kind: str = "raw"):
    status = rt.exp.status() if rt.exp else {}
    runs = status.get("runs") or []
    if not runs:
        latest = db.recent_experiment_runs(1)
        if latest:
            exp_id = latest[0]["exp_id"]
            runs = [r for r in db.recent_experiment_runs(20) if r["exp_id"] == exp_id]

    if not runs:
        return JSONResponse({"error": "暂无可导出的实验数据"}, status_code=404)

    if kind == "summary":
        header = [
            "实验编号", "调控模式", "开始时间", "结束时间", "时长(s)",
            "平均制冷功率(kW)", "超温占比(%)", "平均温度(°C)", "最高温度(°C)",
            "制冷能耗(kWh)", "采样点",
        ]
        rows = [header]
        for r in sorted(runs, key=lambda x: x["started"]):
            rows.append([
                r.get("exp_id", status.get("id", "")),
                CONTROL_MODE_CN.get(r["mode"], r["mode"]),
                datetime.fromtimestamp(r["started"]).strftime("%Y-%m-%d %H:%M:%S"),
                datetime.fromtimestamp(r["finished"]).strftime("%Y-%m-%d %H:%M:%S"),
                round(r["finished"] - r["started"], 1),
                r.get("avg_kw", 0), r.get("over_ratio", 0),
                r.get("avg_temp", 0), r.get("max_temp", 0),
                r.get("kwh", 0), r.get("samples", 0),
            ])
        return _csv_response(rows, "experiment_summary.csv")

    start = min(r["started"] for r in runs)
    end = max(r["finished"] for r in runs)
    samples = db.experiment_samples(start, end)
    header = ["时间戳", "时间", "机房", "调控模式", "温度(°C)", "湿度(%)", "空调档位(%)", "排风", "功率(W)"]
    rows = [header]
    for s in samples:
        rows.append([
            round(s["ts"], 2),
            datetime.fromtimestamp(s["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
            s["room"], CONTROL_MODE_CN.get(s["mode"], s["mode"]),
            s["temp"], s["humidity"], s["ac_level"], s["fan"], round(s["power_w"] or 0, 1),
        ])
    return _csv_response(rows, "experiment_samples.csv")


# ---------------- 链路质量 ----------------
@app.get("/api/link")
async def link_status():
    if not rt.bridge:
        return JSONResponse({"error": "链路未就绪"}, status_code=503)
    return rt.bridge.link_stats()


@app.post("/api/link")
async def link_config(payload: dict):
    if rt.mode != "sim":
        return JSONResponse({"error": "实际监测模式使用真实链路，不支持链路注入"}, status_code=409)
    if not rt.bridge:
        return JSONResponse({"error": "链路未就绪"}, status_code=503)
    try:
        loss_rate = float(payload.get("loss_rate", 0))
        delay_ms = int(payload.get("delay_ms", 0))
        jitter_ms = int(payload.get("jitter_ms", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "链路参数必须为数字"}, status_code=400)
    # 先清零统计，再切换链路；恢复网络时触发的重传计数才能被看到
    if payload.get("reset"):
        rt.bridge.reset_stats()
    rt.bridge.set_link(loss_rate, delay_ms, jitter_ms)
    stats = rt.bridge.link_stats()
    await rt.broadcast({"type": "link", "stats": stats})
    return stats


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    rt.clients.add(ws)
    try:
        await ws.send_text(
            json.dumps(
                {
                    "type": "hello",
                    "mode": rt.ctl.mode,
                    "system": rt.mode,
                    "rooms": {
                        room: db.latest_room_state(room) or {} for room in ROOMS
                    },
                },
                ensure_ascii=False,
            )
        )
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        rt.clients.discard(ws)


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
