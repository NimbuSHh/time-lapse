'use strict';
const $ = id => document.getElementById(id);
let savedToken = '';
try { savedToken = sessionStorage.getItem('timelapse-token') || ''; } catch (_) {}
const token = new URLSearchParams(location.hash.slice(1)).get('token') || savedToken;
try { if (token) sessionStorage.setItem('timelapse-token', token); } catch (_) {}
history.replaceState(null, '', '/refine');
let busy = false, sending = false, stopRequested = false;

async function request(path, data) {
  const response = await fetch(path, {
    method: data === undefined ? 'GET' : 'POST',
    headers: {'X-Timelapse-Token': token, 'Content-Type': 'application/json'},
    body: data === undefined ? undefined : JSON.stringify(data)
  });
  const value = await response.json();
  if (!response.ok) throw Error(value.error || '操作失败');
  return value;
}

function controls() {
  $('start').disabled = !token || busy || sending;
  $('stop').disabled = !busy || sending || stopRequested;
}

function error(message) {
  $('error').textContent = message || '';
  $('error').hidden = !message;
}

function show({task, job}) {
  busy = !!job;
  if (!busy) stopRequested = false;
  const state = task?.state || '';
  const result = task?.result;
  const verified = !busy && ['done', 'complete', 'completed', 'verified', 'passed'].includes(state) && result?.verified === true;
  const names = {running: '正在处理', paused: '已停止，进度已保留', cancelled: '已停止，进度已保留',
    error: '处理遇到问题', failed: '处理遇到问题', candidate: '候选结果，未通过验收',
    'review-needed': '需要复核，尚未通过验收', blocked: '验证未通过，已保留诊断'};
  $('state').textContent = verified ? '已通过验收' : busy ? '正在处理' :
    names[state] || (task ? '当前任务已保留' : '准备继续已有任务');
  $('phase').textContent = task?.phase || '四段小样验证，通过后自动继续810帧';
  const done = Number(task?.done), total = Number(task?.total);
  if (Number.isFinite(done) && Number.isFinite(total) && total > 0) {
    $('progress').max = total;
    $('progress').value = Math.max(0, Math.min(done, total));
    $('count').textContent = `${done} / ${total}`;
  } else {
    $('progress').removeAttribute('value');
    $('count').textContent = '';
  }
  $('message').textContent = stopRequested ? '正在停止，已完成的进度会保留。' :
    task?.message || (busy ? '程序会按验证结果自动继续，切换阶段不会重新开始整个任务。' : '可从已有结果开始或继续。');
  error(task?.error);
  $('resultPanel').hidden = !result;
  if (result) {
    $('resultTitle').textContent = verified ? '验收结果' : '当前结果与诊断';
    $('outcome').textContent = result.message || (verified ? '已通过本次验收。' : '本结果尚不能视为验收通过。');
    const details = [];
    if (result.path) details.push('结果位置：' + result.path);
    if (result.count !== undefined) details.push('帧数：' + result.count);
    if (result.rounds !== undefined) details.push('修正轮数：' + result.rounds);
    if (result.reason) details.push('说明：' + result.reason);
    if (result.report) details.push('检查报告：' + result.report);
    $('result').textContent = details.join('\n');
  }
  controls();
}

async function refresh() { show(await request('/api/refine/status')); }

$('start').addEventListener('click', async () => {
  sending = true; error(''); controls();
  try { await request('/api/refine/start', {}); stopRequested = false; await refresh(); }
  catch (e) { error(e.message); }
  finally { sending = false; controls(); }
});
$('stop').addEventListener('click', async () => {
  sending = true; controls();
  try { await request('/api/refine/cancel', {}); stopRequested = true; await refresh(); }
  catch (e) { error(e.message); }
  finally { sending = false; controls(); }
});

async function poll() {
  try { await refresh(); }
  catch (e) { error(e.message); controls(); }
  finally { setTimeout(poll, 1500); }
}
if (token) poll();
else { $('state').textContent = '需要本机会话'; error('请从含会话密钥的本地启动链接打开此页面。'); }
