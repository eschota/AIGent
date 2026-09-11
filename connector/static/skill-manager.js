'use strict';
// Skill Manager: a local index of Markdown skills from Codex, Claude and project sessions.
// The index is built by the server from the filesystem only; no provider tokens are spent.
let skillStatus = null, skillResults = [], skillTags = [], activeSkillTags = new Set(), skillTimer = null;

const skillWidget = el('div', undefined, 'skills-widget');
skillWidget.id = 'skills-widget';
skillWidget.innerHTML = `<div class="sidebar-label">Скиллы <button id="skills-rescan" title="Пересканировать сейчас">↻</button></div>
<button id="skills-open" class="skill-summary" type="button">
  <span class="skill-state" id="skills-state">Индексация…</span>
  <span class="skill-progress"><i id="skills-bar"></i></span>
  <small id="skills-counts">—</small>
</button>
<div id="skills-hover" class="skills-hover" hidden></div>`;

document.body.insertAdjacentHTML('beforeend', `
<dialog id="skills-dialog"><div class="section-heading"><div><span class="eyebrow">SKILL MANAGER</span><h2>Скиллы Codex, Claude и проектов</h2></div><button type="button" data-close-skills>✕</button></div>
<p class="muted" id="skills-scope">Индекс строится локально из .md файлов доступных сессий. Токены не расходуются.</p>
<form id="skills-search-form"><input id="skills-query" placeholder="Поиск по названию, тегам и содержимому" maxlength="200" autocomplete="off">
<select id="skills-sort"><option value="relevant">Релевантные</option><option value="recent">Последние изменённые</option><option value="active">Самые активные</option></select>
<select id="skills-source"><option value="">Все источники</option><option value="claude">Claude</option><option value="codex">Codex</option><option value="project">Проекты</option><option value="session">Сессии</option><option value="skill">Тип: skill</option><option value="memory">Тип: memory</option><option value="prompt">Тип: prompt</option><option value="instructions">Тип: инструкции</option></select>
<button class="primary" type="submit">Искать</button></form>
<div id="skills-tagbar" class="skills-tagbar"></div>
<div class="skills-layout"><div id="skills-results" class="skills-results"></div><div id="skills-detail" class="skills-detail"><p class="muted">Выберите скилл, чтобы увидеть содержимое.</p></div></div>
<div class="skills-footer"><label>Прикрепить в чат<select id="skills-target"></select></label><span id="skills-note" class="muted"></span></div>
</dialog>`);
document.querySelectorAll('[data-close-skills]').forEach(b => b.onclick = () => $('skills-dialog').close());

function mountSkillWidget() {
  const projects = document.querySelector('.projects-section');
  if (projects) projects.after(skillWidget);
  else $('sessions').previousElementSibling.before(skillWidget);
}
mountSkillWidget();

function skillAge(value) {
  if (!value) return '—';
  const days = (Date.now() / 1000 - value) / 86400;
  if (days < 1 / 24) return 'только что';
  if (days < 1) return Math.round(days * 24) + ' ч назад';
  if (days < 30) return Math.round(days) + ' дн назад';
  return new Date(value * 1000).toLocaleDateString('ru-RU');
}
const sourceLabel = (s) => ({claude: 'Claude', codex: 'Codex', project: 'Проект', session: 'Сессия'}[s] || s);

function renderSkillStatus() {
  if (!skillStatus) return;
  const scanning = skillStatus.state === 'scanning';
  setText('skills-state', scanning ? 'Сканирование…' : skillStatus.error ? 'Индекс с ошибкой' : 'Индекс готов');
  $('skills-bar').style.width = Math.round((skillStatus.progress || 0) * 100) + '%';
  $('skills-bar').classList.toggle('scanning', scanning);
  setText('skills-counts', `${fmt(skillStatus.total)} скиллов · ${skillAge(skillStatus.finished)}`);
  const card = $('skills-hover');
  card.replaceChildren();
  const head = el('div', undefined, 'skills-hover-head');
  head.append(el('strong', scanning ? 'Фоновое сканирование' : 'Skill Manager'),
              el('small', `${fmt(skillStatus.scanned)} файлов просмотрено · найдено ${fmt(skillStatus.total)}`));
  card.append(head);
  const sources = Object.entries(skillStatus.sources || {}).map(([k, v]) => `${sourceLabel(k)}: ${v}`).join(' · ');
  if (sources) card.append(el('small', sources, 'muted'));
  if (skillStatus.error) card.append(el('small', 'Ошибка: ' + skillStatus.error, 'muted'));
  for (const [title, list] of [['Последние изменённые', skillStatus.recent || []], ['Самые активные', skillStatus.active || []]]) {
    if (!list.length) continue;
    card.append(el('div', title, 'skills-hover-title'));
    for (const item of list.slice(0, 5)) {
      const row = el('div', undefined, 'skills-hover-row');
      row.append(el('span', item.title || item.name), el('small', `${sourceLabel(item.source)} · ${skillAge(item.modified)}${item.uses ? ' · ' + item.uses + '×' : ''}`));
      card.append(row);
    }
  }
  card.append(el('small', 'Локальный анализ · токенов потрачено: 0', 'muted'));
}

async function refreshSkillStatus() {
  try {
    skillStatus = await api('/api/skills/status');
    renderSkillStatus();
  } catch { /* login or restart in progress; the next tick retries */ }
}

