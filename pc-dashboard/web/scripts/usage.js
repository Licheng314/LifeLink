let appCharts = { hourly: null };
let usageRankMaxHours = 1;
let usageDayStartHour = 0;

// === 黑名单编辑：平台标签与颜色规范（design §2.2 / §4）===
const PLATFORM_LABELS = { pc: '电脑应用', android: '手机 APP', web: '网站' };

function getUsageColor(name, isBlacklisted, isWeb) {
  if (isBlacklisted) return isWeb ? '#e74c3c' : '#9b59b6';
  return isWeb ? '#f39c12' : '#4f8cff';
}

function normalizePattern(pattern, ruleType) {
  if (ruleType === 'domain') {
    let cleaned = (pattern || '').toLowerCase().trim();
    if (cleaned.startsWith('www.')) cleaned = cleaned.slice(4);
    if (cleaned.endsWith('.')) cleaned = cleaned.slice(0, -1);
    return cleaned;
  }
  return (pattern || '').toLowerCase().trim();
}

function ruleCountReached() {
  return fullRules.filter(r => r.enabled).length >= 10;
}

function usageScopeData(summary, scope) {
  if (scope === 'all') return summary.all;
  const exact = summary.devices.find(device => device.device_key === scope);
  if (exact) return exact;
  const rosterDevice = multiDeviceState.roster.find(device => device.device_key === scope);
  if (rosterDevice) {
    const sameName = summary.devices.find(device =>
      String(device.display_name || '').trim().toLowerCase() ===
      String(rosterDevice.display_name || '').trim().toLowerCase()
    );
    if (sameName) return sameName;
  }
  return {
    device_key: scope,
    display_name: rosterDevice?.display_name || '所选设备',
    platform: rosterDevice?.platform || 'unknown',
    is_local: Boolean(rosterDevice?.is_local),
    events: 0, window_events: 0, web_events: 0, afk_seconds: 0,
    apps: {}, hourly: {}, hourly_apps: {}, hourly_online: {},
    sites: {}, hourly_sites: {},
  };
}

