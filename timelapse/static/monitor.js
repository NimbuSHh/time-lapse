'use strict';
const $ = id => document.getElementById(id);
const params=new URLSearchParams(location.hash.slice(1));
const monitorToken=params.get('token') || sessionStorage.getItem('timelapse-monitor-token') || '';
if(params.has('token')) { sessionStorage.setItem('timelapse-monitor-token',monitorToken); history.replaceState(null,'',location.pathname); }
async function update() {
  try {
    const response=await fetch('/api/progress',{headers:{'X-Timelapse-Token':monitorToken}});
    const task=await response.json(); if(!response.ok) throw Error(task.error || '无法读取进度');
    const view=TimelapseProgress.describe(task), complete=task.state==='done';
    $('percent').textContent=view.percent===null?'—':view.percent.toFixed(1)+'%';
    if(view.percent===null) $('overall').removeAttribute('value'); else $('overall').value=view.percent;
    $('note').textContent=view.note;
    $('phase').textContent=view.phase;
    $('frame').textContent=view.rendering?`本阶段 ${task.done} / ${task.total} 帧 · 全序列 ${task.count} 帧`:`全序列 ${task.count} 帧`;
    const age=Math.max(0,task.observed_at-task.updated_at);
    $('state').textContent=complete?'处理完成':task.state==='error'?'出现错误':task.state==='stopped'?'已停止':age>360?'记录暂未更新':'正在处理';
    document.querySelector('h1').textContent=complete?'自动调色已完成':task.state==='error'?'自动调色需要处理':task.state==='stopped'?'自动调色已停止':'自动调色正在进行';
    $('freshness').textContent=`任务记录更新于 ${new Date(task.updated_at*1000).toLocaleTimeString('zh-CN',{hour12:false})}`+(age>360&&!complete?' · 已超过 6 分钟未更新，需结合后台确认运行状态':'');
    $('error').hidden=!task.error; $('error').textContent=task.error || '';
    $('stages').replaceChildren(...view.stages.map((stage,index)=>{
      const row=document.createElement('li'); if(stage.status==='当前阶段') row.setAttribute('aria-current','step');
      const name=document.createElement('span'), status=document.createElement('small');
      name.textContent=stage.title; status.textContent=stage.status; row.append(name,status); return row;
    }));
    $('result').hidden=!complete;
    if(complete) { $('outcome').textContent=task.result?.message || ''; $('resultPath').textContent=task.result?.path || ''; return; }
  } catch(error) {
    $('state').textContent='进度暂不可用'; $('error').hidden=false; $('error').textContent='读取失败，不代表处理任务停止：'+error.message;
  }
  setTimeout(update,2000);
}
void update();
