const state = {
  data: null,
  days: "all",
  customStart: null,
  customEnd: null,
  metric: "restoredPlotChats",
  restoredPlotId: null,
  chartPoints: [],
};

const metricLabels = {
  restoredPlotChats: "Wayback 복원 플롯",
  averageChatsPerPlot: "플롯당 누적 대화",
  totalChats: "관측 플롯 총대화",
  totalChatsWithRegen: "재생성 포함 총대화",
  totalComments: "댓글 합계",
  observedPlots: "관측 플롯 수",
};

const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("ko-KR");
const signed = new Intl.NumberFormat("ko-KR", { signDisplay: "exceptZero" });

function parseDay(value) {
  return new Date(`${value}T00:00:00+09:00`);
}

function dayString(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function shortDate(value) {
  const d = parseDay(value);
  return `${String(d.getMonth() + 1).padStart(2, "0")}.${String(d.getDate()).padStart(2, "0")}`;
}

function compact(value) {
  if (value == null || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  const sign = value < 0 ? "−" : "";
  if (abs >= 100000000) return `${sign}${(abs / 100000000).toFixed(abs >= 1000000000 ? 1 : 2).replace(/\.0+$/, "")}억`;
  if (abs >= 10000) return `${sign}${(abs / 10000).toFixed(abs >= 1000000 ? 1 : 2).replace(/\.0+$/, "")}만`;
  return number.format(value);
}

function exact(value) {
  return value == null ? "—" : number.format(Math.round(value));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function range() {
  const end = state.days === "custom" && state.customEnd
    ? state.customEnd
    : state.data.latestDate;
  let start;
  if (state.days === "all") {
    const available = state.data.platformHistory.map((point) => point.date).sort();
    start = available[0] || end;
  } else if (state.days === "custom" && state.customStart) {
    start = state.customStart;
  } else {
    const date = parseDay(end);
    date.setDate(date.getDate() - Number(state.days));
    start = dayString(date);
  }
  return { start, end };
}

function filtered(series) {
  const { start, end } = range();
  return (series || []).filter((point) => point.date >= start && point.date <= end);
}

function pair(series, key) {
  const points = filtered(series).filter((point) => point[key] != null);
  if (points.length < 2) return null;
  const first = points[0];
  const last = points[points.length - 1];
  if (first.date === last.date) return null;
  return { first, last, delta: last[key] - first[key] };
}

function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function plotChanges() {
  return state.data.plots.map((plot) => {
    const comparison = pair(plot.series, "chats");
    return { ...plot, comparison, delta: comparison?.delta ?? null };
  });
}

function renderKpis() {
  const changes = plotChanges().filter((plot) => plot.delta != null);
  const values = changes.map((plot) => plot.delta);
  const total = values.reduce((sum, value) => sum + value, 0);
  const avg = values.length ? total / values.length : null;
  const med = median(values);
  const active = values.filter((value) => value > 0).length;
  const coverage = state.data.coverage;

  $("kpi-total").textContent = values.length ? signed.format(total) : "기준선 대기";
  $("kpi-total").title = values.length ? `${signed.format(total)}회` : "";
  $("kpi-total-note").textContent = `비교 가능 플롯 ${number.format(values.length)}개`;
  $("kpi-average").textContent = avg == null ? "—" : signed.format(Math.round(avg));
  $("kpi-average-note").textContent = med == null ? "평균 · 중앙값 —" : `평균 · 중앙값 ${signed.format(Math.round(med))}회`;
  $("kpi-active").textContent = values.length ? `${number.format(active)}개` : "—";
  $("kpi-active-note").textContent = values.length ? `비교군의 ${((active / values.length) * 100).toFixed(1)}%` : "대화량이 증가한 플롯";
  $("kpi-plots").textContent = number.format(coverage.knownPlots);
  $("kpi-plots-note").textContent = `오늘 값 ${number.format(coverage.plotsWithCurrentValues)}개`;
}

function renderTable() {
  const changedPlots = plotChanges();
  const plotMap = new Map(changedPlots.map((plot) => [plot.id, plot]));
  const plots = changedPlots
    .filter((plot) => plot.delta != null)
    .sort((a, b) => b.delta - a.delta)
    .slice(0, 10);
  $("plot-table").innerHTML = plots.length ? plots.map((plot, index) => `
    <tr data-plot-id="${escapeHtml(plot.id)}">
      <td class="rank">${index + 1}</td>
      <td class="name-cell"><strong>${escapeHtml(plot.name)}</strong><small>${escapeHtml(plot.creator || "제작자 미확인")}</small></td>
      <td class="${plot.delta >= 0 ? "positive" : "negative"}" title="${signed.format(plot.delta)}회">${compact(plot.delta)}</td>
      <td title="${exact(plot.chats)}회">${compact(plot.chats)}</td>
    </tr>`).join("") : `<tr><td colspan="4">비교 가능한 두 관측일이 아직 없음.</td></tr>`;

  const tags = state.data.tags.map((tag) => {
    const comparable = (tag.memberIds || [])
      .map((id) => plotMap.get(id))
      .filter((plot) => plot?.delta != null);
    const delta = comparable.length
      ? comparable.reduce((sum, plot) => sum + plot.delta, 0)
      : null;
    return { ...tag, delta, comparablePlots: comparable.length };
  }).filter((tag) => tag.delta != null)
    .sort((a, b) => b.delta - a.delta)
    .slice(0, 10);
  $("tag-table").innerHTML = tags.length ? tags.map((tag, index) => `
    <tr>
      <td class="rank">${index + 1}</td>
      <td class="name-cell"><strong>#${escapeHtml(tag.tag)}</strong><small>${tag.complete ? "검색 순회 완료" : "확장 수집 중"}</small></td>
      <td class="${tag.delta >= 0 ? "positive" : "negative"}" title="${signed.format(tag.delta)}회">${compact(tag.delta)}</td>
      <td title="현재 발견 ${number.format(tag.observedPlots)}개">${number.format(tag.comparablePlots)}</td>
    </tr>`).join("") : `<tr><td colspan="4">태그 비교 기준선을 수집 중임.</td></tr>`;

  document.querySelectorAll("#plot-table tr[data-plot-id]").forEach((row) => {
    row.addEventListener("click", () => showPlot(row.dataset.plotId));
  });
}

function showPlot(plotId) {
  const plot = state.data.plots.find((item) => item.id === plotId);
  if (!plot) return;
  const comparison = pair(plot.series, "chats");
  $("plot-detail").hidden = false;
  $("detail-name").textContent = plot.name;
  $("detail-meta").textContent = `${plot.creator || "제작자 미확인"} · 관측 ${plot.series.length}회`;
  $("detail-link").href = `https://zeta-ai.io/ko/plots/${encodeURIComponent(plot.id)}/profile`;
  $("detail-stats").innerHTML = [
    ["현재 대화", exact(plot.chats)],
    ["재생성 포함", exact(plot.chatsWithRegen)],
    ["댓글", exact(plot.comments)],
    ["선택 기간 증가", comparison ? signed.format(comparison.delta) : "—"],
  ].map(([label, value]) => `<div><span>${label}</span><strong>${value}</strong></div>`).join("");
  $("detail-tags").innerHTML = (plot.tags || []).map((tag) => `<span>#${escapeHtml(tag)}</span>`).join("");
  $("plot-detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderMeta() {
  const updated = new Date(state.data.updatedAt);
  $("updated-at").textContent = `마지막 갱신 ${updated.toLocaleString("ko-KR", { timeZone: "Asia/Seoul", dateStyle: "medium", timeStyle: "short" })}`;
  const c = state.data.coverage;
  $("coverage-line").textContent = `플롯 ${number.format(c.knownPlots)} · 태그 ${number.format(c.knownTags)}`;
  $("method-plots").textContent = number.format(c.knownPlots);
  $("method-tags").textContent = number.format(c.knownTags);
  $("method-comments").textContent = number.format(c.plotsWithComments);
  $("method-queue").textContent = number.format(c.pendingTags);
  if (state.data.errors?.length) {
    $("error-box").hidden = false;
    $("error-list").innerHTML = state.data.errors.map((error) => `<li>${escapeHtml(error)}</li>`).join("");
  }
}

function niceStep(span, ticks = 5) {
  if (span <= 0) return 1;
  const raw = span / ticks;
  const power = 10 ** Math.floor(Math.log10(raw));
  const fraction = raw / power;
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
  return nice * power;
}

function drawChart() {
  const canvas = $("history-chart");
  const wrap = $("chart-wrap");
  const dpr = Math.max(1, window.devicePixelRatio || 1);
  const width = wrap.clientWidth;
  const height = wrap.clientHeight;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);

  const restoredMode = state.metric === "restoredPlotChats";
  const restoredPlot = restoredMode
    ? state.data.plots.find((plot) => plot.id === state.restoredPlotId)
    : null;
  const metric = restoredMode ? "chats" : state.metric;
  const sourceSeries = restoredMode ? (restoredPlot?.series || []) : state.data.platformHistory;
  const points = filtered(sourceSeries).filter((point) => point[metric] != null);
  $("chart-title").textContent = restoredMode
    ? `${restoredPlot?.name || "복원 플롯"} 누적 대화`
    : metricLabels[metric];
  $("chart-annotation").textContent = restoredMode
    ? "동일 플롯의 Wayback·현재 원값 · 표식이 있는 날짜만 확인됨"
    : "표식 = 값이 확인된 날 · 선은 관측값 연결 · 날짜별 관측 플롯 수가 다를 수 있음";
  $("chart-empty").hidden = points.length > 0;
  if (!points.length) {
    state.chartPoints = [];
    $("chart-summary").textContent = "관측값 없음";
    return;
  }
  const values = points.map((point) => point[metric]);
  const first = values[0], last = values[values.length - 1];
  const coverageChanged = !restoredMode && points.length > 1 && points[0].observedPlots !== points[points.length - 1].observedPlots;
  $("chart-summary").textContent = points.length > 1
    ? coverageChanged
      ? `관측 ${number.format(points[0].observedPlots)}→${number.format(points[points.length - 1].observedPlots)}개`
      : `${signed.format(Math.round(last - first))} 변화`
    : `${exact(last)} 현재`;

  const pad = { left: 68, right: 22, top: 20, bottom: 44 };
  const plotW = Math.max(1, width - pad.left - pad.right);
  const plotH = Math.max(1, height - pad.top - pad.bottom);
  let min = Math.min(...values), max = Math.max(...values);
  if (min === max) { min = Math.max(0, min * .9); max = max === 0 ? 1 : max * 1.1; }
  else {
    const margin = (max - min) * .12;
    min = Math.max(0, min - margin);
    max += margin;
  }
  const step = niceStep(max - min);
  min = Math.floor(min / step) * step;
  max = Math.ceil(max / step) * step;
  const firstTime = parseDay(points[0].date).getTime();
  const lastTime = parseDay(points[points.length - 1].date).getTime();
  const timeSpan = Math.max(86400000, lastTime - firstTime);
  const x = (date) => pad.left + ((parseDay(date).getTime() - firstTime) / timeSpan) * plotW;
  const y = (value) => pad.top + (1 - (value - min) / Math.max(1, max - min)) * plotH;

  ctx.font = '11px "IBM Plex Sans KR", sans-serif';
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  ctx.strokeStyle = "#dfe5df";
  ctx.fillStyle = "#7c8883";
  ctx.lineWidth = 1;
  for (let value = min; value <= max + step * .1; value += step) {
    const py = y(value);
    ctx.beginPath(); ctx.moveTo(pad.left, py); ctx.lineTo(width - pad.right, py); ctx.stroke();
    ctx.fillText(compact(Math.round(value)), pad.left - 10, py);
  }

  const labelIndexes = new Set([0, points.length - 1]);
  if (width > 600 && points.length > 2) labelIndexes.add(Math.floor((points.length - 1) / 2));
  ctx.textBaseline = "top";
  points.forEach((point, index) => {
    if (!labelIndexes.has(index)) return;
    ctx.textAlign = index === 0 ? "left" : index === points.length - 1 ? "right" : "center";
    ctx.fillText(shortDate(point.date), x(point.date), height - pad.bottom + 14);
  });

  if (points.length > 1) {
    ctx.beginPath();
    points.forEach((point, index) => {
      const px = x(point.date), py = y(point[metric]);
      if (index === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    });
    ctx.strokeStyle = "#386fe5";
    ctx.lineWidth = 3;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.stroke();
  }
  state.chartPoints = points.map((point) => ({ ...point, x: x(point.date), y: y(point[metric]), value: point[metric], restoredMode }));
  state.chartPoints.forEach((point) => {
    ctx.beginPath(); ctx.arc(point.x, point.y, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = "#fff"; ctx.fill();
    ctx.strokeStyle = "#386fe5"; ctx.lineWidth = 2.5; ctx.stroke();
  });
}

function renderAll() {
  renderKpis();
  renderTable();
  drawChart();
}

function bind() {
  document.querySelectorAll("#period-buttons button").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("#period-buttons button").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      state.days = ["custom", "all"].includes(button.dataset.days)
        ? button.dataset.days
        : Number(button.dataset.days);
      $("custom-dates").hidden = state.days !== "custom";
      renderAll();
    });
  });
  [$("start-date"), $("end-date")].forEach((input) => input.addEventListener("change", () => {
    state.customStart = $("start-date").value;
    state.customEnd = $("end-date").value;
    if (state.customStart && state.customEnd) renderAll();
  }));
  $("metric-select").addEventListener("change", (event) => {
    state.metric = event.target.value;
    $("history-plot-select").hidden = state.metric !== "restoredPlotChats";
    drawChart();
  });
  $("history-plot-select").addEventListener("change", (event) => {
    state.restoredPlotId = event.target.value;
    drawChart();
  });
  window.addEventListener("resize", () => requestAnimationFrame(drawChart));
  $("history-chart").addEventListener("mousemove", (event) => {
    if (!state.chartPoints.length) return;
    const rect = event.target.getBoundingClientRect();
    const mx = event.clientX - rect.left, my = event.clientY - rect.top;
    const nearest = state.chartPoints.reduce((best, point) => {
      const distance = Math.hypot(point.x - mx, point.y - my);
      return !best || distance < best.distance ? { point, distance } : best;
    }, null);
    if (!nearest || nearest.distance > 22) { $("chart-tooltip").hidden = true; return; }
    const tip = $("chart-tooltip");
    tip.innerHTML = nearest.point.restoredMode
      ? `${escapeHtml(nearest.point.date)}<strong>${exact(nearest.point.value)}</strong><span>정확한 누적 대화 원값</span>`
      : `${escapeHtml(nearest.point.date)}<strong>${exact(nearest.point.value)}</strong><span>관측 플롯 ${exact(nearest.point.observedPlots)}개</span>`;
    tip.hidden = false;
    tip.style.left = `${Math.min(rect.width - 170, Math.max(6, nearest.point.x + 12))}px`;
    tip.style.top = `${Math.max(4, nearest.point.y - 70)}px`;
  });
  $("history-chart").addEventListener("mouseleave", () => { $("chart-tooltip").hidden = true; });
}

async function boot() {
  try {
    const response = await fetch(`public/data/dashboard.json?t=${Date.now()}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    const restoredPlots = state.data.plots
      .filter((plot) => (plot.series || []).filter((point) => point.chats != null).length > 1)
      .sort((a, b) => b.series.length - a.series.length || a.series[0].date.localeCompare(b.series[0].date));
    if (restoredPlots.length) {
      state.restoredPlotId = restoredPlots[0].id;
      $("history-plot-select").innerHTML = restoredPlots.map((plot) =>
        `<option value="${escapeHtml(plot.id)}">${escapeHtml(plot.name)} · ${plot.series.length}점</option>`
      ).join("");
    } else {
      state.metric = "averageChatsPerPlot";
      $("metric-select").value = state.metric;
      $("history-plot-select").hidden = true;
    }
    const end = state.data.latestDate;
    const start = parseDay(end); start.setDate(start.getDate() - 30);
    state.customStart = dayString(start); state.customEnd = end;
    $("start-date").value = state.customStart; $("end-date").value = end;
    renderMeta(); bind(); renderAll();
  } catch (error) {
    document.body.innerHTML = `<main class="shell"><section class="panel"><h1>데이터를 불러오지 못함</h1><p>${escapeHtml(error.message)}</p></section></main>`;
  }
}

boot();
