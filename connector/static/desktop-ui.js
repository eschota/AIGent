'use strict';
let accountsData=[], projectsData=[], browsersData=[], selectedProject='', editorWidget=null, editorFile=null, editorDirty=false, terminalWidget=null, terminalId=null, terminalSession=null, activeLoginAccount=null;
const terminalHistory=new Map();

document.body.insertAdjacentHTML('beforeend', `
<dialog id="new-chat-dialog"><form id="new-chat-form"><div class="section-heading"><h2>Новый чат</h2><button type="button" data-close="new-chat-dialog">✕</button></div>
<label>Название<input id="new-chat-title" placeholder="Что будем делать?" maxlength="100"></label>
<label>Аккаунт агента<select id="new-chat-account"></select></label><div class="form-grid"><label>Модель<select id="new-chat-model"></select></label><label>Уровень рассуждений<select id="new-chat-effort"><option value="low">Низкий</option><option value="medium" selected>Средний</option><option value="high">Высокий</option><option value="xhigh">Очень высокий</option></select></label></div>
<label>Проект<select id="new-chat-project"><option value="">Отдельная рабочая папка</option></select></label><label class="checkbox"><input id="new-chat-telegram" type="checkbox"> Создать топик в текущем Telegram-чате</label><p id="new-chat-account-state" class="muted"></p><button class="primary">Создать чат</button></form></dialog>
<dialog id="accounts-dialog"><div class="section-heading"><h2>Аккаунты и лимиты</h2><button data-close="accounts-dialog">✕</button></div><p class="muted">У каждого подключения отдельная авторизация. Лимиты подписки и денежный баланс API учитываются раздельно.</p><button id="refresh-accounts">Обновить подключения</button><div id="account-rows"></div><details><summary>Добавить аккаунт</summary><form id="account-form"><div class="form-grid"><label>Провайдер<select id="account-provider"><option value="codex">OpenAI / Codex</option><option value="claude">Claude</option><option value="deepseek">DeepSeek API</option></select></label><label>Имя или email<input id="account-name" required maxlength="120"></label></div><label>Браузерный профиль<select id="account-browser"><option value="">Браузер по умолчанию</option></select></label><label id="account-key-label" hidden>API-ключ DeepSeek<input id="account-key" type="password" autocomplete="off"></label><button class="primary">Добавить подключение</button></form></details><p id="account-login-state" class="muted"></p></dialog>
<dialog id="native-history-dialog"><div class="section-heading"><h2>Чаты локального провайдера</h2><button data-close="native-history-dialog">✕</button></div><p class="muted">Импорт создаёт отдельную ветку, чтобы сохранить исходный чат.</p><div id="native-history-list"></div></dialog>
<dialog id="session-menu-dialog"><form id="session-menu-form"><div class="section-heading"><h2>Настройки чата</h2><button type="button" data-close="session-menu-dialog">✕</button></div><label>Название<input id="rename-title" required maxlength="100"></label><label>Проект<select id="session-project-select"><option value="">Отдельная рабочая папка</option></select></label><label class="checkbox"><input id="session-auto-approve" type="checkbox"> Автоприменение: выполнять команды терминала и правки агента без подтверждения</label><label class="checkbox"><input id="session-auto-continue" type="checkbox"> Не останавливаться: продолжать ходы, пока текущая цель не достигнута</label><p class="muted" id="session-workspace-note"></p><button class="primary">Сохранить настройки чата</button></form><div class="session-menu-actions"><button id="pin-chat">Закрепить / открепить</button><button id="fork-chat">Создать ветку</button><button id="archive-chat">Архивировать</button></div></dialog>
<dialog id="text-input-dialog"><form id="text-input-form"><div class="section-heading"><h2 id="text-input-title"></h2><button type="button" data-close="text-input-dialog">✕</button></div><label id="text-input-label">Значение<input id="text-input-value" required></label><button class="primary">Продолжить</button></form></dialog>`);
document.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>$(button.dataset.close).close());

