const HEALTH_TIMEZONE = 'Asia/Shanghai';
let healthCharts = { weekSteps: null, sleepWeek: null };
let healthSelectedStepsDeviceId = null;

function healthDateToday() {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: HEALTH_TIMEZONE, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date());
  const value = Object.fromEntries(parts.map(part => [part.type, part.value])); return `${value.year}-${value.month}-${value.day}`;
}
function healthAddDays(date, days) { const [y, m, d] = date.split('-').map(Number); return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10); }
function healthWeekStart(date) { const [y, m, d] = date.split('-').map(Number); return healthAddDays(date, -new Date(Date.UTC(y, m - 1, d)).getUTCDay()); }
function healthWeekLabel(date) { return `${['周日','周一','周二','周三','周四','周五','周六'][new Date(`${date}T00:00:00Z`).getUTCDay()]} ${date.slice(5)}`; }
function healthWeekDates(start) { return Array.from({ length: 7 }, (_, index) => healthAddDays(start, index)); }
function healthDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  const hours = Math.floor(seconds / 3600), minutes = Math.round((seconds % 3600) / 60);
  return hours ? `${hours}小时${minutes}分` : `${minutes}分`;
}
function healthTime(utc) {
  const value = new Date(utc); return !utc || Number.isNaN(value.getTime()) ? '—' : new Intl.DateTimeFormat('zh-CN', { timeZone: HEALTH_TIMEZONE, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(value);
}
function healthDevices(devices) {
  return Array.isArray(devices) && devices.length ? devices.map(device => healthEscape(device?.display_name || device?.device_id || '未知设备')).join('、') : '未提供设备信息';
}
function healthPlatform(platform) { return platform === 'android' ? 'Android' : platform === 'desktop' ? 'PC' : (platform || '未知平台'); }
function healthBoundaryApps(apps, devices) {
  if (!Array.isArray(apps) || !apps.length) return `应用未知 · ${healthDevices(devices)}`;
  return apps.map(item => `${healthEscape(item?.app_name || '未知应用')} · ${healthEscape(healthPlatform(item?.platform))} · ${healthEscape(item?.device_display_name || item?.device_id || '未知设备')}`).join('、');
}
async function fetchHealthInfo(date) {
  const response = await fetch(`/api/health-info?date=${encodeURIComponent(date)}`); if (!response.ok) throw new Error(`健康信息读取失败（${response.status}）`);
  return { payload: await response.json(), stale: response.headers.get('X-Life-Radio-Cache') === 'stale' };
}
function healthDevice(devices, id) {
  const ordered = (devices || []).slice().sort((a, b) => Number(b.status === 'available') - Number(a.status === 'available') || Number(b.sample_count || 0) - Number(a.sample_count || 0));
  return id ? ordered.find(device => device.device_id === id) || null : ordered[0] || null;
}
function renderTodaySteps(result) {
  const target = document.getElementById('health-steps-card'); if (!target) return;
  const devices = result?.payload?.steps?.devices || [], selected = healthDevice(devices, healthSelectedStepsDeviceId);
  if (!selected) { target.innerHTML = '<div class="health-primary">暂无步数数据</div>'; return; }
  healthSelectedStepsDeviceId = selected.device_id;
  target.innerHTML = selected.status === 'available' ? `<div class="health-primary">${Number(selected.steps).toLocaleString('zh-CN')} 步</div><div class="health-detail">${healthEscape(selected.display_name || selected.device_id)} · 主手机当日总步数</div>` : '<div class="health-primary">样本不足</div>';
}
function healthEscape(value) { return String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
function renderHealthSleep(sleep) {
  const target = document.getElementById('health-sleep-card'); if (!target) return;
  if (sleep?.status !== 'final') {
    target.innerHTML = `<div class="health-primary">${sleep?.status === 'estimating' ? '仍在估算中' : '数据不足'}</div><div class="health-detail">尚未形成有睡前与醒后边界的估算区间。</div>`; return;
  }
  target.innerHTML = `<div class="health-primary">${healthTime(sleep.estimated_start)} → ${healthTime(sleep.estimated_end)}</div><div>估算区间跨度 ${healthDuration(sleep.interval_seconds)} · 实际无交互 ${healthDuration(sleep.rest_seconds)}</div><div class="health-detail">中断 ${healthDuration(sleep.interruption_seconds)}</div><div class="health-detail">睡前最后应用：${healthBoundaryApps(sleep.last_activity_apps, sleep.last_activity_devices)}</div><div class="health-detail">起床后第一应用：${healthBoundaryApps(sleep.first_activity_apps, sleep.first_activity_devices)} · 完成时刻 ${healthTime(sleep.finalized_at)}</div>`;
}
function healthLocalParts(utc) {
  const value = new Date(utc); if (Number.isNaN(value.getTime())) return null;
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: HEALTH_TIMEZONE, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(value);
  const local = Object.fromEntries(parts.map(part => [part.type, part.value])); return { date: `${local.year}-${local.month}-${local.day}`, minutes: Number(local.hour) * 60 + Number(local.minute) };
}
function healthSleepPoint(utc, wakeDate) {
  const local = healthLocalParts(utc); if (!local) return null;
  return Math.round((Date.parse(`${local.date}T00:00:00Z`) - Date.parse(`${wakeDate}T00:00:00Z`)) / 86400000) * 1440 + local.minutes;
}
function healthAxisTime(value) {
  const rounded = Math.round(value), day = Math.floor(rounded / 1440), minutes = ((rounded % 1440) + 1440) % 1440;
  const clock = `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`;
  return day === 0 ? clock : `${day < 0 ? '前' : '+'}${Math.abs(day)}日 ${clock}`;
}
function renderWeekSteps(results, dates) {
  const target = document.getElementById('health-steps-week'); if (!target) return;
  const values = results.map((result, index) => { const device = healthDevice(result?.payload?.steps?.devices, healthSelectedStepsDeviceId); return device?.status === 'available' ? Number(device.steps) : null; });
  target.innerHTML = '<div class="health-chart-wrap"><canvas id="health-week-steps-chart"></canvas></div>';
  if (healthCharts.weekSteps) healthCharts.weekSteps.destroy();
  const canvas = document.getElementById('health-week-steps-chart'); if (!canvas || typeof Chart === 'undefined') return;
  healthCharts.weekSteps = new Chart(canvas, { type: 'bar', data: { labels: dates.map(date => healthWeekLabel(date)), datasets: [{ label: '步数', data: values, backgroundColor: chartColors.green, borderRadius: 4 }] }, options: { responsive: true, maintainAspectRatio: false, scales: { y: { beginAtZero: true, title: { display: true, text: '步数' } }, x: { grid: { display: false } } }, plugins: { legend: { display: false } } } });
}
function renderSleepWeek(results, dates) {
  const target = document.getElementById('health-sleep-week'); if (!target) return;
  if (healthCharts.sleepWeek) { healthCharts.sleepWeek.destroy(); healthCharts.sleepWeek = null; }
  const finalIntervals = results.map((result, index) => {
    const sleep = result?.payload?.sleep; const date = dates[index];
    if (sleep?.status !== 'final') return null;
    const start = healthSleepPoint(sleep.estimated_start, date), end = healthSleepPoint(sleep.estimated_end, date);
    if (!(Number.isFinite(start) && Number.isFinite(end) && end > start)) return null;
    return { date, start, end, estimated_start: sleep.estimated_start, estimated_end: sleep.estimated_end, rest_seconds: sleep.rest_seconds, interval_seconds: sleep.interval_seconds };
  }).filter(Boolean);
  target.innerHTML = `<div class="health-sleep-chart-wrap">${finalIntervals.length ? '<canvas id="health-sleep-week-chart"></canvas>' : '<div class="health-week-empty">本周暂无已完成的休息参考。</div>'}</div>`;
  const canvas = document.getElementById('health-sleep-week-chart'); if (!canvas || typeof Chart === 'undefined' || !finalIntervals.length) return;
  const min = Math.min(...finalIntervals.map(item => item.start)), max = Math.max(...finalIntervals.map(item => item.end));
  const padding = Math.max(20, Math.min(90, Math.ceil((max - min) * 0.08)));
  healthCharts.sleepWeek = new Chart(canvas, { type: 'bar', data: { labels: dates.map(date => healthWeekLabel(date)), datasets: [{ label: '估算区间', data: finalIntervals.map(item => ({ x: healthWeekLabel(item.date), y: [item.start, item.end] })), backgroundColor: chartColors.purple, borderRadius: 5, borderSkipped: false }] }, options: { responsive: true, maintainAspectRatio: false, scales: { y: { min: min - padding, max: max + padding, reverse: true, ticks: { callback: healthAxisTime }, title: { display: true, text: '以醒来日期为基准' } }, x: { grid: { display: false } } }, plugins: { legend: { display: false }, tooltip: { callbacks: { label(context) { const item = finalIntervals[context.dataIndex]; if (!item) return ''; const start = healthTime(item.estimated_start), end = healthTime(item.estimated_end); const seconds = item.rest_seconds || item.interval_seconds || 0; const h = Math.floor(seconds / 3600), m = Math.round((seconds % 3600) / 60); const dur = h ? `${h} 小时 ${m} 分钟` : `${m} 分钟`; return [`估算区间：${start} → ${end}`, `约 ${dur}`]; } } } } } });
}
async function loadHealthInfo() {
  const stale = document.getElementById('health-stale-banner'), error = document.getElementById('health-error-banner');
  try {
    const settingsResponse = await fetch('/api/settings');
    if (settingsResponse.ok) {
      const settings = await settingsResponse.json();
      if (settings.primary_health_device_id) healthSelectedStepsDeviceId = settings.primary_health_device_id;
    }
  } catch (_) { /* retain the current local selection */ }
  const today = (typeof getSelectedBusinessDate === 'function' && getSelectedBusinessDate()) || healthDateToday(), currentWeekStart = healthWeekStart(today);
  // 拉取完整一周（周日→周六）的数据，即使未来日期也会返回空数据，确保图表 x 轴固定为 7 天
  const fullWeek = healthWeekDates(currentWeekStart);
  const dates = [...new Set([...healthWeekDates(healthAddDays(currentWeekStart, -7)), ...fullWeek])].filter(date => date <= healthAddDays(currentWeekStart, 6));
  const results = await Promise.all(dates.map(async date => { try { return { date, result: await fetchHealthInfo(date) }; } catch (_) { return { date, result: null }; } }));
  const todayResult = results.find(item => item.date === today)?.result;
  if (error) { error.hidden = Boolean(todayResult); error.textContent = todayResult ? '' : '暂时无法读取健康信息，请稍后刷新。'; }
  if (stale) stale.hidden = !results.some(item => item.result?.stale);
  renderHealthSleep(todayResult?.payload?.sleep); renderTodaySteps(todayResult);
  // 固定用完整一周（周日→周六）的 labels，数据不足的日期补 null
  renderWeekSteps(fullWeek.map(date => results.find(item => item.date === date)?.result || null), fullWeek);
  const sundayFinal = results.find(item => item.date === currentWeekStart)?.result?.payload?.sleep?.status === 'final';
  const sleepWeekStart = sundayFinal ? currentWeekStart : healthAddDays(currentWeekStart, -7);
  const sleepWeek = healthWeekDates(sleepWeekStart);
  renderSleepWeek(sleepWeek.map(date => results.find(item => item.date === date)?.result || null), sleepWeek);
}

let healthInfoLoadPromise = null;
function requestHealthInfoLoad() {
  if (!healthInfoLoadPromise) {
    healthInfoLoadPromise = loadHealthInfo().finally(() => { healthInfoLoadPromise = null; });
  }
  return healthInfoLoadPromise;
}
