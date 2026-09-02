let syncRefreshTimer = null;
let syncRefreshInFlight = false;
let deviceManagementRoster = { devices: [], stale: false, loaded: false };

function showToast(msg, type) {
  type = type || 'info';
  let container = document.querySelector('.toast-container');
  if (!container) { container = document.createElement('div'); container.className = 'toast-container'; document.body.appendChild(container); }
  const el = document.createElement('div');
  el.className = 'toast-msg ' + type;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; setTimeout(() => el.remove(), 300); }, 2500);
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  })[char]);
}

function deviceStatusLabel(status) {
  return status === 'connected' ? '连接中' : status === 'disconnected' ? '已断开' : '未登记';
}

function platformLabel(platform) {
  const map = { desktop: '电脑', android: '手机', web: 'Web' };
  return map[String(platform || '').toLowerCase()] || '未知';
}

const DEVICE_MGMT_ERROR_MESSAGES = {
  cannot_delete_current_device: '不能删除当前正在使用的设备',
  device_not_found: '设备不存在或已删除',
  credential_source_not_mutable: '中央凭据配置当前不支持设备删除',
  credential_cleanup_failed: '设备已停用，但凭据清理未完成，请稍后重试',
  invalid_display_name: '设备名称需为 1-100 个字符',
  invalid_device_update: '请求格式错误',
  central_not_configured: '中央服务未配置',
  central_read_not_configured: '中央服务未配置',
};

function buildRosterFromSnapshot(snapshot) {
  const local = snapshot?.local || {};
  const roster = [];
  if (local.device_id) {
    roster.push({
      device_id: local.device_id,
      device_key: local.device_key || 'local',
      platform: local.platform || 'desktop',
      display_name: local.display_name || local.hostname || '本机',
      reported_name: local.hostname || local.display_name,
      is_current: true,
      status: 'connected',
    });
  }
  for (const d of snapshot?.devices || []) {
    roster.push({
      device_id: d.device_id,
      device_key: d.device_key,
      platform: d.platform,
      display_name: d.display_name,
      reported_name: null,
      is_current: false,
      status: d.status,
      last_seen_at: d.last_seen_at || d.last_received_at,
    });
  }
  return roster;
}

function renderCentralStatusSection(centralStatus) {
  const centralOutbox = centralStatus?.outbox || centralStatus?.last_result?.outbox || {};
  const centralStatusAvailable = centralStatus !== null;
  const centralRunning = centralStatus?.state === 'running';
  const centralConfigured = centralStatus?.configured === true;
  const centralError = centralStatus?.last_result?.error || centralStatus?.outbox_error;
  const centralFeedback = !centralStatusAvailable
    ? '无法读取中央同步状态，请稍后刷新。'
    : centralRunning
    ? '正在上传本机数据到中央服务…'
    : centralError
      ? `最近上传失败：${escapeHtml(centralError)}`
      : `待发送 ${centralOutbox.pending || 0} 条 · 已确认 ${centralOutbox.acked || 0} 条 · 拒绝 ${centralOutbox.rejected || 0} 条`;
  return `
    <div class="device-card-detail" style="margin-top:8px;">
      中央服务 ${escapeHtml(centralStatus?.central_base_url || '地址未配置')} · ${centralConfigured ? '已配置' : '未配置'} · 本机队列共 ${centralOutbox.total || 0} 条
    </div>
    <button id="central-sync-now-button" onclick="syncCentralNow()" ${centralRunning || !centralConfigured ? 'disabled' : ''} style="margin-top:10px;padding:7px 11px;border:0;border-radius:7px;background:var(--accent);color:#fff;cursor:${centralRunning ? 'wait' : 'pointer'}">${centralRunning ? '正在上传…' : '立即上传到中央服务'}</button>
    <div id="central-sync-feedback" class="sub" style="margin-top:6px">${centralFeedback}</div>`;
}

function formatDurationMinutes(totalMinutes) {
  const minutes = Math.max(0, Math.round(totalMinutes));
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (h > 0) return `${h}小时${m}分钟`;
  if (m > 0) return `${m}分钟`;
  return '0分钟';
}

