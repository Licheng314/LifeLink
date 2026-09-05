let activityCanvasResizeHandler = null;
let activityChartHits = [];
let locationLoadGeneration = 0;
const ACTIVITY_META = {
  stationary: { label: '静止', color: '#4caf50' }, walking: { label: '步行', color: '#f3c43b' },
  running: { label: '跑步', color: '#f28c28' }, transport: { label: '交通工具', color: '#4385d1' },
};

function activityClock(value) {
  const date = new Date(value);
  return !value || Number.isNaN(date.getTime()) ? '--:--' : date.toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hour12: false });
}
function activityDuration(seconds) {
  const minutes = Math.max(0, Math.round(Number(seconds || 0) / 60));
  return minutes >= 60 ? `${Math.floor(minutes / 60)}小时${minutes % 60}分` : `${minutes}分钟`;
}
function activityDistance(meters) {
  const value = Math.max(0, Number(meters || 0));
  return value >= 1000 ? `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)} km` : `${Math.round(value)} m`;
}
function activityHeightRatio(distance) {
  const km = Math.max(0, Number(distance || 0)) / 1000;
  return km <= 1 ? 0.15 + km * 0.55 : Math.min(1, 0.70 + Math.log10(km) * 0.18);
}
function activityDisplayRatio(interval) {
  return interval.state === 'stationary' ? 0.15 : activityHeightRatio(interval.distance_m);
}
function activityCoordinates(interval) {
  const latitude = interval.latitude, longitude = interval.longitude;
  return typeof latitude === 'number' && Number.isFinite(latitude) && typeof longitude === 'number' && Number.isFinite(longitude)
    ? `${latitude.toFixed(6)}, ${longitude.toFixed(6)}` : '暂无经纬度';
}

function drawActivityChart(data) {
  const canvas = document.getElementById('activity-state-chart');
  if (!canvas) return;
  const context = canvas.getContext('2d'), ratio = window.devicePixelRatio || 1;
  const width = Math.max(420, canvas.clientWidth || 900), height = Math.max(240, canvas.clientHeight || 280);
  canvas.width = width * ratio; canvas.height = height * ratio; context.setTransform(ratio, 0, 0, ratio, 0, 0); context.clearRect(0, 0, width, height); activityChartHits = [];
  const left = 48, right = 16, top = 18, bottom = 34, plotWidth = width - left - right, plotHeight = height - top - bottom;
  const from = new Date(data.from).getTime(), to = new Date(data.to).getTime();
  context.strokeStyle = '#d9dce3'; context.fillStyle = '#747986'; context.font = '11px sans-serif';
  for (const [distance, label] of [[100, '0.1km'], [1000, '1km'], [10000, '10km']]) {
    const y = top + plotHeight * (1 - activityHeightRatio(distance));
    context.save(); context.setLineDash([3, 4]); context.beginPath(); context.moveTo(left, y); context.lineTo(left + plotWidth, y); context.stroke(); context.restore();
    context.fillText(label, 4, y + 4);
  }
  for (let hour = 0; hour <= 24; hour += 3) {
    const x = left + plotWidth * hour / 24; context.beginPath(); context.moveTo(x, top); context.lineTo(x, top + plotHeight); context.stroke();
    context.fillText(activityClock(new Date(from + hour * 3600000).toISOString()), Math.min(width - 36, x - 14), height - 10);
  }
  for (const interval of data.activity_state?.intervals || []) {
    const start = Math.max(from, new Date(interval.start_at).getTime()), end = Math.min(to, new Date(interval.end_at).getTime());
    if (!(end > start)) continue;
    const meta = ACTIVITY_META[interval.state] || ACTIVITY_META.stationary;
    const x = left + plotWidth * (start - from) / (to - from), barWidth = Math.max(2, plotWidth * (end - start) / (to - from));
    const barHeight = Math.max(5, plotHeight * activityDisplayRatio(interval));
    const barTop = top + plotHeight - barHeight;
    context.fillStyle = meta.color; context.beginPath();
    if (typeof context.roundRect === 'function') context.roundRect(x, barTop, barWidth, barHeight, Math.min(3, barWidth / 2));
    else context.rect(x, barTop, barWidth, barHeight);
    context.fill();
    activityChartHits.push({ x, y: barTop, width: barWidth, height: barHeight, interval });
  }
  context.fillStyle = '#747986'; context.fillText('业务日起点', 4, height - 10);
  // “现在”红线：仅当前业务日显示，与事件业务日时间轴一致
  if (typeof isHistoricalDataView !== 'function' || !isHistoricalDataView()) {
    const nowMs = Date.now();
    if (nowMs >= from && nowMs <= to) {
      const nowX = left + plotWidth * (nowMs - from) / (to - from);
      const dangerColor = getComputedStyle(document.documentElement).getPropertyValue('--danger').trim() || '#ef4444';
      context.save();
      context.strokeStyle = dangerColor; context.lineWidth = 1.5; context.setLineDash([]);
      context.beginPath(); context.moveTo(nowX, top - 2); context.lineTo(nowX, top + plotHeight + 2); context.stroke();
      context.fillStyle = dangerColor; context.font = '700 9px sans-serif'; context.textAlign = 'center';
      context.fillText('现在', nowX, Math.max(9, top - 5));
      context.textAlign = 'start'; context.restore();
    }
  }
}

