"""告警规则引擎：阈值判定、温升速率判定、自动恢复。"""

import time

import db
from config import ENV_FRESH_SECONDS, THRESHOLDS

# (rule_id, metric, level, threshold, hysteresis, message, direction)
# direction: "up" = value >= threshold triggers; "down" = value <= threshold triggers
THRESHOLD_RULES = [
    ("temp_warn", "temperature", "warning", THRESHOLDS["temp_warn"], 0.5, "温度超过预警阈值 28°C", "up"),
    ("temp_critical", "temperature", "critical", THRESHOLDS["temp_critical"], 0.8, "温度超过严重阈值 32°C", "up"),
    ("humidity_warn", "humidity", "warning", THRESHOLDS["humidity_warn"], 2.0, "相对湿度偏高，存在结露风险", "up"),
    ("power_warn", "power", "warning", THRESHOLDS["power_warn"], 80.0, "单路机柜功率超限，负载偏高", "up"),
    ("smoke_critical", "smoke", "critical", THRESHOLDS["smoke_warn"], 0.03, "检测到烟雾浓度异常", "up"),
    ("water_leak_critical", "water_leak", "critical", THRESHOLDS["water_leak_warn"], 0.1, "检测到漏水，存在设备短路风险", "up"),
    ("ups_battery_warn", "battery", "warning", THRESHOLDS["ups_battery_warn"], 5.0, "UPS 电池电量偏低", "down"),
    ("voltage_low_warn", "voltage", "warning", THRESHOLDS["voltage_low"], 2.0, "市电电压偏低，可能影响设备运行", "down"),
    ("voltage_high_warn", "voltage", "warning", THRESHOLDS["voltage_high"], 2.0, "市电电压偏高，存在设备损坏风险", "up"),
    ("ups_voltage_low_warn", "output_voltage", "warning", THRESHOLDS["voltage_low"], 2.0, "UPS 输出电压偏低，负载供电可能异常", "down"),
    ("ups_voltage_high_warn", "output_voltage", "warning", THRESHOLDS["voltage_high"], 2.0, "UPS 输出电压偏高，存在设备损坏风险", "up"),
    ("door_open_warning", "door_state", "warning", THRESHOLDS["door_open_alert"], 0.5, "机房门禁被打开，请注意人员出入", "up"),
]

METRIC_CN = {
    "temperature": "温度",
    "humidity": "湿度",
    "power": "功率",
    "smoke": "烟雾浓度",
    "water_leak": "漏水检测",
    "battery": "UPS电池",
    "voltage": "电压",
    "output_voltage": "UPS输出电压",
    "ups_load": "UPS负载",
    "door_state": "门禁状态",
    "airflow": "风速",
}

# 环境类指标按机房聚合判定：同一机房多路传感器只保留一条告警
ROOM_SCOPE_METRICS = {"temperature", "humidity"}
# 参与机房聚合判定的最新值有效期；超时未上报的传感器不参与，避免离线后告警无法恢复
FRESH_SECONDS = ENV_FRESH_SECONDS
# 最新值缓存清理阈值
STALE_SECONDS = max(300.0, FRESH_SECONDS * 2)


