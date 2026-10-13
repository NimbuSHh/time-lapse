'use strict';
let automaticOutputOverride=null, automaticResult=null, automaticVideoURL='', automaticVideoKey='', automaticLoadedKey='', automaticLoading=false, automaticSupported=false;
let automaticSettingsChanged=false, automaticReference=null, automaticReferenceURL='', automaticReferenceEpoch=0, automaticContract=null, automaticDrag=null, automaticReferenceSource='';
const autoRegionKeys=['primary','secondary','check_a','check_b'];
function autoMessage(text,error=false){$('autoStatus').textContent=text||'';$('autoStatus').classList.toggle('joint-error',error);}
function autoDirectory(){
  const raw=$('autoRaw').value.trim().replace(/\/+$/,'');
  const base=raw.replace(/\/Time-lapse工作(?:\/.*)?$/,'');
  $('autoOutput').textContent=automaticOutputOverride||(base?base+'/Time-lapse工作/':'所选文件夹 / Time-lapse工作/');
  $('autoStart').disabled=!raw||!automaticSupported;
  try{sessionStorage.setItem('timelapse-auto-raw',raw);}catch(_){}
}
function autoIdentity(r){return r?JSON.stringify([r.run_id,r.sha256,r.selection_sha256,r.target_sha256,r.effective_policy_sha256]):'';}
function autoReviewControls(){
  const r=automaticResult, complete=r?.state==='done'&&r?.delivery;
  const bound=r&&[r.run_id,r.sha256,r.selection_sha256,r.target_sha256,r.effective_policy_sha256].every(Boolean);
  const reasons=[];
  if(r?.export_available===false)reasons.push(r.export_block_reason||'当前预览未通过导出兼容性检查，暂不能导出。请查看任务说明。');
  if(complete)reasons.push(`已交付 ${r.delivery.count} 帧 JPG 与对应 XMP，无需重复确认。建议检查全尺寸细节。`);
  else{
    if(!automaticSupported)reasons.push('当前后台尚不支持此确认流程，请重新启动工作台并使用新页面。');
    if(autoBusy)reasons.push('任务仍在处理，完成或停止后才能确认导出。');
    if(automaticSettingsChanged)reasons.push('测量区域已修改，请重新生成完整预览，再观看确认这版设置。');
    if(!r)reasons.push('尚无完整预览，请先生成视频。');
    else{
      if(!bound)reasons.push('预览缺少任务或版本校验信息，暂不能确认导出。请刷新以读取兼容后的任务状态；仍不可用时查看任务说明。');
      if(r.numeric_conditions_met!==true)reasons.push('这版视频的数值检查尚未通过，可播放检查效果；正式全尺寸导出暂不可用。');
      if(automaticLoading)reasons.push('正在载入并校验当前视频，请稍候。');
      else if(automaticLoadedKey!==autoIdentity(r))reasons.push('尚未载入当前版本视频，请点击“载入完整视频”，完整观看后再确认。');
    }
  }
  $('autoReviewed').disabled=reasons.length>0;
  $('autoExport').disabled=$('autoReviewed').disabled||!$('autoReviewed').checked;
  $('autoExport').textContent=complete?'全尺寸结果已保存':'确认并导出全尺寸 JPG 与 XMP';
  $('autoLoadVideo').disabled=automaticLoading||autoBusy;
  $('autoReviewNote').textContent=reasons.length?reasons.join(' '):$('autoReviewed').checked?'已确认这版视频。点击“确认并导出全尺寸 JPG 与 XMP”开始交付。':'当前视频已载入且数值检查通过。完整观看并确认画面效果后，请勾选下方确认框，才可导出全尺寸。';
  $('autoReviewed').setAttribute('aria-describedby','autoReviewNote');
  $('autoExport').setAttribute('aria-describedby','autoReviewNote');
}
function autoRunning(value){
  autoBusy=value;$('autoControls').disabled=value;
  $('manualFlow').inert=value;$('manualWorkspace').inert=value;
  $('autoCancel').hidden=!value;$('autoCancel').disabled=false;$('autoCancel').textContent='停止并保留进度';
  if(value)$('autoProgressPanel').hidden=false;
  autoReviewControls();
}
function autoResetVideo(){
  $('autoVideo').pause();$('autoVideo').removeAttribute('src');$('autoVideo').load();$('autoVideo').hidden=true;
  if(automaticVideoURL)URL.revokeObjectURL(automaticVideoURL);
  automaticVideoURL='';automaticLoadedKey='';$('autoReviewed').checked=false;$('autoLoadVideo').hidden=false;
}
function autoClearResult(){
  automaticResult=null;automaticVideoKey='';autoResetVideo();
  $('autoResult').hidden=true;$('autoViewer').hidden=true;$('autoEmpty').hidden=false;
  $('autoHeading').textContent='先看完整视频，再确认导出';$('autoBadge').textContent='本机 Photoshop 显影';
  autoReviewControls();
}
function autoRegionChanged(){
  automaticSettingsChanged=true;$('autoReviewed').checked=false;autoReviewControls();autoDrawReference();
  if(automaticResult)autoMessage('测量区域已修改。请重新生成完整预览，再观看确认这版设置。');
}
function autoProgressState(task){
  $('autoProgressPanel').hidden=false;
  // These stages vary by scene; the current stage is measurable, overall remaining time is not.
  const total=Number(task.total),done=Number(task.done);
  $('autoProgress').max=total>0?total:1;
  if(total>0){$('autoProgress').value=done||0;$('autoTotalProgress').textContent=`${done||0} / ${total}`;}
  else{$('autoProgress').removeAttribute('value');$('autoTotalProgress').textContent='处理中';}
  $('autoStage').textContent='当前阶段：'+(task.phase||'准备任务');
  $('autoProgressNote').textContent='按当前阶段显示进度；生成完整视频后等待你确认。';
}
function showAutomatic(result){
  if(!result?.video)return;
  const key=autoIdentity(result);if(key!==automaticVideoKey){autoResetVideo();automaticVideoKey=key;}
  automaticResult=result;
  const delivered=result.state==='done'&&result.delivery;
  $('autoResult').hidden=false;$('autoEmpty').hidden=true;$('autoViewer').hidden=false;
  $('autoHeading').textContent=delivered?'全尺寸结果已保存':'完整视频已生成，等待观看确认';
  $('autoOutcome').textContent=result.message||'';
  $('autoBadge').textContent=`${result.count} 帧 · ${result.fps} fps`;
  $('autoResultPath').textContent=delivered?result.delivery.path:result.video;
  $('autoCopy').textContent=delivered?'复制交付目录':'复制视频路径';
  $('autoVideoSummary').textContent=`${result.count} 帧 · ${result.width} × ${result.height} · ${result.fps} fps${Number.isFinite(result.duration_seconds)?' · '+result.duration_seconds.toFixed(1)+' 秒':''}`;
  const offset=result.effective_tone_policy?.sky_offset_ev;
  $('autoRegionsSummary').textContent=(result.region_contract?'本次测量区域已冻结并与视频绑定；更改区域后需生成并确认新预览。':'')+(Number.isFinite(offset)?` 全片亮部对比偏移：${offset>=0?'+':''}${offset.toFixed(3)} EV。`:'');
  $('autoStart').textContent='查看 / 继续当前任务';autoReviewControls();
}
function autoShowState(state){
  const task=state.task;
  autoRunning(!!state.job);
  if(!task){if(automaticResult)autoClearResult();return;}
  automaticReferenceSource=task.raw_dir||'';
  automaticContract=task.region_contract||task.result?.region_contract||null;
  if(task.result?.video)showAutomatic(task.result);
  else if(automaticResult)autoClearResult();
  autoDrawReference();
  if(state.job){autoProgressState(task);autoMessage('程序正在处理，已完成的进度会保留。');}
  else{
    $('autoProgressPanel').hidden=true;
    autoMessage(task.error||task.result?.message||'上次任务尚未完成，点击继续即可接着处理。',task.state==='error');
    if(task.state==='error'||task.state==='stopped')$('autoStart').textContent='继续自动处理';
  }
}
async function followAutomatic(){
  for(;;){const state=await request('/api/auto/status');autoShowState(state);if(!state.job)return;await new Promise(resolve=>setTimeout(resolve,1000));}
}
function autoReadRegions(){
  if(!$('autoManualRegions').checked)return undefined;
  const regions={};
  for(const key of autoRegionKeys){
    const fields=[...document.querySelectorAll(`[data-roi="${key}"]`)];
    const values=fields.map(input=>input.value.trim()===''?NaN:Number(input.value));
    if(values.some(n=>!Number.isFinite(n)||n<0||n>100)||values[0]>=values[2]||values[1]>=values[3])throw Error('请为四个区域填写 0–100 的百分比，且左小于右、上小于下。');
    regions[key]=values.map(n=>n/100);
  }
  for(let i=0;i<autoRegionKeys.length;i++)for(let j=i+1;j<autoRegionKeys.length;j++){
    const a=regions[autoRegionKeys[i]],b=regions[autoRegionKeys[j]];
    if(Math.min(a[2],b[2])>Math.max(a[0],b[0])&&Math.min(a[3],b[3])>Math.max(a[1],b[1]))throw Error('四个测量区域不能重叠，请在参考画面中调整框选。');
  }
  return regions;
}
$('autoManualRegions').addEventListener('change',()=>{
  $('autoRegions').hidden=!$('autoManualRegions').checked;
  for(const field of document.querySelectorAll('[data-roi]'))field.required=$('autoManualRegions').checked;
  autoRegionChanged();
});
for(const field of document.querySelectorAll('[data-roi]'))field.addEventListener('input',autoRegionChanged);
$('autoForm').addEventListener('submit',async event=>{
  event.preventDefault();if(autoBusy||busy||!automaticSupported)return;
  try{
    autoRunning(true);
    const config=await request('/api/config');
    if(config.automatic_api_version!==2||config.trajectory_api_version!==2)throw Error('请重新启动工作台并使用新页面，当前后台不支持视频确认后导出的新流程。');
    const regions=autoReadRegions();autoRunning(true);autoMessage('正在准备素材与本机显影环境…');
    const started=await request('/api/auto/start',{raw_dir:$('autoRaw').value.trim(),...(regions?{regions}:{})});
    autoClearResult();autoClearReference();automaticReferenceSource=$('autoRaw').value.trim();automaticSettingsChanged=false;
    if(started.output_root)$('autoOutput').textContent=started.output_root+'/';
    $('autoReviewed').checked=false;
    await followAutomatic();
  }catch(e){autoMessage(e.message,true);autoRunning(false);}
});
$('autoCancel').addEventListener('click',async()=>{
  try{await request('/api/auto/cancel',{});$('autoCancel').disabled=true;$('autoCancel').textContent='正在停止…';autoMessage('已请求停止，正在保存可继续的进度。');}catch(e){autoMessage(e.message,true);}
});
$('autoLoadVideo').addEventListener('click',async()=>{
  const result=automaticResult,key=autoIdentity(result);if(!result)return;
  automaticLoading=true;autoReviewControls();
  try{
    const response=await fetch('/api/trajectory/video',{headers:{'X-Timelapse-Token':token}});
    if(!response.ok)throw Error((await response.json()).error||'视频读取失败');
    const blob=await response.blob();
    const digest=await crypto.subtle.digest('SHA-256',await blob.arrayBuffer());
    const hash=[...new Uint8Array(digest)].map(n=>n.toString(16).padStart(2,'0')).join('');
    if(hash!==result.sha256)throw Error('视频版本已变化，请刷新任务后重新载入并确认。');
    if(key!==autoIdentity(automaticResult))return;
    autoResetVideo();automaticVideoURL=URL.createObjectURL(blob);automaticLoadedKey=key;
    $('autoVideo').src=automaticVideoURL;$('autoVideo').hidden=false;$('autoLoadVideo').hidden=true;
  }catch(e){autoMessage(e.message,true);}finally{automaticLoading=false;autoReviewControls();}
});
$('autoReviewed').addEventListener('change',autoReviewControls);
$('autoExport').addEventListener('click',async()=>{
  if($('autoExport').disabled||busy)return;
  const r=automaticResult;autoRunning(true);$('autoReviewed').checked=false;
  try{
    await request('/api/trajectory/export',{confirmed:true,run_id:r.run_id,video_sha256:r.sha256,selection_sha256:r.selection_sha256,target_sha256:r.target_sha256,effective_policy_sha256:r.effective_policy_sha256});
    await followAutomatic();
  }catch(e){autoMessage(e.message,true);autoRunning(false);}
});
$('autoRaw').addEventListener('input',()=>{
  autoClearResult();automaticSettingsChanged=false;autoClearReference();$('autoProgressPanel').hidden=true;
  $('autoManualRegions').checked=false;$('autoRegions').hidden=true;
  for(const field of document.querySelectorAll('[data-roi]')){field.value='';field.required=false;}
  $('autoHeading').textContent='先看完整视频，再确认导出';$('autoBadge').textContent='本机 Photoshop 显影';$('autoStart').textContent='开始自动调色';
  autoDirectory();autoMessage('无需预先导出 JPG。所有结果保存到上方工作目录。');autoReviewControls();
});
function autoClearReference(){
  ++automaticReferenceEpoch;automaticReference=null;automaticContract=null;automaticDrag=null;
  if(automaticReferenceURL)URL.revokeObjectURL(automaticReferenceURL);automaticReferenceURL='';
  $('autoReferenceTools').hidden=true;$('autoLoadReference').textContent='载入原始参考';
}
function autoDrawReference(){
  if(!automaticReference)return;
  const canvas=$('autoReferenceCanvas'),ctx=canvas.getContext('2d'),w=canvas.width,h=canvas.height;
  ctx.clearRect(0,0,w,h);ctx.drawImage(automaticReference,0,0,w,h);
  const colors=['#ffcf5c','#8eee9d','#72c6ff','#ff94d3'];
  autoRegionKeys.forEach((key,index)=>{
    ctx.strokeStyle=colors[index];ctx.fillStyle=colors[index];ctx.lineWidth=Math.max(2,w/500);
    let rectangles=[];
    if($('autoManualRegions').checked){
      const fields=[...document.querySelectorAll(`[data-roi="${key}"]`)];
      const v=fields.map(f=>f.value.trim()===''?NaN:Number(f.value)/100);
      if(v.every(Number.isFinite)&&v[0]<v[2]&&v[1]<v[3])rectangles=[v];
    }else if(automaticContract?.mode==='manual')rectangles=[automaticContract.rois[key]];
    else if(automaticContract?.mode==='automatic'){
      const {rows,cols,tiles}=automaticContract;
      rectangles=(tiles[key]||[]).map(i=>[(i%cols)/cols,Math.floor(i/cols)/rows,(i%cols+1)/cols,(Math.floor(i/cols)+1)/rows]);
    }
    for(const r of rectangles){if(!r)continue;const x=r[0]*w,y=r[1]*h,rw=(r[2]-r[0])*w,rh=(r[3]-r[1])*h;ctx.globalAlpha=.18;ctx.fillRect(x,y,rw,rh);ctx.globalAlpha=1;ctx.strokeRect(x,y,rw,rh);}
  });
  $('autoReferenceNote').textContent=$('autoManualRegions').checked?'拖动框选会更新左侧坐标；更改后需重新生成并确认视频。':'黄色：亮部；绿色：参考；蓝色：检查 A；粉色：检查 B。区域在整段序列中固定。';
}
$('autoLoadReference').addEventListener('click',async()=>{
  if(!automaticReferenceSource||automaticReferenceSource!==$('autoRaw').value.trim()){autoMessage('请先开始当前素材的自动处理，初始显影完成后即可查看原始参考。');return;}
  const button=$('autoLoadReference'),epoch=automaticReferenceEpoch;
  const index=Number($('autoReferenceIndex').value||1)-1;
  if(!Number.isInteger(index)||index<0){autoMessage('参考帧序号应为大于 0 的整数。',true);return;}
  button.disabled=true;
  try{
    const response=await fetch('/api/trajectory/reference?index='+index,{headers:{'X-Timelapse-Token':token}});
    if(!response.ok)throw Error((await response.json()).error||'初始参考尚未生成，请在初始显影完成后重试。');
    const url=URL.createObjectURL(await response.blob()),img=new Image();
    try{img.src=url;await img.decode();}catch(e){URL.revokeObjectURL(url);throw e;}
    if(epoch!==automaticReferenceEpoch){URL.revokeObjectURL(url);return;}
    if(automaticReferenceURL)URL.revokeObjectURL(automaticReferenceURL);
    automaticReferenceURL=url;automaticReference=img;
    const canvas=$('autoReferenceCanvas');canvas.width=img.naturalWidth;canvas.height=img.naturalHeight;
    $('autoReferenceTools').hidden=false;button.textContent='载入所选参考帧';autoDrawReference();
  }catch(e){autoMessage(e.message,true);}finally{button.disabled=false;}
});
function autoReferencePoint(event){const rect=$('autoReferenceCanvas').getBoundingClientRect();return [Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width)),Math.max(0,Math.min(1,(event.clientY-rect.top)/rect.height))];}
$('autoReferenceCanvas').addEventListener('pointerdown',event=>{
  if(autoBusy||!$('autoManualRegions').checked||!automaticReference)return;
  automaticDrag={start:autoReferencePoint(event),key:$('autoReferenceRegion').value};$('autoReferenceCanvas').setPointerCapture(event.pointerId);event.preventDefault();
});
$('autoReferenceCanvas').addEventListener('pointermove',event=>{
  if(!automaticDrag||autoBusy)return;
  const end=autoReferencePoint(event),start=automaticDrag.start;
  const values=[Math.min(start[0],end[0]),Math.min(start[1],end[1]),Math.max(start[0],end[0]),Math.max(start[1],end[1])];
  for(const field of document.querySelectorAll(`[data-roi="${automaticDrag.key}"]`))field.value=(values[Number(field.dataset.coordinate)]*100).toFixed(1);
  autoRegionChanged();
});
for(const event of ['pointerup','pointercancel','lostpointercapture'])$('autoReferenceCanvas').addEventListener(event,()=>{automaticDrag=null;});
$('manualFlow').addEventListener('toggle',()=>{$('manualWorkspace').hidden=!$('manualFlow').open;$('autoWorkspace').hidden=$('manualFlow').open;if($('manualFlow').open){$('autoVideo').pause();draw();drawROI();}});
$('autoCopy').addEventListener('click',()=>copyJointPath($('autoResultPath').textContent));
try{$('autoRaw').value=sessionStorage.getItem('timelapse-auto-raw')||$('raw').value;}catch(_){}
autoDirectory();
if(token)void(async()=>{
  try{
    const config=await request('/api/config');
    if(config.automatic_api_version!==2||config.trajectory_api_version!==2)throw Error('请重新启动工作台并使用新页面，当前后台不支持视频确认后导出的新流程。');
    automaticSupported=true;
    automaticOutputOverride=config.workspace_explicit?config.output_root+'/':null;
    const state=await request('/api/auto/status');
    if(state.task?.raw_dir)$('autoRaw').value=state.task.raw_dir;
    if(state.task?.regions){
      $('autoManualRegions').checked=true;$('autoRegions').hidden=false;
      for(const field of document.querySelectorAll('[data-roi]')){
        const value=state.task.regions[field.dataset.roi]?.[Number(field.dataset.coordinate)];
        if(Number.isFinite(value))field.value=String(Math.round(value*1000)/10);
        field.required=true;
      }
    }
    autoDirectory();autoShowState(state);if(state.job)await followAutomatic();
  }catch(e){autoMessage(e.message,true);autoRunning(false);}
})();
