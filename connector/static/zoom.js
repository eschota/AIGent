'use strict';
// Shared with the Electron main process; physical codes also cover Russian layouts and numpad.
function zoomAction(input) {
  if(!(input.ctrlKey||input.metaKey||input.control||input.meta)||input.altKey||input.alt)return null;
  const code=input.code||'',key=input.key||'';
  if(code==='NumpadAdd'||code==='Equal'||key==='+'||key==='=')return 'in';
  if(code==='NumpadSubtract'||code==='Minus'||key==='-'||key==='_')return 'out';
  if(code==='Digit0'||code==='Numpad0'||key==='0')return 'reset';
  return null;
}
if(typeof module!=='undefined')module.exports={zoomAction};
else {
  const controls=document.createElement('div');controls.className='zoom-controls';controls.setAttribute('aria-label','Масштаб интерфейса');
  controls.innerHTML='<button id="zoom-out" type="button" aria-label="Уменьшить масштаб" title="Уменьшить · Ctrl+−">−</button><button id="zoom-reset" type="button" aria-label="Сбросить масштаб" title="Обычный размер · Ctrl+0">100%</button><button id="zoom-in" type="button" aria-label="Увеличить масштаб" title="Увеличить · Ctrl++">+</button>';
  document.getElementById('header-settings').before(controls);
  let factor=1;
  const native=window.aigentDesktop?.zoom;
  const display=value=>{factor=value;document.getElementById('zoom-reset').textContent=Math.round(value*100)+'%';};
  function applyBrowser(value){display(Math.min(2,Math.max(.7,Math.round(value*10)/10)));document.documentElement.style.zoom=String(factor);document.documentElement.style.setProperty('--app-zoom',factor);document.documentElement.dataset.appZoom='true';try{localStorage.setItem('aigent.zoom',String(factor));}catch{}}
  async function change(action){if(native)display(await native(action));else applyBrowser(action==='reset'?1:factor+(action==='in'?.1:-.1));}
  for(const action of ['out','in','reset'])document.getElementById('zoom-'+action).onclick=()=>change(action);
  if(native){native('get').then(display);window.aigentDesktop.onZoom?.(display);}
  else {let saved=1;try{saved=Number(localStorage.getItem('aigent.zoom'))||1;}catch{}applyBrowser(saved);}
  window.addEventListener('keydown',event=>{const action=zoomAction(event);if(!action)return;
    // Let the browser also reset an existing native page zoom on Ctrl+0.
    if(native||action!=='reset')event.preventDefault();
    event.stopImmediatePropagation();change(action);
  },true);
}
