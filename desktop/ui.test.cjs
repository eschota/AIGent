const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {JSDOM}=require('jsdom');
const {zoomAction}=require('../connector/static/zoom.js');
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
  assert.match(d.querySelector('.turn-tool').textContent,/contents 0/);
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
