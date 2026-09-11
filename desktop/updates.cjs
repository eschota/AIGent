// Desktop update helper: pure version/asset logic plus staging of a downloaded build.
// Only the public GitHub release page and the local connector are contacted.
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');

const FILES={
  installer:/^AIGent-Setup-(\d+(?:\.\d+)*)\.exe$/i,
  portable:/^AIGent-(\d+(?:\.\d+)*)-portable\.exe$/i
};

function parseVersion(text){
  const match=/^[vV]?(\d+(?:\.\d+)*)/.exec(String(text??'').trim());
  return match?match[1].split('.').map(Number):[];
}

function isNewer(candidate,current){
  const latest=parseVersion(candidate),running=parseVersion(current);
  if(!latest.length||!running.length)return false;
  const width=Math.max(latest.length,running.length);
  for(let i=0;i<width;i++){
    const left=latest[i]||0,right=running[i]||0;
    if(left!==right)return left>right;
  }
  return false;
}

// Mirrors connector/updates.pick_asset: the newest matching file for this platform.
function pickAsset(assets,kind='installer'){
  const pattern=FILES[kind];
  if(!pattern)throw new Error('Unknown update file kind: '+kind);
  let best=null;
  for(const asset of assets||[]){
    const match=pattern.exec(String(asset?.name||''));
    if(match&&(!best||isNewer(match[1],best.version)))best={version:match[1],asset};
  }
  return best?best.asset:null;
}

// The portable build cannot replace itself while it runs, so the mode changes the advice shown.
function portableMode(environment=process.env){
  return Boolean(environment.PORTABLE_EXECUTABLE_FILE||environment.PORTABLE_EXECUTABLE_DIR);
}

async function requestStatus({origin,token,force=false,fetchImpl=fetch}){
  if(!token)return {ok:false,error:'Connector token is unavailable'};
  try{
    const response=await fetchImpl(origin+'/api/update'+(force?'?force=true':''),{headers:{Authorization:'Bearer '+token},signal:AbortSignal.timeout(20000)});
    if(!response.ok)return {ok:false,error:'Server responded HTTP '+response.status};
    return {ok:true,status:await response.json()};
  }catch(error){
    return {ok:false,error:'Update check failed: '+error.message};
  }
}

function digestOf(file){
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

// Downloads one release file into <data>/update and verifies the published digest when present.
async function stage({asset,directory,fetchImpl=fetch,onProgress=()=>{}}){
  if(!asset?.url)throw new Error('This release has no downloadable file');
  fs.mkdirSync(directory,{recursive:true});
  const target=path.join(directory,path.basename(String(asset.name)));
  const partial=target+'.part';
  const response=await fetchImpl(asset.url,{signal:AbortSignal.timeout(30*60*1000)});
  if(!response.ok)throw new Error('Download failed: HTTP '+response.status);
  const expected=Number(asset.size)||0;
  const total=Number(response.headers?.get?.('content-length'))||expected;
  let written=0;
  const handle=fs.createWriteStream(partial);
  try{
    for await(const chunk of response.body){
      written+=chunk.length;
      if(!handle.write(chunk))await new Promise(resolve=>handle.once('drain',resolve));
      onProgress(total?written/total:0);
    }
  }finally{
    await new Promise(resolve=>handle.end(resolve));
  }
  if(expected&&written!==expected)throw new Error(`Incomplete download: ${written} of ${expected} bytes`);
  const actual=digestOf(partial);
  if(asset.sha256&&actual.toLowerCase()!==String(asset.sha256).toLowerCase()){
    fs.rmSync(partial,{force:true});
    throw new Error('Downloaded file digest does not match the release');
  }
  fs.renameSync(partial,target);
  return {path:target,bytes:written,sha256:actual,verified:Boolean(asset.sha256)};
}

module.exports={FILES,parseVersion,isNewer,pickAsset,portableMode,requestStatus,digestOf,stage};
