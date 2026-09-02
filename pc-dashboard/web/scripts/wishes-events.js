// Blacklist rules loaded from the central service via the PC proxy.
// Empty means either central returned none or the API hasn't responded yet.
let blacklistRules = { processes: [], domains: [] };
// Full rules from central (including disabled), for the management panel.
let fullRules = [];
let blacklistRulesLoaded = false;

// === 心愿与事件系统 ===
let wishState = { wishes: [], historyWishes: [], historyWishesLoaded: false, triggers: [], triggerTypes: [], loading: false, stale: false, bizDate: '', dayStartHour: 0, sharedSettings: null, devicesCache: null, deviceUsageCache: [], background: null, aiReaders: [], aiLogs: [], companionProcessRunning: false, companionProcessName: '' };
let eventsLoadGeneration = 0;
let eventsTimelineCache = null;
let eventsTimelineSignature = null;
let eventsTimelineRefreshInProgress = false;
let eventFilterState = { showNormal: true, showSystem: true };
const EVENTS_TIMELINE_REFRESH_MILLISECONDS = 30_000;
// Temporary visual acceptance switch: show the previous Life Link business day.
const EVENT_DISPLAY_BUSINESS_DAY_OFFSET = 0;

function updateLocalClock() {
  const clock = document.getElementById('local-clock');
  if (!clock) return;
  clock.textContent = new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
  }).format(new Date());
}
updateLocalClock();
setInterval(updateLocalClock, 1000);

// 业务日计算
function computeBizDate(snapshot = wishState.sharedSettings, now = new Date()) {
  const dayStart = Number(snapshot?.day_start_hour ?? 0);
  const local = new Date(now.getTime() + 8 * 3600 * 1000); // UTC+8
  if (local.getUTCHours() < dayStart) local.setUTCDate(local.getUTCDate() - 1);
  return { bizDate: local.toISOString().split('T')[0], dayStartHour: dayStart };
}

function computeEventDisplayBizDate() {
  const result = computeBizDate();
  const selected = typeof getSelectedBusinessDate === 'function' ? getSelectedBusinessDate() : '';
  if (selected) return {...result, bizDate: selected};
  const target = new Date(`${result.bizDate}T00:00:00Z`);
  target.setUTCDate(target.getUTCDate() + EVENT_DISPLAY_BUSINESS_DAY_OFFSET);
  return {...result, bizDate: target.toISOString().slice(0, 10)};
}

function bizDateToUTCWindow(bizDateStr, dayStartHour) {
  const [y, m, d] = bizDateStr.split('-').map(Number);
  // The contract currently fixes the business timezone to Asia/Shanghai.
  // Convert local day-start to UTC instead of treating the local clock as UTC.
  const startUtc = new Date(Date.UTC(y, m-1, d, dayStartHour, 0, 0) - 8 * 3600 * 1000);
  const endUtc = new Date(startUtc.getTime() + 24 * 3600 * 1000);
  return { from: startUtc.toISOString().replace('.000Z', 'Z'), to: endUtc.toISOString().replace('.000Z', 'Z') };
}

// API fns
async function fWishes() {
  const resp = await fetch('/api/wishes');
  if (!resp.ok) throw new Error(`心愿读取失败（${resp.status}）`);
  if (resp.headers.get('X-Life-Radio-Cache') === 'stale') wishState.stale = true;
  const data = await resp.json();
  wishState.wishes = Array.isArray(data) ? data : (data.wishes || []);
}
async function fTriggers() {
  const resp = await fetch('/api/event-triggers');
  if (!resp.ok) throw new Error(`提醒读取失败（${resp.status}）`);
  if (resp.headers.get('X-Life-Radio-Cache') === 'stale') wishState.stale = true;
  const data = await resp.json();
  wishState.triggers = Array.isArray(data) ? data : (data.triggers || []);
}
async function fHistoryWishes() {
  if (wishState.historyWishesLoaded) return wishState.historyWishes;
  const resp = await fetch('/api/wishes?include_archived=true');
  if (!resp.ok) throw new Error(`往期心愿读取失败（${resp.status}）`);
  if (resp.headers.get('X-Life-Radio-Cache') === 'stale') wishState.stale = true;
  const data = await resp.json();
  const wishes = Array.isArray(data) ? data : (data.wishes || []);
  // 非 active 状态代表已结束的心愿；保留其固定每日评估，但不带入触发器。
  wishState.historyWishes = wishes.filter(wish => wish.status !== 'active');
  wishState.historyWishesLoaded = true;
  return wishState.historyWishes;
}
async function fTriggerTypes() {
  const resp = await fetch('/api/trigger-types');
  if (!resp.ok) throw new Error(`提醒类型读取失败（${resp.status}）`);
  if (resp.headers.get('X-Life-Radio-Cache') === 'stale') wishState.stale = true;
  const data = await resp.json();
  wishState.triggerTypes = data.trigger_types || [];
}
async function fSharedSettings() {
  const resp = await fetch('/api/settings');
  if (!resp.ok) throw new Error('共享跨日设置不可用');
  if (resp.headers.get('X-Life-Radio-Cache') === 'stale') wishState.stale = true;
  const data = await resp.json();
  if (!Number.isInteger(data.day_start_hour) || data.day_start_hour < 0 || data.day_start_hour > 23) {
    throw new Error('共享跨日设置响应无效');
  }
  wishState.sharedSettings = data;
}
async function fTimeline() {
  const biz = computeEventDisplayBizDate();
  const win = bizDateToUTCWindow(biz.bizDate, biz.dayStartHour);
  const resp = await fetch(`/api/timeline-events?from=${encodeURIComponent(win.from)}&to=${encodeURIComponent(win.to)}`);
  if (!resp.ok) throw new Error(`时间线读取失败（${resp.status}）`);
  if (resp.headers.get('X-Life-Radio-Cache') === 'stale') wishState.stale = true;
  const data = await resp.json();
  const nextEvents = data.events || [];
  const nextSignature = JSON.stringify(nextEvents);
  const changed = eventsTimelineSignature !== nextSignature;
  if (changed) eventsTimelineCache = nextEvents;
  eventsTimelineSignature = nextSignature;
  return changed;
}
async function fEventBackground() {
  const biz = computeEventDisplayBizDate();
  const resp = await fetch(`/api/event-background?business_date=${encodeURIComponent(biz.bizDate)}`);
  if (!resp.ok) throw new Error(`背景摘要读取失败（${resp.status}）`);
  if (resp.headers.get('X-Life-Radio-Cache') === 'stale') wishState.stale = true;
  const data = await resp.json();
  if (!data || !data.background_summary || !data.ai_understanding) throw new Error('背景摘要响应无效');
  wishState.background = data;
}
async function fDevices() {
  const date = computeEventDisplayBizDate().bizDate;
  const [deviceResp, usageResp] = await Promise.all([
    fetch('/api/devices'), fetch(`/api/usage?date=${encodeURIComponent(date)}`),
  ]);
  if (!deviceResp.ok) throw new Error(`设备摘要读取失败（${deviceResp.status}）`);
  const deviceData = await deviceResp.json();
  const usageData = usageResp.ok ? await usageResp.json() : {};
  const remoteDevices = deviceData.devices || [];
  const list = deviceData.local?.device_id ? [deviceData.local, ...remoteDevices] : remoteDevices;
  wishState.devicesCache = list;
  wishState.deviceUsageCache = usageData.devices || [];
  return list;
}
async function fDeviceManagementNames() {
  const response = await fetch('/api/device-management');
  if (!response.ok) return;
  const data = await response.json();
  const managedNames = new Map((data.devices || [])
    .filter(item => item && item.device_id)
    .map(item => [String(item.device_id), item.display_name || item.custom_name || item.device_id]));
  (wishState.devicesCache || []).forEach(device => {
    const managedName = managedNames.get(String(device.device_id));
    if (managedName) device.display_name = managedName;
  });
}
async function fAIReaders() {
  const resp = await fetch('/api/ai-readers');
  if (!resp.ok) throw new Error(`AI 读取状态不可用（${resp.status}）`);
  const data = await resp.json();
  wishState.aiReaders = data.readers || [];
  const reader = wishState.aiReaders.find(item => item.status === 'active') || null;
  if (reader) {
    const [logsResult, processResult] = await Promise.allSettled([
      fetch(`/api/ai-readers/${encodeURIComponent(reader.reader_id)}/access-logs?limit=10`),
      fetch(`/api/ai-readers/${encodeURIComponent(reader.reader_id)}/process-status`),
    ]);
    if (logsResult.status === 'fulfilled' && logsResult.value.ok) {
      const logs = await logsResult.value.json().catch(() => ({}));
      wishState.aiLogs = logs.logs || [];
    } else wishState.aiLogs = [];
    if (processResult.status === 'fulfilled' && processResult.value.ok) {
      const processStatus = await processResult.value.json().catch(() => ({}));
      wishState.companionProcessRunning = processStatus.process_running === true;
      wishState.companionProcessName = String(processStatus.process_display_name || '');
    } else {
      wishState.companionProcessRunning = false;
      wishState.companionProcessName = '';
    }
  } else {
    wishState.aiLogs = [];
    wishState.companionProcessRunning = false;
    wishState.companionProcessName = '';
  }
}

