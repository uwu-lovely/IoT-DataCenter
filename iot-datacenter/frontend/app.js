/* 智慧机房监控大屏：REST 初始化 + WebSocket 实时增量 */

const state = {
  mode: "predictive",
  system: "sim",
  obDevice: null,
  obEditing: null,
  offlineSeconds: 20,
  thresholds: { temp_warn: 28, temp_critical: 32 },
  rooms: {},
  data: {},
  pduPower: {},
  devices: [],
  charts: {},
  ws: null,
  online: false,
  maxPoints: 420,
  alarmTab: "active",
  alarmsActive: [],
  alarmsHistory: [],
  exp: null,
  link: null,
  lastChartTs: 0,
};

const $ = (id) => document.getElementById(id);
const roomList = ["roomA", "roomB"];
const roomColors = { roomA: "#22d3ee", roomB: "#a78bfa" };
let refreshTimer = null;

/* ---------------- 基础工具 ---------------- */
function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
function hhmmss(ts) {
  const d = new Date(ts);
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, "0"))
    .join(":");
}

function push(arr, point) {
  arr.push(point);
  if (arr.length > state.maxPoints) arr.shift();
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = `${path} ${res.status}`;
    try {
      const body = await res.json();
      if (body && body.error) detail = body.error;
    } catch {
      /* 非 JSON 错误响应，保留状态码 */
    }
    throw new Error(detail);
  }
  return res.json();
}

function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

/* ---------------- 机房卡片 ---------------- */
function buildRooms() {
  const box = $("rooms");
  box.innerHTML = roomList
    .map((room) => {
      const meta = state.rooms[room] || { name: room };
      return `
      <div class="room" data-room="${room}">
        <div class="room-head">
          <h3>${esc(meta.name)}</h3>
          <span class="badge" id="${room}-badge">正常</span>
        </div>
        <div class="room-main">
          <div class="room-temp"><span id="${room}-temp">--</span><em>°C</em></div>
          <div class="room-metrics">
            <div><label>相对湿度</label><b id="${room}-hum">--</b></div>
            <div><label>机柜功率</label><b id="${room}-power">--</b></div>
            <div><label>空调档位</label><b id="${room}-ac">--</b></div>
            <div><label>排风机组</label><b id="${room}-fan">--</b></div>
          </div>
        </div>
        <div class="room-control">
          <input type="range" min="0" max="100" step="5" value="0" id="${room}-range" />
          <label class="switch"><input type="checkbox" id="${room}-fansw" /> 排风</label>
          <button class="mini" data-apply="${room}">下发</button>
        </div>
      </div>`;
    })
    .join("");

  box.querySelectorAll("[data-apply]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const room = btn.dataset.apply;
      const level = Number($(`${room}-range`).value);
      const fan = $(`${room}-fansw`).checked ? 1 : 0;
      try {
        await post("/api/control", { room, level, fan });
      } catch (err) {
        console.warn(err);
      }
    });
  });
}

function updateRoom(room, snap) {
  if (!snap || snap.temp == null) return;
  setText(`${room}-temp`, snap.temp.toFixed(1));
  setText(`${room}-hum`, snap.humidity > 0 ? `${snap.humidity.toFixed(0)}%` : "--");
  setText(`${room}-power`, `${(snap.power_w / 1000).toFixed(2)}kW`);
  setText(`${room}-ac`, `${snap.ac_level}%`);
  setText(`${room}-fan`, snap.fan ? "运行" : "停止");

  const range = $(`${room}-range`);
  if (range && document.activeElement !== range) range.value = snap.ac_level;
  const fanSw = $(`${room}-fansw`);
  if (fanSw && document.activeElement !== fanSw) fanSw.checked = !!snap.fan;

  const badge = $(`${room}-badge`);
  if (badge) {
    if (snap.temp >= state.thresholds.temp_critical) {
      badge.className = "badge crit";
      badge.textContent = "严重超温";
    } else if (snap.temp >= state.thresholds.temp_warn) {
      badge.className = "badge warn";
      badge.textContent = "温度预警";
    } else {
      badge.className = "badge";
      badge.textContent = "正常";
    }
  }
}

function markRoomStale(room) {
  setText(`${room}-temp`, "--");
  setText(`${room}-hum`, "--");
  setText(`${room}-power`, "--");
  setText(`${room}-ac`, "--");
  setText(`${room}-fan`, "--");
  const badge = $(`${room}-badge`);
  if (badge) {
    badge.className = "badge warn";
    badge.textContent = "无实时数据";
  }
}

/* ---------------- 图表 ---------------- */
function baseAxis() {
  return {
    axisLine: { lineStyle: { color: "rgba(126,150,190,.25)" } },
    axisLabel: { color: "#7f8fac", fontSize: 11 },
    splitLine: { lineStyle: { color: "rgba(126,150,190,.10)" } },
  };
}

/* 饼图空状态提示：无数据时在图表中央显示说明文字 */
function emptyHint(show, text) {
  return {
    type: "text",
    left: "center",
    top: "middle",
    silent: true,
    invisible: !show,
    style: { text, fill: "#5f6f8d", fontSize: 12 },
  };
}

