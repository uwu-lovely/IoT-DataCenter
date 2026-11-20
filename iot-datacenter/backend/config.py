"""全局配置：机房拓扑、设备清单、阈值与 MQTT 参数。"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "iot.db"
FRONTEND_DIR = BASE_DIR.parent / "frontend"

MQTT_HOST = "127.0.0.1"
MQTT_PORT = int(os.environ.get("IOT_MQTT_PORT", "1883"))
# Broker 监听地址：实际监测接入局域网设备时设为 0.0.0.0
MQTT_BIND_HOST = os.environ.get("IOT_MQTT_BIND", MQTT_HOST)

# Broker 安全：默认匿名（仅课程演示/本机调试）。接入外网前务必配置用户名密码与 TLS。
MQTT_USERNAME = os.environ.get("IOT_MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("IOT_MQTT_PASSWORD", "")
MQTT_ALLOW_ANONYMOUS = not (MQTT_USERNAME and MQTT_PASSWORD)
# TLS 证书路径（可选）。设置后 Broker 启用 TLS 监听，平台桥接客户端也会启用 TLS。
MQTT_TLS_CERT = os.environ.get("IOT_MQTT_TLS_CERT", "")
MQTT_TLS_KEY = os.environ.get("IOT_MQTT_TLS_KEY", "")
MQTT_TLS_CA = os.environ.get("IOT_MQTT_TLS_CA", "")
MQTT_USE_TLS = bool(MQTT_TLS_CERT and MQTT_TLS_KEY)

# 运行模式：sim=仿真（内置虚拟设备），real=实际监测（只接收外部设备上报）
MODE = os.environ.get("IOT_MODE", "sim")
MODES = {"sim": "仿真模式", "real": "实际监测模式"}
# 实际监测模式的数据保留时长（分钟），默认 30 天
REAL_RETENTION_MINUTES = int(os.environ.get("IOT_RETENTION_MINUTES", str(60 * 24 * 30)))
# 实际监测模式由遥测聚合机房快照的周期（秒）
REAL_TICK_SECONDS = float(os.environ.get("IOT_REAL_TICK", "2"))

# 采样周期（秒）与模拟时间步长保持一致
TICK_SECONDS = 1.0

# 机房配置：每个机房内置热模型参数
ROOMS = {
    "roomA": {
        "name": "机房A · 核心机柜区",
        "outdoor": 31.0,
        "init_temp": 24.2,
        "volume": 120.0,
        "heat_capacity": 3.0,  # kW·min/°C，越大升温越慢
        "leak": 0.004,
    },
    "roomB": {
        "name": "机房B · 配电存储区",
        "outdoor": 29.5,
        "init_temp": 25.1,
        "volume": 90.0,
        "heat_capacity": 2.8,
        "leak": 0.004,
    },
}

# 设备清单：type 决定上报指标
DEVICES = [
    # room, device_id, type, name
    ("roomA", "TH-01", "env", "温湿度传感器-01"),
    ("roomA", "TH-02", "env", "温湿度传感器-02"),
    ("roomA", "PDU-01", "power", "智能PDU-01"),
    ("roomA", "PDU-02", "power", "智能PDU-02"),
    ("roomA", "SMK-01", "smoke", "烟雾传感器-01"),
    ("roomA", "WLK-01", "water", "漏水检测绳-01"),
    ("roomA", "DR-01", "door", "门禁传感器-01"),
    ("roomA", "UPS-01", "ups", "不间断电源-01"),
    ("roomA", "AF-01", "airflow", "风速传感器-01"),
    ("roomA", "AC-01", "actuator", "精密空调-01"),
    ("roomA", "FAN-01", "actuator", "排风机组-01"),
    ("roomB", "TH-03", "env", "温湿度传感器-03"),
    ("roomB", "PDU-03", "power", "智能PDU-03"),
    ("roomB", "SMK-02", "smoke", "烟雾传感器-02"),
    ("roomB", "WLK-02", "water", "漏水检测绳-02"),
    ("roomB", "DR-02", "door", "门禁传感器-02"),
    ("roomB", "UPS-02", "ups", "不间断电源-02"),
    ("roomB", "AF-02", "airflow", "风速传感器-02"),
    ("roomB", "AC-02", "actuator", "精密空调-02"),
    ("roomB", "FAN-02", "actuator", "排风机组-02"),
]

ROOM_DEVICES = {}
for _room, _dev, _type, _name in DEVICES:
    ROOM_DEVICES.setdefault(_room, []).append(_dev)

# 机房编号后缀：roomA -> 01, roomB -> 02，用于拼装执行器设备号
ROOM_SUFFIX = {room: f"{i + 1:02d}" for i, room in enumerate(ROOMS)}

# 告警阈值
THRESHOLDS = {
    "temp_warn": 28.0,
    "temp_critical": 32.0,
    "humidity_warn": 70.0,
    "smoke_warn": 0.20,
    "power_warn": 2600.0,   # 单路 PDU 功率
    "rate_warn": 0.6,       # °C/min 温升速率
    "water_leak_warn": 0.5,   # 漏水检测阈值
    "ups_battery_warn": 30.0,  # UPS 电池电量低于 30% 告警
    "voltage_low": 198.0,      # 电压下限
    "voltage_high": 242.0,     # 电压上限
    "door_open_alert": 1,       # 门禁打开告警
    # 心跳超时判定：实际监测模式下可按设备上报周期用 IOT_OFFLINE_SECONDS 调整
    "offline_seconds": float(os.environ.get("IOT_OFFLINE_SECONDS", "20")),
    "alarm_cooldown": 60.0,
}

# 环境类告警的新鲜窗口：参与机房级聚合判定的测点必须在该窗口内上报过。
# 真实设备采样周期常用 30~60s，因此默认 90s；采样更慢时可通过环境变量调整。
ENV_FRESH_SECONDS = float(os.environ.get("IOT_ENV_FRESH_SECONDS", "90"))

# 实际监测模式：设备类型与应上报的指标
METRICS_BY_TYPE = {
    "env": ["temperature", "humidity"],
    "power": ["power", "current", "voltage"],
    "smoke": ["smoke"],
    "water": ["water_leak"],
    "door": ["door_state"],
    "ups": ["battery", "ups_load", "output_voltage"],
    "airflow": ["airflow"],
    "actuator": ["ac_level", "fan_state", "ac_return_temp"],
}
DEVICE_TYPE_CN = {
    "env": "环境温湿度",
    "power": "电力/PDU",
    "smoke": "烟雾",
    "water": "漏水检测",
    "door": "门禁",
    "ups": "UPS电源",
    "airflow": "风速",
    "actuator": "空调/排风",
}
# 设备编号前缀 -> 类型（实际监测模式自动识别）
DEVICE_TYPE_PREFIX = {
    "TH": "env",
    "PDU": "power",
    "SMK": "smoke",
    "WLK": "water",
    "DR": "door",
    "UPS": "ups",
    "AF": "airflow",
    "AC": "actuator",
    "FAN": "actuator",
}

# 调控目标
CONTROL = {
    "target_temp": 26.0,       # 预测式调控目标温度
    "adaptive_target": 27.0,   # 按需最小制冷的软约束目标温度
    "predict_horizon_min": 3.0,
    "ac_max_kw": 6.0,          # 空调 100% 档位制冷功率
    "fan_kw": 0.6,             # 排风机组等效制冷功率
    "mode": "predictive",      # threshold | adaptive | predictive
}

# 三种调控策略（对照实验按此顺序运行）
CONTROL_MODES = ("threshold", "adaptive", "predictive")
CONTROL_MODE_CN = {
    "threshold": "阈值控制",
    "adaptive": "按需最小制冷",
    "predictive": "预测式调控",
}

# 静态资源版本号，改前端时用于避免浏览器缓存
VERSION = "1.3.0"
