'use strict';
// Project map: a free local inventory and estimate, and a pausable paid DeepSeek scan.
// Everything is built from DOM nodes; model-authored text is never inserted as HTML.
let mapData = null, mapEstimate = null, mapJob = null, mapTimer = null, mapModule = null, mapTab = 'scan';

const mapButton = el('button', '▦ Карта проекта');
mapButton.id = 'map-open';
mapButton.type = 'button';
mapButton.title = 'Карта проекта: файлы, git, зависимости, скиллы и стоимость сканирования';
$('settings-button').before(mapButton);

document.body.insertAdjacentHTML('beforeend', `
<dialog id="map-dialog"><div class="section-heading"><div><span class="eyebrow">КАРТА ПРОЕКТА</span><h2 id="map-title">Карта проекта</h2></div><button type="button" data-close-map>✕</button></div>
<p class="muted" id="map-scope">Инвентаризация, git и оценка считаются локально и бесплатно. Токены расходуют только проверка, сканирование и одна сводка карты.</p>
<div class="map-tabs"><button type="button" class="map-tab active" data-map-tab="scan">Сканирование</button><button type="button" class="map-tab" data-map-tab="graph">Карта модулей</button><button type="button" class="map-tab" data-map-tab="memory">Память</button></div>
<section id="map-scan-tab" class="map-tab-panel">
  <div id="map-summary" class="map-cards"></div>
  <div id="map-estimate" class="map-cards"></div>
  <div class="map-controls">
    <label>Лимит расхода, $<input id="map-budget" type="number" min="0" max="100" step="0.05" value="0.50"></label>
    <button type="button" id="map-probe">Проверить на 3 файлах (оплачивается)</button>
    <button type="button" id="map-scan" class="primary">Сканировать</button>
    <button type="button" id="map-pause">Пауза</button>
    <button type="button" id="map-resume">Продолжить</button>
    <button type="button" id="map-cancel">Отменить</button>
  </div>
  <div id="map-progress" class="map-progress" hidden><div class="map-bar"><i id="map-bar-fill"></i></div><div id="map-progress-text" class="muted"></div></div>
  <div id="map-jobs" class="map-jobs"></div>
</section>
<section id="map-graph-tab" class="map-tab-panel" hidden>
  <div class="map-graph-layout"><div id="map-graph" class="map-graph"></div><div id="map-module" class="map-module"><p class="muted">Выберите модуль на карте, чтобы увидеть файлы, назначения и связанные скиллы.</p></div></div>
</section>
<section id="map-memory-tab" class="map-tab-panel" hidden>
  <h3>Обзор проекта</h3><p id="map-overview" class="muted">Карта ещё не построена.</p>
  <h3>Память для контекста агента</h3><pre id="map-memory" class="code-block"></pre>
  <p class="muted">Этот текст добавляется к правилам проекта в каждом ходе, пока карта актуальна.</p>
</section>
</dialog>`);
document.querySelectorAll('[data-close-map]').forEach(b => b.onclick = () => closeMap());

function closeMap() {
  clearInterval(mapTimer);
  mapTimer = null;
  const dialog = $('map-dialog');
  if (dialog.open) dialog.close ? dialog.close() : (dialog.open = false);
}
$('map-dialog').addEventListener('cancel', () => { clearInterval(mapTimer); mapTimer = null; });
document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && $('map-dialog').open) closeMap(); });

function mapCard(parent, title, value, note) {
  const card = el('div', undefined, 'map-card');
  card.append(el('small', title), el('strong', value));
  if (note) card.append(el('small', note, 'muted'));
  parent.append(card);
  return card;
}

function renderMapSummary() {
  const box = $('map-summary');
  box.replaceChildren();
  if (!mapEstimate) return;
  const inventory = mapEstimate.inventory, git = inventory.git || {};
  mapCard(box, 'Файлы', fmt(inventory.files), `${fmt(inventory.text_files)} текстовых · ${fmt(inventory.binary_files)} бинарных`);
  const languages = Object.entries(inventory.by_language || {}).sort((a, b) => b[1].files - a[1].files).slice(0, 3)
    .map(([name, item]) => `${name}: ${item.files}`).join(' · ');
  mapCard(box, 'Языки', String(Object.keys(inventory.by_language || {}).length), languages || '—');
  mapCard(box, 'Символов', fmt(inventory.total_chars), 'считано локально, 0 токенов');
  mapCard(box, 'Git', git.repository ? (git.branch || 'HEAD') : 'нет репозитория',
          git.repository ? `${git.head || ''} · коммитов: ${fmt((git.commits || []).length)} · изменено: ${fmt(git.dirty)}`
                         : (git.note || ''));
  const skills = inventory.skills || {};
  mapCard(box, 'Скиллы проекта', fmt((skills.own || []).length + (skills.related || []).length),
          `заметок памяти: ${fmt((skills.memory || []).length)}`);
}

