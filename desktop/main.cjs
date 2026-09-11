const {app,BrowserWindow,ipcMain,dialog,shell,Menu,session,nativeTheme,clipboard,nativeImage} = require('electron');
const path=require('node:path');
const fs=require('node:fs');
const os=require('node:os');
const crypto=require('node:crypto');
const {spawn}=require('node:child_process');
const {registerWindows,savedDataDirectory}=require('./windows-integration.cjs');
const {requestStatus,portableMode,pickAsset,stage}=require('./updates.cjs');
const {zoomAction}=require(app.isPackaged?'./shared/zoom.js':'../connector/static/zoom.js');
app.commandLine.appendSwitch('force-renderer-accessibility');

function projectRoot(){
  if(!app.isPackaged)return path.resolve(__dirname,'..');
  const initial=process.env.PORTABLE_EXECUTABLE_DIR||path.dirname(process.execPath);
  let directory=initial;
  for(let i=0;i<5;i++){if(fs.existsSync(path.join(directory,'run.py'))&&fs.existsSync(path.join(directory,'.local','config.json')))return directory;const parent=path.dirname(directory);if(parent===directory)break;directory=parent;}
  return initial;
}
const project=projectRoot();
const dataArgument=process.argv.indexOf('--data-dir');
const explicitData=dataArgument>=0?process.argv[dataArgument+1]:null;
const data=path.resolve(explicitData||savedDataDirectory()||(fs.existsSync(path.join(project,'run.py'))?path.join(project,'.local'):path.join(process.env.LOCALAPPDATA||project,'AIGent','.local')));
fs.mkdirSync(path.join(data,'electron-profile'),{recursive:true});
app.setPath('userData',path.join(data,'electron-profile'));
app.setPath('sessionData',path.join(data,'electron-profile','session'));
app.setPath('logs',path.join(data,'electron-logs'));
app.setPath('crashDumps',path.join(data,'electron-crashes'));
if(process.platform==='win32')app.setAppUserModelId('org.eschota.aigent');
process.env.TEMP=path.join(data,'tmp');process.env.TMP=process.env.TEMP;
fs.mkdirSync(process.env.TEMP,{recursive:true});
const origin='http://127.0.0.1:8787';
let win, backend;
if(!process.argv.includes('--register-only')&&!app.requestSingleInstanceLock()){app.quit();}
app.on('second-instance',()=>{if(win){if(win.isMinimized())win.restore();win.show();win.focus();}});