function showEventsLoadWarning(message) {
  const errorBanner = document.getElementById('events-error-banner');
  if (!errorBanner) return;
  const messages = new Set((errorBanner.dataset.messages || '').split('\n').filter(Boolean));
  messages.add(message);
  errorBanner.dataset.messages = Array.from(messages).join('\n');
  errorBanner.textContent = '部分内容暂不可用：' + Array.from(messages).join('；');
  errorBanner.style.display = 'block';
}

function refreshEventsStaleBanner() {
  const staleBanner = document.getElementById('events-stale-banner');
  if (staleBanner) staleBanner.style.display = wishState.stale ? 'block' : 'none';
}

async function loadEventsSecondaryPanels(generation) {
  try {
    await fDevices();
    if (generation !== eventsLoadGeneration) return;
    renderTodayDeviceConnections();
    refreshEventsStaleBanner();
    await fDeviceManagementNames();
    if (generation !== eventsLoadGeneration) return;
    renderTodayDeviceConnections();
  } catch (error) {
    if (generation === eventsLoadGeneration) {
      wishState.devicesCache = [];
      wishState.deviceUsageCache = [];
      renderTodayDeviceConnections();
      showEventsLoadWarning(error.message || '设备摘要读取失败');
    }
  }
  try {
    await fAIReaders();
    if (generation === eventsLoadGeneration) renderAIReaderPanel();
  } catch (error) {
    if (generation === eventsLoadGeneration) showEventsLoadWarning(error.message || 'AI 管理状态读取失败');
  }
  fTriggerTypes().catch(() => {});
}

// 主加载：共享边界确认后只等待首屏核心；设备摘要与管理诊断渐进补齐。
async function loadEventsTimeline() {
  const generation = ++eventsLoadGeneration;
  wishState.loading = true; wishState.stale = false;
  const staleBanner = document.getElementById('events-stale-banner');
  if (staleBanner) staleBanner.style.display = 'none';
  const errorBanner = document.getElementById('events-error-banner');
  if (errorBanner) { errorBanner.style.display = 'none'; errorBanner.dataset.messages = ''; }
  const loadingEl = document.getElementById('events-timeline-loading');
  if (loadingEl) loadingEl.style.display = 'block';
  try {
    // Timeline uses the current shared boundary; wish chips use each wish's
    // immutable business_day_snapshot below.
    await fSharedSettings();
    // Date-bounded timeline must use the freshly confirmed shared business
    // boundary. Other core resources are independent and may fail separately.
    const core = await Promise.allSettled([fWishes(), fTriggers(), fTimeline()]);
    const labels = ['心愿', '提醒', '时间线'];
    core.forEach((result, index) => {
      if (result.status !== 'rejected') return;
      if (index === 0) wishState.wishes = [];
      if (index === 1) wishState.triggers = [];
      if (index === 2) {
        eventsTimelineCache = [];
        eventsTimelineSignature = null;
      }
      showEventsLoadWarning(`${labels[index]}读取失败`);
    });
  } catch (e) {
    showEventsLoadWarning(e.message || '共享设置读取失败');
  }
  if (generation !== eventsLoadGeneration) return;
  const biz = computeEventDisplayBizDate(); wishState.bizDate = biz.bizDate; wishState.dayStartHour = biz.dayStartHour;
  const dateLabel = document.getElementById('events-date-label');
  if (dateLabel) dateLabel.textContent = biz.bizDate;
  renderEventSettings(); renderWishCards(); renderEventsTimeline(); renderBizDayTimeline();
  if (wishState.stale && staleBanner) staleBanner.style.display = 'block';
  if (loadingEl) loadingEl.style.display = 'none';
  wishState.loading = false;
  loadEventsSecondaryPanels(generation).catch(console.warn);
}

// 小窗和 WebUI 使用同一个固定业务日 URL。浏览器只重读时间线并重绘，
// 后端会把它与小窗的 30 秒读取合并到同一份进程内缓存，不重复加载整页资源。
async function refreshEventsTimelineFromSharedCache() {
  if (
    eventsTimelineRefreshInProgress || wishState.loading ||
    !wishState.sharedSettings || eventsTimelineCache === null
  ) return;
  if (typeof isHistoricalDataView === 'function' && isHistoricalDataView()) return;
  const page = document.getElementById('page-timeline-events');
  if (!page?.classList.contains('active') || document.visibilityState === 'hidden') return;

  const biz = computeEventDisplayBizDate();
  if (wishState.bizDate && biz.bizDate !== wishState.bizDate) {
    await loadEventsTimeline();
    return;
  }

  eventsTimelineRefreshInProgress = true;
  try {
    const changed = await fTimeline();
    if (!changed) return;
    renderEventsTimeline();
    renderBizDayTimeline();
  } catch (error) {
    console.warn('事件时间线自动刷新失败', error);
  } finally {
    eventsTimelineRefreshInProgress = false;
  }
}

setInterval(
  () => refreshEventsTimelineFromSharedCache(),
  EVENTS_TIMELINE_REFRESH_MILLISECONDS,
);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') refreshEventsTimelineFromSharedCache();
});

// 事件时间线渲染
function renderEventsTimeline() {
  const container = document.getElementById('events-timeline-container');
  if (!container) return;
  const events = eventsTimelineCache || [];
  if (!events.length) {
    const date = wishState.bizDate || (typeof getSelectedBusinessDate === 'function' ? getSelectedBusinessDate() : '');
    const label = date ? `${Number(date.slice(5, 7))}月${Number(date.slice(8, 10))}日没有事件记录` : '该业务日没有事件记录';
    container.innerHTML = `<div style="color:var(--text-secondary);padding:20px 0;text-align:center">${label}</div>`;
    return;
  }
  let html = '<div class="events-timeline">';
  for (const e of events) {
    const tone = e.importance === 'high' ? 'high' : (e.category === 'system' ? 'system' : 'normal');
    if (!eventIsVisible(e)) continue;
    const occurred = new Date(e.occurred_at || '');
    const time = Number.isNaN(occurred.getTime()) ? '--:--' : new Intl.DateTimeFormat('zh-CN', {
      timeZone: wishState.sharedSettings?.timezone || 'Asia/Shanghai',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
    }).format(occurred);
    const high = e.importance === 'high';
    const title = eventDisplayTitle(e);
    const icon = eventIcon(e);
    const detail = eventDisplayDetail(e);
    const fullDetail = detail ? eventDetailHtml(detail) : '';
    const collapsible = detail && detail.split('\n').length > 3;
    const reportBody = (e.event_key === 'report.morning' || e.event_key === 'report.evening' || e.event_key === 'report.periodic') && e.evidence && typeof e.evidence.body === 'string' ? e.evidence.body : '';
    const aiState = e.importance === 'low' ? 'not_applicable' : (e.ai_reader?.state || 'not_served');
    const aiName = e.ai_reader?.reader_display_name || '';
    const aiMark = aiState === 'served'
      ? '<span class="event-ai-mark served" title="已推送给 ' + escapeHtml(aiName || 'AI') + '" aria-label="已推送给 ' + escapeHtml(aiName || 'AI') + '">已推送</span>'
      : (aiState === 'not_applicable'
        ? ''
        : '<span class="event-ai-mark pending" title="' + (aiName ? '待推送给 ' + escapeHtml(aiName) : '尚未连接 AI') + '" aria-label="' + (aiName ? '待推送给 ' + escapeHtml(aiName) : '尚未连接 AI') + '">待推送</span>');
    html += '<div class="event-item event-' + tone + '">'
      + '<div class="event-dot" aria-hidden="true" style="background:' + icon.color + '"><i data-lucide="' + icon.icon + '"></i></div>'
      + '<div class="event-time">' + escapeHtml(time) + '</div>'
      + '<div class="event-body">'
      + '<div class="event-title-row"><div class="event-title"><span class="event-icon-tag" style="background:' + icon.color + '" title="' + escapeHtml(icon.label) + '" aria-label="' + escapeHtml(icon.label) + '">' + escapeHtml(icon.label) + '</span> ' + escapeHtml(title) + (high ? ' <span class="event-star" title="应优先关注">⭐</span>' : '') + (reportBody ? ' <button class="report-body-btn" type="button" data-index="' + events.indexOf(e) + '">查看原文本</button>' : '') + '</div>' + aiMark + '</div>'
      + (detail ? '<div class="event-detail' + (collapsible ? ' is-collapsed' : '') + '">' + fullDetail + '</div>' : '')
      + (collapsible ? '<button class="event-detail-toggle" type="button">展开详情</button>' : '')
      + '</div></div>';
  }
  html += '</div>';
  container.innerHTML = html;
  if (typeof lucide !== 'undefined') lucide.createIcons();
  container.querySelectorAll('.event-detail-toggle').forEach(button => button.addEventListener('click', () => {
    const detail = button.previousElementSibling;
    detail.classList.toggle('is-collapsed');
    button.textContent = detail.classList.contains('is-collapsed') ? '展开详情' : '收起详情';
  }));
  container.querySelectorAll('.report-body-btn').forEach(button => button.addEventListener('click', () => {
    const idx = Number(button.dataset.index);
    const event = eventsTimelineCache[idx];
    if (event && event.evidence && typeof event.evidence.body === 'string') showReportBody(event.evidence.body, eventDisplayTitle(event));
  }));
}

