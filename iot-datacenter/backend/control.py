"""调控策略：阈值控制 / 按需最小制冷 / 预测式主动调控。

控制器只使用"上报过来的温度与 PDU 功率"，不读仿真真值，保证数据链路语义正确。
"""

from collections import deque

from config import CONTROL, ROOM_SUFFIX, THRESHOLDS


class Controller:
    def __init__(self, mode: str = CONTROL["mode"]) -> None:
        self.mode = mode
        self._history: dict[str, deque] = {}
        self._level: dict[str, int] = {}
        self._fan: dict[str, int] = {}
        self._cooling: dict[str, bool] = {}

    # ---- 观测 ----
    def observe(self, room: str, ts: float, temp: float) -> None:
        buf = self._history.setdefault(room, deque(maxlen=180))
        buf.append((ts, temp))
        while buf and ts - buf[0][0] > 90:
            buf.popleft()

    def slope(self, room: str, window: float = 60.0) -> float:
        """返回 °C/min 的温升斜率（最小二乘）。"""
        buf = self._history.get(room)
        if not buf or len(buf) < 8:
            return 0.0
        pts = [(t, v) for t, v in buf if buf[-1][0] - t <= window]
        if len(pts) < 8:
            return 0.0
        n = len(pts)
        mx = sum(p[0] for p in pts) / n
        my = sum(p[1] for p in pts) / n
        denom = sum((p[0] - mx) ** 2 for p in pts)
        if denom <= 1e-9:
            return 0.0
        slope_per_sec = sum((p[0] - mx) * (p[1] - my) for p in pts) / denom
        return slope_per_sec * 60.0

    def predict(self, room: str, temp: float) -> tuple[float, float]:
        s = self.slope(room)
        return temp + s * CONTROL["predict_horizon_min"], s

    # ---- 决策 ----
    def decide(self, room: str, temp: float, power_w: float = 0.0) -> dict | None:
        """返回需要下发的命令；无需调整时返回 None。"""
        if self.mode == "threshold":
            return self._decide_threshold(room, temp)
        if self.mode == "adaptive":
            return self._decide_adaptive(room, temp, power_w)
        return self._decide_predictive(room, temp)

    def _decide_adaptive(self, room: str, temp: float, power_w: float) -> dict | None:
        """按需最小制冷：功率前馈保证稳态供需匹配，软约束体温避免过度预冷。

        前馈档位 = 当前热负荷完全被空调抵消时所需的档位，使稳态功率最小；
        反馈项只在温度偏离软约束目标时微调，不追求把温度压到更低。
        """
        level = self._level.get(room, 0)
        fan = self._fan.get(room, 0)
        target_temp = CONTROL["adaptive_target"]
        slope = self.slope(room)
        # 短时预测（1 分钟），只用于越界保护，避免像预测式那样大幅预冷
        predicted = temp + slope * 1.0

        heat_kw = power_w / 1000.0 * 0.92
        feedforward = heat_kw / CONTROL["ac_max_kw"] * 100.0
        target = feedforward + (temp - target_temp) * 40

        # 明显低于软约束目标时不制冷，避免把温度压到目标以下
        if temp <= target_temp - 1.0:
            target = 0
        # 短时预测即将越界时才提前投入制冷
        if predicted >= THRESHOLDS["temp_warn"]:
            target = max(target, 80 + (predicted - THRESHOLDS["temp_warn"]) * 15)
        target = int(max(0, min(100, round(target))))
        target_fan = 1 if target >= 60 else 0

        if abs(target - level) < 6 and target_fan == fan:
            return None
        return self._command(
            room,
            target,
            target_fan,
            reason=f"前馈 {feedforward:.0f}%，温度偏差 {temp - target_temp:+.1f}°C，按需制冷 {target}%",
        )

    def _decide_threshold(self, room: str, temp: float) -> dict | None:
        cooling = self._cooling.get(room, False)
        level = self._level.get(room, 0)
        fan = self._fan.get(room, 0)

        if not cooling and temp >= THRESHOLDS["temp_warn"]:
            return self._command(room, 88, 1, cooling=True, reason="达到 28°C 阈值，空调紧急制冷")
        if cooling and temp <= CONTROL["target_temp"]:
            return self._command(room, 18, 0, cooling=False, reason="温度降至 26°C，回落到低功耗档")
        return None

    def _decide_predictive(self, room: str, temp: float) -> dict | None:
        predicted, slope = self.predict(room, temp)
        level = self._level.get(room, 0)
        fan = self._fan.get(room, 0)

        # 前馈 + 反馈：按温度偏差与温升趋势计算维持档位，避免长时间满功率
        target = 42 + (temp - CONTROL["target_temp"]) * 26 + slope * 22
        if predicted >= THRESHOLDS["temp_warn"]:
            target = max(target, 70 + (predicted - THRESHOLDS["temp_warn"]) * 15)
        elif predicted >= THRESHOLDS["temp_warn"] - 0.8:
            target = max(target, 55)

        target = int(max(10, min(100, round(target))))
        target_fan = 1 if target >= 70 else 0

        if abs(target - level) < 6 and target_fan == fan:
            return None
        return self._command(
            room,
            target,
            target_fan,
            reason=f"预测 {CONTROL['predict_horizon_min']:.0f} 分钟后 {predicted:.1f}°C"
            f"（{slope:+.2f}°C/min），档位调整为 {target}%",
        )

    def _command(self, room: str, level: int, fan: int, reason: str, cooling: bool | None = None) -> dict:
        self._level[room] = level
        self._fan[room] = fan
        if cooling is not None:
            self._cooling[room] = cooling
        return {
            "room": room,
            "target": f"AC-{ROOM_SUFFIX[room]}",
            "level": level,
            "fan_target": f"FAN-{ROOM_SUFFIX[room]}",
            "fan": fan,
            "reason": reason,
        }

    def current(self, room: str) -> dict:
        return {"level": self._level.get(room, 0), "fan": self._fan.get(room, 0)}

    def reset(self) -> None:
        """清空控制器状态，供对照实验的两组起点对齐。"""
        self._history.clear()
        self._level.clear()
        self._fan.clear()
        self._cooling.clear()
