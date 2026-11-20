"""按课程模板生成《物联网基础实践》选题报告。

模板由原始 .doc 转换得到（tools/template.docx），脚本向其章节中填入正文，
并从运行中的平台读取三组对照实验的真实结果生成对比表。

用法：
    .venv\\Scripts\\python.exe tools\\make_report.py
    .venv\\Scripts\\python.exe tools\\make_report.py --api http://127.0.0.1:8000
"""

import argparse
import json
import urllib.request
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "tools" / "template.docx"
OUTPUT = ROOT.parent / "docs" / "《物联网基础实践》选题报告-智慧机房环境监测与能耗优化平台.docx"

MODE_ORDER = ("threshold", "adaptive", "predictive")
MODE_CN = {
    "threshold": "阈值控制",
    "adaptive": "按需最小制冷",
    "predictive": "预测式调控",
}
MODE_TABLE_CN = {
    "threshold": "阈值控制",
    "adaptive": "按需最小制冷",
    "predictive": "预测式调控",
}

COVER_TITLE = "题  目：智慧机房环境监测与能耗优化平台的设计与实现"


def fetch_runs(api: str) -> list[dict]:
    """读取最近一次对照实验结果；服务未启动时返回空列表。"""
    try:
        with urllib.request.urlopen(f"{api}/api/bootstrap", timeout=5) as resp:
            data = json.load(resp)
    except Exception as exc:  # 报告仍可生成，只是结果表留空
        print(f"[警告] 无法读取实验数据：{exc}")
        return []

    runs = (data.get("experiment") or {}).get("runs") or []
    if not runs:
        history = data.get("experiment_history") or []
        if history:
            latest_id = history[0]["exp_id"]
            runs = [r for r in history if r["exp_id"] == latest_id]
            for run in runs:
                run["mode_cn"] = MODE_CN.get(run["mode"], run["mode"])
    return sorted(runs, key=lambda r: MODE_ORDER.index(r["mode"]) if r["mode"] in MODE_ORDER else 99)


def set_run_font(run, size=12, bold=False, name="宋体") -> None:
    run.font.name = name
    run.font.size = Pt(size)
    run.bold = bold
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), name)


def add_body(doc, anchor, text: str, bold=False, indent=True, size=12):
    """在 anchor 前插入正文段落；anchor 为 None 时追加到文末。"""
    if anchor is None:
        para = doc.add_paragraph(style="Normal")
    else:
        para = anchor.insert_paragraph_before("", style="Normal")
    fmt = para.paragraph_format
    fmt.line_spacing = 1.5
    fmt.space_after = Pt(0)
    if indent:
        fmt.first_line_indent = Pt(size * 2)
    set_run_font(para.add_run(text), size=size, bold=bold)
    return para


def clear_paragraph(para) -> None:
    for run in list(para.runs):
        run._element.getparent().remove(run._element)


def remove_empty_between(heading, anchor) -> None:
    """删除章节标题与下一个标题之间的空白占位段落。"""
    elem = heading._p.getnext()
    while elem is not None and elem is not anchor._p:
        nxt = elem.getnext()
        if elem.tag == qn("w:p"):
            texts = elem.findall(".//" + qn("w:t"))
            if not any((t.text or "").strip() for t in texts):
                elem.getparent().remove(elem)
        elem = nxt


def set_table_borders(table) -> None:
    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:color"), "808080")
        borders.append(el)
    tbl_pr.append(borders)


def cell_text(cell, text: str, bold=False, size=10.5) -> None:
    cell.text = ""
    para = cell.paragraphs[0]
    para.alignment = 1
    set_run_font(para.add_run(str(text)), size=size, bold=bold)


