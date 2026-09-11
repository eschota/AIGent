const {contextBridge,ipcRenderer}=require('electron');
contextBridge.exposeInMainWorld('aigentDesktop',{
  chooseProject:()=>ipcRenderer.invoke('aigent:choose-project'),
  browserProfiles:()=>ipcRenderer.invoke('aigent:browser-profiles'),
  openAuth:(url,profile)=>ipcRenderer.invoke('aigent:open-auth',{url,profile}),
  info:()=>ipcRenderer.invoke('aigent:info'),
  reauthenticate:()=>ipcRenderer.invoke('aigent:reauthenticate'),
  updateStatus:force=>ipcRenderer.invoke('aigent:update-status',{force}),
  updateDownload:()=>ipcRenderer.invoke('aigent:update-download'),
  updateInstall:()=>ipcRenderer.invoke('aigent:update-install'),
  zoom:action=>ipcRenderer.invoke('aigent:zoom',action),
  copyImage:dataUrl=>ipcRenderer.invoke('aigent:copy-image',dataUrl),
  copyFile:(path,name)=>ipcRenderer.invoke('aigent:copy-file',{path,name}),
  onZoom:callback=>{const listener=(_event,value)=>callback(value);ipcRenderer.on('aigent:zoom',listener);return()=>ipcRenderer.removeListener('aigent:zoom',listener);},
  onUpdate:callback=>{const listener=(_event,status)=>callback(status);ipcRenderer.on('aigent:update',listener);return()=>ipcRenderer.removeListener('aigent:update',listener);},
  onUpdateProgress:callback=>{const listener=(_event,value)=>callback(value);ipcRenderer.on('aigent:update-progress',listener);return()=>ipcRenderer.removeListener('aigent:update-progress',listener);},
  onAction:(callback)=>{const listener=(_event,action)=>callback(action);ipcRenderer.on('aigent:action',listener);return()=>ipcRenderer.removeListener('aigent:action',listener);}
});