const dock=el('aside',undefined,'developer-dock');dock.id='developer-dock';dock.hidden=true;
dock.innerHTML=`<div class="dock-header"><div class="tabs"><button data-dock="editor">Редактор</button><button data-dock="git">Git</button><button data-dock="terminal">Терминал</button></div><button id="close-dock" aria-label="Закрыть панель">✕</button></div>
<section id="editor-dock" class="dock-panel"><div class="editor-toolbar"><span id="editor-path">Выберите файл</span><button id="save-file" disabled>Сохранить</button></div><div class="editor-layout"><div id="file-tree"></div><div id="code-editor"><p class="muted">Откройте файл проекта или создайте новый.</p></div></div><button id="create-file">＋ Новый файл</button></section>
<section id="git-dock" class="dock-panel" hidden><div class="section-heading"><strong id="git-branch">Git</strong><button id="refresh-git">Обновить</button></div><div id="git-changes"></div><pre id="git-diff"></pre><form id="git-commit-form"><label>Сообщение коммита<input id="commit-message" required></label><button class="primary">Создать коммит</button></form></section>
<section id="terminal-dock" class="dock-panel" hidden><div id="terminal-output"></div><form id="terminal-form"><label class="sr-only" for="terminal-command">Команда терминала</label><input id="terminal-command" placeholder="Команда в папке проекта…"><button>Выполнить</button><button type="button" id="terminal-stop">■</button></form><p class="muted">Команды выполняются в рабочей папке выбранного чата.</p></section>`;
document.querySelector('.workgrid').append(dock);
const headerActions=el('div',undefined,'desktop-header-actions');
// «3D» показывает постоянную панель просмотра моделей (viewer3d.js), она остаётся загруженной.
for(const [text,action] of [['Файлы',()=>showDock('editor')],['Git',()=>showDock('git')],['Терминал',()=>showDock('terminal')],['3D',()=>{if(!window.Viewer3D)throw new Error('Просмотр 3D недоступен: модуль не загрузился');window.Viewer3D.show();}],['⋯',showSessionMenu]]){const b=el('button',text);b.onclick=handle(action);headerActions.append(b);}
document.querySelector('.topbar').insertBefore(headerActions,$('header-settings'));
const projectSection=el('div',undefined,'projects-section');
projectSection.innerHTML='<div class="sidebar-label">Проекты <button id="open-project" title="Открыть проект">＋</button></div><div id="project-list"></div>';
($('chats-label')||$('sessions').previousElementSibling).before(projectSection);
const accountsButton=el('button','Аккаунты и лимиты','accounts-button');accountsButton.id='accounts-button';$('settings-button').before(accountsButton);
const archiveButton=el('button','Архив чатов');archiveButton.id='show-archive';$('settings-button').before(archiveButton);
const trashButton=el('button','Корзина');trashButton.id='show-trash';$('settings-button').before(trashButton);
const chatContextMenu=el('div',undefined,'chat-context-menu');chatContextMenu.id='chat-context-menu';chatContextMenu.role='menu';chatContextMenu.hidden=true;document.body.append(chatContextMenu);
function openChatContext(event,button){
  event.preventDefault();const sid=button.dataset.sessionId;const chat=allSessions.find(s=>s.id===sid);if(!chat)return;
  chatContextMenu.replaceChildren(el('div',chat.title,'context-title'));
  for(const [title,action] of [['Форкнуть',async()=>{const fork=await api(`/api/sessions/${sid}/fork`,{method:'POST'});await selectSession(fork);}],['Удалить',()=>deleteChat(sid)]]){const item=el('button',title);item.role='menuitem';item.onclick=handle(async()=>{chatContextMenu.hidden=true;await action();});chatContextMenu.append(item);}
  chatContextMenu.hidden=false;const rect=button.getBoundingClientRect();const x=event.clientX||rect.left+20,y=event.clientY||rect.bottom;chatContextMenu.style.left=Math.max(8,Math.min(x,innerWidth-230))+'px';chatContextMenu.style.top=Math.max(8,Math.min(y,innerHeight-130))+'px';chatContextMenu.querySelector('button').focus();
}
$('sessions').addEventListener('contextmenu',event=>{const button=event.target.closest('.session-item');if(button)openChatContext(event,button);});
$('sessions').addEventListener('keydown',event=>{if(event.key==='ContextMenu'||(event.shiftKey&&event.key==='F10')){const button=event.target.closest('.session-item');if(button)openChatContext(event,button);}});
document.addEventListener('pointerdown',event=>{if(!chatContextMenu.contains(event.target))chatContextMenu.hidden=true;});
document.addEventListener('keydown',event=>{if(event.key==='Escape')chatContextMenu.hidden=true;});
async function deleteChat(sid){
  if(current?.id===sid&&editorDirty)throw new Error('Сначала сохраните изменения открытого файла.');
  await api('/api/sessions/'+sid,{method:'DELETE'});
  if(current?.id===sid){source?.close();current=null;$('events').replaceChildren();$('task-plan').replaceChildren();$('pending-actions').hidden=true;$('pending-questions').replaceChildren();setText('session-title','Новый чат');$('empty').hidden=false;dock.hidden=true;}
  await refresh();toast('Чат удалён в корзину. Файлы проекта сохранены.');
  const undo=el('button','Отменить');undo.onclick=handle(async()=>{const s=await api(`/api/sessions/${sid}/restore`,{method:'POST'});$('toast').hidden=true;await selectSession(s);});$('toast').append(undo);
}
const questionContainer=el('div');questionContainer.id='pending-questions';$('pending-actions').before(questionContainer);
const planContainer=el('div');planContainer.id='task-plan';$('composer').before(planContainer);