function localDateTime(value) {
  const date = new Date(value || '');
  return Number.isNaN(date.getTime()) ? '—' : new Intl.DateTimeFormat('zh-CN', {
    timeZone: wishState.sharedSettings?.timezone || 'Asia/Shanghai',
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
  }).format(date);
}

function localMonthDay(value) {
  const date = new Date(value || '');
  return Number.isNaN(date.getTime()) ? '—' : new Intl.DateTimeFormat('zh-CN', {
    timeZone: wishState.sharedSettings?.timezone || 'Asia/Shanghai',
    month: 'long', day: 'numeric'
  }).format(date);
}

function renderAIReaderPanel() {
  const container = document.getElementById('ai-reader-container');
  if (!container) return;
  const reader = wishState.aiReaders.find(item => item.status === 'active') || null;
  const clearTop = document.getElementById('ai-progress-clear-top');
  if (clearTop) { clearTop.hidden = !reader; clearTop.onclick = null; }
  let html = '<div class="ai-reader-content">';
  if (!reader) {
    html += '<div class="ai-reader-empty"><strong class="ai-reader-title">AI伴侣待接入</strong><p>连接后，AI 可以读取今日的事件序列和多方面的采集信息。</p></div>';
  } else {
    const latestLog = wishState.aiLogs[0] || null;
    const servedCount = latestLog && Array.isArray(latestLog.served_event_ids) ? latestLog.served_event_ids.length : 0;
    const recentAccess = latestLog
      ? '最近访问：' + localDateTime(latestLog.requested_at) + ' · 已提供 ' + servedCount + ' 条事件'
      : '最近访问：暂无记录';
    const processName = wishState.companionProcessName;
    const normalizedReaderName = String(reader.display_name || '').toLocaleLowerCase();
    const normalizedProcessName = processName.toLocaleLowerCase();
    const processProductName = normalizedProcessName.trim().split(/\s+/)[0] || '';
    const displayNameHasProcess = processName && (
      normalizedReaderName.includes(normalizedProcessName)
      || (processProductName.length >= 4 && normalizedReaderName.includes(processProductName))
    );
    const companionSuffix = wishState.companionProcessRunning && processName && !displayNameHasProcess ? ' (' + processName + ')' : '';
    html += '<div class="ai-reader-status"><strong class="ai-reader-title">已连接 AI：' + escapeHtml(reader.display_name) + escapeHtml(companionSuffix) + '</strong>'
      + '<span class="ai-reader-token">Token 已配对 <span class="ai-reader-expiry">到期：' + escapeHtml(localMonthDay(reader.token_expires_at)) + '</span></span>'
      + (wishState.companionProcessRunning && processName ? '<span class="ai-reader-detection"><span class="ai-reader-detection-dot" aria-hidden="true"></span>检测到 ' + escapeHtml(processName) + ' 进程正在运行</span>' : '')
      + '</div><div class="ai-reader-recent">' + escapeHtml(recentAccess) + '</div>';
  }
  html += '<div class="ai-reader-actions"><button type="button" id="ai-pairing-create" hidden aria-hidden="true" tabindex="-1">生成 AI 配对文本</button>'
    + '<button type="button" class="package" id="ai-connection-package-open">生成 AI 配对包</button>'
    + '<button type="button" class="skill" id="ai-skill-open">查看 Skill</button></div></div>';
  container.innerHTML = html;
  document.getElementById('ai-pairing-create')?.addEventListener('click', async () => {
    const button = document.getElementById('ai-pairing-create'); button.disabled = true;
    try {
      const response = await fetch('/api/ai-readers/pairings', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.pairing_text) throw new Error(data.message || '配对文本生成失败');
      await navigator.clipboard.writeText(data.pairing_text);
      showToast('AI 配对文本已复制，24 小时内有效', 'ok');
    } catch (error) { showToast('生成 AI 配对文本失败：' + error.message, 'err'); }
    finally { button.disabled = false; }
  });
  document.getElementById('ai-skill-open')?.addEventListener('click', async () => {
    const button = document.getElementById('ai-skill-open'); button.disabled = true;
    try {
      const response = await fetch('/api/ai-reader-skill/open', {method:'POST'});
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.message || `HTTP ${response.status}`);
      showToast('已打开可提供给 AI 的 Skill 副本', 'ok');
    } catch (error) { showToast('打开 Skill 失败：' + error.message, 'err'); }
    finally { button.disabled = false; }
  });
  document.getElementById('ai-connection-package-open')?.addEventListener('click', async () => {
    const button = document.getElementById('ai-connection-package-open'); button.disabled = true;
    try {
      const response = await fetch('/api/ai-reader-connection-package/open', {method:'POST'});
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.message || `HTTP ${response.status}`);
      showToast('AI 配对包已生成。将压缩包发送给 AI 来完成连接。', 'ok');
    } catch (error) { showToast('生成 AI 配对包失败：' + error.message, 'err'); }
    finally { button.disabled = false; }
  });
  const clearProgress = async () => {
    if (!reader || !confirm('清理当前 AI 的已提供标记，并让它下次重新读取当前业务日全部事件？')) return;
    const response = await fetch(`/api/ai-readers/${encodeURIComponent(reader.reader_id)}/clear-reading-progress`, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { showToast('清理失败：' + (data.message || response.status), 'err'); return; }
    showToast('标记已清理，AI 下次将重新读取今日', 'ok');
    await Promise.all([fAIReaders(), fTimeline()]); renderAIReaderPanel(); renderEventsTimeline(); renderBizDayTimeline();
  };
  if (clearTop && reader) clearTop.onclick = clearProgress;
}

function renderTodayDeviceConnections() {
  const container = document.getElementById('today-device-connections');
  if (!container) return;
  const devices = wishState.devicesCache || [];
  const onlineDevices = devices.filter(device => device.status === 'connected');
  if (!onlineDevices.length) { container.innerHTML = ''; return; }
  const usageByKey = new Map((wishState.deviceUsageCache || []).map(item => [item.device_key || item.device_id, item]));
  const connectionSeconds = device => {
    const source = usageByKey.get(device.device_key || device.device_id) || device;
    const candidates = [source.today, source.window, source].filter(Boolean);
    for (const item of candidates) {
      for (const key of ['online_seconds', 'connected_seconds', 'connection_seconds']) {
        if (Number.isFinite(Number(item[key]))) return Number(item[key]);
      }
      if (item.hourly_online && typeof item.hourly_online === 'object') {
        return Object.values(item.hourly_online).reduce((sum, value) => sum + Number(value || 0), 0);
      }
    }
    return null;
  };
  const durationText = seconds => {
    if (seconds === null) return '运行时长不可用';
    const totalMinutes = Math.floor(Math.max(0, seconds) / 60);
    return `已运行 ${Math.floor(totalMinutes / 60)} 小时 ${totalMinutes % 60} 分钟`;
  };
  container.innerHTML = onlineDevices.map(device => '<div class="today-device-chip">'
    + '<strong>' + escapeHtml(device.display_name || device.device_id || '未命名设备') + '</strong>'
    + '<span aria-hidden="true">·</span><span class="device-connection-status connected"><span class="device-connection-dot"></span>已连接</span>'
    + '<span class="device-last-seen">' + escapeHtml(durationText(connectionSeconds(device))) + '</span></div>').join('');
}

document.getElementById('ai-context-preview')?.addEventListener('click', async () => {
  const reader = wishState.aiReaders.find(item => item.status === 'active') || null;
  if (!reader) { showToast('请先完成 AI 配对', 'err'); return; }
  const button = document.getElementById('ai-context-preview'); button.disabled = true;
  const previewBox = showReportBody('正在获取发送原文，请稍候…', 'AI 下一次读取原文（预览）');
  try {
    const response = await fetch(`/api/ai-readers/${encodeURIComponent(reader.reader_id)}/context-preview`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || `HTTP ${response.status}`);
    previewBox.setBody(JSON.stringify(data.context || data, null, 2));
  } catch (error) {
    previewBox.setBody('获取发送原文失败：' + error.message);
    showToast('读取发送原文失败：' + error.message, 'err');
  }
  finally { button.disabled = false; }
});

function showReportBody(body, title) {
  let currentBody = String(body || '');
  const overlay = document.createElement('div'); overlay.className = 'wish-form-overlay';
  overlay.innerHTML = '<div class="wish-form-box report-body-box"><h3>' + escapeHtml(title || '报告原文本') + ' <span style="cursor:pointer;float:right;font-size:22px" id="rb-close">✕</span></h3>'
    + '<pre class="report-body-pre">' + escapeHtml(currentBody) + '</pre>'
    + '<div class="form-btns"><button class="btn-primary" id="rb-copy">复制文本</button><button class="btn-cancel" id="rb-close2">关闭</button></div></div>';
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  document.getElementById('rb-close').addEventListener('click', close);
  document.getElementById('rb-close2').addEventListener('click', close);
  overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
  document.getElementById('rb-copy').addEventListener('click', () => {
    navigator.clipboard.writeText(currentBody).then(() => { showToast('已复制', 'ok'); }).catch(() => { showToast('复制失败', 'err'); });
  });
  return {
    setBody(value) {
      currentBody = String(value || '');
      const pre = overlay.querySelector('.report-body-pre');
      if (pre) pre.textContent = currentBody;
    }
  };
}

