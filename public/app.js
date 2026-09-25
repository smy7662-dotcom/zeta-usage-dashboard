const state = {
  data: null,
  days: "all",
  customStart: null,
  customEnd: null,
  metric: "homepageMatchedIndex",
  restoredPlotId: null,
  plotQuery: "",
  chartablePlots: [],
  detailPlotId: null,
  chartPoints: [],
  activityChartPoints: [],
  regenChartPoints: [],
  regenVisible: new Set([10, 30, 50, 100]),
  coreChartPoints: [],
};

const metricLabels = {
  homepageMatchedIndex: "동일 플롯 대화 성장지수",
  homepageAverageChats: "홈 노출 플롯당 평균 대화",
  homepageMedianChats: "홈 노출 플롯 중앙값",
  homepageTotalChats: "홈 노출 플롯 총대화",
  homepageTop10Chats: "홈 노출 상위 10개 총대화",
  restoredPlotChats: "플롯별 실제 누적 대화",
  averageChatsPerPlot: "플롯당 누적 대화",
  totalChats: "관측 플롯 총대화",
  totalChatsWithRegen: "재생성 포함 총대화",
  totalComments: "댓글 합계",
  observedPlots: "관측 플롯 수",
};

const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("ko-KR");
const signed = new Intl.NumberFormat("ko-KR", { signDisplay: "exceptZero" });
const activityDirectionLabels = {
  collecting: "기준선 수집 중",
  provisional: "잠정 변화율",
  broadAcceleration: "광범위한 가속",
  concentratedAcceleration: "소수 플롯 중심 가속",
  decelerating: "감속",
  mixed: "방향 불명확",
};
const regenColors = {
  10: "#386fe5",
  30: "#1d8b67",
  50: "#e87553",
  100: "#7667e8",
};

function parseDay(value) {
  return new Date(`${value}T00:00:00+09:00`);
}

