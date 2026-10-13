'use strict';
const $ = id => document.getElementById(id);
const token = new URLSearchParams(location.hash.slice(1)).get('token') || sessionStorage.getItem('timelapse-token') || '';
if (token) sessionStorage.setItem('timelapse-token', token);
history.replaceState(null, '', '/');
let project = null, result = null, exported = null, actual = null, playing = false, imageRequest = 0, objectURL = null, busy = false;
let measurement = null, measurementCell = null, measurementPicture = null;
let exposureCalibration = null;
let autoBusy = false;
let verificationFailure = '', analysisGeneration = 0, verificationReview = null;
function status(text, error = false) {
  $('status').textContent = text; $('status').classList.toggle('error', error);
  if ($('jointDialog')?.open) $('jointNext').textContent = text;
}
async function request(path, data) {
  const response = await fetch(path, {method: data === undefined ? 'GET' : 'POST', headers: {'X-Timelapse-Token': token, 'Content-Type': 'application/json'}, body: data === undefined ? undefined : JSON.stringify(data)});
  const value = await response.json();
  if (!response.ok) throw Error(value.error || '操作失败');
  return value;
}
async function job(path, data) {
  if (path.startsWith('/api/joint/')) {
    const config = await request('/api/config');
    if (config.joint_api_version !== 1) {
      throw Error('当前后台不支持联合调色，请重新启动工作台并使用新页面。已有修正包不受影响。');
    }
  }
  const started = await request(path, data);
  for (;;) {
    await new Promise(resolve => setTimeout(resolve, 350));
    const current = await request(`/api/job?id=${started.job}`);
    if (current.state === 'error') {
      if (path === '/api/analyze' && current.measurement) renderMeasurement(current.measurement);
      throw Error(current.error);
    }
    if (current.state === 'done') return current.result;
    if (current.progress) {
      if (current.phase === 'finalize') { status('照片读取完成，正在汇总亮度与诊断…'); continue; }
      const phase = current.phase === 'analysis' ? '正在分析' : '正在处理';
      status(`${phase}：${current.progress[0]} / ${current.progress[1]} 帧… 原片保持只读。`);
    }
  }
}
async function action(fn) {
  if (busy || autoBusy) return;
  busy = true; updateVerifyButton(); playing = false; $('play').textContent = '播放';
  document.querySelector('.layout').classList.add('busy');
  document.querySelector('.layout').inert = true; $('regionDialog').inert = true; $('jointDialog').inert = true;
  try { await fn(); } catch (e) { status(e.message, true); if ($('regionDialog').open) $('regionError').textContent = e.message; }
  finally { busy = false; updateVerifyButton(); document.querySelector('.layout').classList.remove('busy'); document.querySelector('.layout').inert = false; $('regionDialog').inert = false; $('jointDialog').inert = false; }
}
function invalidate() {
  ++analysisGeneration; verificationFailure = ''; clearVerificationReview();
  result = null; exported = null; actual = null; $('imageKind').value = 'original'; $('imageKind').disabled = true; $('export').disabled = true; $('applyXmp').disabled = true; $('verify').disabled = true;
  if (exposureCalibration && exposureCalibration.id !== project?.id) clearExposureCalibration(); updateWriteButtons(); updateVerifyButton();
  $('exportResult').textContent = ''; $('verification').textContent = '';
  $('responseStatus').textContent = '分析后显示曝光换算依据。'; $('gapStatus').textContent = '';
  $('rows').replaceChildren(); draw(); if (project) void showFrame();
}
function settings() {
  return {selected: [...document.querySelectorAll('.event input:checked')].map(el => Number(el.value)), strength: Number($('strength').value), window: Number($('window').value), start: Number($('start').value), end: Number($('end').value), smooth_gaps: $('smoothGaps').checked};
}
async function calculate() {
  invalidate();
  await acceptResult(await job('/api/calculate', {id: project.id, settings: settings()}));
}
async function acceptResult(next) {
  reconcileFeedbackTarget(next);
  result = next; verificationFailure = ''; clearVerificationReview(); if (exposureCalibration && exposureCalibration.id !== project?.id) clearExposureCalibration();
  exported = null; actual = null; $('imageKind').value = 'original'; $('imageKind').disabled = true; $('verify').disabled = true; $('export').disabled = true; $('applyXmp').disabled = true;
  $('exportResult').textContent = ''; $('verification').textContent = '';
  renderRows(); draw(); renderResponse(); updateWriteButtons(); updateVerifyButton(); await showFrame();
  if (result.calibration_required) {
    $('exposureCalibration').open = true;
    status('目标预览已生成。请在导出区完成“曝光校准（保留当前目标）”，导入两组 ACR 小图后即可计算曝光增量。', true);
  }
  else if (result.blocked.length) status(`${result.blocked.length} 帧超过 ±2 EV 增量或 ±5 EV 最终曝光，已阻止导出。请核对事件选择、首尾目标，必要时分段处理。`, true);
  else status('预览已更新，可以导出修正包。' + (result.event_warnings?.length ? `已包含 ${result.event_warnings.length} 个需人工判断的候选，请对照原图检查。` : '') + '绿色曲线是亮度目标，实际效果需在 ACR 显影后检查。');
}
function renderResponse() {
  const joins = (result?.gap_joins || []).flatMap(join => join.indices);
  $('gapStatus').textContent = joins.length ? `已平滑 ${joins.length} 处拍摄间隔接缝：${joins.map(i => `第 ${i}→${i+1} 帧`).join('、')}。` : '';
  const response = result?.response;
  $('responseStatus').textContent = result?.calibration_required
    ? '暂无曝光转换依据：当前仅保留亮度目标，曝光增量待校准，禁止导出及写回。'
    : response?.kind === 'camera-prior'
      ? `曝光换算使用相机响应先验（亮度 / 曝光约 ${response.gain.toFixed(3)}），尚未实测 XMP 显影响应。需用真实重显影反馈校准。`
      : response?.kind === 'measured-exposure'
        ? `已实测逐帧 XMP 曝光响应（亮度 / 曝光中位数 ${response.gain.toFixed(3)}），按当前亮度目标计算曝光增量；最终效果仍需 ACR 重新显影复核。`
      : '暂无已实测的 XMP 显影响应；绿色仅为亮度目标。';
}
function clearExposureCalibration() {
  exposureCalibration = null;
  $('exposureCalibrationResult').textContent = '';
  $('exposureBaseline').value = ''; $('exposureProbe').value = '';
  $('exposureCalibrate').disabled = true;
}
function currentExposureCalibration() {
  return exposureCalibration && result && !project?.written && exposureCalibration.id === project?.id && !project?.readOnly;
}
function updateEventSelection() {
  const inputs = [...document.querySelectorAll('.event input:not(:disabled)')];
  const selected = inputs.filter(input => input.checked).length;
  const all = $('selectAllEvents');
  all.disabled = !project || project.written || project.readOnly || !inputs.length;
  all.checked = inputs.length > 0 && selected === inputs.length;
  all.indeterminate = selected > 0 && selected < inputs.length;
}
$('selectAllEvents').addEventListener('change', () => {
  if (busy || !project || project.written || project.readOnly) return;
  document.querySelectorAll('.event input:not(:disabled)').forEach(input => { input.checked = $('selectAllEvents').checked; });
  updateEventSelection(); invalidate();
  status('事件选择已改变，请更新修正预览。');
});
function renderEvents() {
  const container = $('events'); container.replaceChildren();
  const selectable = project.report.events.filter(event => event.selectable).length;
  $('eventCount').textContent = `${selectable} 个可选`;
  if (!project.report.events.length) container.textContent = '没有可定位的调参候选。仅计算短时平滑；这不证明序列不存在曝光台阶。';
  const unavailable = document.createElement('details'); unavailable.className = 'unestimated-events';
  const unavailableSummary = document.createElement('summary');
  unavailableSummary.textContent = `未生成估计（${project.report.events.length - selectable} 处）`;
  unavailable.append(unavailableSummary);
  for (const event of project.report.events) {
    const label = document.createElement('div'); label.className = 'event';
    const input = document.createElement(event.selectable ? 'input' : 'span');
    if (event.selectable) {
      input.type = 'checkbox'; input.value = event.index; input.id = `event-${event.index}`;
      input.addEventListener('change', () => { updateEventSelection(); invalidate(); $('scrub').value = event.index; showFrame(); status('事件选择已改变，请更新修正预览。'); });
    } else { input.textContent = '·'; }
    const content = document.createElement('div');
    const title = document.createElement(event.selectable ? 'label' : 'button');
    if (event.selectable) title.htmlFor = input.id;
    else {
      title.type = 'button';
      title.addEventListener('click', () => { $('scrub').value = event.index; void showFrame(); });
    }
    title.className = 'event-title'; title.textContent = event.method==='dense-cluster' ? `第 ${event.index+1}–${event.end_index+1} 帧 · 连续调参（${event.indices.length} 次）` : `第 ${event.index + 1} 帧 · ${project.frames[event.index].name}`;
    const values = document.createElement('p'); values.textContent = `${event.method==='dense-cluster' ? '整组净变化 · ' : ''}相机 ${event.camera_delta.toFixed(2)} EV · 已有 XMP ${event.xmp_delta.toFixed(2)} EV · 图像跳变 ${event.jump === null ? '不可估计' : event.jump.toFixed(2) + ' EV'}`;
    const explanation = document.createElement('small'); explanation.textContent = event.reason + (event.residual_range ? ` · 残差范围 ±${event.residual_range.toFixed(2)} EV` : '');
    const detail = document.createElement('details'), summary = document.createElement('summary');
    const needsReview = event.selectable && event.recommended === false;
    summary.textContent = event.selectable ? needsReview ? '需人工判断 · 可以勾选' : '查看判断依据' : '未生成估计 · 查看原因';
    detail.open = needsReview || !event.selectable;
    detail.append(summary, values, explanation); content.append(title, detail);
    if (Number.isFinite(event.spatial_support)) {
      const spatial = document.createElement('p');
      spatial.textContent = `空间支持 ${(event.spatial_support * 100).toFixed(1)}%（至少 75%） · 区域跳变幅度差异 ${event.tile_dispersion.toFixed(3)} EV（仅供参考）`;
      detail.append(spatial);
    }
    if (event.components) { const parts=document.createElement('p'); parts.textContent=event.components.map(c=>`第 ${c.index+1} 帧 ${c.jump.toFixed(3)} EV`).join(' · '); detail.append(parts); }
    label.append(input, content); (event.selectable ? container : unavailable).append(label);
  }
  if (selectable < project.report.events.length) container.append(unavailable);
  updateEventSelection();
}
function renderRows() {
  const fragment = document.createDocumentFragment();
  project.frames.forEach((f, i) => { const tr = document.createElement('tr');
    [i + 1, f.name, f.exposure.toFixed(3), result.calibration_required ? '待校准' : result.delta[i].toFixed(3), result.calibration_required ? '待校准' : result.final[i].toFixed(3)].forEach(value => { const td = document.createElement('td'); td.textContent = value; tr.append(td); }); fragment.append(tr);
  }); $('rows').replaceChildren(fragment);
}
function measurementPercent(value) { return Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : '—'; }
function measurementNumber(value) { return Number.isFinite(value) ? value.toLocaleString() : '—'; }
function measurementCellName(index) {
  const cols = measurement?.grid?.cols || 8;
  return `${Math.floor(index / cols) + 1} 行 ${index % cols + 1} 列`;
}
function measurementFrameNumbers(numbers) {
  if (!numbers?.length) return '无';
  // Compress runs without hiding isolated failures or building hundreds of DOM nodes.
  const values = [...new Set(numbers)].sort((a,b) => a-b), ranges = [];
  for (let i=0; i<values.length; i++) {
    const start = values[i]; let end = start;
    while (i+1<values.length && values[i+1]===end+1) end=values[++i];
    ranges.push(start===end ? `${start}` : `${start}–${end}`);
  }
  return ranges.join('、');
}
function renderMeasurement(value) {
  measurement = value || null; measurementCell = null; measurementPicture = null;
  $('measurementPanel').hidden = !measurement;
  $('measurementCanvas').hidden = true; $('measurementImageInfo').textContent = '';
  $('measurementChecks').replaceChildren(); $('measurementGrid').replaceChildren();
  if (!measurement) return;
  $('measurementBadge').textContent = measurement.passed ? '测量检查通过' : '未通过 · 已停止分析';
  $('measurementBadge').classList.toggle('failed', !measurement.passed);
  $('measurementPanel').open = false;
  $('measurementDetails').open = false;
  for (const check of measurement.checks || []) {
    const diagnostic = check.kind === 'diagnostic';
    const card = document.createElement('div'); card.className = `measurement-check ${diagnostic ? 'diagnostic' : check.passed ? 'passed' : 'failed'}`;
    const title = document.createElement('strong'); title.textContent = `${diagnostic ? '参考' : check.passed ? '✓' : '×'} ${check.label}`;
    const description = document.createElement('span');
    const format = check.unit === 'fraction' ? measurementPercent : measurementNumber;
    const value = check.value === null ? '无法评估' : format(check.value);
    description.textContent = diagnostic ? `${value} · ${check.note || '仅供诊断，不作为通过门槛。'}` : `${value} / 至少 ${format(check.minimum)}${check.unit === 'pixels' ? ' 个采样像素' : ''}`;
    card.append(title, description); $('measurementChecks').append(card);
  }
  const support = measurement.support || {};
  $('measurementSummary').textContent = `最终固定支持覆盖 ROI ${measurementPercent(support.coverage)} · ${measurementNumber(support.pixels)} 个不同的 JPG 采样像素（非 RAW 像素，不放大补点）。${measurement.note || ''}`;
  $('measurementGrid').style.gridTemplateColumns = `repeat(${measurement.grid?.cols || 8}, minmax(0, 1fr))`;
  for (const cell of measurement.cells || []) {
    const button = document.createElement('button'); button.type = 'button'; button.dataset.cell = cell.index;
    const abnormal = cell.failed_frames?.length || cell.variation_frames?.length;
    button.className = `measurement-cell ${cell.selected ? 'selected' : 'excluded'}${abnormal ? ' affected' : ''}`;
    button.textContent = `${cell.index + 1} ${cell.selected ? '✓' : '×'}${abnormal ? '!' : ''}`;
    button.title = `${measurementCellName(cell.index)} · 最低有效覆盖 ${measurementPercent(cell.coverage_min)} · ${cell.failed_frames?.length || 0} 帧含剔除像素 · ${cell.variation_frames?.length || 0} 帧有局部亮度变化差异（需人工检查，不据此判定运动或排除网格）`;
    button.setAttribute('aria-label', button.title); button.setAttribute('aria-pressed', 'false');
    button.addEventListener('click', () => selectMeasurementCell(cell.index));
    $('measurementGrid').append(button);
  }
  selectMeasurementCell(null);
}
function selectMeasurementCell(index) {
  measurementCell = index;
  const cell = measurement?.cells?.find(item => item.index === index);
  for (const button of $('measurementGrid').children) button.setAttribute('aria-pressed', String(Number(button.dataset.cell)===index));
  $('measurementCellInfo').textContent = cell
    ? `${measurementCellName(cell.index)}（网格 ${cell.index + 1}）：${cell.selected ? '包含固定测量像素' : '未纳入固定测量'}。最低有效覆盖 ${measurementPercent(cell.coverage_min)}，最低有效采样像素 ${measurementNumber(cell.pixel_count_min)}，全采样像素可用帧比例 ${measurementPercent(cell.usable_fraction)}。高光导致像素剔除的帧：${measurementFrameNumbers(cell.clipped_frames)}；暗部导致像素剔除的帧：${measurementFrameNumbers(cell.dark_frames)}；局部亮度变化差异帧：${measurementFrameNumbers(cell.variation_frames)}（需人工检查，不据此判定运动或排除网格）。高光或暗部只剔除实际受影响像素，其余像素仍可保留。`
    : '当前列出含剔除像素或局部亮度变化差异的帧。高光、暗部按像素剔除；局部亮度变化差异仅供人工检查，不据此判定运动或排除网格。有效像素不足时会停止分析，其余指标供检查。';
  const affected = cell ? new Set([...(cell.failed_frames || []), ...(cell.variation_frames || [])]) : null;
  let frames = (measurement?.frames || []).filter(frame => affected ? affected.has(frame.frame) : frame.failed_cells?.length || frame.variation_cells?.length);
  const onlyClean = !frames.length;
  if (onlyClean) frames = measurement?.frames || [];
  const fragment = document.createDocumentFragment();
  for (const frame of frames) {
    const option = document.createElement('option'); option.value = frame.frame;
    option.textContent = `第 ${frame.frame} 帧 · ${frame.name}${onlyClean ? ' · 没有受影响帧，供检查' : ''}`;
    fragment.append(option);
  }
  $('measurementFrame').replaceChildren(fragment); $('measurementFrame').disabled = !frames.length;
  $('measurementPreview').disabled = !frames.length;
  renderMeasurementFrame();
}
function renderMeasurementFrame() {
  const number = Number($('measurementFrame').value);
  const frame = measurement?.frames?.find(item => item.frame === number);
  $('measurementCanvas').hidden = true; $('measurementImageInfo').textContent = ''; measurementPicture = null;
  if (!frame) { $('measurementFrameInfo').textContent = '没有可显示的帧记录。'; return; }
  const cells = indexes => measurementFrameNumbers((indexes || []).map(index => index+1));
  $('measurementFrameInfo').textContent = `第 ${frame.frame} 帧 · ${frame.name}：本帧有效覆盖 ${measurementPercent(frame.coverage)}，有效采样像素 ${measurementNumber(frame.valid_pixels)}。含剔除像素的网格：${cells(frame.failed_cells)}（其余像素可保留）；局部亮度变化差异网格：${cells(frame.variation_cells)}（需人工检查，不据此判定运动或排除网格）。`;
  if (project) { $('scrub').value = Math.max(0, number-1); void showFrame(); }
}
function drawMeasurementImage() {
  if (!measurement || !measurementPicture) return;
  const canvas = $('measurementCanvas'), picture = measurementPicture;
  const width = Math.min(1000, picture.naturalWidth), height = Math.round(picture.naturalHeight*width/picture.naturalWidth);
  canvas.width = width; canvas.height = height;
  const ctx = canvas.getContext('2d'); ctx.drawImage(picture, 0, 0, width, height);
  const [l,t,r,b] = measurement.roi || [0,0,1,1], rows = measurement.grid?.rows || 8, cols = measurement.grid?.cols || 8;
  const frame = measurement.frames?.find(item => item.frame === Number($('measurementFrame').value));
  ctx.lineWidth = Math.max(1, width/600); ctx.font = `${Math.max(10, width/70)}px sans-serif`;
  for (const cell of measurement.cells || []) {
    const x = (l+(r-l)*cell.col/cols)*width, y = (t+(b-t)*cell.row/rows)*height;
    const w = (r-l)*width/cols, h = (b-t)*height/rows;
    const focused = measurementCell === cell.index, abnormal = frame?.failed_cells?.includes(cell.index) || frame?.variation_cells?.includes(cell.index);
    ctx.strokeStyle = focused ? '#fff' : abnormal ? '#f5aa88' : '#d6e6a5';
    ctx.fillStyle = focused ? 'rgba(255,255,255,.25)' : abnormal ? 'rgba(245,120,80,.18)' : 'rgba(100,180,100,.08)';
    ctx.fillRect(x,y,w,h); ctx.strokeRect(x,y,w,h);
    ctx.fillStyle = '#fff'; ctx.strokeStyle = '#111'; ctx.lineWidth = 3;
    ctx.strokeText(`${cell.index+1}`,x+3,y+Math.min(h-2,16)); ctx.fillText(`${cell.index+1}`,x+3,y+Math.min(h-2,16)); ctx.lineWidth = Math.max(1,width/600);
  }
  canvas.hidden = false;
}
$('measurementAllFrames').addEventListener('click', () => selectMeasurementCell(null));
$('measurementFrame').addEventListener('change', renderMeasurementFrame);
$('measurementPreview').addEventListener('click', () => action(async () => {
  const frame = measurement?.frames?.find(item => item.frame === Number($('measurementFrame').value));
  if (!frame) return;
  const meta = await job('/api/region-preview', {preview_dir:$('previews').value, index:frame.frame-1});
  const picture = new Image(); picture.src = meta.image; await picture.decode(); measurementPicture = picture;
  drawMeasurementImage();
  $('measurementImageInfo').textContent = `${meta.name} · 第 ${frame.frame} 帧。浅色为选中网格；橙色表示本帧含剔除像素或局部亮度变化差异，不等于整格不可用，具体原因见上方。网格边界位于当前 ROI 内。`;
}));
function draw() {
  if (!project) return;
  const area=(project.roi[2]-project.roi[0])*(project.roi[3]-project.roi[1]);
  const support = project.report.measurement?.support;
  $('measurementScope').textContent = `${area<.9999 ? `框选区域占画面 ${(area*100).toFixed(1)}%` : '使用整张图作为参考区域'} · ${support ? `固定测量支持覆盖 ROI ${(support.coverage*100).toFixed(1)}%，共 ${support.pixels.toLocaleString()} 个像素` : `沿用已保存的 ${project.report.common_tiles.length} 个参考网格`}。曲线为固定区域的亮度，不是 XMP 曝光值；绿色为亮度目标，不代表实际曝光或显影预测。`;
  const chart = $('chart'), ns = 'http://www.w3.org/2000/svg'; chart.replaceChildren();
  const series = [{data: project.report.measured, color: '#939f95'}];
  if (result) series.push({data: result.target, color: '#d6e6a5'});
  if (actual) series.push({data: actual, color: '#e2ac79'});
  const values = series.flatMap(s => s.data); const min = Math.min(...values) - .15, max = Math.max(...values) + .15;
  const x = chartFrameX, y = v => 210 - (v - min) / (max - min) * 190;
  for (let i = 0; i < 5; i++) { const v = min + i / 4 * (max - min); const line = document.createElementNS(ns, 'line');
    Object.entries({x1:48, x2:883, y1:y(v), y2:y(v), stroke:'#344238'}).forEach(([k,v]) => line.setAttribute(k,v)); chart.append(line);
    const text = document.createElementNS(ns, 'text'); text.setAttribute('x','2'); text.setAttribute('y',y(v)+4); text.setAttribute('fill','#9ba798'); text.setAttribute('font-size','11'); text.textContent = v.toFixed(1); chart.append(text);
  }
  for (const event of project.report.events) { const line = document.createElementNS(ns, 'line'); Object.entries({x1:x(event.index),x2:x(event.index),y1:15,y2:214,stroke:'#735e43','stroke-dasharray':'3 5'}).forEach(([k,v]) => line.setAttribute(k,v)); chart.append(line); }
  for (const s of series) { const path = document.createElementNS(ns, 'polyline'); path.setAttribute('points', s.data.map((v,i) => `${x(i)},${y(v)}`).join(' ')); path.setAttribute('fill','none'); path.setAttribute('stroke',s.color); path.setAttribute('stroke-width','2'); chart.append(path); }
  for (const row of verificationReview?.uncertain_frames || []) {
    const marker = document.createElementNS(ns, 'circle');
    Object.entries({cx:x(row.frame-1),cy:y(row.observed_ev),r:4,fill:'#101914',stroke:'#e2ac79','stroke-width':2}).forEach(([k,v]) => marker.setAttribute(k,v));
    const title = document.createElementNS(ns, 'title'); title.textContent = `第 ${row.frame} 帧 · 需结合测光区间查看`;
    marker.append(title); chart.append(marker);
  }
  const marker = document.createElementNS(ns, 'line');
  Object.entries({id:'chartSelection',y1:15,y2:214,stroke:'#e8eee7','stroke-width':1,'stroke-dasharray':'4 3','pointer-events':'none'}).forEach(([k,v]) => marker.setAttribute(k,v));
  chart.append(marker);
  const hitArea = document.createElementNS(ns, 'rect');
  Object.entries({x:48,y:15,width:835,height:199,fill:'transparent',class:'chart-hit-area'}).forEach(([k,v]) => hitArea.setAttribute(k,v));
  hitArea.addEventListener('click', e => {
    if (!project || busy) return;
    const matrix = chart.getScreenCTM(); if (!matrix) return;
    const point = new DOMPoint(e.clientX, e.clientY).matrixTransform(matrix.inverse());
    const index = Math.round((point.x - 48) / 835 * (project.frames.length - 1));
    playing = false; $('play').textContent = '播放';
    $('scrub').value = Math.max(0, Math.min(project.frames.length - 1, index));
    void showFrame();
  });
  chart.append(hitArea); updateChartSelection();
}
function chartFrameX(index) {
  return 48 + index / Math.max(1, project.frames.length - 1) * 835;
}
function updateChartSelection() {
  const marker = $('chartSelection'); if (!project || !marker) return;
  const x = chartFrameX(Number($('scrub').value));
  marker.setAttribute('x1', x); marker.setAttribute('x2', x);
}
function updateInfo() {
  if (!project) return; const i = Number($('scrub').value), f = project.frames[i];
  updateChartSelection();
  $('frameTitle').textContent = f.name + ($('imageKind').value === 'verified' ? ' · ACR 复核图' : ' · 原始小图'); $('frameNumber').textContent = `${i+1} / ${project.frames.length}`;
  $('frameInfo').textContent = `原曝光 ${f.exposure.toFixed(3)} EV` + (result ? result.calibration_required ? ' · 曝光增量待校准，仅显示亮度目标' : `  →  增量 ${result.delta[i].toFixed(3)}  →  新曝光 ${result.final[i].toFixed(3)} EV` : '') + ` · ROI 高光 ${(f.clipped*100).toFixed(1)}% · 暗部 ${(f.dark*100).toFixed(1)}%`;
  if ($('imageKind').value === 'verified' && verificationReview?.uncertain_frames?.some(row => row.frame === i+1)) $('frameInfo').textContent += ' · 本帧需结合测光区间查看，见复核详情';
}
function drawROI() {
  if (!project) return; const photo = $('photo'), canvas = $('roiCanvas');
  const scale = Math.min(photo.clientWidth / photo.naturalWidth, photo.clientHeight / photo.naturalHeight);
  if (!Number.isFinite(scale)) return;
  canvas.width = Math.round(photo.naturalWidth * scale); canvas.height = Math.round(photo.naturalHeight * scale);
  const ctx = canvas.getContext('2d'), [l,t,r,b] = project.roi; ctx.strokeStyle = '#d6e6a5'; ctx.lineWidth = 2; ctx.setLineDash([5,5]); ctx.strokeRect(l*canvas.width+1,t*canvas.height+1,(r-l)*canvas.width-2,(b-t)*canvas.height-2);
}
async function showFrame() {
  if (!project) return; const seq = ++imageRequest; $('photo').hidden = true; $('roiCanvas').hidden = true; updateInfo();
  try {
    const r = await fetch(`/api/image?id=${project.id}&index=${$('scrub').value}&kind=${$('imageKind').value}`, {headers:{'X-Timelapse-Token':token}});
    if (!r.ok) throw Error((await r.json()).error);
    const blob = await r.blob(); if (seq !== imageRequest) return;
    const nextURL = URL.createObjectURL(blob); if (objectURL) URL.revokeObjectURL(objectURL); objectURL = nextURL;
    $('photo').onload = () => { if (seq !== imageRequest) return; $('photo').hidden = false; $('roiCanvas').hidden = false; drawROI(); }; $('photo').alt = $('imageKind').value === 'verified' ? '当前 ACR 重显影复核图' : '当前 ACR 原始小图'; $('photo').src = objectURL; $('emptyPhoto').hidden = true;
  } catch (e) { status(e.message, true); playing = false; }
}
$('importForm').addEventListener('submit', e => { e.preventDefault(); action(async () => {
  discardAnalysis();
  status('正在建立只读分析快照…');
  const next = await job('/api/analyze', {raw_dir:$('raw').value,preview_dir:$('previews').value,mode:$('mode').value,roi:referenceRegion});
  project = next; invalidate(); $('controls').disabled=false; $('scrub').disabled=false; $('play').disabled=false;
  $('scrub').max = project.frames.length-1; $('scrub').value=0; $('count').textContent = `${project.frames.length} 帧 · 固定参考区域`;
  renderMeasurement(project.report.measurement);
  $('notes').textContent = project.report.notes.join(' ') || '候选断点只用于辅助判断，默认不选。自然光变化与相机跳变仍可能重叠。';
  renderEvents(); await calculate(); await showFrame();
}); });
$('demo').addEventListener('click', () => action(async () => { status('生成合成测试序列…'); const paths = await job('/api/demo', {work_dir:$('autoRaw').value.trim() || $('raw').value.trim()}); $('sameFolder').checked=false; syncSameFolder(); clearBackups(); $('applyResult').textContent=''; $('raw').value=paths.raw_dir; $('previews').value=paths.preview_dir; updateBackupLocation(); referenceRegion=[0,0,1,1]; updateRegionSummary(); syncJointImport(); discardAnalysis(); status('合成示例已准备：120 帧，包含 1 EV 台阶与轻微闪烁。可先在图片上框选参考区域，再分析。'); }));
$('calculate').addEventListener('click', () => action(calculate));
for (const id of ['strength','window','start','end','smoothGaps']) $(id).addEventListener('input', () => { $('strengthValue').textContent = `${Math.round(Number($('strength').value)*100)}%`; invalidate(); status('参数已改变，请更新修正预览。'); });
function disableWithReason(button, reasonId, reason, readyText = '') {
  $(button).disabled = Boolean(reason); $(button).title = reason;
  $(reasonId).textContent = reason || readyText;
}
function updateWriteButtons() {
  const base = !project ? '请先分析序列。' : !result ? '参数已改变，请更新预览。'
    : project.written ? '此序列已写回；请用最新显影复核或残差修正。'
    : project.readOnly ? '当前恢复目标仅可复核；无法确认原素材快照，请重新分析后导出。' : '';
  const write = base || (result.calibration_required ? '尚无曝光响应，请先完成下方曝光校准。'
    : result.blocked.length ? `${result.blocked.length} 帧超出曝光范围，请减少修正或调整首尾偏移。` : '');
  const destination = $('exportDestination').value || 'source_subdir';
  const destinationReason = destination === 'custom' && !$('exportDirectory').value.trim() ? '请选择修正包的保存目录。'
    : destination === 'source_backup' && !$('exportClosed').checked ? '请先关闭 ACR 中的此序列，并勾选允许备份后替换 XMP。' : '';
  disableWithReason('export', 'exportReason', write || destinationReason);
  disableWithReason('applyXmp', 'applyReason', write || (!$('acrClosed').checked ? '请先关闭 ACR 中的此序列，并勾选允许写回。' : ''));
  disableWithReason('exposurePrepare', 'exposurePrepareReason', base);
  const calibration = base || (!currentExposureCalibration() ? '请先生成两组曝光校准 XMP。'
    : !$('exposureBaseline').value.trim() ? '请选择 baseline 基准显影小图目录。'
    : !$('exposureProbe').value.trim() ? '请选择 probe-exposure 校准显影小图目录。' : '');
  disableWithReason('exposureCalibrate', 'exposureCalibrateReason', calibration);
}
function saveExposureInputs() {
  try { sessionStorage.setItem('timelapse-exposure-inputs', JSON.stringify({id:project?.id, calibration_id:exposureCalibration?.calibration_id, baseline:$('exposureBaseline').value, probe:$('exposureProbe').value})); } catch (_) {}
}
for (const id of ['exposureBaseline','exposureProbe']) $(id).addEventListener('input', () => { saveExposureInputs(); updateWriteButtons(); });
$('exposurePrepare').addEventListener('click', () => action(async () => {
  if (!project || !result || project.written) throw Error('请先分析序列并更新预览，再生成曝光校准包。');
  clearExposureCalibration();
  const context = {id:project.id, revision:result.revision};
  status('正在保留当前目标，生成两组独立曝光校准 XMP…');
  const prepared = await job('/api/exposure/prepare', context);
  if (project?.id !== context.id || result?.revision !== context.revision || project.written) throw Error('预览已改变，请重新生成曝光校准包。');
  exposureCalibration = {...context, calibration_id:prepared.calibration_id, path:prepared.path};
  $('exposureCalibrationResult').textContent = `已生成 ${prepared.count} 帧曝光校准包：\n${prepared.path}\n\nbaseline/：原曝光基准 XMP\nprobe-exposure/：仅改变曝光 ±0.2 EV 的校准 XMP\n分别在两份独立 RAW 工作副本中应用对应 XMP，再用 ACR 导出两组同名、同尺寸、保留元数据的 sRGB 小图。请勿覆盖原素材或更改其他显影设置。`;
  updateWriteButtons(); status('曝光校准包已生成。导入基准与曝光校准两组 ACR 小图，即可继续使用当前目标。');
}));
$('exposureCalibrateForm').addEventListener('submit', event => { event.preventDefault(); action(async () => {
  if (!currentExposureCalibration()) throw Error('当前预览没有有效的曝光校准包，请重新生成。');
  const context = {id:project.id, revision:result.revision, calibration_id:exposureCalibration.calibration_id};
  status('正在核对两组显影来源并测量逐帧曝光响应…');
  const calibrated = await job('/api/exposure/calibrate', {...context, baseline_previews:$('exposureBaseline').value.trim(), exposure_previews:$('exposureProbe').value.trim()});
  if (project?.id !== context.id || result?.revision !== context.revision || project.written) throw Error('预览已改变，本次校准结果未应用，请重新生成曝光校准包。');
  await acceptResult(calibrated);
}); });
$('acrClosed').addEventListener('change', updateWriteButtons);
function updateExportDestination() {
  const mode = $('exportDestination').value || 'source_subdir';
  const raw = $('raw').value.trim().replace(/\/$/, '');
  $('exportCustomDirectory').hidden = mode !== 'custom';
  $('exportAcrConfirmation').hidden = mode !== 'source_backup';
  $('export').textContent = mode === 'source_backup' ? '备份并导出到原目录' : '导出修正包';
  $('outputLocation').textContent = mode === 'custom'
    ? ($('exportDirectory').value.trim() || '请选择保存目录')
    : raw ? raw + (mode === 'source_backup' ? '/' : '/timelapse-…/package/') : '请先选择 RAW/XMP 文件夹';
  $('exportDestinationHint').textContent = mode === 'source_backup'
    ? '先完整备份到原目录的 Time-lapse-XMP备份/，再替换同名 XMP。RAW 文件不变。'
    : mode === 'custom' ? '修正 XMP 直接保存到此目录，不新建子目录；遇到同名文件停止，不覆盖。'
    : '每次新建独立修正包，原 XMP 保持不变；不保存到项目目录下。';
}
for (const id of ['exportDestination','exportDirectory','exportClosed']) $(id).addEventListener(id === 'exportDirectory' ? 'input' : 'change', () => {
  if (id === 'exportDestination') $('exportClosed').checked = false;
  updateExportDestination(); updateWriteButtons();
});
$('export').addEventListener('click', () => action(async () => {
  const destination = $('exportDestination').value || 'source_subdir';
  const data = {id:project.id, revision:result.revision, destination,
    output_dir:$('exportDirectory').value.trim(), acr_closed:$('exportClosed').checked};
  if (destination === 'source_backup') {
    await applyToSource('/api/export', data);
    $('exportResult').textContent = $('applyResult').textContent;
    return;
  }
  status('核对源文件快照并导出…');
  exported = await job('/api/export', data);
  const direct = exported.layout === 'direct';
  fillFeedbackPaths(direct ? '' : exported.path, direct ? exported.path : exported.path.replace(/\/$/, '') + '/corrected');
  $('exportResult').textContent = `已导出 ${exported.count} 个 XMP：\n${exported.path}\n\n` + (direct
    ? '修正 XMP 已直接保存到所选目录，没有新建子目录。请与对应 RAW 工作副本配对，用 ACR 重新显影复核。'
    : 'corrected 为修正 XMP，originals 为原 XMP 备份。请在 RAW 工作副本中应用 corrected，再用 ACR 重新显影复核。');
  updateVerifyButton(); status('修正包已生成，原 RAW / XMP 未被覆盖。下一步由 ACR 重新显影复核。');
}));

