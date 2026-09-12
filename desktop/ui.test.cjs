const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {JSDOM}=require('jsdom');
const {zoomAction}=require('../connector/static/zoom.js');
const {shortcutOptions}=require('./windows-integration.cjs');
const staticDir=path.join(__dirname,'..','connector','static');
function ui(){
  const dom=new JSDOM(fs.readFileSync(path.join(staticDir,'index.html'),'utf8'),{url:'http://localhost/',runScripts:'outside-only'});
  const w=dom.window;
  w.fetch=()=>new Promise(()=>{}); // No network/auth/model activity in renderer tests.
  vm.runInContext(fs.readFileSync(path.join(staticDir,'app.js'),'utf8'),dom.getInternalVMContext());
  let id=0;
  return {dom,w,event:(kind,payload)=>w.renderEvent({id:++id,kind,payload,created:0})};
}
test('a multi-step turn groups tools, preserves arguments/results and totals usage once',()=>{
  const {dom,w,event}=ui();
  event('user',{text:'Find project rules'});
  for(let i=0;i<8;i++){
    event('stream',{id:'s'+i,reasoning:'step '+i,text:'',done:true});
    event('tool',{name:'read_file',arguments:{path:'README.md'},call_id:'c'+i});
    event('tool_result',{name:'read_file',result:{text:'contents '+i},call_id:'c'+i});
    event('usage',{prompt_tokens:100,completion_tokens:10,cache_hit_tokens:80,cost_usd:.001,saved_usd:.002});
  }
  event('assistant',{text:'Found the rules.'});event('turn_completed',{});
  const d=w.document;
  assert.equal(d.querySelectorAll('.agent-turn').length,1);
  assert.equal(d.querySelectorAll('.turn-work').length,1);
  assert.equal(d.querySelectorAll('.turn-tool').length,8);
  assert.equal(d.querySelectorAll('.turn-usage').length,1);
  assert.match(d.querySelector('.turn-usage').textContent,/Вход 800/);
  assert.match(d.querySelector('.turn-usage').textContent,/\$0.008000/);
  assert.equal(d.querySelector('.turn-work').open,false);
  assert.equal(d.querySelector('.turn-reasoning').open,false);
  assert.match(d.querySelector('.turn-reasoning pre').textContent,/step 7/);
  const firstTool=d.querySelector('.turn-tool');
  assert.doesNotMatch(firstTool.textContent,/contents 0/,'a closed row does not build its JSON view');
  firstTool.open=true;firstTool.dispatchEvent(new w.Event('toggle'));
  assert.match(firstTool.textContent,/contents 0/,'opening the row renders the result');
  assert.equal(d.querySelectorAll('.event.assistant').length,1);
  dom.window.close();
});
test('old history pairs repeated calls by order; errors and unknown usage stay explicit',()=>{
  const {dom,w,event}=ui();
  event('user',{text:'Old request'});
  event('tool',{name:'list_files',arguments:{path:'.'}});
  event('tool_result',{name:'list_files',result:{files:['README.md']}});
  event('tool',{name:'list_files',arguments:{path:'missing'}});
  event('tool_result',{name:'list_files',result:{error:'missing'}});
  event('usage',{prompt_tokens:40,completion_tokens:10,cache_hit_tokens:null,cost_usd:null,saved_usd:null});
  event('user',{text:'New request'});
  event('stream',{id:'live',reasoning:'Still working',text:'Streaming'});
  const d=w.document;
  assert.equal(d.querySelectorAll('.turn-tool').length,2);
  assert.match(d.querySelector('.turn-work>summary').textContent,/ошибок: 1/);
  assert.match(d.querySelector('.turn-usage').textContent,/Кеш —/);
  assert.equal(d.querySelectorAll('.agent-turn').length,2);
  assert.equal(d.querySelectorAll('.turn-reasoning')[1].open,true);
  assert.equal(d.querySelector('.live-answer').textContent,'Streaming');
  dom.window.close();
});
test('zoom shortcuts support main keys, Russian layout and numpad without swallowing plain typing',()=>{
  for(const code of ['Minus','NumpadSubtract'])assert.equal(zoomAction({ctrlKey:true,code,key:'-'}),'out');
  for(const code of ['Equal','NumpadAdd'])assert.equal(zoomAction({control:true,code,key:'+'}),'in');
  assert.equal(zoomAction({meta:true,code:'Digit0',key:'0'}),'reset');
  assert.equal(zoomAction({ctrlKey:true,code:'Equal',key:'='}),'in');
  assert.equal(zoomAction({code:'Minus',key:'-'}),null);
  assert.equal(zoomAction({ctrlKey:true,altKey:true,code:'Equal'}),null);
});
test('browser zoom decreases, increases and resets repeatedly without accumulating scale',()=>{
  const {dom,w}=ui();w.eval(fs.readFileSync(path.join(staticDir,'zoom.js'),'utf8'));
  const key=code=>w.dispatchEvent(new w.KeyboardEvent('keydown',{ctrlKey:true,code,bubbles:true,cancelable:true}));
  key('Minus');assert.equal(w.document.documentElement.style.zoom,'0.9');
  key('NumpadAdd');assert.equal(w.document.documentElement.style.zoom,'1');
  key('NumpadSubtract');key('NumpadSubtract');key('Digit0');
  assert.equal(w.document.documentElement.style.zoom,'1');
  const reset=new w.KeyboardEvent('keydown',{ctrlKey:true,code:'Digit0',cancelable:true});
  w.dispatchEvent(reset);assert.equal(reset.defaultPrevented,false); // Also reset pre-existing browser zoom.
  w.document.getElementById('zoom-in').click();assert.equal(w.document.documentElement.style.zoom,'1.1');
  w.document.getElementById('zoom-reset').click();assert.equal(w.document.getElementById('zoom-reset').textContent,'100%');
  dom.window.close();
});

