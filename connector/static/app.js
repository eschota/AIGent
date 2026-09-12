'use strict';
const $ = (id) => document.getElementById(id);
const fmt = (n) => n == null ? '—' : Number(n).toLocaleString('ru-RU');
const money = (n) => n == null ? '—' : '$' + Number(n).toFixed(6);
let current = null, source = null, allSessions = [], setupToken = '', refreshTimer, seen = new Set(), turnView = null, readSaved = 0, sessionSignature='';
const el = (tag, text, cls) => {const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n;};
$('usage-panel').prepend(document.querySelector('.metrics'));
$('pending-actions').append($('approvals'));
$('usage-panel').append(document.querySelector('.inspector'));
document.querySelector('.composer-footer').append($('stop-button'));
$('stop-button').hidden=true;
let attachments=[], projectIndex={}, uiRevision='', uiReloadOffered=false, lastRollback='';
let chatSort='date';try{chatSort=localStorage.getItem('aigent.chatSort')||'date';}catch{}
const attachStrip=el('div',undefined,'composer-attachments');attachStrip.id='composer-attachments';attachStrip.hidden=true;$('message').before(attachStrip);
const queueStrip=el('div',undefined,'queued-messages');queueStrip.id='queued-messages';queueStrip.hidden=true;$('composer').before(queueStrip);
const goalBanner=el('div',undefined,'goal-banner');goalBanner.id='goal-banner';goalBanner.hidden=true;$('composer').before(goalBanner);
const activityBar=el('div',undefined,'activity-bar');activityBar.id='activity-bar';activityBar.hidden=true;$('composer').before(activityBar);
let activity={since:0,label:'',detail:'',tasks:new Map()},activityTimer=null;
const elapsed=(from)=>{const s=Math.max(0,Math.round((Date.now()-from)/1000));return s<60?s+' с':Math.floor(s/60)+' мин '+String(s%60).padStart(2,'0')+' с';};
function renderActivity(){
  const running=activity.since>0;
  const tasks=[...activity.tasks.values()];
  activityBar.hidden=!running&&!tasks.length;
  if(activityBar.hidden)return;
  activityBar.replaceChildren();
  if(running){
    const row=el('div',undefined,'activity-row');
    row.append(el('span',undefined,'activity-spinner'),
               el('span',(activity.icon||'⚙️')+' '+shorten(activity.label||'Работает',26),'activity-label'));
    if(activity.detail)row.append(el('span',shorten(activity.detail,28),'activity-detail'));
    row.append(el('span',elapsed(activity.since),'activity-time'));
    row.title=(activity.label||'')+(activity.detail?' · '+activity.detail:'');
    activityBar.append(row);
  }
  for(const task of tasks){
    const row=el('div',undefined,'activity-row background');
    row.append(el('span',undefined,'activity-spinner farm'),
               el('span',(task.icon||'🎬')+' '+shorten(task.label,24),'activity-label'));
    if(task.detail)row.append(el('span',shorten(task.detail,16),'activity-detail'));
    row.append(el('span',elapsed(task.since),'activity-time'));
    activityBar.append(row);
  }
}
function startActivity(label,detail,icon){
  if(!activity.since)activity.since=Date.now();
  if(label)activity.label=label;
  if(detail!==undefined)activity.detail=detail;
  if(icon)activity.icon=icon;
  renderActivity();
  clearInterval(activityTimer);activityTimer=setInterval(renderActivity,1000);
}
function stopActivity(){
  activity.since=0;activity.label='';activity.detail='';activity.icon='';
  renderActivity();
  if(!activity.tasks.size){clearInterval(activityTimer);activityTimer=null;}
}
function trackTask(id,label,detail,done){
  if(done)activity.tasks.delete(id);
  else if(activity.tasks.has(id)){const item=activity.tasks.get(id);item.label=label||item.label;item.detail=detail??item.detail;}
  else activity.tasks.set(id,{since:Date.now(),label,detail,icon:String(label).includes('видео')?'🎞️':'🎨'});
  renderActivity();
  if(activity.tasks.size&&!activityTimer)activityTimer=setInterval(renderActivity,1000);
}
const questionStrip=el('div',undefined,'background-questions');questionStrip.id='background-questions';questionStrip.hidden=true;$('composer').before(questionStrip);
document.body.insertAdjacentHTML('beforeend','<dialog id="media-viewer"><div class="media-viewer-head"><span id="media-name"></span><span class="media-viewer-actions"><button type="button" id="media-copy">Копировать</button><a id="media-download">Скачать</a><button type="button" id="media-close">✕</button></span></div><div id="media-stage"></div></dialog>');
$('media-close').onclick=()=>$('media-viewer').close();
$('media-viewer').addEventListener('click',event=>{if(event.target===$('media-viewer'))$('media-viewer').close();});
const autoToggle=el('label',undefined,'auto-approve-toggle checkbox');autoToggle.title='Автоматически применять все правки и команды терминала этой сессии без подтверждения';
const autoInput=el('input');autoInput.type='checkbox';autoInput.id='auto-approve';autoToggle.append(autoInput,el('span','Авто'));
document.querySelector('.composer-footer').prepend(autoToggle);
const contextMeter=el('button',undefined,'context-meter');contextMeter.id='context-meter';contextMeter.type='button';contextMeter.hidden=true;
const contextBar=el('span',undefined,'context-bar'),contextFill=el('i'),contextValue=el('span','—','context-value');
contextBar.append(contextFill);contextMeter.append(contextBar,contextValue);
$('composer-model').after(contextMeter);
contextMeter.onclick=handle(()=>showSettings());
const compact=(n)=>n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':String(n);
function renderContext(data){
  if(!data){contextMeter.hidden=true;return;}
  contextMeter.hidden=false;
  const percent=Math.min(100,Math.max(0,data.percent));
  contextFill.style.width=percent+'%';
  contextValue.textContent=Math.round(percent)+'%';
  contextMeter.classList.toggle('warn',percent>=60&&percent<85);
  contextMeter.classList.toggle('full',percent>=85);
  const cache=data.cache||{};
  contextValue.textContent=Math.round(percent)+'%'+(cache.percent!=null?' · кеш '+Math.round(cache.percent)+'%':'');
  contextMeter.title=`Контекст ${compact(data.chars)} / ${compact(data.limit)} символов (${percent}%)`
    +`\n${data.messages} сообщений${data.images?' · '+data.images+' с изображениями':''}`
    +'\n'+(cache.known?`Кеш последнего запроса: ${fmt(cache.hit_tokens)} из ${fmt(cache.prompt_tokens)} токенов (${cache.percent}%)`
                     :'Кеш последнего запроса: данных от API нет')
    +'\nПри переполнении самые старые ходы исключаются из контекста; история чата сохраняется.';
}
async function loadContext(){
  if(!current){renderContext(null);return;}
  try{renderContext(await api(`/api/sessions/${current.id}/context`));}catch{}
}
// The goal is rendered by the goal banner (renderGoal, below); loadGoal restores it after a reload.
async function loadGoal(){
  if(!current){renderGoal(null);return;}
  try{renderGoal(await api(`/api/sessions/${current.id}/goal`));}catch{}
}
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $('toast').hidden = true, 6000); }
let authRecovery=null;
async function recoverAuthentication(){
  if(authRecovery)return authRecovery;
  authRecovery=(async()=>{
    if(window.aigentDesktop?.reauthenticate){await window.aigentDesktop.reauthenticate();return;}
    await new Promise((resolve,reject)=>{
      const dialog=el('dialog');dialog.innerHTML='<form><h2>Восстановить вход</h2><p>Черновик сохранён. Войдите повторно, и отправка продолжится.</p><label>Пароль администратора<input type="password" autocomplete="current-password" required></label><p class="auth-error"></p><button class="primary">Войти и продолжить</button></form>';
      dialog.querySelector('form').onsubmit=async e=>{e.preventDefault();try{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'DeepSeekIDE'},body:JSON.stringify({password:dialog.querySelector('input').value})});if(!r.ok)throw new Error('Не удалось войти. Проверьте пароль.');dialog.remove();resolve();}catch(error){dialog.querySelector('.auth-error').textContent=error.message;}};
      dialog.addEventListener('cancel',()=>{dialog.remove();reject(new Error('Отправка отменена; черновик сохранён'));},{once:true});document.body.append(dialog);dialog.showModal();
    });
  })().finally(()=>{authRecovery=null;});
  return authRecovery;
}
function dropAttachment(item){
  if(item.preview&&URL.revokeObjectURL)URL.revokeObjectURL(item.preview);
  attachments=attachments.filter(x=>x!==item);renderAttachments();
}
function renderAttachments(){
  $('composer-attachments').hidden=!attachments.length;
  $('composer-attachments').replaceChildren(...attachments.map(item=>{
    const chip=el('div',undefined,'attachment-chip'+(item.preview?' visual':''));
    if(item.preview){
      const img=el('img');img.alt=item.name;img.decoding='async';
      // Local object URL: the preview appears immediately and never depends on a server round trip.
      img.src=item.preview;
      img.onerror=()=>{img.src=window.MediaUI?window.MediaUI.url(item.sid,item.path,'thumb'):`/api/sessions/${item.sid}/image?path=${encodeURIComponent(item.path)}`;};
      img.title='Открыть во весь экран';
      img.onclick=()=>window.MediaUI?.open(item.sid,item.path);
      chip.append(img);
    }
    const meta=el('div',undefined,'attachment-meta');
    meta.append(el('span',item.name),el('small',(item.bytes>1048576?(item.bytes/1048576).toFixed(1)+' MB':Math.max(1,Math.round(item.bytes/1024))+' KB')));
    chip.append(meta);
    const remove=el('button','✕');remove.type='button';remove.title='Открепить от сообщения';
    remove.onclick=()=>dropAttachment(item);
    chip.append(remove);return chip;
  }));
}
async function uploadAttachment(file){
  if(!current)await createSession();
  const body=new FormData();
  body.set('file',file,file.name||('clipboard-'+Date.now()+(file.type==='image/png'?'.png':'')));
  body.set('kind',/^image\//.test(file.type)?'photo':'document');
  body.set('caption','');body.set('send_telegram','false');body.set('ask_agent','false');
  const data=await api(`/api/sessions/${current.id}/files`,{method:'POST',body});
  attachments.push({path:data.path,type:file.type||'application/octet-stream',bytes:data.bytes,sid:current.id,
                    name:(file.name||data.path).replace(/^[0-9a-f]{8}-/,'').slice(0,40),
                    preview:/^image\//.test(file.type)&&typeof URL.createObjectURL==='function'?URL.createObjectURL(file):''});
  renderAttachments();return data;
}
async function acceptFiles(files){
  const list=[...files].filter(Boolean);
  if(!list.length)return false;
  for(const file of list){
    if(file.size>50*1024*1024){toast('Файл больше 50 MB: '+file.name);continue;}
    toast('Загрузка вложения: '+(file.name||'изображение')+'…');
    try{const data=await uploadAttachment(file);toast('Вложение добавлено: '+data.path.replace(/^[0-9a-f]{8}-/,'')+' · отправится со следующим сообщением');}
    catch(error){toast('Не удалось прикрепить: '+error.message);}
  }
  return true;
}
function clipboardFiles(event){
  const data=event.clipboardData;
  if(!data)return [];
  const direct=data.files?[...data.files]:[];
  const items=[...(data.items||[])].filter(item=>item.kind==='file').map(item=>item.getAsFile());
  return [...direct,...items].filter(Boolean).filter((file,index,all)=>all.findIndex(x=>x.name===file.name&&x.size===file.size)===index);
}
function pasteTarget(event){
  // Accept a clipboard image from the composer or anywhere in the chat, but never from the code editor.
  const node=event.target;
  if(node?.closest?.('.developer-dock, dialog'))return false;
  return $('workspace') && !$('workspace').hidden;
}
async function pasteHandler(event){
  if(!pasteTarget(event))return;
  const files=clipboardFiles(event);
  if(!files.length)return;
  event.preventDefault();
  await acceptFiles(files);
  $('message').focus();
}
$('message').addEventListener('paste',pasteHandler);
document.addEventListener('paste',event=>{if(event.target!==$('message'))pasteHandler(event);});
const filePicker=el('input');filePicker.type='file';filePicker.multiple=true;filePicker.hidden=true;filePicker.id='composer-file-picker';
document.body.append(filePicker);
filePicker.onchange=handle(async()=>{const files=[...filePicker.files];filePicker.value='';await acceptFiles(files);});
for(const name of ['dragover','drop'])$('composer').addEventListener(name,event=>{
  if(!event.dataTransfer?.types?.includes('Files'))return;
  event.preventDefault();
  $('composer').classList.toggle('dropping',name==='dragover');
  if(name==='drop')acceptFiles(event.dataTransfer.files);
});
$('composer').addEventListener('dragleave',()=>$('composer').classList.remove('dropping'));
async function loadQueue(){
  if(!current){$('queued-messages').hidden=true;return;}
  let items=[];
  try{items=await api(`/api/sessions/${current.id}/queue`);}catch{return;}
  $('queued-messages').hidden=!items.length;
  $('queued-messages').replaceChildren(...items.map((item,index)=>{
    const row=el('div',undefined,'queued-item');
    row.append(el('span','В очереди '+(index+1),'queued-index'),el('pre',(item.text||'').slice(0,400)));
    const cancel=el('button','Отменить');cancel.type='button';
    cancel.onclick=handle(async()=>{await api(`/api/sessions/${current.id}/queue?item=${item.id}`,{method:'DELETE'});await loadQueue();});
    row.append(cancel);return row;
  }));
}
async function loadProjects(){
  try{const list=await api('/api/projects');projectIndex=Object.fromEntries(list.map(p=>[p.id,p]));}catch{}
}
function saveDraft(){try{const key='aigent.draft.'+(current?.id||'new');if($('message').value)localStorage.setItem(key,$('message').value);else localStorage.removeItem(key);}catch{}}
$('message').addEventListener('input',saveDraft);
async function submitMessage(){
  const pending=attachments.filter(item=>item.sid===current?.id);
  const text=$('message').value.trim()||(pending.length?'Вложение из буфера обмена. Посмотри его и продолжи задачу.':'');
  if(!text||!current)return;
  const sid=current.id,original=$('message').value;saveDraft();
  const busy=['running','approval'].includes(current.status);
  const method=busy&&current.provider==='codex'?'steer':'messages';
  let request_id=crypto.randomUUID();
  try{const previous=JSON.parse(localStorage.getItem('aigent.pending.'+sid)||'null');if(previous?.text===text)request_id=previous.id;localStorage.setItem('aigent.pending.'+sid,JSON.stringify({id:request_id,text}));}catch{}
  const result=await api(`/api/sessions/${sid}/${method}`,{method:'POST',body:{text,request_id,attachments:pending.map(item=>item.path)}});
  for(const item of pending)if(item.preview&&URL.revokeObjectURL)URL.revokeObjectURL(item.preview);
  attachments=attachments.filter(item=>item.sid!==sid);renderAttachments();
  if(result?.queued)toast(`Агент занят. Сообщение №${result.position} в очереди — текущий ход не прерван.`);
  try{localStorage.removeItem('aigent.pending.'+sid);if(current?.id!==sid||$('message').value===original)localStorage.removeItem('aigent.draft.'+sid);}catch{}
  if(current?.id===sid&&$('message').value===original)$('message').value='';
  await refresh();
}
async function api(path, options = {}) {
  const headers = {'X-Requested-With': 'DeepSeekIDE', ...(options.headers || {})};
  if (options.body && !(options.body instanceof FormData)) {headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(options.body);}
  const request={...options,headers};
  let response = await fetch(path, request);
  if(response.status===401&&!['/api/login','/api/logout'].includes(path)&&!$('workspace').hidden){saveDraft();await recoverAuthentication();response=await fetch(path,request);}
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
function chatCost(u){
  if(!u||u.cost_usd==null)return '—';
  const c=Number(u.cost_usd);
  if(!(c>0))return '$0';
  if(c<0.01)return '<$0.01';
  return '$'+c.toFixed(2);
}
function chatCostTitle(u){
  if(!u)return '';
  return `${fmt(u.requests)} запросов · вход ${fmt(u.prompt_tokens)} · выход ${fmt(u.completion_tokens)} · кеш ${fmt(u.cache_hit_tokens)}`
    +(u.cost_usd!=null?` · ${money(u.cost_usd)}`:'');
}
function sortSessions(list){
  const arr=[...list];
  const pin=(a,b)=>(b.pinned?1:0)-(a.pinned?1:0);
  if(chatSort==='cost')arr.sort((a,b)=>pin(a,b)||(Number(b.usage?.cost_usd)||0)-(Number(a.usage?.cost_usd)||0));
  else arr.sort((a,b)=>pin(a,b)||(Number(b.created)||0)-(Number(a.created)||0));
  return arr;
}
function renderSessions(){
  const ordered=sortSessions(allSessions);
  const nextSignature=JSON.stringify([current?.id,chatSort,ordered.map(s=>[s.id,s.title,s.status,s.pinned?1:0,s.usage?.cost_usd])]);
  if(nextSignature===sessionSignature)return;
  sessionSignature=nextSignature;
  $('sessions').replaceChildren(...ordered.map(s => {
    const button = el('button', undefined, 'session-item' + (current?.id === s.id ? ' selected' : ''));
    button.dataset.sessionId=s.id;
    const head=el('div',undefined,'session-item-head');
    head.append(el('span', s.title, 'session-title-text'));
    const cost=el('span', chatCost(s.usage), 'session-cost-badge');cost.title=chatCostTitle(s.usage);
    head.append(cost);
    button.append(head, el('small', `${s.chat_id ? 'Telegram' : 'Web / API'} · ${s.status}`));
    button.onclick = handle(() => selectSession(s)); return button;
  }));
}
function renderTotalSpend(u){
  const node=$('total-spend');
  if(!node)return;
  node.textContent='Σ '+(u.cost_usd==null?'—':'$'+Number(u.cost_usd).toFixed(2));
  const names={deepseek:'DeepSeek',codex:'Codex',claude:'Claude'},byProvider={};
  for(const s of allSessions){const p=s.provider||'deepseek';byProvider[p]=(byProvider[p]||0)+(Number(s.usage?.cost_usd)||0);}
  const parts=Object.entries(byProvider).filter(([,v])=>v>0).map(([k,v])=>`${names[k]||k}: $${v.toFixed(2)}`);
  node.title='Суммарный расход всех сессий'+(parts.length?'\n'+parts.join('\n'):'');
}
async function refresh() {
  const [status, sessions, approvals] = await Promise.all([api('/api/status'), api('/api/sessions'), api('/api/approvals')]);
  allSessions = sessions;
  setText('bot-status', status.bot === 'online' ? 'Telegram подключён' : 'Telegram · ' + status.bot);
  $('bot-status').classList.toggle('error', status.bot === 'error');
  $('bot-status').title = status.error || '';
  setText('model-label', status.model); setText('session-count', sessions.length);
  if(status.supervisor?.state==='rolled_back'&&status.supervisor.restored!==lastRollback){
    lastRollback=status.supervisor.restored;
    toast('Сервер откатился на последнюю рабочую версию кода ('+lastRollback+') после неудачной правки.');
  }
  if(!uiRevision)uiRevision=status.ui_revision||'';
  else if(status.ui_revision&&status.ui_revision!==uiRevision&&!uiReloadOffered){
    uiReloadOffered=true;
    toast('Интерфейс обновлён на сервере. Перезагрузите страницу, чтобы получить новые возможности.');
    const button=el('button','Перезагрузить');button.onclick=()=>location.reload();$('toast').append(button);
    clearTimeout(toast.timer);
  }
  const u = status.usage;
  setText('total-tokens', fmt(u.prompt_tokens + u.completion_tokens));
  setText('request-count', fmt(u.requests) + ' запросов');
  setText('cache-percent', u.cache_hit_percent.toFixed(1) + '%');
  setText('cache-tokens', fmt(u.cache_hit_tokens) + ' токенов' + (u.unknown_cache_requests ? ' · данные неполные' : ''));
  setText('total-cost', money(u.cost_usd) + (u.unpriced_requests ? ' + ?' : ''));
  setText('total-saved', 'Экономия ' + money(u.saved_usd));
  renderTotalSpend(u);
  const tg=$('telegram-link');
  if (status.username && tg) tg.href = 'https://t.me/' + status.username;
  renderSessions();
  const selected = sessions.find(s => s.id === current?.id);
  if (selected) {current = selected; updateSessionStats(selected.usage);}
  const busy = selected && ['running','approval'].includes(selected.status);
  const send=document.querySelector('.send-button');
  $('stop-button').hidden=!busy; send.hidden=false; send.classList.toggle('queueing',!!busy);
  send.title=busy?'Агент занят — сообщение встанет в очередь и не прервёт ход':'Отправить';
  $('message').placeholder=busy?'Сообщение уйдёт в очередь и будет обработано после текущего ответа':'Поручите что угодно';
  $('auto-approve').checked=!!selected?.auto_approve;
  autoToggle.classList.toggle('on',!!selected?.auto_approve);
  await loadQueue();
  await loadContext();
  await loadGoal();
  await loadQuestions();
  renderApprovals(approvals.filter(a => a.sid === current?.id));
}
function updateSessionStats(u) {
  setText('session-tokens', fmt(u.prompt_tokens) + ' / ' + fmt(u.completion_tokens));
  setText('session-cache', fmt(u.cache_hit_tokens) + ' · ' + u.cache_hit_percent.toFixed(1) + '%');
  setText('session-cost', money(u.cost_usd) + (u.unpriced_requests ? ' + ?' : ''));
  setText('session-saved', money(u.saved_usd));
  const project=current.project_id?projectIndex[current.project_id]:null;
  setText('session-info', `${current.id}\n${current.chat_id ? 'Telegram topic ' + current.topic_id : 'Локальная сессия'} · ${current.status}`
    + `\nПроект: ${project?project.name+' · '+project.path:(current.workspace||'отдельная папка сессии')}`);
  const chip=$('session-project');
  if(chip){chip.textContent='▱ '+(project?project.name:'Без проекта');chip.title=project?project.path:'Выбрать проект для этой сессии';chip.classList.toggle('none',!project);}
}
async function selectSession(session) {
  if(current||$('message').value)saveDraft();
  for(const item of attachments)if(item.preview&&URL.revokeObjectURL)URL.revokeObjectURL(item.preview);
  attachments=[];renderAttachments();
  renderGoal(null);$('background-questions').replaceChildren();$('background-questions').hidden=true;
  loadQuestions().catch(()=>{});
  if (source) source.close(); current = session; seen = new Set(); turnView = null; readSaved = 0;
  try{$('message').value=localStorage.getItem('aigent.draft.'+session.id)||'';localStorage.setItem('aigent.selectedSession',session.id);}catch{}
  if (window.innerWidth<=700) document.body.classList.remove('sidebar-collapsed');
  $('events').replaceChildren(); $('empty').hidden = true; setText('session-title', session.title); setText('read-saved', '0 символов');
  window.SubAgents?.reset(session.id);
  renderGoal(null);
  let cursor = 0;
  while (true) {
    const events = await api(`/api/sessions/${session.id}/events?after=${cursor}`);
    events.forEach(renderEvent); if (events.length) cursor = events.at(-1).id; if (events.length < 500) break;
  }
  source = new EventSource(`/api/sessions/${session.id}/stream?after=${cursor}`);
  source.onmessage = (event) => renderEvent(JSON.parse(event.data));
  source.onerror = () => { /* EventSource reconnects using Last-Event-ID. */ };
  await refresh(); await Promise.all([loadFiles(), loadUsage()]);
  if(current.status==='idle')finishTurn();
  if(['running','approval'].includes(current.status))startActivity('Агент работает','');else stopActivity();
  $('chat-panel').scrollTop = $('chat-panel').scrollHeight;
}
function ensureTurn() {
  if(turnView)return turnView;
  const root=el('section',undefined,'agent-turn');
  const reasoning=el('details',undefined,'turn-reasoning');reasoning.hidden=true;
  reasoning.append(el('summary','Размышления'),el('pre',''));reasoning.open=true;
  const work=el('details',undefined,'turn-work');work.hidden=true;
  const summary=el('summary','Работа с проектом'),actions=el('div',undefined,'turn-actions');work.append(summary,actions);
  const answer=el('div',undefined,'turn-messages'),usage=el('div',undefined,'turn-usage');usage.hidden=true;
  root.append(reasoning,work,answer,usage);$('events').append(root);
  turnView={root,reasoning,work,summary,actions,answer,usage,streams:new Map(),tools:[],metrics:[],done:false};
  return turnView;
}
function updateWork(t) {
  const failed=t.tools.filter(x=>x.failed).length,pending=t.tools.filter(x=>!x.done).length;
  t.summary.textContent=`${t.done?'Выполнено':'Работа с проектом'} · ${fmt(t.tools.length)} действий`+(pending&&!t.done?` · ${pending} в работе`:'')+(failed?` · ошибок: ${failed}`:'');
  t.summary.classList.toggle('has-errors',!!failed);
}
function finishTurn() {
  if(!turnView||turnView.done)return;
  turnView.done=true;turnView.reasoning.open=false;
  turnView.reasoning.querySelector('summary').textContent='Размышления · завершено';
  for(const stream of turnView.streams.values())stream.preview.remove();
  updateWork(turnView);
}
function renderTurnUsage(t,p) {
  t.metrics.push(p);t.usage.hidden=false;
  const sum=key=>t.metrics.reduce((n,m)=>n+(Number(m[key])||0),0);
  const known=key=>t.metrics.every(m=>m[key]!=null);
  const input=sum('prompt_tokens'),hit=sum('cache_hit_tokens');
  const cache=known('cache_hit_tokens')?`${fmt(hit)}${input?' ('+(100*hit/input).toFixed(0)+'%)':''}`:'—';
  const subscription=t.metrics.every(m=>m.billing==='subscription');
  t.usage.textContent=(t.metrics.every(m=>m.inherited)?'До форка · ':'')+`${fmt(t.metrics.length)} запросов · Вход ${fmt(input)} · Выход ${fmt(sum('completion_tokens'))} · Кеш ${cache}`+(subscription?' · Подписка':` · ≈ ${known('cost_usd')?money(sum('cost_usd')):'—'} · Сэкономлено ≈ ${known('saved_usd')?money(sum('saved_usd')):'—'}`);
  t.usage.title='Итого за этот ход. Подробные данные по запросам — во вкладке «Токены».';
}
const toolNames={list_files:'Список файлов',search_files:'Поиск',read_file:'Чтение файла',write_file:'Запись файла',apply_patch:'Изменение файлов',exec_command:'Команда',run_command:'Команда',write_stdin:'Вывод команды',commandExecution:'Команда',fileChange:'Изменение файлов'};
function renderTool(t,p,isResult) {
  const id=p.call_id||p.arguments?.id||p.result?.id;
  let item=id?t.tools.find(x=>x.id===id):null;
  if(!item&&isResult&&!id)item=t.tools.find(x=>!x.done&&x.name===p.name);
  if(!item){const row=el('details',undefined,'turn-tool'),label=el('summary'),body=el('div');row.append(label,body);t.actions.append(row);item={id,name:p.name,row,label,body,done:false};t.tools.push(item);}
  if(!isResult){item.args=p.arguments||{};lazyStructured(item.row,item.body,item.args);}
  else {item.done=true;item.failed=!!(p.result?.error||p.result?.is_error||p.result?.status==='failed'||(p.result?.exit_code!=null&&p.result.exit_code!==0));
        const output=p.result&&typeof p.result==='object'&&typeof p.result.output==='string'?p.result.output:null;
        const body=el('div');item.body.append(body);
        lazyStructured(item.row,body,p.result);
        if(output)item.body.append(el('pre',output.slice(0,8000),'code-block'));}
  const a=item.args||{},target=a.path||a.pattern||a.query||a.command||a.cmd||'';
  item.label.textContent=(item.done?(item.failed?'! ':'✓ '):'◌ ')+(toolNames[item.name]||item.name)+(target?' · '+String(target).slice(0,150):'');
  item.label.classList.toggle('has-errors',!!item.failed);t.work.hidden=false;updateWork(t);
}
const FARM_STAGES={pending:['⏳','в очереди'],rendering:['🎬','рендер'],done:['✅','готово'],
  ready:['✅','готово'],error:['⛔','ошибка'],failed:['⛔','ошибка'],discarded:['⛔','отменено']};
function farmNotice(text,payload){
  // "Ферма · видео · задача <id> · кадр <path>" and "Ферма · video · rendering" become one chip.
  if(!/Ферма/.test(text||''))return null;
  const id=payload.task_id||(text.match(/задача ([\w-]+)/)||[])[1];
  if(!id)return null;
  const stage=(text.match(/·\s*(pending|rendering|done|ready|error|failed|discarded)\s*$/i)||[])[1];
  const frame=(text.match(/кадр (\S+)/)||[])[1];
  const kind=/видео|video/.test(text)?'video':'image';
  return {id,stage:stage?stage.toLowerCase():'',frame,kind};
}
function renderFarmChip(turn,info){
  let chip=turn.farm?.get(info.id);
  if(!chip){
    const row=el('div',undefined,'farm-chip');
    const thumb=el('span',undefined,'farm-thumb');
    const icon=el('span',info.kind==='video'?'🎞️':'🎨','farm-icon');
    const stage=el('span','⏳ в очереди','farm-stage');
    const time=el('span','','farm-time');
    row.append(thumb,icon,stage,time);
    row.title='Задача фермы '+info.id;
    turn.actions.append(row);turn.work.hidden=false;
    chip={row,thumb,icon,stage,time,since:Date.now()};
    (turn.farm=turn.farm||new Map()).set(info.id,chip);
    chip.timer=setInterval(()=>{chip.time.textContent=elapsed(chip.since);},1000);
    chip.time.textContent=elapsed(chip.since);
  }
  if(info.frame&&!chip.thumb.firstChild&&current){
    const image=el('img');image.alt='';image.src=mediaUrl(current.id,info.frame);
    image.onerror=()=>image.remove();
    chip.thumb.append(image);
  }
  if(info.stage){
    const [mark,label]=FARM_STAGES[info.stage]||['•',info.stage];
    chip.stage.textContent=mark+' '+label;
    chip.row.classList.toggle('active',['pending','rendering'].includes(info.stage));
    if(['done','ready','error','failed','discarded'].includes(info.stage)){
      clearInterval(chip.timer);chip.row.classList.add('finished');
    }
  }
  return chip;
}
function renderEvent(event) {
  if (seen.has(event.id)) return; seen.add(event.id);
  const p = event.payload, kind = event.kind;
  // Re-broadcast every rendered event so separate modules (the 3D dock) can react without patching here.
  try{window.dispatchEvent(new CustomEvent('aigent:event',{detail:{event,sid:current?.id||''}}));}catch{}
  if (kind === 'read_cache') {readSaved += p.avoided_chars; setText('read-saved', fmt(readSaved) + ' символов');}
  const TOOL_ICONS={read_file:'📖',list_files:'📂',search_files:'🔎',write_file:'✏️',apply_patch:'✏️',
    exec_command:'⌨️',run_command:'⌨️',shared_image:'🎨',shared_video:'🎞️',shared_skill:'📎',view_image:'🖼️',
    set_goal:'🎯',ask_user_async:'❓',update_plan:'🗒️'};
  if (kind === 'user') startActivity('Обдумывает задачу','','🧠');
  if (kind === 'tool') startActivity(toolNames[p.name]||p.name||'Инструмент',
      String(p.arguments?.path||p.arguments?.cmd||p.arguments?.query||p.arguments?.prompt||''), TOOL_ICONS[p.name]||'⚙️');
  if (kind === 'tool_result') startActivity('Работает','','⚙️');
  if (kind === 'stream') startActivity(activity.label||'Пишет ответ','','✍️');
  if (kind === 'approval') startActivity('Ждёт подтверждения',String(p.name||''),'⏸️');
  if (kind === 'turn_completed') stopActivity();
  if (kind === 'notice' && /Ферма · (видео|изображение) · задача ([\w-]+)/.test(p.text||'')) {
    const found=(p.text||'').match(/задача ([\w-]+)/);
    if(found)trackTask(found[1],'Ферма · '+((p.text||'').includes('видео')?'видео':'изображение'),'отправлено',false);
  }
  if (kind === 'notice' && /Ферма · (video|image) · (\w+)/.test(p.text||'')) {
    const stage=(p.text||'').match(/Ферма · \w+ · (\w+)/);
    for(const [id,task] of activity.tasks){task.detail=stage?stage[1]:task.detail;if(stage&&['done','error'].includes(stage[1]))activity.tasks.delete(id);}
    renderActivity();
  }
  if (kind === 'media' && (p.direction==='generated')) {activity.tasks.clear();renderActivity();}
  if (kind === 'goal') {renderGoal(p); if(p.status==='done')setTimeout(()=>{if($('goal-banner').classList.contains('status-done'))$('goal-banner').hidden=true;},15000);}
  // Ask the server which questions are still open: a replayed event must not reopen a closed one.
  if (kind === 'background_question') loadQuestions();
  if (kind === 'background_answered') closeBackgroundQuestion(p.id);
  if (kind === 'goal') return;
  if (['approval', 'approval_closed', 'decision', 'read_cache'].includes(kind)) return;
  const nearBottom = $('chat-panel').scrollHeight - $('chat-panel').scrollTop - $('chat-panel').clientHeight < 160;
  if(kind==='user'){finishTurn();turnView=null;const node=el('article',undefined,'event user');node.append(el('pre',p.text||''));$('events').append(node);}
  else if(kind==='turn_completed')finishTurn();
  else {
  const t=ensureTurn();let node;
  if(kind==='notice'){
    const info=farmNotice(p.text,p);
    if(info){renderFarmChip(t,info);if(nearBottom)$('chat-panel').scrollTop=$('chat-panel').scrollHeight;return;}
  }
  if (kind === 'stream') {
    let stream=t.streams.get(p.id);
    if(!stream){stream={reasoning:'',preview:el('pre','', 'live-answer')};t.streams.set(p.id,stream);t.answer.append(stream.preview);}
    if(p.reasoning)stream.reasoning=p.reasoning;
    const reasoning=[...t.streams.values()].map(x=>x.reasoning).filter(Boolean).join('\n\n');
    t.reasoning.hidden=!reasoning;t.reasoning.querySelector('pre').textContent=reasoning;
    stream.preview.textContent=p.done?'':p.text||'';
  } else if(kind==='usage')renderTurnUsage(t,p);
  else if(kind==='tool'||kind==='tool_result')renderTool(t,p,kind==='tool_result');
  else if(['context','provider_session','telegram_payload'].includes(kind)){
    const detail=el('details',undefined,'turn-detail');const summary=el('summary',p.text||'Данные подключения');detail.append(summary);
    const body=el('div');detail.append(body);lazyStructured(detail,body,p);t.actions.append(detail);t.work.hidden=false;
  } else if(kind==='media'&&p.path&&(window.MediaUI?.isViewable?.(p.path)??window.MediaUI?.isMedia(p.path))){
    // Interactive card: server thumbnail, lightbox and inline playback; several in a row share a grid.
    window.MediaUI.grid(t.answer).append(window.MediaUI.render(current.id,p.path,
      {kind:p.kind,direction:p.direction,caption:p.prompt||p.caption||''}));
  } else {
    node = el('article', undefined, 'event ' + kind);
    const names = {user:'ВЫ', assistant:(p.provider||current?.provider||'deepseek').toUpperCase(), tool:'ДЕЙСТВИЕ', tool_result:'РЕЗУЛЬТАТ', media:'🖼 ВЛОЖЕНИЕ', error:'⛔ ОШИБКА', context:'📋 КОНТЕКСТ', notice:'•', usage:'USAGE'};
    const label = el('div', names[kind] || kind.toUpperCase(), 'event-label'); label.append(el('time', new Date(event.created * 1000).toLocaleTimeString())); node.append(label);
    if (kind === 'media' && p.path) {
      node.append(mediaCard(p, current.id));
    } else if (kind === 'assistant') {const body=el('div',undefined,'rich-message');richText(body,p.text||'');node.append(body);}
    else if(p.text!==undefined)node.append(el('pre',p.text));
    else structured(node,p);
    t.answer.append(node);
  }
  }
  if (nearBottom) $('chat-panel').scrollTop = $('chat-panel').scrollHeight;
}
const mediaUrl=(sid,path)=>`/api/sessions/${sid}/media?path=${encodeURIComponent(path)}`;
const isImagePath=(path)=>/\.(png|jpe?g|webp|gif)$/i.test(path);
const isVideoPath=(path)=>/\.(mp4|webm)$/i.test(path);
async function pngBlob(sid,path){
  const response=await fetch(mediaUrl(sid,path));
  if(!response.ok)throw new Error('Файл недоступен для копирования');
  const blob=await response.blob();
  if(blob.type==='image/png')return blob;
  const bitmap=await createImageBitmap(blob);
  const canvas=document.createElement('canvas');canvas.width=bitmap.width;canvas.height=bitmap.height;
  canvas.getContext('2d').drawImage(bitmap,0,0);
  return new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
}
async function copyMedia(sid,path){
  // The desktop shell copies through the OS clipboard; the browser path needs a granted permission.
  const blob=await pngBlob(sid,path);
  if(window.aigentDesktop?.copyImage){
    const reader=new FileReader();
    const dataUrl=await new Promise((resolve,reject)=>{reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error('Не удалось прочитать изображение'));reader.readAsDataURL(blob);});
    await window.aigentDesktop.copyImage(dataUrl);
    return 'image';
  }
  if(!navigator.clipboard?.write)throw new Error('Браузер не разрешает запись в буфер обмена');
  try{await navigator.clipboard.write([new ClipboardItem({'image/png':blob})]);return 'image';}
  catch(error){
    if(/focus/i.test(error.message))throw new Error('Кликните в окно приложения и повторите копирование');
    throw new Error('Копирование не разрешено: '+error.message);
  }
}
async function copyFileToClipboard(sid,path){
  // Not an image: put the real file on the clipboard when the desktop shell is available.
  if(window.aigentDesktop?.copyFile){
    const info=await api(`/api/sessions/${sid}/file-path`,{method:'POST',body:{path}});
    await window.aigentDesktop.copyFile(info.absolute,info.name);
    return 'file';
  }
  const url=new URL(`/api/sessions/${sid}/file?path=${encodeURIComponent(path)}`,location.origin).href;
  if(!navigator.clipboard?.writeText)throw new Error('Буфер обмена недоступен');
  await navigator.clipboard.writeText(url);
  return 'link';
}
function openViewer(sid,path){
  const viewer=$('media-viewer');
  const stage=$('media-stage');
  stage.replaceChildren();
  if(isVideoPath(path)){
    const video=el('video');video.src=mediaUrl(sid,path);video.controls=true;video.autoplay=true;video.loop=true;stage.append(video);
  }else{
    const image=el('img');image.src=mediaUrl(sid,path);image.alt=path;stage.append(image);
  }
  setText('media-name',path);
  setText('media-copy',isVideoPath(path)?'Копировать файл':'Копировать');
  $('media-copy').onclick=handle(async()=>{
    if(isVideoPath(path)){const kind=await copyFileToClipboard(sid,path);toast(kind==='file'?'Файл скопирован':'Ссылка скопирована');return;}
    await copyMedia(sid,path);toast('Изображение скопировано в буфер обмена');});
  $('media-download').href=`/api/sessions/${sid}/file?path=${encodeURIComponent(path)}`;
  $('media-download').download=path.split('/').pop();
  if(viewer.showModal)viewer.showModal();else viewer.open=true;
}
function mediaCard(p,sid){
  const card=el('figure',undefined,'media-card');
  const path=p.path;
  if(isImagePath(path)||isVideoPath(path)){
    const frame=el('button',undefined,'media-frame');frame.type='button';
    frame.title='Открыть в просмотрщике';
    if(isVideoPath(path)){
      const video=el('video');video.src=mediaUrl(sid,path);video.controls=true;video.preload='metadata';
      frame.onclick=(event)=>{if(event.target!==video)openViewer(sid,path);};
      frame.append(video);
    }else{
      const image=el('img');image.alt=path;image.decoding='async';image.src=mediaUrl(sid,path);
      image.onerror=()=>{frame.replaceChildren(el('span','Предпросмотр недоступен: файл повреждён или не изображение','media-broken'));};
      frame.onclick=()=>openViewer(sid,path);
      frame.append(image);
    }
    card.append(frame);
  }
  const caption=el('figcaption');
  caption.append(el('span',path.split('/').pop()),el('small',' · '+(p.direction||'')+(p.billing==='free-farm'?' · ферма':'')));
  card.append(caption);
  const actions=el('div',undefined,'media-actions');
  if(isImagePath(path)||isVideoPath(path)){
    const open=el('button','Открыть');open.type='button';open.onclick=()=>openViewer(sid,path);
    actions.append(open);
  }
  if(isImagePath(path)){
    const copy=el('button','Копировать');copy.type='button';
    copy.onclick=handle(async()=>{await copyMedia(sid,path);toast('Изображение скопировано в буфер обмена');});
    const reuse=el('button','В сообщение');reuse.type='button';reuse.title='Прикрепить это изображение к следующему сообщению';
    reuse.onclick=handle(async()=>{
      const response=await fetch(mediaUrl(sid,path));
      if(!response.ok)throw new Error('Файл недоступен');
      const blob=await response.blob();
      await acceptFiles([new File([blob],path.split('/').pop(),{type:blob.type||'image/png'})]);
    });
    actions.append(copy,reuse);
  }
  if(!isImagePath(path)){
    const copyFile=el('button','Копировать файл');copyFile.type='button';
    copyFile.onclick=handle(async()=>{
      const kind=await copyFileToClipboard(sid,path);
      toast(kind==='file'?'Файл скопирован в буфер обмена':'Ссылка на файл скопирована');
    });
    actions.append(copyFile);
  }
  const download=el('a','Скачать');download.href=`/api/sessions/${sid}/file?path=${encodeURIComponent(path)}`;
  download.download=path.split('/').pop();actions.append(download);
  card.append(actions);
  return card;
}
const GOAL_ICONS={analyze:'🔍',code:'💻',fix:'🛠️',generate:'🎨',verify:'✅',deploy:'🚀',wait:'⏳'};
const STATUS_ICONS={active:'',done:'✔',blocked:'⛔'};
const shorten=(text,limit)=>{text=String(text||'').replace(/\s+/g,' ').trim();return text.length>limit?text.slice(0,limit-1)+'…':text;};
function renderGoal(goal){
  const banner=$('goal-banner');
  if(!goal||!goal.goal){banner.hidden=true;return;}
  banner.hidden=false;
  banner.className='goal-banner kind-'+(goal.kind||'analyze')+' status-'+(goal.status||'active');
  banner.replaceChildren(el('span',GOAL_ICONS[goal.kind]||'🎯','goal-mark'),
                         el('span',shorten(goal.goal,46),'goal-text'));
  if(STATUS_ICONS[goal.status])banner.append(el('span',STATUS_ICONS[goal.status],'goal-status'));
  const steps=goal.steps||[],done=steps.filter(s=>s.status==='completed').length;
  if(steps.length)banner.append(el('span',`${done}/${steps.length}`,'goal-count'));
  banner.title=`Цель (${goal.kind||'—'}, ${goal.status||'active'}): ${goal.goal}`
    +(steps.length?`\nПлан: ${steps.map(s=>(s.status==='completed'?'✓ ':s.status==='in_progress'?'◌ ':'· ')+(s.text||'')).join('\n')}`:'')
    +(goal.auto_continue?'\nПока цель активна, ход продолжается автоматически.':'');
}
// The server owns a background question: it is asked once, retired when it gets stale, and a
// closed one never comes back — reloading the page cannot resurrect it from the event log.
async function loadQuestions(){
  const strip=$('background-questions');
  if(!current){strip.replaceChildren();strip.hidden=true;return;}
  let open=[];
  try{open=await api(`/api/sessions/${current.id}/async-questions`);}catch{return;}
  const alive=new Set(open.map(item=>String(item.id)));
  for(const card of [...strip.children])if(!alive.has(card.dataset.questionId))card.remove();
  for(const item of open)showBackgroundQuestion(item);
  strip.hidden=!strip.children.length;
}
function closeBackgroundQuestion(id){
  const strip=$('background-questions');
  const card=[...strip.children].find(node=>node.dataset.questionId===String(id||''));
  if(card)card.remove();
  strip.hidden=!strip.children.length;
}
function showBackgroundQuestion(p){
  const strip=$('background-questions'), id=String(p.id||'');
  if(!id||[...strip.children].some(node=>node.dataset.questionId===id))return;
  const card=el('form',undefined,'background-question');
  card.dataset.questionId=id;
  const head=el('div',undefined,'question-head');
  head.append(el('span','❓','question-icon'),el('strong',shorten(p.question,150)));
  card.append(head);
  if(p.assumption)card.append(el('small','▸ допущение: '+shorten(p.assumption,120)));
  const row=el('div',undefined,'question-row');
  const input=el('input');input.placeholder='Ответ агенту…';input.required=true;
  const send=el('button','Ответить','primary');
  const close=el('button','Закрыть');close.type='button';close.title='Закрыть вопрос без ответа';
  close.onclick=handle(async()=>{
    await api(`/api/sessions/${current.id}/async-questions/${id}`,{method:'POST',body:{answer:''}});
    closeBackgroundQuestion(id);
  });
  row.append(input,send,close);card.append(row);
  card.title=p.question||'';
  card.onsubmit=handle(async(event)=>{
    event.preventDefault();
    const result=await api(`/api/sessions/${current.id}/async-questions/${id}`,{method:'POST',body:{answer:input.value}});
    closeBackgroundQuestion(id);
    toast(result.queued?'Ответ поставлен в очередь агенту':'Ответ отправлен агенту');
    await refresh();
  });
  strip.append(card);strip.hidden=false;
}
function renderJson(target,value,depth=0){
  // Model output is rendered as DOM nodes, never as markup: structure is visible, nothing is executed.
  const type=value===null?'null':Array.isArray(value)?'array':typeof value;
  if(type==='array'||type==='object'){
    const entries=type==='array'?value.map((v,i)=>[i,v]):Object.entries(value);
    if(!entries.length){target.append(el('span',type==='array'?'[]':'{}','json-punct'));return;}
    target.append(el('span',type==='array'?'[':'{','json-punct'));
    const body=el('div',undefined,'json-body');
    for(const [key,item] of entries){
      const line=el('div',undefined,'json-line');
      if(type!=='array')line.append(el('span','"'+key+'"','json-key'),el('span',': ','json-punct'));
      renderJson(line,item,depth+1);
      body.append(line);
    }
    target.append(body,el('span',type==='array'?']':'}','json-punct'));
    return;
  }
  const text=type==='string'?'"'+value+'"':String(value);
  target.append(el('span',text,'json-'+type));
}
function lazyStructured(details,target,value){
  // A long history holds thousands of tool results; render each one when its row is opened.
  let rendered=false;
  const draw=()=>{if(rendered||!details.open)return;rendered=true;structured(target,value);};
  details.addEventListener('toggle',draw);
  if(details.open)draw();
}
function structured(target,value){
  let data=value;
  if(typeof value==='string'){
    const trimmed=value.trim();
    if(!(trimmed.startsWith('{')||trimmed.startsWith('['))){target.append(el('pre',value));return false;}
    try{data=JSON.parse(trimmed);}catch{target.append(el('pre',value));return false;}
  }
  if(data===null||typeof data!=='object'){target.append(el('pre',String(data)));return false;}
  const box=el('div',undefined,'json-view');
  renderJson(box,data);
  target.append(box);
  return true;
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
  $('files-list').replaceChildren(...files.map(f => {const row = el('div', undefined, 'file-row'); const link = el('a', f.path); link.href = `/api/sessions/${current.id}/file?path=${encodeURIComponent(f.path)}`;
    // Images, video and audio open in the viewer instead of downloading.
    if(window.MediaUI?.isViewable?.(f.path)??window.MediaUI?.isMedia(f.path)){link.title='Открыть просмотр';link.onclick=e=>{e.preventDefault();window.MediaUI.open(current.id,f.path);};}
    row.append(link, el('small', fmt(f.size) + ' B')); return row;}));
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
async function enter() {$('login').hidden=true;$('workspace').hidden=false;await loadProjects();await refresh();if(allSessions.length&&!current){let saved;try{saved=localStorage.getItem('aigent.selectedSession');}catch{}await selectSession(allSessions.find(s=>s.id===saved)||allSessions[0]);}clearInterval(refreshTimer);refreshTimer=setInterval(()=>{refresh().catch(()=>{});if(current&&!$('usage-panel').hidden)loadUsage().catch(()=>{});},4000);refreshBalance().catch(()=>{});const settings=await api('/api/settings');setText('thinking-label',settings.thinking?'Thinking включён':'Thinking выключен');}
async function createSession() {const s=await api(current?.chat_id ? '/api/sessions/'+current.id+'/topics' : '/api/sessions',{method:'POST',body:{title:'Сессия '+new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'})}});await selectSession(s);return s;}
$('login-form').onsubmit=handle(async()=>{await api('/api/login',{method:'POST',body:{password:$('login-password').value}});$('login-password').value='';await enter();});
$('new-session').onclick=handle(createSession);
$('settings-button').onclick=handle(showSettings);
$('header-settings').onclick=handle(showSettings);
$('composer-model').onclick=handle(showSettings);
$('toggle-sidebar').onclick=()=>document.body.classList.toggle('sidebar-collapsed');
document.querySelectorAll('#chat-sort button').forEach(b=>{
  b.classList.toggle('active',b.dataset.sort===chatSort);
  b.onclick=()=>{chatSort=b.dataset.sort;try{localStorage.setItem('aigent.chatSort',chatSort);}catch{}
    document.querySelectorAll('#chat-sort button').forEach(x=>x.classList.toggle('active',x===b));
    sessionSignature='';renderSessions();};
});
{
  const toggle=$('providers-toggle'),popover=$('providers-popover');
  if(toggle&&popover){
    const place=()=>{const r=toggle.getBoundingClientRect();popover.style.left=Math.max(8,Math.min(r.left,innerWidth-266))+'px';popover.style.top=(r.bottom+6)+'px';};
    const close=()=>{popover.hidden=true;toggle.setAttribute('aria-expanded','false');};
    toggle.onclick=e=>{e.stopPropagation();const show=popover.hidden;popover.hidden=!show;toggle.setAttribute('aria-expanded',String(show));if(show)place();};
    document.addEventListener('pointerdown',e=>{if(!popover.hidden&&!popover.contains(e.target)&&!toggle.contains(e.target))close();});
    document.addEventListener('keydown',e=>{if(e.key==='Escape')close();});
  }
}
$('attach-button').onclick=()=>$('composer-file-picker').click();
$('close-settings').onclick=()=>{if(!setupToken)$('settings-dialog').close();};
$('settings-dialog').addEventListener('cancel',e=>{if(setupToken)e.preventDefault();});
$('settings-form').onsubmit=handle(async()=>{const f=$('settings-form'),body={};for(const field of f.elements){if(!field.name)continue;body[field.name]=field.type==='checkbox'?field.checked:field.type==='number'?Number(field.value):field.value;}const adminPassword=body.admin_password;
  await api(setupToken?'/api/setup':'/api/settings',{method:'POST',body,headers:setupToken?{Authorization:'Bearer '+setupToken}:{}});
  if(setupToken||adminPassword)await api('/api/login',{method:'POST',body:{password:adminPassword}});
  setupToken='';history.replaceState(null,'',location.pathname);$('settings-dialog').close();for(const field of f.elements)if(field.type==='password')field.value='';await enter();toast('Настройки сохранены');});
$('composer').onsubmit=handle(async()=>{if(!current)await createSession();await submitMessage();});
$('message').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('composer').requestSubmit();}};
$('auto-approve').onchange=handle(async()=>{
  if(!current){$('auto-approve').checked=false;throw new Error('Сначала выберите или создайте чат');}
  current=await api('/api/sessions/'+current.id,{method:'PATCH',body:{auto_approve:$('auto-approve').checked}});
  toast(current.auto_approve?'Автоприменение включено: команды и правки выполняются без подтверждения':'Подтверждения снова обязательны');
  await refresh();
});
$('session-project')?.addEventListener('click',()=>{if(typeof window.showSessionMenu==='function')window.showSessionMenu().catch(error=>toast(error.message));});
$('stop-button').onclick=handle(async()=>{if(current){await api(`/api/sessions/${current.id}/stop`,{method:'POST'});toast('Запрошена остановка');}});
$('refresh-balance').onclick=handle(refreshBalance);$('refresh-files').onclick=handle(loadFiles);
$('upload-form').onsubmit=handle(async()=>{if(!current)await createSession();const file=$('upload-file').files[0];if(!file)throw new Error('Выберите файл');const body=new FormData();body.set('file',file);body.set('kind',$('media-kind').value);body.set('caption',$('media-caption').value);body.set('send_telegram',$('send-telegram').checked);body.set('ask_agent',$('ask-agent').checked);const result=await api(`/api/sessions/${current.id}/files`,{method:'POST',body});toast(result.telegram_delivered?'Файл доставлен в Telegram':'Файл сохранён');$('upload-file').value='';await loadFiles();});
$('rotate-token').onclick=handle(async()=>{const data=await api('/api/connector-token',{method:'POST'});setText('connector-token',data.token);toast('Новый ключ создан. Предыдущий ключ отозван.');});
$('logout-button').onclick=handle(async()=>{await api('/api/logout',{method:'POST'});location.reload();});
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=handle(async()=>{document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('active',x===b));for(const name of ['chat','files','usage'])$(name+'-panel').hidden=b.dataset.tab!==name;if(b.dataset.tab==='usage')await loadUsage();if(b.dataset.tab==='files')await loadFiles();}));
setText('api-url',location.origin+'/v1');
// Self-heal UI (repair button on errors, fix-chat banner) loads as a separate same-origin module.
if(!document.getElementById('selfheal-ui-script')){const s=document.createElement('script');s.id='selfheal-ui-script';s.src='/static/selfheal-ui.js';s.defer=true;document.head.appendChild(s);}
(async()=>{const status=await api('/api/bootstrap');if(status.setup_required){setupToken=new URLSearchParams(location.hash.slice(1)).get('setup')||'';$('login').hidden=false;if(setupToken){$('settings-title').textContent='Первый запуск AIGent';$('settings-dialog').showModal();$('rotate-token').hidden=true;}}else{history.replaceState(null,'',location.pathname);try{await enter();}catch{$('workspace').hidden=true;$('login').hidden=false;}}})().catch(e=>toast(e.message));
if(document.modelContext?.registerTool){const lifecycle=new AbortController();window.addEventListener('pagehide',()=>lifecycle.abort(),{once:true});Promise.resolve(document.modelContext.registerTool({name:'aigent_read_session_usage',title:'Read AIGent session usage',description:'Read real token and cache accounting for an existing session. Requires admin login.',inputSchema:{type:'object',properties:{session_id:{type:'string'}},required:['session_id'],additionalProperties:false},annotations:{readOnlyHint:true,untrustedContentHint:false},async execute(input){if(typeof input.session_id!=='string'||!/^[a-f0-9]{16}$/.test(input.session_id))throw new Error('Invalid session id');return api('/api/sessions/'+input.session_id+'/usage');}},{signal:lifecycle.signal})).catch(()=>{});}
