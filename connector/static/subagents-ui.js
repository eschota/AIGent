'use strict';
// Code subagents: a compact live panel of the current parallel fan-out. It is driven entirely
// by the `subagent` SSE events (spawn/update/done) re-broadcast on the window `aigent:event`
// channel, and it collapses to nothing when there are no subagents. DOM-only: nothing that a
// subagent produced is ever inserted as HTML.
(function () {
  const CAP = 16;                 // keep at most this many chips, trimming finished ones first
  const LIVE = new Set(['queued', 'running']);
  let sid = null;                 // the session the panel currently reflects
  const items = new Map();        // id -> record, insertion order preserved
  let selected = null, timer = null;

  const panel = el('div', undefined, 'subagents-panel');
  panel.id = 'subagents-panel';
  panel.hidden = true;
  const head = el('div', undefined, 'subagents-head');
  const total = el('div', undefined, 'subagents-total');
  const dot = el('span', undefined, 'sa-dot');
  const totalText = el('span', 'Субагенты');
  const totalSum = el('span', '', 'subagents-sum');
  total.append(dot, totalText, totalSum);
  const cancel = el('button', 'Отменить все', 'subagents-cancel');
  cancel.type = 'button';
  head.append(total, cancel);
  const chips = el('div', undefined, 'subagents-chips');
  const detail = el('div', undefined, 'sa-detail');
  detail.hidden = true;
  panel.append(head, chips, detail);
  // Sit with the other volatile bars just above the composer.
  const anchor = $('activity-bar') || $('composer');
  anchor.before(panel);

  cancel.onclick = handle(async () => {
    if (!current) return;
    await api(`/api/sessions/${current.id}/subagents/cancel`, { method: 'POST' });
    toast('Останавливаю субагентов…');
  });

  const seconds = (v) => v == null ? '0 с' : (v < 60 ? Math.round(v) + ' с' : Math.floor(v / 60) + ' мин ' + String(Math.round(v) % 60).padStart(2, '0') + ' с');

  function reset(nextSid) {
    sid = nextSid || null;
    items.clear();
    selected = null;
    detail.hidden = true;
    detail.replaceChildren();
    render();
  }

  function trim() {
    while (items.size > CAP) {
      let victim = null;
      for (const [id, rec] of items) { if (!LIVE.has(rec.status)) { victim = id; break; } }
      if (victim == null) break;
      items.delete(victim);
    }
  }

  function apply(rec) {
    const existing = items.get(rec.id) || {};
    const merged = { ...existing, ...rec };
    // Anchor a live clock the first time a subagent starts running.
    if (rec.status === 'running' && !existing.startMs) merged.startMs = Date.now() - (rec.seconds || 0) * 1000;
    if (!LIVE.has(rec.status)) merged.startMs = 0;   // finished: freeze at the reported seconds
    items.set(rec.id, merged);
    trim();
  }

  function liveCount() {
    let n = 0;
    for (const rec of items.values()) if (LIVE.has(rec.status)) n++;
    return n;
  }

  function render() {
    const list = [...items.values()];
    panel.hidden = list.length === 0;
    if (panel.hidden) { stopTimer(); return; }
    const count = liveCount();
    const tokens = list.reduce((s, r) => s + (r.tokens || 0), 0);
    const cost = list.reduce((s, r) => s + (r.cost_usd || 0), 0);
    dot.style.visibility = count ? 'visible' : 'hidden';
    totalText.textContent = count ? count + ' ' + plural(count, ['субагент', 'субагента', 'субагентов']) : 'Субагенты';
    totalSum.textContent = `Σ ${fmt(tokens)} токенов · Σ ${money(cost)}`;
    cancel.hidden = count === 0;

    chips.replaceChildren();
    for (const rec of list) {
      const chip = el('button', undefined, 'sa-chip ' + rec.status);
      chip.type = 'button';
      chip.style.setProperty('--sa-color', rec.color || '#4f8cff');
      chip.append(el('span', undefined, 'sa-spin'));
      chip.append(el('span', rec.emoji || '▪', 'sa-emoji'));
      chip.append(el('span', rec.goal || rec.id, 'sa-goal'));
      const clock = LIVE.has(rec.status) && rec.startMs
        ? seconds((Date.now() - rec.startMs) / 1000) : seconds(rec.seconds);
      const meta = el('span', `${clock} · ${fmt(rec.context_tokens || 0)} т. · ${money(rec.cost_usd || 0)}`, 'sa-meta');
      chip.append(meta);
      chip.title = (rec.goal || '') + ' · ' + statusLabel(rec.status);
      chip.onclick = () => { selected = selected === rec.id ? null : rec.id; renderDetail(); };
      chips.append(chip);
    }
    renderDetail();
    if (count) startTimer(); else stopTimer();
  }

  function renderDetail() {
    const rec = selected && items.get(selected);
    detail.hidden = !rec;
    if (!rec) return;
    detail.replaceChildren();
    detail.append(el('h4', (rec.emoji || '') + ' ' + (rec.goal || rec.id)));
    const bits = [statusLabel(rec.status)];
    if (rec.kind) bits.push('тип: ' + rec.kind);
    if (rec.score != null) bits.push('оценка ' + rec.score);
    bits.push(fmt(rec.tokens || 0) + ' токенов');
    bits.push('контекст ' + fmt(rec.context_tokens || 0) + ' т.');
    bits.push(money(rec.cost_usd || 0));
    bits.push(seconds(rec.seconds));
    detail.append(el('p', bits.join(' · '), 'muted'));
    if (rec.path) detail.append(el('p', 'Предложен файл: ' + rec.path));
    if (rec.summary) detail.append(el('pre', rec.summary));
    if (rec.error) detail.append(el('p', rec.error, 'muted'));
    if (LIVE.has(rec.status)) detail.append(el('p', 'Выполняется…', 'muted'));
    else if (!rec.error) detail.append(el('p', 'Это предложение; изменение применяется через обычную проверку.', 'muted'));
  }

  function statusLabel(status) {
    return { queued: 'в очереди', running: 'выполняется', done: 'готово',
             failed: 'ошибка', cancelled: 'отменён' }[status] || status;
  }
  function plural(n, forms) {
    const mod10 = n % 10, mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return forms[0];
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return forms[1];
    return forms[2];
  }

  function startTimer() { if (!timer) timer = setInterval(render, 1000); }
  function stopTimer() { if (timer) { clearInterval(timer); timer = null; } }

  window.addEventListener('aigent:event', (event) => {
    const { event: entry, sid: eventSid } = event.detail || {};
    if (!entry || entry.kind !== 'subagent') return;
    // Historical events are replayed on session switch; only reflect the session in view.
    if (current && eventSid && eventSid !== current.id) return;
    if (eventSid && eventSid !== sid) reset(eventSid);
    apply(entry.payload || {});
    render();
  });

  // Expose a reset the main app calls when the selected session changes, so a switch to a
  // session with no live subagents clears any leftover chips before its history replays.
  window.SubAgents = {
    reset: (nextSid) => reset(nextSid),
    async load() {
      if (!current) return reset(null);
      reset(current.id);
      try {
        const snap = await api(`/api/sessions/${current.id}/subagents`);
        for (const rec of snap.subagents || []) apply(rec);
        render();
      } catch { /* a session without subagents simply stays collapsed */ }
    },
  };
})();
