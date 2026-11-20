"""下行网关适配器：主题、红外码与 Modbus 寄存器映射。"""

import db
from gateway import (
    IrGateway,
    ModbusGateway,
    MqttGateway,
    device_gateway,
)


class FakeBridge:
    def __init__(self):
        self.calls = []

    def publish(self, topic, payload, apply_link=False):
        self.calls.append((topic, payload))


def test_mqtt_gateway_publishes_standard_topic():
    bridge = FakeBridge()
    gw = MqttGateway(bridge)

    res = gw.send_ac("roomA", "AC-01", 60, "manual")
    fan = gw.send_fan("roomA", "FAN-01", True, "manual")

    assert res["topic"] == "iot/roomA/AC-01/cmd"
    assert bridge.calls[0][1]["level"] == 60
    assert fan["topic"] == "iot/roomA/FAN-01/cmd"
    assert bridge.calls[1][1]["on"] == 1


def test_ir_gateway_maps_level_to_code():
    gw = IrGateway()
    assert gw.send_ac("roomA", "AC-01", 0, "manual")["ir_code"] == "A900"
    assert gw.send_ac("roomA", "AC-01", 30, "manual")["ir_code"] == "A901"
    assert gw.send_ac("roomA", "AC-01", 50, "manual")["ir_code"] == "A902"
    assert gw.send_ac("roomA", "AC-01", 80, "manual")["ir_code"] == "A903"


def test_modbus_gateway_writes_expected_registers():
    gw = ModbusGateway()
    ac = gw.send_ac("roomB", "AC-02", 45, "auto")
    fan = gw.send_fan("roomB", "FAN-02", False, "auto")

    assert ac["register"] == "0x0001" and ac["value"] == 45
    assert fan["register"] == "0x0002" and fan["value"] == 0


def test_device_gateway_follows_device_setting():
    db.register_device("AC-09", "roomA", "actuator", "空调-09", gateway="modbus")
    db.register_device("AC-10", "roomA", "actuator", "空调-10", gateway="ir")
    db.register_device("AC-11", "roomA", "actuator", "空调-11")

    assert isinstance(device_gateway("AC-09"), ModbusGateway)
    assert isinstance(device_gateway("AC-10"), IrGateway)
    assert isinstance(device_gateway("AC-11"), MqttGateway)
    # 未登记设备回退到直连 MQTT
    assert isinstance(device_gateway("UNKNOWN-1"), MqttGateway)