function clearVerificationReview() {
  verificationReview = null;
  $('verificationWarnings').hidden = true;
  $('verificationFrame').replaceChildren();
  $('verificationFrameInfo').textContent = '';
}
function verificationRange(values) {
  return values ? values.map(v => v.toFixed(6)).join(' 至 ') + ' EV' : '旧测量规则无法界定区间';
}
function selectedVerificationWarning() {
  return verificationReview?.uncertain_frames?.find(row => row.frame === Number($('verificationFrame').value));
}
function showVerificationWarning() {
  const row = selectedVerificationWarning();
  if (!row) return;
  $('verificationFrameInfo').textContent = `${row.name}：固定参考中 ${row.support_pixels} 个像素，${row.clipped_pixels ?? '未知'} 个进入高光阈值、${row.dark_pixels ?? '未知'} 个进入暗部阈值。\nJPEG 实测亮度：${row.observed_ev.toFixed(6)} EV\n保守测光区间：${verificationRange(row.brightness_bounds_ev)}\n相对目标的误差区间：${verificationRange(row.residual_bounds_ev)}。这些区间不代表恢复了 RAW 高光或暗部细节。`;
}
function renderVerificationReview(v) {
  verificationReview = v;
  const uncertain = v.uncertain_frames || [];
  const heading = uncertain.length ? `已测量 ${v.total_frames} 帧：${v.comparable_frames} 帧通过可比性检查，${uncertain.length} 帧需结合区间查看。\n以下为 JPEG 实测统计，包含需确认帧，不能视为整批已通过严格校验。\n` : '';
  const range = uncertain.length ? `RMSE 保守区间：${verificationRange(v.rmse_bounds_ev)}\n` : '';
  $('verification').textContent = heading + `目标误差 RMSE：${v.rmse_ev.toFixed(3)} EV\n` + range
    + `整体偏差中位数：${v.median_bias_ev.toFixed(3)} EV\n首尾趋势偏离：${v.trend_error_ev.toFixed(3)} EV\n最高高光占比：${(v.max_clipped_fraction*100).toFixed(1)}%\n仍与原小图完全相同：${v.unchanged_previews} 帧\n`
    + v.events.flatMap(e => e.components ? [`第 ${e.index+1}–${e.end_index+1} 帧组内最大剩余台阶：${e.max_component_jump.toFixed(3)} EV`, ...e.components.map(c => `  第 ${c.index+1} 帧：${c.remaining_jump.toFixed(3)} EV`)] : [`第 ${e.index+1} 帧剩余台阶：${e.remaining_jump === null ? '不可估计' : e.remaining_jump.toFixed(3)+' EV'}`]).join('\n')
    + '\n' + v.note + (uncertain.length ? '已作为最近实测小图沿用；测光区间提示保留，不代表严格校验通过。' : '');
  $('verificationWarnings').hidden = !uncertain.length;
  $('verificationFrame').replaceChildren(...uncertain.map(row => new Option(`第 ${row.frame} 帧 · ${row.name}`, row.frame)));
  if (uncertain.length) { $('verificationFrame').value = uncertain[0].frame; showVerificationWarning(); }
}
$('verificationFrame').addEventListener('change', showVerificationWarning);
$('verificationLocate').addEventListener('click', () => {
  const row = selectedVerificationWarning();
  if (row) { $('scrub').value = row.frame - 1; $('imageKind').value = 'verified'; void showFrame(); }
});
$('verify').addEventListener('click', () => action(async () => {
  status('测量 ACR 重新导出的小图…');
  let v;
  try {
    v = await job('/api/verify', {id:project.id, revision:result.revision, preview_dir:$('verifyPath').value.trim()});
    verificationFailure = '';
  } catch (e) {
    verificationFailure = `上次复核失败：${e.message} 修正后可重试。`;
    throw e;
  }
  acceptJointVerified($('verifyPath').value);
  actual = v.actual; renderVerificationReview(v);
  $('imageKind').disabled = false; $('imageKind').value = 'verified';
  await showFrame(); draw();
  status(v.measurement_status === 'needs-review'
    ? `已完成 ${v.total_frames} 帧复核测量，${v.uncertain_frames.length} 帧带测光区间提示，可定位查看；原目标未改变。`
    : '复核测量完成。请同时检查连续播放效果；数值下降不等于创作意图正确。');
}));
$('scrub').addEventListener('input', showFrame);
$('imageKind').addEventListener('change', showFrame);
$('play').addEventListener('click', async () => { playing=!playing; $('play').textContent=playing?'暂停':'播放'; while(playing) { $('scrub').value=(Number($('scrub').value)+1)%project.frames.length; await showFrame(); await new Promise(r=>setTimeout(r,180)); } $('play').textContent='播放'; });
window.addEventListener('resize', drawROI);
if (!token) status('缺少本机会话密钥。请使用终端输出的完整启动链接打开工作台。', true);