function renderMapEstimate() {
  const box = $('map-estimate');
  box.replaceChildren();
  if (!mapEstimate) return;
  const e = mapEstimate.estimate;
  mapCard(box, 'Оценка входа', fmt(e.est_input_tokens) + ' т.', `${fmt(e.est_requests)} запросов · калибровка ×${e.calibration}`);
  mapCard(box, 'Оценка выхода', fmt(e.est_output_tokens) + ' т.', e.formula);
  mapCard(box, 'Полное сканирование ≈', e.priced ? money(e.est_cost_usd) : 'тариф неизвестен',
          e.priced ? 'с кешем 80% ≈ ' + money(e.est_cost_usd_cached) : 'модель ' + e.model);
  mapCard(box, 'Сводка карты ≈', e.priced ? money(e.est_cost_map_generation) : '—', 'один запрос поверх кратких описаний');
  mapCard(box, 'Итого ≈', e.priced ? money(e.total_cost_usd) : '—', e.note);
}

function renderMapJobs() {
  const box = $('map-jobs');
  box.replaceChildren();
  const jobs = (mapData && mapData.jobs) || [];
  for (const job of jobs.slice(0, 6)) {
    const row = el('div', undefined, 'map-job');
    const stats = job.stats || {};
    row.append(el('strong', mapStateLabel(job.state)),
               el('small', `${fmt(job.done)} / ${fmt(job.total)} пакетов · ${fmt(job.files)} файлов · `
                  + `${fmt((stats.prompt_tokens || 0) + (stats.completion_tokens || 0))} токенов · ${money(stats.cost_usd || 0)}`),
               el('small', new Date(job.created * 1000).toLocaleString('ru-RU'), 'muted'));
    if (job.reason) row.append(el('small', job.reason, 'muted'));
    box.append(row);
  }
  if (!jobs.length) box.append(el('p', 'Сканирование ещё не запускалось.', 'muted'));
}

function mapStateLabel(state) {
  return {running: 'Идёт сканирование', pausing: 'Останавливается после текущего запроса', paused: 'На паузе',
          completed: 'Завершено', cancelled: 'Отменено', failed: 'Ошибка'}[state] || state;
}

function renderMapProgress() {
  const job = mapJob;
  $('map-progress').hidden = !job;
  $('map-pause').disabled = !job || !['running', 'queued'].includes(job.state);
  $('map-resume').disabled = !job || !['paused', 'failed'].includes(job.state);
  $('map-cancel').disabled = !job || ['completed', 'cancelled'].includes(job.state);
  if (!job) return;
  const stats = job.stats || {};
  const percent = job.total ? Math.round(100 * job.done / job.total) : 0;
  $('map-bar-fill').style.width = percent + '%';
  $('map-bar-fill').classList.toggle('paused', job.state !== 'running');
  setText('map-progress-text', `${mapStateLabel(job.state)} · ${fmt(job.done)}/${fmt(job.total)} · `
    + `${fmt((stats.prompt_tokens || 0) + (stats.completion_tokens || 0))} токенов · потрачено ${money(stats.cost_usd || 0)}`
    + (job.reason ? ' · ' + job.reason : ''));
}

// --------------------------------------------------------------- module graph
function layout(modules, edges, width, height) {
  const nodes = modules.map((m, i) => ({
    name: m.name, count: m.count,
    x: width / 2 + Math.cos(i * 2.4) * (60 + i * 12), y: height / 2 + Math.sin(i * 2.4) * (45 + i * 9),
    vx: 0, vy: 0,
  }));
  const index = Object.fromEntries(nodes.map(n => [n.name, n]));
  const links = edges.filter(e => index[e.source] && index[e.target]);
  for (let step = 0; step < 220; step++) {
    for (const a of nodes) {
      for (const b of nodes) {
        if (a === b) continue;
        const dx = a.x - b.x, dy = a.y - b.y, distance = Math.max(18, Math.hypot(dx, dy));
        const force = 2600 / (distance * distance);
        a.vx += dx / distance * force; a.vy += dy / distance * force;
      }
      a.vx += (width / 2 - a.x) * 0.012; a.vy += (height / 2 - a.y) * 0.012;
    }
    for (const link of links) {
      const a = index[link.source], b = index[link.target];
      const dx = b.x - a.x, dy = b.y - a.y, distance = Math.max(1, Math.hypot(dx, dy));
      const pull = (distance - 120) * 0.02;
      a.vx += dx / distance * pull; a.vy += dy / distance * pull;
      b.vx -= dx / distance * pull; b.vy -= dy / distance * pull;
    }
    for (const node of nodes) {
      node.x = Math.min(width - 40, Math.max(40, node.x + (node.vx *= 0.6)));
      node.y = Math.min(height - 30, Math.max(30, node.y + (node.vy *= 0.6)));
    }
  }
  return {nodes, index, links};
}