function eventIcon(event) {
  const icons = {
    'wish.scheduled_reminder':        ['target',        '心愿定时提醒', '#F59E0B'],
    'system.device_usage_milestone':  ['monitor',       '设备使用',     '#3B82F6'],
    'system.blacklist_usage_milestone': ['shield-alert', '黑名单',       '#EF4444'],
    'system.location_stay_milestone': ['map-pin',       '位置',         '#8B5CF6'],
    'system.activity_duration_milestone': ['activity',  '活动',         '#10B981'],
    'system.late_online_check':       ['moon',          '晚睡',         '#6366F1'],
    'report.morning':                 ['sunrise',       '今日早报',     '#F97316'],
    'report.evening':                 ['sunset',        '今日晚报',     '#EC4899'],
    'report.periodic':                ['clock',         '定时总结',     '#14B8A6'],
  };
  const pair = icons[event.event_key];
  if (pair) return {icon: pair[0], label: pair[1], color: pair[2]};
  if (event.category === 'system') return {icon: 'settings', label: 'Life Link 系统', color: '#6B7280'};
  if (event.wish_id) return {icon: 'target', label: '心愿关联提醒', color: '#F59E0B'};
  return {icon: 'zap', label: '事件', color: '#3B82F6'};
}

function toggleEventFilter(type) {
  eventFilterState[type === 'normal' ? 'showNormal' : 'showSystem'] = !eventFilterState[type === 'normal' ? 'showNormal' : 'showSystem'];
  const btn = document.getElementById('event-filter-' + type);
  if (btn) btn.classList.toggle('off', !eventFilterState[type === 'normal' ? 'showNormal' : 'showSystem']);
  renderEventsTimeline();
  renderBizDayTimeline(); // 让时间轴标记同步半透明
}

function eventIsVisible(e) {
  const tone = e.importance === 'high' ? 'high' : (e.category === 'system' ? 'system' : 'normal');
  if (tone === 'normal' && !eventFilterState.showNormal) return false;
  if (tone === 'system' && !eventFilterState.showSystem) return false;
  return true;
}

function eventDisplayTitle(event) {
  const title = String(event.title || '未命名事件');
  return event.event_key === 'wish.scheduled_reminder' && !title.startsWith('心愿提醒·') ? `心愿提醒·${title}` : title;
}
function eventDisplayDetail(event) {
  if (event.delivery && typeof event.delivery === 'object') {
    const reportKind = String(event.evidence?.report_kind || '').replace('report.', '');
    const label = { morning: '今日早报', evening: '今日晚报', periodic: event.title || '定时总结' }[reportKind] || event.title || '报告';
    const target = event.delivery.target_display_name || 'AI';
    const phrase = { pending: '正在发送…', sent: '已成功发送！', not_configured: '等待接入。', failed: '发送失败。' }[event.delivery.state];
    if (phrase) {
      if (event.delivery.state === 'not_configured') return `${label}已准备就绪。等待 ${target} 接入。`;
      return `${label}发送至 ${target}。${phrase}`;
    }
  }
  return String(event.detail || '');
}

function eventDetailHtml(detail) {
  return escapeHtml(String(detail || ''))
    .replace(/(\d+小时(?:\d+分钟)?|\d+分钟)/g, '<span class="event-duration-emphasis">$1</span>')
    .replace(/\n/g, '<br>');
}

function renderEventBackground() {
  const panel = document.getElementById('event-background-panel');
  const summary = document.getElementById('event-background-summary');
  const guide = document.getElementById('event-ai-understanding');
  const background = wishState.background;
  if (!panel || !summary || !guide || !background) return;
  const sections = ['wish', 'device_and_apps', 'blacklist', 'location_and_activity']
    .map(key => background.background_summary[key]).filter(Boolean)
    .filter(section => Array.isArray(section.items) && section.items.length);
  const summarySections = sections.map(section => '<section class="background-section"><h4>' + escapeHtml(section.title) + '</h4><ul>'
    + section.items.map(item => '<li>' + escapeHtml(item.text || '') + '</li>').join('') + '</ul></section>');
  const realTimeItems = Array.isArray(background.real_time_items) ? background.real_time_items : [];
  const currentStatus = realTimeItems
    .filter(item => !(item && item.is_stale && (item.kind === 'device_online' || item.kind === 'current_app')))
    .filter(item => item && typeof item.display_text === 'string' && item.display_text.trim())
    .map(formatRealTimeBackgroundItem);
  if (currentStatus.length) {
    summarySections.unshift('<section class="background-section background-current-status"><h4>当前状态</h4><ul>'
      + currentStatus.map(text => '<li>' + escapeHtml(text) + '</li>').join('') + '</ul></section>');
  }
  const nowLine = (background.generated_at_label && String(background.generated_at_label).trim()) || '';
  if (nowLine) {
    summarySections.unshift('<div class="background-now">当前时间 ' + escapeHtml(nowLine) + '</div>');
  }
  summary.innerHTML = summarySections.join('') || '<p class="event-muted">当前业务日暂无背景事实。</p>';
  guide.innerHTML = '<h4>' + escapeHtml(background.ai_understanding.title || 'AI 理解说明') + '</h4><ul>'
    + (background.ai_understanding.items || []).map(item => '<li>' + escapeHtml(item.text || '') + '</li>').join('') + '</ul>';
  panel.style.display = 'block';
}

function formatRealTimeBackgroundItem(item) {
  const text = item.display_text.trim();
  if (!item.is_stale || /上次更新\s*[:：]/.test(text)) return text;
  const observed = new Date(item.observed_at || '');
  if (Number.isNaN(observed.getTime())) return text;
  const time = new Intl.DateTimeFormat('zh-CN', {
    timeZone: wishState.sharedSettings?.timezone || 'Asia/Shanghai',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).format(observed);
  return `${text}（上次更新：${time}）`;
}

function renderEventSettings() {
  const container = document.getElementById('event-settings-container');
  const settings = wishState.sharedSettings;
  if (!container || !settings) return;
  if (!container._settingsHome) container._settingsHome = container.parentElement;
  const openButton = document.getElementById('event-settings-open');
  if (openButton) {
    openButton.onclick = () => {
      if (document.getElementById('event-settings-modal')) return;
      const overlay = document.createElement('div');
      overlay.id = 'event-settings-modal';
      overlay.className = 'wish-form-overlay';
      overlay.innerHTML = '<div class="wish-form-box event-settings-dialog"><h3>事件与报告设置 <span class="event-settings-close" role="button" tabindex="0" aria-label="关闭">✕</span></h3><div class="event-settings-modal-content"></div></div>';
      document.body.appendChild(overlay);
      const content = overlay.querySelector('.event-settings-modal-content');
      content.appendChild(container);
      const close = () => { container._settingsHome.appendChild(container); overlay.remove(); };
      overlay.querySelector('.event-settings-close').addEventListener('click', close);
      overlay.addEventListener('click', event => { if (event.target === overlay) close(); });
    };
  }
  const disabled = wishState.stale ? ' disabled' : '';
  const morning = settings.morning_report || {};
  const evening = settings.evening_report || {};
  const periodic = settings.periodic_summary || {};
  container.innerHTML = '<form id="event-settings-form" class="event-settings-form">'
    + '<label>睡觉时间 <input name="sleep_local_time" type="time" value="' + escapeHtml(settings.sleep_local_time || '23:00') + '"' + disabled + '></label>'
    + '<fieldset class="morning-report-settings"><legend>今日早报</legend>'
    + '<label class="morning-mode-row"><input name="morning_mode" type="radio" value="after_first_usage"' + (morning.enabled && morning.mode === 'after_first_usage' ? ' checked' : '') + disabled + '>首次使用设备 <span>延迟（分钟）</span><input name="morning_delay" type="number" min="1" max="720" value="' + Number(morning.delay_minutes || 60) + '"' + disabled + '></label>'
    + '<label class="morning-mode-row"><input name="morning_mode" type="radio" value="fixed_time"' + (morning.enabled && morning.mode === 'fixed_time' ? ' checked' : '') + disabled + '>固定时间 <input name="morning_time" type="time" value="' + escapeHtml(morning.local_time || '09:00') + '"' + disabled + '></label></fieldset>'
    + '<fieldset><legend>今日晚报</legend><label><input name="evening_enabled" type="checkbox"' + (evening.enabled ? ' checked' : '') + disabled + '> 启用</label><label>时间 <input name="evening_time" type="time" value="' + escapeHtml(evening.local_time || '23:00') + '"' + disabled + '></label></fieldset>'
    + '<fieldset><legend>定时总结</legend><label><input name="periodic_enabled" type="checkbox"' + (periodic.enabled ? ' checked' : '') + disabled + '> 启用</label><label>开始 <input name="periodic_start" type="time" value="' + escapeHtml(periodic.start_local_time || '10:00') + '"' + disabled + '></label><label>结束 <input name="periodic_end" type="time" value="' + escapeHtml(periodic.end_local_time || '22:00') + '"' + disabled + '></label><label>周期 <select name="periodic_interval"' + disabled + '><option value="30">30 分钟</option><option value="60">1 小时</option><option value="120">2 小时</option><option value="180">3 小时</option><option value="240">4 小时</option></select></label><div class="periodic-times" id="periodic-times" aria-live="polite"></div></fieldset>'
    + '<button type="submit" class="btn-primary"' + disabled + '>保存设置</button>' + (wishState.stale ? '<span class="event-offline-note">当前离线中，设置不可修改。</span>' : '') + '</form>';
  const interval = container.querySelector('[name=periodic_interval]'); interval.value = String(periodic.interval_minutes || 120);
  const form = container.querySelector('form');
  form.querySelectorAll('input[name=morning_mode]').forEach(radio => {
    radio.addEventListener('pointerdown', () => { radio.dataset.wasChecked = radio.checked ? '1' : '0'; });
    radio.addEventListener('click', () => {
      if (radio.dataset.wasChecked === '1') radio.checked = false;
      delete radio.dataset.wasChecked;
    });
  });
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (wishState.stale) return;
    const values = new FormData(form); const selectedMode = values.get('morning_mode');
    const mode = selectedMode || morning.mode || 'after_first_usage';
    const payload = {
      sleep_local_time: values.get('sleep_local_time'),
      morning_report: { enabled: !!selectedMode, mode, delay_minutes: Number(values.get('morning_delay')), local_time: mode === 'fixed_time' ? values.get('morning_time') : null },
      evening_report: { enabled: values.has('evening_enabled'), local_time: values.get('evening_time') },
      periodic_summary: { enabled: values.has('periodic_enabled'), start_local_time: values.get('periodic_start'), end_local_time: values.get('periodic_end'), interval_minutes: Number(values.get('periodic_interval')) },
    };
    try {
      const response = await fetch('/api/settings', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
      if (!response.ok) throw new Error((await response.json().catch(() => ({}))).message || '设置保存失败');
      wishState.sharedSettings = await response.json(); showToast('设置已保存', 'ok'); renderEventSettings();
    } catch (error) { showToast('设置保存失败：' + error.message, 'err'); }
  });
  updatePeriodicTimes(form);
  form.querySelectorAll('[name=periodic_start], [name=periodic_end], [name=periodic_interval], [name=periodic_enabled]').forEach(el => {
    const type = el.tagName === 'SELECT' ? 'change' : 'input';
    el.addEventListener(type, () => updatePeriodicTimes(form));
  });
}

