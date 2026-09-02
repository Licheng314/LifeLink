// ============================================================
// Tools: Bilibili audio extraction
// ============================================================
(function () {
  const urlInput = document.getElementById('bili-url');
  const extractBtn = document.getElementById('bili-extract');
  const folderBtn = document.getElementById('bili-open-folder');
  const statusEl = document.getElementById('bili-status');
  const jobsEl = document.getElementById('bili-jobs');
  const filesBody = document.getElementById('bili-files');
  if (!urlInput || !extractBtn) return;

  let pollTimer = null;
  let lastActiveJobId = null;

  function setStatus(msg, isError) {
    statusEl.textContent = msg || '';
    statusEl.style.color = isError ? 'var(--danger)' : 'var(--text-secondary)';
  }
  function formatBytes(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return n + ' B';
    const units = ['KB', 'MB', 'GB'];
    let v = n / 1024, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(v >= 10 ? 0 : 1) + ' ' + units[i];
  }
  function formatTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString('zh-CN');
  }
  async function fetchJson(url, options) {
    const resp = await fetch(url, options);
    let data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    if (!resp.ok) {
      const msg = (data && data.message) || ('HTTP ' + resp.status);
      throw new Error(msg);
    }
    return data || {};
  }
  async function loadItems() {
    try {
      const data = await fetchJson('/api/media/items');
      const items = data.items || [];
      if (!items.length) {
        filesBody.innerHTML = '<tr><td colspan="3" style="color:var(--text-secondary)">还没有音乐文件</td></tr>';
        return;
      }
      filesBody.innerHTML = items.map(it =>
        `<tr><td>${escapeHtml(it.name)}</td><td>${formatBytes(it.size)}</td><td>${formatTime(it.modified_at)}</td></tr>`
      ).join('');
    } catch (e) {
      filesBody.innerHTML = `<tr><td colspan="3" style="color:var(--danger)">${escapeHtml(e.message)}</td></tr>`;
    }
  }
  const STATUS_LABEL = { queued: '排队中', processing: '处理中', completed: '完成', failed: '失败' };
  async function loadJobs() {
    try {
      const data = await fetchJson('/api/media/jobs');
      const jobs = data.jobs || [];
      if (!jobs.length) { jobsEl.innerHTML = ''; return; }
      jobsEl.innerHTML = jobs.slice(0, 8).map(j => {
        const st = j.status || '';
        const detail = j.file ? j.file.name : (j.message || '');
        const host = String(j.url || '').replace(/^https?:\/\//, '');
        return `<div class="job"><span title="${escapeHtml(j.url || '')}">${escapeHtml(host)}${detail ? ' · ' + escapeHtml(detail) : ''}</span><span class="job-status ${escapeHtml(st)}">${STATUS_LABEL[st] || st}</span></div>`;
      }).join('');
      const active = jobs.find(j => j.status === 'queued' || j.status === 'processing');
      if (active) {
        extractBtn.disabled = true;
        setStatus('正在处理，请稍候…');
        lastActiveJobId = active.id;
      } else {
        extractBtn.disabled = false;
        if (lastActiveJobId) {
          const done = jobs.find(j => j.id === lastActiveJobId);
          if (done) {
            setStatus(done.status === 'completed' ? ('完成：' + (done.file ? done.file.name : '')) : ('任务失败：' + (done.message || '')), done.status === 'failed');
            loadItems();
          }
          lastActiveJobId = null;
        }
      }
    } catch (e) {
      jobsEl.innerHTML = '';
    }
  }
  function startPolling() {
    loadItems();
    loadJobs();
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(loadJobs, 3000);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }
  document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', () => {
      if (item.dataset.page === 'tools') startPolling();
      else stopPolling();
    });
  });
  extractBtn.addEventListener('click', async () => {
    const url = urlInput.value.trim();
    if (!url) { setStatus('请先粘贴 B 站视频链接。', true); return; }
    extractBtn.disabled = true;
    setStatus('已提交，等待中央服务处理…');
    try {
      await fetchJson('/api/media/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url })
      });
      urlInput.value = '';
      lastActiveJobId = null;
      loadJobs();
    } catch (e) {
      setStatus(e.message, true);
      extractBtn.disabled = false;
    }
  });
  folderBtn.addEventListener('click', async () => {
    folderBtn.disabled = true;
    try {
      await fetchJson('/api/media/open-folder', { method: 'POST' });
      setStatus('已在服务器上打开音乐文件夹。');
    } catch (e) {
      setStatus(e.message, true);
    } finally {
      folderBtn.disabled = false;
    }
  });
})();