function renderSkillTags() {
  $('skills-tagbar').replaceChildren(...skillTags.map(item => {
    const b = el('button', `${item.tag} · ${item.count}`, 'skill-tag' + (activeSkillTags.has(item.tag) ? ' selected' : ''));
    b.type = 'button';
    b.onclick = handle(async () => {
      activeSkillTags.has(item.tag) ? activeSkillTags.delete(item.tag) : activeSkillTags.add(item.tag);
      renderSkillTags(); await searchSkills();
    });
    return b;
  }));
}

function renderSkillResults() {
  $('skills-results').replaceChildren(...skillResults.map(item => {
    const row = el('article', undefined, 'skill-row');
    const head = el('div', undefined, 'skill-row-head');
    head.append(el('strong', item.title || item.name), el('span', sourceLabel(item.source), 'skill-badge'));
    row.append(head, el('p', (item.summary || '').slice(0, 180) || item.name, 'muted'));
    const meta = el('div', undefined, 'skill-meta');
    meta.append(el('small', `${item.kind || 'doc'} · ${item.origin || ''} · ${skillAge(item.modified)}${item.uses ? ' · использован ' + item.uses + '×' : ''}`));
    row.append(meta);
    const tags = el('div', undefined, 'skill-tags');
    for (const tag of (item.tags || []).slice(0, 8)) {
      const b = el('button', tag, 'skill-tag small'); b.type = 'button';
      b.onclick = handle(async () => { activeSkillTags.add(tag); renderSkillTags(); await searchSkills(); });
      tags.append(b);
    }
    row.append(tags);
    const actions = el('div', undefined, 'skill-actions');
    const open = el('button', 'Просмотр'); open.type = 'button';
    open.onclick = handle(() => showSkill(item.id));
    const attach = el('button', 'Прикрепить в чат', 'primary'); attach.type = 'button';
    attach.onclick = handle(() => attachSkill(item.id));
    actions.append(open, attach); row.append(actions);
    return row;
  }));
  if (!skillResults.length) $('skills-results').append(el('p', 'Ничего не найдено. Уточните запрос или снимите теги.', 'muted'));
}

async function searchSkills() {
  const params = new URLSearchParams({query: $('skills-query').value, sort: $('skills-sort').value,
                                      source: $('skills-source').value, tags: [...activeSkillTags].join(','), limit: '60'});
  skillResults = await api('/api/skills?' + params);
  renderSkillResults();
}

async function showSkill(id) {
  const item = await api('/api/skills/' + id);
  const pane = $('skills-detail');
  pane.replaceChildren();
  pane.append(el('h3', item.title || item.name), el('small', item.path, 'muted'));
  if (!item.available) pane.append(el('p', 'Файл больше не доступен на диске; показан сохранённый фрагмент.', 'muted'));
  pane.append(el('pre', (item.text || '').slice(0, 20000), 'code-block'));
  const attach = el('button', 'Прикрепить в выбранный чат', 'primary'); attach.type = 'button';
  attach.onclick = handle(() => attachSkill(item.id));
  pane.append(attach);
}

async function attachSkill(id) {
  const sid = $('skills-target').value || current?.id;
  if (!sid) throw new Error('Сначала выберите или создайте чат');
  const result = await api(`/api/skills/${id}/attach`, {method: 'POST', body: {session_id: sid}});
  setText('skills-note', `Прикреплён «${result.title}» → ${result.path}`);
  if (current?.id === sid) {
    const box = $('message');
    box.value = (box.value ? box.value.replace(/\s*$/, '\n\n') : '') + result.reference + '\n';
    box.dispatchEvent(new Event('input'));
    box.focus();
  }
  toast(`Скилл «${result.title}» прикреплён к чату`);
  await refreshSkillStatus();
  await searchSkills();
}

function fillSkillTargets() {
  const target = $('skills-target');
  const previous = target.value || current?.id || '';
  target.replaceChildren(...(typeof allSessions !== 'undefined' ? allSessions : []).map(s => {
    const o = el('option', s.title + ' · ' + (s.chat_id ? 'Telegram' : 'Web'));
    o.value = s.id; return o;
  }));
  if ([...target.options].some(o => o.value === previous)) target.value = previous;
}

async function openSkills() {
  await Promise.all([refreshSkillStatus(), (async () => { skillTags = await api('/api/skills/tags?limit=24'); })()]);
  renderSkillTags(); fillSkillTargets();
  await searchSkills();
  setText('skills-note', skillStatus?.roots?.length ? `Источников: ${skillStatus.roots.length} · анализ локальный, 0 токенов` : '');
  const dialog=$('skills-dialog');
  if (dialog.showModal) dialog.showModal(); else dialog.open = true;
}

$('skills-open').onclick = handle(openSkills);
$('skills-rescan').onclick = handle(async (event) => {
  event.stopPropagation();
  setText('skills-state', 'Сканирование…');
  skillStatus = await api('/api/skills/rescan', {method: 'POST'});
  await refreshSkillStatus();
  if ($('skills-dialog').open) await searchSkills();
  toast(`Индекс обновлён: ${fmt(skillStatus.total ?? skillStatus.files)} скиллов`);
});
$('skills-search-form').onsubmit = handle(searchSkills);
$('skills-sort').onchange = handle(searchSkills);
$('skills-source').onchange = handle(searchSkills);
skillWidget.addEventListener('pointerenter', () => { $('skills-hover').hidden = false; refreshSkillStatus(); });
skillWidget.addEventListener('pointerleave', () => { $('skills-hover').hidden = true; });
skillWidget.addEventListener('focusin', () => { $('skills-hover').hidden = false; });
skillWidget.addEventListener('focusout', () => { $('skills-hover').hidden = true; });

refreshSkillStatus();
clearInterval(skillTimer);
skillTimer = setInterval(() => { if (!$('workspace').hidden) refreshSkillStatus(); }, 20000);