function inputDialog(title,initial=''){return new Promise(resolve=>{setText('text-input-title',title);$('text-input-value').value=initial;const d=$('text-input-dialog');const onClose=()=>{d.removeEventListener('close',onClose);resolve(null);};d.addEventListener('close',onClose);$('text-input-form').onsubmit=e=>{e.preventDefault();const value=$('text-input-value').value;d.removeEventListener('close',onClose);d.close();resolve(value);};d.showModal();$('text-input-value').focus();});}
function accountLabel(a){return `${a.provider==='codex'?'Codex':a.provider==='claude'?'Claude':'DeepSeek'} · ${a.name.replace(/^(Codex|Claude|Deepseek) · /,'')}`;}
async function refreshConnections(force=false){
  [accountsData,projectsData]=await Promise.all([api('/api/accounts'+(force?'?refresh=true':'')),api('/api/projects')]);
  if(window.aigentDesktop){try{browsersData=await window.aigentDesktop.browserProfiles();}catch{}}
  renderProjects();renderAccounts();
  const old=$('new-chat-account').value;
  $('new-chat-account').replaceChildren(...accountsData.map(a=>{const o=el('option',accountLabel(a)+(a.status.connected?'':' · нужен вход'));o.value=a.id;return o;}));
  if(accountsData.some(a=>a.id===old))$('new-chat-account').value=old;
  $('new-chat-project').replaceChildren(el('option','Отдельная рабочая папка'),...projectsData.map(p=>{const o=el('option',p.name);o.value=p.id;return o;}));$('new-chat-project').options[0].value='';
  $('account-browser').replaceChildren(el('option','Браузер по умолчанию'),...browsersData.map(b=>{const o=el('option',`${b.browser} · ${b.name}${b.email?' · '+b.email:''}`);o.value=b.id;return o;}));$('account-browser').options[0].value='';
  updateNewChatModels();
}
function renderProjects(){
  $('project-list').replaceChildren(...projectsData.map(p=>{const b=el('button','▱ '+p.name,selectedProject===p.id?'selected':'');b.title=p.path;b.onclick=()=>{selectedProject=p.id;renderProjects();showNewChat();};return b;}));
}
function renderAccounts(){
  $('account-rows').replaceChildren(...accountsData.map(a=>{
    const row=el('article',undefined,'account-row');row.append(el('strong',accountLabel(a)));
    const detail=el('p',a.status.connected?'Подключён':'Требуется вход','muted');
    const identity=a.status.account?.email||a.status.account?.emailAddress;
    if(identity)detail.textContent+=' · '+identity;
    if(a.status.error)detail.textContent+=' · '+a.status.error;row.append(detail);
    const limits=a.status.limits?.rateLimitsByLimitId||{};
    for(const [key,bucket] of Object.entries(limits)){for(const [windowName,window] of [['Основной',bucket.primary],['Дополнительный',bucket.secondary]]){if(!window)continue;const used=Math.max(0,Math.min(100,window.usedPercent));const wrap=el('div',undefined,'limit-row');const progress=el('progress');progress.max=100;progress.value=used;wrap.append(el('span',`${key} · ${windowName}: осталось ${(100-used).toFixed(0)}%`),progress,el('small','Сброс '+new Date(window.resetsAt*1000).toLocaleString()));row.append(wrap);}}

    if(a.provider==='claude')row.append(el('small','Расход токенов показывается по сессиям CLI. Лимиты подписки доступны в официальном Claude Code.'));
    row.append(el('small','Проверено '+new Date(a.status.checked_at*1000).toLocaleTimeString()));
    if(a.provider!=='deepseek'){
      const label=el('label','Браузерный профиль');const select=el('select');select.append(el('option','Браузер по умолчанию'));select.options[0].value='';
      for(const b of browsersData){const option=el('option',`${b.browser} · ${b.name}${b.email?' · '+b.email:''}`);option.value=b.id;select.append(option);}
      const matching=browsersData.find(b=>b.email&&b.email.toLowerCase()===a.name.toLowerCase());
      select.value=a.browser_profile||matching?.id||'';
      if(!a.browser_profile&&matching){a.browser_profile=matching.id;api('/api/accounts/'+a.id,{method:'PATCH',body:{browser_profile:matching.id}}).catch(()=>{});}
      select.onchange=handle(async()=>{a.browser_profile=select.value;await api('/api/accounts/'+a.id,{method:'PATCH',body:{browser_profile:select.value}});});label.append(select);row.append(label);
    }
    const actions=el('div',undefined,'account-actions');
    if(a.provider!=='deepseek'){if(a.provider==='codex'){const login=el('button',a.status.connected?'Проверить вход':'Войти через браузер');login.onclick=handle(()=>loginAccount(a));actions.append(login);}else{const link=el('a','Штатный вход в Claude CLI ↗');link.href='https://code.claude.com/docs/en/authentication';link.target='_blank';link.rel='noreferrer';actions.append(link);}const history=el('button','Локальные чаты');history.onclick=handle(()=>nativeHistory(a));actions.append(history);}
    const chat=el('button','Новый чат');chat.onclick=()=>{$('accounts-dialog').close();showNewChat(a.id);};actions.append(chat);row.append(actions);return row;
  }));
  const list=$('providers-list');
  if(list){
    let connectedTotal=0;
    list.replaceChildren(...[['deepseek','D','DeepSeek','API'],['codex','C','Codex',''],['claude','C','Claude','']].map(([kind,mark,name,tag])=>{
      const found=accountsData.filter(a=>a.provider===kind);
      const connected=found.filter(a=>a.status.connected).length;connectedTotal+=connected;
      const row=el('button',undefined,'provider'+(connected?'':' future'));row.type='button';
      row.append(el('span',mark,'provider-icon'));
      const body=el('div');body.append(el('span',name),el('small',`${connected} из ${found.length} подключены`));row.append(body);
      if(tag)row.append(el('span',tag,'tag'));
      row.onclick=()=>{$('providers-popover').hidden=true;$('providers-toggle')?.setAttribute('aria-expanded','false');showAccounts();};
      return row;
    }));
    const count=$('providers-count');if(count)count.textContent=connectedTotal?String(connectedTotal):'';
  }
}
async function showAccounts(){await refreshConnections();$('accounts-dialog').showModal();}
async function loginAccount(account){
  if(activeLoginAccount&&activeLoginAccount!==account.id)throw new Error('Сначала завершите текущий вход. Авторизации выполняются по очереди.');
  activeLoginAccount=account.id;
  setText('account-login-state','Открываю вход для '+accountLabel(account));
  let response;try{response=await api(`/api/accounts/${account.id}/login`,{method:'POST'});}catch(error){activeLoginAccount=null;throw error;}let opened=false;
  if(response.state==='connected'){activeLoginAccount=null;setText('account-login-state','Этот аккаунт уже подключён. Для другого аккаунта создайте отдельное подключение.');return;}
  const open=async(url)=>{if(!url||opened)return;opened=true;if(window.aigentDesktop)await window.aigentDesktop.openAuth(url,account.browser_profile);else window.open(url,'_blank','noopener');};
  await open(response.url);
  let count=0;const timer=setInterval(async()=>{try{response=await api(`/api/accounts/${account.id}/login`);await open(response.url);$('login-code-form').hidden=!response.requires_code;if(response.connected){clearInterval(timer);activeLoginAccount=null;$('login-code-form').hidden=true;setText('account-login-state','Подключён: '+accountLabel(account));await refreshConnections(true);}else if(++count>=120){clearInterval(timer);activeLoginAccount=null;setText('account-login-state','Вход ожидает завершения. Можно повторно открыть вход.');}}catch(error){clearInterval(timer);activeLoginAccount=null;toast(error.message);}},3000);
  setText('account-login-state','Завершите вход в выбранном браузерном профиле. Подключение проверяется автоматически.');
}
async function nativeHistory(account){
  const items=await api(`/api/accounts/${account.id}/sessions`);$('native-history-list').replaceChildren();
  for(const item of items){const row=el('div',undefined,'native-session-row');row.append(el('strong',item.title||item.summary||item.session_id||item.id),el('small',item.cwd||item.directory||''));const b=el('button','Открыть отдельную ветку');b.onclick=handle(async()=>{const s=await api(`/api/accounts/${account.id}/sessions/${encodeURIComponent(item.id||item.session_id)}/import`,{method:'POST'});$('native-history-dialog').close();$('accounts-dialog').close();await selectSession(s);});row.append(b);$('native-history-list').append(row);}
  if(!items.length)$('native-history-list').append(el('p','В этом подключении локальных чатов пока нет.','muted'));$('native-history-dialog').showModal();
}
function updateNewChatModels(){
  const a=accountsData.find(x=>x.id===$('new-chat-account').value);if(!a)return;
  $('new-chat-model').replaceChildren(el('option','По умолчанию'),...(a.status.models||[]).map(m=>{const o=el('option',m.displayName||m.model||m.id);o.value=m.model||m.id;return o;}));$('new-chat-model').options[0].value='';
  setText('new-chat-account-state',a.status.connected?'Подключение готово':a.provider==='deepseek'?'Задайте API-ключ в настройках':'Завершите вход во вкладке «Аккаунты и лимиты».');
}
async function showNewChat(accountId){
  await refreshConnections();$('new-chat-title').value='';
  const aid=accountId||current?.account_id||'deepseek-default';if(accountsData.some(a=>a.id===aid))$('new-chat-account').value=aid;
  $('new-chat-project').value=selectedProject||current?.project_id||'';$('new-chat-telegram').checked=!!current?.chat_id;$('new-chat-telegram').disabled=!current?.chat_id;
  updateNewChatModels();$('new-chat-dialog').showModal();
}
async function addProject(){let folder=window.aigentDesktop?await window.aigentDesktop.chooseProject():await inputDialog('Полный путь к папке проекта');if(!folder)return;const p=await api('/api/projects',{method:'POST',body:{path:folder}});selectedProject=p.id;await refreshConnections();toast('Проект подключён: '+p.name);}
async function showSessionMenu(){
  if(!current){await showNewChat();return;}
  if(!projectsData.length)await refreshConnections();
  $('rename-title').value=current.title;
  $('session-project-select').replaceChildren(el('option','Отдельная рабочая папка'),...projectsData.map(p=>{const o=el('option',p.name);o.value=p.id;o.title=p.path;return o;}));
  $('session-project-select').options[0].value='';
  $('session-project-select').value=current.project_id||'';
  $('session-auto-approve').checked=!!current.auto_approve;
  $('session-auto-continue').checked=current.auto_continue!==0;
  setText('session-workspace-note','Рабочая папка: '+(current.workspace||'отдельная папка сессии')+'. Смена проекта доступна, когда ход завершён.');
  $('session-menu-dialog').showModal();
}
window.showSessionMenu=showSessionMenu;