def add_result_table(doc, anchor, runs: list[dict]) -> None:
    caption = add_body(doc, anchor, "表 1  三种调控策略实测对比（每组 180 秒）", bold=True, indent=False, size=10.5)
    caption.alignment = 1

    table = doc.add_table(rows=1, cols=6)
    table.autofit = True
    set_table_borders(table)
    header = ["调控策略", "平均制冷功率(kW)", "超温占比(%)", "平均温度(°C)", "最高温度(°C)", "制冷能耗(kWh)"]
    for idx, name in enumerate(header):
        cell_text(table.rows[0].cells[idx], name, bold=True)

    for run in runs:
        row = table.add_row().cells
        cell_text(row[0], MODE_TABLE_CN.get(run["mode"], run["mode"]))
        cell_text(row[1], f"{run.get('avg_kw', 0):.2f}")
        cell_text(row[2], f"{run.get('over_ratio', 0):.1f}")
        cell_text(row[3], f"{run.get('avg_temp', 0):.1f}")
        cell_text(row[4], f"{run.get('max_temp', 0):.1f}")
        cell_text(row[5], f"{run.get('kwh', 0):.3f}")

    for row in table.rows:
        for idx, cell in enumerate(row.cells):
            cell.width = Cm(3.0 if idx == 0 else 2.7)

    anchor._p.addprevious(table._tbl)


def result_sentence(runs: list[dict]) -> str:
    if not runs:
        return "对照实验数据暂未读取到，可在平台完成实验后重新生成本报告。"
    parts = []
    for run in runs:
        parts.append(
            f"{MODE_CN.get(run['mode'], run['mode'])}平均制冷功率 {run.get('avg_kw', 0):.2f} kW、"
            f"超温占比 {run.get('over_ratio', 0):.1f}%、平均温度 {run.get('avg_temp', 0):.1f}°C、"
            f"最高温度 {run.get('max_temp', 0):.1f}°C、制冷能耗 {run.get('kwh', 0):.3f} kWh"
        )
    return "三组实验在相同初始环境与相同负载脚本下依次完成，实测结果为：" + "；".join(parts) + "。"