// The picker works before analysis, including when the full frame is unmeasurable.
let referenceRegion = [0, 0, 1, 1], draftRegion = [...referenceRegion];
let regionPurpose = 'reference';
const jointRegions = {mid: [0,0,1,1], high: [0,0,1,1]};
let jointAutomaticSelection = null;
function automaticRegionPreview() { return regionPurpose !== 'reference' && $('jointRegionMode').value === 'auto'; }
let regionImage = null, regionMeta = null, regionBounds = null, regionGesture = null;
const regionCanvas = $('regionCanvas');
function updateRegionSummary() {
  const [l,t,r,b] = referenceRegion;
  $('roiSummary').textContent = l === 0 && t === 0 && r === 1 && b === 1
    ? '当前使用整张画面。可先框选，再分析。'
    : `已框选参考区域 · 占画面 ${Math.round((r-l)*(b-t)*100)}%。更改后需重新分析。`;
}
function discardAnalysis() {
  playing = false; $('play').textContent = '播放'; project = null; ++imageRequest; invalidate();
  $('controls').disabled = true; $('scrub').disabled = true; $('play').disabled = true;
  $('photo').hidden = true; $('roiCanvas').hidden = true; $('emptyPhoto').hidden = false;
  $('frameTitle').textContent = '逐帧查看'; $('frameNumber').textContent = '—';
  $('frameInfo').textContent = '点击分析序列，查看当前参考区域的结果。';
  $('chart').replaceChildren(); $('count').textContent = '等待分析'; $('eventCount').textContent = '0 个候选';
  $('events').textContent = '重新分析后显示候选断点及依据。';
  updateEventSelection();
  $('notes').textContent = '参考区域或输入已改变，旧分析已失效。';
  $('measurementScope').textContent = '分析后可展开测量诊断查看参考区域。';
  renderMeasurement(null);
}
function drawRegion() {
  const width = regionCanvas.clientWidth, height = regionCanvas.clientHeight;
  if (!width || !height) return;
  const ratio = window.devicePixelRatio || 1;
  regionCanvas.width = Math.round(width * ratio); regionCanvas.height = Math.round(height * ratio);
  const ctx = regionCanvas.getContext('2d'); ctx.scale(ratio, ratio); ctx.fillStyle = '#0c120e'; ctx.fillRect(0,0,width,height);
  regionBounds = null;
  if (!regionImage) return;
  const scale = Math.min(width / regionImage.naturalWidth, height / regionImage.naturalHeight);
  const w = regionImage.naturalWidth*scale, h = regionImage.naturalHeight*scale, x = (width-w)/2, y = (height-h)/2;
  regionBounds = {x,y,w,h}; ctx.drawImage(regionImage,x,y,w,h);
  if (automaticRegionPreview() && jointAutomaticSelection) {
    const selection=jointAutomaticSelection, cells=selection[regionPurpose==='mid'?'mid_tiles':'high_tiles'];
    ctx.fillStyle='rgba(0,0,0,.45)'; ctx.fillRect(x,y,w,h);
    ctx.fillStyle='rgba(162,214,172,.18)'; ctx.strokeStyle='#a2d6ac'; ctx.lineWidth=1;
    for (const cell of cells) {
      const row=Math.floor(cell/selection.cols), col=cell%selection.cols;
      ctx.fillRect(x+col*w/selection.cols,y+row*h/selection.rows,w/selection.cols,h/selection.rows);
      ctx.strokeRect(x+col*w/selection.cols,y+row*h/selection.rows,w/selection.cols,h/selection.rows);
    }
    $('regionSelection').textContent=`自动选择 ${cells.length} 个固定网格 · 全段 ${selection.analysed_frames} 帧`;
    return;
  }
  const [l,t,r,b] = draftRegion, left=x+l*w, top=y+t*h, right=x+r*w, bottom=y+b*h;
  ctx.fillStyle='rgba(0,0,0,.55)';
  ctx.fillRect(x,y,w,top-y); ctx.fillRect(x,bottom,w,y+h-bottom);
  ctx.fillRect(x,top,left-x,bottom-top); ctx.fillRect(right,top,x+w-right,bottom-top);
  ctx.strokeStyle='#d6e6a5'; ctx.lineWidth=2; ctx.setLineDash([6,4]);
  ctx.strokeRect(left+1,top+1,Math.max(0,right-left-2),Math.max(0,bottom-top-2));
  $('regionSelection').textContent = l===0 && t===0 && r===1 && b===1 ? '整张画面' : `已选画面的 ${Math.round((r-l)*(b-t)*100)}%`;
}
async function loadRegionFrame(index) {
  regionImage=null; regionMeta=null; regionGesture=null; $('applyRegion').disabled=true;
  $('regionError').textContent=''; $('regionFrame').textContent='加载图片…'; drawRegion();
  const meta = await job('/api/region-preview', {preview_dir:$(regionPurpose === 'reference' ? 'previews' : 'jointPreviews').value, index});
  const picture = new Image(); picture.src=meta.image; await picture.decode();
  regionMeta=meta; regionImage=picture;
  $('regionScrub').max=meta.count-1; $('regionScrub').value=meta.index;
  $('regionScrub').disabled=meta.count===1;
  $('regionFrame').textContent=`${meta.name} · ${meta.index+1} / ${meta.count}`;
  $('applyRegion').disabled=false; drawRegion();
}
function regionPoint(event, allowOutside=false) {
  if (!regionBounds) return null;
  const rect=regionCanvas.getBoundingClientRect(), {x,y,w,h}=regionBounds;
  const px=(event.clientX-rect.left-x)/w, py=(event.clientY-rect.top-y)/h;
  if (!allowOutside && (px<0 || px>1 || py<0 || py>1)) return null;
  return [Math.min(1,Math.max(0,px)), Math.min(1,Math.max(0,py))];
}
function moveRegion(event) {
  if (!regionGesture || event.pointerId!==regionGesture.id) return;
  const point=regionPoint(event,true); if (!point) return;
  const [x,y]=regionGesture.start;
  draftRegion=[Math.min(x,point[0]),Math.min(y,point[1]),Math.max(x,point[0]),Math.max(y,point[1])];
  drawRegion();
}
function validRegion() {
  if (!regionMeta) return false;
  const [l,t,r,b]=draftRegion;
  return Math.round(r*regionMeta.width)-Math.round(l*regionMeta.width)>=32 && Math.round(b*regionMeta.height)-Math.round(t*regionMeta.height)>=32;
}
regionCanvas.addEventListener('pointerdown', event => {
  if (busy || automaticRegionPreview() || !event.isPrimary || event.button!==0 || regionGesture) return;
  const point=regionPoint(event); if (!point) return;
  event.preventDefault(); regionGesture={id:event.pointerId,start:point,previous:[...draftRegion]};
  regionCanvas.setPointerCapture(event.pointerId); $('regionError').textContent='';
});
regionCanvas.addEventListener('pointermove', moveRegion);
regionCanvas.addEventListener('pointerup', event => {
  if (!regionGesture || regionGesture.id!==event.pointerId) return;
  moveRegion(event);
  if (!validRegion()) { draftRegion=regionGesture.previous; $('regionError').textContent='选区太小，请拖出更大的方框（至少覆盖原小图的 32 × 32 像素）。'; }
  regionGesture=null; regionCanvas.releasePointerCapture(event.pointerId); drawRegion();
});
function cancelRegionGesture() {
  if (regionGesture) { draftRegion=regionGesture.previous; regionGesture=null; drawRegion(); }
}
regionCanvas.addEventListener('pointercancel',cancelRegionGesture);
regionCanvas.addEventListener('lostpointercapture',cancelRegionGesture);
$('pickRegion').addEventListener('click', () => action(async () => {
  if (!$('previews').value.trim()) throw Error('请先填写 ACR 小图文件夹，再打开图片框选。');
  regionPurpose='reference'; $('regionTitle').textContent='框选参考区域';
  $('regionHelp').textContent='选择稳定、有细节的区域，避开太阳、深黑与运动物体；只测光，不裁切。';
  $('resetRegion').disabled=false; $('applyRegion').textContent='使用此选区';
  draftRegion=[...referenceRegion]; regionImage=null; regionMeta=null; regionGesture=null;
  $('regionDialog').showModal(); drawRegion(); await loadRegionFrame(project ? Number($('scrub').value) : 0);
}));
$('regionScrub').addEventListener('change', () => action(() => loadRegionFrame(Number($('regionScrub').value))));
$('resetRegion').addEventListener('click', () => { draftRegion=[0,0,1,1]; $('regionError').textContent=''; drawRegion(); });
$('cancelRegion').addEventListener('click', () => $('regionDialog').close());
$('regionDialog').addEventListener('cancel', event => { if (busy) event.preventDefault(); });
$('regionDialog').addEventListener('close',cancelRegionGesture);
$('applyRegion').addEventListener('click', () => {
  if (automaticRegionPreview()) { $('regionDialog').close(); return; }
  if (!validRegion()) { $('regionError').textContent='选区太小，请选择更大的参考区域。'; return; }
  if (regionPurpose !== 'reference') {
    jointRegions[regionPurpose]=[...draftRegion]; resetJointPreparation(); updateJointWorkflow();
    updateJointRegions(); saveJointPaths(); $('jointResult').replaceChildren(); $('regionDialog').close();
    status('联合反馈测量区域已更新；下次生成校准包时生效。'); return;
  }
  const changed=draftRegion.some((v,i)=>v!==referenceRegion[i]);
  referenceRegion=[...draftRegion]; updateRegionSummary(); $('regionDialog').close();
  if (changed) discardAnalysis();
  status(changed ? '参考区域已更新。点击“分析序列”，将同一选区应用到整组照片。' : '参考区域保持不变。');
});
for (const id of ['raw','previews','mode']) $(id).addEventListener('input', () => {
  const previewChanged = id==='previews' || (id==='raw' && $('sameFolder').checked);
  if (id==='raw') { syncSameFolder(); clearBackups(); $('applyResult').textContent=''; }
  if (previewChanged) { referenceRegion=[0,0,1,1]; updateRegionSummary(); }
  if (id==='raw' || id==='previews') syncJointImport();
  discardAnalysis(); status('输入已改变，请按当前设置重新分析。');
});
new ResizeObserver(() => { if ($('regionDialog').open) { cancelRegionGesture(); drawRegion(); } }).observe(regionCanvas);