function config(){try{return JSON.parse(fs.readFileSync(path.join(data,'config.json'),'utf8'));}catch{return null;}}
async function verifyServer(){
  const c=config();if(!c)return false;
  const nonce=crypto.randomBytes(24).toString('hex');
  try{
    const r=await fetch(origin+'/api/identity?nonce='+nonce,{signal:AbortSignal.timeout(2000)});
    if(!r.ok)return false;
    const got=(await r.json()).signature;
    const expected=crypto.createHmac('sha256',c.connector_token).update('aigent:'+nonce).digest('hex');
    return typeof got==='string'&&got.length===expected.length&&crypto.timingSafeEqual(Buffer.from(got),Buffer.from(expected));
  }catch{return false;}
}
async function ensureBackend(){
  if(await verifyServer())return;
  let executable,args;
  if(app.isPackaged){executable=path.join(process.resourcesPath,'backend','AIGentServer.exe');args=['--no-browser','--data-dir',data];}
  else{executable=path.join(project,'.venv','Scripts','python.exe');if(process.platform!=='win32')executable=path.join(project,'.venv','bin','python');args=[path.join(project,'run.py'),'--supervise','--no-browser','--data-dir',data];}
  const out=fs.openSync(path.join(data,'desktop-server.log'),'a');
  backend=spawn(executable,args,{cwd:project,windowsHide:true,stdio:['ignore',out,out],env:{...process.env,TEMP:process.env.TEMP,TMP:process.env.TEMP}});
  backend.on('error',()=>{});fs.closeSync(out);
  for(let i=0;i<60;i++){if(await verifyServer())return;await new Promise(r=>setTimeout(r,500));}
  throw new Error('AIGent backend could not start. Check .local/desktop-server.log and port 8787.');
}
async function authenticate(){
  const c=config();if(!c?.admin_password)return;
  if(!await verifyServer())throw new Error('Local server identity mismatch');
  const r=await fetch(origin+'/api/desktop/session',{method:'POST',headers:{Authorization:'Bearer '+c.connector_token}});
  if(!r.ok)throw new Error('Desktop authentication failed');
  const token=await r.json();
  await session.defaultSession.cookies.set({url:origin,name:'ide_admin',value:token.cookie,httpOnly:true,sameSite:'strict',expirationDate:token.expires});
}
function validateSender(event){if(!win||event.sender!==win.webContents||!event.senderFrame.url.startsWith(origin+'/'))throw new Error('Invalid IPC sender');}
function browserProfiles(){
  const root=process.env.LOCALAPPDATA||path.join(os.homedir(),'AppData','Local');
  const entries=[];
  const candidates=[
    {key:'chrome',state:path.join(root,'Google','Chrome','User Data','Local State'),bins:[path.join(process.env.PROGRAMFILES||'C:\\Program Files','Google','Chrome','Application','chrome.exe'),path.join(root,'Google','Chrome','Application','chrome.exe')]},
    {key:'edge',state:path.join(root,'Microsoft','Edge','User Data','Local State'),bins:[path.join(process.env['PROGRAMFILES(X86)']||'C:\\Program Files (x86)','Microsoft','Edge','Application','msedge.exe')]}
  ];
  for(const b of candidates){try{const metadata=JSON.parse(fs.readFileSync(b.state,'utf8')).profile?.info_cache||{};const exe=b.bins.find(f=>fs.existsSync(f));if(!exe)continue;
    for(const [directory,profile] of Object.entries(metadata)){entries.push({id:b.key+':'+directory,browser:b.key,directory,name:profile.name||directory,email:profile.user_name||'',exe});}
  }catch{}}
  return entries;
}
const authHosts=new Set(['auth.openai.com','auth0.openai.com','chatgpt.com','claude.ai','console.anthropic.com','platform.claude.com','accounts.anthropic.com']);
function safeExternal(url,auth=false){const parsed=new URL(url);if(!['http:','https:'].includes(parsed.protocol))throw new Error('Unsupported URL');if(auth&&(!authHosts.has(parsed.hostname)||parsed.protocol!=='https:'))throw new Error('Unsupported authentication origin');return parsed.href;}
ipcMain.handle('aigent:choose-project',async(event)=>{validateSender(event);const r=await dialog.showOpenDialog(win,{title:'Выберите папку проекта',properties:['openDirectory'],defaultPath:project});return r.canceled?null:r.filePaths[0];});
ipcMain.handle('aigent:browser-profiles',event=>{validateSender(event);return browserProfiles().map(({exe,...metadata})=>metadata);});
ipcMain.handle('aigent:open-auth',async(event,{url,profile})=>{validateSender(event);url=safeExternal(url,true);const b=browserProfiles().find(x=>x.id===profile);if(b){const child=spawn(b.exe,['--profile-directory='+b.directory,url],{detached:true,windowsHide:true,stdio:'ignore'});child.unref();}else await shell.openExternal(url);return {opened:true};});
ipcMain.handle('aigent:info',event=>{validateSender(event);return {name:'AIGent',version:app.getVersion(),platform:process.platform,project};});
const updateDirectory=path.join(data,'update');
let staged=null;
async function checkUpdates(force=false){
  const c=config();
  const result=await requestStatus({origin:origin,token:c?.connector_token,force});
  return result.ok?result.status:{current:app.getVersion(),latest:'',update_available:false,assets:[],installer:null,page:'',notes:'',published:'',error:result.error,channel:''};
}
ipcMain.handle('aigent:update-status',async(event,{force}={})=>{validateSender(event);return checkUpdates(Boolean(force));});
ipcMain.handle('aigent:update-download',async(event)=>{
  validateSender(event);
  const status=await checkUpdates(true);
  const kind=portableMode()?'portable':'installer';
  const asset=kind==='portable'?(pickAsset(status.assets,'portable')||status.installer):status.installer;
  if(!asset)throw new Error('Release has no file for this build: '+(status.latest||'unknown version'));
  staged=await stage({asset,directory:updateDirectory,onProgress:progress=>{if(win)win.webContents.send('aigent:update-progress',{progress});}});
  return {version:status.latest,kind,path:staged.path,bytes:staged.bytes,verified:staged.verified};
});
ipcMain.handle('aigent:update-install',async(event)=>{
  validateSender(event);
  if(!staged||!fs.existsSync(staged.path))throw new Error('Сначала скачайте обновление');
  if(portableMode()){
    shell.showItemInFolder(staged.path);
    return {action:'replace',path:staged.path};
  }
  const answer=await dialog.showMessageBox(win,{type:'question',title:'Обновление AIGent',buttons:['Установить и закрыть','Позже'],cancelId:1,defaultId:0,
    message:'Установить AIGent '+path.basename(staged.path)+'?',detail:'AIGent закроется, установщик продолжит работу.'});
  if(answer.response!==0)return {action:'cancelled'};
  const error=await shell.openPath(staged.path);
  if(error)throw new Error(error);
  setTimeout(()=>app.quit(),1500);
  return {action:'installing'};
});
ipcMain.handle('aigent:reauthenticate',async event=>{validateSender(event);await authenticate();return {authenticated:true};});
function changeZoom(action){
  if(!['get','in','out','reset'].includes(action))throw new Error('Unknown zoom action');
  const before=win.webContents.getZoomFactor();
  const next=action==='reset'?1:action==='get'?before:before+(action==='in'?.1:-.1);
  const value=Math.max(.7,Math.min(2,Math.round(next*10)/10));
  win.webContents.setZoomFactor(value);win.webContents.send('aigent:zoom',value);return value;
}
ipcMain.handle('aigent:zoom',(event,action)=>{validateSender(event);return changeZoom(action);});
ipcMain.handle('aigent:copy-image',(event,dataUrl)=>{
  validateSender(event);
  if(typeof dataUrl!=='string'||!dataUrl.startsWith('data:image/'))throw new Error('Ожидается data:image URL');
  const image=nativeImage.createFromDataURL(dataUrl);
  if(image.isEmpty())throw new Error('Изображение не распознано');
  clipboard.writeImage(image);
  return {copied:true,size:image.getSize()};
});
ipcMain.handle('aigent:copy-file',(event,{path:target,name}={})=>{
  validateSender(event);
  if(typeof target!=='string'||!target)throw new Error('Нужен путь к файлу');
  const resolved=path.resolve(target);
  if(!fs.existsSync(resolved))throw new Error('Файл не найден');
  // Windows explorers read CF_HDROP-like FileNameW; the path as text is the portable fallback.
  const payload={text:resolved};
  if(process.platform==='win32')payload['FileNameW']=Buffer.from(resolved+'\u0000','ucs2');
  clipboard.write(payload);
  return {copied:true,path:resolved,name:name||path.basename(resolved)};
});