function periodicTriggerTimes(startHHMM, endHHMM, intervalMinutes) {
  const toMin = hhmm => { const parts = String(hhmm || '').split(':'); if (parts.length !== 2) return NaN; const h = Number(parts[0]); const m = Number(parts[1]); return (Number.isFinite(h) && Number.isFinite(m)) ? h * 60 + m : NaN; };
  const toHHMM = total => { const t = ((total % (24 * 60)) + 24 * 60) % (24 * 60); return String(Math.floor(t / 60)).padStart(2, '0') + ':' + String(t % 60).padStart(2, '0'); };
  const start = toMin(startHHMM), end = toMin(endHHMM);
  const interval = Number(intervalMinutes);
  if (Number.isNaN(start) || Number.isNaN(end) || !Number.isFinite(interval) || interval <= 0) return [];
  let span = end - start;
  if (span < 0) span += 24 * 60;
  const n = Math.floor(span / interval);
  if (n <= 0 || n > 24 * 60) return [];
  // Inclusive of end: every slot start + k*interval that stays <= end is kept.
  const count = n + 1;
  const times = [];
  for (let k = 0; k < count; k++) times.push(toHHMM(start + k * interval));
  return times;
}

function updatePeriodicTimes(form) {
  const el = document.getElementById('periodic-times');
  if (!el) return;
  const enabled = form.querySelector('[name=periodic_enabled]').checked;
  if (!enabled) { el.textContent = ''; return; }
  const start = form.querySelector('[name=periodic_start]').value;
  const end = form.querySelector('[name=periodic_end]').value;
  const interval = form.querySelector('[name=periodic_interval]').value;
  const times = periodicTriggerTimes(start, end, interval);
  if (!times.length) { el.textContent = '将触发：暂无时间点'; return; }
  const MAX_SHOWN = 8;
  const shown = times.slice(0, MAX_SHOWN);
  const omitted = times.length > MAX_SHOWN;
  const text = '将触发：' + shown.join(' ') + (omitted ? ' …… ' + times[times.length - 1] : '');
  el.textContent = text;
}

// 心愿卡片
function getWishTrigger(wishId) { return wishState.triggers.find(t => t.wish_id === wishId && t.enabled); }
function getDayStatus(wish, day) { const biz = computeBizDate(wish.business_day_snapshot).bizDate; const date = day.business_date; if (date > biz) return 'future'; if (day.evaluation === 'completed') return 'completed'; if (day.evaluation === 'not_completed') return 'not_completed'; if (date === biz) return 'today'; return 'pending'; }
function canCompleteWish(wish) { return wish.status === 'active' && computeBizDate(wish.business_day_snapshot).bizDate > wish.ends_on; }
function formatWishDate(date) { const parts = String(date).split('-'); return parts.length === 3 ? `${Number(parts[1])}月${Number(parts[2])}日` : String(date); }
function getDayCSS(st) { return st; }
function getDayIcon(st) { return {completed:'🟢',today:'🔵',future:'🔲',not_completed:'🔴',pending:'⬛'}[st]||'⬛'; }

function renderWishCards() {
  const panel = document.getElementById('wish-cards-panel');
  const container = document.getElementById('wish-cards-container');
  const addRow = document.getElementById('wish-add-row');
  let countLabel = document.getElementById('wish-count-label');
  const title = document.getElementById('wish-cards-title');
  if (!panel || !container) return;
  if (title) title.innerHTML = '<i data-lucide="target"></i> 当前心愿 <span id="wish-count-label">(0/3)</span>';
  // title is replaced above, so reacquire the counter before rendering current state.
  countLabel = document.getElementById('wish-count-label');
  const wishes = wishState.wishes.filter(w => w.status === 'active');
  if (countLabel) countLabel.textContent = `(${wishes.length}/3)`;
  panel.style.display = 'block';
  if (wishes.length === 0) { container.innerHTML = ''; if (addRow) addRow.style.display = 'block'; document.getElementById('wish-add-btn').textContent = '+'; bindWishCardEvents(); return; }
  let html = '';
  for (const w of wishes) {
    const trig = getWishTrigger(w.wish_id);
    const daysHtml = (w.wish_days || []).map(d => {
      const st = getDayStatus(w, d);
      const dateStr = d.business_date.slice(5);
      return `<span class="wish-day-btn ${st}" data-wish-id="${w.wish_id}" data-date="${d.business_date}" data-status="${st}"><span class="day-dot"></span>${dateStr}</span>`;
    }).join('');
    const missingDates = (w.wish_days || []).filter(d => { const st = getDayStatus(w, d); return st === 'pending' || st === 'today'; }).map(d => { const p = String(d.business_date).split('-'); return p.length === 3 ? `${Number(p[1])}-${Number(p[2])}` : d.business_date; });
    const missingHtml = missingDates.length ? '<span class="wish-missing-hint">' + escapeHtml(missingDates.join(', ')) + ' 尚未填写，进度待更新</span>' : '';
    html += '<div class="wish-card-row"><div class="wish-card-top"><span class="wish-card-text">' + escapeHtml(w.text) + '</span><div class="wish-card-actions">' + missingHtml + (trig ? '<span title="已启用提醒" style="display:inline-flex;align-items:center"><i data-lucide="bell" style="width:13px;height:13px"></i></span>' : '') + (canCompleteWish(w) ? '<button class="wish-complete-btn" data-wish-id="' + w.wish_id + '">✓ 完结心愿</button>' : '') + '<button class="wish-edit-btn" data-wish-id="' + w.wish_id + '" style="padding:4px 12px;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text);cursor:pointer;font-size:12px">编辑</button></div></div><div class="wish-card-days">' + daysHtml + '</div></div>';
  }
  container.innerHTML = html;
  if (addRow) { addRow.style.display = wishes.length >= 3 ? 'none' : 'block'; if (wishes.length === 1) document.getElementById('wish-add-btn').textContent = '+'; }
  bindWishCardEvents();
}

function renderWishHistory(container) {
  if (!container) return;
  const wishes = wishState.historyWishes || [];
  if (!wishes.length) {
    container.innerHTML = '<div class="event-muted">暂无往期心愿</div>';
    return;
  }
  container.innerHTML = wishes.map(wish => {
    const days = (wish.wish_days || []).map(day => {
      const outcome = day.evaluation === 'completed' ? '已完成' : day.evaluation === 'not_completed' ? '未完成' : '未评估';
      const outcomeClass = day.evaluation || 'pending';
      return `<span class="wish-history-day ${outcomeClass}">${escapeHtml(formatWishDate(day.business_date))} ${outcome}</span>`;
    }).join('');
    return `<div class="wish-card-row wish-history-row"><div class="wish-card-top"><span class="wish-card-text">${escapeHtml(wish.text)}</span></div><div class="wish-history-days">${days}</div></div>`;
  }).join('');
  if (typeof lucide !== 'undefined') lucide.createIcons();
}