test('right-click menu targets the clicked chat, including an unselected chat',async()=>{
  const {dom,w}=ui();
  vm.runInContext(fs.readFileSync(path.join(staticDir,'desktop-ui.js'),'utf8'),dom.getInternalVMContext());
  vm.runInContext("allSessions=[{id:'clicked',title:'Test chat'}];current={id:'other'};",dom.getInternalVMContext());
  const b=w.document.createElement('button');b.className='session-item';b.dataset.sessionId='clicked';w.document.getElementById('sessions').append(b);
  b.dispatchEvent(new w.MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:50,clientY:100}));
  const menu=w.document.getElementById('chat-context-menu');
  assert.equal(menu.hidden,false);
  assert.deepEqual([...menu.querySelectorAll('button')].map(b=>b.textContent),['Форкнуть','Удалить']);
  vm.runInContext("deleteChat=async sid=>{window.deletedFixture=sid;};",dom.getInternalVMContext());
  menu.querySelectorAll('button')[1].click();
  await Promise.resolve();
  assert.equal(w.deletedFixture,'clicked');assert.equal(menu.hidden,true);
  dom.window.close();
});

test('Start menu shortcut retains data path and correct packaged/development target',()=>{
  const options={executable:'C:\\Programs\\AIGent.exe',appDirectory:'C:\\Programs',dataDirectory:'R:\\My Project\\.local',icon:'C:\\Programs\\AIGent.exe'};
  const packed=shortcutOptions({...options,packaged:true});
  assert.equal(packed.appUserModelId,'org.eschota.aigent');
  assert.equal(packed.args,'--data-dir "R:\\My Project\\.local"');
  const dev=shortcutOptions({...options,packaged:false});
  assert.equal(dev.args,'"C:\\Programs" --data-dir "R:\\My Project\\.local"');
});

test('expired auth recovers and retries identical message without losing the draft',async()=>{
  const {dom,w}=ui();
  vm.runInContext("current={id:'auth-fixture'};",dom.getInternalVMContext());
  w.document.getElementById('workspace').hidden=false;
  w.document.getElementById('message').value='Keep my draft';
  let renewed=0;const requests=[];
  w.aigentDesktop={reauthenticate:async()=>{renewed++;}};
  w.fetch=async(url,options)=>{
    // Startup fetches (entities, status) are not the subject here: only the message request is counted.
    if(!String(url).includes('/messages'))return new Response('{}',{status:200,headers:{'content-type':'application/json'}});
    requests.push(options);return new Response(JSON.stringify(requests.length===1?{detail:'expired'}:{accepted:true}),{status:requests.length===1?401:202,headers:{'content-type':'application/json'}});};
  assert.deepEqual(await w.api('/api/sessions/auth-fixture/messages',{method:'POST',body:{text:'Keep my draft',request_id:'same'}}),{accepted:true});
  assert.equal(renewed,1);assert.equal(requests.length,2);assert.equal(requests[0].body,requests[1].body);
  assert.equal(w.document.getElementById('message').value,'Keep my draft');
  assert.equal(w.localStorage.getItem('aigent.draft.auth-fixture'),'Keep my draft');
  dom.window.close();
});

