'use strict';
const fs=require('node:fs');
const path=require('node:path');
const {execFileSync}=require('node:child_process');
const registryKey='HKCU\\Software\\AIGent';
function savedDataDirectory(){
  if(process.platform!=='win32')return null;
  try {
    const output=execFileSync('reg.exe',['query',registryKey,'/v','DataDir'],{encoding:'utf8',windowsHide:true,stdio:['ignore','pipe','ignore']});
    const value=output.match(/DataDir\s+REG_SZ\s+(.+)/)?.[1]?.trim();
    return value&&fs.existsSync(path.join(value,'config.json'))?value:null;
  }catch{return null;}
}
function shortcutOptions({packaged,executable,appDirectory,dataDirectory,icon}){
  return {target:executable,cwd:appDirectory,args:(packaged?'':`"${appDirectory}" `)+`--data-dir "${dataDirectory}"`,
    description:'AIGent — DeepSeek, Codex and Claude agent workspace',icon,iconIndex:0,appUserModelId:'org.eschota.aigent'};
}
function registerWindows({app,shell,dataDirectory}){
  if(process.platform!=='win32')return {registered:false,reason:'Windows only'};
  const location=path.join(app.getPath('appData'),'Microsoft','Windows','Start Menu','Programs','AIGent.lnk');
  // Development runs must not replace a valid installed app shortcut.
  if(!app.isPackaged&&fs.existsSync(location)){
    try{const old=shell.readShortcutLink(location);if(fs.existsSync(old.target)&&old.target!==process.execPath)return {registered:true,path:location,target:old.target};}catch{}
  }
  const appDirectory=app.isPackaged?path.dirname(process.env.PORTABLE_EXECUTABLE_FILE||process.execPath):__dirname;
  const executable=process.env.PORTABLE_EXECUTABLE_FILE||process.execPath;
  const options=shortcutOptions({packaged:app.isPackaged,executable,appDirectory,dataDirectory,icon:app.isPackaged?executable:path.join(__dirname,'assets','icon.ico')});
  fs.mkdirSync(path.dirname(location),{recursive:true});
  if(!shell.writeShortcutLink(location,'create',options))throw new Error('Cannot register AIGent in the Windows Start menu');
  execFileSync('reg.exe',['add',registryKey,'/v','DataDir','/t','REG_SZ','/d',dataDirectory,'/f'],{windowsHide:true,stdio:'ignore'});
  return {registered:true,path:location,...shell.readShortcutLink(location)};
}
module.exports={registerWindows,savedDataDirectory,shortcutOptions};