class RuleEngine:
    def __init__(self) -> None:
        self._last_temp: dict[str, tuple[float, float]] = {}
        self._rate_window: dict[str, tuple[float, float]] = {}
        self._latest: dict[tuple[str, str, str], tuple[float, float]] = {}
        self._rates: dict[tuple[str, str], tuple[float, float]] = {}

    def reset(self) -> None:
        """清空速率计算与最新值缓存，避免跨实验组误判。"""
        self._last_temp.clear()
        self._rate_window.clear()
        self._latest.clear()
        self._rates.clear()

    def evaluate(self, ts: float, room: str, device: str, metric: str, value: float) -> list[dict]:
        events: list[dict] = []
        self._latest[(room, device, metric)] = (ts, value)
        # 定期丢弃长期未更新的测点，避免缓存无限增长
        for key, (t, _v) in list(self._latest.items()):
            if ts - t > STALE_SECONDS:
                del self._latest[key]

        for rule_id, rule_metric, level, threshold, hyst, msg, direction in THRESHOLD_RULES:
            if rule_metric != metric:
                continue
            if metric in ROOM_SCOPE_METRICS:
                events.extend(
                    self._eval_room_rule(
                        ts, room, device, metric, rule_id, level, threshold, hyst, msg, direction
                    )
                )
                continue
            triggered = value >= threshold if direction == "up" else value <= threshold
            recovered = value < threshold - hyst if direction == "up" else value > threshold + hyst
            if triggered:
                if not db.has_open_alarm(device, rule_id):
                    alarm_id = db.add_alarm(ts, room, device, rule_id, level, msg)
                    events.append(
                        {
                            "type": "alarm",
                            "id": alarm_id,
                            "ts": ts,
                            "room": room,
                            "device": device,
                            "rule": rule_id,
                            "level": level,
                            "message": msg,
                            "value": value,
                        }
                    )
            elif recovered:
                if db.has_open_alarm(device, rule_id):
                    db.resolve_alarm(device, rule_id, ts)
                    events.append(
                        {
                            "type": "resolve",
                            "ts": ts,
                            "room": room,
                            "device": device,
                            "rule": rule_id,
                        }
                    )

        if metric == "temperature":
            events.extend(self._check_rate(ts, room, device, value))

        return events

    def _eval_room_rule(
        self,
        ts: float,
        room: str,
        device: str,
        metric: str,
        rule_id: str,
        level: str,
        threshold: float,
        hyst: float,
        msg: str,
        direction: str,
    ) -> list[dict]:
        """机房环境类告警：以机房内所有新鲜测点聚合判定，只保留一条活动告警。"""
        fresh = [
            (dev, val)
            for (r, dev, m), (t, val) in self._latest.items()
            if r == room and m == metric and ts - t <= FRESH_SECONDS
        ]
        if not fresh:
            return []
        if direction == "up":
            hit = [(dev, val) for dev, val in fresh if val >= threshold]
            safe = all(val < threshold - hyst for _dev, val in fresh)
            pick = max(hit, key=lambda item: item[1]) if hit else None
        else:
            hit = [(dev, val) for dev, val in fresh if val <= threshold]
            safe = all(val > threshold + hyst for _dev, val in fresh)
            pick = min(hit, key=lambda item: item[1]) if hit else None

        if pick:
            if db.has_open_room_alarm(room, rule_id):
                return []
            dev, val = pick
            alarm_id = db.add_alarm(ts, room, dev, rule_id, level, msg)
            return [
                {
                    "type": "alarm",
                    "id": alarm_id,
                    "ts": ts,
                    "room": room,
                    "device": dev,
                    "rule": rule_id,
                    "level": level,
                    "message": msg,
                    "value": val,
                }
            ]
        if safe and db.has_open_room_alarm(room, rule_id):
            db.resolve_room_alarm(room, rule_id, ts)
            return [
                {
                    "type": "resolve",
                    "ts": ts,
                    "room": room,
                    "device": device,
                    "rule": rule_id,
                }
            ]
        return []

    def _check_rate(self, ts: float, room: str, device: str, value: float) -> list[dict]:
        """温升速率：按 60s 窗口计算 °C/min，并按机房保留一条告警。

        真实设备采样可能 30~60s，因此允许更大的采样间隔，只丢弃超过 2 分钟的断档。
        """
        prev = self._last_temp.get(device)
        self._last_temp[device] = (ts, value)
        if not prev:
            return []
        prev_ts, prev_val = prev
        span = ts - prev_ts
        if span <= 0 or span > 120.0:
            return []

        # 使用累计 60 秒窗口的斜率，避免单步噪声
        window_start = self._rate_window.get(device)
        if window_start is None or ts - window_start[0] > 65:
            window_start = (ts, value)
            self._rate_window[device] = window_start
        elapsed_min = (ts - window_start[0]) / 60.0
        if elapsed_min < 0.5:
            return []

        rate = (value - window_start[1]) / elapsed_min
        self._rates[(room, device)] = (ts, rate)
        for key, (t, _r) in list(self._rates.items()):
            if ts - t > FRESH_SECONDS:
                del self._rates[key]

        fresh_rates = [
            r for (rm, _dev), (t, r) in self._rates.items() if rm == room and ts - t <= FRESH_SECONDS
        ]
        if not fresh_rates:
            return []
        max_rate = max(fresh_rates)
        rule_id = "temp_rate"
        if max_rate >= THRESHOLDS["rate_warn"]:
            if not db.has_open_room_alarm(room, rule_id):
                msg = f"温升速率 {max_rate:.2f}°C/min，超过 {THRESHOLDS['rate_warn']}°C/min"
                alarm_id = db.add_alarm(ts, room, device, rule_id, "warning", msg)
                return [
                    {
                        "type": "alarm",
                        "id": alarm_id,
                        "ts": ts,
                        "room": room,
                        "device": device,
                        "rule": rule_id,
                        "level": "warning",
                        "message": msg,
                        "value": round(max_rate, 2),
                    }
                ]
        elif max_rate < THRESHOLDS["rate_warn"] * 0.6 and db.has_open_room_alarm(room, rule_id):
            db.resolve_room_alarm(room, rule_id, ts)
            return [{"type": "resolve", "ts": ts, "room": room, "device": device, "rule": rule_id}]
        return []