function updateBackupLocation() { updateExportDestination(); $('exportClosed').checked = false; $('backupLocation').textContent = $('raw').value.trim() ? $('raw').value.trim().replace(/\/$/, '') + '/Time-lapse-XMP备份/' : '所选 RAW/XMP 文件夹 / Time-lapse-XMP备份/'; }
function syncSameFolder() {
  updateBackupLocation();
  const same = $('sameFolder').checked;
  if (same) $('previews').value = $('raw').value;
  $('previews').disabled = same; $('pickPreviews').disabled = same;
}
$('sameFolder').addEventListener('change', () => {
  const oldPath=$('previews').value; syncSameFolder();
  if (oldPath!==$('previews').value) $('previews').dispatchEvent(new Event('input',{bubbles:true}));
  else syncJointImport();
});
for (const button of document.querySelectorAll('[data-folder]')) button.addEventListener('click', () => action(async () => {
  const field=button.dataset.folder, input=$(field);
  status('请在系统窗口中选择文件夹…');
  const selected=await job('/api/choose-folder', {field:button.dataset.purpose || field,initial:input.value || $('raw').value});
  if (selected.cancelled) { status('已取消选择，原路径与分析结果保持不变。'); return; }
  if (input.value!==selected.path) { input.value=selected.path; input.dispatchEvent(new Event('input',{bubbles:true})); }
  status(field === 'autoRaw' ? '素材已选择，点击开始自动调色即可。' : field === 'exportDirectory' ? '修正包保存目录已选择。' : field.startsWith('feedback') ? '残差修正来源目录已更新；核对四个路径后生成独立修正包。' : field.startsWith('joint') ? '目录已更新，请按当前步骤继续。' : field==='verifyPath' ? '复核小图文件夹已选择。' : '文件夹已选择，可以框选参考区域或分析序列。');
}));