function initCharts() {
  state.charts.temp = echarts.init($("tempChart"));
  state.charts.power = echarts.init($("powerChart"));
  state.charts.humidity = echarts.init($("humidityChart"));
  state.charts.pduPie = echarts.init($("pduPieChart"));
  state.charts.deviceStatus = echarts.init($("deviceStatusChart"));
  state.charts.compare = echarts.init($("compareChart"));

  state.charts.temp.setOption({
    backgroundColor: "transparent",
    grid: { left: 48, right: 22, top: 42, bottom: 30 },
    tooltip: { trigger: "axis", backgroundColor: "rgba(9,16,30,.95)", borderColor: "rgba(96,165,250,.25)", textStyle: { color: "#dbe6f7" } },
    legend: { top: 6, right: 10, textStyle: { color: "#8ea2c6" }, itemWidth: 12, itemHeight: 6 },
    xAxis: { type: "time", ...baseAxis(), splitLine: { show: false }, axisLabel: { color: "#7f8fac", formatter: (v) => hhmmss(v) } },
    yAxis: { type: "value", min: 22, ...baseAxis(), name: "°C", nameTextStyle: { color: "#7f8fac" } },
    series: roomList.map((room) => ({
      name: state.rooms[room]?.name || room,
      type: "line",
      showSymbol: false,
      smooth: true,
      lineStyle: { width: 2, color: roomColors[room] },
      itemStyle: { color: roomColors[room] },
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: room === "roomA" ? "rgba(34,211,238,.28)" : "rgba(167,139,250,.24)" },
          { offset: 1, color: "rgba(0,0,0,0)" },
        ]),
      },
      data: [],
      markLine: room === "roomA" ? {
        silent: true, symbol: "none",
        data: [
          { yAxis: state.thresholds.temp_warn, lineStyle: { color: "#fbbf24", type: "dashed", width: 1 }, label: { formatter: "预警 28°C", color: "#fbbf24", fontSize: 10 } },
          { yAxis: state.thresholds.temp_critical, lineStyle: { color: "#f87171", type: "dashed", width: 1 }, label: { formatter: "严重 32°C", color: "#f87171", fontSize: 10 } },
        ],
      } : undefined,
    })),
  });

  state.charts.power.setOption({
    backgroundColor: "transparent",
    grid: { left: 52, right: 22, top: 42, bottom: 30 },
    tooltip: { trigger: "axis", backgroundColor: "rgba(9,16,30,.95)", borderColor: "rgba(96,165,250,.25)", textStyle: { color: "#dbe6f7" } },
    legend: { top: 6, right: 10, textStyle: { color: "#8ea2c6" }, itemWidth: 12, itemHeight: 6 },
    xAxis: { type: "time", ...baseAxis(), splitLine: { show: false }, axisLabel: { color: "#7f8fac", formatter: (v) => hhmmss(v) } },
    yAxis: { type: "value", min: 0, ...baseAxis(), name: "kW", nameTextStyle: { color: "#7f8fac" } },
    series: roomList.map((room) => ({
      name: state.rooms[room]?.name || room,
      type: "line",
      showSymbol: false,
      smooth: true,
      lineStyle: { width: 2, color: roomColors[room] },
      itemStyle: { color: roomColors[room] },
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: room === "roomA" ? "rgba(34,211,238,.20)" : "rgba(167,139,250,.18)" },
          { offset: 1, color: "rgba(0,0,0,0)" },
        ]),
      },
      data: [],
    })),
  });

  state.charts.humidity.setOption({
    backgroundColor: "transparent",
    grid: { left: 48, right: 22, top: 42, bottom: 30 },
    tooltip: { trigger: "axis", backgroundColor: "rgba(9,16,30,.95)", borderColor: "rgba(96,165,250,.25)", textStyle: { color: "#dbe6f7" } },
    legend: { top: 6, right: 10, textStyle: { color: "#8ea2c6" }, itemWidth: 12, itemHeight: 6 },
    xAxis: { type: "time", ...baseAxis(), splitLine: { show: false }, axisLabel: { color: "#7f8fac", formatter: (v) => hhmmss(v) } },
    yAxis: { type: "value", min: 20, max: 100, ...baseAxis(), name: "%RH", nameTextStyle: { color: "#7f8fac" } },
    series: roomList.map((room) => ({
      name: state.rooms[room]?.name || room,
      type: "line",
      showSymbol: false,
      smooth: true,
      lineStyle: { width: 2, color: roomColors[room], type: "dashed" },
      itemStyle: { color: roomColors[room] },
      data: [],
      markLine: room === "roomA" ? {
        silent: true, symbol: "none",
        data: [
          { yAxis: state.thresholds.humidity_warn, lineStyle: { color: "#fbbf24", type: "dashed", width: 1 }, label: { formatter: "预警 70%", color: "#fbbf24", fontSize: 10 } },
        ],
      } : undefined,
    })),
  });

  state.charts.pduPie.setOption({
    backgroundColor: "transparent",
    tooltip: { trigger: "item", backgroundColor: "rgba(9,16,30,.95)", borderColor: "rgba(96,165,250,.25)", textStyle: { color: "#dbe6f7" }, formatter: "{b}: {c}W ({d}%)" },
    legend: { orient: "vertical", right: 8, top: "middle", textStyle: { color: "#8ea2c6", fontSize: 11 }, itemWidth: 10, itemHeight: 6 },
    series: [{
      type: "pie",
      radius: ["46%", "72%"],
      center: ["36%", "52%"],
      avoidLabelOverlap: true,
      itemStyle: { borderRadius: 4, borderColor: "#0d1424", borderWidth: 2 },
      label: { show: false },
      labelLine: { show: false },
      data: [],
    }],
  });

  state.charts.deviceStatus.setOption({
    backgroundColor: "transparent",
    tooltip: { trigger: "item", backgroundColor: "rgba(9,16,30,.95)", borderColor: "rgba(96,165,250,.25)", textStyle: { color: "#dbe6f7" }, formatter: "{b}: {c} 台 ({d}%)" },
    legend: { orient: "vertical", right: 8, top: "middle", textStyle: { color: "#8ea2c6", fontSize: 11 }, itemWidth: 10, itemHeight: 6 },
    series: [{
      type: "pie",
      radius: ["46%", "72%"],
      center: ["36%", "52%"],
      avoidLabelOverlap: true,
      itemStyle: { borderRadius: 4, borderColor: "#0d1424", borderWidth: 2 },
      label: { show: false },
      labelLine: { show: false },
      data: [
        { value: 0, name: "在线", itemStyle: { color: "#34d399" } },
        { value: 0, name: "离线", itemStyle: { color: "#f87171" } },
        { value: 0, name: "暂停", itemStyle: { color: "#fbbf24" } },
      ],
    }],
  });

  state.charts.compare.setOption({
    backgroundColor: "transparent",
    grid: { left: 42, right: 16, top: 24, bottom: 26 },
    tooltip: { trigger: "axis" },
    xAxis: { type: "category", data: ["阈值控制", "按需制冷", "预测式"], ...baseAxis(), splitLine: { show: false } },
    yAxis: { type: "value", ...baseAxis(), name: "kW", nameTextStyle: { color: "#7f8fac" } },
    series: [{
      type: "bar",
      barWidth: 30,
      data: [
        { value: 0, itemStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: "#a78bfa" }, { offset: 1, color: "rgba(167,139,250,.25)" }]), borderRadius: [6, 6, 0, 0] } },
        { value: 0, itemStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: "#fbbf24" }, { offset: 1, color: "rgba(251,191,36,.25)" }]), borderRadius: [6, 6, 0, 0] } },
        { value: 0, itemStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: "#22d3ee" }, { offset: 1, color: "rgba(34,211,238,.25)" }]), borderRadius: [6, 6, 0, 0] } },
      ],
      label: { show: true, position: "top", color: "#cfe4ff", formatter: "{c} kW" },
    }],
  });

  window.addEventListener("resize", () => Object.values(state.charts).forEach((c) => c.resize()));
}

