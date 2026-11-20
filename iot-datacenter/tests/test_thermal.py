"""热模型与 PDU 模型的纯逻辑单元测试。"""

from config import ROOMS
from thermal import PduModel, RoomThermal


def make_room() -> RoomThermal:
    return RoomThermal("roomA", dict(ROOMS["roomA"]))


def test_heat_without_cooling_raises_temperature():
    room = make_room()
    start = room.temp
    # 半分钟内 4kW 发热、空调关闭，温度应明显上升
    for _ in range(30):
        room.step(1 / 60, 4000, 0, 0)
    assert room.temp > start


def test_full_cooling_lowers_temperature():
    room = make_room()
    start = room.temp
    for _ in range(30):
        room.step(1 / 60, 1000, 100, 1)
    assert room.temp < start


def test_temperature_and_humidity_stay_in_bounds():
    room = make_room()
    for _ in range(1000):
        room.step(0.1, 20000, 0, 0)
    assert 16.0 <= room.temp <= 48.0
    assert 28.0 <= room.humidity <= 88.0


def test_sensor_offset_is_applied():
    room = make_room()
    room.temp = 25.0
    sample = room.sensor_temp(0.5)
    assert 25.3 <= sample <= 25.7


def test_pdu_injection_decays_and_has_floor():
    pdu = PduModel("PDU-01", 1000, 0)
    pdu.inject(500)
    pdu.step(0.0)
    before = pdu.injected
    assert before < 500
    pdu.step(1.0)
    assert pdu.injected < before

    # 基础负载很低时仍保证 PDU 输出不低于 200W
    light = PduModel("PDU-02", 100, 0)
    assert all(light.step(t) >= 200.0 for t in range(5))