function dayString(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function axisDate(value, showYear) {
  const d = parseDay(value);
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return showYear ? `${String(d.getFullYear()).slice(2)}.${month}.${day}` : `${month}.${day}`;
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

function activityData() {
  const history = state.data.activityHistory || [];
  return {
    history,
    latest: state.data.activityLatest || history[history.length - 1] || null,
    panel: state.data.activityPanel || { panelSize: 0, selectedDate: null, segments: {} },
  };
}

function renderActivitySummary() {
  const { history, latest, panel } = activityData();
  const validDays = history.filter((point) =>
    point.totalNewChats != null && point.comparisonCoveragePct >= 95
  ).length;
  if (!latest) {
    $("activity-seven-day").textContent = "기준선 대기";
    $("activity-acceleration").textContent = "판정 대기";
    $("activity-per-plot").textContent = "—";
    $("activity-breadth").textContent = "—";
    $("activity-summary").textContent = panel.panelSize
      ? `고정 패널 ${number.format(panel.panelSize)}개`
      : "패널 생성 대기";
    $("activity-coverage").textContent = "연속 관측값 없음";
    return;
  }

  $("activity-seven-day").textContent = latest.sevenDayAverageNewChats == null
    ? "기준선 대기"
    : compact(latest.sevenDayAverageNewChats);
  $("activity-seven-day-note").textContent = latest.sevenDayAverageNewChats == null
    ? `유효 관측 ${Math.min(validDays, 7)}/7일`
    : `정확히 ${exact(latest.sevenDayAverageNewChats)}회/일`;

  $("activity-acceleration").textContent = latest.accelerationPct == null
    ? "판정 대기"
    : `${latest.accelerationPct > 0 ? "+" : ""}${latest.accelerationPct.toFixed(1)}%`;
  $("activity-acceleration-note").textContent = latest.accelerationPct == null
    ? `유효 관측 ${Math.min(validDays, 14)}/14일`
    : `${activityDirectionLabels[latest.direction] || "잠정 변화율"} · 직전 7일 대비`;

  $("activity-per-plot").textContent = latest.averageCumulativeChatsPerPlot == null
    ? "—"
    : compact(latest.averageCumulativeChatsPerPlot);
  $("activity-per-plot").title = latest.averageCumulativeChatsPerPlot == null
    ? ""
    : `평균 ${exact(latest.averageCumulativeChatsPerPlot)}회`;
  $("activity-per-plot-note").textContent = latest.medianNewChatsPerPlot == null
    ? "고정 패널 평균 · 오늘 신규 중앙값"
    : `누적 중앙 ${compact(latest.medianCumulativeChatsPerPlot)} · 신규 중앙 ${signed.format(latest.medianNewChatsPerPlot)}회`;
  $("activity-breadth").textContent = latest.activeSharePct == null
    ? "—"
    : `${latest.activeSharePct.toFixed(1)}%`;
  $("activity-breadth-note").textContent = latest.activeSharePct == null
    ? "대화량이 증가한 비교 가능 플롯"
    : `활성 ${number.format(latest.activePlots)}개 · 상위 10 기여 ${latest.top10ContributionPct == null ? "—" : `${latest.top10ContributionPct.toFixed(1)}%`}`;

  $("activity-summary").textContent = latest.accelerationPct == null
    ? `관측 ${Math.min(validDays, 14)}/14일`
    : activityDirectionLabels[latest.direction] || "잠정 변화율";
  const correctionNote = latest.negativeCorrections
    ? ` · 원값 정정 ${number.format(latest.negativeCorrections)}개 제외`
    : "";
  $("activity-coverage").textContent = `고정 패널 당일 ${number.format(latest.observedPanelPlots)}/${number.format(latest.panelSize)}개 · 일일 비교 ${number.format(latest.matchedPlots)}개 (${latest.comparisonCoveragePct.toFixed(1)}%)${correctionNote}`;
}

function drawActivityChart() {
  const canvas = $("activity-chart");
  const wrap = $("activity-chart-wrap");
  const points = activityData().history.filter((point) => point.totalNewChats != null);
  $("activity-empty").hidden = points.length > 0;
  if (!points.length) {
    state.activityChartPoints = [];
    return;
  }

  const dpr = Math.max(1, window.devicePixelRatio || 1);
  const width = wrap.clientWidth;
  const height = wrap.clientHeight;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);

  const pad = { left: 68, right: 24, top: 18, bottom: 42 };
  const plotW = Math.max(1, width - pad.left - pad.right);
  const plotH = Math.max(1, height - pad.top - pad.bottom);
  const values = points.flatMap((point) => [
    point.totalNewChats,
    point.sevenDayAverageNewChats,
  ]).filter((value) => value != null);
  const maxRaw = Math.max(...values, 1);
  const step = niceStep(maxRaw, 5);
  const maxValue = Math.ceil(maxRaw / step) * step;
  const firstTime = parseDay(points[0].date).getTime();
  const lastTime = parseDay(points[points.length - 1].date).getTime();
  const timeSpan = Math.max(86400000, lastTime - firstTime);
  const x = (date) => pad.left + ((parseDay(date).getTime() - firstTime) / timeSpan) * plotW;
  const y = (value) => pad.top + (1 - value / maxValue) * plotH;

  ctx.font = '11px "IBM Plex Sans KR", sans-serif';
  ctx.lineWidth = 1;
  ctx.strokeStyle = "#dfe5df";
  ctx.fillStyle = "#7c8883";
  ctx.textBaseline = "middle";
  for (let value = 0; value <= maxValue + step * .1; value += step) {
    const py = y(value);
    ctx.beginPath();
    ctx.moveTo(pad.left, py);
    ctx.lineTo(width - pad.right, py);
    ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(compact(value), pad.left - 9, py);
  }

  const barWidth = Math.min(42, Math.max(8, plotW / Math.max(points.length, 1) * .48));
  const visualPoints = points.map((point, index) => {
    let px = x(point.date);
    if (index === 0) px = Math.max(pad.left + barWidth / 2, px);
    if (index === points.length - 1) px = Math.min(width - pad.right - barWidth / 2, px);
    const top = y(point.totalNewChats);
    ctx.fillStyle = point.comparisonCoveragePct < 95
      ? "rgba(232,117,83,.30)"
      : "rgba(232,117,83,.64)";
    ctx.fillRect(px - barWidth / 2, top, barWidth, pad.top + plotH - top);
    return {
      ...point,
      x: px,
      barTop: top,
      lineY: point.sevenDayAverageNewChats == null ? null : y(point.sevenDayAverageNewChats),
    };
  });

  ctx.strokeStyle = "#386fe5";
  ctx.lineWidth = 3;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  let drawing = false;
  visualPoints.forEach((point) => {
    if (point.lineY == null) {
      if (drawing) ctx.stroke();
      drawing = false;
      return;
    }
    if (!drawing) {
      ctx.beginPath();
      ctx.moveTo(point.x, point.lineY);
      drawing = true;
    } else {
      ctx.lineTo(point.x, point.lineY);
    }
  });
  if (drawing) ctx.stroke();
  visualPoints.filter((point) => point.lineY != null).forEach((point) => {
    ctx.beginPath();
    ctx.arc(point.x, point.lineY, 4, 0, Math.PI * 2);
    ctx.fillStyle = "#fff";
    ctx.fill();
    ctx.strokeStyle = "#386fe5";
    ctx.lineWidth = 2.5;
    ctx.stroke();
  });

  const labelIndexes = new Set([0, points.length - 1]);
  if (width > 600 && points.length > 2) labelIndexes.add(Math.floor((points.length - 1) / 2));
  const showYear = parseDay(points[0].date).getFullYear() !== parseDay(points[points.length - 1].date).getFullYear();
  ctx.fillStyle = "#7c8883";
  ctx.textBaseline = "top";
  visualPoints.forEach((point, index) => {
    if (!labelIndexes.has(index)) return;
    ctx.textAlign = index === 0 ? "left" : index === points.length - 1 ? "right" : "center";
    ctx.fillText(axisDate(point.date, showYear), point.x, height - pad.bottom + 14);
  });
  state.activityChartPoints = visualPoints;
}

function regenerationData() {
  return state.data.regenerationCohorts || {
    cohorts: [],
    latestSeparableDate: null,
    unseparableFrom: null,
    currentSeparable: false,
  };
}

function renderRegenerationSummary() {
  const data = regenerationData();
  const cohorts = new Map((data.cohorts || []).map((cohort) => [cohort.size, cohort]));
  [10, 30, 50, 100].forEach((size) => {
    const cohort = cohorts.get(size);
    const latest = cohort?.history?.[cohort.history.length - 1];
    const rate = $(`regen-rate-${size}`);
    const note = $(`regen-note-${size}`);
    rate.textContent = latest == null ? "—" : `${latest.regenerationRatePct.toFixed(1)}%`;
    rate.title = latest == null ? "" : `정확히 ${latest.regenerationRatePct.toFixed(2)}%`;
    note.textContent = latest == null
      ? "비교 가능한 복원 구간 없음"
      : `${latest.startDate.replaceAll("-", ".")}→${latest.date.replaceAll("-", ".")} · ${number.format(latest.matchedPlots)}/${number.format(size)}개`;
  });

  $("regen-summary").textContent = data.latestSeparableDate
    ? `${data.latestSeparableDate.replaceAll("-", ".")}까지 구분 가능`
    : "구분 가능한 원값 없음";
  const histories = (data.cohorts || []).flatMap((cohort) => cohort.history || []);
  $("regen-coverage").textContent = histories.length
    ? `실제 구간 ${number.format(histories.length)}점 · 점마다 동일 플롯 비교 수 표시`
    : "재생성 포함·제외 원값이 분리된 구간 없음";
  $("regen-caveat").textContent = data.unseparableFrom
    ? `${data.unseparableFrom.replaceAll("-", ".")}부터 공개 API의 재생성 포함·제외 값이 같아져 0%로 해석하지 않고 선을 중단함. 과거도 코호트 전체가 아니라 당시 두 시점에 모두 잡힌 동일 플롯만 계산함.`
    : "같은 플롯의 두 관측점을 비교하며 누락값은 0으로 채우지 않음. 음수 증분과 공개 원값 정정은 계산에서 제외함.";
}

function drawRegenerationChart() {
  const canvas = $("regen-chart");
  const wrap = $("regen-chart-wrap");
  const cohorts = (regenerationData().cohorts || [])
    .filter((cohort) => state.regenVisible.has(cohort.size) && cohort.history?.length);
  const allPoints = cohorts.flatMap((cohort) =>
    cohort.history.map((point) => ({ ...point, cohortSize: cohort.size }))
  );
  $("regen-empty").hidden = allPoints.length > 0;
  if (!allPoints.length) {
    state.regenChartPoints = [];
    const context = canvas.getContext("2d");
    context.clearRect(0, 0, canvas.width, canvas.height);
    return;
  }

  const dpr = Math.max(1, window.devicePixelRatio || 1);
  const width = wrap.clientWidth;
  const height = wrap.clientHeight;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);

  const pad = { left: 58, right: 24, top: 22, bottom: 44 };
  const plotW = Math.max(1, width - pad.left - pad.right);
  const plotH = Math.max(1, height - pad.top - pad.bottom);
  const dates = allPoints.map((point) => point.date).sort();
  const firstTime = parseDay(dates[0]).getTime();
  const lastTime = parseDay(dates[dates.length - 1]).getTime();
  const timeSpan = Math.max(86400000, lastTime - firstTime);
  const maxRaw = Math.max(...allPoints.map((point) => point.regenerationRatePct), 1);
  const step = niceStep(maxRaw, 5);
  const maxValue = Math.max(step, Math.ceil(maxRaw / step) * step);
  const x = (date) => pad.left + ((parseDay(date).getTime() - firstTime) / timeSpan) * plotW;
  const y = (value) => pad.top + (1 - value / maxValue) * plotH;

  ctx.font = '11px "IBM Plex Sans KR", sans-serif';
  ctx.lineWidth = 1;
  ctx.strokeStyle = "#dfe5df";
  ctx.fillStyle = "#7c8883";
  ctx.textBaseline = "middle";
  for (let value = 0; value <= maxValue + step * .1; value += step) {
    const py = y(value);
    ctx.beginPath();
    ctx.moveTo(pad.left, py);
    ctx.lineTo(width - pad.right, py);
    ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(`${value.toFixed(value < 1 ? 1 : 0)}%`, pad.left - 9, py);
  }

  const visualPoints = [];
  cohorts.forEach((cohort) => {
    const color = regenColors[cohort.size];
    const points = cohort.history.map((point) => ({
      ...point,
      cohortSize: cohort.size,
      x: x(point.date),
      y: y(point.regenerationRatePct),
      color,
    }));
    if (points.length > 1) {
      ctx.beginPath();
      points.forEach((point, index) => {
        if (index === 0) ctx.moveTo(point.x, point.y);
        else ctx.lineTo(point.x, point.y);
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = cohort.size === 100 ? 3.2 : 2.4;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.stroke();
    }
    points.forEach((point) => {
      ctx.beginPath();
      ctx.arc(point.x, point.y, cohort.size === 100 ? 4.5 : 3.8, 0, Math.PI * 2);
      ctx.fillStyle = "#fff";
      ctx.fill();
      ctx.strokeStyle = color;
      ctx.lineWidth = 2.2;
      ctx.stroke();
    });
    visualPoints.push(...points);
  });

  const labelDates = [dates[0], dates[dates.length - 1]];
  if (width > 600 && dates.length > 2) labelDates.splice(1, 0, dates[Math.floor((dates.length - 1) / 2)]);
  const showYear = parseDay(dates[0]).getFullYear() !== parseDay(dates[dates.length - 1]).getFullYear();
  ctx.fillStyle = "#7c8883";
  ctx.textBaseline = "top";
  labelDates.forEach((date, index) => {
    ctx.textAlign = index === 0 ? "left" : index === labelDates.length - 1 ? "right" : "center";
    ctx.fillText(axisDate(date, showYear), x(date), height - pad.bottom + 14);
  });
  state.regenChartPoints = visualPoints;
}

function coreData() {
  if (state.data.coreHistory?.length) {
    return {
      history: state.data.coreHistory,
      latest: state.data.coreInventory || state.data.coreHistory[state.data.coreHistory.length - 1],
    };
  }
  const policy = state.data.corePolicy || { coreThreshold: 1000000, watchThreshold: 500000 };
  const available = (state.data.plots || []).filter((plot) => Number.isFinite(plot.chats));
  const core = available.filter((plot) => plot.chats >= policy.coreThreshold);
  const watch = available.filter((plot) => plot.chats >= policy.watchThreshold && plot.chats < policy.coreThreshold);
  if (!available.length) return { history: [], latest: null };
  const latest = {
    date: state.data.latestDate,
    corePlotCount: core.length,
    coreTotalChats: core.reduce((sum, plot) => sum + plot.chats, 0),
    watchPlotCount: watch.length,
    watchTotalChats: watch.reduce((sum, plot) => sum + plot.chats, 0),
    observedPlots: available.length,
    refreshedCorePlots: 0,
    coreCoveragePct: null,
    crossedCoreCount: 0,
    discoveredCoreCount: 0,
  };
  return { history: [latest], latest };
}

function renderCoreSummary() {
  const { latest } = coreData();
  const policy = state.data.corePolicy || { coreThreshold: 1000000, watchThreshold: 500000 };
  if (!latest) {
    $("core-count").textContent = "—";
    $("core-chats").textContent = "—";
    $("watch-count").textContent = "—";
    $("core-entrants").textContent = "—";
    $("core-summary").textContent = "기준선 대기";
    $("core-coverage").textContent = "당일 갱신 범위 없음";
    return;
  }

  const entrants = (latest.crossedCoreCount || 0) + (latest.discoveredCoreCount || 0);
  $("core-count").textContent = `${number.format(latest.corePlotCount)}개`;
  $("core-count").title = `${number.format(latest.corePlotCount)}개`;
  $("core-count-note").textContent = `${compact(policy.coreThreshold)}회 이상 · 최신 확보값`;
  $("core-chats").textContent = compact(latest.coreTotalChats);
  $("core-chats").title = `${exact(latest.coreTotalChats)}회`;
  $("core-chats-note").textContent = `정확히 ${exact(latest.coreTotalChats)}회`;
  $("watch-count").textContent = `${number.format(latest.watchPlotCount)}개`;
  $("watch-count-note").textContent = `${compact(policy.watchThreshold)}~${compact(policy.coreThreshold)}회 미만`;
  $("core-entrants").textContent = `${number.format(entrants)}개`;
  $("core-entrants-note").textContent = `실제 돌파 ${number.format(latest.crossedCoreCount || 0)} · 늦게 발견 ${number.format(latest.discoveredCoreCount || 0)}`;
  $("core-summary").textContent = `${latest.date} 최신 확보값`;
  $("core-coverage").textContent = latest.coreCoveragePct == null
    ? "당일 갱신 범위 없음"
    : `당일 핵심 재조회 ${number.format(latest.refreshedCorePlots)}/${number.format(latest.corePlotCount)}개 · ${latest.coreCoveragePct.toFixed(1)}%`;
}

function drawCoreChart() {
  const canvas = $("core-chart");
  const wrap = $("core-chart-wrap");
  const points = coreData().history;
  $("core-empty").hidden = points.length > 0;
  if (!points.length) {
    state.coreChartPoints = [];
    return;
  }

  const dpr = Math.max(1, window.devicePixelRatio || 1);
  const width = wrap.clientWidth;
  const height = wrap.clientHeight;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);

  const pad = { left: 58, right: 72, top: 20, bottom: 42 };
  const plotW = Math.max(1, width - pad.left - pad.right);
  const plotH = Math.max(1, height - pad.top - pad.bottom);
  const countMaxRaw = Math.max(...points.map((point) => point.corePlotCount), 1);
  const countStep = niceStep(countMaxRaw, 5);
  const countMax = Math.ceil(countMaxRaw / countStep) * countStep;
  const chatValues = points.map((point) => point.coreTotalChats);
  let chatMin = Math.min(...chatValues);
  let chatMax = Math.max(...chatValues);
  if (chatMin === chatMax) {
    const margin = Math.max(1, chatMax * .05);
    chatMin = Math.max(0, chatMin - margin);
    chatMax += margin;
  } else {
    const margin = (chatMax - chatMin) * .12;
    chatMin = Math.max(0, chatMin - margin);
    chatMax += margin;
  }

  const firstTime = parseDay(points[0].date).getTime();
  const lastTime = parseDay(points[points.length - 1].date).getTime();
  const timeSpan = Math.max(86400000, lastTime - firstTime);
  const x = (date) => pad.left + ((parseDay(date).getTime() - firstTime) / timeSpan) * plotW;
  const yCount = (value) => pad.top + (1 - value / countMax) * plotH;
  const yChats = (value) => pad.top + (1 - (value - chatMin) / Math.max(1, chatMax - chatMin)) * plotH;

  ctx.font = '11px "IBM Plex Sans KR", sans-serif';
  ctx.lineWidth = 1;
  ctx.strokeStyle = "#dfe5df";
  ctx.fillStyle = "#7c8883";
  ctx.textBaseline = "middle";
  for (let value = 0; value <= countMax + countStep * .1; value += countStep) {
    const py = yCount(value);
    ctx.beginPath();
    ctx.moveTo(pad.left, py);
    ctx.lineTo(width - pad.right, py);
    ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(number.format(value), pad.left - 9, py);
  }
  ctx.textAlign = "left";
  for (let index = 0; index <= 4; index += 1) {
    const value = chatMin + ((chatMax - chatMin) * index) / 4;
    ctx.fillText(compact(Math.round(value)), width - pad.right + 9, yChats(value));
  }

  const barWidth = Math.min(42, Math.max(8, plotW / Math.max(points.length, 1) * .48));
  const barX = (point, index) => {
    const px = x(point.date);
    if (index === 0) return Math.max(pad.left + barWidth / 2, px);
    if (index === points.length - 1) return Math.min(width - pad.right - barWidth / 2, px);
    return px;
  };
  const visualPoints = points.map((point, index) => {
    const px = barX(point, index);
    const top = yCount(point.corePlotCount);
    ctx.fillStyle = point.coreCoveragePct != null && point.coreCoveragePct < 95
      ? "rgba(56,111,229,.42)"
      : "rgba(56,111,229,.75)";
    ctx.fillRect(px - barWidth / 2, top, barWidth, pad.top + plotH - top);
    return {
      ...point,
      x: px,
      lineY: yChats(point.coreTotalChats),
      barLeft: px - barWidth / 2,
      barRight: px + barWidth / 2,
      barTop: top,
    };
  });

  if (visualPoints.length > 1) {
    ctx.beginPath();
    visualPoints.forEach((point, index) => {
      if (index === 0) ctx.moveTo(point.x, point.lineY);
      else ctx.lineTo(point.x, point.lineY);
    });
    ctx.strokeStyle = "#e87553";
    ctx.lineWidth = 3;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.stroke();
  }
  visualPoints.forEach((point) => {
    ctx.beginPath();
    ctx.arc(point.x, point.lineY, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = "#fff";
    ctx.fill();
    ctx.strokeStyle = "#e87553";
    ctx.lineWidth = 2.5;
    ctx.stroke();
  });

  const labelIndexes = new Set([0, points.length - 1]);
  if (width > 600 && points.length > 2) labelIndexes.add(Math.floor((points.length - 1) / 2));
  const showYear = parseDay(points[0].date).getFullYear() !== parseDay(points[points.length - 1].date).getFullYear();
  ctx.fillStyle = "#7c8883";
  ctx.textBaseline = "top";
  visualPoints.forEach((point, index) => {
    if (!labelIndexes.has(index)) return;
    ctx.textAlign = index === 0 ? "left" : index === points.length - 1 ? "right" : "center";
    ctx.fillText(axisDate(point.date, showYear), point.x, height - pad.bottom + 14);
  });
  state.coreChartPoints = visualPoints;
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
  state.detailPlotId = plotId;
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

function sourceLabel(source) {
  if (source === "wayback-home") return "Wayback 홈 캡처";
  if (source === "wayback-api") return "Wayback API JSON";
  if (source === "wayback") return "Wayback 프로필";
  if (source?.startsWith("ranking:")) return "현재 랭킹 API";
  if (source === "detail" || source === "live") return "현재 상세 API";
  return source || "공개 원값";
}

function renderPlotOptions(query = "") {
  state.plotQuery = query.trim().toLocaleLowerCase("ko-KR");
  const matches = state.chartablePlots.filter((plot) => {
    if (!state.plotQuery) return true;
    return [plot.name, plot.creator, ...(plot.tags || [])]
      .filter(Boolean)
      .some((value) => String(value).toLocaleLowerCase("ko-KR").includes(state.plotQuery));
  });
  const visible = matches.slice(0, 300);
  if (!visible.length) {
    state.restoredPlotId = null;
    $("history-plot-select").innerHTML = '<option value="">검색 결과 없음</option>';
    return;
  }
  if (!visible.some((plot) => plot.id === state.restoredPlotId)) {
    state.restoredPlotId = visible[0].id;
  }
  $("history-plot-select").innerHTML = visible.map((plot) => {
    const points = (plot.series || []).filter((point) => point.chats != null).length;
    return `<option value="${escapeHtml(plot.id)}"${plot.id === state.restoredPlotId ? " selected" : ""}>${escapeHtml(plot.name)} · ${number.format(points)}점 · ${compact(plot.chats)}</option>`;
  }).join("");
}

function showPlotChart(plotId) {
  const plot = state.data.plots.find((item) => item.id === plotId);
  if (!plot) return;
  state.metric = "restoredPlotChats";
  state.restoredPlotId = plotId;
  $("metric-select").value = state.metric;
  $("plot-picker").hidden = false;
  $("plot-search").value = plot.name;
  renderPlotOptions(plot.name);
  drawChart();
  document.querySelector(".chart-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderMeta() {
  const updated = new Date(state.data.updatedAt);
  $("updated-at").textContent = `마지막 갱신 ${updated.toLocaleString("ko-KR", { timeZone: "Asia/Seoul", dateStyle: "medium", timeStyle: "short" })}`;
  const c = state.data.coverage;
  $("coverage-line").textContent = `플롯 ${number.format(c.knownPlots)} · 태그 ${number.format(c.knownTags)}`;
  $("method-plots").textContent = number.format(c.knownPlots);
  $("method-tags").textContent = number.format(c.knownTags);
  $("method-comments").textContent = number.format(c.plotsWithComments);
  $("method-history").textContent = number.format(c.plotsWithHistory || 0);
  if (state.data.homepageHistory?.length) {
    const history = state.data.homepageHistory;
    const coverages = history.map((point) => point.observedPlots).filter(Number.isFinite);
    $("method-note").textContent = `Wayback 한국 홈은 색인 71개 중 ${number.format(history.length)}개 날짜(${history[0].date}~${history[history.length - 1].date})를 복원했고, 보관 API JSON에서는 플롯-날짜 원값 ${number.format(c.waybackApiObservations || 0)}개를 추가 복원함. 현재 ${number.format(c.plotsWithHistory || 0)}개 플롯이 2개 이상의 대화수 관측점을 가짐. 홈 캡처는 플랫폼 전수가 아니라 공개 표본이며 빈 날짜는 생성하지 않음.`;
  }
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
  const homepageMode = state.metric.startsWith("homepage");
  const restoredPlot = restoredMode
    ? state.data.plots.find((plot) => plot.id === state.restoredPlotId)
    : null;
  const homepageMetric = {
    homepageMatchedIndex: "matchedGrowthIndex",
    homepageAverageChats: "averageChatsPerPlot",
    homepageMedianChats: "medianChatsPerPlot",
    homepageTotalChats: "totalChats",
    homepageTop10Chats: "top10Chats",
  }[state.metric];
  const metric = restoredMode ? "chats" : homepageMode ? homepageMetric : state.metric;
  const sourceSeries = restoredMode
    ? (restoredPlot?.series || [])
    : homepageMode
      ? state.metric === "homepageMatchedIndex"
        ? (state.data.matchedGrowthHistory || state.data.homepageHistory || [])
        : (state.data.homepageHistory || [])
      : state.data.platformHistory;
  const points = filtered(sourceSeries).filter((point) => point[metric] != null);
  $("chart-title").textContent = restoredMode
    ? `${restoredPlot?.name || "복원 플롯"} 누적 대화`
    : metricLabels[metric];
  if (homepageMode) $("chart-title").textContent = metricLabels[state.metric];
  $("chart-annotation").textContent = restoredMode
    ? "동일 플롯의 Wayback·현재 원값 · 표식이 있는 날짜만 확인됨"
    : homepageMode
      ? state.metric === "homepageMatchedIndex"
        ? "같은 플롯이 10개 이상 겹치는 캡처끼리 증가율 중앙값을 연결 · 2024.05.22 = 100"
        : state.metric === "homepageTop10Chats"
          ? "각 캡처의 홈 노출 플롯 중 대화량 상위 10개 합계 · 플랫폼 전체 상위 10이 아님"
        : "Wayback 홈 화면 공개 표본 · 표식마다 당시 노출 플롯 수가 다름"
      : "표식 = 값이 확인된 날 · 선은 관측값 연결 · 날짜별 관측 플롯 수가 다를 수 있음";

  const facts = $("chart-facts");
  facts.hidden = false;
  if (homepageMode) {
    const history = state.data.homepageHistory || [];
    const coverages = history.map((point) => point.observedPlots).filter(Number.isFinite);
    $("history-source").textContent = state.metric === "homepageMatchedIndex"
      ? "Wayback 홈 표본 + 현재 API"
      : "Wayback 홈 표본 · 색인 71캡처";
    $("history-points").textContent = points.length
      ? `${state.metric === "homepageMatchedIndex" ? "지수" : "원값"} ${number.format(points.length)}점 · ${points[0].date}→${points[points.length - 1].date}`
      : "관측점 없음";
    const latestPoint = points[points.length - 1];
    $("history-coverage").textContent = state.metric === "homepageMatchedIndex" && latestPoint?.isCurrent
      ? `최신 연결 동일 플롯 ${number.format(latestPoint.matchedPlots)}개 · 홈 원값 ${number.format(history.length)}일`
      : coverages.length
      ? `원값 ${number.format(history.length)}일 · 캡처당 ${number.format(Math.min(...coverages))}~${number.format(Math.max(...coverages))}개 플롯`
      : "캡처당 플롯 —";
    $("history-source-link").hidden = false;
    const latestSource = points[points.length - 1]?.sourceUrl;
    $("history-source-link").href = latestSource || "https://web.archive.org/web/20240522165134id_/https://zeta-ai.io/ko";
    $("history-source-link").textContent = state.metric === "homepageMatchedIndex" && points[points.length - 1]?.isCurrent
      ? "현재 API ↗"
      : "원문 캡처 ↗";
  } else if (restoredMode) {
    $("history-source").textContent = "플롯별 원값 · Wayback+현재 API";
    $("history-points").textContent = points.length
      ? `${number.format(points.length)}점 · ${points[0].date}→${points[points.length - 1].date}`
      : "관측점 없음";
    $("history-coverage").textContent = "값이 확인된 날짜만 연결";
    const latestSource = points[points.length - 1]?.sourceUrl;
    $("history-source-link").hidden = !latestSource;
    if (latestSource) {
      $("history-source-link").href = latestSource;
      $("history-source-link").textContent = "최근 원문 ↗";
    }
  } else {
    $("history-source").textContent = "일일 공개 관측";
    $("history-points").textContent = `관측 ${number.format(points.length)}일`;
    $("history-coverage").textContent = "플롯 수 변화를 함께 확인";
    $("history-source-link").hidden = true;
  }
  $("chart-empty").hidden = points.length > 0;
  if (!points.length) {
    state.chartPoints = [];
    $("chart-summary").textContent = "관측값 없음";
    return;
  }
  const values = points.map((point) => point[metric]);
  const first = values[0], last = values[values.length - 1];
  const coverageChanged = !restoredMode && !homepageMode && points.length > 1 && points[0].observedPlots !== points[points.length - 1].observedPlots;
  $("chart-summary").textContent = points.length > 1
    ? state.metric === "homepageMatchedIndex"
      ? `${(last / first).toFixed(1)}배 · ${number.format(points.length)}개 관측점`
      : coverageChanged
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
  const showYear = parseDay(points[0].date).getFullYear() !== parseDay(points[points.length - 1].date).getFullYear();
  ctx.textBaseline = "top";
  points.forEach((point, index) => {
    if (!labelIndexes.has(index)) return;
    ctx.textAlign = index === 0 ? "left" : index === points.length - 1 ? "right" : "center";
    ctx.fillText(axisDate(point.date, showYear), x(point.date), height - pad.bottom + 14);
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
  renderActivitySummary();
  drawActivityChart();
  renderRegenerationSummary();
  drawRegenerationChart();
  renderCoreSummary();
  drawCoreChart();
  renderKpis();
  renderTable();
  drawChart();
}

function bind() {
  document.querySelectorAll("#regen-legend button").forEach((button) => {
    button.addEventListener("click", () => {
      const cohort = Number(button.dataset.cohort);
      if (state.regenVisible.has(cohort)) {
        if (state.regenVisible.size === 1) return;
        state.regenVisible.delete(cohort);
      } else {
        state.regenVisible.add(cohort);
      }
      button.classList.toggle("active", state.regenVisible.has(cohort));
      button.setAttribute("aria-pressed", String(state.regenVisible.has(cohort)));
      drawRegenerationChart();
    });
  });
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
    $("plot-picker").hidden = state.metric !== "restoredPlotChats";
    drawChart();
  });
  $("plot-search").addEventListener("input", (event) => {
    renderPlotOptions(event.target.value);
    drawChart();
  });
  $("history-plot-select").addEventListener("change", (event) => {
    state.restoredPlotId = event.target.value;
    drawChart();
  });
  $("detail-chart-button").addEventListener("click", () => {
    if (state.detailPlotId) showPlotChart(state.detailPlotId);
  });
  window.addEventListener("resize", () => requestAnimationFrame(() => {
    drawActivityChart();
    drawRegenerationChart();
    drawCoreChart();
    drawChart();
  }));
  $("activity-chart").addEventListener("mousemove", (event) => {
    if (!state.activityChartPoints.length) return;
    const rect = event.target.getBoundingClientRect();
    const mx = event.clientX - rect.left;
    const nearest = state.activityChartPoints.reduce((best, point) => {
      const distance = Math.abs(point.x - mx);
      return !best || distance < best.distance ? { point, distance } : best;
    }, null);
    if (!nearest || nearest.distance > 34) {
      $("activity-tooltip").hidden = true;
      return;
    }
    const point = nearest.point;
    const tip = $("activity-tooltip");
    tip.innerHTML = `${escapeHtml(point.date)}<strong>신규 대화 ${exact(point.totalNewChats)}회</strong><span>일일 비교 ${number.format(point.matchedPlots)}/${number.format(point.panelSize)}개 (${point.comparisonCoveragePct.toFixed(1)}%)</span><span>플롯당 중앙 ${exact(point.medianNewChatsPerPlot)}회 · 활성 ${point.activeSharePct == null ? "—" : `${point.activeSharePct.toFixed(1)}%`}</span><span>7일 평균 ${point.sevenDayAverageNewChats == null ? "기준선 수집 중" : `${exact(point.sevenDayAverageNewChats)}회`}</span>`;
    tip.hidden = false;
    tip.style.left = `${Math.min(rect.width - 230, Math.max(6, point.x + 12))}px`;
    tip.style.top = `${Math.max(4, point.barTop - 96)}px`;
  });
  $("activity-chart").addEventListener("mouseleave", () => { $("activity-tooltip").hidden = true; });
  $("regen-chart").addEventListener("mousemove", (event) => {
    if (!state.regenChartPoints.length) return;
    const rect = event.target.getBoundingClientRect();
    const mx = event.clientX - rect.left, my = event.clientY - rect.top;
    const nearest = state.regenChartPoints.reduce((best, point) => {
      const distance = Math.hypot(point.x - mx, point.y - my);
      return !best || distance < best.distance ? { point, distance } : best;
    }, null);
    if (!nearest || nearest.distance > 22) {
      $("regen-tooltip").hidden = true;
      return;
    }
    const point = nearest.point;
    const tip = $("regen-tooltip");
    tip.innerHTML = `Top ${number.format(point.cohortSize)} · ${escapeHtml(point.startDate)}→${escapeHtml(point.date)}<strong>재생성률 ${point.regenerationRatePct.toFixed(2)}%</strong><span>동일 플롯 ${number.format(point.matchedPlots)}/${number.format(point.cohortSize)}개 (${point.coveragePct.toFixed(1)}%)</span><span>재생성 ${exact(point.regenerationDelta)}회 · 재생성 포함 신규 ${exact(point.withRegenDelta)}회</span>`;
    tip.hidden = false;
    tip.style.left = `${Math.min(rect.width - 245, Math.max(6, point.x + 12))}px`;
    tip.style.top = `${Math.max(4, point.y - 92)}px`;
  });
  $("regen-chart").addEventListener("mouseleave", () => { $("regen-tooltip").hidden = true; });
  $("core-chart").addEventListener("mousemove", (event) => {
    if (!state.coreChartPoints.length) return;
    const rect = event.target.getBoundingClientRect();
    const mx = event.clientX - rect.left;
    const nearest = state.coreChartPoints.reduce((best, point) => {
      const distance = Math.abs(point.x - mx);
      return !best || distance < best.distance ? { point, distance } : best;
    }, null);
    if (!nearest || nearest.distance > 34) {
      $("core-tooltip").hidden = true;
      return;
    }
    const point = nearest.point;
    const tip = $("core-tooltip");
    tip.innerHTML = `${escapeHtml(point.date)}<strong>${number.format(point.corePlotCount)}개 · ${exact(point.coreTotalChats)}회</strong><span>당일 재조회 ${number.format(point.refreshedCorePlots)}개 (${point.coreCoveragePct == null ? "—" : `${point.coreCoveragePct.toFixed(1)}%`})</span><span>실제 돌파 ${number.format(point.crossedCoreCount || 0)} · 늦게 발견 ${number.format(point.discoveredCoreCount || 0)}</span>`;
    tip.hidden = false;
    tip.style.left = `${Math.min(rect.width - 230, Math.max(6, point.x + 12))}px`;
    tip.style.top = `${Math.max(4, point.lineY - 82)}px`;
  });
  $("core-chart").addEventListener("mouseleave", () => { $("core-tooltip").hidden = true; });
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
        ? `${escapeHtml(nearest.point.date)}<strong>${exact(nearest.point.value)}</strong><span>${escapeHtml(sourceLabel(nearest.point.source))}</span>`
      : state.metric === "homepageMatchedIndex"
        ? `${escapeHtml(nearest.point.date)}<strong>지수 ${exact(nearest.point.value)}</strong><span>${nearest.point.matchedPlots ? `동일 플롯 ${exact(nearest.point.matchedPlots)}개 매칭` : "기준값"}</span>`
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
    state.chartablePlots = state.data.plots
      .filter((plot) => (plot.series || []).some((point) => point.chats != null))
      .sort((a, b) => {
        const aPoints = a.series.filter((point) => point.chats != null).length;
        const bPoints = b.series.filter((point) => point.chats != null).length;
        return bPoints - aPoints || (b.chats || 0) - (a.chats || 0);
      });
    if (state.chartablePlots.length) {
      state.restoredPlotId = state.chartablePlots[0].id;
      renderPlotOptions();
    }
    if (!state.data.homepageHistory?.length) {
      state.metric = state.chartablePlots.length ? "restoredPlotChats" : "averageChatsPerPlot";
      $("metric-select").value = state.metric;
    }
    $("plot-picker").hidden = state.metric !== "restoredPlotChats";
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
