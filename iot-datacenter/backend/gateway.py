"""下行控制网关适配层。

平台统一使用标准 MQTT 命令主题 `iot/<room>/<device>/cmd`，
真实设备（红外空调、Modbus PDU 等）需要通过网关把标准命令翻译为设备私有协议。

本模块提供三种网关适配器：
- MqttGateway：直接向标准主题发布（仿真模式 / 已支持 MQTT 的设备）
- IrGateway：红外网关，把空调档位翻译为红外码序列（通过 HTTP 调用红外转发器）
- ModbusGateway：Modbus 网关，把档位/开关翻译为寄存器写入（RTU 或 TCP）

设备的 gateway 字段决定使用哪种适配器，默认 mqtt。
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod

import db
from config import ROOM_SUFFIX

GATEWAY_TYPES = ("mqtt", "ir", "modbus")
GATEWAY_CN = {
    "mqtt": "直连 MQTT",
    "ir": "红外网关",
    "modbus": "Modbus 网关",
}

# 红外码表：空调档位 -> 红外码（示例，真实场景需按空调型号学习）
IR_CODES = {
    "off": "A900",
    "cool_26": "A901",
    "cool_24": "A902",
    "cool_22": "A903",
    "fan_on": "B100",
    "fan_off": "B101",
}

# Modbus 寄存器映射
MODBUS_REGISTERS = {
    "ac_level": 0x0001,  # 空调档位寄存器（0~100）
    "fan_state": 0x0002,  # 排风状态寄存器（0/1）
}


class GatewayAdapter(ABC):
    """所有下行网关的基类：把标准命令翻译为设备私有协议并下发。"""

    def __init__(self, bridge=None) -> None:
        self.bridge = bridge

    @abstractmethod
    def send_ac(self, room: str, device: str, level: int, source: str) -> dict:
        """下发空调档位命令，返回下发结果（含原始协议报文）。"""

    @abstractmethod
    def send_fan(self, room: str, device: str, on: bool, source: str) -> dict:
        """下发排风开关命令，返回下发结果（含原始协议报文）。"""

    def log_action(self, room: str, target: str, action: str, reason: str, source: str) -> None:
        ts = time.time()
        db.insert_action(ts, room, source, target, action, reason, source)


class MqttGateway(GatewayAdapter):
    """直连 MQTT：向标准主题发布 JSON 命令（仿真模式与 MQTT 设备的默认行为）。"""

    def send_ac(self, room: str, device: str, level: int, source: str) -> dict:
        payload = {"level": level, "source": source, "ts": time.time()}
        topic = f"iot/{room}/{device}/cmd"
        if self.bridge:
            self.bridge.publish(topic, payload)
        return {
            "gateway": "mqtt",
            "topic": topic,
            "payload": payload,
            "ok": True,
        }

    def send_fan(self, room: str, device: str, on: bool, source: str) -> dict:
        payload = {"on": int(on), "source": source, "ts": time.time()}
        topic = f"iot/{room}/{device}/cmd"
        if self.bridge:
            self.bridge.publish(topic, payload)
        return {
            "gateway": "mqtt",
            "topic": topic,
            "payload": payload,
            "ok": True,
        }


class IrGateway(GatewayAdapter):
    """红外网关：把空调档位翻译为红外码，通过红外转发器下发。

    真实部署时红外转发器通常是一个 HTTP 服务（如 ESP8266 + 红外发射管），
    这里以日志形式模拟下发，实际项目中替换为 HTTP 调用即可。
    """

    def _level_to_code(self, level: int) -> str:
        if level <= 0:
            return IR_CODES["off"]
        if level < 40:
            return IR_CODES["cool_26"]
        if level < 70:
            return IR_CODES["cool_24"]
        return IR_CODES["cool_22"]

    def send_ac(self, room: str, device: str, level: int, source: str) -> dict:
        code = self._level_to_code(level)
        # 真实场景：requests.post(f"http://{ir_host}/ir/send", json={"code": code})
        print(f"[IR网关] {device} 下发红外码 {code}（档位 {level}%）")
        self.log_action(
            room, device,
            f"AC={level}% (IR:{code})",
            f"红外网关下发，档位={level}%",
            source,
        )
        return {
            "gateway": "ir",
            "device": device,
            "ir_code": code,
            "level": level,
            "ok": True,
        }

    def send_fan(self, room: str, device: str, on: bool, source: str) -> dict:
        code = IR_CODES["fan_on"] if on else IR_CODES["fan_off"]
        print(f"[IR网关] {device} 下发红外码 {code}（排风 {'开' if on else '关'}）")
        self.log_action(
            room, device,
            f"FAN={'on' if on else 'off'} (IR:{code})",
            f"红外网关下发，排风={'开' if on else '关'}",
            source,
        )
        return {
            "gateway": "ir",
            "device": device,
            "ir_code": code,
            "fan": on,
            "ok": True,
        }


class ModbusGateway(GatewayAdapter):
    """Modbus 网关：把档位/开关翻译为寄存器写入，通过 RTU 或 TCP 下发。

    真实部署时使用 pymodbus 连接 Modbus 设备；这里以日志形式模拟寄存器写入。
    """

    def send_ac(self, room: str, device: str, level: int, source: str) -> dict:
        reg = MODBUS_REGISTERS["ac_level"]
        # 真实场景：client.write_register(reg, level, unit=device_addr)
        print(f"[Modbus网关] {device} 写寄存器 0x{reg:04X} = {level}（空调档位）")
        self.log_action(
            room, device,
            f"AC={level}% (Modbus:0x{reg:04X})",
            f"Modbus 写寄存器 0x{reg:04X}={level}",
            source,
        )
        return {
            "gateway": "modbus",
            "device": device,
            "register": f"0x{reg:04X}",
            "value": level,
            "ok": True,
        }

    def send_fan(self, room: str, device: str, on: bool, source: str) -> dict:
        reg = MODBUS_REGISTERS["fan_state"]
        value = 1 if on else 0
        print(f"[Modbus网关] {device} 写寄存器 0x{reg:04X} = {value}（排风开关）")
        self.log_action(
            room, device,
            f"FAN={'on' if on else 'off'} (Modbus:0x{reg:04X})",
            f"Modbus 写寄存器 0x{reg:04X}={value}",
            source,
        )
        return {
            "gateway": "modbus",
            "device": device,
            "register": f"0x{reg:04X}",
            "value": value,
            "ok": True,
        }


_GATEWAY_CLASSES = {
    "mqtt": MqttGateway,
    "ir": IrGateway,
    "modbus": ModbusGateway,
}


def get_gateway(gateway_type: str, bridge=None) -> GatewayAdapter:
    """根据设备配置的 gateway 类型返回对应的适配器实例。"""
    cls = _GATEWAY_CLASSES.get(gateway_type, MqttGateway)
    return cls(bridge)


def device_gateway(device: str, bridge=None) -> GatewayAdapter:
    """根据设备记录中的 gateway 字段选择适配器，默认 mqtt。"""
    dev = db.get_device(device)
    gw_type = (dev or {}).get("gateway") or "mqtt"
    if gw_type not in _GATEWAY_CLASSES:
        gw_type = "mqtt"
    return get_gateway(gw_type, bridge)