function computeDeviceUsageSeconds(device, stats) {
  const source = stats || {};
  if (source.hourly_online && typeof source.hourly_online === 'object') {
    return Object.values(source.hourly_online).reduce((sum, s) => sum + Number(s || 0), 0);
  }
  if (source.hourly && typeof source.hourly === 'object') {
    return Object.values(source.hourly).reduce((sum, s) => sum + Number(s || 0), 0);
  }
  if (source.today?.hourly_online && typeof source.today.hourly_online === 'object') {
    return Object.values(source.today.hourly_online).reduce((sum, s) => sum + Number(s || 0), 0);
  }
  if (source.today?.hourly && typeof source.today.hourly === 'object') {
    return Object.values(source.today.hourly).reduce((sum, s) => sum + Number(s || 0), 0);
  }
  return 0;
}

function renderDeviceMgmtCard(device, { statsById, centralStatus, disabled }) {
  const isCurrent = device.is_current === true;
  const platformText = platformLabel(device.platform);
  const display = escapeHtml(device.display_name || device.reported_name || device.device_id);
  const reported = device.reported_name && String(device.reported_name) !== String(device.display_name) ? escapeHtml(device.reported_name) : '';
  const stats = statsById[device.device_id];
  const connected = device.status === 'connected' || stats?.status === 'connected';
  const lastSeenAt = device.last_seen_at || stats?.last_received_at || stats?.last_seen_at;
  const lastSeenText = lastSeenAt ? relativeConnectionText(lastSeenAt) : '无连接记录';
  const statusTag = `<span class="device-status ${connected ? 'connected' : ''}">${connected ? '连接中' : '已断开'}</span>`;
  const usageSeconds = computeDeviceUsageSeconds(device, stats);
  const usageText = `今日用量时长: ${formatDurationMinutes(usageSeconds / 60)}`;
  const currentBadge = isCurrent ? '<span class="current-badge">当前设备</span>' : '';
  const saveBtn = `<button class="primary" data-action="save" data-device-id="${escapeHtml(device.device_id)}" disabled>保存</button>`;
  const input = `<input class="device-name-input" type="text" value="${display}" data-original="${display}" data-device-id="${escapeHtml(device.device_id)}" ${disabled ? 'disabled' : ''}>`;
  const deleteBtn = isCurrent
    ? ''
    : `<button class="danger" data-action="delete" data-device-id="${escapeHtml(device.device_id)}" ${disabled ? 'disabled' : ''}>删除设备</button>`;
  const offlineBanner = disabled ? '<div class="offline-banner">当前离线中，重命名和删除已禁用</div>' : '';
  const centralSection = isCurrent ? renderCentralStatusSection(centralStatus) : '';

  return `<div class="stat-card device-mgmt-card ${isCurrent ? 'current' : ''}" data-device-id="${escapeHtml(device.device_id)}">
    <div class="device-mgmt-header">
      <span class="device-platform">${platformText}</span>
      ${statusTag}
      ${currentBadge}
    </div>
    <div class="device-name-wrap">
      ${input}
      ${saveBtn}
    </div>
    ${reported ? `<div class="device-reported">原始名称: ${reported}</div>` : ''}
    <div class="device-card-detail">${lastSeenText}</div>
    <div class="device-card-detail">${usageText}</div>
    ${centralSection}
    ${isCurrent ? '' : `<div class="device-actions">${deleteBtn}</div>`}
    ${offlineBanner}
  </div>`;
}

function safeDeviceSelector(deviceId) {
  try { return CSS.escape(deviceId); } catch (_) { return deviceId.replace(/"/g, '\\"'); }
}

function attachDeviceMgmtListeners(host) {
  if (deviceManagementRoster.stale) return;
  host.querySelectorAll('.device-name-input').forEach(input => {
    const deviceId = input.dataset.deviceId;
    input.addEventListener('input', () => {
      const changed = input.value.trim() !== input.dataset.original;
      const saveBtn = host.querySelector(`button[data-action="save"][data-device-id="${safeDeviceSelector(deviceId)}"]`);
      if (saveBtn) saveBtn.disabled = !changed;
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); saveDeviceName(deviceId); }
      if (e.key === 'Escape') {
        input.value = input.dataset.original;
        const saveBtn = host.querySelector(`button[data-action="save"][data-device-id="${safeDeviceSelector(deviceId)}"]`);
        if (saveBtn) saveBtn.disabled = true;
      }
    });
  });
  host.querySelectorAll('button[data-action="save"]').forEach(btn => {
    btn.addEventListener('click', (e) => { e.stopPropagation(); saveDeviceName(btn.dataset.deviceId); });
  });
  host.querySelectorAll('button[data-action="delete"]').forEach(btn => {
    btn.addEventListener('click', (e) => { e.stopPropagation(); deleteDevice(btn.dataset.deviceId); });
  });
}