function renderMapGraph() {
  const box = $('map-graph');
  box.replaceChildren();
  const document_ = mapData && mapData.map && mapData.map.map;
  if (!document_ || !document_.modules.length) {
    box.append(el('p', 'Карта ещё не построена. Запустите сканирование на вкладке «Сканирование».', 'muted'));
    return;
  }
  const width = 620, height = 420;
  const {nodes, index, links} = layout(document_.modules, document_.edges || [], width, height);
  const svg = svgNode('svg', {viewBox: `0 0 ${width} ${height}`, role: 'img',
                              'aria-label': 'Граф зависимостей модулей проекта'});
  const defs = svgNode('defs', {});
  const marker = svgNode('marker', {id: 'map-arrow', viewBox: '0 0 10 10', refX: '10', refY: '5',
                                    markerWidth: '7', markerHeight: '7', orient: 'auto-start-reverse'});
  marker.append(svgNode('path', {d: 'M 0 0 L 10 5 L 0 10 z', fill: '#4d6472'}));
  defs.append(marker);
  svg.append(defs);
  for (const link of links) {
    const a = index[link.source], b = index[link.target];
    const line = svgNode('line', {x1: a.x, y1: a.y, x2: b.x, y2: b.y, stroke: link.origin === 'model' ? '#6a5b7a' : '#3f5361',
                                  'stroke-width': Math.min(4, 1 + (link.weight || 1) / 2), 'marker-end': 'url(#map-arrow)'});
    line.append(svgNode('title', {}, `${link.source} → ${link.target} · ${link.weight || 1}`));
    svg.append(line);
  }
  const maximum = Math.max(...nodes.map(n => n.count), 1);
  for (const node of nodes) {
    const radius = 14 + 22 * Math.sqrt(node.count / maximum);
    const group = svgNode('g', {class: 'map-node' + (mapModule === node.name ? ' selected' : ''),
                                tabindex: '0', role: 'button'});
    const circle = svgNode('circle', {cx: node.x, cy: node.y, r: radius,
                                      fill: mapModule === node.name ? '#26584d' : '#1b2a34', stroke: '#4d6472'});
    circle.append(svgNode('title', {}, `${node.name}: ${node.count} файлов`));
    const label = svgNode('text', {x: node.x, y: node.y + radius + 13, 'text-anchor': 'middle'}, node.name);
    const count = svgNode('text', {x: node.x, y: node.y + 4, 'text-anchor': 'middle', class: 'map-node-count'},
                          String(node.count));
    group.append(circle, count, label);
    group.addEventListener('click', () => showModule(node.name));
    group.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') showModule(node.name); });
    svg.append(group);
  }
  box.append(svg);
  const legend = el('p', 'Размер узла — число файлов. Серые связи найдены локально по импортам, лиловые — от модели.', 'muted');
  box.append(legend);
}

function showModule(name) {
  mapModule = name;
  const document_ = mapData && mapData.map && mapData.map.map;
  const module = (document_ ? document_.modules : []).find(m => m.name === name);
  const pane = $('map-module');
  pane.replaceChildren();
  if (!module) return;
  pane.append(el('h3', module.name), el('small', `${fmt(module.count)} файлов · ${fmt(module.chars)} символов`, 'muted'));
  if (module.skills && module.skills.length) {
    const chips = el('div', undefined, 'map-chips');
    for (const skill of module.skills) {
      const chip = el('button', skill.title || skill.path, 'skill-tag small');
      chip.type = 'button';
      chip.title = skill.path;
      chip.onclick = handle(async () => {
        if (window.SkillManager && typeof window.SkillManager.open === 'function') await window.SkillManager.open(skill.id);
        else toast('Скилл: ' + (skill.title || skill.path) + ' · ' + skill.path);
      });
      chips.append(chip);
    }
    pane.append(el('div', 'Связанные скиллы', 'map-subtitle'), chips);
  }
  const list = el('div', undefined, 'map-files');
  for (const file of module.files.slice(0, 200)) {
    const row = el('div', undefined, 'map-file');
    row.append(el('strong', file.path), el('small', `${file.language} · ${fmt(file.lines)} строк`, 'muted'));
    if (file.purpose) row.append(el('p', file.purpose));
    else if (file.unparsed) row.append(el('p', 'Модель вернула ответ не в JSON; описание не сохранено.', 'muted'));
    if (file.key_symbols && file.key_symbols.length) row.append(el('small', 'Символы: ' + file.key_symbols.join(', '), 'muted'));
    list.append(row);
  }
  pane.append(list);
  renderMapGraph();
}