function updateCharts() {
  state.charts.temp.setOption({
    series: roomList.map((room) => ({ data: state.data[room].temp })),
  });
  state.charts.power.setOption({
    series: roomList.map((room) => ({ data: state.data[room].power })),
  });
  state.charts.humidity.setOption({
    series: roomList.map((room) => ({ data: state.data[room].humidity })),
  });
  // PDU 功率占比饼图
  const pduData = [];
  for (const room of roomList) {
    for (const [dev, val] of Object.entries(state.pduPower[room] || {})) {
      pduData.push({ name: dev, value: Math.round(val) });
    }
  }
  const pduEmpty = !pduData.length || pduData.every((item) => !item.value);
  state.charts.pduPie.setOption({
    series: [{ data: pduData }],
    graphic: [emptyHint(pduEmpty, "暂无远传功率数据")],
  });
  // 设备状态分布：台账接口返回 online(0/1) 与 paused(0/1)
  // 实际监测模式下必须有过上报且在心跳阈值内才算在线，避免 waiting 设备被算作在线
  let online = 0, offline = 0, paused = 0;
  const now = Date.now() / 1000;
  for (const d of state.devices) {
    const fresh = !!d.last_seen && now - d.last_seen <= state.offlineSeconds;
    let isOnline;
    if (d.status) isOnline = d.status === "online";
    else if (state.system === "real") isOnline = !!d.online && fresh;
    else isOnline = !!d.online;
    if (d.paused) paused++;
    else if (isOnline) online++;
    else offline++;
  }
  state.charts.deviceStatus.setOption({
    series: [{ data: [
      { value: online, name: "在线", itemStyle: { color: "#34d399" } },
      { value: offline, name: "离线", itemStyle: { color: "#f87171" } },
      { value: paused, name: "暂停", itemStyle: { color: "#fbbf24" } },
    ] }],
    graphic: [emptyHint(!state.devices.length, "暂无接入设备")],
  });
}

/* ---------------- 历史回填 ---------------- */
async function loadHistory() {
  // 六个历史请求并行发出，避免逐机房串行等待
  const requests = [];
  for (const room of roomList) {
    for (const metric of ["temperature", "power", "humidity"]) {
      requests.push(
        api(`/api/history?room=${room}&metric=${metric}&minutes=10`).catch((err) => {
          console.warn("history load failed", metric, room, err);
          return null;
        })
      );
    }
  }
  const [tempA, powerA, humA, tempB, powerB, humB] = await Promise.all(requests);
  applyHistory("roomA", { temperature: tempA, power: powerA, humidity: humA });
  applyHistory("roomB", { temperature: tempB, power: powerB, humidity: humB });
  updateCharts();
}

function applyHistory(room, data) {
  if (!data.temperature || !data.power || !data.humidity) return;
  if (!state.data[room]) state.data[room] = { temp: [], power: [], humidity: [] };

  // 温湿度按时间点对机房内多路传感器取平均，与顶部机房卡片的聚合口径保持一致
  state.data[room].temp = averageSeries(data.temperature.series);
  state.data[room].humidity = averageSeries(data.humidity.series);

  // 功率按时间点累加同一机房的所有 PDU
  const totals = new Map();
  data.power.series.forEach((s) =>
    s.points.forEach(([ts, v]) => {
      const key = Math.round(ts);
      totals.set(key, (totals.get(key) || 0) + v);
    })
  );
  state.data[room].power = [...totals.entries()]
    .sort((x, y) => x[0] - y[0])
    .slice(-state.maxPoints)
    .map(([ts, v]) => [ts * 1000, +(v / 1000).toFixed(2)]);
}

/* 把同一机房多路传感器的历史点按秒对齐后取平均 */
function averageSeries(series) {
  const buckets = new Map();
  (series || []).forEach((s) =>
    s.points.forEach(([ts, v]) => {
      const key = Math.round(ts);
      const bucket = buckets.get(key) || { sum: 0, count: 0 };
      bucket.sum += v;
      bucket.count += 1;
      buckets.set(key, bucket);
    })
  );
  return [...buckets.entries()]
    .sort((x, y) => x[0] - y[0])
    .slice(-state.maxPoints)
    .map(([ts, bucket]) => [ts * 1000, +(bucket.sum / bucket.count).toFixed(2)]);
}

/* ---------------- 渲染列表 ---------------- */
function renderAlarmPanel() {
  const box = $("alarmList");
  const list = state.alarmTab === "active" ? state.alarmsActive : state.alarmsHistory;
  const statusMeta = {
    open: ["warning", "未确认"],
    acked: ["info", "已确认"],
    resolved: ["resolved", "已恢复"],
  };

  if (!list || !list.length) {
    box.innerHTML = `<li class="empty">${state.alarmTab === "active" ? "当前无活动告警，系统运行正常" : "暂无历史告警记录"}</li>`;
  } else {
    box.innerHTML = list.map((a) => {
      const [cls, txt] = statusMeta[a.status] || ["info", a.status];
      const note = a.note
        ? `<div class="li-note">处理记录：${esc(a.acked_by || "")} · ${esc(a.note)}</div>`
        : "";
      const ackBtn = a.status === "open" ? `<button class="ack" data-ack="${Number(a.id)}">确认</button>` : "";
      return `
      <li class="clickable" data-room="${esc(a.room)}" title="点击定位到对应机房">
        <span class="li-time">${hhmmss(a.ts * 1000)}</span>
        <div class="li-main">
          <div class="li-title">${esc(a.device)} · ${esc(a.message)}</div>
          <div class="li-sub">${esc(state.rooms[a.room]?.name || a.room)} · 规则 ${esc(a.rule)}${a.acked_ts ? ` · 确认于 ${hhmmss(a.acked_ts * 1000)}` : ""}</div>
          ${note}
        </div>
        <span class="tag ${a.level}">${a.level === "critical" ? "严重" : "预警"}</span>
        <span class="tag ${cls}">${txt}</span>
        ${ackBtn}
      </li>`;
    }).join("");
  }

  const active = state.alarmsActive || [];
  const crit = active.filter((a) => a.level === "critical").length;
  const unacked = active.filter((a) => a.status === "open").length;
  setText("kpiAlarms", String(active.length));
  setText("kpiAlarmFoot", `严重 ${crit} · 预警 ${active.length - crit} · 未确认 ${unacked}`);
}