const originalSelect=selectSession;
selectSession=async function(s){if(editorDirty){toast('Сначала сохраните открытый файл.');return;}terminalHistory.clear();$('task-plan').replaceChildren();await originalSelect(s);selectedProject=s.project_id||'';editorFile=null;editorWidget?.destroy();editorWidget=null;$('code-editor').replaceChildren(el('p','Выберите файл.','muted'));setText('editor-path','Выберите файл');$('save-file').disabled=true;renderProjects();setComposerProvider();if(!dock.hidden)await showDock(dock.dataset.active||'editor');};
function setComposerProvider(){if(!current)return;const a=accountsData.find(a=>a.id===current.account_id);const name=current.provider==='codex'?'Codex':current.provider==='claude'?'Claude':'DeepSeek';$('composer-model').firstChild.textContent=(current.model||name)+' ';setText('thinking-label',current.provider==='deepseek'?'Thinking':current.effort||'');$('composer-model').title=a?accountLabel(a):name;}
const originalRefresh=refresh;
refresh=async function(){await originalRefresh();setComposerProvider();try{const questions=await api('/api/questions');renderQuestions(questions.filter(q=>q.sid===current?.id&&!isAnsweredQuestion(q.id)));}catch{}};
function isAnsweredQuestion(id){try{return answeredQuestionIds().has(id);}catch{return false;}}
function rememberAnsweredQuestion(id){try{rememberAnswer(id,'');}catch{}}
function renderQuestions(questions){
  const signature=JSON.stringify(questions);if($('pending-questions').dataset.signature===signature)return;$('pending-questions').dataset.signature=signature;
  $('pending-questions').replaceChildren(...questions.map(q=>{const form=el('form',undefined,'approval-card');const fields=[];for(const item of q.questions){const label=el('label',item.question);const input=el('input');input.required=true;label.append(input);if(item.options)label.append(el('small',item.options.map(o=>o.label||o).join(' / ')));form.append(label);fields.push([item.id,input]);}form.append(el('button','Ответить','primary'));form.onsubmit=handle(async()=>{await api('/api/questions/'+q.id,{method:'POST',body:{answers:Object.fromEntries(fields.map(([id,input])=>[id,input.value]))}});rememberAnsweredQuestion(q.id);await refresh();});return form;}));
}
const originalRenderEvent=renderEvent;
renderEvent=function(event){
  if(event.session_id&&event.session_id!==current?.id)return;
  if(event.kind==='terminal'){if(seen.has(event.id))return;seen.add(event.id);const id=event.payload.id;terminalHistory.set(id,(terminalHistory.get(id)||'')+(event.payload.delta||''));if(terminalSession===event.session_id&&terminalId===id)terminalWidget?.write(event.payload.delta||'');if(event.payload.exit_code!==undefined&&terminalId===id)$('terminal-command').placeholder='Команда завершена · новая команда…';return;}
  if(event.kind==='plan'){const steps=event.payload.plan||[];$('task-plan').replaceChildren(...steps.map(s=>el('span',(s.status==='completed'?'✓ ':s.status==='in_progress'?'◌ ':'○ ')+(s.step||s.description),s.status)));return;}
  if(event.kind==='diff'){setText('git-diff',event.payload.text||'');return;}
  if(['question','question_closed'].includes(event.kind))return;
  originalRenderEvent(event);
};

