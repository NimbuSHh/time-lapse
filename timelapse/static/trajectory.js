'use strict';
const $=id=>document.getElementById(id);
let stored='';try{stored=sessionStorage.getItem('timelapse-token')||'';}catch(_){}
const token=new URLSearchParams(location.hash.slice(1)).get('token')||stored;
try{if(token)sessionStorage.setItem('timelapse-token',token);}catch(_){}
history.replaceState(null,'','/trajectory');
let busy=false,sending=false,waiting=false,blobURL='',result=null,resultKey='',loadedKey='',loading=false,hasTask=false,supported=false;
async function request(path,data){const r=await fetch(path,{method:data===undefined?'GET':'POST',headers:{'X-Timelapse-Token':token,'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)});const v=await r.json();if(!r.ok)throw Error(v.error||'操作失败');return v;}
function failure(msg){$('error').textContent=msg||'';$('error').hidden=!msg;}
function identity(r){return r?JSON.stringify([r.run_id,r.sha256,r.selection_sha256,r.target_sha256,r.effective_policy_sha256]):'';}
function controls(){
  $('start').hidden=waiting||result?.state==='done';$('start').disabled=!token||!supported||busy||sending||!hasTask;
  $('stop').hidden=!busy;$('stop').disabled=sending;
  const r=result, complete=r?.state==='done';
  const bound=r&&[r.run_id,r.sha256,r.selection_sha256,r.target_sha256,r.effective_policy_sha256].every(Boolean);
  const reasons=[];
  if(r?.export_available===false)reasons.push(r.export_block_reason||'当前预览未通过导出兼容性检查，暂不能导出。请查看任务说明。');
  if(complete)reasons.push('全尺寸结果已保存，无需重复确认。交付使用这版预览对应的配方，请检查全尺寸细节。');
  else{
    if(!supported)reasons.push('当前后台尚不支持此确认流程，请重新启动工作台并使用新页面。');
    if(busy||sending)reasons.push('任务仍在处理，完成或停止后才能确认导出。');
    if(!r)reasons.push('尚无完整预览，请先生成视频。');
    else{
      if(!bound)reasons.push('预览缺少任务或版本校验信息，暂不能确认导出。请刷新以读取兼容后的任务状态；仍不可用时查看任务说明。');
      if(r.numeric_conditions_met!==true)reasons.push('这版视频的数值检查尚未通过，可播放检查效果；正式全尺寸导出暂不可用。');
      if(loading)reasons.push('正在载入并校验当前视频，请稍候。');
      else if(loadedKey!==identity(r))reasons.push('尚未载入当前版本视频，请点击“载入完整视频”，完整观看后再确认。');
    }
  }
  $('reviewed').disabled=reasons.length>0;
  $('export').disabled=$('reviewed').disabled||!$('reviewed').checked;
  $('export').textContent=complete?'全尺寸结果已保存':'确认并导出全尺寸 JPG 与 XMP';
  $('loadVideo').disabled=busy||sending||loading;
  $('reviewNote').textContent=reasons.length?reasons.join(' '):$('reviewed').checked?'已确认这版视频。点击“确认并导出全尺寸 JPG 与 XMP”开始交付。':'当前视频已载入且数值检查通过。完整观看并确认画面效果后，请勾选下方确认框，才可导出全尺寸。';
  $('reviewed').setAttribute('aria-describedby','reviewNote');
  $('export').setAttribute('aria-describedby','reviewNote');
}
function resetVideo(){
  $('video').pause();$('video').removeAttribute('src');$('video').load();$('video').hidden=true;
  if(blobURL)URL.revokeObjectURL(blobURL);blobURL='';loadedKey='';$('reviewed').checked=false;$('loadVideo').hidden=false;
}
function show({task,job}){
  hasTask=!!task;busy=!!job;waiting=task?.state==='awaiting-user-review';
  const names={error:'处理遇到问题',stopped:'已停止，进度保留',ready:'已准备',done:'全尺寸结果已保存'};
  $('state').textContent=busy?'正在处理':waiting?'等待完整视频确认':names[task?.state]||(task?'准备生成完整预览':'请先到工作台选择素材');
  $('phase').textContent=task?.phase||'新素材可直接从工作台开始，无需已有完整任务。';
  const done=Number(task?.done),total=Number(task?.total);
  if(total>0){$('progress').max=total;$('progress').value=done||0;$('count').textContent=`${done||0} / ${total}`;}
  else{$('progress').removeAttribute('value');$('count').textContent='';}
  $('progress').hidden=!busy;
  $('message').textContent=waiting?'请完整播放并确认这版视频，再开始全尺寸导出。':task?.result?.message||'仅处理独立工作副本。';failure(task?.error);
  result=task?.result?.video?task.result:null;
  const key=identity(result);if(key!==resultKey){resetVideo();resultKey=key;}
  $('resultPanel').hidden=!result;
  if(result){
    const r=result,delivered=r.state==='done'&&r.delivery;
    $('resultHeading').textContent=delivered?'全尺寸结果已保存':'完整预览视频';
    $('outcome').textContent=r.message||'';
    $('videoSummary').textContent=`${r.count} 帧 · ${r.width} × ${r.height} · ${r.fps} fps${Number.isFinite(r.duration_seconds)?' · '+r.duration_seconds.toFixed(1)+' 秒':''}`;
    $('delivery').textContent=delivered?`${r.delivery.count} 帧 JPG 与对应 XMP 已保存至：${r.delivery.path}`:'';
  }
  controls();
}
async function refresh(){show(await request('/api/trajectory/status'));}
$('start').onclick=async()=>{sending=true;controls();failure('');try{await request('/api/trajectory/start',{});await refresh();}catch(e){failure(e.message);}finally{sending=false;controls();}};
$('stop').onclick=async()=>{sending=true;controls();try{await request('/api/trajectory/cancel',{});$('message').textContent='当前处理结束后停止，保留已完成结果。';}catch(e){failure(e.message);}finally{sending=false;controls();}};
$('loadVideo').onclick=async()=>{
  const current=result,key=identity(current);loading=true;controls();
  try{
    const r=await fetch('/api/trajectory/video',{headers:{'X-Timelapse-Token':token}});if(!r.ok)throw Error((await r.json()).error||'视频读取失败');
    const blob=await r.blob(),digest=await crypto.subtle.digest('SHA-256',await blob.arrayBuffer());
    const hash=[...new Uint8Array(digest)].map(n=>n.toString(16).padStart(2,'0')).join('');
    if(hash!==current.sha256)throw Error('视频版本已变化，请刷新任务后重新载入并确认。');
    if(key!==identity(result))return;
    resetVideo();blobURL=URL.createObjectURL(blob);loadedKey=key;$('video').src=blobURL;$('video').hidden=false;$('loadVideo').hidden=true;
  }catch(e){failure(e.message);}finally{loading=false;controls();}
};
$('reviewed').onchange=controls;
$('export').onclick=async()=>{
  if($('export').disabled)return;
  const r=result;sending=true;$('reviewed').checked=false;controls();failure('');
  try{await request('/api/trajectory/export',{confirmed:true,run_id:r.run_id,video_sha256:r.sha256,selection_sha256:r.selection_sha256,target_sha256:r.target_sha256,effective_policy_sha256:r.effective_policy_sha256});await refresh();}
  catch(e){failure(e.message);}finally{sending=false;controls();}
};
async function poll(){try{await refresh();}catch(e){failure(e.message);}finally{setTimeout(poll,2000);}}
if(token)void(async()=>{
  try{const config=await request('/api/config');if(config.automatic_api_version!==2||config.trajectory_api_version!==2)throw Error('请重新启动工作台并使用新页面，当前后台不支持视频确认后导出的新流程。');supported=true;await poll();}
  catch(e){failure(e.message);controls();}
})();else{failure('请从本机启动链接打开此页面。');controls();}