/* ---------------- 对照实验 ---------------- */
function renderExperiment(exp) {
  if (!exp) return;
  state.exp = exp;

  const phaseLabel = {
    idle: "未运行",
    preparing: "准备中",
    threshold: "第一组 · 阈值控制",
    adaptive: "第二组 · 按需最小制冷",
    predictive: "第三组 · 预测式调控",
    done: "实验完成",
    cancelled: "实验已取消",
  };
  const expOrder = ["threshold", "adaptive", "predictive"];
  const phaseEl = $("expPhase");
  const running = !!exp.running;
  phaseEl.textContent = phaseLabel[exp.phase] || exp.phase;
  phaseEl.className = "exp-phase" + (running ? " running" : exp.phase === "done" ? " done" : "");

  const duration = exp.duration || 180;
  const elapsed = Math.min(duration, exp.elapsed || 0);
  let pct = 0;
  if (running) {
    const idx = Math.max(0, expOrder.indexOf(exp.phase));
    pct = ((idx * duration + elapsed) / (duration * expOrder.length)) * 100;
  } else if (exp.phase === "done") pct = 100;
  $("expBar").style.width = `${Math.min(100, pct).toFixed(1)}%`;

  if (running) {
    $("expMeta").textContent = `${exp.message || "运行中"} · 本组剩余 ${Math.max(0, duration - elapsed).toFixed(0)}s`;
  } else if (exp.phase === "done") {
    $("expMeta").textContent = "三组实验已完成，结果如下，可导出明细/汇总 CSV";
  } else if (exp.phase === "cancelled") {
    $("expMeta").textContent = "实验已取消，当前数据可能不完整";
  } else {
    $("expMeta").textContent = "依次运行「阈值控制 → 按需最小制冷 → 预测式调控」，自动注入相同负载";
  }

  const runs = exp.runs || [];
  $("expRuns").innerHTML = runs.length
    ? runs.map((r) => `
      <tr>
        <td>${r.mode_cn || { threshold: "阈值控制", adaptive: "按需最小制冷", predictive: "预测式调控" }[r.mode] || r.mode}</td>
        <td>${Number(r.avg_kw || 0).toFixed(2)} kW</td>
        <td>${Number(r.over_ratio || 0).toFixed(1)}%</td>
        <td>${Number(r.avg_temp || 0).toFixed(1)}°C</td>
        <td>${Number(r.max_temp || 0).toFixed(1)}°C</td>
        <td>${Number(r.kwh || 0).toFixed(3)} kWh</td>
      </tr>`).join("")
    : '<tr><td colspan="6" class="empty">暂无实验结果</td></tr>';

  $("expStart").classList.toggle("disabled", running);
  $("expCancel").classList.toggle("disabled", !running);
  $("expCsvRaw").classList.toggle("disabled", !runs.length);
  $("expCsvSum").classList.toggle("disabled", !runs.length);
}

/* ---------------- 链路质量 ---------------- */
function renderLink(stats) {
  if (!stats) return;
  state.link = stats;
  setText("linkSent", stats.sent ?? 0);
  setText("linkDropped", stats.dropped ?? 0);
  setText("linkRatio", `${Number(stats.delivery_ratio ?? 100).toFixed(1)}%`);
  setText("linkRecv", stats.received ?? 0);
  setText("linkBuffer", stats.buffered ?? 0);
  setText("linkReplay", stats.replayed ?? 0);

  if (stats.offline) {
    $("linkNote").textContent =
      `链路中断：设备侧已缓存 ${stats.buffered ?? 0} 条报文，恢复后自动按序重传（QoS 1）`;
    return;
  }

  const parts = [];
  if (stats.loss_rate > 0) parts.push(`丢包率 ${(stats.loss_rate * 100).toFixed(0)}%`);
  if (stats.delay_ms > 0) parts.push(`附加时延 ${stats.delay_ms}ms`);
  if (stats.jitter_ms > 0) parts.push(`抖动 ±${stats.jitter_ms}ms`);
  const replay = stats.replayed > 0 ? ` · 已重传 ${stats.replayed} 条` : "";
  $("linkNote").textContent = parts.length
    ? `当前链路：${parts.join(" · ")}（QoS 1，上行遥测）${replay}`
    : `当前链路正常，无丢包与时延注入（QoS 1，上行遥测）${replay}`;
}

function renderActions(list) {
  const box = $("actionList");
  if (!list || !list.length) {
    box.innerHTML = '<li class="empty">暂无调控动作，等待环境变化触发</li>';
    return;
  }
  box.innerHTML = list
    .map((a) => `
      <li>
        <span class="li-time">${hhmmss(a.ts * 1000)}</span>
        <div class="li-main">
          <div class="li-title"><span class="action-text">${esc(a.action)}</span></div>
          <div class="reason">${esc(a.reason)}</div>
        </div>
        <span class="tag ${a.mode === "manual" ? "info" : "resolved"}">${a.mode === "manual" ? "手动" : a.mode === "predictive" ? "预测式" : a.mode === "adaptive" ? "按需" : "阈值"}</span>
      </li>`)
    .join("");
}