app.whenReady().then(async()=>{
  nativeTheme.themeSource='dark';
  try{const registration=registerWindows({app,shell,dataDirectory:data});if(process.argv.includes('--register-only')){console.log(JSON.stringify(registration));app.quit();return;}}
  catch(error){if(process.argv.includes('--register-only'))throw error;fs.appendFileSync(path.join(data,'desktop-integration.log'),error.message+'\n');}
  await ensureBackend();await authenticate();
  win=new BrowserWindow({width:1440,height:960,minWidth:800,minHeight:600,title:'AIGent',icon:path.join(__dirname,'assets','icon.png'),backgroundColor:'#181818',show:false,
    webPreferences:{preload:path.join(__dirname,'preload.cjs'),contextIsolation:true,nodeIntegration:false,sandbox:true}});
  win.webContents.setWindowOpenHandler(({url})=>{try{shell.openExternal(safeExternal(url));}catch{}return {action:'deny'};});
  win.webContents.on('will-navigate',(event,url)=>{if(!url.startsWith(origin+'/')){event.preventDefault();try{shell.openExternal(safeExternal(url));}catch{}}});
  // Writing to the clipboard is a user action inside our own window; everything else stays denied.
  const allowed=new Set(['clipboard-sanitized-write','clipboard-write']);
  win.webContents.session.setPermissionRequestHandler((_contents,permission,callback)=>callback(allowed.has(permission)));
  win.webContents.session.setPermissionCheckHandler((_contents,permission)=>allowed.has(permission));
  win.webContents.on('before-input-event',(event,input)=>{if(input.type!=='keyDown')return;const action=zoomAction(input);if(action){event.preventDefault();changeZoom(action);}});
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {label:'Файл',submenu:[{label:'Новый чат',accelerator:'CmdOrCtrl+N',click:()=>win.webContents.send('aigent:action','new-chat')},{label:'Открыть проект…',accelerator:'CmdOrCtrl+O',click:()=>win.webContents.send('aigent:action','open-project')},{type:'separator'},{role:'quit',label:'Выйти'}]},
    {label:'Правка',submenu:[{role:'undo',label:'Отменить'},{role:'redo',label:'Повторить'},{type:'separator'},{role:'cut',label:'Вырезать'},{role:'copy',label:'Копировать'},{role:'paste',label:'Вставить'},{role:'selectAll',label:'Выделить всё'}]},
    {label:'Вид',submenu:[{role:'reload',label:'Обновить'},{role:'toggleDevTools',label:'Инструменты разработчика'},{label:'Обычный размер',accelerator:'CmdOrCtrl+0',click:()=>changeZoom('reset')},{label:'Увеличить',accelerator:'CmdOrCtrl+Plus',click:()=>changeZoom('in')},{label:'Уменьшить',accelerator:'CmdOrCtrl+-',click:()=>changeZoom('out')},{role:'togglefullscreen',label:'Полный экран'}]},
    {label:'Справка',submenu:[{label:'Проверить обновления…',click:()=>{win.webContents.send('aigent:action','check-updates');checkUpdates(true).then(status=>{if(status.update_available)win.webContents.send('aigent:update',status);}).catch(()=>{});}},{label:'Открыть папку обновлений',click:()=>{fs.mkdirSync(updateDirectory,{recursive:true});shell.openPath(updateDirectory);}},{type:'separator'},{label:'GitHub',click:()=>shell.openExternal('https://github.com/eschota/AIGent')},{label:'Версия '+app.getVersion(),enabled:false}]}
  ]));
  let url=origin+'/';const c=config();if(c&&!c.admin_password)url+='#setup='+encodeURIComponent(c.setup_token);
  await win.loadURL(url);win.show();
  // Automatic check respects the saved setting; a failure stays silent and is visible in Settings.
  if(config()?.auto_update_check!==false)setTimeout(()=>{checkUpdates(false).then(status=>{if(status.update_available&&win)win.webContents.send('aigent:update',status);}).catch(()=>{});},4000).unref();
  setInterval(()=>authenticate().catch(()=>{}),60*60*1000).unref();
}).catch(error=>{dialog.showErrorBox('AIGent',error.message);app.quit();});
app.on('window-all-closed',()=>app.quit());
// The Python connector intentionally stays available for Telegram after closing the desktop window.