function renderUsageScope(summary, scope) {
  const data = usageScopeData(summary, scope);
  if (!data) return;
  // App "+" creates rules for the selected device's platform. The aggregate
  // "all" view cannot infer a single platform, so it defaults to pc (this is
  // the PC dashboard, whose primary rules are desktop apps).
  const scopeDevice = summary.devices.find(d => d.device_key === scope);
  const appPlatform = (scope === 'all' || !scopeDevice || String(scopeDevice.platform).toLowerCase() !== 'android') ? 'pc' : 'android';

  const allApps = Object.entries(data.apps || {}).map(([app, seconds]) => ({ name: app, hours: seconds / 3600 })).sort((a, b) => b.hours - a.hours);
  const siteRows = Object.entries(data.sites || {}).map(([site, seconds]) => ({ name: site, hours: seconds / 3600 })).sort((a, b) => b.hours - a.hours);
  // Apps and sites share one denominator so bar lengths are directly comparable,
  // and the hour axis is identical across both charts.
  const sharedMaxHours = Math.max(0.001, ...allApps.map(a => a.hours), ...siteRows.map(s => s.hours));
  usageRankMaxHours = sharedMaxHours;
  const hasSites = siteRows.length > 0;
  // In the aggregate view, mark each app's source platform so users can tell
  // which apps came from a phone vs a desktop. Specific-device views need none.
  const appPlatformMap = {};
  if (scope === 'all') {
    for (const dev of summary.devices || []) {
      const raw = String(dev.platform || '').toLowerCase();
      // Devices use 'desktop'/'android', but blacklist platform_scope uses
      // 'pc'/'android'/'web'. Normalize so the add-button sends a valid scope.
      const plat = raw === 'desktop' ? 'pc' : (raw === 'android' ? 'android' : null);
      if (plat === null) continue;
      for (const name of Object.keys(dev.apps || {})) {
        (appPlatformMap[name] = appPlatformMap[name] || new Set()).add(plat);
      }
    }
  }
  const totalMinutes = Math.round(Object.values(data.hourly || {}).reduce((sum, seconds) => sum + seconds, 0) / 60);
  const currentHour = String(new Date().getHours());
  const currentHourMinutes = Math.round((data.hourly?.[currentHour] || 0) / 60);
  const isBlacklisted = app => isBlacklistedApp(app);
  const isBlacklistedSite = site => isBlacklistedDomain(site);
  const blackForHour = hour => {
    const apps = Object.entries(data.hourly_apps?.[hour] || {}).filter(([app]) => isBlacklisted(app)).reduce((sum, [, seconds]) => sum + seconds, 0);
    const sites = Object.entries(data.hourly_sites?.[hour] || {}).filter(([site]) => isBlacklistedSite(site)).reduce((sum, [, seconds]) => sum + seconds, 0);
    const application = data.hourly?.[hour] || 0;
    return Math.min(application, apps + sites);
  };
  const blackTotal = Math.round(Object.keys(data.hourly || {}).reduce((sum, hour) => sum + blackForHour(hour), 0) / 60);
  const blackCurrent = Math.round(blackForHour(currentHour) / 60);
  const statCards = [
    ['今日设备使用时长', formatMinutesHtml(totalMinutes), 'PC 已剪去明确 AFK 区间'],
    ['本小时设备使用时长', formatMinutesHtml(currentHourMinutes), `${currentHour.padStart(2,'0')}:00 至现在`],
    ['今日黑名单时长', formatMinutesHtml(blackTotal), '按应用名匹配黑名单'],
    ['本小时黑名单时长', formatMinutesHtml(blackCurrent), `${currentHour.padStart(2,'0')}:00 至现在`],
  ];
  ['stat-today-total','stat-last-hour','stat-current-status','stat-afk-status'].forEach((id, index) => {
    const value = document.getElementById(id); const card = value?.closest('.stat-card');
    if (!value || !card) return;
    card.querySelector('.label').textContent = statCards[index][0];
    value.innerHTML = statCards[index][1];
    card.querySelector('.sub').textContent = statCards[index][2];
  });
  renderAllAppsChart(allApps, appPlatform, sharedMaxHours, appPlatformMap);
  renderUsageHourly(data.hourly || {}, data.hourly_apps || {}, data.hourly_sites || {});
  renderSiteUsage(siteRows, sharedMaxHours, hasSites);
  renderBlacklistManagementPanel();
}

function renderUsageHourly(hourly, hourlyApps, hourlySites) {
  document.getElementById('hourly-loading').style.display = 'none';
  const canvas = document.getElementById('chartBlacklistHourly');
  canvas.style.display = 'block';
  if (appCharts.hourly) appCharts.hourly.destroy();
  // The service bins in Beijing calendar hours; show a stable 00-23 axis
  // instead of deriving labels from the browser clock.
  const hours = Array.from({ length: 24 }, (_, index) => (usageDayStartHour + index) % 24);
  const black = hour => {
    const appBlack = Object.entries(hourlyApps[String(hour)] || {}).filter(([app]) => isBlacklistedApp(app)).reduce((sum, [, seconds]) => sum + seconds, 0);
    const siteBlack = Object.entries(hourlySites[String(hour)] || {}).filter(([site]) => isBlacklistedDomain(site)).reduce((sum, [, seconds]) => sum + seconds, 0);
    const appTotal = hourly[String(hour)] || 0;
    return Math.min(appTotal, appBlack + siteBlack);
  };
  const datasets = [
    { label: '设备使用时长', data: hours.map(hour => +((hourly[String(hour)] || 0) / 60).toFixed(1)), backgroundColor: '#4f8cff', borderRadius: 4 },
    { label: '黑名单时长', data: hours.map(hour => +(black(hour) / 60).toFixed(1)), backgroundColor: '#e74c3c', borderRadius: 4 },
  ];
  appCharts.hourly = new Chart(canvas, { type: 'bar', data: { labels: hours.map(hour => String(hour).padStart(2, '0')), datasets }, options: { responsive: true, maintainAspectRatio: false, scales: { y: { beginAtZero: true, suggestedMax: 60, title: { display: true, text: '分钟' } }, x: { grid: { display: false } } }, plugins: { legend: { position: 'bottom' } } }, plugins: [usageNowLinePlugin] });
}