function setupActivityChartHover() {
  const canvas = document.getElementById('activity-state-chart'), tooltip = document.getElementById('activity-chart-tooltip');
  if (!canvas || !tooltip || canvas.dataset.hoverBound) return;
  canvas.dataset.hoverBound = '1';
  canvas.addEventListener('mousemove', event => {
    const rect = canvas.getBoundingClientRect(), x = (event.clientX - rect.left) * canvas.clientWidth / rect.width, y = (event.clientY - rect.top) * canvas.clientHeight / rect.height;
    const hit = [...activityChartHits].reverse().find(item => x >= item.x && x <= item.x + item.width && y >= item.y && y <= item.y + item.height);
    if (!hit) { tooltip.hidden = true; canvas.style.cursor = 'default'; return; }
    const interval = hit.interval, meta = ACTIVITY_META[interval.state] || ACTIVITY_META.stationary;
    const measurement = interval.state === 'stationary' ? '与设备使用区间相交 · 固定基础高度' : `${interval.steps} 步 · 展示距离 ${activityDistance(interval.distance_m)}`;
    tooltip.innerHTML = `<strong>${meta.label}${interval.is_current ? ' · 当前区间' : ''}</strong><span>${activityClock(interval.start_at)}–${activityClock(interval.end_at)} · ${activityDuration(interval.duration_seconds)}</span><span>${escapeHtml(interval.address || '暂无定位信息')}</span><span>${activityCoordinates(interval)}</span><span>${measurement}</span><span>距离来源 ${interval.distance_source} · 置信度 ${interval.confidence}</span>`;
    tooltip.hidden = false; canvas.style.cursor = 'pointer';
    tooltip.style.left = `${Math.min(canvas.clientWidth - tooltip.offsetWidth - 8, Math.max(8, event.clientX - rect.left + 12))}px`;
    tooltip.style.top = `${Math.max(8, event.clientY - rect.top - tooltip.offsetHeight - 10)}px`;
  });
  canvas.addEventListener('mouseleave', () => { tooltip.hidden = true; canvas.style.cursor = 'default'; });
}