function renderMetrics(metrics) {
  const byMode = {};
  (metrics || []).forEach((m) => { byMode[m.mode] = m; });
  const thr = byMode.threshold || {};
  const adp = byMode.adaptive || {};
  const pred = byMode.predictive || {};

  const rows = [
    ["平均制冷功率 (kW)", thr.avg_kw ?? 0, adp.avg_kw ?? 0, pred.avg_kw ?? 0],
    ["超温时间占比 (%)", thr.over_ratio ?? 0, adp.over_ratio ?? 0, pred.over_ratio ?? 0],
    ["平均温度 (°C)", thr.avg_temp ?? 0, adp.avg_temp ?? 0, pred.avg_temp ?? 0],
    ["最高温度 (°C)", thr.max_temp ?? 0, adp.max_temp ?? 0, pred.max_temp ?? 0],
    ["统计时长 (min)", thr.minutes ?? 0, adp.minutes ?? 0, pred.minutes ?? 0],
  ];
  $("compareBody").innerHTML = rows
    .map(([name, a, b, c]) =>
      `<tr><td>${name}</td><td class="thr">${a}</td><td class="adapt">${b}</td><td class="pred">${c}</td></tr>`)
    .join("");

  state.charts.compare.setOption({
    series: [{
      data: [
        { value: thr.avg_kw ?? 0 },
        { value: adp.avg_kw ?? 0 },
        { value: pred.avg_kw ?? 0 },
      ],
    }],
  });
}

function renderDevices(devices) {
  const now = Date.now() / 1000;
  const typeCn = { env: "环境", power: "电力", smoke: "烟雾", water: "漏水", door: "门禁", ups: "UPS", airflow: "风速", actuator: "执行器" };
  $("deviceBody").innerHTML = (devices || [])
    .map((d) => {
      const online = d.online && !d.paused;
      const ago = d.last_seen ? Math.max(0, Math.round(now - d.last_seen)) : null;
      const waiting = state.system === "real" && !d.last_seen;
      const statusClass = waiting ? "waiting" : online ? "online" : "offline";
      return `<tr class="${online ? "" : "off"}">
        <td>${esc(d.device)}</td>
        <td>${esc(d.name)}</td>
        <td>${esc(state.rooms[d.room]?.name || d.room)}</td>
        <td>${esc(typeCn[d.type] || d.type)}</td>
        <td class="${statusClass}">${d.paused ? "暂停上报" : waiting ? "等待上报" : online ? "在线" : "离线"}</td>
        <td>${ago === null ? "—" : ago + "s 前"}</td>
      </tr>`;
    })
    .join("");
}

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll("#modeSwitch button").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === mode);
  });
}

/* 全量刷新频率：实际监测依赖轮询补充设备台账与 PDU 功率，仿真模式可降频 */
function scheduleRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  const interval = state.system === "real" ? 10000 : 30000;
  refreshTimer = setInterval(() => { refresh().catch(() => {}); }, interval);
}

/* ---------------- 运行模式：仿真 / 实际监测 ---------------- */
function renderSystem(sys) {
  if (!sys) return;
  const mode = typeof sys === "string" ? sys : sys.mode;
  if (mode !== "sim" && mode !== "real") return;
  const changed = state.system !== mode;
  state.system = mode;
  const real = mode === "real";
  if (sys.offline_seconds) state.offlineSeconds = sys.offline_seconds;

  document.querySelectorAll("#sourceSwitch button").forEach((b) => {
    b.classList.toggle("active", b.dataset.source === mode);
  });
  document.body.classList.toggle("mode-real", real);
  if (changed) scheduleRefresh();
  const banner = $("realBanner");
  if (banner) banner.hidden = !real;
  // 演示注入、链路注入、对照实验与自动调控仅在仿真模式可用
  document.querySelectorAll("[data-demo], [data-link], #expStart, #modeSwitch button").forEach((el) => {
    el.classList.toggle("disabled", real);
  });
  if (sys.offline_seconds) {
    state.offlineSeconds = sys.offline_seconds;
    $("offlineSeconds").value = sys.offline_seconds;
  }
  setText("deviceLedgerSub", `心跳超时 ${state.offlineSeconds}s 判定离线`);
  setText(
    "onboardSub",
    `填写编号即可生成上报主题；超过 ${state.offlineSeconds}s 未上报判定离线`
  );
  if (changed) resetOnboard();
  if (real) {
    setText("expPhase", "实际监测模式下不可用");
    setText("expMeta", "对照实验与链路注入仅用于仿真模式");
    loadRealDevices().catch(() => {});
  }
}

function resetLocalData() {
  roomList.forEach((room) => {
    state.data[room] = { temp: [], power: [], humidity: [] };
  });
  state.alarmsActive = [];
  state.alarmsHistory = [];
  state.pduPower = {};
  state.exp = null;
  renderAlarmPanel();
  renderActions([]);
  if (state.charts.temp) updateCharts();
}

/* ---------------- 实际监测：设备接入向导 ---------------- */
function setObMsg(text, error) {
  const el = $("obMsg");
  el.textContent = text || "";
  el.classList.toggle("error", !!error);
}

function clearObResult() {
  $("obResult").hidden = true;
  $("obTopic").value = "";
  $("obPayload").value = "";
  $("obHint").textContent = "";
  state.obDevice = null;
}

/* 退出编辑或切换模式时复位接入表单 */
function resetOnboard() {
  state.obEditing = null;
  $("obDevice").disabled = false;
  $("obRoom").disabled = false;
  $("obDevice").value = "";
  $("obName").value = "";
  $("obType").value = "";
  $("obGateway").value = "mqtt";
  $("obRoom").value = "roomA";
  $("obSubmit").textContent = "接入并检测";
  $("obCancel").hidden = true;
  clearObResult();
  setObMsg("");
}

async function saveTopic() {
  if (!state.obDevice) return;
  const topic = $("obTopic").value.trim();
  try {
    const res = await post("/api/real/devices/topic", {
      device: state.obDevice,
      topic,
    });
    $("obTopic").value = res.topic;
    setObMsg("上报主题已更新");
    await loadRealDevices();
  } catch (err) {
    setObMsg(err.message || "主题保存失败", true);
  }
}

