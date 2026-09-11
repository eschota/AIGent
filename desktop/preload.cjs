const {contextBridge,ipcRenderer}=require('electron');
contextBridge.exposeInMainWorld('aigentDesktop',{
  chooseProject:()=>ipcRenderer.invoke('aigent:choose-project'),
  browserProfiles:()=>ipcRenderer.invoke('aigent:browser-profiles'),
  openAuth:(url,profile)=>ipcRenderer.invoke('aigent:open-auth',{url,profile}),
  info:()=>ipcRenderer.invoke('aigent:info'),
  zoom:action=>ipcRenderer.invoke('aigent:zoom',action),
  onZoom:callback=>{const listener=(_event,value)=>callback(value);ipcRenderer.on('aigent:zoom',listener);return()=>ipcRenderer.removeListener('aigent:zoom',listener);},
  onAction:(callback)=>{const listener=(_event,action)=>callback(action);ipcRenderer.on('aigent:action',listener);return()=>ipcRenderer.removeListener('aigent:action',listener);}
});
