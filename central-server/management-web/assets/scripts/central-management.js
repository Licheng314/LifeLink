(() => {
  const token = document.querySelector('meta[name="lifelink-csrf"]')?.content || '';
  const headers = {'Content-Type': 'application/json', 'X-CSRF-Token': token};
  const byId = id => document.getElementById(id);
  const feedback = (id, text, kind = '') => {
    const target = byId(id); if (!target) return;
    target.textContent = text; target.dataset.kind = kind;
  };
  const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  })[char]);
  async function post(path, payload = {}) {
    const response = await fetch(path, {method: 'POST', headers, body: JSON.stringify(payload)});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.message || body.error || `HTTP ${response.status}`);
    return body;
  }
  async function loadStatus() {
    const response = await fetch('/api/status', {cache: 'no-store'});
    const state = await response.json();
    if (!response.ok) throw new Error(state.message || state.error || '中央状态不可用');
    const endpoint = state.public_endpoint;
    byId('central-network-summary').textContent = endpoint
      ? `当前已验证地址：${endpoint.base_url}`
      : `中央数据服务：${state.data_api.host}:${state.data_api.port}；尚未配置远程连接`;
    if (endpoint?.base_url) byId('central-base-url').value = endpoint.base_url;
    if (endpoint?.provider) byId('central-provider').value = endpoint.provider;
  }
  byId('central-verify-network')?.addEventListener('click', async () => {
    const input = byId('central-base-url'); const baseUrl = input.value.trim();
    input.setCustomValidity('');
    if (!baseUrl || !baseUrl.toLowerCase().startsWith('https://')) {
      input.setCustomValidity('请填写以 https:// 开头的外部地址。'); input.reportValidity();
      return;
    }
    feedback('central-network-feedback', '正在验证 HTTPS、服务身份和中央实例…');
    try {
      const result = await post('/api/network/verify', {provider: byId('central-provider').value, base_url: baseUrl});
      feedback('central-network-feedback', `已验证并保存：${result.public_endpoint.base_url}`, 'success'); await loadStatus();
    } catch (error) { feedback('central-network-feedback', error.message, 'error'); }
  });
  byId('central-detect-tailscale')?.addEventListener('click', async () => {
    feedback('central-network-feedback', '正在检测 Tailscale…');
    try {
      const result = await post('/api/network/tailscale/detect');
      byId('central-provider').value = 'tailscale'; byId('central-base-url').value = result.base_url;
      feedback('central-network-feedback', result.message, 'success');
    } catch (error) { feedback('central-network-feedback', error.message, 'error'); }
  });
  byId('central-create-invitation')?.addEventListener('click', async () => {
    const output = byId('central-invitation'); output.textContent = '正在生成设备配对码…';
    try {
      const result = await post('/api/device-invitations');
      output.textContent = `${result.code}\n有效至：${result.expires_at}`;
      await navigator.clipboard?.writeText(result.code).catch(() => {});
    } catch (error) { output.textContent = error.message; }
  });
  async function downloadAIPackage(options = {}) {
    const statusTarget = options.statusTarget || 'central-package-feedback';
    feedback(statusTarget, '正在生成并下载 AI 配对包…');
    try {
      const response = await fetch('/api/ai-connection-package', {method: 'POST', headers, body: '{}'});
      if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(error.message || error.error || `HTTP ${response.status}`); }
      const blob = await response.blob(); const link = document.createElement('a');
      link.href = URL.createObjectURL(blob); link.download = 'LifeLink-AI-MCP.zip'; link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
      feedback(statusTarget, 'AI 配对包已开始下载。', 'success');
      if (statusTarget !== 'central-package-feedback' && typeof showToast === 'function') showToast('AI 配对包已开始下载。', 'ok');
      await showMcpConfig();
    } catch (error) {
      feedback(statusTarget, error.message, 'error');
      if (statusTarget !== 'central-package-feedback' && typeof showToast === 'function') showToast('生成 AI 配对包失败：' + error.message, 'err');
    }
  }
  async function showAISkill() {
    try {
      const response = await fetch('/api/ai-reader-skill', {cache: 'no-store'});
      const body = await response.json().catch(() => ({}));
      if (!response.ok || typeof body.skill !== 'string') throw new Error(body.message || body.error || `HTTP ${response.status}`);
      if (typeof showReportBody === 'function') showReportBody(body.skill, 'Life Link AI Reader Skill');
      else if (typeof showToast === 'function') showToast('Skill 已读取，请从 AI 配对包中提供给 AI。', 'ok');
    } catch (error) {
      if (typeof showToast === 'function') showToast('读取 Skill 失败：' + error.message, 'err');
    }
  }
  function showMcpConfigModal(config) {
    const text = JSON.stringify(config, null, 2);
    const highlighted = escapeHtml(text)
      .replaceAll('&lt;PYTHON_COMMAND&gt;', '<mark class="mcp-placeholder">&lt;PYTHON_COMMAND&gt;</mark>')
      .replaceAll('&lt;LIFE_LINK_MCP_DIR&gt;', '<mark class="mcp-placeholder">&lt;LIFE_LINK_MCP_DIR&gt;</mark>');
    const overlay = document.createElement('div');
    overlay.className = 'wish-form-overlay';
    overlay.id = 'mcp-config-modal';
    overlay.innerHTML = '<div class="wish-form-box report-body-box mcp-config-box" role="dialog" aria-modal="true" aria-labelledby="mcp-config-title">'
      + '<h3 id="mcp-config-title">Life Link MCP JSON <span class="event-settings-close" role="button" tabindex="0" aria-label="关闭">✕</span></h3>'
      + '<p class="mcp-config-hint">以下文本可让 AI 根据配对包生成并填写并使用，或按照以下规则自行填写。<br><code class="mcp-placeholder">&lt;PYTHON_COMMAND&gt;</code>：需替换为 AI 所在环境的 Python 指令，Windows 通常用 <code>python</code>，Linux 通常用 <code>python3</code>。<br><code class="mcp-placeholder">&lt;LIFE_LINK_MCP_DIR&gt;</code>：需替换为 AI 主机中 MCP 脚本安装的绝对路径。</p>'
      + '<pre class="report-body-pre mcp-config-pre">' + highlighted + '</pre>'
      + '<div class="mcp-config-actions"><button type="button" class="central-action primary" id="mcp-config-copy">复制 JSON</button></div></div>';
    const close = () => overlay.remove();
    overlay.addEventListener('click', event => { if (event.target === overlay) close(); });
    overlay.querySelector('.event-settings-close')?.addEventListener('click', close);
    overlay.querySelector('.event-settings-close')?.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') close(); });
    overlay.querySelector('#mcp-config-copy')?.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(text);
        if (typeof showToast === 'function') showToast('MCP JSON 已复制；请先替换红色占位文本。', 'ok');
      } catch (_) {
        if (typeof showToast === 'function') showToast('复制失败，请手动复制 JSON。', 'err');
      }
    });
    document.body.appendChild(overlay);
    overlay.querySelector('.event-settings-close')?.focus();
  }
  async function showMcpConfig() {
    try {
      const response = await fetch('/api/ai-connection-mcp-config', {cache: 'no-store'});
      const body = await response.json().catch(() => ({}));
      if (!response.ok || !body.mcp_config) throw new Error(body.message || body.error || `HTTP ${response.status}`);
      showMcpConfigModal(body.mcp_config);
    } catch (error) {
      if (typeof showToast === 'function') showToast('读取 MCP JSON 失败：' + error.message, 'err');
    }
  }
  window.LifeLinkCentralManagement = {
    downloadAIPackage,
    showAISkill,
    showMcpConfig,
  };
  loadStatus().catch(error => feedback('central-network-summary', error.message, 'error'));
})();