// “现在”红线插件：与事件业务日时间轴一致，仅当前业务日显示
const usageNowLinePlugin = {
  id: 'usageNowLine',
  afterDatasetsDraw(chart) {
    if (typeof isHistoricalDataView === 'function' && isHistoricalDataView()) return;
    const dayStart = usageDayStartHour || 0;
    const now = new Date();
    // 以 UTC+8 本地时间计算当前小时在业务日中的偏移
    const local = new Date(now.getTime() + 8 * 3600 * 1000);
    const utcHour = local.getUTCHours();
    const utcMin = local.getUTCMinutes();
    let hourOffset = utcHour - dayStart;
    if (hourOffset < 0) hourOffset += 24;
    const fractionalIndex = hourOffset + utcMin / 60;
    if (fractionalIndex < 0 || fractionalIndex > 24) return;
    const xAxis = chart.scales.x;
    if (!xAxis) return;
    const x = xAxis.getPixelForValue(fractionalIndex - 0.5);
    const top = chart.chartArea.top, bottom = chart.chartArea.bottom;
    const { ctx } = chart;
    const dangerColor = getComputedStyle(document.documentElement).getPropertyValue('--danger').trim() || '#ef4444';
    ctx.save();
    ctx.strokeStyle = dangerColor; ctx.lineWidth = 1.5; ctx.setLineDash([]);
    ctx.beginPath(); ctx.moveTo(x, top - 2); ctx.lineTo(x, bottom + 2); ctx.stroke();
    ctx.fillStyle = dangerColor; ctx.font = '700 9px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText('现在', x, Math.max(9, top - 5));
    ctx.textAlign = 'start'; ctx.restore();
  }
};

// === Top 10 rank chart with inline "+" and hour axis (design §3.1) ===
function formatHours(h) {
  if (h >= 100) return Math.round(h) + 'h';
  const r = Math.round(h * 10) / 10;
  return (r % 1 === 0 ? r : r.toFixed(1)) + 'h';
}

function formatDurationShort(minutes) {
  if (minutes >= 60) {
    const h = Math.floor(minutes / 60);
    const m = Math.round(minutes % 60);
    return `${h}h${m}m`;
  }
  return `${minutes.toFixed(1)}m`;
}

function formatMinutesHtml(minutes) {
  const m = Math.max(0, Math.round(minutes));
  const h = Math.floor(m / 60);
  const rem = m % 60;
  const unit = '<span style="font-size:16px">';
  if (h > 0 && rem > 0) return `${h}${unit}小时</span>${rem}${unit}分钟</span>`;
  if (h > 0) return `${h}${unit}小时</span>`;
  return `${rem}${unit}分钟</span>`;
}

function renderRankAxis(maxH) {
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(p => {
    const v = maxH * p;
    return `<span class="rank-tick" style="left:${(p * 100).toFixed(1)}%">${formatHours(v)}</span>`;
  }).join('');
  return `<div class="rank-axis"><span></span><span></span><span class="rank-axis-scale">${ticks}</span><span></span></div>`;
}

