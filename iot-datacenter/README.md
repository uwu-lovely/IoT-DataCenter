# 智慧机房环境监测与能耗优化平台

《物联网基础实践》课程项目。默认纯软件仿真，无需真实硬件；也可切换为“实际监测模式”，直接接入外部机房的 MQTT 设备。虚拟传感器经内嵌 MQTT Broker 上报遥测，平台完成规则告警、时序存储、策略调控、大屏展示与受控对照实验。

## 功能特性

- **仿真数据链路**：虚拟设备 → MQTT Broker（内嵌）→ 规则引擎/时序库 → WebSocket → 浏览器大屏，主题为 `iot/{room}/{device}/telemetry|cmd`，上行遥测使用 QoS 1。
- **数字孪生热模型**：一阶集总热模型 + PDU 功率模型，机柜负载、室外温度、空调档位共同决定室温与湿度。
- **规则告警**：温度、湿度、单路功率、烟雾、漏水、门禁、UPS 电量、电压、温升速率、设备心跳超时；温度/湿度按机房聚合判定，同一机房多路传感器只保留一条活动告警，避免重复刷屏。
- **测试数据隔离**：测试报文标记 `is_test=1` 入库（便于链路审计），但不更新最新值、不触发告警、不影响在线状态，所有历史/统计查询均过滤 `is_test=0`。
- **设备信息在线编辑**：已接入设备可在页面点「编辑」修改名称、机房、类型、上报主题与下行网关，无需重新接入。
- **离线阈值可配置**：设备心跳超时阈值默认 20s，可在「设备台账」卡片运行时调整（5~86400s），真实设备采样 30~60s 时建议设为 90~120s，变更后立即重新评估在线状态。
- **三种调控策略**：阈值控制、按需最小制冷、预测式主动调控，可在线切换。
- **下行网关适配**：手动/自动控制命令经网关适配器下发，支持直连 MQTT（默认）、红外网关（空调红外码）、Modbus 网关（寄存器写入），按设备独立配置。
- **Broker 安全**：支持用户名密码认证（`IOT_MQTT_USERNAME`/`IOT_MQTT_PASSWORD`）、TLS 加密（`IOT_MQTT_TLS_CERT`/`IOT_MQTT_TLS_KEY`）与主题 ACL，默认匿名仅用于本机演示。
- **受控对照实验**：相同负载脚本、相同初始环境，依次运行三种策略，自动统计平均功率、超温占比、均温、峰值与制冷能耗，支持导出明细/汇总 CSV。
- **链路质量仿真**：丢包、时延、抖动注入；断网时设备侧缓存报文，恢复后按序重传并统计重传条数。
- **可视化大屏**：机房实时状态、温度/功率/湿度趋势、各 PDU 功率占比、设备状态分布、告警中心、调控日志、设备台账、策略对比；告警条目可点击定位到对应机房卡片。
- **工程化配套**：pytest 单元测试（热模型、三种策略、规则引擎、时序库、网关适配）、MQTT 并发压测脚本。

## 快速开始

```powershell
cd iot-datacenter
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe run.py
```

浏览器访问 <http://127.0.0.1:8000>。仿真模式启动时会重建演示数据库并清空运行期数据，从干净状态开始。

## 运行模式

平台支持两种可切换的数据源模式，页面顶部按钮或启动参数均可选择：

| 模式 | 数据来源 | 启用能力 |
| --- | --- | --- |
| 仿真模式（默认） | 内置虚拟设备与热模型 | 演示注入、链路质量仿真、三种调控策略、受控对照实验 |
| 实际监测模式 | 外部设备经 MQTT 上报 | 实时监测、规则告警、设备自动登记、手动下发命令、30 天数据保留 |

切换方式：

- 页面顶部「仿真模式 / 实际监测」按钮（切换会清空当前遥测、告警与实验数据）；
- 启动参数：`.venv\Scripts\python.exe run.py --mode real`；
- 环境变量：`$env:IOT_MODE = "real"`。

实际监测模式下，设备按以下主题上报，平台自动登记设备并把遥测聚合为机房快照：

```
iot/<room>/<device>/telemetry
{"ts": 1763600000.0, "room": "roomA", "device": "TH-01",
 "metrics": {"temperature": 24.6, "humidity": 55.2}}
```