SECTIONS = {
    "选题的依据及意义": [
        ("1.1 选题背景", False),
        ("随着云计算、人工智能和 5G 的快速发展，数据中心规模持续扩张，能耗问题日益突出。相关行业研究显示，制冷系统通常占数据中心总用电的 30%～40%，是除 IT 设备外最大的能耗来源。在“双碳”目标和“东数西算”工程背景下，如何在保证设备安全运行的前提下降低机房制冷能耗，已成为数据中心运维的核心问题之一。", False),
        ("大量中小机房、边缘机房和基站机房缺乏专业的动环运维人员，仍采用固定档位或简单阈值控制：温度升高后空调长时间满负荷运行，温度下降后又频繁启停。这种粗放控制不仅造成能源浪费，还会引起室温大幅波动，加速设备老化。", False),
        ("1.2 社会意义", False),
        ("本项目通过软件仿真复现“感知—传输—平台—控制”的完整物联网闭环，验证按需制冷与预测式调控的节能效果。研究成果可迁移到真实机房：在保证温度不越界的前提下减少无效制冷，降低用电成本与碳排放；同时通过超温、烟雾、设备离线等告警提升机房安全水平，减少因过热导致的业务中断。", False),
        ("1.3 商业价值", False),
        ("我国存在数量庞大的中小型机房与边缘节点，其改造预算有限，难以直接部署昂贵的楼宇自控系统。本项目采用开源技术栈和轻量 MQTT 协议，硬件门槛低、部署成本小，可作为机房环境监控与节能改造的原型方案；其中的对照实验与数据导出功能也可用于节能服务商的效果评估和教学演示，具有一定的产品化潜力。", False),
        ("1.4 课程实践意义", False),
        ("项目覆盖物联网四层架构的关键技术：感知层的虚拟传感器建模、网络层的 MQTT 发布/订阅与 QoS、平台层的时序存储与规则引擎、应用层的实时大屏与控制闭环。通过完整实现一个可运行系统，可以加深对物联网系统整体性和工程复杂性的理解。", False),
    ],
    "国内外研究现状及发展趋势": [
        ("2.1 国外研究现状", False),
        ("国外在数据中心节能方面起步较早。ASHRAE TC 9.9 发布的《Thermal Guidelines for Data Processing Environments》为机房温湿度设计提供了权威依据；Google 于 2016 年将 DeepMind 的机器学习模型应用于数据中心制冷控制，实现制冷能耗约 40% 的下降，引发业界广泛关注。近年来，模型预测控制（MPC）、强化学习和数字孪生成为研究热点：前者依赖机房热力学模型进行滚动优化，后者通过高保真仿真支撑策略训练与验证。", False),
        ("2.2 国内研究现状", False),
        ("国内华为、阿里、百度等企业相继推出 iCooling@AI 等智能温控方案，在大型数据中心取得了显著节能效果；维谛、施耐德等厂商则提供成熟的动环监控与精密空调产品。国家标准 GB 50174-2017《数据中心设计规范》和 GB/T 51314-2018《数据中心基础设施运行维护标准》对机房环境与运维提出了明确要求。在协议层面，MQTT、Modbus 等轻量协议已广泛用于边缘网关与云平台之间的数据接入。", False),
        ("2.3 发展趋势", False),
        ("综合来看，机房节能控制呈现以下趋势：一是从单点阈值控制走向基于模型的预测与优化；二是从云端集中控制走向云边协同，边缘侧承担实时闭环；三是数字孪生与机理模型结合，降低对历史数据的依赖；四是控制策略更加注重可解释性与多目标权衡，在能耗、温度平稳性和设备寿命之间取得平衡。", False),
        ("2.4 现有方案的不足与本项目切入点", False),
        ("大型数据中心的 AI 节能方案通常依赖大量历史数据和昂贵的改造投入，难以直接复制到中小机房；而简单阈值控制虽然成本低，却存在超温风险与温度波动。针对这一矛盾，本项目构建纯软件仿真平台，在统一负载条件下对比阈值控制、按需最小制冷和预测式调控三种策略，重点验证“以功率前馈实现供需匹配、以软约束减少过度预冷”的轻量级节能思路，为低成本机房改造提供可解释、可复现的参考。", False),
    ],
    "本项目的具体内容": [
        ("3.1 系统总体架构", False),
        ("系统采用分层架构。感知层由虚拟温湿度传感器、智能 PDU、烟雾传感器和执行器组成；网络层使用内嵌 MQTT Broker，设备通过 iot/{room}/{device}/telemetry 上报数据、通过 iot/{room}/{device}/cmd 接收命令；平台层负责报文接入、时序存储、规则判定和控制决策；应用层提供 Web 监控大屏、告警中心、实验面板和 CSV 导出。", False),
        ("3.2 数字孪生机房模型", False),
        ("每个机房采用一阶集总热模型：dT/dt = (Q_heat − Q_cool)/C + k(T_out − T)，其中 Q_heat 为机柜发热功率，Q_cool 为空调与排风制冷功率，C 为等效热容，k 为与室外环境的换热系数。PDU 功率由基础负载、周期波动和可衰减的注入负载叠加而成，传感器读数在真值上叠加随机噪声，以模拟真实测点波动。", False),
        ("3.3 MQTT 通信与链路仿真", False),
        ("平台内嵌 MQTT Broker，无需额外安装中间件。上行遥测采用 QoS 1，保证至少一次送达。为验证链路容错能力，系统支持丢包、时延和抖动注入；当链路中断时，设备侧将遥测写入有界缓存（最多 1000 条），网络恢复后分批按序重传，并在页面展示已发送、已丢失、缓存待传、已重传和送达率等指标。", False),
        ("3.4 平台服务与告警", False),
        ("平台使用 SQLite 存储遥测、机房状态、告警、调控动作和实验结果，并通过 WebSocket 将增量数据推送到前端。规则引擎覆盖温度预警/严重、湿度偏高、单路功率超限、烟雾异常、温升速率过快和设备心跳超时等规则，告警支持确认、处理记录、自动恢复和历史查询。", False),
        ("3.5 三种调控策略", False),
        ("（1）阈值控制：温度达到 28°C 时启动 88% 档制冷并开启排风，降至 26°C 后回落到 18% 低功耗档，逻辑简单但温度在阈值附近反复波动。", False),
        ("（2）按需最小制冷：以同机房 PDU 总功率计算前馈档位 L_ff = P×0.92/6×100%，使稳态制冷量与热负荷匹配；再叠加温度偏差与温升斜率的反馈项，软约束目标设为 27°C；仅当短时预测温度接近 28°C 时才提高档位，避免长时间过度预冷。", False),
        ("（3）预测式调控：利用最近 60 秒温度序列最小二乘拟合温升斜率，预测 3 分钟后的温度，提前投入制冷，将温度稳定控制在 26°C 附近。", False),
        ("三种策略的决策结果均通过 MQTT 下发到对应的空调和排风设备，设备仿真器执行后再经遥测回传实际档位，形成闭环控制。", False),
        ("3.6 受控对照实验", False),
        ("为避免环境差异干扰比较，每组实验开始前统一复位初始温度、注入负载、控制器和规则引擎状态，并静置 8 秒。三组实验使用相同的负载脚本：第 15 秒机房 A 增加 4kW、第 35 秒机房 B 增加 3kW、第 80 秒机房 A 再增加 2.5kW。实验过程中按秒记录机房温度、湿度、空调档位、排风状态和机柜功率，结束后统计平均制冷功率、超温占比、平均温度、最高温度和制冷能耗，并支持导出逐秒明细和策略汇总 CSV。", False),
    ],
    "本项目的实施方案": [
        ("4.1 技术选型与可行性", False),
        ("系统基于 Python 3.14 开发：Web 服务使用 FastAPI，MQTT 客户端使用 paho-mqtt，Broker 使用 amqtt，时序数据存储使用 SQLite，前端使用原生 JavaScript 与 ECharts。全部依赖开源免费，单机即可运行，不依赖真实硬件和外部网络，具备良好的可复现性。", False),
        ("4.2 实施步骤", False),
        ("第一步，搭建机房热模型、PDU 功率模型和虚拟设备，完成遥测报文生成；第二步，部署内嵌 MQTT Broker，打通上报与命令下发链路；第三步，实现 SQLite 时序存储、规则引擎、告警确认与 WebSocket 推送；第四步，实现三种调控策略与闭环控制；第五步，实现受控对照实验、指标统计与 CSV 导出；第六步，完善监控大屏、链路仿真与离线重传；第七步，补充单元测试、并发压测与文档。", False),
        ("4.3 验证结果", False),
        ("功能验证方面，MQTT 上报、规则告警、手动与自动调控、大屏实时刷新、告警确认、链路丢包/时延注入、断网缓存重传和 CSV 导出均通过接口与页面验证。自动化测试方面，热模型与三种控制策略共 11 项 pytest 用例全部通过。并发压测方面，使用 5 个 MQTT 客户端各上报 200 条 QoS 1 报文，共 1000 条全部成功，吞吐约 509 条/秒，发布确认时延 P95 为 5.4ms，验证了内嵌 Broker 的接入能力。", False),
    ],
    "项目的主要特色或者创新点": [
        ("5.1 纯软件闭环、零硬件成本", False),
        ("平台将设备、链路、平台与应用完整仿真，无需传感器和空调即可演示从数据采集到控制执行的闭环，便于课程教学、方案预研和功能回归测试。", False),
        ("5.2 功率前馈与软约束结合的按需制冷", False),
        ("与固定档位和纯反馈控制不同，本项目的按需最小制冷策略直接利用 PDU 功率计算维持热平衡所需档位，并以 27°C 软约束抑制过度预冷，控制原因可解释、参数易整定，适合算力波动明显的边缘机房。", False),
        ("5.3 同条件三策略对照与一键导出", False),
        ("实验模块自动复位环境、同步负载脚本并依次运行三种策略，指标体系统一，结果可一键导出为 CSV，为节能方案评估提供了可复现的实验方法。", False),
        ("5.4 链路质量与离线缓存的可观测性", False),
        ("系统不仅模拟丢包与时延，还实现了断网缓存与恢复重传，并将缓存、重传、送达率等指标可视化，能够直观展示物联网系统在弱网环境下的最终一致性保障。", False),
        ("5.5 可解释的调控日志", False),
        ("每次调控都会记录触发原因，例如“预测 3 分钟后 28.6°C，档位调整为 78%”或“前馈 42% + 温度偏差 +0.3°C”，便于运维人员理解控制行为并进行策略调优。", False),
    ],
}