function renderPrimaryDevice(activity) {
  const target = document.getElementById('activity-primary-device'), devices = activity?.devices || [];
  if (!target) return;
  if (!devices.length) { target.innerHTML = '<span class="health-detail">暂无可选 Android 设备</span>'; return; }
  const historical = typeof isHistoricalDataView === 'function' && isHistoricalDataView();
  target.innerHTML = `<label for="activity-primary-select">主手机</label><select id="activity-primary-select" ${historical ? 'disabled' : ''}>${devices.map(device => `<option value="${escapeHtml(device.device_id)}" ${device.device_id === activity.primary_device_id ? 'selected' : ''}>${escapeHtml(device.display_name)} · 步数样本 ${device.step_sample_count} · 位置 ${device.location_observation_count}</option>`).join('')}</select><span>${historical ? '历史数据不修改当前主手机设置' : (activity.selection_source === 'configured' ? '中央已选定' : '按当日数据量自动选择')}</span>`;
  document.getElementById('activity-primary-select')?.addEventListener('change', async event => {
    event.target.disabled = true;
    try {
      const response = await fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ primary_health_device_id: event.target.value }) });
      if (!response.ok) throw new Error(await response.text());
      await loadLocationSummary(); if (typeof showToast === 'function') showToast('主手机已更新', 'success');
    } catch (_) {
      if (typeof showToast === 'function') showToast('主手机更新失败，已保留原设置', 'error'); event.target.disabled = false;
    }
  });
}

function renderActivityIntervals(activity) {
  const target = document.getElementById('activity-state-list'); if (!target) return;
  const intervals = activity?.intervals || [];
  target.innerHTML = intervals.length ? intervals.slice().reverse().map(interval => {
    const meta = ACTIVITY_META[interval.state] || ACTIVITY_META.stationary;
    const evidence = interval.state === 'stationary' ? '固定基础值' : `${interval.steps} 步 · ${activityDistance(interval.distance_m)}`;
    const width = Math.round(activityDisplayRatio(interval) * 1000) / 10;
    return `<article class="activity-state-row" style="--activity-color:${meta.color}"><div class="activity-state-main"><div class="activity-state-heading"><span class="activity-state-badge">${meta.label}</span><span>${activityClock(interval.start_at)}–${activityClock(interval.end_at)} · ${activityDuration(interval.duration_seconds)}</span>${interval.is_current ? '<b>当前区间</b>' : ''}</div><strong class="activity-state-address">${escapeHtml(interval.address || '暂无定位信息')}</strong><span class="activity-state-coordinates">${activityCoordinates(interval)}</span></div><div class="activity-distance"><div><span>展示距离</span><strong>${escapeHtml(evidence)}</strong></div><div class="activity-distance-track" title="${escapeHtml(evidence)}"><i style="width:${width}%;background:${meta.color}"></i></div></div></article>`;
  }).join('') : `<div class="activity-empty">${Number((getSelectedBusinessDate?.() || '').slice(5, 7)) || ''}月${Number((getSelectedBusinessDate?.() || '').slice(8, 10)) || ''}日没有活动状态记录</div>`;
}

function renderLocationSummary(data) {
  multiDeviceState.location = data; const activity = data.activity_state || {};
  const hasActivity = Array.isArray(activity.intervals) && activity.intervals.length > 0;
  renderPrimaryDevice(activity); renderActivityIntervals(activity); drawActivityChart(data); setupActivityChartHover();
  renderLocationMap(data);
  const chartPanel = document.getElementById('activity-state-chart-panel'), listPanel = document.getElementById('activity-state-list-panel');
  if (chartPanel) chartPanel.hidden = !hasActivity;
  if (listPanel) listPanel.hidden = false;
  const status = document.getElementById('activity-current-status');
  const historical = typeof isHistoricalDataView === 'function' && isHistoricalDataView();
  if (status) status.textContent = historical ? (activity.current ? `最后状态：${ACTIVITY_META[activity.current.state]?.label || '未知'} · ${activityClock(activity.current.end_at)}` : '该业务日暂无双来源可靠活动状态') : (activity.current ? `当前：${ACTIVITY_META[activity.current.state]?.label || '未知'} · ${activityClock(activity.current.end_at)} 更新` : (hasActivity ? '当前状态暂无双来源可靠证据' : '步数或定位证据不足，暂不生成活动状态'));
  const aiText = data.ai_summary || '暂无位置摘要';
  if (typeof window !== 'undefined') window.locationAIContextText = aiText;
  const aiBtn = document.getElementById('location-ai-btn');
  if (aiBtn) aiBtn.style.display = '';
  if (activityCanvasResizeHandler) window.removeEventListener('resize', activityCanvasResizeHandler);
  activityCanvasResizeHandler = () => drawActivityChart(multiDeviceState.location || data); window.addEventListener('resize', activityCanvasResizeHandler);
}