async function submitDevice() {
  const device = $("obDevice").value.trim();
  const room = $("obRoom").value;
  const type = $("obType").value;
  const name = $("obName").value.trim();
  if (!device) {
    setObMsg("请输入设备编号，例如 TH-101", true);
    return;
  }
  const editing = !!state.obEditing;
  setObMsg(editing ? "保存中…" : "校验中…");
  try {
    const body = { room, device, type, name, gateway: $("obGateway").value };
    // 编辑时把当前主题一并保存，避免名称/类型与主题分两步修改
    if (editing) body.topic = $("obTopic").value.trim();
    const res = await post("/api/real/devices", body);
    if (editing) {
      const warn = res.type_mismatch_warning;
      resetOnboard();
      setObMsg(`设备 ${device} 信息已保存${warn ? "（" + warn + "）" : ""}`);
      await loadRealDevices();
      await refresh();
      return;
    }
    state.obDevice = res.device.device;
    $("obTopic").value = res.topic;
    $("obPayload").value = JSON.stringify(res.payload, null, 2);
    $("obResult").hidden = false;
    const warn = res.type_mismatch_warning;
    $("obHint").textContent =
      (warn ? warn + "。" : "") +
      `已登记，等待设备上报：${(res.expected || []).join(" / ")}。` +
      "主题与报文可直接修改；清空主题后保存可恢复默认，测试会按当前报文发送。" +
      "测试报文只验证链路与格式，不写入历史/统计，也不触发告警（ts/room/device 由平台填充）。";
    setObMsg("接入成功，等待数据上报");
    await loadRealDevices();
    await refresh();
  } catch (err) {
    setObMsg(err.message || (editing ? "保存失败" : "接入失败"), true);
  }
}

async function loadRealDevices() {
  if (state.system !== "real") return;
  const data = await api("/api/real/devices");
  renderRealDevices(data.devices || []);
}

function renderRealDevices(list) {
  const box = $("realDeviceBody");
  if (!box) return;
  const statusMeta = {
    waiting: ["waiting", "等待上报"],
    online: ["online", "在线"],
    offline: ["offline", "离线"],
    test: ["test", "测试数据"],
  };
  if (!list.length) {
    box.innerHTML = '<tr><td colspan="8" class="empty">暂无接入设备</td></tr>';
    return;
  }
  box.innerHTML = list
    .map((d) => {
      const [cls, txt] = statusMeta[d.status] || ["waiting", d.status];
      const metrics = d.metrics.length ? d.metrics.map(esc).join(" / ") : "—";
      const missing = d.missing.length
        ? `<span class="miss">缺少 ${d.missing.map(esc).join(" / ")}</span>`
        : "";
      const testMetrics = (d.test_metrics || []).length
        ? `<span class="test-note">测试收到 ${d.test_metrics.map(esc).join(" / ")}</span>`
        : "";
      const ago = d.last_seen
        ? Math.max(0, Math.round(Date.now() / 1000 - d.last_seen))
        : null;
      const statusText =
        d.status === "online" && ago !== null ? `在线 · ${ago}s 前` : txt;
      return `<tr>
        <td>${esc(d.device)}</td>
        <td>${esc(d.name)}</td>
        <td>${esc(d.room_name)}</td>
        <td>${esc(d.type_cn)}</td>
        <td>${esc(d.gateway_cn || d.gateway || "直连 MQTT")}</td>
        <td>${metrics}${missing}${testMetrics}</td>
        <td class="${cls}">${esc(statusText)}</td>
        <td class="row-actions">
          <button class="mini" data-edit="${esc(d.device)}">编辑</button>
          <button class="mini" data-test="${esc(d.device)}">测试</button>
          <button class="mini danger" data-remove="${esc(d.device)}">移除</button>
        </td>
      </tr>`;
    })
    .join("");
}

/* 点击「编辑」：拉取设备当前配置并回填接入表单，进入编辑态 */
async function editDevice(device) {
  try {
    const tpl = await api(`/api/real/devices/${encodeURIComponent(device)}/template`);
    state.obEditing = device;
    $("obDevice").value = tpl.device;
    $("obDevice").disabled = true;
    $("obRoom").value = tpl.room;
    $("obName").value = tpl.name;
    $("obType").value = tpl.type;
    $("obGateway").value = tpl.gateway || "mqtt";
    $("obTopic").value = tpl.topic;
    $("obPayload").value = JSON.stringify(tpl.payload, null, 2);
    $("obResult").hidden = false;
    $("obHint").textContent =
      `编辑模式：可修改名称、机房、类型与上报主题，保存后生效。` +
      `测试报文只验证链路与格式，不写入历史/统计，也不触发告警。`;
    $("obSubmit").textContent = "保存修改";
    $("obCancel").hidden = false;
    setObMsg(`正在编辑设备 ${device}`);
  } catch (err) {
    setObMsg(err.message || "加载设备信息失败", true);
  }
}

/* ---------------- 刷新 ---------------- */
async function refresh() {
  const data = await api("/api/bootstrap");
  state.rooms = {};
  (data.rooms || []).forEach((r) => {
    state.rooms[r.room] = { name: r.name };
    if (!state.data[r.room]) state.data[r.room] = { temp: [], power: [], humidity: [] };
    if (r.state && r.state.temp !== undefined) updateRoom(r.room, r.state);
  });
  state.thresholds = data.thresholds || state.thresholds;
  setMode(data.mode);
  state.alarmsActive = data.alarms || [];
  state.alarmsHistory = data.alarm_history || [];
  // 收集每台 PDU 的最新功率，用于饼图（仿真模式另有 tick 读数实时覆盖）
  const pduLatest = (data.device_latest || []).filter((r) => r.metric === "power");
  state.pduPower = {};
  pduLatest.forEach((r) => {
    if (!state.pduPower[r.room]) state.pduPower[r.room] = {};
    state.pduPower[r.room][r.device] = r.value;
  });
  renderAlarmPanel();
  renderActions(data.actions);
  renderMetrics(data.metrics);
  renderDevices(data.devices);
  state.devices = data.devices || [];
  renderExperiment(data.experiment);
  renderLink(data.link);
  renderSystem(data.system);
  // 设备台账与 PDU 最新功率在此刷新，同步更新饼图
  updateCharts();
}

