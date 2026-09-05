// Lucide icons: render after DOM ready
if (typeof lucide !== 'undefined') lucide.createIcons();

// Auto-render Lucide icons in dynamically inserted content.
// MutationObserver keeps this idempotent — icons already rendered are skipped.
if (typeof lucide !== 'undefined' && typeof MutationObserver !== 'undefined') {
  const iconObserver = new MutationObserver(mutations => {
    let needsRender = false;
    for (const mutation of mutations) {
      if (mutation.type !== 'childList') continue;
      for (const node of mutation.addedNodes) {
        if (!(node instanceof Element)) continue;
        if (node.matches?.('i[data-lucide]') || node.querySelector?.('i[data-lucide]')) {
          needsRender = true;
          break;
        }
      }
      if (needsRender) break;
    }
    if (needsRender) lucide.createIcons();
  });
  iconObserver.observe(document.body, { childList: true, subtree: true });
}

// Restore last active page after setup so refresh keeps the user's current tab.
async function restoreActivePage() {
  if (typeof initializeBusinessCalendar === 'function') await initializeBusinessCalendar();
  let saved = null;
  try { saved = localStorage.getItem(ACTIVE_PAGE_KEY); } catch (_) {}
  const savedNav = saved ? document.querySelector(`.nav-item[data-page="${saved}"]`) : null;
  const target = savedNav ? saved : 'timeline-events';
  if (!savedNav && saved) {
    try { localStorage.setItem(ACTIVE_PAGE_KEY, target); } catch (_) {}
  }
  // activatePage 统一处理样式切换 + 按需数据加载，避免刷新恢复的页面不触发加载钩子
  if (typeof activatePage === 'function') activatePage(target);
  if (target === 'timeline-events' && typeof loadEventsTimeline === 'function') loadEventsTimeline().catch(console.warn);
}
restoreActivePage().catch(console.warn);
// Device roster first paint; no recurring timer — pages refresh on demand.
refreshSyncData();