async function showDock(name){if(!current){await showNewChat();return;}dock.hidden=false;dock.dataset.active=name;document.querySelectorAll('[data-dock]').forEach(b=>b.classList.toggle('active',b.dataset.dock===name));for(const key of ['editor','git','terminal'])$(key+'-dock').hidden=key!==name;
  if(name==='editor')await loadTree();if(name==='git')await loadGit();if(name==='terminal')ensureTerminal();}
async function loadTree(){const files=await api(`/api/sessions/${current.id}/files`);$('file-tree').replaceChildren(...files.map(f=>{const b=el('button',f.path);b.title=f.path;
  // A picture or a clip opens in the media viewer; the code editor only receives text files.
  // Модель .glb открывается в 3D-панели, картинка или клип — в просмотрщике, остальное — в редакторе.
  b.onclick=handle(()=>(window.MediaUI?.isViewable?.(f.path)??window.MediaUI?.isMedia(f.path))?window.MediaUI.open(current.id,f.path):openEditor(f.path));return b;}));}
async function openEditor(name){if(editorDirty){toast('Сохраните изменения перед открытием другого файла.');return;}const value=await api(`/api/sessions/${current.id}/editor?path=${encodeURIComponent(name)}`);editorFile=value;editorWidget?.destroy();$('code-editor').replaceChildren();editorWidget=window.AIGentWidgets.editor($('code-editor'),{path:name,text:value.text,onChange:()=>{editorDirty=true;$('save-file').disabled=false;setText('editor-path',name+' •');}});setText('editor-path',name);$('save-file').disabled=true;}
async function saveEditor(){if(!editorFile||!editorWidget)return;editorFile=await api(`/api/sessions/${current.id}/editor`,{method:'PUT',body:{path:editorFile.path,text:editorWidget.text(),revision:editorFile.revision}});editorDirty=false;$('save-file').disabled=true;setText('editor-path',editorFile.path);toast('Файл сохранён');}
async function loadGit(){try{const result=await api(`/api/sessions/${current.id}/git`);setText('git-branch','⑂ '+(result.branch||'Detached HEAD'));$('git-changes').replaceChildren(...result.changes.map(f=>{const row=el('div',undefined,'git-file');row.append(el('span',f.status),el('span',f.path));const diff=el('button','Diff');diff.onclick=handle(async()=>{const r=await api(`/api/sessions/${current.id}/git/diff?path=${encodeURIComponent(f.path)}`);setText('git-diff',r.diff||'Новый файл или изменения уже добавлены в индекс.');});const stage=el('button',f.status[0]===' '||f.status==='??'?'+':'−');stage.onclick=handle(async()=>{await api(`/api/sessions/${current.id}/git`,{method:'POST',body:{action:stage.textContent==='+'?'stage':'unstage',paths:[f.path]}});await loadGit();});row.append(diff,stage);return row;}));if(!result.changes.length)$('git-changes').append(el('p','Рабочая папка чистая.','muted'));}catch(error){setText('git-branch','Git');$('git-changes').replaceChildren(el('p',error.message,'muted'));}}
function ensureTerminal(){if(terminalWidget&&terminalSession===current.id){terminalWidget.fit();return;}terminalWidget?.destroy();$('terminal-output').replaceChildren();terminalSession=current.id;terminalId=null;terminalWidget=window.AIGentWidgets.terminal($('terminal-output'),data=>{if(terminalId)api(`/api/sessions/${terminalSession}/terminal/${terminalId}/stdin`,{method:'POST',body:{text:data}}).catch(()=>{});});terminalWidget.write('AIGent terminal\r\n');}