// --- Self-development UI: clipboard attachments, queued turns and the Skill Manager widget ---
function withScripts(...names){
  const {dom,w,event}=ui();
  for(const name of names)vm.runInContext(fs.readFileSync(path.join(staticDir,name),'utf8'),dom.getInternalVMContext());
  return {dom,w,event};
}
const tick=async(times=12)=>{for(let i=0;i<times;i++)await new Promise(r=>setTimeout(r,0));};
function router(table){
  const calls=[];
  return {calls,fetch:async(url,options={})=>{
    calls.push({url,method:options.method||'GET',body:options.body});
    const key=Object.keys(table).find(k=>url.startsWith(k)||url.includes(k));
    const value=key?table[key]:{};
    const data=typeof value==='function'?value(url,options):value;
    return new Response(JSON.stringify(data),{status:200,headers:{'content-type':'application/json'}});
  }};
}
const sessionUsage={prompt_tokens:10,completion_tokens:2,cache_hit_tokens:8,cache_hit_percent:80,cost_usd:0.001,saved_usd:0.002,unpriced_requests:0,unknown_cache_requests:0,requests:1};

test('a pasted screenshot uploads once and travels with the next message',async()=>{
  const {dom,w}=withScripts();
  vm.runInContext("current={id:'paste-fixture',status:'idle',provider:'deepseek'};refresh=async()=>{};",dom.getInternalVMContext());
  w.document.getElementById('workspace').hidden=false;
  const {calls,fetch}=router({'/files':{path:'ab12cd34-clipboard.png',bytes:120},'/messages':{accepted:true,queued:false,position:0}});
  w.fetch=fetch;
  let revoked=0;
  w.URL.createObjectURL=()=>'blob:clipboard-fixture';w.URL.revokeObjectURL=()=>{revoked++;};
  const file=new w.File([new Uint8Array([1,2,3])],'clipboard.png',{type:'image/png'});
  const paste=new w.Event('paste',{bubbles:true,cancelable:true});
  paste.clipboardData={items:[{kind:'file',getAsFile:()=>file}]};
  w.document.getElementById('message').dispatchEvent(paste);
  await tick();
  assert.equal(paste.defaultPrevented,true,'the image must not be pasted as text');
  const chips=w.document.querySelectorAll('.attachment-chip');
  assert.equal(chips.length,1);
  assert.match(chips[0].textContent,/clipboard\.png/);
  assert.equal(chips[0].querySelector('img').src,'blob:clipboard-fixture','the preview renders from the local file');
  assert.equal(calls.filter(c=>c.url.endsWith('/files')).length,1,'one upload per pasted image');

  w.document.getElementById('message').value='Что на скриншоте?';
  await w.submitMessage();
  const sent=JSON.parse(calls.find(c=>c.url.includes('/messages')).body);
  assert.deepEqual(sent.attachments,['ab12cd34-clipboard.png']);
  assert.equal(sent.text,'Что на скриншоте?');
  assert.equal(w.document.querySelectorAll('.attachment-chip').length,0,'attachments clear after sending');
  assert.equal(revoked>0,true,'preview URLs are released');
  dom.window.close();
});

