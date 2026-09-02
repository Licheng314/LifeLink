// ============================================================
// Navigation
// ============================================================
const ACTIVE_PAGE_KEY = 'life-radio-active-page';
const calendarState = { selectedDate: '', todayDate: '', weekStart: '', earliestDate: '', latestDate: '', days: new Map() };
let calendarLoadGeneration = 0;

function calendarDateIsValid(value) { return /^\d{4}-\d{2}-\d{2}$/.test(String(value || '')); }
function calendarAddDays(date, amount) {
  const [year, month, day] = String(date).split('-').map(Number);
  return new Date(Date.UTC(year, month - 1, day + amount)).toISOString().slice(0, 10);
}
function calendarWeekStart(date) {
  const day = new Date(`${date}T00:00:00Z`).getUTCDay();
  return calendarAddDays(date, -day);
}
function calendarDateFromUrl() {
  const value = new URLSearchParams(window.location.search).get('date');
  return calendarDateIsValid(value) ? value : '';
}
function getSelectedBusinessDate() { return calendarState.selectedDate || calendarDateFromUrl() || ''; }
function isHistoricalDataView() {
  const selected = getSelectedBusinessDate();
  const today = calendarState.todayDate || (typeof computeBizDate === 'function' ? computeBizDate().bizDate : '');
  return Boolean(selected && today && selected !== today);
}
function updateHistoricalDataBadges() {
  const historical = isHistoricalDataView();
  document.querySelectorAll('[data-history-badge]').forEach(badge => { badge.hidden = !historical; });
}
function formatCalendarBytes(bytes) {
  const value = Math.max(0, Number(bytes || 0));
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}
function calendarMonthLabel(start, end) {
  const [startYear, startMonth] = start.split('-').map(Number);
  const [endYear, endMonth] = end.split('-').map(Number);
  if (startYear === endYear && startMonth === endMonth) return `${startYear} 年 ${startMonth} 月`;
  if (startYear === endYear) return `${startYear} 年 ${startMonth}–${endMonth} 月`;
  return `${startYear} 年 ${startMonth} 月–${endYear} 年 ${endMonth} 月`;
}
function calendarBreakdown(day) {
  const labels = { usage: '用量', location: '位置', health: '健康', timeline: '时间线', other: '其他' };
  return Object.entries(labels).map(([key, label]) => {
    const module = day?.modules?.[key] || {};
    return `${label}：${formatCalendarBytes(module.bytes)} · ${Number(module.records || 0)} 条`;
  }).join('\n');
}
function renderBusinessCalendar() {
  const container = document.getElementById('business-calendar-days');
  const label = document.getElementById('calendar-week-label');
  const previousWeek = document.getElementById('calendar-prev-week');
  const nextWeek = document.getElementById('calendar-next-week');
  const returnToday = document.getElementById('calendar-return-today');
  const size = document.getElementById('calendar-day-size');
  if (!container || !calendarState.weekStart) return;
  const weekDates = Array.from({ length: 7 }, (_, index) => calendarAddDays(calendarState.weekStart, index));
  const selectedDay = calendarState.days.get(calendarState.selectedDate);
  if (label) label.textContent = calendarMonthLabel(calendarState.weekStart, weekDates[6]);
  if (previousWeek) previousWeek.disabled = Boolean(calendarState.earliestDate && calendarState.weekStart <= calendarWeekStart(calendarState.earliestDate));
  if (nextWeek) nextWeek.disabled = Boolean(calendarState.todayDate && calendarState.weekStart >= calendarWeekStart(calendarState.todayDate));
  container.innerHTML = weekDates.map(date => {
    const day = calendarState.days.get(date);
    const future = calendarState.todayDate && date > calendarState.todayDate;
    const disabled = future || (date !== calendarState.todayDate && !day?.available);
    return `<button type="button" class="calendar-day${day?.available ? ' has-data' : ''}${date === calendarState.selectedDate ? ' is-selected' : ''}${date === calendarState.todayDate ? ' is-today' : ''}" data-business-date="${date}" ${disabled ? 'disabled' : ''}>${Number(date.slice(8))}</button>`;
  }).join('');
  container.querySelectorAll('[data-business-date]').forEach(button => button.addEventListener('click', () => selectBusinessDate(button.dataset.businessDate)));
  if (returnToday) returnToday.hidden = !calendarState.todayDate || calendarState.weekStart === calendarWeekStart(calendarState.todayDate);
  if (size) {
    size.textContent = `当日数据量：${selectedDay ? formatCalendarBytes(selectedDay.total_bytes) : '暂无数据'}`;
    if (selectedDay) {
      size.dataset.breakdown = calendarBreakdown(selectedDay);
    } else {
      delete size.dataset.breakdown;
    }
    size.removeAttribute('title');
  }
  updateHistoricalDataBadges();
}
async function loadBusinessCalendarWeek() {
  if (!calendarState.weekStart) return;
  const requestedWeek = calendarState.weekStart;
  const generation = ++calendarLoadGeneration;
  const to = calendarAddDays(requestedWeek, 6);
  const response = await fetch(`/api/calendar-days?from=${encodeURIComponent(requestedWeek)}&to=${encodeURIComponent(to)}`);
  if (!response.ok) throw new Error(`周历读取失败（${response.status}）`);
  const data = await response.json();
  if (generation !== calendarLoadGeneration || requestedWeek !== calendarState.weekStart) return;
  calendarState.todayDate = data.today_business_date || calendarState.todayDate;
  calendarState.earliestDate = data.earliest_available_date || '';
  calendarState.latestDate = data.latest_available_date || '';
  (data.days || []).forEach(day => calendarState.days.set(day.business_date, day));
  renderBusinessCalendar();
}
function changeBusinessCalendarWeek(amount) {
  calendarState.weekStart = calendarAddDays(calendarState.weekStart, amount);
  renderBusinessCalendar();
  loadBusinessCalendarWeek().catch(error => console.warn('周历读取失败', error));
}
async function selectBusinessDate(date) {
  if (!calendarDateIsValid(date) || date === calendarState.selectedDate) return;
  const selectedDay = calendarState.days.get(date);
  if (date !== calendarState.todayDate && !selectedDay?.available) return;
  const nextWeekStart = calendarWeekStart(date);
  const weekChanged = nextWeekStart !== calendarState.weekStart;
  calendarState.selectedDate = date;
  calendarState.weekStart = nextWeekStart;
  const url = new URL(window.location.href);
  if (date === calendarState.todayDate) url.searchParams.delete('date'); else url.searchParams.set('date', date);
  window.history.pushState({}, '', url);
  renderBusinessCalendar();
  if (weekChanged) await loadBusinessCalendarWeek();
  const page = document.querySelector('.page.active')?.id?.replace('page-', '');
  if (page === 'timeline-events' && typeof loadEventsTimeline === 'function') await loadEventsTimeline();
  if (page === 'app-usage' && typeof loadMultiDeviceUsage === 'function') await loadMultiDeviceUsage();
  if (page === 'location' && typeof loadLocationSummary === 'function') await loadLocationSummary();
  if (page === 'health-info' && typeof requestHealthInfoLoad === 'function') await requestHealthInfoLoad();
}
async function initializeBusinessCalendar() {
  const requested = calendarDateFromUrl();
  const fallback = typeof computeBizDate === 'function' ? computeBizDate().bizDate : healthDateToday();
  calendarState.selectedDate = requested || fallback;
  calendarState.todayDate = fallback;
  calendarState.weekStart = calendarWeekStart(calendarState.selectedDate);
  renderBusinessCalendar();
  document.getElementById('calendar-prev-week')?.addEventListener('click', event => {
    if (event.currentTarget.disabled) return;
    changeBusinessCalendarWeek(-7);
  });
  document.getElementById('calendar-next-week')?.addEventListener('click', event => {
    if (event.currentTarget.disabled) return;
    changeBusinessCalendarWeek(7);
  });
  document.getElementById('calendar-return-today')?.addEventListener('click', () => selectBusinessDate(calendarState.todayDate));
  try {
    await loadBusinessCalendarWeek();
    // The first client-side fallback can use the natural calendar date before
    // shared settings have loaded.  The central response is authoritative for
    // the current business date, so align an automatic selection with it.
    // An explicit ?date= remains the user's historical-view choice.
    if (!requested && calendarState.selectedDate !== calendarState.todayDate) {
      const previousWeek = calendarState.weekStart;
      calendarState.selectedDate = calendarState.todayDate;
      calendarState.weekStart = calendarWeekStart(calendarState.todayDate);
      renderBusinessCalendar();
      if (calendarState.weekStart !== previousWeek) await loadBusinessCalendarWeek();
    }
    if (requested && requested !== calendarState.todayDate && !calendarState.days.get(requested)?.available) await selectBusinessDate(calendarState.todayDate);
  } catch (error) {
    console.warn('周历读取失败', error);
    const container = document.getElementById('business-calendar-days');
    if (container) container.innerHTML = '<span class="calendar-loading">周历暂不可用</span>';
  }
}

