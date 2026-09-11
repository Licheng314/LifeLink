(() => {
  const palette = ['#2563EB', '#7C3AED', '#DB2777', '#EA580C', '#16A34A', '#0891B2'];
  const token = document.querySelector('meta[name="lifelink-csrf"]')?.content || '';
  let snapshot = {intervals: [], state: {}};
  let selectedId = null;
  let editorOpen = false;
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  const minutes = text => { const [h, m] = text.split(':').map(Number); return h * 60 + m; };
  const clock = value => `${String(Math.floor(value / 60) % 24).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`;
  const translucent = color => `${color}2E`;
  const segments = (start, end) => start === end ? [] : start < end ? [[start, end]] : [[start, 1440], [0, end]];
  const overlaps = (left, right) => left[0] < right[1] && right[0] < left[1];
  const isFree = (start, end) => {
    const candidate = segments(start, end);
    return snapshot.intervals.every(item => candidate.every(part => segments(minutes(item.start_local_time), minutes(item.end_local_time)).every(otherPart => !overlaps(part, otherPart))));
  };
  const circularDistance = (left, right) => Math.min(Math.abs(left - right), 1440 - Math.abs(left - right));
  const nextUnusedColor = () => palette.find(color => !snapshot.intervals.some(item => item.color === color)) || palette.reduce((best, color) => {
    const count = snapshot.intervals.filter(item => item.color === color).length;
    const bestCount = snapshot.intervals.filter(item => item.color === best).length;
    return count < bestCount ? color : best;
  }, palette[0]);
  const intervalName = item => `${item.tag === 'alert' ? '⚠ ' : ''}${item.name}`;
  const intervalSpan = item => {
    const total = (minutes(item.end_local_time) - minutes(item.start_local_time) + 1440) % 1440;
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
  };
  function findNewInterval() {
    const now = new Date(); const currentMinute = now.getHours() * 60 + now.getMinutes();
    const nearestHour = Math.round(currentMinute / 60) * 60 % 1440;
    if (isFree(nearestHour, (nearestHour + 60) % 1440)) return nearestHour;
    return Array.from({length:144}, (_, index) => index * 10)
      .sort((left, right) => circularDistance(left, currentMinute) - circularDistance(right, currentMinute))
      .find(start => isFree(start, (start + 60) % 1440));
  }
  function canAdjust(item, edge, time) {
    const start = edge === 'start' ? minutes(time) : minutes(item.start_local_time);
    const end = edge === 'end' ? minutes(time) : minutes(item.end_local_time);
    return canPlace(item, start, end);
  }
  function canPlace(item, start, end) {
    const candidate = segments(start, end);
    return snapshot.intervals.every(other => other.interval_id === item.interval_id || candidate.every(part => segments(minutes(other.start_local_time), minutes(other.end_local_time)).every(otherPart => !overlaps(part, otherPart))));
  }
  const post = async (path, method, body) => {
    const response = await fetch(path, {method, headers: {'Content-Type':'application/json', 'X-CSRF-Token':token}, body: body ? JSON.stringify(body) : undefined});
    const json = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(json.message || json.error || `HTTP ${response.status}`);
    return json;
  };
  function resetEditor() {
    selectedId = null; editorOpen = false;
    const actions = $('time-interval-actions');
    if (actions) {
      actions.innerHTML = '<button type="button" id="time-interval-create" class="wish-history-open">新增区间</button>';
      actions.querySelector('#time-interval-create').addEventListener('click', event => { event.stopPropagation(); openEditor(); });
    }
    render();
  }
  const duration = seconds => {
    const total = Math.max(0, Math.ceil(seconds)); const hours = Math.floor(total / 3600); const mins = Math.floor(total % 3600 / 60); const secs = total % 60;
    return hours ? `${hours}时${mins}分${secs}秒` : mins ? `${mins}分${secs}秒` : `${secs}秒`;
  };
  function renderCurrent() {
    const current = $('time-interval-current'); if (!current) return;
    const business = typeof computeEventDisplayBizDate === 'function' ? computeEventDisplayBizDate() : null;
    const window = business && typeof bizDateToUTCWindow === 'function' ? bizDateToUTCWindow(business.bizDate, business.dayStartHour) : null;
    const nowMs = Date.now();
    if (!window || nowMs < new Date(window.from).getTime() || nowMs > new Date(window.to).getTime()) {
      current.hidden = true; current.textContent = ''; return;
    }
    current.hidden = false;
    const now = new Date(); const nowSeconds = now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds();
    const intervals = snapshot.intervals.filter(item => minutes(item.start_local_time) !== minutes(item.end_local_time));
    const active = intervals.find(item => {
      const start = minutes(item.start_local_time) * 60; const end = minutes(item.end_local_time) * 60;
      return start < end ? nowSeconds >= start && nowSeconds < end : nowSeconds >= start || nowSeconds < end;
    });
    if (active) {
      const start = minutes(active.start_local_time) * 60; const end = minutes(active.end_local_time) * 60;
      const total = (end - start + 86400) % 86400; const elapsed = (nowSeconds - start + 86400) % 86400; const remaining = Math.max(0, total - elapsed);
      const percent = Math.min(100, remaining / total * 100);
      current.innerHTML = `${esc(intervalName(active))}，剩余 <span class="time-interval-countdown active" style="color:${active.color}">${duration(remaining)}</span><span class="time-interval-progress" aria-label="区间剩余 ${Math.round(percent)}%"><span style="width:${percent}%;background:${active.color}"></span></span>`;
      return;
    }
    const next = intervals.map(item => ({item, seconds:(minutes(item.start_local_time) * 60 - nowSeconds + 86400) % 86400})).sort((left, right) => left.seconds - right.seconds)[0];
    current.innerHTML = next ? `距 ${esc(intervalName(next.item))} <span class="time-interval-countdown waiting">${duration(next.seconds)}</span>` : '尚未创建时间区间。';
  }
  function render() {
    const axis = $('bizday-time-intervals'); const current = $('time-interval-current');
    if (!axis || !current) return;
    axis.replaceChildren();
    axis.parentElement?.querySelectorAll('.time-interval-timeline-label').forEach(label => label.remove());
    renderCurrent();
    const dayStart = (typeof computeEventDisplayBizDate === 'function' ? Number(computeEventDisplayBizDate().dayStartHour || 0) : 0) * 60;
    const businessMinute = value => (value - dayStart + 1440) % 1440;
    for (const item of snapshot.intervals) {
      const start = businessMinute(minutes(item.start_local_time)), end = businessMinute(minutes(item.end_local_time));
      const open = () => openEditor(item);
      const addBar = (left, width, label, canAdjustStart, canAdjustEnd, segment) => {
        const bar = document.createElement('button'); bar.type = 'button'; bar.className = 'time-interval-bar'; bar.dataset.intervalId = item.interval_id;
        if (segment === 'cross-start') bar.classList.add('crosses-right');
        if (segment === 'cross-end') bar.classList.add('crosses-left');
        bar.style.left = `${left / 14.4}%`; bar.style.width = `${width / 14.4}%`; bar.style.background = translucent(item.color); bar.style.borderColor = item.color;
        if (selectedId === item.interval_id) bar.classList.add('selected');
        const barLabel = document.createElement('span'); barLabel.className = 'time-interval-timeline-label'; barLabel.textContent = `${item.tag === 'alert' ? '⚠ ' : ''}${label}\n${intervalSpan(item)}`; barLabel.style.left = `${left / 14.4}%`; barLabel.style.color = item.color; axis.parentElement?.appendChild(barLabel);
        bar.title = `${item.name} · ${item.start_local_time}–${item.end_local_time}；拖动左右边缘可调整（10 分钟刻度）`;
        let dragged = false;
        bar.addEventListener('pointermove', event => {
          if (bar.classList.contains('dragging-start') || bar.classList.contains('dragging-end')) return;
          const rect = bar.getBoundingClientRect();
          const edge = segment === 'anchor'
            ? (event.clientX <= (rect.left + rect.right) / 2 ? 'start' : 'end')
            : event.clientX - rect.left < 9 && canAdjustStart ? 'start' : rect.right - event.clientX < 9 && canAdjustEnd ? 'end' : null;
          bar.classList.toggle('edge-start', edge === 'start'); bar.classList.toggle('edge-end', edge === 'end');
        });
        bar.addEventListener('pointerleave', () => { bar.classList.remove('edge-start', 'edge-end'); });
        const moveWholeInterval = event => {
          event.preventDefault(); event.stopPropagation(); bar.setPointerCapture(event.pointerId);
          selectedId = item.interval_id; editorOpen = true; bar.classList.add('selected'); showEditor(item, true, false);
          const axisRect = axis.getBoundingClientRect();
          const initialPointer = (event.clientX - axisRect.left) / axisRect.width * 1440;
          const intervalDuration = (end - start + 1440) % 1440;
          const sourceBars = Array.from(axis.querySelectorAll(`[data-interval-id="${item.interval_id}"]`));
          let moved = false; let preview = null; let timeLabel = null;
          const drawPreview = (previewStart, previewEnd) => {
            if (!preview) { preview = document.createElement('div'); preview.className = 'time-interval-move-preview'; axis.appendChild(preview); }
            preview.replaceChildren();
            const addPiece = (pieceStart, pieceWidth, squareLeft = false, squareRight = false) => {
              const piece = document.createElement('div'); piece.className = 'time-interval-move-preview-piece';
              if (squareLeft) piece.classList.add('crosses-left'); if (squareRight) piece.classList.add('crosses-right');
              piece.style.left = `${pieceStart / 14.4}%`; piece.style.width = `${pieceWidth / 14.4}%`; piece.style.background = translucent(item.color); piece.style.borderColor = item.color; preview.appendChild(piece);
            };
            if (previewStart < previewEnd) addPiece(previewStart, previewEnd - previewStart);
            else { addPiece(previewStart, 1440 - previewStart, false, true); addPiece(0, previewEnd, true, false); }
          };
          const update = move => {
            const pointer = (move.clientX - axisRect.left) / axisRect.width * 1440;
            if (!moved && Math.abs(pointer - initialPointer) < 4) return;
            if (!moved) sourceBars.forEach(source => source.classList.add('moving-source'));
            moved = true; dragged = true; bar.classList.add('dragging-move');
            const targetStart = ((Math.round((start + pointer - initialPointer) / 10) * 10) % 1440 + 1440) % 1440;
            const targetEnd = (targetStart + intervalDuration) % 1440;
            const boundary = typeof computeEventDisplayBizDate === 'function' ? Number(computeEventDisplayBizDate().dayStartHour || 0) * 60 : 0;
            const nextStart = clock((targetStart + boundary) % 1440); const nextEnd = clock((targetEnd + boundary) % 1440);
            if (!timeLabel) { timeLabel = document.createElement('div'); timeLabel.className = 'time-interval-drag-time'; axis.appendChild(timeLabel); }
            timeLabel.style.left = `${targetStart / 14.4}%`;
            if (!canPlace(item, minutes(nextStart), minutes(nextEnd))) { timeLabel.textContent = `${nextStart}–${nextEnd}（与其他区间重叠）`; return; }
            timeLabel.textContent = `${nextStart}–${nextEnd}`;
            bar.dataset.pendingStart = nextStart; bar.dataset.pendingEnd = nextEnd;
            const form = $('time-interval-actions');
            const startInput = form?.querySelector('input[name="start"]'); const endInput = form?.querySelector('input[name="end"]');
            if (startInput) startInput.value = nextStart; if (endInput) endInput.value = nextEnd;
            drawPreview(targetStart, targetEnd);
          };
          const finish = async () => {
            bar.removeEventListener('pointermove', update); bar.removeEventListener('pointerup', finish); bar.classList.remove('dragging-move'); sourceBars.forEach(source => source.classList.remove('moving-source')); preview?.remove(); timeLabel?.remove();
            const nextStart = bar.dataset.pendingStart; const nextEnd = bar.dataset.pendingEnd; delete bar.dataset.pendingStart; delete bar.dataset.pendingEnd;
            if (!moved || !nextStart || !nextEnd) return;
            try { await post(`/api/time-intervals/${item.interval_id}`, 'PATCH', {start_local_time: nextStart, end_local_time: nextEnd}); await load(); }
            catch (error) { if (!String(error.message).includes('must not overlap')) alert(`无法移动时间区间：${error.message}`); await load().catch(() => {}); }
          };
          bar.addEventListener('pointermove', update); bar.addEventListener('pointerup', finish);
        };
        bar.addEventListener('pointerdown', event => {
          const rect = bar.getBoundingClientRect();
          const edge = segment === 'anchor'
            ? (event.clientX <= (rect.left + rect.right) / 2 ? 'start' : 'end')
            : event.clientX - rect.left < 9 && canAdjustStart ? 'start' : rect.right - event.clientX < 9 && canAdjustEnd ? 'end' : null;
          if (!edge) { moveWholeInterval(event); return; }
          event.preventDefault(); event.stopPropagation(); dragged = true; bar.setPointerCapture(event.pointerId);
          selectedId = item.interval_id; editorOpen = true; bar.classList.add('selected'); showEditor(item, true, false);
          bar.classList.add(`dragging-${edge}`);
          const timeLabel = document.createElement('div'); timeLabel.className = 'time-interval-drag-time'; axis.appendChild(timeLabel);
          const update = move => {
            const axisRect = axis.getBoundingClientRect();
            const raw = Math.max(0, Math.min(1439, (move.clientX - axisRect.left) / axisRect.width * 1440));
            const snapped = Math.max(0, Math.min(1430, Math.round(raw / 10) * 10));
            const boundary = typeof computeEventDisplayBizDate === 'function' ? Number(computeEventDisplayBizDate().dayStartHour || 0) * 60 : 0;
            const pendingTime = clock((snapped + boundary) % 1440);
            timeLabel.style.left = `${snapped / 14.4}%`;
            if (!canAdjust(item, edge, pendingTime)) { timeLabel.textContent = `${pendingTime}（与其他区间重叠）`; return; }
            timeLabel.textContent = pendingTime;
            bar.dataset.pendingTime = pendingTime;
            const editorTime = $('time-interval-actions')?.querySelector(`input[name="${edge}"]`);
            if (editorTime) editorTime.value = pendingTime;
            if (edge === 'start') {
              bar.style.left = `${snapped / 14.4}%`;
              barLabel.style.left = `${snapped / 14.4}%`;
              const previewWidth = segment === 'cross-start' ? 1440 - snapped : Math.max(0, end - snapped);
              bar.style.width = `${previewWidth / 14.4}%`;
            } else {
              const previewWidth = segment === 'cross-end' ? snapped : Math.max(0, snapped - start);
              bar.style.width = `${previewWidth / 14.4}%`;
            }
          };
          const finish = async move => {
            bar.removeEventListener('pointermove', update); bar.removeEventListener('pointerup', finish); bar.classList.remove(`dragging-${edge}`); timeLabel.remove();
            const next = bar.dataset.pendingTime; delete bar.dataset.pendingTime;
            if (!next) return;
            try { await post(`/api/time-intervals/${item.interval_id}`, 'PATCH', edge === 'start' ? {start_local_time: next} : {end_local_time: next}); await load(); }
            catch (error) {
              // A concurrent edit can still make a formerly valid drag collide. Reload quietly;
              // overlap is constrained during dragging and is not a user-facing error state.
              if (!String(error.message).includes('must not overlap')) alert(`无法调整时间区间：${error.message}`);
              await load().catch(() => {});
            }
          };
          bar.addEventListener('pointermove', update); bar.addEventListener('pointerup', finish);
        });
        bar.addEventListener('click', event => { event.stopPropagation(); if (!dragged) open(); dragged = false; }); axis.appendChild(bar);
      };
      if (item.is_time_anchor) addBar(start, 0, item.name, true, true, 'anchor');
      else if (start < end) addBar(start, end - start, item.name, true, true, 'regular');
      else { addBar(start, 1440 - start, item.name, true, false, 'cross-start'); addBar(0, end, item.name, false, true, 'cross-end'); }
    }
  }
  async function load() {
    const response = await fetch('/api/time-intervals', {cache:'no-store'});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || data.error || '时间区间读取失败');
    snapshot = data; render();
  }
  function showEditor(item, existing, focus = true) {
    const actions = $('time-interval-actions'); if (!actions) return;
    const colorChoices = palette.map((color, index) => `<input id="time-interval-color-${index}" class="time-interval-color" type="radio" name="color" value="${color}"${color === item.color ? ' checked' : ''}><label for="time-interval-color-${index}" title="${color}" style="background:${color}"></label>`).join('');
    actions.innerHTML = `<form class="time-interval-editor"><span class="time-interval-colors" role="radiogroup" aria-label="区间颜色">${colorChoices}</span><input name="name" type="text" maxlength="50" value="${esc(item.name)}" aria-label="区间名称" required><input name="start" type="time" step="600" value="${item.start_local_time}" aria-label="开始时间" required><input name="end" type="time" step="600" value="${item.end_local_time}" aria-label="结束时间" required><label title="警戒标签"><input name="tag" type="checkbox"${item.tag === 'alert' ? ' checked':''}> 警戒</label>${existing ? '<button type="button" class="interval-delete">删除</button>' : ''}</form>`;
    const form = actions.querySelector('form');
    form.querySelector('.interval-delete')?.addEventListener('click', async () => { if (!confirm(`删除「${item.name}」？`)) return; try { await post(`/api/time-intervals/${item.interval_id}`, 'DELETE'); resetEditor(); await load(); } catch (error) { alert(`删除失败：${error.message}`); } });
    const save = async () => {
      const values = new FormData(form); const payload = {name:String(values.get('name') || '').trim(), start_local_time:values.get('start'), end_local_time:values.get('end'), color:values.get('color'), tag:values.has('tag') ? 'alert' : null, foreground_display:true};
      if (!payload.name || !payload.start_local_time || !payload.end_local_time) return;
      try { await post(`/api/time-intervals/${item.interval_id}`, 'PATCH', payload); await load(); }
      catch (error) { alert(`自动保存失败：${error.message}`); const latest = snapshot.intervals.find(candidate => candidate.interval_id === item.interval_id); if (latest) showEditor(latest, true, false); }
    };
    form.addEventListener('change', save);
    form.addEventListener('submit', event => { event.preventDefault(); save(); });
    if (focus) form.querySelector('input[name=name]').focus();
  }
  async function openEditor(existing = null, initialStart = null) {
    const start = existing ? null : (initialStart ? minutes(initialStart) : findNewInterval());
    if (!existing && start === undefined) { alert('没有可容纳一小时的新时间区间，请先调整或删除现有区间。'); return; }
    const item = existing || {name:'新时间区间', start_local_time:clock(start), end_local_time:clock((start + 60) % 1440), color:nextUnusedColor(), tag:null, foreground_display:true};
    if (!existing) {
      try { existing = await post('/api/time-intervals', 'POST', item); await load(); }
      catch (error) { alert(`无法新增时间区间：${error.message}`); return; }
    }
    selectedId = existing.interval_id; editorOpen = true; render();
    showEditor(existing, true);
  }
  $('time-interval-create')?.addEventListener('click', event => { event.stopPropagation(); openEditor(); });
  $('bizday-time-intervals')?.addEventListener('click', event => { if (event.target !== event.currentTarget) return; event.stopPropagation(); if (editorOpen) resetEditor(); });
  document.addEventListener('click', event => {
    if (!editorOpen) return;
    if (event.target.closest('#time-interval-actions, .time-interval-bar, .time-interval-anchor')) return;
    resetEditor();
  });
  window.loadTimeIntervals = load;
  window.renderTimeIntervals = render;
  load().catch(error => { const node = $('time-interval-current'); if (node) node.textContent = error.message; });
  setInterval(() => load().catch(() => {}), 30_000);
  setInterval(renderCurrent, 1_000);
})();