function renderRankRows(rows, isWeb, platform, sharedMaxHours, platformMap) {
  const top = rows.slice(0, 10);
  const limitReached = ruleCountReached();
  return top.map(row => {
    const isBl = isWeb ? isBlacklistedDomain(row.name) : isBlacklistedApp(row.name);
    // 查找匹配的黑名单规则 ID
    let blRuleId = null;
    if (isBl) {
      const norm = normalizePattern(row.name, isWeb ? 'domain' : 'app');
      const matched = fullRules.find(r => r.enabled && normalizePattern(r.pattern, r.rule_type || (isWeb ? 'domain' : 'app')) === norm);
      if (matched) blRuleId = matched.rule_id;
    }
    const sourceSet = !isWeb && platformMap ? platformMap[row.name] : null;
    const ambiguous = sourceSet && sourceSet.size > 1;
    const sourcePlatform = sourceSet && sourceSet.size === 1 ? [...sourceSet][0] : platform;
    const disabled = (isBl && !blRuleId) || limitReached || ambiguous;
    const color = getUsageColor(row.name, isBl, isWeb);
    const pct = (row.hours / sharedMaxHours * 100).toFixed(1);
    const btnTitle = isBl ? '点击移除黑名单' : (limitReached ? '已达 10 条上限' : (ambiguous ? '多平台同名，需分别添加' : '加入黑名单'));
    const btnClass = isBl ? 'rank-add rank-remove' : 'rank-add';
    const btnText = isBl ? '−' : '+';
    const emoji = platformEmoji(row.name, platformMap);
    return `<div class="rank-row">
      <span class="rank-label" title="${escapeHtml(row.name)}">${emoji}${escapeHtml(row.name)}</span>
      <button class="${btnClass}${disabled && !isBl ? ' disabled' : ''}" title="${btnTitle}" ${disabled && !isBl ? 'disabled' : ''}
        data-pattern="${escapeHtml(row.name)}" data-label="${escapeHtml(row.name)}" data-type="${isWeb ? 'domain' : 'app'}" data-platform="${sourcePlatform}" ${blRuleId ? `data-rule-id="${escapeHtml(blRuleId)}"` : ''}>${btnText}</button>
      <span class="rank-bar-track"><span class="rank-bar" style="width:${pct}%;background:${color}"></span></span>
      <span class="bl-duration">${formatDurationShort(row.hours * 60)}</span>
    </div>`;
  }).join('');
}

function platformEmoji(name, platformMap) {
  if (!platformMap) return '';
  const plats = platformMap[name];
  if (!plats || plats.size === 0) return '';
  const parts = [];
  if (plats.has('pc')) parts.push('<i data-lucide="monitor" style="width:12px;height:12px;vertical-align:-1px"></i>');
  if (plats.has('android')) parts.push('<i data-lucide="smartphone" style="width:12px;height:12px;vertical-align:-1px"></i>');
  return parts.join(' ') + ' ';
}

function renderAllAppsChart(allApps, appPlatform, sharedMaxHours, appPlatformMap) {
  const panel = document.getElementById('app-usage-panel');
  if (!panel) return;
  if (allApps.length === 0) { panel.innerHTML = '<h3><i data-lucide="smartphone"></i> 全应用使用时长排行 (Top 10)</h3><div class="rank-empty">暂无数据</div>'; return; }
  const rowsHtml = renderRankRows(allApps, false, appPlatform, sharedMaxHours, appPlatformMap);
  panel.innerHTML = `<h3><i data-lucide="smartphone"></i> 全应用使用时长排行 (Top 10)</h3><div class="rank-chart">${rowsHtml}${renderRankAxis(sharedMaxHours)}</div>`;
}

function renderSiteUsage(rows, sharedMaxHours, hasSites) {
  const panel = document.getElementById('site-usage-panel');
  const pair = document.querySelector('.usage-rank-pair');
  // Android has no web browsing; hide the site area entirely and let the app
  // chart span the full width instead of leaving an empty half column.
  if (!hasSites) {
    if (panel) panel.style.display = 'none';
    if (pair) pair.style.gridTemplateColumns = '1fr';
    return;
  }
  if (panel) panel.style.display = '';
  if (pair) pair.style.gridTemplateColumns = '1fr 1fr';
  if (!panel) return;
  const rowsHtml = renderRankRows(rows, true, 'web', sharedMaxHours, null);
  panel.innerHTML = `<h3><i data-lucide="globe"></i> 网站访问使用时长</h3><div class="rank-chart">${rowsHtml}${renderRankAxis(sharedMaxHours)}</div>`;
}