async function loadLocationSummary() {
  const generation = ++locationLoadGeneration;
  await ensureMultiDeviceSnapshot(); const date = typeof getSelectedBusinessDate === 'function' ? getSelectedBusinessDate() : '';
  const response = await fetch(`/api/locations${date ? `?date=${encodeURIComponent(date)}` : ''}`);
  if (!response.ok) throw new Error(`位置数据加载失败：HTTP ${response.status}`);
  const data = await response.json();
  if (generation !== locationLoadGeneration) return;
  renderLocationSummary(data);
}
function usageMetric(device, categoryDevice) {
  const source = device.device_key === 'all' ? multiDeviceState.usage?.all : categoryDevice;
  const seconds = Object.values(source?.apps || {}).reduce((sum, value) => sum + Number(value || 0), 0);
  return `今日应用用量 ${Math.round(seconds / 60)} 分钟`;
}
function renderUsagePageState() {
  const summary = multiDeviceState.usage; if (!summary) return;
  renderSharedDeviceCards('usage-device-cards', summary.devices, usageMetric); renderUsageScope(summary, multiDeviceState.selectedKey);
}
async function loadMultiDeviceUsage() {
  const date = typeof getSelectedBusinessDate === 'function' ? getSelectedBusinessDate() : '';
  const historical = typeof isHistoricalDataView === 'function' && isHistoricalDataView();
  const usageUrl = `/api/usage${date ? `?date=${encodeURIComponent(date)}` : ''}`;
  const [response, , liveResponse] = await Promise.all([fetch(usageUrl), ensureMultiDeviceSnapshot(), historical ? Promise.resolve(null) : fetch('/api/live-usage').catch(() => null)]);
  if (!response.ok) throw new Error('用量数据不可用');
  const summary = await response.json(); usageDayStartHour = Number(summary.day_start_hour || 0); multiDeviceState.usage = summary;
  const warning = document.getElementById('usage-collection-warning');
  if (warning) {
    let browserStatus = null;
    if (liveResponse?.ok) {
      try {
        const live = await liveResponse.json();
        browserStatus = live?.collection?.browser?.status;
      } catch (_) {
        browserStatus = null;
      }
    }
    if (browserStatus === 'port_in_use') {
      warning.textContent = '网站采集未启动：本机 5600 端口已被占用，可能是 ActivityWatch 仍在运行。请退出 ActivityWatch 后重启 Life Link。应用用量统计不受影响。';
      warning.style.display = '';
    } else {
      warning.style.display = 'none';
    }
  }
  document.getElementById('usage-device-tabs')?.replaceChildren(); renderUsagePageState();
  const aiResponse = await fetch(`/api/ai-context/usage.md?date=${encodeURIComponent(summary.date)}`);
  if (aiResponse.ok) {
    appAIContextText = await aiResponse.text();
    const btn = document.getElementById('app-ai-btn');
    if (btn) btn.style.display = '';
  }
  return summary;
}
function renderAllMultiDeviceViews() { if (!blacklistRulesLoaded) loadBlacklistRules().then(renderUsagePageState); else renderUsagePageState(); }
function setupLocationPage() {
  const page = document.getElementById('page-location'); if (!page) return;
  const legend = Object.values(ACTIVITY_META).map(meta => `<span class="activity-legend-item"><i style="background:${meta.color}"></i>${meta.label}</span>`).join('');
  page.innerHTML = `<div class="page-header" style="display:flex;align-items:flex-end;justify-content:space-between;gap:20px"><div><h2><i data-lucide="map-pin"></i> 位置轨迹</h2><div class="desc">当前业务日 · 按中央跨日设置融合步数、位移与全部设备真实使用</div></div><button id="location-ai-btn" class="report-body-btn" style="display:none" onclick="showLocationAIContext()">AI 上下文摘要</button></div><div class="activity-toolbar"><div id="activity-primary-device" class="activity-primary-device">加载主手机…</div><div id="activity-current-status" class="activity-current-status">加载当前状态…</div></div><section id="location-map-panel" class="chart-panel"><h3><i data-lucide="map"></i> 停留地图</h3><div id="location-map" class="location-map"></div><div id="location-map-legend" class="location-map-legend"></div></section><section id="activity-state-chart-panel" class="chart-panel"><h3><i data-lucide="compass"></i> 活动状态</h3><div class="activity-legend">${legend}</div><div class="activity-chart-wrap"><canvas id="activity-state-chart"></canvas><div id="activity-chart-tooltip" class="activity-chart-tooltip" hidden></div></div></section><section id="activity-state-list-panel" class="chart-panel"><h3>活动状态区间</h3><div id="activity-state-list"></div></section>`;
  if (typeof updateHistoricalDataBadges === 'function') updateHistoricalDataBadges();
}
setupLocationPage(); document.querySelector('[data-page="location"]')?.addEventListener('click', () => loadLocationSummary().catch(console.warn));
setTimeout(() => { if (document.getElementById('page-location')?.classList.contains('active')) loadLocationSummary().catch(console.warn); }, 150);