function clearBackups() {
  $('backupChoice').replaceChildren(new Option('请先读取备份', ''));
  $('backupChoice').disabled=true; $('restoreXmp').disabled=true;
  $('restoreClosed').checked=false; $('restoreResult').textContent='';
}
function updateRestoreButton() { $('restoreXmp').disabled = !($('backupChoice').value && $('restoreClosed').checked); }
async function refreshBackups() {
  const listing = await job('/api/backups', {raw_dir:$('raw').value});
  const states = {applied:'已写回',applying:'写回未完成',restoring:'恢复未完成',restored:'已恢复',invalid:'备份不完整'};
  $('backupChoice').replaceChildren(new Option('选择备份', ''));
  let activeFound=false;
  for (const b of listing.backups) {
    const when = Number.isNaN(Date.parse(b.created)) ? b.created : new Date(b.created).toLocaleString('zh-CN', {hour12:false});
    const option = new Option(`${when} · ${b.count} 帧 · ${states[b.state] || b.state}`, b.id);
    option.disabled = b.state==='restored' || b.state==='invalid' || activeFound;
    if (b.state!=='restored') activeFound=true;
    $('backupChoice').append(option);
  }
  $('backupChoice').disabled = !listing.backups.length;
  updateRestoreButton();
  return listing;
}
$('loadBackups').addEventListener('click', () => action(async () => { const listing=await refreshBackups(); status(listing.backups.length ? '已读取备份。关闭 ACR 后，选择最新备份恢复。' : '此 RAW 目录还没有备份。'); }));
$('backupChoice').addEventListener('change', updateRestoreButton);
$('restoreClosed').addEventListener('change', updateRestoreButton);
async function applyToSource(route, data) {
  status('正在完整备份原 XMP，然后写回修正…');
  try {
    exported=await job(route, data);
    fillFeedbackPaths(exported.backup_path, exported.raw_dir || $('raw').value.trim());
    project.written=true;

    $('applyResult').textContent=`已写回 ${exported.count} 个 XMP 到：\n${exported.raw_dir}\n原 XMP 备份：\n${exported.backup_path}\n重新在 ACR 打开 RAW，使修正生效。`;
    updateVerifyButton();
    status('备份与写回完成。请用 ACR 重新导出小图，在“更多操作 → ACR 显影复核”中检查。');
  } catch (e) {
    discardAnalysis();
    try { await refreshBackups(); } catch (_) { /* Keep original write failure visible. */ }
    throw e;
  }
  $('controls').disabled=true;
  document.querySelectorAll('.event input').forEach(el=>{el.disabled=true;});
  updateEventSelection();
  updateWriteButtons();
  await refreshBackups();
}
$('applyXmp').addEventListener('click', () => action(() => applyToSource('/api/apply', {id:project.id,revision:result.revision,acr_closed:$('acrClosed').checked})));
$('restoreXmp').addEventListener('click', () => action(async () => {
  status('正在校验备份与当前 XMP，恢复原设置…');
  try {
    const restored=await job('/api/restore',{raw_dir:$('raw').value,backup_id:$('backupChoice').value,acr_closed:$('restoreClosed').checked});
    $('restoreResult').textContent=`已恢复 ${restored.count} 个原 XMP。\n备份保留在：${restored.backup_path}`;
    $('applyResult').textContent='';
    status('原 XMP 已恢复。重新打开 ACR 查看；后续分析请使用与当前 XMP 一致的小图。');
  } finally {
    discardAnalysis();  $('restoreClosed').checked=false;
    try { await refreshBackups(); } catch (_) { /* Existing error is more relevant. */ }
  }
}));