async function showWishHistoryDialog() {
  if (document.getElementById('wish-history-modal')) return;
  const overlay = document.createElement('div');
  overlay.id = 'wish-history-modal';
  overlay.className = 'wish-form-overlay';
  overlay.innerHTML = '<div class="wish-form-box wish-history-dialog"><h3><span><i data-lucide="archive"></i> 往期心愿</span><span class="event-settings-close" role="button" tabindex="0" aria-label="关闭">✕</span></h3><div class="wish-history-modal-list"><div class="event-muted">加载中...</div></div></div>';
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.querySelector('.event-settings-close').addEventListener('click', close);
  overlay.addEventListener('click', event => { if (event.target === overlay) close(); });
  if (typeof lucide !== 'undefined') lucide.createIcons();
  const container = overlay.querySelector('.wish-history-modal-list');
  try {
    await fHistoryWishes();
    if (document.body.contains(overlay)) renderWishHistory(container);
  } catch (error) {
    if (document.body.contains(overlay)) container.innerHTML = '<div class="event-muted">往期心愿读取失败</div>';
  }
}

function bindWishCardEvents() {
  document.querySelectorAll('.wish-day-btn').forEach(btn => {
    btn.addEventListener('click', function(ev) { ev.stopPropagation(); if (this.dataset.status === 'future') return; showAssessmentModal(this.dataset.wishId, this.dataset.date); });
  });
  document.querySelectorAll('.wish-edit-btn').forEach(btn => {
    if (btn.dataset._b) return; btn.dataset._b = '1';
    btn.addEventListener('click', function(ev) { ev.stopPropagation();
      if (wishState.stale) { showToast('当前离线中，无法编辑', 'err'); return; }
      const w = wishState.wishes.find(x => x.wish_id === this.dataset.wishId);
      if (!w) { showToast('心愿未找到', 'err'); return; }
      showWishForm(w).catch(console.warn);
    });
  });
  document.querySelectorAll('.wish-complete-btn').forEach(btn => {
    btn.addEventListener('click', function(ev) {
      ev.stopPropagation();
      if (wishState.stale) { showToast('当前离线中，无法完结心愿', 'err'); return; }
      const wish = wishState.wishes.find(item => item.wish_id === this.dataset.wishId);
      if (wish) showWishCompleteDialog(wish);
    });
  });
  const addBtn = document.getElementById('wish-add-btn');
  if (addBtn && !addBtn.dataset._b) { addBtn.dataset._b = '1'; addBtn.addEventListener('click', () => { if (wishState.stale) { showToast('当前离线中', 'err'); return; } showWishForm(); }); }
  const historyButton = document.getElementById('wish-history-open');
  if (historyButton && !historyButton.dataset._b) { historyButton.dataset._b = '1'; historyButton.addEventListener('click', () => showWishHistoryDialog()); }
}

function showWishCompleteDialog(wish) {
  const missing = (wish.wish_days || []).filter(day => !day.evaluation).map(day => formatWishDate(day.business_date));
  const overlay = document.createElement('div'); overlay.className = 'wish-form-overlay';
  const warning = missing.length ? `<p class="wish-complete-warning">${escapeHtml(missing.join('、'))} 日期结果还未填写，请先填写后再完结心愿。</p>` : `<p>确认完结“${escapeHtml(wish.text)}”吗？完结后将移入往期心愿，并生成结果事件。</p>`;
  overlay.innerHTML = `<div class="wish-form-box wish-complete-box"><h3>✓ 完结心愿</h3>${warning}<div class="form-btns"><button class="btn-cancel" id="wc-cancel">${missing.length ? '知道了' : '取消'}</button>${missing.length ? '' : '<button class="btn-primary" id="wc-confirm">确认完结</button>'}</div></div>`;
  document.body.appendChild(overlay);
  overlay.querySelector('#wc-cancel').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', event => { if (event.target === overlay) overlay.remove(); });
  const confirm = overlay.querySelector('#wc-confirm');
  if (confirm) confirm.addEventListener('click', async () => {
    confirm.disabled = true;
    try {
      const response = await fetch(`/api/wishes/${wish.wish_id}/complete`, { method: 'POST' });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (body.error === 'wish_days_incomplete') {
          const dates = (body.missing_business_dates || []).map(formatWishDate).join('、');
          throw new Error(`${dates || '部分'}日期结果还未填写`);
        }
        throw new Error(body.message || body.error || `HTTP ${response.status}`);
      }
      overlay.remove(); showToast('心愿已完结', 'ok');
      wishState.historyWishesLoaded = false;
      await loadEventsTimeline().catch(() => {});
      renderWishCards();
    } catch (error) {
      confirm.disabled = false; showToast(`完结失败：${error.message}`, 'err');
    }
  });
}

// 评估弹窗
function showAssessmentModal(wishId, date) {
  const overlay = document.createElement('div'); overlay.className = 'modal-overlay';
  overlay.innerHTML = '<div class="modal-box"><h3>' + date.slice(5) + ' 心愿是否完成？</h3><div class="modal-btns"><button class="btn-done" id="md-done">已完成</button><button class="btn-not-done" id="md-not">未完成</button></div></div>';
  document.body.appendChild(overlay);
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  overlay.querySelector('#md-done').addEventListener('click', async () => { overlay.remove(); await doAssess(wishId, date, 'completed'); });
  overlay.querySelector('#md-not').addEventListener('click', async () => { overlay.remove(); await doAssess(wishId, date, 'not_completed'); });
}
async function doAssess(wishId, date, ev) {
  try {
    const r = await fetch('/api/wishes/' + wishId + '/days/' + date, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ evaluation: ev }) });
    if (r.ok) { showToast(ev === 'completed' ? '已完成 ✓' : '未完成 ✗', 'ok'); await loadEventsTimeline().catch(() => {}); }
    else showToast('评估失败', 'err');
  } catch (e) { showToast('评估失败', 'err'); }
}

// 创建心愿表单
const TRIGGER_LABELS = {
  'blacklist_usage_milestone': '黑名单使用过量提醒',
  'device_usage_milestone': '设备使用过量提醒',
  'late_usage_milestone': '使用过晚提醒',
  'scheduled_reminder': '定时提醒',
};
const TRIGGER_DESCS = {
  'blacklist_usage_milestone': '当黑名单应用累计使用达到设定期限时提醒',
  'device_usage_milestone': '当单台设备累计使用达到设定期限时提醒',
  'late_usage_milestone': '当在指定晚间时段后仍检测到使用时提醒',
  'scheduled_reminder': '在每天固定时间提醒一次',
};