test('an attached image can be detached before the message is sent',async()=>{
  const {dom,w}=withScripts();
  vm.runInContext("current={id:'detach',status:'idle'};refresh=async()=>{};",dom.getInternalVMContext());
  w.document.getElementById('workspace').hidden=false;
  const {calls,fetch}=router({'/files':{path:'ff00ff00-shot.png',bytes:2048},'/messages':{accepted:true,queued:false}});
  w.fetch=fetch;
  w.URL.createObjectURL=()=>'blob:detach-fixture';w.URL.revokeObjectURL=()=>{};
  const paste=new w.Event('paste',{bubbles:true,cancelable:true});
  paste.clipboardData={items:[{kind:'file',getAsFile:()=>new w.File([new Uint8Array([1])],'shot.png',{type:'image/png'})}]};
  w.document.getElementById('message').dispatchEvent(paste);
  await tick();
  const chip=w.document.querySelector('.attachment-chip');
  assert.ok(chip,'the pasted image is shown as a detachable card');
  assert.match(chip.textContent,/2 KB/);
  chip.querySelector('button').click();
  await tick(3);
  assert.equal(w.document.querySelectorAll('.attachment-chip').length,0,'the ✕ detaches it before sending');
  assert.equal(w.document.getElementById('composer-attachments').hidden,true);
  w.document.getElementById('message').value='без вложения';
  await w.submitMessage();
  assert.deepEqual(JSON.parse(calls.find(c=>c.url.includes('/messages')).body).attachments,[]);
  dom.window.close();
});

test('the context meter fills and colours with the real trimming budget',async()=>{
  const {dom,w}=withScripts();
  const sessions=[{id:'ctx',title:'Context',status:'idle',chat_id:0,topic_id:0,project_id:null,auto_approve:0,usage:sessionUsage}];
  const {fetch}=router({'/api/status':{bot:'online',model:'m',usage:sessionUsage,ui_revision:'1'},
    '/api/sessions/ctx/context':{chars:180000,limit:200000,percent:90.0,messages:64,images:3},
    '/api/sessions/ctx/queue':[],'/api/sessions':sessions,'/api/approvals':[],'/api/projects':[],'/api/questions':[]});
  w.fetch=fetch;
  vm.runInContext("current={id:'ctx',status:'idle'};",dom.getInternalVMContext());
  await w.refresh();
  const meter=w.document.getElementById('context-meter');
  assert.equal(meter.hidden,false);
  assert.equal(meter.textContent,'90%');
  assert.equal(meter.querySelector('i').style.width,'90%');
  assert.equal(meter.classList.contains('full'),true,'a nearly full context is coloured as full');
  assert.match(meter.title,/180.0k \/ 200.0k/);
  assert.match(meter.title,/64 сообщений · 3 с изображениями/);
  assert.equal(meter.previousElementSibling.id,'composer-model','the meter sits next to the model');
  dom.window.close();
});

test('an interface newer than the open page offers a single-click reload',async()=>{
  const {dom,w}=withScripts();
  const sessions=[{id:'s',title:'S',status:'idle',chat_id:0,topic_id:0,project_id:null,auto_approve:0,usage:sessionUsage}];
  let revision='100';
  const {fetch}=router({'/api/status':()=>({bot:'online',model:'m',usage:sessionUsage,ui_revision:revision}),
    '/api/sessions/s/context':{chars:1,limit:100,percent:1,messages:1,images:0},
    '/api/sessions/s/queue':[],'/api/sessions':sessions,'/api/approvals':[],'/api/projects':[],'/api/questions':[]});
  w.fetch=fetch;
  vm.runInContext("current={id:'s',status:'idle'};",dom.getInternalVMContext());
  await w.refresh();
  assert.equal(w.document.getElementById('toast').hidden,true);
  revision='200';
  await w.refresh();
  const toast=w.document.getElementById('toast');
  assert.match(toast.textContent,/Интерфейс обновлён/);
  assert.equal(toast.querySelector('button').textContent,'Перезагрузить');
  dom.window.close();
});

