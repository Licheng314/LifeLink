(() => {
  const PAGE_SIZE = 30;
  const state = {
    currentOnly: false,
    cursor: null,
    photos: [],
    loading: false,
    loaded: false,
    exhausted: false,
    generation: 0,
    imageObserver: null,
    loadObserver: null,
  };

  const byId = id => document.getElementById(id);
  const pageIsActive = () => byId('page-photos')?.classList.contains('active');
  const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  })[char]);
  const formatDate = value => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value || '未知时间') : new Intl.DateTimeFormat('zh-CN', {
      month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(date);
  };
  const businessDateLabel = value => /^\d{4}-\d{2}-\d{2}$/.test(String(value || ''))
    ? `${value.slice(0, 4)} 年 ${Number(value.slice(5, 7))} 月 ${Number(value.slice(8, 10))} 日`
    : '未归档日期';
  const photoKey = photo => `${photo.source_device_id || ''}:${photo.photo_id || ''}`;

  function setStatus(kind, text) {
    const target = byId('photos-status');
    if (!target) return;
    target.className = `photos-status${kind ? ` ${kind}` : ''}`;
    target.textContent = text;
    target.hidden = !text;
  }

  function updateToggle() {
    const all = byId('photos-show-all');
    const current = byId('photos-show-current');
    if (all) { all.classList.toggle('active', !state.currentOnly); all.setAttribute('aria-pressed', String(!state.currentOnly)); }
    if (current) { current.classList.toggle('active', state.currentOnly); current.setAttribute('aria-pressed', String(state.currentOnly)); }
  }

  function photoContentUrl(photo) {
    const source = new URLSearchParams({source_device_id: String(photo.source_device_id || '')});
    return `/api/photos/${encodeURIComponent(photo.photo_id)}/content?${source}`;
  }

  function setupImageLazyLoad() {
    state.imageObserver?.disconnect();
    const load = image => {
      if (!image.dataset.src) return;
      image.src = image.dataset.src;
      delete image.dataset.src;
    };
    const images = document.querySelectorAll('#photos-groups img[data-src]');
    if (!('IntersectionObserver' in window)) { images.forEach(load); return; }
    state.imageObserver = new IntersectionObserver(entries => entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      load(entry.target);
      state.imageObserver.unobserve(entry.target);
    }), {rootMargin: '320px 0px'});
    images.forEach(image => state.imageObserver.observe(image));
  }

  function renderPhotos() {
    const container = byId('photos-groups');
    const more = byId('photos-load-more-wrap');
    if (!container || !more) return;
    if (!state.photos.length) {
      container.innerHTML = `<section class="photos-empty"><i data-lucide="image-off"></i><strong>${state.loaded ? '暂无照片中央副本' : '正在读取照片…'}</strong><span>${state.loaded ? (state.currentOnly ? '当前业务日还没有已同步的照片。' : '在手机上选择照片并确认同步后，会显示在这里。') : ''}</span></section>`;
      more.hidden = true;
      if (typeof lucide !== 'undefined') lucide.createIcons();
      return;
    }
    const groups = new Map();
    for (const photo of state.photos) {
      const key = photo.business_date || '';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(photo);
    }
    container.replaceChildren(...Array.from(groups, ([businessDate, photos]) => {
      const section = document.createElement('section');
      section.className = 'photo-group';
      const heading = document.createElement('h3');
      heading.className = 'photo-group-heading';
      heading.textContent = businessDateLabel(businessDate);
      const count = document.createElement('span');
      count.textContent = `${photos.length} 张`;
      heading.appendChild(count);
      section.appendChild(heading);
      const grid = document.createElement('div');
      grid.className = 'photo-grid';
      photos.forEach(photo => grid.appendChild(makePhotoCard(photo)));
      section.appendChild(grid);
      return section;
    }));
    more.hidden = !state.cursor;
    setupImageLazyLoad();
    if (typeof lucide !== 'undefined') lucide.createIcons();
  }

  function makePhotoCard(photo) {
    const card = document.createElement('article');
    card.className = `photo-card${photo.status === 'deleted' ? ' deleted' : ''}`;
    card.dataset.photoKey = photoKey(photo);
    const preview = document.createElement('div');
    preview.className = 'photo-preview';
    if (photo.status === 'active') {
      const image = document.createElement('img');
      image.alt = `来自 ${photo.source_device_id || '未知设备'} 的照片`;
      image.loading = 'lazy';
      image.dataset.src = photoContentUrl(photo);
      image.addEventListener('error', () => {
        image.classList.add('failed');
        image.alt = '照片暂不可读取';
      }, {once: true});
      preview.appendChild(image);
    } else {
      preview.innerHTML = '<i data-lucide="trash-2"></i><span>中央副本已删除</span>';
    }
    const body = document.createElement('div');
    body.className = 'photo-card-body';
    const meta = document.createElement('div');
    meta.className = 'photo-card-meta';
    meta.innerHTML = `<span title="拍摄时间">${escapeHtml(formatDate(photo.captured_at))}</span><span class="photo-source" title="来源设备">${escapeHtml(photo.source_device_id || '未知设备')}</span>`;
    body.appendChild(meta);
    if (photo.time_source === 'added') {
      const fallback = document.createElement('span');
      fallback.className = 'photo-time-fallback';
      fallback.textContent = '使用加入相册时间';
      body.appendChild(fallback);
    }
    if (photo.status === 'active') {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'photo-delete';
      remove.textContent = '删除中央副本';
      remove.addEventListener('click', () => deletePhoto(photo, remove));
      body.appendChild(remove);
    }
    card.append(preview, body);
    return card;
  }

  async function responseJson(response) {
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.message || body.error || `HTTP ${response.status}`);
    return body;
  }

  async function loadPhotos({reset = false} = {}) {
    if (state.loading || (!reset && !state.cursor && state.loaded)) return;
    const generation = reset ? ++state.generation : state.generation;
    if (reset) {
      state.cursor = null; state.photos = []; state.loaded = false; state.exhausted = false;
      setStatus('', '正在读取照片…'); renderPhotos();
    }
    state.loading = true;
    byId('photos-load-more')?.setAttribute('disabled', '');
    try {
      const params = new URLSearchParams({limit: String(PAGE_SIZE)});
      if (state.cursor) params.set('cursor', state.cursor);
      if (state.currentOnly) params.set('current_business_date_only', 'true');
      const payload = await responseJson(await fetch(`/api/photos?${params}`, {cache: 'no-store'}));
      if (generation !== state.generation) return;
      const incoming = Array.isArray(payload.photos) ? payload.photos : [];
      const existing = new Set(state.photos.map(photoKey));
      state.photos.push(...incoming.filter(photo => photo && !existing.has(photoKey(photo))));
      state.cursor = typeof payload.next_cursor === 'string' && payload.next_cursor ? payload.next_cursor : null;
      state.loaded = true; state.exhausted = !state.cursor;
      setStatus('', state.photos.length ? '' : '');
      renderPhotos();
    } catch (error) {
      if (generation === state.generation) {
        state.loaded = true;
        setStatus('error', `照片读取失败：${error.message}`);
        renderPhotos();
      }
    } finally {
      if (generation === state.generation) {
        state.loading = false;
        byId('photos-load-more')?.removeAttribute('disabled');
      }
    }
  }

  async function deletePhoto(photo, button) {
    const device = photo.source_device_id || '该设备';
    if (!window.confirm(`删除这张照片的中央副本吗？\n\n来源：${device}\n手机相册原图不会被删除。`)) return;
    button.disabled = true; button.textContent = '正在删除…';
    try {
      const source = new URLSearchParams({source_device_id: String(photo.source_device_id || '')});
      await responseJson(await fetch(`/api/photos/${encodeURIComponent(photo.photo_id)}?${source}`, {method: 'DELETE'}));
      state.photos = state.photos.filter(item => photoKey(item) !== photoKey(photo));
      setStatus('success', '中央副本已删除；手机相册原图未受影响。');
      renderPhotos();
    } catch (error) {
      button.disabled = false; button.textContent = '删除中央副本';
      setStatus('error', `删除失败：${error.message}`);
    }
  }

  function changeScope(currentOnly) {
    if (state.currentOnly === currentOnly && state.loaded) return;
    state.currentOnly = currentOnly;
    updateToggle();
    loadPhotos({reset: true});
  }

  function initialize() {
    byId('photos-show-all')?.addEventListener('click', () => changeScope(false));
    byId('photos-show-current')?.addEventListener('click', () => changeScope(true));
    byId('photos-load-more')?.addEventListener('click', () => loadPhotos());
    if ('IntersectionObserver' in window) {
      state.loadObserver = new IntersectionObserver(entries => {
        if (entries.some(entry => entry.isIntersecting) && pageIsActive() && state.cursor && !state.loading) loadPhotos();
      }, {rootMargin: '480px 0px'});
      const sentinel = byId('photos-load-sentinel'); if (sentinel) state.loadObserver.observe(sentinel);
    }
  }

  window.loadPhotosPage = () => loadPhotos({reset: !state.loaded});
  initialize();
})();