async function showWishForm(wish) {
  const isEdit = !!wish;
  if (!wishState.triggerTypes.length) { try { await fTriggerTypes(); } catch (_) {} }
  // Render immediately using cached devices if available; otherwise wait once and cache
  const devices = wishState.devicesCache || (await fDevices());
  wishState.devicesCache = devices;
  const dx = devices.map(d => '<option value="' + d.device_id + '">' + escapeHtml(d.display_name) + '</option>').join('');

  // Determine existing trigger for this wish
  const existingTrigger = isEdit ? wishState.triggers.find(t => t.wish_id === wish.wish_id) : null;
  const existingType = existingTrigger ? existingTrigger.trigger_type : null;

  // Build trigger cards (vertical list, inline params)
  let trigCards = '';
  for (const tt of wishState.triggerTypes) {
    if (!Array.isArray(tt.target_scopes) || !tt.target_scopes.includes('wish')) continue;
    const allowed = tt.interval_minutes && Array.isArray(tt.interval_minutes.allowed_values)
      ? tt.interval_minutes.allowed_values : [60];
    // For new trigger, default 60; for existing, pick its value
    let selectedInterval = 60;
    let selectedDevice = devices[0]?.device_id || '';
    let selectedStart = '23:00';
    if (existingType === tt.trigger_type && existingTrigger && existingTrigger.parameters) {
      const p = existingTrigger.parameters;
      if (p.interval_minutes) selectedInterval = Number(p.interval_minutes);
      if (p.device_id) selectedDevice = p.device_id;
      if (p.start_local_time) selectedStart = p.start_local_time;
      if (p.reminder_local_time) selectedStart = p.reminder_local_time;
    }
    const intervalOpts = allowed.map(v => '<option value="' + v + '"' + (v === selectedInterval ? ' selected' : '') + '>' + v + ' 分钟</option>').join('');
    const deviceOpts = dx.replace(/value="' + selectedDevice + '"/g, 'value="' + selectedDevice + '" selected');
    let paramsHtml = '';
    if (tt.trigger_type === 'blacklist_usage_milestone') {
      paramsHtml = '<div class="trigger-card-params"><span class="param-group"><label>提醒时限</label><select class="tp" data-key="interval_minutes">' + intervalOpts + '</select></span></div>';
    } else if (tt.trigger_type === 'device_usage_milestone') {
      paramsHtml = '<div class="trigger-card-params"><span class="param-group"><label>设备</label><select class="tp" data-key="device_id">' + deviceOpts + '</select></span><span class="param-group"><label>提醒时限</label><select class="tp" data-key="interval_minutes">' + intervalOpts + '</select></span></div>';
    } else if (tt.trigger_type === 'late_usage_milestone') {
      paramsHtml = '<div class="trigger-card-params"><span class="param-group"><label>起始时间</label><input class="tp" data-key="start_local_time" type="time" value="' + selectedStart + '"></span><span class="param-group"><label>提醒时限</label><select class="tp" data-key="interval_minutes">' + intervalOpts + '</select></span></div>';
    } else if (tt.trigger_type === 'scheduled_reminder') {
      // This is once per fixed business day; its required transport interval is
      // centrally fixed at 1 and intentionally has no meaningless UI control.
      paramsHtml = '<div class="trigger-card-params"><span class="param-group"><label>提醒时间</label><input class="tp" data-key="reminder_local_time" type="time" value="' + selectedStart + '"></span></div>';
    }
    const selectedClass = (existingType === tt.trigger_type) ? ' selected' : '';
    trigCards += '<div class="trigger-option-card' + selectedClass + '" data-type="' + tt.trigger_type + '">'
      + '<span class="trigger-card-title">' + (TRIGGER_LABELS[tt.trigger_type] || tt.trigger_type) + '</span>'
      + paramsHtml + '</div>';
  }

  const overlay = document.createElement('div'); overlay.className = 'wish-form-overlay';
  const titleText = isEdit ? '编辑心愿' : '设置心愿';
  const submitText = isEdit ? '保存' : '创建';
  const extraBtn = isEdit ? '<button class="btn-danger" id="wf-delete" style="margin-left:auto;background:#e74c3c;color:#fff;border:none;border-radius:6px;padding:8px 20px;cursor:pointer;font-size:14px">删除心愿</button>' : '';
  overlay.innerHTML = '<div class="wish-form-box"><h3>' + titleText + ' <span style="cursor:pointer;font-size:22px" id="wf-close">✕</span></h3>'
    + '<div class="field-group"><label>心愿内容</label><input type="text" id="wf-text" maxlength="30" placeholder="减少游戏时间" value="' + (isEdit ? escapeHtml(wish.text) : '') + '"></div>'
    + '<div class="field-group" id="wf-dur-group"><label>周期</label><div class="radio-group"><label><input type="radio" name="wf-dur" value="3"' + (!isEdit || wish.duration_days === 3 ? ' checked' : '') + ' ' + (isEdit ? 'disabled' : '') + '> 三日</label><label><input type="radio" name="wf-dur" value="7"' + (isEdit && wish.duration_days === 7 ? ' checked' : '') + ' ' + (isEdit ? 'disabled' : '') + '> 七日</label></div></div>'
    + '<div class="field-group"><label>提醒触发器（单选）</label><div class="trigger-list">' + trigCards + '</div></div>'
    + '<div class="form-btns" style="display:flex;align-items:center">' + extraBtn + '<button class="btn-cancel" id="wf-cancel">取消</button><button class="btn-primary" id="wf-submit">' + submitText + '</button></div></div>';
  document.body.appendChild(overlay);

  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  document.getElementById('wf-close').addEventListener('click', () => overlay.remove());
  document.getElementById('wf-cancel').addEventListener('click', () => overlay.remove());

  // Toggle: click to select, click again to deselect. Clicks on interactive params do not toggle.
  const cards = overlay.querySelectorAll('.trigger-option-card');
  cards.forEach(card => {
    card.addEventListener('click', function(ev) {
      if (ev.target.closest('.trigger-card-params')) return;
      if (this.classList.contains('selected')) {
        this.classList.remove('selected');
      } else {
        cards.forEach(c => c.classList.remove('selected'));
        this.classList.add('selected');
      }
    });
  });

  if (isEdit) {
    document.getElementById('wf-delete').addEventListener('click', async () => {
      if (!confirm('确定永久删除该心愿？删除后无法恢复。')) return;
      try {
        const r = await fetch('/api/wishes/' + wish.wish_id, { method: 'DELETE' });
        if (r.ok) { showToast('心愿已删除', 'ok'); overlay.remove(); loadEventsTimeline().catch(() => {}); }
        else showToast('删除失败', 'err');
      } catch (e) { showToast('删除失败', 'err'); }
    });
  }

  document.getElementById('wf-submit').addEventListener('click', async () => {
    const text = document.getElementById('wf-text').value.trim();
    if (!text || text.length > 30) { showToast('内容需 1-30 字', 'err'); return; }
    const sel = overlay.querySelector('.trigger-option-card.selected');
    const tt = sel ? sel.dataset.type : null;
    const btn = document.getElementById('wf-submit'); btn.disabled = true; btn.textContent = isEdit ? '保存中...' : '创建中...';
    try {
      if (isEdit) {
        // 1. Patch text
        const patchResp = await fetch('/api/wishes/' + wish.wish_id, { method: 'PATCH', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ text }) });
        if (!patchResp.ok) { const e = await patchResp.text().catch(()=>'failed'); showToast('保存失败: '+e, 'err'); btn.disabled = false; btn.textContent = '保存'; return; }
        showToast('文字已保存', 'ok');

        // 2. Trigger changes
        if (existingType && tt === null) {
          // Remove existing trigger
          const delResp = await fetch('/api/event-triggers/' + existingTrigger.trigger_id, { method: 'DELETE' });
          if (!delResp.ok) showToast('提醒关闭失败', 'err'); else showToast('提醒已关闭', 'ok');
        } else if (tt && !existingType) {
          // Create new trigger
          const params = {}; sel.querySelectorAll('.tp').forEach(el => { params[el.dataset.key] = el.value; });
          const intervalMinutes = tt === 'scheduled_reminder' ? 1 : (parseInt(params.interval_minutes) || 60);
          delete params.interval_minutes;
          if (tt === 'blacklist_usage_milestone') params.platform_scope = 'all';
          if (tt === 'late_usage_milestone') params.device_id = 'all';
          const tr = await fetch('/api/event-triggers', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ request_id: crypto.randomUUID(), wish_id: wish.wish_id, trigger_type: tt, config_version: 1, parameters: params, interval_minutes: intervalMinutes, enabled: true }) });
          if (!tr.ok) showToast('提醒设置失败', 'err'); else showToast('提醒已设置', 'ok');
        } else if (tt && existingType) {
          if (tt === existingType) {
            // Patch same type parameters
            const params = {}; sel.querySelectorAll('.tp').forEach(el => { params[el.dataset.key] = el.value; });
            const intervalMinutes = tt === 'scheduled_reminder' ? 1 : (parseInt(params.interval_minutes) || 60);
            delete params.interval_minutes;
            if (tt === 'blacklist_usage_milestone') params.platform_scope = 'all';
            if (tt === 'late_usage_milestone') params.device_id = 'all';
            const tr = await fetch('/api/event-triggers/' + existingTrigger.trigger_id, { method: 'PATCH', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ parameters: params, interval_minutes: intervalMinutes, enabled: true }) });
            if (!tr.ok) showToast('提醒更新失败', 'err'); else showToast('提醒已更新', 'ok');
          } else {
            // Replace type: delete old, create new
            await fetch('/api/event-triggers/' + existingTrigger.trigger_id, { method: 'DELETE' });
            const params = {}; sel.querySelectorAll('.tp').forEach(el => { params[el.dataset.key] = el.value; });
            const intervalMinutes = tt === 'scheduled_reminder' ? 1 : (parseInt(params.interval_minutes) || 60);
            delete params.interval_minutes;
            if (tt === 'blacklist_usage_milestone') params.platform_scope = 'all';
            if (tt === 'late_usage_milestone') params.device_id = 'all';
            const tr = await fetch('/api/event-triggers', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ request_id: crypto.randomUUID(), wish_id: wish.wish_id, trigger_type: tt, config_version: 1, parameters: params, interval_minutes: intervalMinutes, enabled: true }) });
            if (!tr.ok) showToast('提醒替换失败', 'err'); else showToast('提醒已替换', 'ok');
          }
        }
        overlay.remove(); loadEventsTimeline().catch(() => {});
      } else {
        const dur = parseInt(overlay.querySelector('input[name=wf-dur]:checked').value);
        const wr = await fetch('/api/wishes', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ request_id: crypto.randomUUID(), text, duration_days: dur, ai_tracking_enabled: false }) });
        if (!wr.ok) { const e = await wr.text().catch(()=>'failed'); showToast('创建失败: '+e, 'err'); btn.disabled = false; btn.textContent = '创建'; return; }
        const w = await wr.json(); showToast('已创建', 'ok');
        if (tt) {
          const params = {}; sel.querySelectorAll('.tp').forEach(el => { params[el.dataset.key] = el.value; });
          const intervalMinutes = tt === 'scheduled_reminder' ? 1 : (parseInt(params.interval_minutes) || 60);
          delete params.interval_minutes;
          if (tt === 'blacklist_usage_milestone') params.platform_scope = 'all';
          if (tt === 'late_usage_milestone') params.device_id = 'all';
          const tr = await fetch('/api/event-triggers', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ request_id: crypto.randomUUID(), wish_id: w.wish_id, trigger_type: tt, config_version: 1, parameters: params, interval_minutes: intervalMinutes, enabled: true }) });
          if (!tr.ok) showToast('提醒设置失败', 'err'); else showToast('提醒已设置', 'ok');
        }
        overlay.remove(); loadEventsTimeline().catch(() => {});
      }
    } catch (e) { showToast((isEdit ? '保存失败' : '创建失败') + ':' + e.message, 'err'); btn.disabled = false; btn.textContent = isEdit ? '保存' : '创建'; }
  });
}

