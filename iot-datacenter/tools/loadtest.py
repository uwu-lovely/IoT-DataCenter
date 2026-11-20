"""MQTT 并发压测脚本：模拟多台设备同时以 QoS 1 上报遥测。

用法：
    .venv\\Scripts\\python.exe tools\\loadtest.py --clients 5 --messages 200

统计指标为发布到 Broker 确认的往返时延（不含平台业务处理耗时），
用于验证内嵌 Broker 与平台接入链路在高并发上报下的稳定性。
"""

import argparse
import json
import statistics
import threading
import time

import paho.mqtt.client as mqtt


def worker(
    idx: int,
    host: str,
    port: int,
    prefix: str,
    messages: int,
    latencies: list[float],
    failures: list[int],
    lock: threading.Lock,
) -> None:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"loadtest-{idx}")
    connected = threading.Event()
    client.on_connect = lambda *args, **kwargs: connected.set()

    client.connect(host, port, keepalive=30)
    client.loop_start()
    if not connected.wait(5):
        client.loop_stop()
        with lock:
            failures.append(messages)
        return

    local: list[float] = []
    failed = 0
    for n in range(messages):
        payload = json.dumps(
            {"ts": time.time(), "metrics": {"temperature": 25.0, "humidity": 50.0}},
            ensure_ascii=False,
        )
        topic = f"{prefix}/{idx:02d}/DEV-{n % 10:02d}/telemetry"
        started = time.perf_counter()
        info = client.publish(topic, payload, qos=1)
        try:
            info.wait_for_publish(timeout=5)
        except (RuntimeError, ValueError):
            failed += 1
            continue
        local.append((time.perf_counter() - started) * 1000)

    client.loop_stop()
    client.disconnect()
    with lock:
        latencies.extend(local)
        failures.append(failed)


def main() -> None:
    parser = argparse.ArgumentParser(description="MQTT 并发上报压测")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--clients", type=int, default=5, help="并发客户端数")
    parser.add_argument("--messages", type=int, default=200, help="每个客户端上报条数")
    parser.add_argument("--prefix", default="iot/bench")
    args = parser.parse_args()

    total = args.clients * args.messages
    latencies: list[float] = []
    failures: list[int] = []
    lock = threading.Lock()

    threads = [
        threading.Thread(
            target=worker,
            args=(i + 1, args.host, args.port, args.prefix, args.messages, latencies, failures, lock),
            daemon=True,
        )
        for i in range(args.clients)
    ]

    print(f"[压测] 客户端 {args.clients} × 报文 {args.messages} = {total} 条，QoS 1")
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - started

    ok = len(latencies)
    failed = sum(failures)
    print(f"[压测] 完成 {ok} 条，失败 {failed} 条，耗时 {elapsed:.2f}s")
    print(f"[压测] 吞吐 {ok / elapsed:.1f} 条/秒")
    if ok:
        ordered = sorted(latencies)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        print(
            f"[压测] 时延 平均 {statistics.mean(ordered):.1f}ms · "
            f"P95 {p95:.1f}ms · 最大 {ordered[-1]:.1f}ms"
        )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