// === 停留地图 ===
let locationMapInstance = null;

function isValidMapCoord(lat, lng) {
  return typeof lat === 'number' && Number.isFinite(lat) && typeof lng === 'number' && Number.isFinite(lng);
}

// 过滤 GPS 漂移离群点：计算质心，剔除偏离质心超过 maxKm 的点
function filterOutlierCoords(points, getLat, getLng, maxKm = 100) {
  if (points.length <= 1) return points;
  // 质心
  let sumLat = 0, sumLng = 0;
  for (const p of points) { sumLat += getLat(p); sumLng += getLng(p); }
  const cLat = sumLat / points.length, cLng = sumLng / points.length;
  const cosLat = Math.cos(cLat * Math.PI / 180);
  const filtered = points.filter(p => {
    const dLat = (getLat(p) - cLat) * 111;
    const dLng = (getLng(p) - cLng) * 111 * cosLat;
    return Math.sqrt(dLat * dLat + dLng * dLng) <= maxKm;
  });
  // 如果过滤后点数少于原始的 50%，说明质心被漂移点拉偏，用中位数重新算
  if (filtered.length < points.length * 0.5 && points.length > 2) {
    const sortedLat = points.map(getLat).sort((a, b) => a - b);
    const sortedLng = points.map(getLng).sort((a, b) => a - b);
    const mid = Math.floor(points.length / 2);
    const mLat = sortedLat[mid], mLng = sortedLng[mid];
    const mCosLat = Math.cos(mLat * Math.PI / 180);
    return points.filter(p => {
      const dLat = (getLat(p) - mLat) * 111;
      const dLng = (getLng(p) - mLng) * 111 * mCosLat;
      return Math.sqrt(dLat * dLat + dLng * dLng) <= maxKm;
    });
  }
  return filtered;
}

function getStayMarkerStyle(durationSec, isActive) {
  if (isActive) return { size: 22, fillColor: '#22c55e', opacity: 1.0, showLabel: true };
  const minutes = durationSec / 60;
  if (minutes < 5)  return { size: 10, fillColor: '#93c5fd', opacity: 0.7, showLabel: false };
  if (minutes < 30) return { size: 16, fillColor: '#3b82f6', opacity: 0.85, showLabel: true };
  if (minutes < 60) return { size: 22, fillColor: '#1d4ed8', opacity: 0.9, showLabel: true };
  return { size: 30, fillColor: '#1e3a8a', opacity: 1.0, showLabel: true };
}