也可以不手写报文：切换到实际监测模式后，页面顶部会出现「接入真实设备」面板。填写机房、设备编号（如 `TH-101`）、类型与下行网关（直连 MQTT / 红外网关 / Modbus 网关），点「接入并检测」，平台会校验编号格式与类型前缀，生成上报主题和示例报文；主题和报文都可以直接编辑，主题保存后平台会自动订阅新主题（支持非 `iot/` 开头的自定义主题），报文编辑后点「测试」会按当前内容发送。设备上报后状态从「等待上报」变为「在线」，并列出已收到的指标与缺失指标。默认 Broker ACL 已放行自定义主题；若自行收紧 ACL，需要同步放行平台账号订阅和设备发布主题。

**已接入设备可在线编辑**：在设备列表点「编辑」即可修改名称、机房、类型、上报主题与下行网关，保存后立即生效，无需移除重接。设备登记（编号、名称、机房、类型、上报主题、网关）会持久化保存，仿真/实际监测模式来回切换后自动恢复，无需重新接入。

**测试报文隔离**：点「测试」发送的报文会标记 `is_test=1` 入库（便于链路审计），但不会更新设备最新值、不会触发告警、不会影响在线状态，所有历史曲线与统计指标均只统计真实遥测（`is_test=0`）。

**下行网关适配**：控制命令（空调档位、排风开关）经网关适配器下发：
- 直连 MQTT（默认）：向 `iot/<room>/<device>/cmd` 发布 JSON 命令，适用于仿真模式与支持 MQTT 的设备；
- 红外网关：把空调档位翻译为红外码序列，通过红外转发器下发（适合无 MQTT 的传统空调）；
- Modbus 网关：把档位/开关翻译为寄存器写入，通过 Modbus RTU/TCP 下发（适合工业 PDU/执行器）。

每种网关的原始协议报文会写入调控动作日志，便于联调与审计。

机房名沿用 `roomA` / `roomB`（大屏按这两个机房展示）；设备号建议用 `TH-`、`PDU-`、`SMK-`、`WLK-`、`DR-`、`UPS-`、`AF-`、`AC-`、`FAN-` 前缀，平台据此识别温湿度、电力、烟雾、漏水、门禁、UPS、风速、空调/排风等类型。接入向导的设备类型下拉框同样支持这些类型，测试报文本也会按类型生成对应指标。

局域网设备接入内嵌 Broker 时，需要让 Broker 监听所有网卡后重启：

```powershell
$env:IOT_MQTT_BIND = "0.0.0.0"
.venv\Scripts\python.exe run.py --mode real
```

实际监测模式不启动仿真器与自动调控，演示注入、链路注入、对照实验接口均返回 409；遥测与机房状态默认保留 30 天（可用 `IOT_RETENTION_MINUTES` 调整）。

## Broker 安全

默认匿名认证仅用于本机演示。接入外网设备前，务必启用账号密码与 TLS：

```powershell
# 用户名密码认证
$env:IOT_MQTT_USERNAME = "device"
$env:IOT_MQTT_PASSWORD = "your-strong-password"

# TLS 加密（需先生成自签证书或使用 CA 签发的证书）
$env:IOT_MQTT_TLS_CERT = "C:\certs\broker.crt"
$env:IOT_MQTT_TLS_KEY  = "C:\certs\broker.key"
$env:IOT_MQTT_TLS_CA   = "C:\certs\ca.crt"   # 可选，用于客户端校验
```

启用认证后：
- 外部设备必须使用配置的用户名密码连接 Broker；
- 平台桥接客户端使用内部账号（自动注入，无需手动配置）；
- 主题 ACL 限制：设备只能向 `iot/+/+/telemetry` 发布、订阅 `iot/+/+/cmd`，平台账号拥有 `iot/#` 完整权限。

平台启动时会打印 Broker 安全状态（匿名/密码认证、是否启用 TLS），`/api/bootstrap` 的 `system` 字段也会返回 `mqtt_auth` 与 `mqtt_tls` 供前端展示。

## 离线阈值配置