// === 黑名单管理面板（design §3.2）===
function computeRuleUsage(rule, usage) {
  // Match within the rule's platform_scope only, mirroring central semantics:
  // an app rule never counts across PC/Android, a domain rule covers web sites.
  const isWeb = rule.rule_type === 'domain';
  const scope = rule.platform_scope || (isWeb ? 'web' : 'pc');
  let seconds = 0;
  if (isWeb) {
    const sites = usage?.all?.sites || {};
    const pattern = normalizePattern(rule.pattern, 'domain');
    for (const [name, s] of Object.entries(sites)) {
      const norm = normalizePattern(name, 'domain');
      if (norm === pattern || norm.endsWith('.' + pattern)) seconds += Number(s || 0);
    }
    return seconds;
  }
  const targetPlatform = scope === 'android' ? 'android' : 'desktop';
  for (const dev of usage?.devices || []) {
    if (String(dev.platform || '').toLowerCase() !== targetPlatform) continue;
    const pattern = (rule.pattern || '').toLowerCase();
    for (const [name, s] of Object.entries(dev.apps || {})) {
      if (name.toLowerCase().includes(pattern)) seconds += Number(s || 0);
    }
  }
  return seconds;
}

function renderBlacklistManagementPanel() {
  const panel = document.getElementById('bl-management-panel');
  const list = document.getElementById('bl-management-list');
  if (!panel || !list) return;
  const enabled = fullRules.filter(r => r.enabled);
  if (enabled.length === 0) {
    panel.style.display = '';
    list.innerHTML = '<div class="bl-empty">暂无黑名单条目，点击上方图表旁的 + 按钮添加</div>';
    return;
  }
  panel.style.display = '';
  const withUsage = enabled.map(r => ({ rule: r, seconds: computeRuleUsage(r, multiDeviceState.usage) }))
    .sort((a, b) => b.seconds - a.seconds);
  const maxSeconds = Math.max(1, ...withUsage.map(x => x.seconds));

  const items = withUsage.map(({ rule: r, seconds }) => {
    const isWeb = r.rule_type === 'domain';
    const pscope = r.platform_scope || (isWeb ? 'web' : 'pc');
    const platformLabel = PLATFORM_LABELS[pscope] || pscope;
    const minutes = seconds / 60;
    const pct = ((seconds / maxSeconds) * 100).toFixed(1);
    const color = getUsageColor(r.pattern, true, isWeb);
    const durText = formatDurationShort(minutes);
    return `<div class="bl-item">
      <span class="bl-platform ${escapeHtml(pscope)}">${platformLabel}</span>
      <input class="bl-name-input" type="text" value="${escapeHtml(r.label || r.pattern)}" data-rule-id="${escapeHtml(r.rule_id)}" data-original="${escapeHtml(r.label || r.pattern)}" maxlength="100">
      <span class="bl-bar-wrap"><span class="bl-bar" style="width:${pct}%;background:${color}"></span></span>
      <span class="bl-duration">${durText}</span>
      <button class="bl-remove" data-rule-id="${escapeHtml(r.rule_id)}">移除</button>
    </div>`;
  }).join('');
  list.innerHTML = items;
}

// === 黑名单 CRUD（design §3.2 / §6）===
let confirmRemoveRuleId = null;

async function addBlacklistRule(pattern, label, ruleType, platformScope) {
  if (!pattern || !label || !ruleType || !platformScope) return;
  try {
    const resp = await fetch('/api/blacklist/rules', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rule_type: ruleType, pattern, label, platform_scope: platformScope, enabled: true }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      if (err.error === 'duplicate_rule') { showToast('该规则已存在', 'info'); return; }
      throw new Error(err.message || `HTTP ${resp.status}`);
    }
    showToast('已添加到黑名单', 'ok');
    await loadBlacklistRules();
    renderUsagePageState();
  } catch (e) { showToast('添加失败: ' + e.message, 'err'); }
}