// Persist path ownership and sequence identity so reloads cannot silently mix sequences.
const feedbackFields = ['feedbackSource', 'feedbackOriginal', 'feedbackXmps', 'feedbackPreviews'];
let feedbackContext = null, feedbackCandidate = null, feedbackXmpDefault = '', feedbackSourceTarget = null;
function feedbackImportContext() { return {raw:$('raw').value.trim(), previews:$('previews').value.trim()}; }
function saveFeedbackPaths() {
  try { sessionStorage.setItem('timelapse-feedback-paths', JSON.stringify({
    fields:Object.fromEntries(feedbackFields.map(id => [id, $(id).value])),
    automatic:Object.fromEntries(feedbackFields.filter(id => $(id).dataset.automatic !== undefined).map(id => [id, $(id).dataset.automatic])),
    context:feedbackContext, sourceTarget:feedbackSourceTarget, xmpDefault:feedbackXmpDefault, verifyPath:$('verifyPath').value,
  })); } catch (_) {}
}
function feedbackReason() {
  return !token ? '缺少本机会话密钥，请从启动链接打开工作台。'
    : busy ? '正在处理任务，请等待完成。'
    : !$('feedbackSource').value.trim() && (!project || !result) ? '请先分析并更新预览，或指定保存目标的备份／修正包。'
    : !$('feedbackOriginal').value.trim() ? '请选择首次分析的原始小图目录。'
    : !$('feedbackXmps').value.trim() ? '请选择本次显影实际使用的 XMP 目录。'
    : !$('feedbackPreviews').value.trim() ? '请选择最新 ACR 显影小图目录。' : '';
}
function updateFeedbackButton() {
  disableWithReason('feedbackSubmit','feedbackReason',feedbackReason(), $('feedbackSource').value.trim()
    ? '本轮使用指定包内的冻结目标。' : '本轮使用当前已保存的分析目标，无需先导出。');
}
function setFeedbackAutomatic(id, value, force = false) {
  const field = $(id);
  if (force || !field.value || field.value === field.dataset.automatic) {
    field.value = value; field.dataset.automatic = value;
  }
}
function reconcileFeedbackTarget(next) {
  const source = $('feedbackSource');
  if (feedbackSourceTarget && source.value === source.dataset.automatic
      && feedbackSourceTarget.id === project?.id && feedbackSourceTarget.revision !== next.revision) {
    // The render and XMP paths remain valid evidence; only the intended target changed.
    setFeedbackAutomatic('feedbackSource', '', true); feedbackSourceTarget = null;
    feedbackCandidate = null; $('feedbackResult').replaceChildren(); saveFeedbackPaths();
  }
}
function fillFeedbackPaths(source, xmps) {
  const next = feedbackImportContext();
  if (feedbackContext && (next.raw !== feedbackContext.raw || next.previews !== feedbackContext.previews)) {
    for (const id of feedbackFields) { $(id).value = ''; delete $(id).dataset.automatic; }
    feedbackCandidate = null; feedbackXmpDefault = ''; feedbackSourceTarget = null; $('feedbackResult').replaceChildren();
    // A rendered directory from another sequence must never be inherited.
    $('verifyPath').value = ''; verificationFailure = '';
  }
  feedbackContext = next;
  if (source !== undefined) {
    setFeedbackAutomatic('feedbackSource', source, true);
    feedbackSourceTarget = project && result ? {id:project.id, revision:result.revision} : null;
  }
  if (xmps !== undefined) feedbackXmpDefault = xmps;
  setFeedbackAutomatic('feedbackOriginal', next.previews);
  setFeedbackAutomatic('feedbackXmps', feedbackXmpDefault || next.raw);
  setFeedbackAutomatic('feedbackPreviews', $('verifyPath').value.trim());
  saveFeedbackPaths(); updateFeedbackButton();
}
try {
  const saved = JSON.parse(sessionStorage.getItem('timelapse-feedback-paths') || '{}');
  const fields = saved.fields || saved;
  if (saved.sourceTarget && typeof saved.sourceTarget.id === 'string' && saved.sourceTarget.revision !== undefined) feedbackSourceTarget = saved.sourceTarget;
  if (typeof saved.xmpDefault === 'string') feedbackXmpDefault = saved.xmpDefault;
  if (typeof saved.verifyPath === 'string') $('verifyPath').value = saved.verifyPath;
  for (const id of feedbackFields) {
    if (typeof fields[id] === 'string') $(id).value = fields[id];
    if (typeof saved.automatic?.[id] === 'string') $(id).dataset.automatic = saved.automatic[id];
  }
  if (saved.context && typeof saved.context.raw === 'string' && typeof saved.context.previews === 'string') feedbackContext = saved.context;
} catch (_) {}
for (const id of feedbackFields) $(id).addEventListener('input', () => {
  delete $(id).dataset.automatic;
  if (id === 'feedbackSource') feedbackSourceTarget = null;
  feedbackCandidate = null; $('feedbackResult').replaceChildren(); saveFeedbackPaths(); updateFeedbackButton();
});
for (const id of ['raw','previews','verifyPath']) $(id).addEventListener('input', () => { fillFeedbackPaths(); updateVerifyButton(); });
function beginFeedbackRound(candidate) {
  setFeedbackAutomatic('feedbackSource', candidate.path, true);
  // Choosing a next-round package explicitly keeps that package's frozen target.
  delete $('feedbackSource').dataset.automatic; feedbackSourceTarget = null;
  feedbackXmpDefault = candidate.path.replace(/\/$/, '') + '/corrected';
  setFeedbackAutomatic('feedbackXmps', feedbackXmpDefault, true);
  setFeedbackAutomatic('feedbackPreviews', '', true);
  $('verifyPath').value = ''; verificationFailure = '';
  saveFeedbackPaths(); updateVerifyButton();
  status('已采用新包作为下一轮来源。请在工作副本应用 corrected，再选择新一轮 ACR 显影小图；原始小图与冻结目标保持不变。');
}
$('feedbackForm').addEventListener('submit', event => {
  event.preventDefault();
  const reason = feedbackReason();
  if (reason) { status(reason, true); updateFeedbackButton(); return; }
  action(async () => {
    saveFeedbackPaths(); $('feedbackResult').replaceChildren();
    status('正在核对显影来源，并按保存的亮度目标计算残差修正…');
    const source = $('feedbackSource').value.trim();
    const feedback = await job('/api/feedback', {
      source, ...(source ? {} : {id:project.id, revision:result.revision}),
      original_previews: $('feedbackOriginal').value.trim(),
      current_xmps: $('feedbackXmps').value.trim(), previews: $('feedbackPreviews').value.trim(),
    });
    const generated = feedback.candidate_generated ?? feedback.ready;
    const noChanges = feedback.status === 'no-changes';
    const box = document.createElement('div'); box.className = 'destination';
    const heading = document.createElement('strong');
    heading.textContent = noChanges ? '当前无需额外曝光变更' : feedback.partial ? `部分候选已生成 · ${feedback.changed_count} 帧调整，${feedback.held_count} 帧保持当前值`
      : generated ? `独立候选包已生成 · ${feedback.count} 帧` : '检查报告已生成，未生成可应用的修正包';
    const path = document.createElement('code'); path.textContent = feedback.path;
    const audit = document.createElement('span');
    const labels = {verified:'已核对', incomplete:'证据不完整', mismatch:'来源不符'};
    audit.textContent = '显影来源核对：' + Object.entries(feedback.audit_counts || {}).map(([key, count]) => `${labels[key] || key} ${count} 帧`).join(' · ');
    const error = document.createElement('span');
    error.textContent = Number.isFinite(feedback.rmse_before_ev) ? `本轮修正前目标误差：${feedback.rmse_before_ev.toFixed(3)} EV` : '修正前目标误差暂不可用';
    const note = document.createElement('p'); note.className = 'fine';
    note.textContent = noChanges ? '当前残差处于测量噪声内，或建议增量未达到曝光可写精度；保持当前曝光和冻结目标。仍须实际播放检查连续性与视觉效果。' : generated ? '此包尚未经 ACR 重新显影验证，不代表全序列通过。保持当前值的帧仍须按下方原因处理；应用候选后再复核残差与播放效果。'
      : '未覆盖原素材，冻结目标保持不变。请按下方具体原因修正输入后重试。';
    box.append(heading, path, audit, error, note);
    const issues = feedback.issues || [];
    if (issues.length) {
      const list = document.createElement('ul'); list.className = 'feedback-issues';
      for (const issue of issues) {
        const item = document.createElement('li');
        item.textContent = `${issue.frame ? `第 ${issue.frame} 帧 · ` : ''}${issue.name || ''}：${issue.reason} ${issue.action || ''}`;
        list.append(item);
      }
      box.append(list);
    } else if (!generated && !noChanges) {
      const detail = document.createElement('p'); detail.textContent = `来源或响应检查未通过，请查看 ${feedback.path}/source-audit.json 的逐帧原因。`; box.append(detail);
    }
    if (generated) {
      feedbackCandidate = feedback;
      const next = document.createElement('button'); next.type = 'button'; next.textContent = '用此包开始下一轮';
      next.addEventListener('click', () => beginFeedbackRound(feedback)); box.append(next);
    }
    $('feedbackResult').append(box);
    status(noChanges ? '当前无需额外曝光变更，已保留检查报告。仍须实际播放检查连续性与视觉效果。' : generated ? (feedback.partial ? '已生成部分候选：有证据的帧调整，其余保持当前值。请查看逐帧原因并重新显影复核。' : '独立候选包已生成，须 ACR 重新显影复核。') : '未生成候选包，具体原因已显示在残差修正结果中。', !generated && !noChanges);
  });
});