$('new-session').onclick=handle(()=>showNewChat());$('accounts-button').onclick=handle(showAccounts);$('open-project').onclick=handle(addProject);$('refresh-accounts').onclick=handle(()=>refreshConnections(true));$('new-chat-account').onchange=updateNewChatModels;
$('new-chat-form').onsubmit=handle(async()=>{const account=accountsData.find(a=>a.id===$('new-chat-account').value);if(!account)throw new Error('Выберите аккаунт');if(!account.status.connected)throw new Error('Сначала подключите выбранный аккаунт.');const body={title:$('new-chat-title').value.trim()||'Новый чат',account_id:account.id,model:$('new-chat-model').value,effort:$('new-chat-effort').value,project_id:$('new-chat-project').value||null};const endpoint=$('new-chat-telegram').checked&&current?.chat_id?`/api/sessions/${current.id}/topics`:'/api/sessions';const s=await api(endpoint,{method:'POST',body});$('new-chat-dialog').close();await selectSession(s);});
$('account-provider').onchange=()=>$('account-key-label').hidden=$('account-provider').value!=='deepseek';
$('account-browser').onchange=()=>{const b=browsersData.find(b=>b.id===$('account-browser').value);if(b?.email)$('account-name').value=b.email;};
$('account-form').onsubmit=handle(async()=>{await api('/api/accounts',{method:'POST',body:{provider:$('account-provider').value,name:$('account-name').value,browser_profile:$('account-browser').value,api_key:$('account-key').value}});$('account-key').value='';await refreshConnections();toast('Подключение добавлено. Выполните вход в нужный аккаунт.');});
$('session-menu-form').onsubmit=handle(async()=>{
  const body={title:$('rename-title').value,auto_approve:$('session-auto-approve').checked,
              auto_continue:$('session-auto-continue').checked};
  if(($('session-project-select').value||'')!==(current.project_id||''))body.project_id=$('session-project-select').value;
  current=await api('/api/sessions/'+current.id,{method:'PATCH',body});
  setText('session-title',current.title);selectedProject=current.project_id||'';
  $('session-menu-dialog').close();await loadProjects();await refresh();renderProjects();
  toast(current.workspace?'Чат работает в проекте: '+current.workspace:'Чат работает в отдельной папке сессии');
});
$('pin-chat').onclick=handle(async()=>{await api('/api/sessions/'+current.id,{method:'PATCH',body:{pinned:!current.pinned}});$('session-menu-dialog').close();await refresh();});
$('archive-chat').onclick=handle(async()=>{await api('/api/sessions/'+current.id,{method:'PATCH',body:{archived:true}});$('session-menu-dialog').close();current=null;source?.close();$('events').replaceChildren();setText('session-title','Новый чат');await refresh();});
$('fork-chat').onclick=handle(async()=>{const s=await api(`/api/sessions/${current.id}/fork`,{method:'POST'});$('session-menu-dialog').close();await selectSession(s);});
$('show-archive').onclick=handle(async()=>{const sessions=await api('/api/sessions?archived=true');$('native-history-list').replaceChildren(...sessions.map(s=>{const row=el('div',undefined,'native-session-row');row.append(el('strong',s.title));const button=el('button','Восстановить');button.onclick=handle(async()=>{const restored=await api('/api/sessions/'+s.id,{method:'PATCH',body:{archived:false}});$('native-history-dialog').close();await selectSession(restored);});row.append(button);return row;}));$('native-history-dialog').showModal();});
$('show-trash').onclick=handle(async()=>{const sessions=await api('/api/sessions?deleted=true');$('native-history-list').replaceChildren(...sessions.map(s=>{const row=el('div',undefined,'native-session-row');row.append(el('strong',s.title));const button=el('button','Восстановить');button.onclick=handle(async()=>{const restored=await api(`/api/sessions/${s.id}/restore`,{method:'POST'});$('native-history-dialog').close();await selectSession(restored);});row.append(button);return row;}));if(!sessions.length)$('native-history-list').append(el('p','Корзина пуста.','muted'));$('native-history-dialog').showModal();});
document.querySelectorAll('[data-dock]').forEach(b=>b.onclick=handle(()=>showDock(b.dataset.dock)));$('close-dock').onclick=()=>dock.hidden=true;$('save-file').onclick=handle(saveEditor);$('refresh-git').onclick=handle(loadGit);
$('create-file').onclick=handle(async()=>{const name=await inputDialog('Путь нового файла');if(!name)return;await api(`/api/sessions/${current.id}/editor`,{method:'PUT',body:{path:name,text:'',revision:null}});await loadTree();await openEditor(name);});
$('git-commit-form').onsubmit=handle(async()=>{const r=await api(`/api/sessions/${current.id}/git`,{method:'POST',body:{action:'commit',message:$('commit-message').value}});toast(r.result);$('commit-message').value='';await loadGit();});
$('terminal-form').onsubmit=handle(async()=>{if(!current)return;const command=$('terminal-command').value.trim();if(!command)return;ensureTerminal();const r=await api(`/api/sessions/${current.id}/terminal`,{method:'POST',body:{command}});terminalId=r.id;terminalSession=current.id;terminalWidget.write(terminalHistory.get(r.id)||'');$('terminal-command').value='';});
$('terminal-stop').onclick=handle(async()=>{if(terminalId)await api(`/api/sessions/${terminalSession}/terminal/${terminalId}/stop`,{method:'POST'});});
$('composer').onsubmit=handle(async()=>{if(!current){await showNewChat();return;}await submitMessage();});
document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='s'&&editorFile){event.preventDefault();saveEditor().catch(e=>toast(e.message));}});
document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='p'&&!document.querySelector('dialog[open]')){event.preventDefault();(async()=>{if(!current)return toast('Сначала выберите чат');const file=await inputDialog('Открыть файл проекта');if(file){await showDock('editor');await openEditor(file);editorWidget?.focus();}})().catch(e=>toast(e.message));}},true);
window.aigentDesktop?.onAction(action=>{if(action==='new-chat')showNewChat().catch(e=>toast(e.message));if(action==='open-project')addProject().catch(e=>toast(e.message));});
refreshConnections().catch(()=>{});setInterval(()=>refreshConnections().catch(()=>{}),60000);