async function doRemoveRule(ruleId) {
  try {
    const resp = await fetch(`/api/blacklist/rules/${encodeURIComponent(ruleId)}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    showToast('已从黑名单移除', 'ok');
    confirmRemoveRuleId = null;
    await loadBlacklistRules();
    renderUsagePageState();
  } catch (e) { showToast('移除失败: ' + e.message, 'err'); }
}

async function updateBlacklistRule(ruleId, label) {
  try {
    const resp = await fetch(`/api/blacklist/rules/${encodeURIComponent(ruleId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    showToast('标签已更新', 'ok');
  } catch (e) {
    showToast('更新失败: ' + e.message, 'err');
  } finally {
    await loadBlacklistRules();
    renderUsagePageState();
  }
}

function commitLabelEdit(input) {
  const ruleId = input.dataset.ruleId;
  const original = input.dataset.original;
  const value = input.value.trim();
  if (!value || value === original) {
    input.value = original; // no change
    return;
  }
  // Replace the input immediately to avoid a second focusout re-entry, then PATCH.
  const span = document.createElement('span');
  span.className = 'bl-name';
  span.textContent = value;
  input.replaceWith(span);
  updateBlacklistRule(ruleId, value);
}

function clickRemoveRule(ruleId, button) {
  if (confirmRemoveRuleId === ruleId) {
    doRemoveRule(ruleId);
  } else {
    if (confirmRemoveRuleId) resetRemoveButtons();
    confirmRemoveRuleId = ruleId;
    button.textContent = '确认移除';
    button.classList.add('confirm');
  }
}

function cancelRemoveConfirmation() {
  if (confirmRemoveRuleId) resetRemoveButtons();
}

function resetRemoveButtons() {
  confirmRemoveRuleId = null;
  document.querySelectorAll('.bl-remove.confirm').forEach(b => { b.textContent = '移除'; b.classList.remove('confirm'); });
}

// Event delegation for inline "+" and remove buttons. Bound once; the lists
// are re-rendered as innerHTML, so delegation avoids re-binding each render.
(function initBlacklistUI() {
  if (window.__blUIInited) return;
  window.__blUIInited = true;
  document.addEventListener('click', (e) => {
    const addBtn = e.target.closest('.rank-add');
    if (addBtn && !addBtn.classList.contains('disabled')) {
      if (addBtn.classList.contains('rank-remove')) {
        const ruleId = addBtn.dataset.ruleId;
        if (ruleId) doRemoveRule(ruleId);
        return;
      }
      addBlacklistRule(addBtn.dataset.pattern, addBtn.dataset.label, addBtn.dataset.type, addBtn.dataset.platform);
      return;
    }
    const removeBtn = e.target.closest('.bl-remove');
    if (removeBtn) {
      clickRemoveRule(removeBtn.dataset.ruleId, removeBtn);
      return;
    }
    if (confirmRemoveRuleId && !e.target.closest('.bl-remove')) cancelRemoveConfirmation();
  });
  document.addEventListener('keydown', (e) => {
    const input = e.target.closest('.bl-name-input');
    if (!input) return;
    if (e.key === 'Enter') { e.preventDefault(); commitLabelEdit(input); }
    else if (e.key === 'Escape') { input.value = input.dataset.original; input.blur(); }
  });
  document.addEventListener('focusout', (e) => {
    const input = e.target.closest('.bl-name-input');
    if (input) commitLabelEdit(input);
  });
})();

// ============================================================
// AI 上下文摘要弹窗（复用 showReportBody 样式与布局）
// ============================================================
let appAIContextText = '';

function showAppAIContext() {
  showReportBody(appAIContextText || '暂无 AI 上下文摘要', 'AI 上下文摘要');
}