设备心跳超时阈值默认 20 秒（`IOT_OFFLINE_SECONDS` 环境变量可改默认值）。真实设备采样周期通常为 30~60 秒，固定 20s 会误报离线。可在页面「设备台账」卡片的「离线判定阈值」输入框中运行时调整（5~86400 秒），保存后立即重新评估所有设备的在线状态，阈值放宽后被误判离线的设备会自动恢复在线并收敛离线告警。

## 项目结构

```
iot-datacenter/
├─ run.py                  启动入口（uvicorn）
├─ requirements.txt
├─ backend/
│  ├─ config.py            机房/设备拓扑、阈值、调控参数、Broker 安全配置
│  ├─ db.py                SQLite 时序存储与统计查询
│  ├─ thermal.py           热模型与 PDU 功率模型
│  ├─ simulator.py         虚拟设备与 MQTT 报文生成
│  ├─ mqtt.py              内嵌 Broker（认证/ACL/TLS）、MQTT 桥接、链路质量与离线缓存
│  ├─ gateway.py           下行控制网关适配（直连 MQTT / 红外 / Modbus）
│  ├─ rules.py             告警规则引擎
│  ├─ control.py           三种调控策略
│  ├─ experiment.py        受控对照实验调度
│  └─ main.py              FastAPI 接口、WebSocket、后台循环
├─ frontend/
│  ├─ index.html           监控大屏
│  ├─ app.js               实时渲染与交互
│  ├─ style.css
│  └─ vendor/echarts.min.js
├─ tests/                  pytest 单元测试
└─ tools/loadtest.py       MQTT 并发压测脚本
```

## 调控策略

| 策略 | 控制思想 | 目标温度 | 特点 |
| --- | --- | --- | --- |
| 阈值控制（threshold） | 温度达到 28°C 才启动 88% 档制冷，降到 26°C 回落空档 | 26°C | 实现简单、能耗低，但温度在阈值附近反复波动 |
| 按需最小制冷（adaptive） | 以 PDU 功率做前馈，按“抵消当前热负荷所需档位”供冷；软约束目标 27°C，只有温度偏差或即将越界时追加制冷 | 27°C | 供需匹配、避免过度预冷，在能耗与温度之间折中 |
| 预测式调控（predictive） | 用最小二乘拟合温升斜率，预测 3 分钟后温度，提前投入制冷 | 26°C | 温度最平稳、超温最少，但前期预冷带来更高能耗 |

控制命令经 MQTT 下发到 `AC-xx` / `FAN-xx`，设备仿真器执行后通过遥测回传实际档位，形成闭环。

## 受控对照实验

- 实验开始前统一复位机房初始温度、PDU 注入负载、控制器与规则引擎状态，并静置 8 秒。
- 三组策略使用同一负载脚本：第 15 秒机房A +4kW、第 35 秒机房B +3kW、第 80 秒机房A +2.5kW。
- 指标定义：平均制冷功率 = `ac_level/100*6kW + fan*0.6kW`；超温占比 = 室温 > 28°C 的采样比例；制冷能耗 = 平均功率 × 时长。
- 实验完成后可在页面导出 `experiment_samples.csv`（逐秒明细）与 `experiment_summary.csv`（策略汇总）。

本机实测结果（每组 180 秒）：

| 策略 | 平均制冷功率 | 超温占比 | 平均温度 | 最高温度 | 制冷能耗 |
| --- | --- | --- | --- | --- | --- |
| 阈值控制 | 1.77 kW | 17.6% | 26.7°C | 28.4°C | 0.089 kWh |
| 按需最小制冷 | 2.86 kW | 0.0% | 26.1°C | 26.9°C | 0.144 kWh |
| 预测式调控 | 3.33 kW | 0.0% | 25.6°C | 26.4°C | 0.167 kWh |

阈值控制能耗最低但超温风险明显；按需最小制冷在零超温前提下比预测式调控更省电，体现了功率前馈与软约束的作用。结果随机器性能与随机噪声略有波动，以本机实测导出为准。

## 链路质量与离线重传