let jointPreviewSource = {imported: null, verified: '', manual: null};
let jointStage = 'prepare', jointLastResult = null;
const jointGroups = [['jointBaseline','baseline','基准'],['jointExposure','probe-exposure','曝光'],['jointHighlights','probe-highlights','高光'],['jointWhites','probe-whites','白色色阶']];
function jointPath(root, child) { return root.replace(/\/+$/, '') + '/' + child; }
function clearJointCandidate() {
  $('jointSolution').value = ''; $('jointVerified').value = ''; jointLastResult = null;
  $('jointResult').replaceChildren();
}
function resetJointPreparation() {
  clearJointCandidate(); $('jointSource').value = ''; $('jointRenderRoot').value = '';
  for (const [id] of jointGroups) $(id).value = '';
  jointStage = 'prepare';
}
function fillJointRenderPaths(root) {
  $('jointRenderRoot').value = root;
  for (const [id, folder] of jointGroups) $(id).value = root ? jointPath(root, folder) : '';
}
async function copyJointPath(path) {
  try { await navigator.clipboard.writeText(path); status('目录已复制，可粘贴到 Finder 或 ACR 的保存位置。'); }
  catch (_) { status('无法自动复制，请选中页面显示的目录路径复制。', true); }
}
function renderJointPlan() {
  const rows = [];
  for (const [id, folder, label] of jointGroups) {
    const row = document.createElement('div'); row.className = 'joint-render-row';
    const heading = document.createElement('strong'); heading.textContent = label + ' · ' + folder;
    const destination = document.createElement('code'); destination.textContent = $(id).value;
    const actions = document.createElement('div'); actions.className = 'joint-copy-actions';
    for (const [text, path] of [['复制 XMP 目录', jointPath($('jointSource').value, folder)], ['复制 JPG 保存位置', $(id).value]]) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = text;
      button.addEventListener('click', () => copyJointPath(path)); actions.append(button);
    }
    const caption = document.createElement('span'); caption.textContent = 'JPG 保存到：';
    row.append(heading, caption, destination, actions); rows.push(row);
  }
  $('jointRenderPlan').replaceChildren(...rows);
}
function updateJointWorkflow() {
  const hasSource = !!$('jointSource').value.trim(), hasSolution = !!$('jointSolution').value.trim();
  if (jointStage === 'verify' && !hasSolution) jointStage = hasSource ? 'calibrate' : 'prepare';
  if (jointStage === 'calibrate' && !hasSource) jointStage = 'prepare';
  for (const [stage, suffix] of [['prepare','Prepare'],['calibrate','Calibrate'],['verify','Verify']]) {
    $('jointStage'+suffix).hidden = jointStage !== stage;
    const button = $('jointNav'+suffix);
    button.disabled = stage === 'calibrate' ? !hasSource : stage === 'verify' ? !hasSolution : false;
    button.setAttribute('aria-current', jointStage === stage ? 'step' : 'false');
  }
  const verified = jointLastResult?.status?.startsWith('render-measured-');
  const residual = jointLastResult?.status === 'render-measured-residual-remains';
  $('jointNext').textContent = jointStage === 'prepare'
    ? ($('jointPreviews').value.trim() ? '自动选择测光区域，准备 ACR 所需的四套设置。' : '先在第 01 步选择小图，或在下面更换目录。')
    : jointStage === 'calibrate' ? '下一步：在 ACR 导出四组小图，再点击下方自动计算。'
    : verified ? (residual ? '检查完成，仍有残差。可继续一轮改善。' : '数值检查通过。播放确认后，即可在 ACR 导出最终成片。')
    : '下一步：用修正 XMP 导出一组小图，检查实际效果。';
  $('jointContinue').hidden = !residual;
  $('jointPrepare').textContent = $('jointPrevious').value.trim() ? '按原目标准备下一轮' : '开始自动准备';
  $('jointCorrectedPath').textContent = hasSolution ? jointPath($('jointSolution').value.trim(), 'corrected') : '';
  $('jointCalibrate').disabled = !hasSource;
  $('jointVerify').disabled = !hasSolution;
  if (hasSource) renderJointPlan();
}
function jointAction(fn) {
  return action(async () => {
    try { await fn(); }
    catch (e) {
      const message = document.createElement('p'); message.className = 'joint-error';
      message.textContent = e.message; $('jointResult').replaceChildren(message);
      throw e;
    } finally { updateJointWorkflow(); }
  });
}
const jointFields=['jointPreviews','jointPrevious','jointSource','jointBaseline','jointExposure','jointHighlights','jointWhites','jointSolution','jointVerified','jointWindow','jointRegionMode','jointRenderRoot'];
function saveJointPaths() {
  try { sessionStorage.setItem('timelapse-joint',JSON.stringify({fields:Object.fromEntries(jointFields.map(id=>[id,$(id).value])),regions:jointRegions,previewSource:jointPreviewSource,stage:jointStage,lastResult:jointLastResult})); } catch (_) {}
}
function jointImportContext() {
  return {raw: $('raw').value.trim(), previews: $('previews').value.trim(), sameFolder: $('sameFolder').checked};
}
function updateJointPreviewSource() {
  const path = jointPreviewSource.manual ?? (jointPreviewSource.verified || jointPreviewSource.imported?.previews || '');
  if ($('jointPreviews').value !== path) {
    $('jointPreviews').value = path;
    jointAutomaticSelection = null;
    $('jointResult').replaceChildren();
  }
  $('jointPreviewPath').textContent = path || '请先在第 01 步选择小图目录，也可在此更换目录。';
  $('jointPreviewOrigin').textContent = jointPreviewSource.manual !== null ? '当前小图 · 手动更换'
    : jointPreviewSource.verified ? '当前小图 · 最近一次 ACR 复核实测（不代表校验通过）' : '当前小图 · 沿用第 01 步';
  $('jointUseAutomatic').hidden = jointPreviewSource.manual === null;
  $('jointPrepare').disabled = !path;
  updateJointRegions(); updateJointWorkflow(); saveJointPaths();
}
function syncJointImport() {
  const next = jointImportContext(), previous = jointPreviewSource.imported;
  if (previous && next.raw === previous.raw && next.previews === previous.previews) {
    jointPreviewSource.imported = next; saveJointPaths(); return;
  }
  jointPreviewSource = {imported: next, verified: '', manual: null};
  jointAutomaticSelection = null; resetJointPreparation();
  // A different sequence must not inherit the previous sequence's frozen target.
  for (const id of ['jointPrevious','jointSource','jointBaseline','jointExposure','jointHighlights','jointWhites','jointSolution','jointVerified']) $(id).value = '';
  $('jointResult').replaceChildren();
  updateJointPreviewSource();
}
function acceptJointVerified(path, source = '') {
  if (!source) resetJointPreparation();
  jointPreviewSource.verified = path.trim(); jointPreviewSource.manual = null;
  jointAutomaticSelection = null;
  // Joint verification carries the matching target into the next feedback round.
  $('jointPrevious').value = source;
  $('jointChangeDirectory').open = false;
  updateJointPreviewSource();
}
function updateJointRegions() {
  const continuing=!!$('jointPrevious').value.trim(), auto=$('jointRegionMode').value==='auto';
  $('jointPickMid').disabled=continuing; $('jointPickHigh').disabled=continuing; $('jointRegionMode').disabled=continuing;
  $('jointPickMid').textContent=auto?'查看自动中间调区域':'框选中间调区域';
  $('jointPickHigh').textContent=auto?'查看自动高光区域':'框选高光区域';
  if (continuing) { $('jointRoiSummary').textContent='继续上轮：沿用来源包中冻结的区域和亮度目标，不重新选区。'; return; }
  if (auto) { $('jointRoiSummary').textContent=jointAutomaticSelection
    ? `整段 ${jointAutomaticSelection.analysed_frames} 帧：中间调 ${jointAutomaticSelection.mid_tiles.length} 个固定网格，高光 ${jointAutomaticSelection.high_tiles.length} 个固定网格。点击上方按钮查看。`
    : '自动分析整段序列，选择持续可测、相对稳定的中间调与高光网格。生成校准包时会重新核对并冻结；也可先点击查看。'; return; }
  $('jointRoiSummary').textContent=`中间调：${jointRegions.mid.map(v=>(v*100).toFixed(1)+'%').join(' / ')}；高光：${jointRegions.high.map(v=>(v*100).toFixed(1)+'%').join(' / ')}（左 / 上 / 右 / 下）。继续上一轮时使用包内冻结区域。`;
}
try {
  const saved=JSON.parse(sessionStorage.getItem('timelapse-joint')||'{}');
  for (const id of jointFields) if (typeof saved.fields?.[id]==='string') $(id).value=saved.fields[id];
  jointStage = ['prepare','calibrate','verify'].includes(saved.stage) ? saved.stage : ($('jointSolution').value ? 'verify' : $('jointSource').value ? 'calibrate' : 'prepare');
  jointLastResult = saved.lastResult && typeof saved.lastResult.status === 'string' ? saved.lastResult : null;
  const source = saved.previewSource;
  if (source && typeof source.verified === 'string' && (source.manual === null || typeof source.manual === 'string') &&
      source.imported && typeof source.imported.raw === 'string' && typeof source.imported.previews === 'string') {
    jointPreviewSource = source;
    $('raw').value = source.imported.raw; $('previews').value = source.imported.previews;
    $('sameFolder').checked = source.imported.sameFolder === true; syncSameFolder();
  } else {
    // Preserve an explicitly selected directory from older versions.
    jointPreviewSource.manual = $('jointPreviews').value.trim() || null;
  }
  for (const kind of ['mid','high']) {
    const a=saved.regions?.[kind];
    if (Array.isArray(a) && a.length===4 && a.every(v=>Number.isFinite(v)&&v>=0&&v<=1) && a[0]<a[2] && a[1]<a[3]) jointRegions[kind]=a;
  }
} catch (_) {}
jointPreviewSource.imported ||= jointImportContext();
if ($('jointSource').value && !jointGroups.some(([id]) => $(id).value)) fillJointRenderPaths(jointPath($('jointSource').value, 'jpg'));
updateJointPreviewSource();
if (jointLastResult) showJointResult(jointLastResult);
fillFeedbackPaths();
for (const [stage, suffix] of [['prepare','Prepare'],['calibrate','Calibrate'],['verify','Verify']]) {
  $('jointNav'+suffix).addEventListener('click', () => { jointStage = stage; updateJointWorkflow(); saveJointPaths(); });
}
$('jointOpen').addEventListener('click', () => { $('jointDialog').showModal(); updateJointWorkflow(); });
$('jointClose').addEventListener('click', () => $('jointDialog').close());
$('jointCopyCorrected').addEventListener('click', () => copyJointPath($('jointCorrectedPath').textContent));
$('jointContinue').addEventListener('click', () => {
  resetJointPreparation(); updateJointWorkflow(); saveJointPaths();
  $('jointPrepareForm').dispatchEvent(new Event('submit', {bubbles:true, cancelable:true}));
});
$('jointUseAutomatic').addEventListener('click', () => {
  resetJointPreparation(); jointPreviewSource.manual = null; $('jointChangeDirectory').open = false; updateJointPreviewSource();
});
for (const id of jointFields) $(id).addEventListener('input',()=>{
  if (['jointPreviews','jointPrevious','jointWindow','jointRegionMode'].includes(id)) resetJointPreparation();
  if (id==='jointPreviews') {
    jointPreviewSource.manual = $('jointPreviews').value.trim(); jointAutomaticSelection = null;
    updateJointPreviewSource();
  } else if (id==='jointSource') {
    clearJointCandidate();
    fillJointRenderPaths($('jointSource').value.trim() ? jointPath($('jointSource').value.trim(), 'jpg') : '');
  } else if (id==='jointRenderRoot') {
    fillJointRenderPaths($('jointRenderRoot').value.trim()); clearJointCandidate(); jointStage = 'calibrate';
  } else if (jointGroups.some(([field]) => field === id)) {
    clearJointCandidate(); jointStage = 'calibrate';
  } else if (id==='jointSolution') {
    $('jointVerified').value = '';
  }
  jointLastResult = null;
  updateJointRegions(); updateJointWorkflow(); saveJointPaths(); $('jointResult').replaceChildren();
});
for (const [id,kind,label] of [['jointPickMid','mid','框选中间调：稳定海面或建筑'],['jointPickHigh','high','框选高光：晨曦亮带']]) $(id).addEventListener('click',()=>action(async()=>{
  if (!$('jointPreviews').value.trim()) throw Error('请先在第 01 步选择小图目录，或通过“更换目录”指定小图。');
  if ($('jointRegionMode').value==='auto' && !jointAutomaticSelection) {
    status('正在比较整段序列，自动选择固定测光区域…');
    jointAutomaticSelection=(await job('/api/joint/regions',{previews:$('jointPreviews').value.trim()})).selection;
    updateJointRegions();
    status(`自动选区完成：已分析 ${jointAutomaticSelection.analysed_frames} 帧，可换帧检查固定区域。`);
  }
  regionPurpose=kind; draftRegion=[...jointRegions[kind]]; regionImage=null; regionMeta=null; regionGesture=null;
  const auto=automaticRegionPreview();
  $('regionTitle').textContent=auto ? (kind==='mid'?'自动中间调测光区域':'自动高光测光区域') : label;
  $('regionHelp').textContent=auto?'绿色网格是按整段序列选定的固定区域，换帧时不会移动。自动分析不识别物体；若区域不合适，请关闭后切换“手动框选”。':(kind==='mid'?'选择有细节的大片海面或受光建筑，避开黑色剪影、灯光、强反射和运动物体。':'选择有层次的晨曦亮带或亮云，避开纯白太阳和大片黑色前景；拖动帧滑块检查整段。');
  $('resetRegion').disabled=auto; $('applyRegion').textContent=auto?'完成查看':'使用此选区';
  $('regionDialog').showModal(); drawRegion(); await loadRegionFrame(0);
}));
function showJointResult(result) {
  jointLastResult = result;
  const labels={'awaiting-acr-calibration':'准备好了，接下来去 ACR 导出小图', 'candidate-needs-acr-verification':'修正 XMP 已生成，接下来检查实际效果', 'partial-or-unsupported-calibration':'还有部分帧未完成，请补充后重新计算', 'render-measured-pass':'数值检查通过，请播放确认视觉效果', 'render-measured-residual-remains':'检查完成，仍有残差，可继续改善'};
  const box=document.createElement('div'); box.className='destination';
  const title=document.createElement('strong'); title.textContent=labels[result.status]||result.status;
  const counts=document.createElement('span');
  counts.textContent = `${result.count} 帧` + (result.status==='partial-or-unsupported-calibration' ? ` · ${result.blocked_count ?? 0} 帧待处理` : '');
  box.append(title,counts);
  for (const issue of result.issues || []) {
    const p = document.createElement('p'); p.textContent = `${issue.count} 帧：${issue.reason}。${issue.action}`; box.append(p);
  }
  const detail=document.createElement('details'), summary=document.createElement('summary'); summary.textContent='查看文件位置与诊断';
  const path=document.createElement('code'); path.textContent=result.path;
  const note=document.createElement('p'); note.className='fine'; note.textContent='包内 review.html 包含曲线和逐帧诊断；使用说明.txt 可随时查阅。';
  detail.append(summary,path,note); box.append(detail);
  $('jointResult').replaceChildren(box); updateJointWorkflow(); saveJointPaths(); status(title.textContent);
}
$('jointPrepareForm').addEventListener('submit',event=>{event.preventDefault();jointAction(async()=>{
  saveJointPaths(); status('正在自动选区，准备四套 ACR 设置…');
  const result=await job('/api/joint/prepare',{work_dir:$('raw').value.trim() || $('jointPreviews').value.trim(),previews:$('jointPreviews').value.trim(),source:$('jointPrevious').value.trim(),region_mode:$('jointRegionMode').value,mid_roi:jointRegions.mid,high_roi:jointRegions.high,window:Number($('jointWindow').value)});
  resetJointPreparation();
  jointAutomaticSelection=result.measurement_selection||null; updateJointRegions();
  $('jointSource').value=result.path; fillJointRenderPaths(jointPath(result.path, 'jpg'));
  jointStage='calibrate'; showJointResult(result);
});});
$('jointCalibrateForm').addEventListener('submit',event=>{event.preventDefault();jointAction(async()=>{
  clearJointCandidate(); jointStage='calibrate'; updateJointWorkflow(); saveJointPaths(); status('正在核对四组小图，自动计算联合修正…');
  const result=await job('/api/joint/calibrate',{work_dir:$('raw').value.trim() || $('jointPreviews').value.trim(),source:$('jointSource').value.trim(),baseline_previews:$('jointBaseline').value.trim(),exposure_previews:$('jointExposure').value.trim(),highlights_previews:$('jointHighlights').value.trim(),whites_previews:$('jointWhites').value.trim()});
  if(result.ready) { $('jointSolution').value=result.path; jointStage='verify'; }
  showJointResult(result);
});});
$('jointVerifyForm').addEventListener('submit',event=>{event.preventDefault();jointAction(async()=>{
  jointLastResult=null; updateJointWorkflow(); saveJointPaths(); status('正在检查实际亮度与高光连续性…');
  const result=await job('/api/joint/verify',{work_dir:$('raw').value.trim() || $('jointPreviews').value.trim(),source:$('jointSolution').value.trim(),previews:$('jointVerified').value.trim()});
  acceptJointVerified($('jointVerified').value, $('jointSolution').value.trim()); showJointResult(result);
});});