/* ---------------- WebSocket ---------------- */
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;

  ws.onopen = () => {
    state.online = true;
    $("connDot").className = "dot online";
    setText("connText", "实时链路已连接");
  };

  ws.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }

    if (msg.type === "hello" && msg.system) {
      renderSystem({ mode: msg.system });
      return;
    }

    if (msg.type === "tick") {
      const ts = msg.ts * 1000;
      const rooms = msg.rooms || {};
      Object.entries(rooms).forEach(([room, snap]) => {
        if (!snap || snap.temp == null) return;
        if (!state.data[room]) state.data[room] = { temp: [], power: [], humidity: [] };
        push(state.data[room].temp, [ts, snap.temp]);
        push(state.data[room].power, [ts, +(snap.power_w / 1000).toFixed(2)]);
        // 无湿度传感器的机房不追加 0 值，避免曲线被拉平
        if (snap.humidity > 0) push(state.data[room].humidity, [ts, snap.humidity]);
        updateRoom(room, snap);
      });
      // 仿真 tick 直接携带各设备读数，实时刷新 PDU 功率占比
      if (msg.readings && msg.readings.length) {
        const pdu = {};
        msg.readings.forEach((r) => {
          const value = r.metrics && r.metrics.power;
          if (value == null) return;
          (pdu[r.room] = pdu[r.room] || {})[r.device] = value;
        });
        if (Object.keys(pdu).length) state.pduPower = pdu;
      }
      // 实际监测下，没有新鲜遥测的机房标记为无数据，避免一直显示旧值
      if (state.system === "real") {
        roomList.forEach((room) => {
          if (!rooms[room]) markRoomStale(room);
        });
        if (!rooms.roomA) {
          setText("kpiTempA", "--");
          setText("kpiTempAFoot", "无实时数据");
        }
        if (!rooms.roomB) {
          setText("kpiTempB", "--");
          setText("kpiTempBFoot", "无实时数据");
        }
        if (!rooms.roomA || !rooms.roomB) setText("kpiPower", "--");
      }
      const a = rooms.roomA;
      const b = rooms.roomB;
      if (a) {
        setText("kpiTempA", a.temp.toFixed(1));
        $("kpiTempA").closest(".kpi").className = "kpi" + (a.temp >= state.thresholds.temp_critical ? " crit" : a.temp >= state.thresholds.temp_warn ? " warn" : "");
        setText("kpiTempAFoot", `空调 ${a.ac_level}% · 湿度 ${a.humidity.toFixed(0)}%`);
      }
      if (b) {
        setText("kpiTempB", b.temp.toFixed(1));
        $("kpiTempB").closest(".kpi").className = "kpi" + (b.temp >= state.thresholds.temp_critical ? " crit" : b.temp >= state.thresholds.temp_warn ? " warn" : "");
        setText("kpiTempBFoot", `空调 ${b.ac_level}% · 湿度 ${b.humidity.toFixed(0)}%`);
      }
      if (a && b) {
        const kw = (a.power_w + b.power_w) / 1000;
        setText("kpiPower", kw.toFixed(2));
      }
      // 图表重绘做 2 秒节流：KPI 与机房卡片仍按 1 秒刷新
      if (!state.lastChartTs || ts - state.lastChartTs >= 2000) {
        state.lastChartTs = ts;
        updateCharts();
      }
      if (msg.link) renderLink(msg.link);
    } else if (msg.type === "experiment") {
      renderExperiment(msg.state);
    } else if (msg.type === "link") {
      renderLink(msg.stats);
    } else if (msg.type === "alarm") {
      refresh().catch(() => {});
    } else if (msg.type === "resolve" || msg.type === "action" || msg.type === "notice") {
      refresh().catch(() => {});
    } else if (msg.type === "mode") {
      setMode(msg.mode);
    } else if (msg.type === "system") {
      renderSystem({ mode: msg.mode });
      resetLocalData();
      refresh().catch(() => {});
      loadHistory().catch(() => {});
    } else if (msg.type === "device") {
      loadRealDevices().catch(() => {});
      refresh().catch(() => {});
    }
  };

  ws.onclose = () => {
    state.online = false;
    $("connDot").className = "dot offline";
    setText("connText", "链路断开，重连中…");
    setTimeout(connectWS, 2000);
  };
  ws.onerror = () => ws.close();
}

