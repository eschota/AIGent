'use strict';
const $ = (id) => document.getElementById(id);
const fmt = (n) => n == null ? '—' : Number(n).toLocaleString('ru-RU');
const money = (n) => n == null ? '—' : '$' + Number(n).toFixed(6);
let current = null, source = null, allSessions = [], setupToken = '', refreshTimer, seen = new Set(), streamNodes = new Map(), readSaved = 0;
const el = (tag, text, cls) => {const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n;};
$('usage-panel').prepend(document.querySelector('.metrics'));
$('pending-actions').append($('approvals'));
$('usage-panel').append(document.querySelector('.inspector'));
document.querySelector('.composer-footer').append($('stop-button'));
$('stop-button').hidden=true;
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $('toast').hidden = true, 6000); }
async function api(path, options = {}) {
  const headers = {'X-Requested-With': 'DeepSeekIDE', ...(options.headers || {})};
  if (options.body && !(options.body instanceof FormData)) {headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(options.body);}
  const response = await fetch(path, {...options, headers});
  const data = await response.json().catch(() => ({detail: response.statusText}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
  return data;
}
function handle(fn) {return async (e) => {if (e?.preventDefault) e.preventDefault(); try {await fn(e);} catch (error) {toast(error.message);}};}
function setText(id, value) {$(id).textContent = value;}
function richText(target, text) {
  // DOM-only Markdown subset: never inject model-authored HTML.
  function inline(parent, value) {
    const pattern = /(`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\))/g;
    let cursor=0, match;
    while ((match=pattern.exec(value))) {
      parent.append(document.createTextNode(value.slice(cursor,match.index)));
      if(match[2])parent.append(el('code',match[2]));
      else if(match[3])parent.append(el('strong',match[3]));
      else {const link=el('a',match[4]);link.href=match[5];link.target='_blank';link.rel='noreferrer noopener';parent.append(link);}
      cursor=pattern.lastIndex;
    }
    parent.append(document.createTextNode(value.slice(cursor)));
  }
  let paragraph=[], list=null, code=null;
  function flush(){if(paragraph.length){const p=el('p');inline(p,paragraph.join('\n'));target.append(p);paragraph=[];}list=null;}
  for(const line of text.split('\n')) {
    if(line.startsWith('```')) {flush();if(code){target.append(el('pre',code.join('\n'),'code-block'));code=null;}else code=[];continue;}
    if(code){code.push(line);continue;}
    if(!line.trim()){flush();continue;}
    const item=line.match(/^\s*[-*] (.*)$/);
    if(item){if(paragraph.length)flush();if(!list){list=el('ul');target.append(list);}const li=el('li');inline(li,item[1]);list.append(li);continue;}
    const heading=line.match(/^#{1,4} (.*)$/);
    if(heading){flush();const h=el('h3');inline(h,heading[1]);target.append(h);continue;}
    if(list)list=null;paragraph.push(line);
  }
  flush();if(code)target.append(el('pre',code.join('\n'),'code-block'));
}
async function refresh() {
  const [status, sessions, approvals] = await Promise.all([api('/api/status'), api('/api/sessions'), api('/api/approvals')]);
  allSessions = sessions;
  setText('bot-status', status.bot === 'online' ? 'Telegram подключён' : 'Telegram · ' + status.bot);
  $('bot-status').classList.toggle('error', status.bot === 'error');
  $('bot-status').title = status.error || '';
  setText('model-label', status.model); setText('session-count', sessions.length);
  const u = status.usage;
  setText('total-tokens', fmt(u.prompt_tokens + u.completion_tokens));
  setText('request-count', fmt(u.requests) + ' запросов');
  setText('cache-percent', u.cache_hit_percent.toFixed(1) + '%');
  setText('cache-tokens', fmt(u.cache_hit_tokens) + ' токенов' + (u.unknown_cache_requests ? ' · данные неполные' : ''));
  setText('total-cost', money(u.cost_usd) + (u.unpriced_requests ? ' + ?' : ''));
  setText('total-saved', 'Экономия ' + money(u.saved_usd));
  if (status.username) {
    $('telegram-link').href = 'https://t.me/' + status.username;
    $('group-link').href = 'https://t.me/' + status.username + '?startgroup=aigent&admin=manage_topics';
  }
  $('sessions').replaceChildren(...sessions.map(s => {
    const button = el('button', undefined, 'session-item' + (current?.id === s.id ? ' selected' : ''));
    button.append(el('span', s.title), el('small', `${s.chat_id ? 'Telegram' : 'Web / API'} · ${s.status}`));
    button.onclick = handle(() => selectSession(s)); return button;
  }));
  const selected = sessions.find(s => s.id === current?.id);
  if (selected) {current = selected; updateSessionStats(selected.usage);}
  const busy = selected && ['running','approval'].includes(selected.status);
  $('stop-button').hidden=!busy; document.querySelector('.send-button').hidden=!!busy;
  renderApprovals(approvals.filter(a => a.sid === current?.id));
}
function updateSessionStats(u) {
  setText('session-tokens', fmt(u.prompt_tokens) + ' / ' + fmt(u.completion_tokens));
  setText('session-cache', fmt(u.cache_hit_tokens) + ' · ' + u.cache_hit_percent.toFixed(1) + '%');
  setText('session-cost', money(u.cost_usd) + (u.unpriced_requests ? ' + ?' : ''));
  setText('session-saved', money(u.saved_usd));
  setText('session-info', `${current.id}\n${current.chat_id ? 'Telegram topic ' + current.topic_id : 'Локальная сессия'} · ${current.status}`);
}
async function selectSession(session) {
  if (source) source.close(); current = session; seen = new Set(); streamNodes = new Map(); readSaved = 0;
  if (window.innerWidth<=700) document.body.classList.remove('sidebar-collapsed');
  $('events').replaceChildren(); $('empty').hidden = true; setText('session-title', session.title); setText('read-saved', '0 символов');
  let cursor = 0;
  while (true) {
    const events = await api(`/api/sessions/${session.id}/events?after=${cursor}`);
    events.forEach(renderEvent); if (events.length) cursor = events.at(-1).id; if (events.length < 500) break;
  }
  source = new EventSource(`/api/sessions/${session.id}/stream?after=${cursor}`);
  source.onmessage = (event) => renderEvent(JSON.parse(event.data));
  source.onerror = () => { /* EventSource reconnects using Last-Event-ID. */ };
  await refresh(); await Promise.all([loadFiles(), loadUsage()]);
  $('chat-panel').scrollTop = $('chat-panel').scrollHeight;
}
function renderEvent(event) {
  if (seen.has(event.id)) return; seen.add(event.id);
  const p = event.payload, kind = event.kind;
  if (kind === 'read_cache') {readSaved += p.avoided_chars; setText('read-saved', fmt(readSaved) + ' символов');}
  if (['approval', 'approval_closed', 'decision', 'read_cache'].includes(kind)) return;
  const nearBottom = $('chat-panel').scrollHeight - $('chat-panel').scrollTop - $('chat-panel').clientHeight < 160;
  let node;
  if (kind === 'stream') {
    node = streamNodes.get(p.id);
    if (!node) {
      node = el('article', undefined, 'event stream');
      const details = el('details'); details.open = true; details.append(el('summary', 'Размышления DeepSeek'), el('pre', ''));
      node.append(details, el('pre', '', 'answer')); streamNodes.set(p.id, node); $('events').append(node);
    }
    const details = node.querySelector('details'); details.hidden = !p.reasoning; details.querySelector('pre').textContent = p.reasoning || '';
    node.querySelector('.answer').textContent = p.done ? '' : p.text || '';
    if (p.done) {details.querySelector('summary').textContent = 'Размышления DeepSeek · завершено'; details.open = false;}
  } else {
    node = el('article', undefined, 'event ' + kind);
    const names = {user:'ВЫ', assistant:'DEEPSEEK', tool:'ДЕЙСТВИЕ', tool_result:'РЕЗУЛЬТАТ', media:'ВЛОЖЕНИЕ', error:'ОШИБКА', context:'КОНТЕКСТ', notice:'СОБЫТИЕ', usage:'USAGE'};
    const label = el('div', names[kind] || kind.toUpperCase(), 'event-label'); label.append(el('time', new Date(event.created * 1000).toLocaleTimeString())); node.append(label);
    if (kind === 'usage') {node.append(el('span', `Input ${fmt(p.prompt_tokens)} · cache read ${fmt(p.cache_hit_tokens)} · miss ${fmt(p.cache_miss_tokens)} · output ${fmt(p.completion_tokens)} · ≈ ${money(p.cost_usd)} · saved ≈ ${money(p.saved_usd)}`));}
    else if (['tool', 'tool_result', 'telegram_payload'].includes(kind)) {
      const details = el('details'); details.append(el('summary', p.name || kind), el('pre', JSON.stringify(p.arguments || p.result || p, null, 2))); node.append(details);
    } else if (kind === 'media' && p.path) {
      const a = el('a', p.path); a.href = `/api/sessions/${current.id}/file?path=${encodeURIComponent(p.path)}`; node.append(a, el('small', ' · ' + p.direction));
    } else if (kind === 'assistant') {const body=el('div',undefined,'rich-message');richText(body,p.text||'');node.append(body);}
    else node.append(el('pre', p.text || JSON.stringify(p, null, 2)));
    $('events').append(node);
  }
  if (nearBottom) $('chat-panel').scrollTop = $('chat-panel').scrollHeight;
}
function renderApprovals(items) {
  $('pending-actions').hidden=!items.length;
  if (!items.length) {$('approvals').replaceChildren(); $('approvals').dataset.signature=''; return;}
  const signature = JSON.stringify(items.map(i => i.id));
  if ($('approvals').dataset.signature === signature) return;
  $('approvals').dataset.signature = signature;
  $('approvals').replaceChildren(...items.map(a => {
    const card = el('div', undefined, 'approval-card'); card.append(el('h3', a.name), el('pre', a.detail));
    for (const accepted of [true, false]) {const b = el('button', accepted ? 'Применить' : 'Отклонить', accepted ? 'primary' : ''); b.onclick = handle(async () => {await api('/api/approvals/' + a.id, {method:'POST', body:{accepted}}); $('approvals').dataset.signature = ''; await refresh();}); card.append(b);}
    return card;
  }));
}
async function loadFiles() {
  if (!current) return;
  const files = await api(`/api/sessions/${current.id}/files`);
  $('files-list').replaceChildren(...files.map(f => {const row = el('div', undefined, 'file-row'); const link = el('a', f.path); link.href = `/api/sessions/${current.id}/file?path=${encodeURIComponent(f.path)}`; row.append(link, el('small', fmt(f.size) + ' B')); return row;}));
  if (!files.length) $('files-list').append(el('p', 'В этой сессии пока нет файлов.', 'muted'));
}
function svgNode(tag, attrs, text) {const n = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const [k,v] of Object.entries(attrs)) n.setAttribute(k, v); if (text != null) n.textContent = text; return n;}
async function loadUsage() {
  if (!current) return;
  const data = await api(`/api/sessions/${current.id}/usage`); updateSessionStats(data.totals); setText('usage-session-id', current.id);
  const points = data.points.slice(-80), chartWidth=Math.max(300,$('chart').clientWidth), svg = svgNode('svg', {viewBox:`0 0 ${chartWidth} 220`, role:'img', 'aria-label':'Токены по запросам: cache hit, cache miss и output'});
  const max = Math.max(1, ...points.map(p => p.prompt_tokens + p.completion_tokens));
  for (let i=0;i<4;i++) {const y=180-i*50;svg.append(svgNode('line',{x1:55,x2:chartWidth-5,y1:y,y2:y,stroke:'#353535'}),svgNode('text',{x:0,y:y+4},fmt(Math.round(max*i/3))));}
  const width=(chartWidth-65)/Math.max(1,points.length);
  points.forEach((p,i) => {let y=180; const x=60+i*width; for (const [value,color] of [[p.cache_hit_tokens||0,'#b6d3bd'],[p.cache_miss_tokens??p.prompt_tokens,'#ababd2'],[p.completion_tokens,'#ceb9a9']]) {const h=value/max*150; y-=h; const rect=svgNode('rect',{x,y,width:Math.max(2,width*.65),height:h,rx:2,fill:color});rect.append(svgNode('title',{},`#${p.id}: ${fmt(value)} tokens`));svg.append(rect);} if(points.length<16||i%10===0)svg.append(svgNode('text',{x,y:207},String(i+1)));});
  if (!points.length) svg.append(svgNode('text',{x:60,y:95},'Нет запросов'));
  $('chart').replaceChildren(svg);
  $('usage-table').replaceChildren(...data.points.slice(-100).reverse().map(p => {const row=el('tr'); [new Date(p.created*1000).toLocaleTimeString(),p.model,fmt(p.cache_hit_tokens),fmt(p.cache_miss_tokens),fmt(p.completion_tokens),money(p.cost_usd)].forEach(x=>row.append(el('td',x)));return row;}));
}
async function refreshBalance() {setText('balance','…');try {const data = await api('/api/balance'); setText('balance',data.balance_infos.map(b => `${b.currency === 'USD' ? '$' : b.currency + ' '}${b.total_balance}`).join(' / ') || '—');} catch(e) {setText('balance','Недоступен'); throw e;}}
async function showSettings() {const data=await api('/api/settings'); const f=$('settings-form'); for(const [k,v] of Object.entries(data)) {const field=f.elements.namedItem(k);if(field){if(field.type==='checkbox')field.checked=!!v;else field.value=v;}} f.elements.deepseek_key.placeholder=data.deepseek_configured?'Ключ сохранён · пусто = сохранить':'sk-…';f.elements.telegram_token.placeholder=data.telegram_configured?'Токен сохранён · пусто = сохранить':'123456:…';$('settings-dialog').showModal();}
async function enter() {$('login').hidden=true;$('workspace').hidden=false;await refresh();if(allSessions.length&&!current) await selectSession(allSessions[0]);clearInterval(refreshTimer);refreshTimer=setInterval(()=>{refresh().catch(()=>{});if(current&&!$('usage-panel').hidden)loadUsage().catch(()=>{});},4000);refreshBalance().catch(()=>{});const settings=await api('/api/settings');setText('thinking-label',settings.thinking?'Thinking включён':'Thinking выключен');}
async function createSession() {const s=await api(current?.chat_id ? '/api/sessions/'+current.id+'/topics' : '/api/sessions',{method:'POST',body:{title:'Сессия '+new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'})}});await selectSession(s);return s;}
$('login-form').onsubmit=handle(async()=>{await api('/api/login',{method:'POST',body:{password:$('login-password').value}});$('login-password').value='';await enter();});
$('new-session').onclick=handle(createSession);
$('settings-button').onclick=handle(showSettings);
$('header-settings').onclick=handle(showSettings);
$('composer-model').onclick=handle(showSettings);
$('toggle-sidebar').onclick=()=>document.body.classList.toggle('sidebar-collapsed');
$('attach-button').onclick=()=>document.querySelector('[data-tab="files"]').click();
$('close-settings').onclick=()=>{if(!setupToken)$('settings-dialog').close();};
$('settings-dialog').addEventListener('cancel',e=>{if(setupToken)e.preventDefault();});
$('settings-form').onsubmit=handle(async()=>{const f=$('settings-form'),body={};for(const field of f.elements){if(!field.name)continue;body[field.name]=field.type==='checkbox'?field.checked:field.type==='number'?Number(field.value):field.value;}const adminPassword=body.admin_password;
  await api(setupToken?'/api/setup':'/api/settings',{method:'POST',body,headers:setupToken?{Authorization:'Bearer '+setupToken}:{}});
  if(setupToken||adminPassword)await api('/api/login',{method:'POST',body:{password:adminPassword}});
  setupToken='';history.replaceState(null,'',location.pathname);$('settings-dialog').close();for(const field of f.elements)if(field.type==='password')field.value='';await enter();toast('Настройки сохранены');});
$('composer').onsubmit=handle(async()=>{const text=$('message').value.trim();if(!text)return;if(!current)await createSession();await api(`/api/sessions/${current.id}/messages`,{method:'POST',body:{text}});$('message').value='';await refresh();});
$('message').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('composer').requestSubmit();}};
$('stop-button').onclick=handle(async()=>{if(current){await api(`/api/sessions/${current.id}/stop`,{method:'POST'});toast('Запрошена остановка');}});
$('refresh-balance').onclick=handle(refreshBalance);$('refresh-files').onclick=handle(loadFiles);
$('upload-form').onsubmit=handle(async()=>{if(!current)await createSession();const file=$('upload-file').files[0];if(!file)throw new Error('Выберите файл');const body=new FormData();body.set('file',file);body.set('kind',$('media-kind').value);body.set('caption',$('media-caption').value);body.set('send_telegram',$('send-telegram').checked);body.set('ask_agent',$('ask-agent').checked);const result=await api(`/api/sessions/${current.id}/files`,{method:'POST',body});toast(result.telegram_delivered?'Файл доставлен в Telegram':'Файл сохранён');$('upload-file').value='';await loadFiles();});
$('rotate-token').onclick=handle(async()=>{const data=await api('/api/connector-token',{method:'POST'});setText('connector-token',data.token);toast('Новый ключ создан. Предыдущий ключ отозван.');});
$('logout-button').onclick=handle(async()=>{await api('/api/logout',{method:'POST'});location.reload();});
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=handle(async()=>{document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('active',x===b));for(const name of ['chat','files','usage'])$(name+'-panel').hidden=b.dataset.tab!==name;if(b.dataset.tab==='usage')await loadUsage();if(b.dataset.tab==='files')await loadFiles();}));
setText('api-url',location.origin+'/v1');
(async()=>{const status=await api('/api/bootstrap');if(status.setup_required){setupToken=new URLSearchParams(location.hash.slice(1)).get('setup')||'';$('login').hidden=false;if(setupToken){$('settings-title').textContent='Первый запуск AIGent';$('settings-dialog').showModal();$('rotate-token').hidden=true;}}else{history.replaceState(null,'',location.pathname);try{await enter();}catch{$('workspace').hidden=true;$('login').hidden=false;}}})().catch(e=>toast(e.message));
if(document.modelContext?.registerTool){const lifecycle=new AbortController();window.addEventListener('pagehide',()=>lifecycle.abort(),{once:true});Promise.resolve(document.modelContext.registerTool({name:'aigent_read_session_usage',title:'Read AIGent session usage',description:'Read real token and cache accounting for an existing session. Requires admin login.',inputSchema:{type:'object',properties:{session_id:{type:'string'}},required:['session_id'],additionalProperties:false},annotations:{readOnlyHint:true,untrustedContentHint:false},async execute(input){if(typeof input.session_id!=='string'||!/^[a-f0-9]{16}$/.test(input.session_id))throw new Error('Invalid session id');return api('/api/sessions/'+input.session_id+'/usage');}},{signal:lifecycle.signal})).catch(()=>{});}