// 延迟到 multiDeviceState 初始化后加载
setTimeout(() => loadEventsTimeline().catch(console.warn), 100);

async function loadBlacklistRules() {
  try {
    const response = await fetch("/api/blacklist/rules");
    if (!response.ok) {
      console.warn("blacklist: central returned", response.status);
      return;
    }
    const data = await response.json();
    fullRules = data.rules || [];
  } catch (_) {
    // Central unreachable: keep last known values (may be empty).
    return;
  }
  rebuildBlacklistCaches();
}

function rebuildBlacklistCaches() {
  const apps = [];
  const doms = [];
  for (const r of fullRules) {
    if (!r.enabled) continue;
    if (r.rule_type === "app") apps.push({ name: r.pattern, label: r.label });
    else if (r.rule_type === "domain") doms.push({ domain: r.pattern, label: r.label });
  }
  blacklistRules = { processes: apps, domains: doms };
  blacklistRulesLoaded = true;
}

// 颜色映射
const blColors = {
  "bilibili.com": "#fb7299", "zhihu.com": "#0066ff", "jandan.net": "#ff6b35",
  "steam": "#1b2838", "Minecraft": "#5a9e3f", "javaw": "#7cb342", "steamwebhelper": "#66c0f4",
};

// === 业务日时间轴 ===
let bizDayTimelineNowTimer = null;

function renderBizDayTimeline() {
  const container = document.getElementById('bizday-timeline');
  if (!container) return;

  const events = eventsTimelineCache || [];
  const biz = computeEventDisplayBizDate();
  const win = bizDateToUTCWindow(biz.bizDate, biz.dayStartHour);
  const fromMs = new Date(win.from).getTime();
  const toMs = new Date(win.to).getTime();
  const span = toMs - fromMs;

  // 清除旧标记和竖线（保留轴和红线）
  container.querySelectorAll('.bizday-tl-tick, .bizday-tl-marker, .bizday-tl-stem').forEach(el => el.remove());

  // 刻度：每 4 小时，居中显示在轴下方
  const dayStart = biz.dayStartHour;
  for (let i = 0; i <= 24; i += 4) {
    const hour = (dayStart + i) % 24;
    const pct = (i / 24) * 100;
    const tick = document.createElement('div');
    tick.className = 'bizday-tl-tick';
    tick.style.left = pct + '%';
    tick.textContent = String(hour).padStart(2, '0') + ':00';
    container.appendChild(tick);
  }

  // 事件标记（轴上下交错）
  const sorted = events
    .map(e => ({ e, ms: new Date(e.occurred_at || '').getTime() }))
    .filter(item => !Number.isNaN(item.ms) && item.ms >= fromMs && item.ms <= toMs)
    .sort((a, b) => a.ms - b.ms);

  const containerHeight = container.clientHeight || 192;
  const axisY = containerHeight / 2; // 轴的 Y 坐标
  const containerWidth = container.clientWidth || 800;
  const minPixelGap = 30;
  let layer = 0;
  let lastX = -Infinity;
  let lastSide = 1; // 1=上方, -1=下方
  const markerData = [];

  for (const item of sorted) {
    const pct = ((item.ms - fromMs) / span) * 100;
    const xPx = (pct / 100) * containerWidth;
    // 相邻事件太近时交错+加层
    if (xPx - lastX < minPixelGap) {
      lastSide = -lastSide; // 上下交替
      if (lastSide === 1) layer = (layer + 1) % 3; // 回到同侧时加层
    } else {
      layer = 0;
      lastSide = lastSide === 1 ? -1 : 1; // 交替
    }
    lastX = xPx;

    const e = item.e;
    const high = e.importance === 'high';
    const tone = high ? 'high' : (e.category === 'system' ? 'system' : 'normal');
    const hidden = !eventIsVisible(e);
    const icon = eventIcon(e);
    const title = eventDisplayTitle(e);
    const detail = eventDisplayDetail(e);
    const time = new Intl.DateTimeFormat('zh-CN', {
      timeZone: wishState.sharedSettings?.timezone || 'Asia/Shanghai',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
    }).format(new Date(item.ms));

    const markerSize = high ? 26 : 22;
    const marker = document.createElement('div');
    marker.className = `bizday-tl-marker tone-${tone}`;
    if (hidden) marker.style.opacity = '0.3';
    marker.style.left = `calc(${pct}% - ${markerSize / 2}px)`;

    // 上下交错定位：side=1 在轴上方，side=-1 在轴下方
    const offset = (lastSide === 1 ? 6 : 24) + layer * 28; // 下方留出刻度文字空间
    if (lastSide === 1) {
      marker.style.top = `${axisY - offset - markerSize}px`;
    } else {
      marker.style.top = `${axisY + offset}px`;
    }
    marker.innerHTML = '<i data-lucide="' + icon.icon + '" style="color:#fff;width:14px;height:14px"></i>';
    marker.style.background = icon.color;
    container.appendChild(marker);

    // 竖线：从标记边缘到轴
    const stem = document.createElement('div');
    stem.className = 'bizday-tl-stem';
    stem.style.left = `${pct}%`;
    if (lastSide === 1) {
      stem.style.top = `${axisY - offset - markerSize + markerSize}px`;
      stem.style.height = `${offset}px`;
    } else {
      stem.style.top = `${axisY}px`;
      stem.style.height = `${offset}px`;
    }
    container.appendChild(stem);

    markerData.push({ el: marker, time, title, detail, tone });
  }

  // 当前时间红线
  updateBizDayNowLine(fromMs, span);

  // Tooltip
  const tooltip = document.getElementById('bizday-tl-tooltip');
  if (tooltip) {
    container.onmousemove = null;
    container.onmouseleave = null;
    container.onmousemove = (event) => {
      const rect = container.getBoundingClientRect();
      const mx = event.clientX - rect.left;
      const my = event.clientY - rect.top;
      // 找最近的标记（30px 范围内）
      let closest = null;
      let minDist = Infinity;
      for (const m of markerData) {
        const elRect = m.el.getBoundingClientRect();
        const cx = elRect.left - rect.left + elRect.width / 2;
        const cy = elRect.top - rect.top + elRect.height / 2;
        const dist = Math.hypot(mx - cx, my - cy);
        if (dist < 30 && dist < minDist) {
          minDist = dist;
          closest = m;
        }
      }
      if (!closest) {
        tooltip.hidden = true;
        container.style.cursor = 'default';
        return;
      }
      const detailText = closest.detail ? escapeHtml(closest.detail) : '<span style="color:#9ca3af">无附加内容</span>';
      const toneColor = closest.tone === 'high' ? '#F59E0B' : closest.tone === 'system' ? '#8B95A5' : '#3B82F6';
      tooltip.innerHTML = `<span style="color:${toneColor};font-weight:700;font-size:11px;letter-spacing:.02em">${escapeHtml(closest.time)}</span><strong>${escapeHtml(closest.title)}</strong><span style="display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden">${detailText}</span>`;
      tooltip.hidden = false;
      container.style.cursor = 'pointer';
      tooltip.style.left = `${Math.min(rect.width - tooltip.offsetWidth - 8, Math.max(8, mx + 12))}px`;
      tooltip.style.top = `${Math.max(8, my - tooltip.offsetHeight - 10)}px`;
    };
    container.onmouseleave = () => {
      tooltip.hidden = true;
      container.style.cursor = 'default';
    };
  }

  // 每分钟更新红线
  if (bizDayTimelineNowTimer) clearInterval(bizDayTimelineNowTimer);
  bizDayTimelineNowTimer = setInterval(() => updateBizDayNowLine(fromMs, span), 60000);
  if (typeof lucide !== 'undefined') lucide.createIcons();
}

function updateBizDayNowLine(fromMs, span) {
  const nowEl = document.getElementById('bizday-tl-now');
  if (!nowEl) return;
  const nowMs = Date.now();
  if (nowMs < fromMs || nowMs > fromMs + span) {
    nowEl.style.display = 'none';
    return;
  }
  const pct = ((nowMs - fromMs) / span) * 100;
  nowEl.style.left = pct + '%';
  nowEl.style.display = 'block';
  const timeText = new Intl.DateTimeFormat('zh-CN', {
    timeZone: wishState.sharedSettings?.timezone || 'Asia/Shanghai',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
  }).format(new Date(nowMs));
  const timeSpan = nowEl.querySelector('.now-time');
  if (timeSpan) timeSpan.textContent = timeText;
}