- 页面预设：正常、10% 丢包、300ms 时延、20% 丢包 + 800ms 抖动、断网。
- 断网（丢包率 100%）或 Broker 未连接时，上行遥测进入设备侧有界缓存（最多 1000 条）；链路恢复后分批回放，保证数据最终一致。
- 统计项：已发送、已丢失、缓存待传、已重传、送达率、平台接收。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/bootstrap` | 首屏全量数据（机房、设备、告警、指标、实验、链路、系统信息） |
| GET | `/api/history` | 历史遥测（room、metric、minutes），仅返回真实数据（is_test=0） |
| GET | `/api/alarms` | 活动告警与历史告警 |
| POST | `/api/alarms/ack` | 确认告警 |
| POST | `/api/mode` | 切换调控策略（threshold/adaptive/predictive） |
| POST | `/api/control` | 手动下发空调/排风命令（经网关适配器） |
| POST | `/api/demo/{load,smoke,fault,water-leak,door,ac-fault,outdoor-spike,humidity-spike,voltage-fluct}` | 演示注入：负载、烟雾、设备故障、漏水、门禁、空调故障、室外高温、湿度骤升、电压波动（仅仿真模式） |
| GET/POST | `/api/link` | 查询/配置链路质量 |
| GET/POST | `/api/experiment/status\|start\|cancel` | 对照实验控制 |
| GET | `/api/experiment/export?kind=raw\|summary` | 导出实验结果 CSV |
| POST | `/api/system/mode` | 切换仿真 / 实际监测模式 |
| POST | `/api/system/offline-seconds` | 运行时调整设备离线判定阈值 |
| GET/POST | `/api/real/devices` | 查询 / 接入（或编辑）真实设备 |
| GET | `/api/real/devices/{device}/template` | 获取设备编辑回填信息（名称/类型/机房/主题/网关） |
| POST | `/api/real/devices/test` | 发送测试遥测（is_test=1，不影响统计与告警） |
| POST | `/api/real/devices/topic` | 修改设备上报主题 |
| POST | `/api/real/devices/gateway` | 修改设备下行网关类型（mqtt/ir/modbus） |
| DELETE | `/api/real/devices/{device}` | 移除设备及其遥测与告警 |
| WS | `/ws` | 实时增量推送 |

## 测试与压测

```powershell
# 单元测试（热模型、三种控制策略、告警规则与机房去重、时序库、网关适配）
.venv\Scripts\python.exe -m pytest -q

# MQTT 并发压测（默认 5 客户端 × 200 条 QoS1 报文）
.venv\Scripts\python.exe tools\loadtest.py --clients 5 --messages 200
```

## 环境变量参考

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `IOT_MODE` | `sim` | 运行模式：`sim`（仿真）/ `real`（实际监测） |
| `IOT_MQTT_PORT` | `1883` | MQTT Broker 监听端口 |
| `IOT_MQTT_BIND` | `127.0.0.1` | Broker 绑定地址，接入局域网设备设为 `0.0.0.0` |
| `IOT_MQTT_USERNAME` | _空_ | Broker 认证用户名，设置后启用密码认证 |
| `IOT_MQTT_PASSWORD` | _空_ | Broker 认证密码 |
| `IOT_MQTT_TLS_CERT` | _空_ | Broker TLS 证书路径，设置后启用 TLS |
| `IOT_MQTT_TLS_KEY` | _空_ | Broker TLS 私钥路径 |
| `IOT_MQTT_TLS_CA` | _空_ | Broker TLS CA 证书路径（可选） |
| `IOT_OFFLINE_SECONDS` | `20` | 设备离线判定阈值（秒），运行时也可通过接口调整 |
| `IOT_ENV_FRESH_SECONDS` | `90` | 温湿度机房级告警的测点新鲜窗口（秒），真实设备采样 30~60s 时保持默认即可 |
| `IOT_RETENTION_MINUTES` | `43200` | 实际监测模式数据保留时长（分钟，默认 30 天） |
| `IOT_REAL_TICK` | `2` | 实际监测模式机房快照聚合周期（秒） |

## 说明

- 所有设备均为软件仿真，温度、湿度、功率带有可控随机噪声，用于模拟真实测点波动。
- SQLite 采用 WAL 模式，单连接加锁，满足课程演示规模；启动时自动清理运行期数据。
- 前端依赖本地 `vendor/echarts.min.js`，无需外网。
- 数据库 schema 版本号为 6，启动时自动迁移旧库结构（topic / is_test / gateway 字段）。