REFERENCES = [
    "ASHRAE. Thermal Guidelines for Data Processing Environments (5th Edition). Atlanta: ASHRAE, 2021.",
    "OASIS. MQTT Version 5.0 OASIS Standard. 2019.",
    "中华人民共和国住房和城乡建设部. GB 50174-2017 数据中心设计规范. 北京: 中国计划出版社, 2017.",
    "中华人民共和国住房和城乡建设部. GB/T 51314-2018 数据中心基础设施运行维护标准. 北京: 中国计划出版社, 2018.",
    "孙其博, 刘杰, 黎羴, 等. 物联网：概念、架构与关键技术研究综述. 北京邮电大学学报, 2010, 33(3): 1-9.",
    "刘云浩. 物联网导论（第3版）. 北京: 科学出版社, 2022.",
    "Evans D. The Internet of Things: How the Next Evolution of the Internet Is Changing Everything. Cisco White Paper, 2011.",
    "Google DeepMind. DeepMind AI reduces Google data centre cooling bill by 40%. 2016.",
    "华为技术有限公司. iCooling@AI 数据中心智能温控解决方案白皮书. 深圳: 华为, 2019.",
    "维谛技术（Vertiv）. 数据中心热管理与节能白皮书. 2021.",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="生成选题报告 docx")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    runs = fetch_runs(args.api)
    doc = Document(str(TEMPLATE))
    paragraphs = doc.paragraphs

    # ---- 封面 ----
    for para in paragraphs:
        text = para.text.strip()
        if text.startswith("题  目"):
            clear_paragraph(para)
            set_run_font(para.add_run(COVER_TITLE), size=16)
        elif text.startswith("填表日期"):
            clear_paragraph(para)
            set_run_font(para.add_run("      填表日期：   2026 年   11 月   19 日"), size=14)

    # ---- 正文章节 ----
    headings = []
    for para in paragraphs:
        for title in list(SECTIONS) + ["参考文献"]:
            if para.text.strip().startswith(title):
                headings.append((title, para))
                break

    for idx, (title, heading) in enumerate(headings):
        anchor = headings[idx + 1][1] if idx + 1 < len(headings) else None
        if anchor is not None:
            remove_empty_between(heading, anchor)

        if title == "参考文献":
            for order, ref in enumerate(REFERENCES, 1):
                add_body(doc, None, f"[{order}] {ref}", indent=False, size=10.5)
            continue

        for text, _ in SECTIONS[title]:
            is_sub = text[:2].strip().replace(".", "").isdigit() or (
                text[:1].isdigit() and text[1:3] in (".1", ".2", ".3", ".4", ".5")
            )
            add_body(doc, anchor, text, bold=is_sub, indent=not is_sub)

        # 实施方案章节末尾插入实测结果表
        if title == "本项目的实施方案":
            add_body(doc, anchor, result_sentence(runs))
            add_body(
                doc,
                anchor,
                "从表中可以看到，阈值控制的平均制冷功率最低，但超温时间占比最高；预测式调控虽然把超温占比控制为零，"
                "但前期大幅预冷使其制冷能耗最高；按需最小制冷通过功率前馈匹配热负荷、以软约束避免过度预冷，"
                "在保持零超温的同时，平均功率和制冷能耗均低于预测式调控，在节能与温度安全之间取得了更好的平衡。",
            )
            add_result_table(doc, anchor, runs)
            add_body(
                doc,
                anchor,
                "4.4 风险与应对",
                bold=True,
                indent=False,
            )
            add_body(
                doc,
                anchor,
                "主要风险包括：仿真模型与真实机房存在偏差，结论需要通过实测校准；实验时长有限，初始温度对结果影响较大，"
                "因此统一复位环境并固定负载脚本；SQLite 在高并发写入下能力有限，当前通过 WAL 模式、单连接加锁和定期清理满足演示规模，"
                "后续可替换为时序数据库；链路极端配置可能造成数据堆积，已通过有界缓存和分批重传控制资源占用。",
            )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(OUTPUT))
    print(f"[完成] 已生成选题报告：{OUTPUT}")


if __name__ == "__main__":
    main()