test('a busy session keeps sending: the queue is shown and can be cancelled',async()=>{
  const {dom,w}=withScripts();
  const sessions=[{id:'q',title:'Queued chat',status:'running',chat_id:0,topic_id:0,project_id:null,
                   auto_approve:0,provider:'deepseek',usage:sessionUsage}];
  const {calls,fetch}=router({
    '/api/status':{bot:'online',model:'deepseek-flash',usage:sessionUsage,sessions:1,running:1},
    '/api/sessions/q/queue':[{id:5,created:0,text:'второе сообщение'}],
    '/api/sessions':sessions,'/api/approvals':[],'/api/projects':[],'/api/questions':[]});
  w.fetch=fetch;
  vm.runInContext("current={id:'q',status:'running',provider:'deepseek'};",dom.getInternalVMContext());
  await w.refresh();
  const send=w.document.querySelector('.send-button');
  assert.equal(send.hidden,false,'sending stays available while the agent works');
  assert.equal(send.classList.contains('queueing'),true);
  assert.match(w.document.getElementById('message').placeholder,/очередь/);
  const items=w.document.querySelectorAll('.queued-item');
  assert.equal(items.length,1);
  assert.match(items[0].textContent,/второе сообщение/);
  items[0].querySelector('button').click();
  await tick();
  const cancelled=calls.find(c=>c.method==='DELETE');
  assert.equal(cancelled.url,'/api/sessions/q/queue?item=5');
  dom.window.close();
});

test('the auto-apply switch turns confirmations off for the selected chat only',async()=>{
  const {dom,w}=withScripts();
  const {calls,fetch}=router({'/api/sessions/auto':{id:'auto',title:'A',status:'idle',auto_approve:1,project_id:null},
                              '/api/status':{bot:'online',model:'m',usage:sessionUsage},
                              '/api/sessions':[],'/api/approvals':[],'/api/questions':[],'/api/projects':[]});
  w.fetch=fetch;
  vm.runInContext("current={id:'auto',status:'idle',auto_approve:0};refresh=async()=>{};",dom.getInternalVMContext());
  const box=w.document.getElementById('auto-approve');
  box.checked=true;box.dispatchEvent(new w.Event('change'));
  await tick();
  const patch=calls.find(c=>c.method==='PATCH');
  assert.equal(patch.url,'/api/sessions/auto');
  assert.deepEqual(JSON.parse(patch.body),{auto_approve:true});
  assert.match(w.document.getElementById('toast').textContent,/без подтверждения/);
  dom.window.close();
});

test('the skill widget reports index progress on hover and attaches a skill into the chat',async()=>{
  const {dom,w}=withScripts('skill-manager.js');
  const now=Date.now()/1000;
  const status={state:'scanning',progress:0.4,total:12,scanned:400,files:12,finished:now,error:null,
                analysis:'local',free_model:null,tokens_spent:0,roots:[{source:'claude',path:'C:/x'}],
                sources:{claude:9,codex:3},
                recent:[{id:'a1',title:'Release check',source:'claude',kind:'skill',modified:now,uses:2,tags:['release']}],
                active:[{id:'a1',title:'Release check',source:'claude',kind:'skill',modified:now,uses:2,tags:['release']}]};
  const {calls,fetch}=router({'/api/skills/status':status,'/api/skills/tags':[{tag:'release',count:2}],
    '/api/skills/a1/attach':{path:'skills/SKILL.md',title:'Release check',id:'a1',source:'claude',tags:['release'],
                             reference:'Скилл «Release check» приложен к сессии: skills/SKILL.md'},
    '/api/skills':[{id:'a1',title:'Release check',name:'SKILL.md',source:'claude',kind:'skill',summary:'Verify the build',
                    tags:['release','desktop'],modified:now,uses:2,origin:'skills'}]});
  w.fetch=fetch;
  vm.runInContext("current={id:'chat-1',status:'idle'};allSessions=[{id:'chat-1',title:'Dev chat',chat_id:0}];",dom.getInternalVMContext());
  await w.refreshSkillStatus();

  const widget=w.document.getElementById('skills-widget');
  assert.ok(widget,'the widget mounts in the sidebar');
  widget.dispatchEvent(new w.Event('pointerenter'));
  const hover=w.document.getElementById('skills-hover');
  assert.equal(hover.hidden,false,'hovering shows status, progress and the latest skills');
  assert.match(hover.textContent,/Release check/);
  assert.match(hover.textContent,/Claude: 9/);
  assert.match(hover.textContent,/токенов потрачено: 0/);
  assert.equal(w.document.getElementById('skills-bar').style.width,'40%');
  assert.match(w.document.getElementById('skills-state').textContent,/Сканирование/);

  await w.openSkills();
  const rows=w.document.querySelectorAll('.skill-row');
  assert.equal(rows.length,1);
  assert.match(rows[0].textContent,/Verify the build/);
  assert.deepEqual([...w.document.querySelectorAll('#skills-tagbar .skill-tag')].map(b=>b.textContent),['release · 2']);
  rows[0].querySelector('.skill-actions .primary').click();
  await tick();
  const attach=calls.find(c=>c.url.includes('/attach'));
  assert.deepEqual(JSON.parse(attach.body),{session_id:'chat-1'});
  assert.match(w.document.getElementById('message').value,/Скилл «Release check» приложен/);
  widget.dispatchEvent(new w.Event('pointerleave'));
  assert.equal(hover.hidden,true);
  dom.window.close();
});

