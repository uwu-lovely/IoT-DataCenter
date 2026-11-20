"""虚拟设备仿真器：把热模型产生的数据整理成各设备的 MQTT 上报报文。"""

import random

from config import CONTROL, ROOMS, ROOM_SUFFIX, THRESHOLDS
from thermal import PduModel, RoomThermal

PDU_BASE = {
    "roomA": [("PDU-01", 1400.0, 0.0), ("PDU-02", 1150.0, 2.0)],
    "roomB": [("PDU-03", 1850.0, 1.2)],
}

ENV_SENSORS = {
    "roomA": [("TH-01", 0.0), ("TH-02", 0.18)],
    "roomB": [("TH-03", 0.0)],
}

SMOKE_SENSORS = {"roomA": "SMK-01", "roomB": "SMK-02"}
WATER_SENSORS = {"roomA": "WLK-01", "roomB": "WLK-02"}
DOOR_SENSORS = {"roomA": "DR-01", "roomB": "DR-02"}
UPS_SENSORS = {"roomA": "UPS-01", "roomB": "UPS-02"}
AIRFLOW_SENSORS = {"roomA": "AF-01", "roomB": "AF-02"}


class Simulator:
    def __init__(self) -> None:
        self.t = 0.0
        self.rooms = {room: RoomThermal(room, cfg) for room, cfg in ROOMS.items()}
        self.pdus = {
            room: [PduModel(dev, base, phase) for dev, base, phase in items]
            for room, items in PDU_BASE.items()
        }
        self.actuators = {room: {"ac": 0, "fan": 0} for room in ROOMS}
        self.paused: set[str] = set()
        self.smoke_event = {room: 0.0 for room in ROOMS}
        # 新增注入场景状态
        self.water_leak = {room: 0.0 for room in ROOMS}       # 漏水浓度 0~1
        self.door_open = {room: 0 for room in ROOMS}           # 门禁状态 0/1
        self.ac_fault = {room: False for room in ROOMS}        # 空调故障
        self.outdoor_spike = {room: 0.0 for room in ROOMS}     # 室外温度叠加
        self.humidity_spike = {room: 0.0 for room in ROOMS}    # 湿度骤升叠加
        self.voltage_fluct = {room: 0.0 for room in ROOMS}     # 电压波动幅度

    # ---- 外部操作（演示注入） ----
    def inject_load(self, room: str, delta_w: float) -> None:
        for pdu in self.pdus[room]:
            pdu.inject(delta_w / len(self.pdus[room]))

    def set_smoke(self, room: str, level: float) -> None:
        self.smoke_event[room] = level

    def set_water_leak(self, room: str, level: float) -> None:
        self.water_leak[room] = level

    def set_door(self, room: str, open_: bool) -> None:
        self.door_open[room] = 1 if open_ else 0

    def set_ac_fault(self, room: str, fault: bool) -> None:
        self.ac_fault[room] = fault

    def set_outdoor_spike(self, room: str, delta: float) -> None:
        self.outdoor_spike[room] = delta

    def set_humidity_spike(self, room: str, delta: float) -> None:
        self.humidity_spike[room] = delta

    def set_voltage_fluct(self, room: str, amplitude: float) -> None:
        self.voltage_fluct[room] = amplitude

    def set_paused(self, device: str, paused: bool) -> None:
        if paused:
            self.paused.add(device)
        else:
            self.paused.discard(device)

    def reset_environment(self) -> None:
        """对照实验前把环境恢复到同一初始状态，保证两组条件一致。"""
        for room, thermal in self.rooms.items():
            thermal.temp = thermal.cfg["init_temp"]
            thermal.humidity = 52.0
        for pdus in self.pdus.values():
            for pdu in pdus:
                pdu.injected = 0.0
        self.actuators = {room: {"ac": 0, "fan": 0} for room in ROOMS}
        self.smoke_event = {room: 0.0 for room in ROOMS}
        self.water_leak = {room: 0.0 for room in ROOMS}
        self.door_open = {room: 0 for room in ROOMS}
        self.ac_fault = {room: False for room in ROOMS}
        self.outdoor_spike = {room: 0.0 for room in ROOMS}
        self.humidity_spike = {room: 0.0 for room in ROOMS}
        self.voltage_fluct = {room: 0.0 for room in ROOMS}

    def apply_command(self, room: str, target: str, payload: dict) -> None:
        if room not in self.actuators:
            return
        if target.startswith("AC"):
            level = int(payload.get("level", self.actuators[room]["ac"]))
            self.actuators[room]["ac"] = max(0, min(100, level))
        elif target.startswith("FAN"):
            self.actuators[room]["fan"] = 1 if payload.get("on") else 0

    # ---- 推进一个仿真步 ----
    def step(self, dt: float) -> tuple[list[dict], dict]:
        self.t += dt
        dt_min = dt / 60.0
        readings: list[dict] = []
        snapshots: dict[str, dict] = {}

        for room, thermal in self.rooms.items():
            powers = [pdu.step(self.t) for pdu in self.pdus[room]]
            heat_w = sum(powers)
            act = self.actuators[room]
            # 空调故障：制冷功率归零
            ac_effective = 0 if self.ac_fault[room] else act["ac"]
            thermal.step(
                dt_min,
                heat_w,
                ac_effective,
                act["fan"],
                ac_max_kw=CONTROL["ac_max_kw"],
                fan_kw=CONTROL["fan_kw"],
            )
            # 室外高温叠加：直接抬升室温
            thermal.temp += self.outdoor_spike[room] * dt_min / 3.0
            # 湿度骤升叠加
            thermal.humidity += self.humidity_spike[room] * dt_min / 2.0
            snapshots[room] = {
                "temp": round(thermal.temp, 2),
                "humidity": round(thermal.humidity, 1),
                "ac_level": act["ac"],
                "ac_fault": int(self.ac_fault[room]),
                "fan": act["fan"],
                "power_w": round(heat_w, 0),
            }

            for device, offset in ENV_SENSORS[room]:
                if device in self.paused:
                    continue
                readings.append(
                    {
                        "room": room,
                        "device": device,
                        "metrics": {
                            "temperature": thermal.sensor_temp(offset),
                            "humidity": round(thermal.humidity + random.gauss(0, 0.4), 1),
                        },
                    }
                )

            for device, _base, _phase in PDU_BASE[room]:
                if device in self.paused:
                    continue
                idx = [d for d, _b, _p in PDU_BASE[room]].index(device)
                value = powers[idx]
                base_v = 220.0 + self.voltage_fluct[room] * random.gauss(0, 1)
                readings.append(
                    {
                        "room": room,
                        "device": device,
                        "metrics": {
                            "power": round(value, 0),
                            "current": round(value / 220.0, 2),
                            "voltage": round(base_v, 1),
                        },
                    }
                )

            smoke_dev = SMOKE_SENSORS[room]
            if smoke_dev not in self.paused:
                target = self.smoke_event[room]
                readings.append(
                    {
                        "room": room,
                        "device": smoke_dev,
                        "metrics": {
                            "smoke": round(max(0.0, target + random.gauss(0, 0.008)), 3)
                        },
                    }
                )

            # 漏水检测绳：0 表示干燥，>0.5 表示检测到漏水
            wlk_dev = WATER_SENSORS[room]
            if wlk_dev not in self.paused:
                readings.append(
                    {
                        "room": room,
                        "device": wlk_dev,
                        "metrics": {
                            "water_leak": round(max(0.0, self.water_leak[room] + random.gauss(0, 0.02)), 3)
                        },
                    }
                )

            # 门禁传感器：0 关闭，1 打开
            dr_dev = DOOR_SENSORS[room]
            if dr_dev not in self.paused:
                readings.append(
                    {
                        "room": room,
                        "device": dr_dev,
                        "metrics": {"door_state": self.door_open[room]},
                    }
                )

            # UPS：电池电量 0~100%，负载率 0~100%，输出电压
            ups_dev = UPS_SENSORS[room]
            if ups_dev not in self.paused:
                # 市电正常时电池 100%，模拟偶尔充放电波动
                battery = 100.0 - abs(random.gauss(0, 0.3))
                ups_load = min(100.0, heat_w / 3000.0 * 100 + random.gauss(0, 2))
                out_v = 220.0 + self.voltage_fluct[room] * random.gauss(0, 0.5)
                readings.append(
                    {
                        "room": room,
                        "device": ups_dev,
                        "metrics": {
                            "battery": round(battery, 1),
                            "ups_load": round(ups_load, 1),
                            "output_voltage": round(out_v, 1),
                        },
                    }
                )

            # 风速传感器：m/s，空调开启时风速增大
            af_dev = AIRFLOW_SENSORS[room]
            if af_dev not in self.paused:
                base_air = 0.3 + ac_effective / 100.0 * 2.5
                readings.append(
                    {
                        "room": room,
                        "device": af_dev,
                        "metrics": {
                            "airflow": round(max(0.0, base_air + random.gauss(0, 0.15)), 2)
                        },
                    }
                )

            if f"AC-{ROOM_SUFFIX[room]}" not in self.paused:
                # 空调回风温度：略高于室温（空调吸风口）
                return_temp = thermal.temp + 0.8 + random.gauss(0, 0.1)
                readings.append(
                    {
                        "room": room,
                        "device": f"AC-{ROOM_SUFFIX[room]}",
                        "metrics": {
                            "ac_level": act["ac"],
                            "ac_return_temp": round(return_temp, 2),
                        },
                    }
                )
            if f"FAN-{ROOM_SUFFIX[room]}" not in self.paused:
                readings.append(
                    {
                        "room": room,
                        "device": f"FAN-{ROOM_SUFFIX[room]}",
                        "metrics": {"fan_state": act["fan"]},
                    }
                )

        return readings, snapshots