async function saveDeviceName(deviceId) {
  const input = document.querySelector(`.device-mgmt-card[data-device-id="${safeDeviceSelector(deviceId)}"] .device-name-input`);
  const saveBtn = document.querySelector(`.device-mgmt-card[data-device-id="${safeDeviceSelector(deviceId)}"] button[data-action="save"]`);
  if (!input) return;
  const name = input.value.trim();
  if (!name || name.length > 100) { showToast('设备名称需为 1-100 个字符', 'err'); return; }
  if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = '保存中…'; }
  try {
    const resp = await fetch(`/api/device-management/${encodeURIComponent(deviceId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: name }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      showToast(DEVICE_MGMT_ERROR_MESSAGES[data.error] || '保存失败', 'err');
      await refreshSyncData();
      return;
    }
    showToast('设备名称已保存', 'ok');
    input.dataset.original = name;
    wishState.devicesCache = null;
    await refreshSyncData();
    loadMultiDeviceUsage().catch(() => {});
    loadLocationSummary().catch(() => {});
    loadEventsTimeline().catch(() => {});
  } catch (e) {
    showToast('保存失败: ' + e.message, 'err');
    await refreshSyncData();
  }
}

async function deleteDevice(deviceId) {
  const device = (deviceManagementRoster.devices || []).find(d => d.device_id === deviceId);
  const name = device?.display_name || device?.reported_name || '该设备';
  if (!confirm(`删除“${name}”？\n删除后该设备将无法继续同步；历史数据会保留。重新使用时需要重新连接。`)) return;
  try {
    const resp = await fetch(`/api/device-management/${encodeURIComponent(deviceId)}`, { method: 'DELETE' });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      if (data.error === 'device_not_found') {
        showToast('该设备已不存在或已被删除', 'info');
        await refreshSyncData();
        return;
      }
      showToast(DEVICE_MGMT_ERROR_MESSAGES[data.error] || '删除失败', 'err');
      return;
    }
    showToast('设备已删除', 'ok');
    document.querySelector(`.device-mgmt-card[data-device-id="${safeDeviceSelector(deviceId)}"]`)?.remove();
    wishState.devicesCache = null;
    await refreshSyncData();
    loadMultiDeviceUsage().catch(() => {});
    loadLocationSummary().catch(() => {});
    loadEventsTimeline().catch(() => {});
  } catch (e) {
    showToast('删除失败: ' + e.message, 'err');
  }
}

function initializeDeviceSettings() {
  const select = document.getElementById('day-boundary-hour');
  if (!select) return;
  for (let hour = 0; hour < 24; hour++) {
    select.add(new Option(`${String(hour).padStart(2, '0')}:00`, hour));
  }
  let confirmedValue = select.value;
  fetch('/api/settings').then(response => response.json()).then(settings => {
    select.value = settings.day_start_hour;
    confirmedValue = select.value;
  }).catch(() => {});
  select.onchange = async () => {
    const previousValue = confirmedValue;
    try {
      const response = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ day_start_hour: Number(select.value) }),
      });
      if (!response.ok) throw new Error('central shared settings update failed');
      const settings = await response.json();
      select.value = settings.day_start_hour;
      confirmedValue = select.value;
      await loadMultiDeviceUsage();
    } catch (_) {
      select.value = previousValue;
      window.alert('跨日时间修改失败，已保留原设置。');
    }
  };
}

initializeDeviceSettings();

function initializePcLoginStartup() {
  const input = document.getElementById('pc-login-startup');
  const hint = document.getElementById('pc-login-startup-status');
  if (!input) return;
  let confirmed = input.checked;
  const renderState = state => {
    input.checked = state.enabled === true;
    confirmed = input.checked;
    if (!hint) return;
    if (state.blocked_by_windows === true) {
      hint.textContent = '已被 Windows 或管理软件拦截；重新开启将恢复 LifeLink 启动项';
    } else if (state.enabled === true) {
      hint.textContent = '已启用：登录 Windows 后静默启动 PC 客户端';
    } else if (state.state === 'missing') {
      hint.textContent = '启动项缺失或被管理软件移除；重新开启将修复';
    } else {
      hint.textContent = '已关闭：不会随 Windows 登录启动';
    }
  };
  fetch('/api/runtime/login-startup').then(response => {
    if (!response.ok) throw new Error('登录后启动状态不可用');
    return response.json();
  }).then(state => {
    renderState(state);
  }).catch(() => {
    input.disabled = true;
  });
  input.onchange = async () => {
    const requested = input.checked;
    input.disabled = true;
    try {
      const response = await fetch('/api/runtime/login-startup', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: requested }),
      });
      const state = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(state.message || '登录后启动设置失败');
      renderState(state);
      showToast(confirmed ? '已启用登录后自动启动' : '已关闭登录后自动启动', 'ok');
    } catch (error) {
      input.checked = confirmed;
      showToast(error.message, 'err');
    } finally {
      input.disabled = false;
    }
  };
}

initializePcLoginStartup();

function renderSyncDeviceCards(snapshot, centralStatus = null, managementRoster = null) {
  const cards = document.getElementById('sync-device-cards');
  if (!cards) return;
  let roster = [];
  let stale = false;
  if (managementRoster && Array.isArray(managementRoster.devices)) {
    roster = managementRoster.devices;
    stale = !!managementRoster.stale;
  }
  if (!roster.length) {
    roster = buildRosterFromSnapshot(snapshot);
    stale = true;
  }
  const statsById = {};
  for (const d of snapshot?.devices || []) statsById[d.device_id] = d;
  const disabled = stale;

  // Current device first, then others.
  const currentIndex = roster.findIndex(d => d.is_current);
  const ordered = currentIndex >= 0
    ? [roster[currentIndex], ...roster.slice(0, currentIndex), ...roster.slice(currentIndex + 1)]
    : roster;

  if (ordered.length === 0) {
    cards.innerHTML = `
      <div class="stat-card warning"><div class="label">本机</div><div class="value" style="color: var(--danger)">中央未配置</div><div class="sub">请先配置中央服务，或刷新页面获取设备名册</div></div>
      <div class="stat-card"><div class="label">其他设备</div><div class="value" style="color: var(--text-secondary)">尚无设备记录</div><div class="sub">设备成功上传后会在这里显示</div></div>`;
    return;
  }

  cards.innerHTML = ordered.map(device => renderDeviceMgmtCard(device, { statsById, centralStatus, disabled })).join('');
  attachDeviceMgmtListeners(cards);
}

async function syncCentralNow() {
  const button = document.getElementById('central-sync-now-button');
  const feedback = document.getElementById('central-sync-feedback');
  if (button) { button.disabled = true; button.textContent = '正在上传…'; }
  if (feedback) feedback.textContent = '正在收集本机最新数据并上传到中央服务…';
  try {
    const response = await fetch('/api/sync/central', { method: 'POST' });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '中央上传未能启动');
    if (result.status === 'already_running' && feedback) {
      feedback.textContent = '已有中央上传正在进行，正在等待完成…';
    }
    for (let attempt = 0; attempt < 20; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 750));
      const statusResponse = await fetch('/api/sync/central');
      const status = await statusResponse.json();
      if (!statusResponse.ok) throw new Error(status.error || '无法读取中央上传状态');
      if (status.state !== 'running') {
        await refreshSyncData();
        return;
      }
    }
    if (feedback) feedback.textContent = '上传仍在进行；页面会在下一次刷新时更新待发送与确认数量。';
  } catch (error) {
    if (feedback) feedback.textContent = `无法上传到中央服务：${error.message}`;
    if (button) { button.disabled = false; button.textContent = '立即上传到中央服务'; }
  }
}

function renderSyncUnavailable() {
  document.getElementById('sync-device-cards').innerHTML = `
    <div class="stat-card warning"><div class="label">本机</div><div class="value" style="color: var(--danger)">客户端未运行</div><div class="sub">请启动 pc-dashboard/start_central_client.bat；中央服务端需另行启动 central-server/start_server.bat</div></div>
    <div class="stat-card"><div class="label">远端设备</div><div class="value">--</div><div class="sub">等待本机服务恢复</div></div>`;
}

function renderSyncReadFailure(error) {
  const detail = escapeHtml(error?.message || '未知读取错误');
  document.getElementById('sync-device-cards').innerHTML = `
    <div class="stat-card warning"><div class="label">本机</div><div class="value" style="color: var(--warning)">客户端运行中</div><div class="sub">设备或中央数据暂时读取失败，页面将自动重试。${detail}</div></div>
    <div class="stat-card"><div class="label">远端设备</div><div class="value">暂不可用</div><div class="sub">这不代表本机客户端已经停止</div></div>`;
}

async function isLocalClientHealthy() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 3000);
  try {
    const response = await fetch(`/health?_=${Date.now()}`, {
      cache: 'no-store',
      signal: controller.signal,
    });
    return response.ok;
  } catch (_) {
    return false;
  } finally {
    clearTimeout(timeout);
  }
}

async function updateSidebarServerStatus() {
  const statusEl = document.getElementById('sidebar-server-status');
  if (!statusEl) return;
  const textEl = document.getElementById('sidebar-server-text');
  let healthy = false;
  try {
    const response = await fetch('/api/central-health', { cache: 'no-store' });
    if (response.ok) {
      const data = await response.json();
      healthy = data.connected === true;
    }
  } catch (_) {
    healthy = false;
  }
  statusEl.classList.toggle('connected', healthy);
  if (textEl) textEl.textContent = healthy ? '已连接' : '已断开';
}

async function loadDeviceManagementRoster() {
  const response = await fetch('/api/device-management');
  const stale = response.headers.get('X-Life-Radio-Cache') === 'stale';
  if (!response.ok) throw new Error(`device management unavailable: ${response.status}`);
  const data = await response.json();
  deviceManagementRoster = { devices: data.devices || [], stale, loaded: true };
  return deviceManagementRoster;
}

function hasPendingDeviceNameEdit() {
  return [...document.querySelectorAll('.device-name-input')].some(input =>
    document.activeElement === input || input.value.trim() !== input.dataset.original
  );
}

async function refreshSyncData() {
  if (syncRefreshInFlight) return;
  syncRefreshInFlight = true;
  try {
    // The editable roster is the useful first paint. Start the slower status
    // requests at the same time, but do not make cards wait for usage data.
    const rosterPromise = loadDeviceManagementRoster().catch(() => null);
    const snapshotPromise = loadMultiDeviceSnapshot(true).then(
      value => ({ value, error: null }),
      error => ({ value: null, error })
    );
    const centralStatusPromise = fetch('/api/sync/central').then(async response =>
      response.ok ? response.json() : null
    ).catch(() => null);

    const roster = await rosterPromise;
    if (roster && !hasPendingDeviceNameEdit()) {
      renderSyncDeviceCards(multiDeviceState.snapshot, null, roster);
    }

    const [snapshotResult, centralStatus] = await Promise.all([snapshotPromise, centralStatusPromise]);
    if (snapshotResult.error) throw snapshotResult.error;
    const deviceSnapshot = snapshotResult.value;
    if (!hasPendingDeviceNameEdit()) {
      renderSyncDeviceCards(deviceSnapshot, centralStatus, roster);
    }
  } catch (error) {
    const localClientHealthy = await isLocalClientHealthy();
    if (!hasPendingDeviceNameEdit()) {
      if (localClientHealthy) renderSyncReadFailure(error);
      else renderSyncUnavailable();
    }
  } finally {
    syncRefreshInFlight = false;
    updateSidebarServerStatus().catch(() => {});
  }
}


const MULTI_DEVICE_SELECTION_KEY = 'life-radio-selected-device';
const multiDeviceState = {
  selectedKey: localStorage.getItem(MULTI_DEVICE_SELECTION_KEY) || 'all',
  snapshot: null,
  snapshotPromise: null,
  roster: [],
  usage: null,
  location: null,
};

function normalizedDeviceName(value) {
  return String(value || '').trim().toLocaleLowerCase();
}

function displayDeviceName(value) {
  return String(value || '')
    .replace(/\s*[（(]同步来源[）)]\s*$/u, '')
    .trim();
}

function buildMultiDeviceRoster(snapshot) {
  const local = snapshot?.local || {};
  const roster = [{
    device_key: local.device_key || 'local',
    display_name: local.display_name || local.hostname || '本机',
    platform: 'desktop',
    is_local: true,
    status: 'connected',
    last_connected_at: new Date().toISOString(),
  }];
  (snapshot?.devices || []).forEach(device => {
    if (!device?.device_key || device.device_key === roster[0].device_key) return;
    roster.push({ ...device, is_local: false });
  });
  const localEntry = roster.shift();
  roster.sort((a, b) => {
    if (a.status !== b.status) return a.status === 'connected' ? -1 : 1;
    return String(b.last_connected_at || b.last_received_at || '')
      .localeCompare(String(a.last_connected_at || a.last_received_at || ''));
  });
  return [localEntry, ...roster];
}

function setMultiDeviceSnapshot(snapshot) {
  multiDeviceState.snapshot = snapshot;
  multiDeviceState.roster = buildMultiDeviceRoster(snapshot);
  if (
    multiDeviceState.selectedKey !== 'all' &&
    !multiDeviceState.roster.some(device => device.device_key === multiDeviceState.selectedKey)
  ) {
    multiDeviceState.selectedKey = 'all';
    localStorage.setItem(MULTI_DEVICE_SELECTION_KEY, 'all');
  }
  renderAllMultiDeviceViews();
}

async function ensureMultiDeviceSnapshot() {
  return loadMultiDeviceSnapshot(false);
}

async function loadMultiDeviceSnapshot(force = false) {
  if (!force && multiDeviceState.snapshot) return multiDeviceState.snapshot;
  if (multiDeviceState.snapshotPromise) return multiDeviceState.snapshotPromise;
  multiDeviceState.snapshotPromise = fetch('/api/devices')
    .then(response => {
      if (!response.ok) throw new Error('设备状态不可用');
      return response.json();
    })
    .then(snapshot => {
      setMultiDeviceSnapshot(snapshot);
      return snapshot;
    })
    .finally(() => {
      multiDeviceState.snapshotPromise = null;
    });
  return multiDeviceState.snapshotPromise;
}

function relativeConnectionText(timestamp) {
  if (!timestamp) return '无连接记录';
  const elapsed = Math.max(0, Date.now() - new Date(timestamp).getTime());
  if (!Number.isFinite(elapsed)) return '无连接记录';
  const minutes = Math.floor(elapsed / 60000);
  if (minutes < 1) return '刚刚连接';
  if (minutes < 60) return `${minutes} 分钟前连接`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前连接`;
  return `${Math.floor(hours / 24)} 天前连接`;
}

function categoryDeviceFor(rosterDevice, categoryDevices) {
  if (!rosterDevice) return null;
  return (categoryDevices || []).find(device => device.device_key === rosterDevice.device_key)
    || (categoryDevices || []).find(device =>
      normalizedDeviceName(device.display_name) === normalizedDeviceName(rosterDevice.display_name)
    )
    || null;
}

function renderSharedDeviceCards(hostId, categoryDevices, metricText) {
  const host = document.getElementById(hostId);
  if (!host) return;
  host.classList.add('device-card-grid');
  const entries = [
    { device_key: 'all', display_name: '所有设备', platform: 'aggregate', status: null },
    ...multiDeviceState.roster,
  ];
  host.innerHTML = entries.map(device => {
    const selected = device.device_key === multiDeviceState.selectedKey;
    const categoryDevice = device.device_key === 'all'
      ? null : categoryDeviceFor(device, categoryDevices);
    const isAll = device.device_key === 'all';
    const connected = device.status === 'connected';
    const title = isAll
      ? '总集'
      : `${device.is_local ? '本机 · ' : ''}${escapeHtml(displayDeviceName(device.display_name))}`;
    const status = isAll
      ? '<span class="device-status">聚合视图</span>'
      : `<span class="device-status ${connected ? 'connected' : ''}">${connected ? '连接中' : '已断开'}</span>`;
    const detail = isAll
      ? '汇总当前名单中的全部设备'
      : connected
        ? `${escapeHtml(String(device.platform || 'unknown'))} · 当前可用`
        : `${escapeHtml(String(device.platform || 'unknown'))} · ${escapeHtml(relativeConnectionText(device.last_connected_at || device.last_received_at))}`;
    return `<div class="stat-card device-card ${selected ? 'selected' : ''}" role="button" tabindex="0" data-device-key="${escapeHtml(device.device_key)}" aria-pressed="${selected}">
      <span class="selection-mark">✓</span>
      <div class="device-card-title">${title}</div>
      ${status}
      <div class="device-card-metric">${escapeHtml(metricText(device, categoryDevice))}</div>
      <div class="device-card-detail">${detail}</div>
    </div>`;
  }).join('');
  const select = key => {
    multiDeviceState.selectedKey = key;
    localStorage.setItem(MULTI_DEVICE_SELECTION_KEY, key);
    renderAllMultiDeviceViews();
  };
  host.querySelectorAll('[data-device-key]').forEach(card => {
    card.addEventListener('click', () => select(card.dataset.deviceKey));
    card.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        select(card.dataset.deviceKey);
      }
    });
  });
}

function selectionMatchesDevice(deviceKey, displayName) {
  if (multiDeviceState.selectedKey === 'all') return true;
  if (deviceKey === multiDeviceState.selectedKey) return true;
  const rosterDevice = multiDeviceState.roster.find(
    device => device.device_key === multiDeviceState.selectedKey
  );
  return Boolean(
    rosterDevice &&
    normalizedDeviceName(rosterDevice.display_name) === normalizedDeviceName(displayName)
  );
}