// --- Interactive media: cards, grouping and the lightbox (needs jsdom; see README checks) ---
test('media events become interactive cards, group into one grid and open a lightbox',async()=>{
  const {dom,w,event}=withScripts('media-ui.js');
  vm.runInContext("current={id:'media-fixture',status:'idle',provider:'deepseek'};",dom.getInternalVMContext());
  const {fetch}=router({'/media/info':{kind:'image',width:1280,height:720,bytes:2048,duration:null}});
  w.fetch=fetch;
  event('user',{text:'Нарисуй три кадра'});
  event('media',{path:'shared/one.png',kind:'image',direction:'generated'});
  event('media',{path:'shared/two.png',kind:'image',direction:'vision'});
  event('media',{path:'shared/clip.mp4',kind:'video',direction:'generated'});
  const d=w.document;
  assert.equal(d.querySelectorAll('.media-grid').length,1,'consecutive media share one responsive grid');
  const cards=d.querySelectorAll('.media-card');
  assert.equal(cards.length,3);
  assert.match(cards[0].querySelector('img').src,/variant=thumb/);
  assert.match(cards[0].querySelector('img').src,/path=shared%2Fone\.png/);
  assert.match(cards[1].textContent,/Агент посмотрел/);
  assert.equal(cards[2].dataset.kind,'video');
  assert.ok(cards[2].querySelector('.media-play'),'a clip shows a play control on its poster');
  assert.ok([...cards[2].querySelectorAll('button')].some(b=>/лайтбокс/i.test(b.textContent)));
  assert.match(cards[0].querySelector('.media-actions a').href,/variant=original/);

  cards[0].querySelector('.media-frame').click();
  await tick(2);
  const box=d.querySelector('.media-lightbox');
  assert.ok(box,'clicking an image opens the lightbox');
  assert.match(box.querySelector('.media-stage img').src,/variant=preview/);
  box.querySelector('.media-stage img').click();
  assert.equal(box.querySelector('.media-stage img').classList.contains('zoomed'),true,'a second click zooms 1:1');
  d.dispatchEvent(new w.KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));
  await tick(2);
  assert.equal(d.querySelector('.media-lightbox').dataset.path,'shared/two.png','arrows walk the chat gallery');
  d.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  assert.equal(d.querySelector('.media-lightbox'),null,'Esc closes it');
  dom.window.close();
});

test('a video card waits for the transcode and then plays inline',async()=>{
  const {dom,w}=withScripts('media-ui.js');
  let status=202;
  w.fetch=async(url,options={})=>{
    if(url.includes('/media/info'))return new Response(JSON.stringify({kind:'video',width:640,height:360,bytes:4096,duration:15}),{status:200,headers:{'content-type':'application/json'}});
    if(options.method==='HEAD')return new Response(null,{status});
    return new Response('{}',{status:200,headers:{'content-type':'application/json'}});
  };
  const card=w.MediaUI.render('vid-fixture','shared/clip.mp4',{kind:'video',direction:'generated'});
  w.document.body.append(card);
  card.querySelector('.media-frame').click();
  await tick(4);
  const badge=card.querySelector('.media-badge');
  assert.equal(badge.hidden,false,'a running transcode shows progress instead of a broken player');
  assert.match(badge.textContent,/Перекодирование|Готовим/);
  status=200;
  await new Promise(r=>setTimeout(r,2100));
  await tick(6);
  const player=card.querySelector('video');
  assert.ok(player,'the ready preview replaces the poster with a player');
  assert.match(player.src,/variant=preview/);
  assert.equal(player.controls,true);
  assert.match(card.querySelector('.media-meta').textContent,/640×360 · 0:15/);
  dom.window.close();
});
