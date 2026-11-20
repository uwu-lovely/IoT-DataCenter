"""内嵌 MQTT Broker + 平台侧 MQTT 桥接（含链路质量仿真）。

课程要求纯软件仿真，Broker 也内嵌进进程，避免依赖外部 Mosquitto 安装。
链路质量支持丢包、时延、抖动注入，用于验证 QoS1 与监控系统的容错能力。

Broker 安全：
- 默认匿名（仅本机演示）；设置 IOT_MQTT_USERNAME / IOT_MQTT_PASSWORD 后启用账号密码。
- 设置 IOT_MQTT_TLS_CERT / IOT_MQTT_TLS_KEY 后启用 TLS（同时需配置 CA）。
- ACL 限制：设备只能向自己的 telemetry 主题发布、订阅自己的 cmd 主题；
  平台桥接客户端拥有 iot/# 的完整读写权限。
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import tempfile
from collections import deque
from pathlib import Path

import paho.mqtt.client as mqtt
from amqtt.broker import Broker
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from config import (
    MQTT_ALLOW_ANONYMOUS,
    MQTT_BIND_HOST,
    MQTT_HOST,
    MQTT_PASSWORD,
    MQTT_PORT,
    MQTT_TLS_CA,
    MQTT_TLS_CERT,
    MQTT_TLS_KEY,
    MQTT_USE_TLS,
    MQTT_USERNAME,
)

logging.getLogger("amqtt").setLevel(logging.WARNING)
logging.getLogger("transitions").setLevel(logging.WARNING)

# 平台桥接客户端的固定账号（不受环境变量影响），拥有 iot/# 完整权限
PLATFORM_USERNAME = "iot-platform"
PLATFORM_PASSWORD = hashlib.sha256(b"iot-platform-internal-secret").hexdigest()[:16]

# amqtt FileAuthPlugin 仅支持 Argon2 / sha512_crypt 哈希，统一用 Argon2 生成
_pwd_hasher = PasswordHash((Argon2Hasher(),))


def _write_password_file() -> str | None:
    """生成 amqtt FileAuthPlugin 所需的密码文件（Argon2 哈希），返回文件路径。匿名模式返回 None。"""
    if MQTT_ALLOW_ANONYMOUS:
        return None
    lines = []
    lines.append(f"{PLATFORM_USERNAME}:{_pwd_hasher.hash(PLATFORM_PASSWORD)}")
    if MQTT_USERNAME and MQTT_PASSWORD:
        lines.append(f"{MQTT_USERNAME}:{_pwd_hasher.hash(MQTT_PASSWORD)}")
    fd, path = tempfile.mkstemp(prefix="mqtt_passwd_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _build_broker_config() -> dict:
    """根据环境变量构建 Broker 配置。

    使用 amqtt 新风格 plugins dict，只加载显式列出的插件，
    避免 entry point 方式加载全部插件（会在匿名模式下误实例化 FileAuthPlugin 并告警）。
    """
    listeners = {
        "default": {
            "type": "tcp",
            "bind": f"{MQTT_BIND_HOST}:{MQTT_PORT}",
            "max_connections": 100,
        }
    }
    if MQTT_USE_TLS:
        listeners["default"]["ssl"] = "on"
        listeners["default"]["certfile"] = MQTT_TLS_CERT
        listeners["default"]["keyfile"] = MQTT_TLS_KEY
        if MQTT_TLS_CA:
            listeners["default"]["cafile"] = MQTT_TLS_CA

    plugins: dict[str, dict] = {
        "amqtt.plugins.sys.broker.BrokerSysPlugin": {"sys_interval": 10},
    }

    if MQTT_ALLOW_ANONYMOUS:
        plugins["amqtt.plugins.authentication.AnonymousAuthPlugin"] = {
            "allow_anonymous": True
        }
    else:
        plugins["amqtt.plugins.authentication.FileAuthPlugin"] = {
            "password_file": _write_password_file()
        }

    # 主题 ACL：平台账号 iot/# 全权限；设备/匿名账号限 telemetry 发布、cmd 订阅
    publish_acl: dict[str, list[str]] = {PLATFORM_USERNAME: ["iot/#"]}
    subscribe_acl: dict[str, list[str]] = {PLATFORM_USERNAME: ["iot/#"]}
    if not MQTT_ALLOW_ANONYMOUS and MQTT_USERNAME:
        publish_acl[MQTT_USERNAME] = ["iot/+/+/telemetry"]
        subscribe_acl[MQTT_USERNAME] = ["iot/+/+/cmd"]
    else:
        publish_acl["anonymous"] = ["iot/+/+/telemetry"]
        subscribe_acl["anonymous"] = ["iot/+/+/cmd"]
    plugins["amqtt.plugins.topic_checking.TopicAccessControlListPlugin"] = {
        "publish_acl": publish_acl,
        "acl": subscribe_acl,
    }

    return {"listeners": listeners, "plugins": plugins}


async def start_broker() -> Broker:
    config = _build_broker_config()
    broker = Broker(config)
    await broker.start()
    if not MQTT_ALLOW_ANONYMOUS:
        print(f"[Broker] 已启用账号密码认证（用户：{MQTT_USERNAME or '仅内部账号'}）")
    if MQTT_USE_TLS:
        print(f"[Broker] 已启用 TLS 加密（端口 {MQTT_PORT}）")
    return broker


class MqttBridge:
    """单客户端同时负责发布与订阅；消息回调转交给 asyncio 事件循环。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, on_message) -> None:
        self.loop = loop
        self._on_message = on_message
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="iot-platform")
        # 平台桥接客户端始终使用内部账号：ACL 中只有该账号拥有 iot/# 订阅权限，
        # 匿名连接只能订阅 cmd 主题，会导致上行遥测无法回传平台
        self.client.username_pw_set(PLATFORM_USERNAME, PLATFORM_PASSWORD)
        if MQTT_USE_TLS:
            # 平台桥接与 Broker 在同一进程，使用 TLS 时需校验证书链
            ca_certs = MQTT_TLS_CA or MQTT_TLS_CERT
            self.client.tls_set(ca_certs=ca_certs)
            self.client.tls_insecure_set(True)  # 本地自签证书不校验域名
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message_cb
        self.connected = False

        self.loss_rate = 0.0
        self.delay_ms = 0
        self.jitter_ms = 0
        # 设备侧离线缓存：断网期间上行报文先落缓存，恢复后按序重传
        self.buffer: deque = deque(maxlen=1000)
        # 已调度的时延报文，切换运行模式时需要取消，避免旧模式报文串到新模式
        self._pending: list = []
        # 设备自定义上报主题（不在 iot/# 之下时需要单独订阅）
        self.custom_topics: set[str] = set()
        self.stats = {
            "sent": 0,
            "dropped": 0,
            "delayed": 0,
            "received": 0,
            "buffered": 0,
            "replayed": 0,
        }

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = True
        client.subscribe("iot/#", qos=1)
        for topic in self.custom_topics:
            client.subscribe(topic, qos=1)
        # 连接恢复后立即回放离线缓存
        self.loop.call_soon_threadsafe(self._flush)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False

    def _on_message_cb(self, client, userdata, msg):
        # 统计上行遥测；下行 cmd 也会被本客户端订阅到，计入会让“平台接收”大于“已发送”
        if not msg.topic.endswith("/cmd"):
            self.stats["received"] += 1
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        asyncio.run_coroutine_threadsafe(self._on_message(msg.topic, payload), self.loop)

    def start(self) -> None:
        self.client.reconnect_delay_set(min_delay=1, max_delay=8)
        self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def subscribe_topic(self, topic: str) -> None:
        """订阅设备自定义主题；连接建立前先登记，连上后由 _on_connect 统一订阅。"""
        self.custom_topics.add(topic)
        if self.connected:
            self.client.subscribe(topic, qos=1)

    def unsubscribe_topic(self, topic: str) -> None:
        self.custom_topics.discard(topic)
        if self.connected:
            self.client.unsubscribe(topic)

    # ---- 链路质量 ----
    def set_link(self, loss_rate: float = 0.0, delay_ms: int = 0, jitter_ms: int = 0) -> None:
        was_offline = self.loss_rate >= 1.0
        self.loss_rate = max(0.0, min(1.0, loss_rate))
        self.delay_ms = max(0, int(delay_ms))
        self.jitter_ms = max(0, int(jitter_ms))
        if was_offline and self.loss_rate < 1.0:
            print(f"[链路] 网络恢复，准备重传缓存报文 {len(self.buffer)} 条")
            self._flush()

    def link_stats(self) -> dict:
        sent = self.stats["sent"]
        dropped = self.stats["dropped"]
        pending = len(self.buffer)
        delivered = max(0, sent - dropped - pending)
        return {
            "connected": self.connected,
            "offline": self.loss_rate >= 1.0 or not self.connected,
            "loss_rate": round(self.loss_rate, 3),
            "delay_ms": self.delay_ms,
            "jitter_ms": self.jitter_ms,
            "sent": sent,
            "dropped": dropped,
            "received": self.stats["received"],
            "buffered": pending,
            "replayed": self.stats["replayed"],
            "delivery_ratio": round(delivered / sent * 100, 2) if sent else 100.0,
            "qos": 1,
        }

    def reset_stats(self) -> None:
        self.stats = {
            "sent": 0,
            "dropped": 0,
            "delayed": 0,
            "received": 0,
            "buffered": 0,
            "replayed": 0,
        }

    def drop_buffer(self) -> None:
        """切换运行模式时丢弃缓存与链路统计，避免回放上一模式的旧报文。"""
        for handle in self._pending:
            handle.cancel()
        self._pending.clear()
        self.buffer.clear()
        self.set_link(0.0, 0, 0)
        self.reset_stats()

    def publish(self, topic: str, payload: dict, apply_link: bool = False) -> None:
        """apply_link=True 时按配置注入丢包与时延，仅用于上行遥测。"""
        if apply_link:
            self.stats["sent"] += 1
            # 断网（丢包率 100%）或 Broker 未连接时，设备侧先缓存，恢复后重传
            if not self.connected or self.loss_rate >= 1.0:
                self._buffer(topic, payload)
                return
            if self.loss_rate > 0 and random.random() < self.loss_rate:
                self.stats["dropped"] += 1
                return
            delay = self.delay_ms
            if self.jitter_ms:
                delay = max(0, delay + random.uniform(-self.jitter_ms, self.jitter_ms))
            if delay > 0:
                self.stats["delayed"] += 1
                handle = self.loop.call_later(delay / 1000.0, self._do_publish, topic, payload)
                self._pending.append(handle)
                if len(self._pending) > 500:
                    self._pending = [h for h in self._pending if not h.cancelled()]
                return
        self._do_publish(topic, payload)

    def _buffer(self, topic: str, payload: dict) -> None:
        if len(self.buffer) >= self.buffer.maxlen:
            self.buffer.popleft()
            self.stats["dropped"] += 1
        self.buffer.append((topic, payload))
        self.stats["buffered"] += 1

    def _flush(self) -> None:
        """分批回放离线缓存，避免恢复瞬间压垮 Broker。"""
        if not self.connected or not self.buffer:
            return
        batch = [self.buffer.popleft() for _ in range(min(80, len(self.buffer)))]
        for topic, payload in batch:
            self.client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1)
            self.stats["replayed"] += 1
        if self.buffer:
            self.loop.call_later(0.15, self._flush)

    def _do_publish(self, topic: str, payload: dict) -> None:
        self.client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1)