function renderMapMemory() {
  const latest = mapData && mapData.map;
  setText('map-overview', (latest && latest.map.overview) || 'Карта ещё не построена.');
  setText('map-memory', (mapData && mapData.memory_context) || 'Память появится после первого сканирования.');
}

// --------------------------------------------------------------- loading and actions
async function loadMapState() {
  if (!current) return;
  mapData = await api(`/api/sessions/${current.id}/map`);
  mapJob = mapData.active || (mapData.jobs || [])[0] || null;
  renderMapJobs(); renderMapProgress(); renderMapMemory();
  if (mapTab === 'graph') renderMapGraph();
  scheduleMapPolling();
}

function scheduleMapPolling() {
  const running = mapJob && ['running', 'pausing', 'queued'].includes(mapJob.state);
  if (!running || !$('map-dialog').open) { clearInterval(mapTimer); mapTimer = null; return; }
  if (mapTimer) return;
  mapTimer = setInterval(() => {
    if (!$('map-dialog').open || !current) { clearInterval(mapTimer); mapTimer = null; return; }
    api(`/api/sessions/${current.id}/map`).then(data => {
      mapData = data;
      mapJob = data.active || (data.jobs || [])[0] || null;
      renderMapJobs(); renderMapProgress(); renderMapMemory();
      if (!mapJob || !['running', 'pausing', 'queued'].includes(mapJob.state)) {
        clearInterval(mapTimer); mapTimer = null;
        if (mapTab === 'graph') renderMapGraph();
      }
    }).catch(() => {});
  }, 2000);
}

async function loadMapEstimate() {
  if (!current) return;
  mapEstimate = await api(`/api/sessions/${current.id}/map/estimate`);
  renderMapSummary(); renderMapEstimate();
  setText('map-scope', mapEstimate.token_test);
}

async function openMap() {
  if (!current) throw new Error('Сначала выберите или создайте чат');
  setText('map-title', 'Карта проекта · ' + (current.title || current.id));
  const dialog = $('map-dialog');
  if (dialog.showModal) dialog.showModal(); else dialog.open = true;
  $('map-budget').value = String((await api('/api/settings')).project_map_budget_usd ?? 0.5);
  await Promise.all([loadMapEstimate(), loadMapState()]);
}

function selectMapTab(name) {
  mapTab = name;
  document.querySelectorAll('[data-map-tab]').forEach(b => b.classList.toggle('active', b.dataset.mapTab === name));
  for (const tab of ['scan', 'graph', 'memory']) $('map-' + tab + '-tab').hidden = tab !== name;
  if (name === 'graph') renderMapGraph();
  if (name === 'memory') renderMapMemory();
}
document.querySelectorAll('[data-map-tab]').forEach(b => b.onclick = () => selectMapTab(b.dataset.mapTab));

mapButton.onclick = handle(openMap);
$('map-probe').onclick = handle(async () => {
  $('map-probe').disabled = true;
  try {
    const result = await api(`/api/sessions/${current.id}/map/probe`, {method: 'POST'});
    mapEstimate = {...mapEstimate, estimate: result.estimate};
    renderMapEstimate();
    toast(`Проверка: ожидали ${fmt(result.expected_input_tokens)} входных токенов, фактически `
      + `${fmt(result.actual_input_tokens)} · ${money(result.cost_usd)} · калибровка ×${result.calibration}`);
  } finally {
    $('map-probe').disabled = false;
  }
});
$('map-scan').onclick = handle(async () => {
  const budget = Number($('map-budget').value || 0);
  mapJob = await api(`/api/sessions/${current.id}/map/scan`,
                     {method: 'POST', body: {mode: 'summaries', max_cost_usd: budget}});
  toast(`Сканирование запущено: ${fmt(mapJob.total)} запросов, лимит ${money(budget)}`);
  await loadMapState();
});
for (const action of ['pause', 'resume', 'cancel']) {
  $('map-' + action).onclick = handle(async () => {
    if (!mapJob) throw new Error('Нет активной задачи сканирования');
    mapJob = await api(`/api/sessions/${current.id}/map/jobs/${mapJob.id}/${action}`, {method: 'POST'});
    renderMapProgress();
    await loadMapState();
  });
}
