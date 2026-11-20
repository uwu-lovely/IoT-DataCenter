"""机房热力学简化模型：机柜功率 -> 发热 -> 室温演化，用于生成可信的仿真数据。"""

import math
import random


class RoomThermal:
    """一阶集总热模型。

    dT/dt = (Q_heat - Q_cool) / C + leak * (T_out - T)
    其中 Q 单位为 kW，C 为 kW·min/°C，因此积分步长使用分钟。
    """

    def __init__(self, room: str, cfg: dict):
        self.room = room
        self.cfg = cfg
        self.temp = cfg["init_temp"]
        self.humidity = 52.0
        self.outdoor = cfg["outdoor"]
        self.capacity = cfg["heat_capacity"]
        self.leak = cfg["leak"]

    def step(
        self,
        dt_min: float,
        heat_w: float,
        ac_level: float,
        fan_on: int,
        ac_max_kw: float = 6.0,
        fan_kw: float = 0.6,
    ) -> None:
        heat_kw = heat_w / 1000.0 * 0.92
        cool_kw = ac_level / 100.0 * ac_max_kw + fan_on * fan_kw
        d_temp = (heat_kw - cool_kw) / self.capacity + self.leak * (self.outdoor - self.temp)
        self.temp += d_temp * dt_min + random.gauss(0, 0.008)
        self.temp = min(48.0, max(16.0, self.temp))

        # 相对湿度：空调除湿 + 温度耦合，简单一阶跟随
        target_hum = 58.0 - 0.22 * ac_level + 0.9 * (self.temp - 25.0)
        self.humidity += (target_hum - self.humidity) * min(1.0, dt_min * 2.0)
        self.humidity += random.gauss(0, 0.15)
        self.humidity = min(88.0, max(28.0, self.humidity))

    def sensor_temp(self, offset: float = 0.0) -> float:
        return round(self.temp + offset + random.gauss(0, 0.05), 2)


class PduModel:
    """单路机柜 PDU 功率模型：基础负载 + 周期波动 + 注入负载（指数衰减）。"""

    def __init__(self, device: str, base_w: float, phase: float):
        self.device = device
        self.base_w = base_w
        self.phase = phase
        self.injected = 0.0

    def inject(self, delta_w: float) -> None:
        self.injected += delta_w

    def step(self, t: float) -> float:
        wave = 120 * math.sin(t / 37.0 + self.phase) + 60 * math.sin(t / 11.0 + self.phase)
        noise = random.gauss(0, 18)
        self.injected *= 0.997
        if abs(self.injected) < 1:
            self.injected = 0.0
        return max(200.0, self.base_w + wave + self.injected + noise)
