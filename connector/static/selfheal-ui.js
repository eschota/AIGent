'use strict';
// Self-heal UI: a "🔧 Починить" button on every error/trace event, and a banner in a fix chat
// with confirm / restart controls. DOM-only; no innerHTML from server data; consistent with the
// dark theme. It reuses app.js globals (el, api, handle, toast, current, refresh, selectSession).

// --- 0. Minimal styling, injected so index.html needs no change -----------------------------
const healStyle = document.createElement('style');
healStyle.textContent = `
.heal-btn{display:inline-block;margin-top:8px;padding:3px 10px;font-size:12px;cursor:pointer;
  border-radius:6px;border:1px solid #7a4;background:rgba(120,170,70,.15);color:inherit;}
.heal-btn:hover{background:rgba(120,170,70,.3);}
.heal-btn:disabled{opacity:.5;cursor:default;}
.heal-banner{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:0 0 10px;padding:8px 12px;
  border-radius:8px;border:1px solid rgba(200,160,60,.5);background:rgba(200,160,60,.12);font-size:13px;}
.heal-banner .heal-source{opacity:.7;}
.heal-banner .heal-actions{display:flex;flex-wrap:wrap;gap:6px;margin-left:auto;}
.heal-banner button{padding:3px 10px;font-size:12px;cursor:pointer;border-radius:6px;
  border:1px solid rgba(255,255,255,.25);background:rgba(255,255,255,.08);color:inherit;}
.heal-banner button.primary{border-color:#7a4;background:rgba(120,170,70,.25);}
.heal-banner button.heal-danger{border-color:#c55;background:rgba(200,80,80,.18);}
.heal-banner button:hover{filter:brightness(1.2);}
.heal-banner button:disabled{opacity:.5;cursor:default;}`;
document.head.appendChild(healStyle);

// --- 1. A repair button on each rendered error / trace event -------------------------------
// aigent:event fires (from app.js) just before the node is created, so queue the event id and let
// a MutationObserver bind it to the node the moment it appears — order preserved, no polling.
const healQueue = [];
window.addEventListener('aigent:event', (event) => {
  const detail = event.detail && event.detail.event;
  if (detail && (detail.kind === 'error' || detail.kind === 'trace')) {
    healQueue.push({id: detail.id, sid: event.detail.sid});
  }
});

function healButton(article, ref) {
  if (!article || article.querySelector('.heal-btn')) return;
  const button = el('button', '🔧 Починить', 'heal-btn');
  button.type = 'button';
  button.title = 'Создать чат-починку: он получит контекст этой ошибки и начнёт исправление';
  button.onclick = handle(async () => {
    const sid = (ref && ref.sid) || (current && current.id);
    if (!sid) throw new Error('Нет активной сессии');
    button.disabled = true;
    try {
      const result = await api(`/api/sessions/${sid}/heal`, {method: 'POST',
        body: {error_ref: ref ? ref.id : null}});
      toast('Чат-починка создан — открываю');
      await refresh();
      const target = (allSessions || []).find((s) => s.id === result.fix_sid);
      await selectSession(target || result.session);
    } finally {
      button.disabled = false;
    }
  });
  article.append(button);
}

const healObserver = new MutationObserver((mutations) => {
  for (const mutation of mutations) {
    for (const node of mutation.addedNodes) {
      if (node.nodeType !== 1) continue;
      const matches = [];
      if (node.matches && node.matches('article.event.error, article.event.trace')) matches.push(node);
      if (node.querySelectorAll) matches.push(...node.querySelectorAll('article.event.error, article.event.trace'));
      for (const article of matches) {
        if (article.querySelector('.heal-btn')) continue;
        healButton(article, healQueue.shift());
      }
    }
  }
});
const events = document.getElementById('events');
if (events) healObserver.observe(events, {childList: true, subtree: true});

// --- 2. A banner + controls when the open chat is itself a fix chat --------------------------
const healBanner = el('div', undefined, 'heal-banner');
healBanner.hidden = true;
const chatPanel = document.getElementById('chat-panel');
if (chatPanel) chatPanel.prepend(healBanner);

function renderHealBanner(fix) {
  healBanner.replaceChildren();
  const title = el('strong', fix.archived ? '🔧 Чат-починка · заархивирован' : '🔧 Чат-починка');
  const source = el('span', ' · источник ' + fix.source_sid, 'heal-source');
  healBanner.append(title, source);
  const actions = el('div', undefined, 'heal-actions');

  const open = el('button', 'Открыть источник', 'heal-link');
  open.type = 'button';
  open.onclick = handle(async () => {
    await refresh();
    const target = (allSessions || []).find((s) => s.id === fix.source_sid);
    if (target) await selectSession(target); else toast('Исходная сессия недоступна');
  });
  actions.append(open);

  if (!fix.archived) {
    const confirm = el('button', '✓ Фикс подтверждён', 'primary');
    confirm.type = 'button';
    confirm.onclick = handle(async () => {
      confirm.disabled = true;
      try {
        await api(`/api/heal/${fix.fix_sid}/confirm`, {method: 'POST', body: {verified: true, restart: false}});
        toast('Фикс подтверждён и заархивирован');
        await refresh();
        await refreshHealBanner();
      } finally {
        confirm.disabled = false;
      }
    });
    actions.append(confirm);

    // Restart is powerful: it is only ever triggered after an explicit browser confirmation.
    const restart = el('button', 'Перезапустить клиент', 'heal-danger');
    restart.type = 'button';
    restart.onclick = handle(async () => {
      if (!window.confirm('Перезапустить клиент сейчас? Ход других сессий блокирует перезапуск.')) return;
      const result = await api(`/api/heal/${fix.fix_sid}/restart`, {method: 'POST'});
      toast(result.note || (result.queued ? 'Перезапуск запрошен' : 'Перезапуск отклонён'));
    });
    actions.append(restart);
  }
  healBanner.append(actions);
  healBanner.hidden = false;
}

async function refreshHealBanner() {
  const sid = current && current.id;
  if (!sid) { healBanner.hidden = true; return; }
  let fixes;
  try { fixes = await api('/api/heal'); } catch { return; }
  const mine = (fixes || []).find((f) => f.fix_sid === sid);
  if (mine) renderHealBanner(mine); else { healBanner.hidden = true; healBanner.replaceChildren(); }
}

// The fix chat replays a heal_context event on open; react to it, and keep the banner honest as
// the fix is confirmed. A light interval covers plain session switches without patching app.js.
window.addEventListener('aigent:event', (event) => {
  const kind = event.detail && event.detail.event && event.detail.event.kind;
  if (['heal_context', 'heal_confirmed', 'heal_started', 'turn_completed'].includes(kind)) {
    refreshHealBanner().catch(() => {});
  }
});
setInterval(() => { refreshHealBanner().catch(() => {}); }, 5000);
