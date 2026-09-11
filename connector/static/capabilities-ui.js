'use strict';
const capabilitiesButton=el('button','Экран и ферма');$('settings-button').before(capabilitiesButton);
const capabilitiesDialog=el('dialog');capabilitiesDialog.id='capabilities-dialog';
capabilitiesDialog.innerHTML='<form id="capabilities-form"><div class="section-heading"><h2>Инструменты DeepSeek</h2><button type="button" id="close-capabilities">✕</button></div><label>Окно для Computer Use<select id="computer-window"></select></label><p>Скриншоты выбранного окна передаются DeepSeek. Клики и ввод требуют вашего подтверждения. После перезапуска сервера окно нужно выбрать заново.</p><label class="checkbox"><input id="autorig-enabled" type="checkbox"> Бесплатная ферма AutoRig: картинки и 3D</label><p>Агент сможет отправлять выбранные референсы на autorig.online, создавать задачи и загружать результаты в папку чата. Проверка геометрии выполняется до выдачи модели.</p><button class="primary">Сохранить</button><div id="autorig-jobs" class="muted"></div></form>';
document.body.append(capabilitiesDialog);
let capabilitiesSession=null;
capabilitiesButton.onclick=handle(async()=>{
  if(!current)throw new Error('Сначала выберите чат');
  if(current.provider!=='deepseek')throw new Error('Выберите чат DeepSeek для этих инструментов');
  capabilitiesSession=current.id;
  const [windows,computer,farm]=await Promise.all([api('/api/computer/windows'),api(`/api/sessions/${current.id}/computer`),api(`/api/sessions/${current.id}/autorig`)]);
  const select=$('computer-window');select.replaceChildren(el('option','Отключено'));select.options[0].value='';
  for(const window of windows){const option=el('option',window.title);option.value=window.id;select.append(option);}
  select.value=computer.window?.id||'';select.disabled=!computer.supported;
  $('autorig-enabled').checked=farm.enabled;
  $('autorig-jobs').textContent=Object.entries(farm.jobs).map(([id,j])=>`${id} · ${j.stage}`).join('\n');
  capabilitiesDialog.showModal();
});
$('close-capabilities').onclick=()=>capabilitiesDialog.close();
$('capabilities-form').onsubmit=handle(async()=>{
  if(current?.id!==capabilitiesSession)throw new Error('Чат изменился; откройте настройки ещё раз');
  await api(`/api/sessions/${capabilitiesSession}/computer`,{method:'POST',body:{window_id:$('computer-window').value?Number($('computer-window').value):null}});
  await api(`/api/sessions/${capabilitiesSession}/autorig`,{method:'POST',body:{accepted:$('autorig-enabled').checked}});
  capabilitiesDialog.close();toast('Инструменты чата настроены');
});