/* ---------------- 事件绑定 ---------------- */
function bindEvents() {
  // 真实设备接入：校验、生成主题、测试与移除
  $("obSubmit").addEventListener("click", submitDevice);
  $("obSaveTopic").addEventListener("click", saveTopic);
  $("obDevice").addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitDevice();
  });
  $("realDeviceBody").addEventListener("click", async (e) => {
    const editBtn = e.target.closest("[data-edit]");
    if (editBtn) {
      await editDevice(editBtn.dataset.edit);
      return;
    }
    const testBtn = e.target.closest("[data-test]");
    if (testBtn) {
      const device = testBtn.dataset.test;
      let body = null;
      if (state.obDevice === device) {
        try {
          body = JSON.parse($("obPayload").value);
        } catch {
          setObMsg("示例报文不是合法 JSON，请修正后再测试", true);
          return;
        }
      }
      try {
        const res = await post("/api/real/devices/test", { device, payload: body });
        setObMsg(
          `已按${body ? "当前编辑的" : "默认"}报文发送，主题：${res.topic}`
        );
        setTimeout(() => loadRealDevices().catch(() => {}), 800);
        setTimeout(() => refresh().catch(() => {}), 1200);
      } catch (err) {
        alert(`测试失败：${err.message}`);
      }
      return;
    }
    const removeBtn = e.target.closest("[data-remove]");
    if (removeBtn) {
      const device = removeBtn.dataset.remove;
      if (!confirm(`移除设备 ${device}？将同时删除其遥测与告警。`)) return;
      try {
        await api(`/api/real/devices/${encodeURIComponent(device)}`, { method: "DELETE" });
        if (device === state.obDevice) clearObResult();
        setObMsg(`已移除设备 ${device}`);
        await loadRealDevices();
        await refresh();
      } catch (err) {
        alert(`移除失败：${err.message}`);
      }
    }
  });

  // 编辑态下点击「取消编辑」复位表单
  $("obCancel").addEventListener("click", resetOnboard);

  // 离线判定阈值：运行时可调整，保存后立即重新评估在线状态
  $("offlineSave").addEventListener("click", async () => {
    const seconds = Number($("offlineSeconds").value);
    if (!seconds || seconds < 5 || seconds > 86400) {
      alert("阈值需在 5 ~ 86400 秒之间");
      return;
    }
    try {
      const res = await post("/api/system/offline-seconds", { seconds });
      state.offlineSeconds = res.offline_seconds;
      setText("deviceLedgerSub", `心跳超时 ${state.offlineSeconds}s 判定离线`);
      setText(
        "onboardSub",
        `填写编号即可生成上报主题；超过 ${state.offlineSeconds}s 未上报判定离线`
      );
      $("offlineHint").textContent = "已保存，阈值变更后设备在线状态会立即重新评估";
      setTimeout(() => { $("offlineHint").textContent = "真实设备采样 30~60s 时建议设为 90~120s"; }, 3000);
      await refresh();
    } catch (err) {
      alert(`保存失败：${err.message}`);
    }
  });
  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const el = $(btn.dataset.copy);
      const text = el && "value" in el ? el.value : el.textContent;
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = "已复制";
        setTimeout(() => { btn.textContent = "复制"; }, 1200);
      } catch {
        /* 剪贴板不可用时忽略 */
      }
    });
  });

  // 仿真 / 实际监测模式切换
  document.querySelectorAll("#sourceSwitch button").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const target = btn.dataset.source;
      if (target === state.system) return;
      const label = target === "real" ? "实际监测模式" : "仿真模式";
      if (!confirm(`切换到${label}会清空当前遥测、告警与实验数据，确定继续？`)) return;
      try {
        const res = await post("/api/system/mode", { mode: target });
        renderSystem(res.system || { mode: target });
        resetLocalData();
        clearObResult();
        setObMsg("");
        await refresh();
        await loadHistory();
      } catch (err) {
        alert(`切换失败：${err.message}`);
      }
    });
  });

  document.querySelectorAll("#modeSwitch button").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await post("/api/mode", { mode: btn.dataset.mode });
        setMode(btn.dataset.mode);
      } catch (err) { console.warn(err); }
    });
  });

  document.querySelectorAll("[data-demo]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const kind = btn.dataset.demo;
      const room = btn.dataset.room || "roomA";
      const device = btn.dataset.device;
      try {
        if (kind === "load") await post("/api/demo/load", { room, delta_w: 4000 });
        if (kind === "smoke") await post("/api/demo/smoke", { room, level: 0.65 });
        if (kind === "smoke-off") await post("/api/demo/smoke", { room, level: 0 });
        if (kind === "water") await post("/api/demo/water-leak", { room, level: 0.8 });
        if (kind === "water-off") await post("/api/demo/water-leak", { room, level: 0 });
        if (kind === "door") await post("/api/demo/door", { room, open: true });
        if (kind === "door-off") await post("/api/demo/door", { room, open: false });
        if (kind === "ac-fault") await post("/api/demo/ac-fault", { room, fault: true });
        if (kind === "ac-fault-off") await post("/api/demo/ac-fault", { room, fault: false });
        if (kind === "outdoor") await post("/api/demo/outdoor-spike", { room, delta: 5 });
        if (kind === "humidity") await post("/api/demo/humidity-spike", { room, delta: 20 });
        if (kind === "voltage") await post("/api/demo/voltage-fluct", { room, amplitude: 25 });
        if (kind === "fault") await post("/api/demo/fault", { device, paused: true });
        if (kind === "fault-off") await post("/api/demo/fault", { device, paused: false });
      } catch (err) { console.warn(err); }
    });
  });

  // 对照实验
  $("expStart").addEventListener("click", async () => {
    try {
      await post("/api/experiment/start", { duration: 180 });
      refresh().catch(() => {});
    } catch (err) { console.warn(err); }
  });
  $("expCancel").addEventListener("click", async () => {
    try { await post("/api/experiment/cancel", {}); } catch (err) { console.warn(err); }
  });

  // 链路质量
  const linkPresets = {
    normal: { loss_rate: 0, delay_ms: 0, jitter_ms: 0 },
    loss10: { loss_rate: 0.1, delay_ms: 0, jitter_ms: 0 },
    delay: { loss_rate: 0, delay_ms: 300, jitter_ms: 0 },
    bad: { loss_rate: 0.2, delay_ms: 800, jitter_ms: 300 },
    offline: { loss_rate: 1.0, delay_ms: 0, jitter_ms: 0 },
  };
  document.querySelectorAll("[data-link]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const cfg = linkPresets[btn.dataset.link];
      if (!cfg) return;
      try {
        const stats = await post("/api/link", { ...cfg, reset: true });
        renderLink(stats);
        document.querySelectorAll("[data-link]").forEach((b) => b.classList.toggle("active", b === btn));
      } catch (err) { console.warn(err); }
    });
  });

  // 告警 Tab 与确认
  $("alarmTabs").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-tab]");
    if (!btn) return;
    state.alarmTab = btn.dataset.tab;
    document.querySelectorAll("#alarmTabs button").forEach((b) => b.classList.toggle("active", b === btn));
    renderAlarmPanel();
  });
  $("alarmList").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-ack]");
    if (btn) {
      try {
        await post("/api/alarms/ack", { id: Number(btn.dataset.ack), operator: "值班员", note: "已现场确认" });
        await refresh();
      } catch (err) { console.warn(err); }
      return;
    }
    // 点击告警条目定位到对应机房卡片并高亮
    const item = e.target.closest("li[data-room]");
    if (!item) return;
    const roomEl = document.querySelector(`.room[data-room="${item.dataset.room}"]`);
    if (!roomEl) return;
    roomEl.scrollIntoView({ behavior: "smooth", block: "center" });
    roomEl.classList.add("flash");
    setTimeout(() => roomEl.classList.remove("flash"), 1600);
  });
}

/* ---------------- 启动 ---------------- */
async function main() {
  $("clock").textContent = hhmmss(Date.now());
  setInterval(() => { $("clock").textContent = hhmmss(Date.now()); }, 1000);

  try {
    const boot = await api("/api/bootstrap");
    (boot.rooms || []).forEach((r) => {
      state.rooms[r.room] = { name: r.name };
      state.data[r.room] = { temp: [], power: [], humidity: [] };
    });
    state.thresholds = boot.thresholds;
    initCharts();
    buildRooms();
    renderSystem(boot.system);
    await refresh();
    await loadHistory();
    bindEvents();
    connectWS();
    setInterval(() => { loadHistory().catch(() => {}); }, 60000);
    // 仿真 30 秒 / 实际监测 10 秒全量刷新，切换模式时自动重排
    scheduleRefresh();
    setInterval(() => {
      if (state.system === "real") loadRealDevices().catch(() => {});
    }, 3000);
  } catch (err) {
    console.error("初始化失败", err);
  }
}

main();