function activatePage(page) {
  const nav = document.querySelector(`.nav-item[data-page="${page}"]`);
  const pageEl = document.getElementById('page-' + page);
  if (nav && pageEl) {
    document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    nav.classList.add('active');
    pageEl.classList.add('active');
  }
  try { localStorage.setItem(ACTIVE_PAGE_KEY, page); } catch (_) {}
  // 按需加载页面数据
  if (page === 'app-usage' && typeof loadMultiDeviceUsage === 'function') {
    loadMultiDeviceUsage().catch(console.warn);
  }
  if (page === 'devices' && typeof refreshSyncData === 'function') {
    refreshSyncData().catch(console.warn);
  }
  if (page === 'health-info' && typeof requestHealthInfoLoad === 'function') {
    requestHealthInfoLoad().catch(console.warn);
  }
  if (page === 'timeline-events' && typeof refreshEventsTimelineFromSharedCache === 'function') {
    if (isHistoricalDataView() && typeof loadEventsTimeline === 'function') loadEventsTimeline().catch(console.warn);
    else refreshEventsTimelineFromSharedCache().catch(console.warn);
  }
}

document.querySelectorAll('.nav-item').forEach(item => {
  item.addEventListener('click', () => activatePage(item.dataset.page));
});

// ============================================================
// Chart.js Initialization
// ============================================================
const chartColors = {
  blue:   '#4f8cff',
  green:  '#27ae60',
  red:    '#e74c3c',
  orange: '#f39c12',
  purple: '#9b59b6',
  gray:   '#95a5a6',
  teal:   '#1abc9c',
  pink:   '#e91e63',
  indigo: '#3f51b5',
};

// ============================================================
// 统一黑名单匹配
// App: 不区分大小写的子串匹配
// Domain: 等值或子域名匹配 (notbilibili.com 不命中 bilibili.com)
// ============================================================
function isBlacklistedApp(name) {
  const lowered = (name || '').toLowerCase();
  for (const p of blacklistRules.processes) {
    if (lowered.includes(p.name.toLowerCase())) return true;
  }
  return false;
}

function isBlacklistedDomain(hostname) {
  let cleaned = (hostname || '').toLowerCase();
  if (cleaned.startsWith('www.')) cleaned = cleaned.slice(4);
  if (cleaned.endsWith('.')) cleaned = cleaned.slice(0, -1);
  for (const d of blacklistRules.domains) {
    const pattern = (d.domain || '').toLowerCase();
    if (!pattern) continue;
    if (cleaned === pattern || cleaned.endsWith('.' + pattern)) return true;
  }
  return false;
}