function formatStayDuration(sec) {
  const m = Math.round(sec / 60);
  if (m >= 60) { const h = Math.floor(m / 60), rem = m % 60; return rem > 0 ? h + '小时' + rem + '分' : h + '小时'; }
  return m + '分钟';
}

function renderLocationMap(data) {
  const container = document.getElementById('location-map');
  if (!container || typeof L === 'undefined') return;

  const rawStays = (data.segments || []).filter(s => s.kind === 'stay' && isValidMapCoord(s.latitude, s.longitude));
  const rawObs = (data.observations || []).filter(o => isValidMapCoord(o.latitude, o.longitude));
  // 合并所有点计算质心，统一过滤 GPS 漂移离群点（偏离主体 >100km）
  const allPoints = [
    ...rawStays.map(s => ({ lat: s.latitude, lng: s.longitude, _ref: s })),
    ...rawObs.map(o => ({ lat: o.latitude, lng: o.longitude, _ref: o })),
  ];
  const keptRefs = new Set(filterOutlierCoords(allPoints, p => p.lat, p => p.lng).map(p => p._ref));
  const stays = rawStays.filter(s => keptRefs.has(s));
  const observations = rawObs.filter(o => keptRefs.has(o));
  const legendEl = document.getElementById('location-map-legend');

  // 无数据时显示占位
  if (!stays.length && !observations.length) {
    container.innerHTML = '<div class="location-map-empty">本业务日暂无定位数据</div>';
    if (legendEl) legendEl.innerHTML = '';
    return;
  }

  // 初始化或复用地图实例
  if (locationMapInstance) {
    locationMapInstance.remove();
  }
  locationMapInstance = L.map(container, { zoomControl: true, attributionControl: true }).setView([29.56, 106.55], 12);

  const baseTiles = L.tileLayer('/map-tiles/vec/{z}/{x}/{y}.png', { maxZoom: 18, attribution: '天地图', updateWhenIdle: true });
  const labelsTiles = L.tileLayer('/map-tiles/cva/{z}/{x}/{y}.png', { maxZoom: 18, updateWhenIdle: true });
  let pendingTileLayers = 0;
  const setTileLoading = (loading) => container.classList.toggle('location-map-loading', loading);
  [baseTiles, labelsTiles].forEach(layer => {
    layer.on('loading', () => { pendingTileLayers += 1; setTileLoading(true); });
    layer.on('load', () => { pendingTileLayers = Math.max(0, pendingTileLayers - 1); if (!pendingTileLayers) setTileLoading(false); });
    layer.on('tileerror', () => setTileLoading(false));
    layer.addTo(locationMapInstance);
  });

  const bounds = L.latLngBounds([]);
  const tz = data.timezone || 'Asia/Shanghai';
  const fmtClock = (iso) => iso ? new Date(iso).toLocaleTimeString('zh-CN', { timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false }) : '--:--';

  // --- 观察点按时间去重并排序 ---
  const seen = new Set();
  const validObs = [];
  for (const o of observations) {
    const key = `${o.latitude.toFixed(4)},${o.longitude.toFixed(4)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    validObs.push(o);
  }
  validObs.sort((a, b) => (a.observed_at || '').localeCompare(b.observed_at || ''));

  // --- 渐变轨迹连线 + 长线段箭头 ---
  if (validObs.length >= 2) {
    const tMin = new Date(validObs[0].observed_at).getTime();
    const tMax = new Date(validObs[validObs.length - 1].observed_at).getTime();
    const tRange = Math.max(1, tMax - tMin);

    // 计算线段长度，找最长 20% 阈值
    const segs = [];
    for (let i = 0; i < validObs.length - 1; i++) {
      const a = validObs[i], b = validObs[i + 1];
      const dlat = (b.latitude - a.latitude) * 111000;
      const dlng = (b.longitude - a.longitude) * 111000 * Math.cos(a.latitude * Math.PI / 180);
      segs.push({ a, b, dist: Math.sqrt(dlat * dlat + dlng * dlng) });
    }
    const distSorted = segs.slice().sort((x, y) => y.dist - x.dist);
    const n20 = Math.max(1, Math.floor(segs.length * 0.2));
    const distThreshold = distSorted[n20 - 1].dist;

    for (const seg of segs) {
      const tMid = ((new Date(seg.a.observed_at).getTime() - tMin) + (new Date(seg.b.observed_at).getTime() - tMin)) / 2 / tRange;
      const hue = 120 * (1 - tMid);
      const segColor = `hsl(${Math.round(hue)},70%,50%)`;

      L.polyline([[seg.a.latitude, seg.a.longitude], [seg.b.latitude, seg.b.longitude]], {
        color: segColor, weight: 3, opacity: 0.6, lineCap: 'round'
      }).addTo(locationMapInstance);

      // 最长 20% 的线段画方向箭头
      if (seg.dist >= distThreshold && seg.dist > 200) {
        const midLat = (seg.a.latitude + seg.b.latitude) / 2;
        const midLng = (seg.a.longitude + seg.b.longitude) / 2;
        const angle = Math.atan2(seg.b.latitude - seg.a.latitude, -(seg.b.longitude - seg.a.longitude)) * 180 / Math.PI;
        const svg = `<svg width="22" height="14" viewBox="0 0 22 14" xmlns="http://www.w3.org/2000/svg"><polygon points="0,7 22,0 22,14" fill="${segColor}" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
        const arrowIcon = L.divIcon({ className: '', html: `<div style="transform:rotate(${angle.toFixed(1)}deg);width:22px;height:14px">${svg}</div>`, iconSize: [22, 14], iconAnchor: [11, 7] });
        L.marker([midLat, midLng], { icon: arrowIcon, interactive: false, zIndexOffset: 500 }).addTo(locationMapInstance);
      }
    }
  }

  // --- 观察点 ---
  validObs.forEach((o, idx) => {
    bounds.extend([o.latitude, o.longitude]);
    if (idx === 0 || idx === validObs.length - 1) return; // 起终点用特殊标记
    const dot = L.circleMarker([o.latitude, o.longitude], { radius: 4, color: '#4f46e5', fillColor: '#818cf8', fillOpacity: 0.8, weight: 1.5 });
    dot.bindTooltip(`${fmtClock(o.observed_at)} · 精度${Math.round(o.accuracy_m || 0)}m`, { direction: 'top', offset: [0, -6], className: 'stay-duration-tooltip' });
    dot.addTo(locationMapInstance);
  });

  // --- 起点终点标记 ---
  if (validObs.length >= 2) {
    const sO = validObs[0], eO = validObs[validObs.length - 1];
    const mkEndpointIcon = (bg, text) => L.divIcon({ className: '', html: `<div style="display:flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:50%;background:${bg};border:3px solid #fff;box-shadow:0 1px 6px rgba(0,0,0,.35);color:#fff;font-size:12px;font-weight:700;line-height:1">${text}</div>`, iconSize: [26, 26], iconAnchor: [13, 13] });
    const sM = L.marker([sO.latitude, sO.longitude], { icon: mkEndpointIcon('#22c55e', '起'), zIndexOffset: 900 }).addTo(locationMapInstance);
    sM.bindTooltip('起点 ' + fmtClock(sO.observed_at), { permanent: true, direction: 'top', offset: [0, -16], className: 'stay-duration-tooltip' });
    sM.bindPopup(`<div class="stay-popup-title" style="color:#16a34a">起点</div><div class="stay-popup-row"><strong>时间：</strong>${fmtClock(sO.observed_at)}</div>`, { maxWidth: 260 });
    const eM = L.marker([eO.latitude, eO.longitude], { icon: mkEndpointIcon('#ef4444', '终'), zIndexOffset: 900 }).addTo(locationMapInstance);
    eM.bindTooltip('终点 ' + fmtClock(eO.observed_at), { permanent: true, direction: 'top', offset: [0, -16], className: 'stay-duration-tooltip' });
    eM.bindPopup(`<div class="stay-popup-title" style="color:#dc2626">终点</div><div class="stay-popup-row"><strong>时间：</strong>${fmtClock(eO.observed_at)}</div>`, { maxWidth: 260 });
  }

  // --- 停留标记 ---
  for (const stay of stays) {
    const style = getStayMarkerStyle(stay.duration_seconds || 0, stay.is_active);
    const latlng = [stay.latitude, stay.longitude];
    bounds.extend(latlng);
    const icon = L.divIcon({ className: '', html: `<div class="stay-marker${stay.is_active ? ' active' : ''}" style="width:${style.size}px;height:${style.size}px;background:${style.fillColor};opacity:${style.opacity}"></div>`, iconSize: [style.size, style.size], iconAnchor: [style.size / 2, style.size / 2] });
    const marker = L.marker(latlng, { icon: icon }).addTo(locationMapInstance);
    if (style.showLabel) marker.bindTooltip(formatStayDuration(stay.duration_seconds || 0), { permanent: true, direction: 'top', offset: [0, -style.size / 2 - 2], className: 'stay-duration-tooltip' });
    const label = stay.label || '未知位置';
    marker.bindPopup(`<div class="stay-popup-title">${escapeHtml(label)}</div><div class="stay-popup-row"><strong>停留：</strong><span class="stay-popup-duration">${formatStayDuration(stay.duration_seconds || 0)}</span></div><div class="stay-popup-row"><strong>时间：</strong>${fmtClock(stay.observed_at)}</div><div class="stay-popup-row"><strong>坐标：</strong>${stay.latitude.toFixed(6)}, ${stay.longitude.toFixed(6)}</div>${stay.is_active ? '<div class="stay-popup-row" style="color:#16a34a;font-weight:700">● 当前活跃停留</div>' : ''}`, { maxWidth: 280 });
  }

  // 自适应视野
  if (bounds.isValid()) locationMapInstance.fitBounds(bounds, { padding: [50, 50] });

  // 图例
  if (legendEl) {
    legendEl.innerHTML = `
      <span class="lm-legend-item"><span style="width:20px;height:3px;background:linear-gradient(to right,#22c55e,#f59e0b,#ef4444);border-radius:2px;display:inline-block"></span>轨迹（绿=早 → 红=晚）</span>
      <span class="lm-legend-item"><span style="font-size:14px;color:#f59e0b">▶</span> 方向</span>
      <span class="lm-legend-item"><span style="width:16px;height:16px;background:#22c55e;border:2px solid #fff;border-radius:50%;color:#fff;font-size:9px;display:inline-flex;align-items:center;justify-content:center;font-weight:700">起</span>起点</span>
      <span class="lm-legend-item"><span style="width:16px;height:16px;background:#ef4444;border:2px solid #fff;border-radius:50%;color:#fff;font-size:9px;display:inline-flex;align-items:center;justify-content:center;font-weight:700">终</span>终点</span>
      <span class="lm-legend-item"><span style="width:8px;height:8px;background:#818cf8;border:1.5px solid #4f46e5;border-radius:50%;display:inline-block"></span>定位点</span>
      <span class="lm-legend-item"><span style="width:22px;height:14px;background:#1d4ed8;border-radius:50%;display:inline-block"></span>停留点（越大越久）</span>`;
  }

  // The page can still be settling from display:none to display:block when
  // the location request returns.  Recheck after layout and after tile work,
  // otherwise Leaflet may keep a zero-sized viewport until a manual refresh.
  const map = locationMapInstance;
  const refreshMapSize = () => { if (locationMapInstance === map) map.invalidateSize({ animate: false }); };
  requestAnimationFrame(refreshMapSize);
  setTimeout(refreshMapSize, 200);
  setTimeout(refreshMapSize, 800);
}

// ============================================================
// 位置 AI 上下文摘要弹窗（复用 showReportBody）
// ============================================================
function showLocationAIContext() {
  const text = (typeof window !== 'undefined' && window.locationAIContextText) || '暂无位置摘要';
  showReportBody(text, 'AI 上下文摘要');
}