function updateVerifyButton() {
  const reason = !token ? '缺少本机会话密钥，请从启动链接打开工作台。'
    : busy ? '正在处理任务，请等待完成。'
    : !project ? '尚无分析结果，请先分析序列。'
    : !result ? '分析目标已失效，请点击“更新预览”；素材或参考区域改变后需重新分析。'
    : !$('verifyPath').value.trim() ? '请选择 ACR 重新导出的小图文件夹。' : '';
  $('verify').disabled = Boolean(reason);
  $('verify').title = reason;
  $('verifyReason').textContent = [reason, verificationFailure].filter(Boolean).join(' ') || '将对照当前已保存的亮度目标复核，无需导出修正包。';
  updateFeedbackButton();
}
$('verifyPath').addEventListener('input', () => { verificationFailure = ''; updateVerifyButton(); });

async function restoreVerificationTarget() {
  const generation = analysisGeneration;
  if ($('raw').value.trim()) await request('/api/workspace', {work_dir:$('raw').value.trim()});
  const state = await request('/api/verification-target');
  if (analysisGeneration !== generation || project) return;
  if (!state.target) {
    verificationFailure = state.reason || '';
    $('verifyReason').textContent = verificationFailure || '尚无分析结果，请先分析序列。';
    return;
  }
  const target = state.target;
  project = {...target.public, written:state.written, readOnly:!state.editable};
  reconcileFeedbackTarget(target.result);
  result = target.result;
  exposureCalibration = state.exposure_calibration || null;
  if (exposureCalibration) {
    $('exposureCalibrationResult').textContent = `已恢复曝光校准包：${exposureCalibration.path}。可继续导入两组显影小图；修改亮度目标无需重新生成。`;
    try {
      const saved = JSON.parse(sessionStorage.getItem('timelapse-exposure-inputs') || '{}');
      if (saved.id === project.id && saved.calibration_id === exposureCalibration.calibration_id) { $('exposureBaseline').value = saved.baseline || ''; $('exposureProbe').value = saved.probe || ''; }
    } catch (_) {}
  }
  $('sameFolder').checked = false; syncSameFolder();
  $('raw').value = target.raw_dir; $('previews').value = target.preview_dir;
  $('mode').value = project.mode; referenceRegion = [...target.roi]; updateRegionSummary();
  for (const id of ['strength','window','start','end']) $(id).value = result.settings[id];
  $('strengthValue').textContent = `${Math.round(Number($('strength').value)*100)}%`;
  $('smoothGaps').checked = result.settings.smooth_gaps;
  $('controls').disabled = project.readOnly || project.written;
  $('scrub').disabled = false; $('play').disabled = false;
  $('scrub').max = project.frames.length - 1; $('scrub').value = 0;
  $('count').textContent = `${project.frames.length} 帧 · 已恢复分析目标`;
  $('notes').textContent = project.report.notes.join(' ');
  renderMeasurement(project.report.measurement); renderEvents();
  document.querySelectorAll('.event input').forEach(input => {
    input.checked = result.settings.selected.includes(Number(input.value));
    input.disabled = project.readOnly || project.written;
  });
  updateEventSelection(); renderRows(); draw(); renderResponse(); updateWriteButtons();
  updateBackupLocation(); syncJointImport(); fillFeedbackPaths(); updateVerifyButton();
  await showFrame();
  status(project.readOnly ? '已恢复本机保存的分析目标，可直接选择小图复核。调整目标或导出 XMP 需重新分析。' : '已恢复当前分析目标，可直接选择小图复核，无需导出。');
}
updateExportDestination();
updateWriteButtons();
updateVerifyButton();
if (token) action(async () => {
  try { await restoreVerificationTarget(); }
  catch (e) { verificationFailure = `无法恢复分析目标：${e.message}`; status(verificationFailure, true); }
});
