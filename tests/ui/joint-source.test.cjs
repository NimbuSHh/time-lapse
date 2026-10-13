const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');

// Run the actual page handlers with an in-memory DOM and mocked job responses.
// No source images, file picker, or server writes are involved.
function page(saved = new Map(), authenticated = false) {
  class Element {
    constructor() { this.value = ''; this.dataset = {}; this.checked = false; this.handlers = {}; this.classList = {add(){}, remove(){}, toggle(){}}; }
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); }
    dispatchEvent(e) { for (const fn of this.handlers[e.type] || []) fn(e); }
    replaceChildren(...children) { this.children = children; }
    append(...children) { (this.children ||= []).push(...children); }
    setAttribute() {}
  }
  const elements = Object.fromEntries([...fs.readFileSync(path.join(root, 'timelapse/static/index.html'), 'utf8').matchAll(/id="([^"]+)"/g)].map(m => [m[1], new Element()]));
  elements.jointRegionMode.value = 'auto'; elements.jointWindow.value = '31';
  const ctx = vm.createContext({
    document: {getElementById: id => elements[id], querySelectorAll: selector => {
      const inputs = (elements.events.children || []).map(row => row.children[0]).filter(input => input.type === 'checkbox');
      if (selector === '.event input:not(:disabled)') return inputs.filter(input => !input.disabled);
      if (selector === '.event input:checked') return inputs.filter(input => input.checked);
      return [];
    }, querySelector: () => new Element(), createElement: () => new Element(), createDocumentFragment: () => new Element()},
    window: {addEventListener(){}}, ResizeObserver: class {observe(){}},
    sessionStorage: {getItem: k => saved.get(k), setItem: (k,v) => saved.set(k,v)},
    fetch: async url => ({ok:true, json:async()=>url==='/api/config'?{output_root:'/outputs'}:{target:null}}),
    location: {hash:authenticated?'#token=test':''}, history: {replaceState(){}}, URLSearchParams, Event, Option: Element,
    setTimeout, console,
  });
  vm.runInContext(fs.readFileSync(path.join(root, 'timelapse/static/app.js'), 'utf8'), ctx);
  const input = (id, value) => { elements[id].value = value; elements[id].dispatchEvent(new Event('input')); };
  const fire = async (id, type = 'click') => {
    for (const fn of elements[id].handlers[type] || []) await fn({preventDefault(){}});
    await new Promise(resolve => setImmediate(resolve));
  };
  return {elements, ctx, saved, input, fire, read: expression => vm.runInContext(expression, ctx)};
}

test('warning estimates remain selectable and unestimated events have no disabled checkbox', async () => {
  const p = page();
  p.read(`project = {frames:Array.from({length:35}, (_,i)=>({name:'frame'+i})), report:{events:[
    {index:20,camera_delta:-1.06,xmp_delta:0,jump:-1.56,selectable:true,reason:'待确认',spatial_support:1,tile_dispersion:.244},
    {index:30,end_index:33,indices:[30,31,33],method:'dense-cluster',camera_delta:-1,xmp_delta:0,jump:-1.49,selectable:true,reason:'待确认',spatial_support:.938,tile_dispersion:.246},
    {index:10,camera_delta:-1,xmp_delta:0,jump:-1,selectable:true,recommended:false,reason:'同向跳变支持不足'},
    {index:1,camera_delta:-1,xmp_delta:0,jump:null,selectable:false,reason:'窗口样本不足'}
  ]}}; invalidate=()=>{}; showFrame=async()=>{}; renderEvents();`);
  const rows = p.elements.events.children;
  assert.equal(rows.filter(row => row.children[0].type === 'checkbox').length, 3);
  assert.ok(rows.slice(0,3).every(row => !row.children[0].disabled));
  const details = rows.slice(0,3).map(row => row.children[1].children[1]);
  assert.match(details[0].children[3].textContent, /100.0%.*0.244 EV（仅供参考）/);
  assert.match(details[1].children[3].textContent, /93.8%/);
  assert.equal(details[2].children[0].textContent, '需人工判断 · 可以勾选');
  assert.equal(details[2].open, true);
  const unavailable = rows[3];
  assert.equal(unavailable.children[0].textContent, '未生成估计（1 处）');
  assert.equal(unavailable.children[1].children[0].type, undefined);
  const location = unavailable.children[1].children[1].children[0];
  location.dispatchEvent(new Event('click'));
  assert.equal(p.elements.scrub.value, 1);
  p.elements.selectAllEvents.checked = true;
  await p.fire('selectAllEvents', 'change');
  assert.equal(p.read('JSON.stringify(settings().selected)'), '[20,30,10]');
  assert.equal(p.elements.selectAllEvents.checked, true);
  rows[0].children[0].checked = false;
  rows[0].children[0].dispatchEvent(new Event('change'));
  assert.equal(p.read('JSON.stringify(settings().selected)'), '[30,10]');
  assert.equal(p.elements.selectAllEvents.indeterminate, true);
});

test('first import, same-folder RAW changes and manual override follow page events', async () => {
  const p = page();
  p.input('previews', '/sequence/initial');
  assert.equal(p.elements.jointPreviews.value, '/sequence/initial');
  assert.equal(p.elements.jointPrepare.disabled, false);
  p.input('jointPreviews', '/manual');
  p.input('mode', 'manual');
  assert.equal(p.elements.jointPreviews.value, '/manual');
  await p.fire('jointUseAutomatic');
  assert.equal(p.elements.jointPreviews.value, '/sequence/initial');
  p.elements.sameFolder.checked = true;
  p.input('raw', '/other');
  assert.equal(p.elements.jointPreviews.value, '/other');
});

test('only completed verification promotes a source, including a result with residuals', async () => {
  const p = page(); p.input('previews', '/initial');
  p.input('jointVerified', '/unverified'); p.input('verifyPath', '/also-unverified');
  assert.equal(p.elements.jointPreviews.value, '/initial');
  p.input('jointSolution', '/joint-package'); p.input('jointVerified', '/unverified');
  p.read("job = async () => { throw Error('source mismatch'); }");
  await p.fire('jointVerifyForm', 'submit');
  assert.equal(p.elements.jointPreviews.value, '/initial');
  p.read("job = async () => ({status:'render-measured-residual-remains', path:'/report', count:12});");
  await p.fire('jointVerifyForm', 'submit');
  assert.equal(p.elements.jointPreviews.value, '/unverified');
  assert.equal(p.elements.jointPrevious.value, '/joint-package');
  assert.match(p.elements.jointPreviewOrigin.textContent, /最近一次 ACR 复核/);
  p.input('jointPreviews', '/manual');
  p.input('jointSolution', '/next-package');
  p.input('jointVerified', '/next-render');
  await p.fire('jointVerifyForm', 'submit');
  assert.equal(p.elements.jointPreviews.value, '/next-render');
  assert.equal(p.elements.jointUseAutomatic.hidden, true);
});

test('step 06 verification updates the same source and clears unrelated joint lineage', async () => {
  const p = page(); p.input('previews', '/initial'); p.input('jointPrevious', '/old-joint');
  p.input('verifyPath', '/step06');
  p.read("project={id:'p'}; result={revision:'r'}; exported=null; showFrame=async()=>{}; draw=()=>{}; job=async()=>({actual:[],rmse_ev:0,median_bias_ev:0,trend_error_ev:0,max_clipped_fraction:0,unchanged_previews:0,events:[],note:''});");
  await p.fire('verify');
  assert.equal(p.elements.jointPreviews.value, '/step06');
  assert.equal(p.elements.jointPrevious.value, '');
});

test('refresh preserves source and import context; changing sequence drops stale sources and grids', async () => {
  let p = page(); p.input('raw', '/raw'); p.input('previews', '/initial');
  p.read("acceptJointVerified('/latest', '/package');");
  p = page(p.saved);
  assert.equal(p.elements.previews.value, '/initial');
  assert.equal(p.elements.raw.value, '/raw');
  assert.equal(p.elements.jointPreviews.value, '/latest');
  p.input('jointPreviews', '/manual');
  p = page(p.saved);
  assert.equal(p.elements.jointPreviews.value, '/manual');
  await p.fire('jointUseAutomatic');
  assert.equal(p.elements.jointPreviews.value, '/latest');
  p.read("jointAutomaticSelection = {old:true};");
  p.input('previews', '/new-sequence');
  assert.equal(p.elements.jointPreviews.value, '/new-sequence');
  assert.equal(p.elements.jointPrevious.value, '');
  assert.equal(p.read('jointAutomaticSelection'), null);
});

test('prepare sends the inherited source without any directory input in 04B', async () => {
  const p = page(); p.input('previews', '/initial');
  p.read("job=async(route,data)=>{ globalThis.sent={route,data}; return {path:'/new-package',count:12,status:'awaiting-acr-calibration'}; };");
  await p.fire('jointPrepareForm', 'submit');
  assert.equal(p.read('sent.route'), '/api/joint/prepare');
  assert.equal(p.read('sent.data.previews'), '/initial');
});

test('new UI stops before sending joint requests to a backend without joint support', async () => {
  const p = page();
  p.read("globalThis.calls=[]; request=async(path)=>{calls.push(path);return {output_root:'/outputs'};};");
  await assert.rejects(p.read("job('/api/joint/regions', {previews:'/initial'})"), /当前后台不支持联合调色/);
  assert.equal(p.read('calls.join()'), '/api/config');
});

test('compatible backend accepts joint jobs without an analysis id', async () => {
  const p = page();
  p.read("request=async(path,data)=>path==='/api/config'?{joint_api_version:1}:path.startsWith('/api/job')?{state:'done',result:{selection:'ok'}}:{job:'j'};");
  assert.equal((await p.read("job('/api/joint/regions', {previews:'/initial'})")).selection, 'ok');
});

async function exposurePage() {
  const p = page();
  p.read(`
    project={id:'p',frames:[{name:'frame01',exposure:0},{name:'frame02',exposure:0}]};
    draw=()=>{globalThis.draws=(globalThis.draws||0)+1;}; showFrame=async()=>{};
    globalThis.waiting={revision:1,target:[.1,.2],calibration_required:true,blocked:[],delta:[null,null],final:[null,null]};
    globalThis.measured={...waiting,revision:2,calibration_required:false,delta:[.2,.4],final:[.2,.4],response:{kind:'measured-exposure',gain:.5,gains:[.5,.5]}};
    globalThis.calls=[];
    job=async(route,data)=>{calls.push({route,data});
      if(route==='/api/exposure/prepare') return {calibration_id:'c1',path:'/calibration',count:2};
      if(route==='/api/exposure/calibrate') return measured;
      if(route==='/api/calculate') return waiting;
      if(route==='/api/export') return {path:'/corrected',count:2,package_id:'e2'};
      throw Error('unexpected route '+route);
    };
  `);
  await p.fire('calculate');
  return p;
}

test('missing exposure response guides to two-render calibration and preserves the target through export', async () => {
  const p = await exposurePage();
  assert.match(p.elements.status.textContent, /曝光校准（保留当前目标）/);
  assert.doesNotMatch(p.elements.status.textContent, /联合调色/);
  assert.equal(p.elements.exposureCalibration.open, true);
  assert.equal(p.elements.export.disabled, true);
  assert.equal(p.elements.exposurePrepare.disabled, false);
  assert.equal(p.elements.exposureCalibrate.disabled, true);
  await p.fire('exposurePrepare');
  assert.equal(p.read('JSON.stringify(calls[1])'), JSON.stringify({route:'/api/exposure/prepare',data:{id:'p',revision:1}}));
  assert.match(p.elements.exposureCalibrationResult.textContent, /baseline\/.*probe-exposure\//s);
  assert.match(p.elements.exposureCalibrationResult.textContent, /独立 RAW.*保留元数据/s);
  p.input('exposureBaseline', '/renders/baseline'); p.input('exposureProbe', '/renders/exposure');
  assert.equal(p.elements.exposureCalibrate.disabled, false);
  p.read("exported={package_id:'old'}; actual=[9,9];");
  p.elements.exportResult.textContent = 'old export'; p.elements.verification.textContent = 'old verification';
  await p.fire('exposureCalibrateForm', 'submit');
  assert.equal(p.read('JSON.stringify(calls[2])'), JSON.stringify({route:'/api/exposure/calibrate',data:{id:'p',revision:1,calibration_id:'c1',baseline_previews:'/renders/baseline',exposure_previews:'/renders/exposure'}}));
  assert.equal(p.read('JSON.stringify(result.target)'), '[0.1,0.2]');
  assert.equal(p.read('result.revision'), 2);
  assert.equal(p.read('exported'), null); assert.equal(p.read('actual'), null);
  assert.equal(p.elements.exportResult.textContent, ''); assert.equal(p.elements.verification.textContent, '');
  assert.equal(p.elements.verify.disabled, true);
  assert.equal(p.elements.imageKind.value, 'original');
  assert.equal(p.elements.export.disabled, false);
  assert.equal(p.elements.applyXmp.disabled, true);
  assert.match(p.elements.responseStatus.textContent, /已实测逐帧.*0.500/);
  assert.equal(p.elements.rows.children[0].children[0].children[3].textContent, '0.200');
  assert.ok(p.read('draws') >= 2);
  assert.equal(p.read('exposureCalibration.calibration_id'), 'c1');
  p.elements.acrClosed.checked = true; await p.fire('acrClosed', 'change');
  assert.equal(p.elements.applyXmp.disabled, false);
  await p.fire('export');
  assert.equal(p.read('JSON.stringify(calls[3])'), JSON.stringify({route:'/api/export',data:{id:'p',revision:2,destination:'source_subdir',output_dir:'',acr_closed:false}}));
  assert.equal(p.elements.verify.disabled, true); // Export cannot bypass missing session/path prerequisites.
});

test('failed calibration retains the target and never enables export or writeback', async () => {
  const p = await exposurePage(); await p.fire('exposurePrepare');
  p.input('exposureBaseline', '/baseline'); p.input('exposureProbe', '/probe');
  p.read("job=async()=>{throw Error('显影来源不匹配');};");
  p.elements.acrClosed.checked = true;
  await p.fire('exposureCalibrateForm', 'submit');
  assert.match(p.elements.status.textContent, /显影来源不匹配/);
  assert.equal(p.read('result.revision'), 1);
  assert.equal(p.read('JSON.stringify(result.target)'), '[0.1,0.2]');
  assert.equal(p.elements.export.disabled, true); assert.equal(p.elements.applyXmp.disabled, true);
});

test('measured response does not bypass blocked exposure limits and is displayed after recalculation', async () => {
  const p = await exposurePage(); await p.fire('exposurePrepare');
  p.input('exposureBaseline', '/baseline'); p.input('exposureProbe', '/probe');
  p.read('measured.blocked=[0];');
  await p.fire('exposureCalibrateForm', 'submit');
  assert.equal(p.read('result.calibration_required'), false);
  assert.equal(p.elements.export.disabled, true); assert.equal(p.elements.applyXmp.disabled, true);
  assert.match(p.elements.status.textContent, /1 帧超过/);
  p.read('job=async()=>({...measured,revision:3,blocked:[]});');
  await p.fire('calculate');
  assert.match(p.elements.responseStatus.textContent, /已实测逐帧/);
  assert.equal(p.elements.export.disabled, false);
});

test('same-source calibration survives parameter and revision changes, but sequence and writeback invalidate it', async () => {
  for (const change of ['parameter','revision','sequence','written']) {
    const p = await exposurePage(); await p.fire('exposurePrepare');
    p.input('exposureBaseline', '/baseline'); p.input('exposureProbe', '/probe');
    if (change === 'parameter') { p.input('strength', '.8'); await p.fire('calculate'); }
    if (change === 'revision') p.read('result={...result,revision:99}; updateWriteButtons();');
    if (change === 'sequence') p.input('previews', '/new-sequence');
    if (change === 'written') p.read('project.written=true; updateWriteButtons();');
    if (change === 'parameter' || change === 'revision') {
      assert.equal(p.elements.exposureCalibrate.disabled, false, change);
      assert.equal(p.read('exposureCalibration.calibration_id'), 'c1');
      const revision = p.read('result.revision');
      p.read("job=async(route,data)=>{calls.push({route,data});return measured;};");
      await p.fire('exposureCalibrateForm','submit');
      assert.equal(p.read('calls.at(-1).data.revision'), revision);
    } else {
      assert.equal(p.elements.exposureCalibrate.disabled, true, change);
      const count = p.read('calls.length');
      await p.fire('exposureCalibrateForm', 'submit');
      assert.equal(p.read('calls.length'), count, change);
      assert.match(p.elements.status.textContent, /没有有效的曝光校准包/, change);
    }
  }
});


test('verification needs a target and directory, never an export or calibrated exposure', async () => {
  const p = page(new Map(), true);
  await new Promise(resolve => setImmediate(resolve));
  assert.match(p.elements.verifyReason.textContent, /尚无分析/);
  p.read("project={id:'p'}; result={revision:'r',calibration_required:true,blocked:[1]}; verificationFailure=''; updateVerifyButton();");
  assert.equal(p.elements.verify.disabled, true);
  assert.match(p.elements.verifyReason.textContent, /请选择.*小图/);
  p.input('verifyPath',' /rendered ');
  assert.equal(p.elements.verify.disabled, false);
  p.read("globalThis.sent=null; job=async(route,data)=>{sent={route,data}; return {actual:[],rmse_ev:0,median_bias_ev:0,trend_error_ev:0,max_clipped_fraction:0,unchanged_previews:0,events:[],note:''};}; showFrame=async()=>{}; draw=()=>{};");
  await p.fire('verify');
  assert.equal(p.read('exported'), null);
  assert.equal(p.read('JSON.stringify(sent)'), JSON.stringify({route:'/api/verify',data:{id:'p',revision:'r',preview_dir:'/rendered'}}));
  assert.equal(p.elements.verify.disabled, false);
  p.read('invalidate();');
  assert.equal(p.elements.verify.disabled, true);
  assert.match(p.elements.verifyReason.textContent, /更新预览/);
});

test('verification failure is visible beside the button and can be retried', async () => {
  const p = page(new Map(), true); await new Promise(resolve => setImmediate(resolve));
  p.read("project={id:'p'}; result={revision:'r'}; verificationFailure=''; job=async()=>{throw Error('复核 JPEG 与原序列未一一对应');};");
  p.input('verifyPath','/bad'); await p.fire('verify');
  assert.equal(p.elements.verify.disabled, false);
  assert.match(p.elements.verifyReason.textContent, /一一对应/);
  p.input('verifyPath','/fixed');
  assert.doesNotMatch(p.elements.verifyReason.textContent, /失败/);
  p.read("busy=true; updateVerifyButton();");
  assert.equal(p.elements.verify.disabled, true);
  assert.match(p.elements.verifyReason.textContent, /正在处理/);
});

test('fresh page restores the saved target without recalculation and forbids stale writes', async () => {
  const p = page(new Map(), true); await new Promise(resolve => setImmediate(resolve));
  p.read(`
    globalThis.savedTarget={snapshot_id:'saved',raw_dir:'/raw',preview_dir:'/original',roi:[0,0,1,1],
      public:{id:'saved',mode:'manual',roi:[0,0,1,1],frames:[{name:'a',exposure:0}],report:{events:[],notes:[]}},
      result:{revision:'frozen',target:[-.5],delta:[0],final:[0],blocked:[],settings:{selected:[],strength:.6,window:31,start:0,end:0,smooth_gaps:true}}};
    request=async()=>({target:savedTarget,editable:false,written:false});
    renderMeasurement=()=>{}; renderRows=()=>{}; draw=()=>{}; renderResponse=()=>{}; showFrame=async()=>{};
    job=()=>{throw Error('must not recalculate');};
  `);
  await p.read('restoreVerificationTarget()');
  p.input('verifyPath','/rendered');
  assert.equal(p.read('result.revision'),'frozen');
  assert.equal(p.read('result.target[0]'),-.5);
  assert.equal(p.elements.verify.disabled,false);
  assert.equal(p.elements.controls.disabled,true);
  assert.equal(p.elements.export.disabled,true);
  assert.equal(p.elements.exposurePrepare.disabled,true);
  p.elements.acrClosed.checked=true; await p.fire('acrClosed','change');
  assert.equal(p.elements.applyXmp.disabled,true);
  assert.equal(p.elements.raw.value,'/raw');
  assert.match(p.elements.status.textContent,/已恢复/);
});


test('uncertain review carries measured source forward without claiming validation passed', async () => {
  const p = page(new Map(), true); await new Promise(resolve => setImmediate(resolve));
  p.input('previews', '/original'); p.input('verifyPath', '/review');
  p.read(`
    project={id:'p'}; result={revision:'r'}; showFrame=async()=>{}; draw=()=>{};
    job=async()=>({actual:[-2,-2],rmse_ev:.1,median_bias_ev:-.1,trend_error_ev:0,max_clipped_fraction:0,unchanged_previews:0,events:[],note:'目标未改变。',
      measurement_status:'needs-review',total_frames:2,comparable_frames:1,rmse_bounds_ev:[.1,.101],
      uncertain_frames:[{frame:2,name:'a.ARW',observed_ev:-2,target_ev:-1.9,support_pixels:70000,clipped_pixels:40,dark_pixels:0,brightness_bounds_ev:[-2.002,-2],residual_bounds_ev:[-.102,-.1]}]});
  `);
  await p.fire('verify');
  assert.equal(p.elements.verify.disabled, false);
  assert.match(p.elements.verification.textContent, /已测量 2 帧.*1 帧通过.*1 帧需/);
  assert.match(p.elements.verification.textContent, /不能视为整批已通过/);
  assert.match(p.elements.verification.textContent, /0.100000 至 0.101000/);
  assert.equal(p.elements.verificationWarnings.hidden, false);
  assert.match(p.elements.verificationFrameInfo.textContent, /40 个进入高光/);
  assert.match(p.elements.verificationFrameInfo.textContent, /-2.002000 至 -2.000000/);
  assert.equal(p.elements.jointPreviews.value, '/review');
  assert.match(p.elements.jointPreviewOrigin.textContent, /不代表校验通过/);
  assert.match(p.elements.status.textContent, /已完成 2 帧/);
  await p.fire('verificationLocate');
  assert.equal(p.elements.scrub.value, 1);
  assert.equal(p.elements.imageKind.value, 'verified');
  p.read('invalidate()');
  assert.equal(p.elements.verificationWarnings.hidden, true);
  assert.equal(p.read('verificationReview'), null);
});


test('feedback paths follow export and writeback, preserve manual edits and clear another sequence after reload', async () => {
  let p = page(); p.input('raw','/A/raw'); p.input('previews','/A/original'); p.input('verifyPath','/A/render');
  p.read("fillFeedbackPaths('/A/package','/A/package/corrected');");
  assert.equal(p.elements.feedbackXmps.value,'/A/package/corrected');
  p.input('verifyPath','/A/new-render');
  assert.equal(p.elements.feedbackXmps.value,'/A/package/corrected');
  p = page(p.saved);
  assert.equal(p.elements.feedbackXmps.value,'/A/package/corrected');
  assert.equal(p.elements.feedbackPreviews.value,'/A/new-render');
  p.input('feedbackXmps','/A/manual'); p.input('verifyPath','/A/third-render');
  assert.equal(p.elements.feedbackXmps.value,'/A/manual');
  p = page(p.saved); p.input('raw','/B/raw'); p.input('previews','/B/original');
  assert.equal(p.elements.feedbackSource.value,'');
  assert.equal(p.elements.feedbackOriginal.value,'/B/original');
  assert.equal(p.elements.feedbackXmps.value,'/B/raw');
  assert.equal(p.elements.feedbackPreviews.value,'');
  assert.equal(p.elements.verifyPath.value,'');
  p.read("fillFeedbackPaths('/B/backup','/B/raw');");
  assert.equal(p.elements.feedbackXmps.value,'/B/raw');
});

test('feedback accepts the saved target without export and renders actionable partial candidates', async () => {
  const p = page(new Map(),true); await new Promise(resolve=>setImmediate(resolve));
  p.input('raw','/raw'); p.input('previews','/original'); p.input('verifyPath','/render');
  assert.equal(p.elements.feedbackSubmit.disabled,true);
  assert.match(p.elements.feedbackReason.textContent,/先分析/);
  p.read(`project={id:'p'}; result={revision:7}; updateVerifyButton();
    job=async(route,data)=>{globalThis.sent={route,data}; return {path:'/next/package',candidate_generated:true,ready:false,partial:true,count:2,changed_count:1,held_count:1,audit_counts:{verified:2},rmse_before_ev:.1,issues:[{frame:2,name:'b.ARW',reason:'缺少曝光响应',action:'保留当前曝光，补充校准'}]};};`);
  assert.equal(p.elements.feedbackSubmit.disabled,false);
  await p.fire('feedbackForm','submit');
  assert.equal(p.read('sent.data.source'),''); assert.equal(p.read('sent.data.id'),'p'); assert.equal(p.read('sent.data.revision'),7);
  const box=p.elements.feedbackResult.children[0];
  assert.match(box.children[0].textContent,/部分候选.*1 帧调整.*1 帧保持/);
  assert.match(box.children[4].textContent,/不代表全序列通过/);
  assert.match(box.children[5].children[0].textContent,/第 2 帧.*缺少曝光响应.*补充校准/);
  box.children.at(-1).dispatchEvent(new Event('click'));
  assert.equal(p.elements.feedbackSource.value,'/next/package');
  assert.equal(p.elements.feedbackXmps.value,'/next/package/corrected');
  assert.equal(p.elements.feedbackOriginal.value,'/original');
  assert.equal(p.elements.feedbackPreviews.value,''); assert.equal(p.elements.verifyPath.value,'');
  assert.equal(p.elements.feedbackSubmit.disabled,true);
  p.input('verifyPath','/next-render');
  assert.equal(p.elements.feedbackPreviews.value,'/next-render');
  assert.equal(p.elements.feedbackXmps.value,'/next/package/corrected');
});

test('feedback report-only result exposes precise reasons and has no next-round action', async () => {
  const p=page(new Map(),true); await new Promise(resolve=>setImmediate(resolve));
  p.input('feedbackSource','/saved-package'); p.input('feedbackOriginal','/original'); p.input('feedbackXmps','/working'); p.input('feedbackPreviews','/render');
  p.read(`job=async(route,data)=>{globalThis.sent=data; return {path:'/report',ready:false,candidate_generated:false,count:1,audit_counts:{mismatch:1},issues:[{frame:1,name:'a.ARW',reason:'曝光设置不一致',action:'选择本次显影使用的 XMP'}]};};`);
  await p.fire('feedbackForm','submit');
  assert.equal(p.read('sent.id'),undefined);
  const box=p.elements.feedbackResult.children[0];
  assert.match(box.children[0].textContent,/未生成可应用/);
  assert.match(box.children.at(-1).children[0].textContent,/曝光设置不一致.*本次显影/);
  assert.equal(box.children.some(c=>c.type==='button'),false);
});

test('disabled export and calibration controls always show the missing prerequisite', async () => {
  const p=await exposurePage();
  assert.match(p.elements.exportReason.textContent,/曝光校准/);
  assert.match(p.elements.exposureCalibrateReason.textContent,/先生成/);
  await p.fire('exposurePrepare');
  assert.match(p.elements.exposureCalibrateReason.textContent,/baseline/);
  p.input('exposureBaseline','/baseline');
  assert.match(p.elements.exposureCalibrateReason.textContent,/probe-exposure/);
  p.read('result=measured; updateWriteButtons();');
  assert.match(p.elements.applyReason.textContent,/关闭 ACR/);
});


test('restored editable target recovers its calibration and syncs feedback to the recovered sequence', async () => {
  const p = page(new Map(),true); await new Promise(resolve=>setImmediate(resolve));
  p.input('raw','/old/raw'); p.input('previews','/old/original'); p.input('verifyPath','/old/render');
  p.read(`fillFeedbackPaths('/old/package','/old/package/corrected');
    sessionStorage.setItem('timelapse-exposure-inputs',JSON.stringify({id:'saved',calibration_id:'c1',baseline:'/baseline',probe:'/probe'}));
    globalThis.savedTarget={snapshot_id:'saved',raw_dir:'/raw',preview_dir:'/original',roi:[0,0,1,1],
      public:{id:'saved',mode:'manual',roi:[0,0,1,1],frames:[{name:'a',exposure:0}],report:{events:[],notes:[]}},
      result:{revision:'new',target:[-.5],delta:[0],final:[0],blocked:[],calibration_required:true,settings:{selected:[],strength:.6,window:31,start:0,end:0,smooth_gaps:true}}};
    request=async()=>({target:savedTarget,editable:true,written:false,exposure_calibration:{id:'saved',revision:'old',calibration_id:'c1',path:'/calibration'}});
    renderMeasurement=()=>{}; renderRows=()=>{}; draw=()=>{}; renderResponse=()=>{}; showFrame=async()=>{};
  `);
  await p.read('restoreVerificationTarget()');
  assert.equal(p.elements.controls.disabled,false);
  assert.equal(p.elements.exposureCalibrate.disabled,false);
  assert.equal(p.read('exposureCalibration.calibration_id'),'c1');
  assert.equal(p.elements.exposureBaseline.value,'/baseline');
  assert.match(p.elements.exposureCalibrationResult.textContent,/已恢复.*校准包/);
  assert.equal(p.elements.feedbackSource.value,'');
  assert.equal(p.elements.feedbackOriginal.value,'/original');
  assert.equal(p.elements.feedbackXmps.value,'/raw');
  assert.equal(p.elements.feedbackPreviews.value,'');
  p.read("job=async(route,data)=>{globalThis.sent=data;return {...savedTarget.result,calibration_required:false};};");
  await p.fire('exposureCalibrateForm','submit');
  assert.equal(p.read('sent.revision'),'new');
  assert.equal(p.read('sent.calibration_id'),'c1');
});


test('new target revision replaces only an automatic package source, retaining render evidence', async () => {
  for (const mode of ['automatic','manual','next-round','unchanged','reloaded']) {
    let p=page(); p.input('raw','/raw'); p.input('previews','/original'); p.input('verifyPath','/render');
    p.read("project={id:'p'}; result={revision:1}; fillFeedbackPaths('/old-package','/old-package/corrected');");
    if (mode === 'manual') p.input('feedbackSource','/explicit-package');
    if (mode === 'next-round') { p.read("beginFeedbackRound({path:'/next-package'});"); p.input('verifyPath','/render'); }
    if (mode === 'reloaded') p=page(p.saved);
    p.read(`project={id:'p'}; renderRows=()=>{}; draw=()=>{}; renderResponse=()=>{}; showFrame=async()=>{};
      result={revision:1}; invalidate();`);
    await p.read(`acceptResult({revision:${mode === 'unchanged' ? 1 : 2},blocked:[],calibration_required:false})`);
    const expected = mode === 'manual' ? '/explicit-package' : mode === 'next-round' ? '/next-package' : mode === 'unchanged' ? '/old-package' : '';
    assert.equal(p.elements.feedbackSource.value,expected,mode);
    assert.equal(p.elements.feedbackOriginal.value,'/original',mode);
    assert.equal(p.elements.feedbackXmps.value,mode === 'next-round' ? '/next-package/corrected' : '/old-package/corrected',mode);
    assert.equal(p.elements.feedbackPreviews.value,'/render',mode);
  }
});

test('export offers source subfolder and custom paths without invalidating the preview', async () => {
  const p = page(); p.input('raw', '/素材');
  p.read("project={id:'p'}; result={revision:'r',blocked:[]}; job=async(route,data)=>{globalThis.sent={route,data};return {path:'/素材/timelapse-one/package',count:30};}; updateWriteButtons();");
  assert.match(p.elements.outputLocation.textContent, /^\/素材\/timelapse-/);
  await p.fire('export');
  assert.equal(p.read('sent.data.destination'), 'source_subdir');
  p.elements.exportDestination.value = 'custom';
  await p.fire('exportDestination', 'change');
  assert.equal(p.elements.exportCustomDirectory.hidden, false);
  assert.equal(p.elements.export.disabled, true);
  p.input('exportDirectory', '/自选 目录');
  assert.equal(p.elements.export.disabled, false);
  assert.equal(p.read('result.revision'), 'r');
  assert.equal(p.elements.outputLocation.textContent, '/自选 目录');
  p.read("job=async(route,data)=>{globalThis.sent={route,data};return {path:data.output_dir,count:30,layout:'direct'};};");
  await p.fire('export');
  assert.equal(p.elements.feedbackXmps.value, '/自选 目录');
  assert.equal(p.elements.feedbackSource.value, '');
  assert.match(p.elements.exportResult.textContent, /没有新建子目录/);
  assert.equal(p.read('sent.data.output_dir'), '/自选 目录');
  assert.equal(p.read('sent.data.destination'), 'custom');
});

test('backup export requires confirmation and consumes the live analysis', async () => {
  const p = page(); p.input('raw', '/素材');
  p.read("project={id:'p'}; result={revision:'r',blocked:[]}; refreshBackups=async()=>{}; job=async(route,data)=>{globalThis.sent={route,data};return {backup_path:'/素材/备份/one',raw_dir:'/素材',count:30};};");
  p.elements.exportDestination.value = 'source_backup';
  await p.fire('exportDestination', 'change');
  assert.equal(p.elements.exportAcrConfirmation.hidden, false);
  assert.equal(p.elements.export.disabled, true);
  p.elements.exportClosed.checked = true;
  await p.fire('exportClosed', 'change');
  assert.equal(p.elements.export.disabled, false);
  await p.fire('export');
  assert.equal(p.read('sent.route'), '/api/export');
  assert.equal(p.read('sent.data.acr_closed'), true);
  assert.equal(p.read('project.written'), true);
  assert.equal(p.elements.export.disabled, true);
  assert.match(p.elements.exportResult.textContent, /原 XMP 备份/);
});


test('feedback with no exposure changes is a normal result requiring playback, not a retry or another round', async () => {
  const p=page(new Map(),true); await new Promise(resolve=>setImmediate(resolve));
  p.input('feedbackSource','/saved-package'); p.input('feedbackOriginal','/original'); p.input('feedbackXmps','/working'); p.input('feedbackPreviews','/render');
  p.read(`globalThis.statusCalls=[]; const originalStatus=status; status=(message,error=false)=>{statusCalls.push({message,error});originalStatus(message,error);};
    job=async()=>({path:'/report',status:'no-changes',ready:false,candidate_generated:false,count:1,audit_counts:{verified:1},rmse_before_ev:.001,issues:[]});`);
  await p.fire('feedbackForm','submit');
  const box=p.elements.feedbackResult.children[0];
  assert.equal(box.children[0].textContent,'当前无需额外曝光变更');
  assert.match(box.children[4].textContent,/保持当前曝光.*实际播放检查/);
  assert.equal(box.children.length,5);
  assert.equal(box.children.some(c=>c.type==='button'),false);
  assert.doesNotMatch(box.children.map(c=>c.textContent||'').join(' '),/未通过|重试|修正输入/);
  assert.equal(p.read('statusCalls.at(-1).error'),false);
  assert.match(p.elements.status.textContent,/无需额外曝光变更.*实际播放检查/);
});

test('joint wizard advances with real handlers, remembers progress and routes only the matching paths', async () => {
  let p = page(); p.input('previews', '/initial');
  assert.equal(p.elements.jointStagePrepare.hidden, false);
  assert.equal(p.elements.jointNavCalibrate.disabled, true);
  p.read("job=async()=>({path:'/package',count:12,status:'awaiting-acr-calibration'});");
  await p.fire('jointPrepareForm', 'submit');
  assert.equal(p.elements.jointStageCalibrate.hidden, false);
  assert.equal(p.elements.jointStagePrepare.hidden, true);
  assert.equal(p.elements.jointBaseline.value, '/package/jpg/baseline');
  assert.equal(p.elements.jointWhites.value, '/package/jpg/probe-whites');
  p = page(p.saved);
  assert.equal(p.elements.jointStageCalibrate.hidden, false);
  assert.equal(p.elements.jointRenderPlan.children.length, 4);
  p.input('jointRenderRoot', '/exported');
  p.read("job=async(route,data)=>{globalThis.sent={route,data};return {path:'/solution',ready:true,count:12,status:'candidate-needs-acr-verification'};};");
  await p.fire('jointCalibrateForm', 'submit');
  assert.equal(p.read('sent.data.source'), '/package');
  assert.equal(p.read('sent.data.highlights_previews'), '/exported/probe-highlights');
  assert.equal(p.elements.jointStageVerify.hidden, false);
  assert.equal(p.elements.jointCorrectedPath.textContent, '/solution/corrected');
  p.input('jointVerified', '/rendered');
  p.read("job=async()=>({path:'/review',count:12,status:'render-measured-residual-remains'});");
  await p.fire('jointVerifyForm', 'submit');
  assert.equal(p.elements.jointContinue.hidden, false);
  p = page(p.saved);
  assert.equal(p.elements.jointContinue.hidden, false);
  p.read("job=async(route,data)=>{globalThis.sent={route,data};return {path:'/round2',count:12,status:'awaiting-acr-calibration'};};");
  await p.fire('jointContinue');
  assert.equal(p.read('sent.data.previews'), '/rendered');
  assert.equal(p.read('sent.data.source'), '/solution');
  assert.equal(p.elements.jointSource.value, '/round2');
  assert.equal(p.elements.jointVerified.value, '');
  assert.equal(p.elements.jointSolution.value, '');
  assert.equal(p.elements.jointBaseline.value, '/round2/jpg/baseline');
});

test('partial and failed calibration stay actionable and revoke any earlier candidate', async () => {
  const p=page(); p.input('jointSource','/package'); p.input('jointSolution','/old-solution');
  p.read("job=async()=>({path:'/partial',count:12,blocked_count:7,ready:false,status:'partial-or-unsupported-calibration',issues:[{count:7,reason:'缺少校准小图',action:'补齐全序列'}]});");
  await p.fire('jointCalibrateForm','submit');
  assert.equal(p.elements.jointStageCalibrate.hidden,false);
  assert.equal(p.elements.jointNavVerify.disabled,true);
  assert.equal(p.elements.jointSolution.value,'');
  assert.match(p.elements.jointResult.children[0].children[2].textContent,/7 帧.*补齐全序列/);
  p.read("job=async()=>{throw Error('曝光小图尚未导出');};");
  await p.fire('jointCalibrateForm','submit');
  assert.match(p.elements.jointResult.children[0].textContent,/曝光小图尚未导出/);
  assert.equal(p.elements.jointSource.value,'/package');
  assert.equal(p.elements.jointNavVerify.disabled,true);
});

test('changing preparation settings or sequence clears stale candidates; restoring a path keeps focus in preparation', async () => {
  const p=page(); p.input('previews','/initial');
  p.input('jointSource','/partial-typing');
  assert.equal(p.elements.jointStagePrepare.hidden,false);
  assert.equal(p.elements.jointNavCalibrate.disabled,false);
  p.input('jointSolution','/solution'); p.input('jointVerified','/old-render');
  p.input('jointWindow','61');
  assert.equal(p.elements.jointStagePrepare.hidden,false);
  assert.equal(p.elements.jointSource.value,'');
  assert.equal(p.elements.jointSolution.value,'');
  assert.equal(p.elements.jointVerified.value,'');
  p.input('jointSource','/package'); p.input('jointRenderRoot','/exports');
  p.input('previews','/different');
  assert.equal(p.elements.jointRenderRoot.value,'');
  assert.equal(p.elements.jointNavCalibrate.disabled,true);
});

test('passed verification resumes with final guidance and no unnecessary next-round button', async () => {
  let p=page(); p.input('jointSolution','/solution'); await p.fire('jointNavVerify');
  p.input('jointVerified','/rendered');
  p.read("job=async()=>({path:'/review',count:12,status:'render-measured-pass'});");
  await p.fire('jointVerifyForm','submit');
  p=page(p.saved);
  assert.match(p.elements.jointNext.textContent,/播放确认.*最终成片/);
  assert.equal(p.elements.jointContinue.hidden,true);
  assert.equal(p.elements.jointStageVerify.hidden,false);
});

function automaticPage() {
  const p = page();
  p.elements.autoVideo.pause = () => {};
  p.elements.autoVideo.load = () => {};
  p.elements.autoVideo.removeAttribute = name => { delete p.elements.autoVideo[name]; };
  p.ctx.URL = {createObjectURL: () => 'blob:synthetic-video', revokeObjectURL() {}};
  p.ctx.crypto = require('node:crypto').webcrypto;
  vm.runInContext(fs.readFileSync(path.join(root, 'timelapse/static/progress.js'), 'utf8'), p.ctx);
  vm.runInContext(fs.readFileSync(path.join(root, 'timelapse/static/auto.js'), 'utf8'), p.ctx);
  p.read('automaticSupported=true;');
  p.input('autoRaw', '/photos');
  return p;
}
function previewResult() {
  return {run_id:'run1',state:'awaiting-user-review',count:5,fps:25,width:1280,height:854,
    video:'/photos/Time-lapse工作/run/preview.mp4',sha256:'a'.repeat(64),
    selection_sha256:'b'.repeat(64),target_sha256:'c'.repeat(64),effective_policy_sha256:'d'.repeat(64),
    numeric_conditions_met:true,message:'完整预览已生成'};
}
test('one click submits only RAW selection and stops at video review without exporting', async () => {
  const p = automaticPage();
  p.ctx.preview = previewResult();
  p.read(`calls=[];
    request=async(url,data)=>{calls.push([url,data]);
      if(url==='/api/config') return {automatic_api_version:2,trajectory_api_version:2};
      if(url==='/api/auto/start') return {job:'auto1',output_root:'/photos/Time-lapse工作'};
      if(url==='/api/auto/status') return {job:null,task:{state:'awaiting-user-review',raw_dir:'/photos',result:preview}};
      throw Error(url);
    };`);
  await p.fire('autoForm', 'submit');
  assert.equal(p.read('JSON.stringify(calls[1])'), '["/api/auto/start",{"raw_dir":"/photos"}]');
  assert.equal(p.elements.autoControls.disabled, false);
  assert.equal(p.elements.autoResult.hidden, false);
  assert.equal(p.elements.autoHeading.textContent, '完整视频已生成，等待观看确认');
  assert.equal(p.elements.autoOutput.textContent, '/photos/Time-lapse工作/');
  assert.equal(p.elements.autoResultPath.textContent, p.ctx.preview.video);
  assert.equal(p.elements.autoReviewed.disabled, true);
  assert.equal(p.elements.autoExport.disabled, true);
  assert.equal(p.read("calls.some(([url])=>url==='/api/trajectory/export')"), false);
  p.input('autoRaw', '/other/Time-lapse工作/subset');
  assert.equal(p.elements.autoResult.hidden, true);
  assert.equal(p.read('automaticResult'), null);
  assert.equal(p.elements.autoOutput.textContent, '/other/Time-lapse工作/');
});
test('automatic failure permits resuming without showing success or blocking manual controls', async () => {
  const p = automaticPage();
  p.read(`request=async(url)=>url==='/api/config'?{automatic_api_version:2,trajectory_api_version:2}:url==='/api/auto/start'?{job:'fail'}:{job:null,task:{state:'error',error:'显影参数不一致'}};`);
  await p.fire('autoForm', 'submit');
  assert.equal(p.elements.autoStatus.textContent, '显影参数不一致');
  assert.equal(p.elements.autoStart.textContent, '继续自动处理');
  assert.equal(p.elements.autoResult.hidden, true);
  assert.equal(p.elements.manualFlow.inert, false);
  assert.equal(p.read('autoBusy'), false);
});
test('video hash and explicit confirmation bind full-size export to the reviewed preview', async () => {
  const p = automaticPage();
  const blob = new Blob(['synthetic video bytes']);
  const sha256 = require('node:crypto').createHash('sha256').update(Buffer.from(await blob.arrayBuffer())).digest('hex');
  p.ctx.preview = {...previewResult(),sha256};
  p.ctx.fetch = async () => ({ok:true,blob:async()=>blob});
  p.read(`calls=[]; showAutomatic(preview);
    request=async(url,data)=>{calls.push([url,data]);
      if(url==='/api/trajectory/export')return {job:'export1'};
      if(url==='/api/auto/status')return {job:null,task:{state:'done',result:{...preview,state:'done',delivery:{count:5,path:'/delivery'}}}};
      throw Error(url);
    };`);
  await p.fire('autoExport');
  assert.equal(p.read('calls.length'), 0);
  await p.fire('autoLoadVideo');
  assert.equal(p.elements.autoReviewed.disabled, false);
  assert.equal(p.elements.autoExport.disabled, true);
  assert.equal(p.elements.autoVideo.src, 'blob:synthetic-video');
  p.elements.autoReviewed.checked = true;
  await p.fire('autoReviewed', 'change');
  assert.equal(p.elements.autoExport.disabled, false);
  await p.fire('autoExport');
  const [url,body] = JSON.parse(p.read('JSON.stringify(calls[0])'));
  assert.equal(url, '/api/trajectory/export');
  assert.deepEqual(body, {confirmed:true,run_id:'run1',video_sha256:sha256,
    selection_sha256:p.ctx.preview.selection_sha256,target_sha256:p.ctx.preview.target_sha256,
    effective_policy_sha256:p.ctx.preview.effective_policy_sha256});
  assert.equal(p.elements.autoResultPath.textContent, '/delivery');
  assert.equal(p.elements.autoHeading.textContent, '全尺寸结果已保存');
  assert.equal(p.elements.autoExport.disabled, true);
});
test('changed video bytes cannot enable confirmation or full-size export', async () => {
  const p = automaticPage();
  p.ctx.preview = previewResult();
  p.ctx.fetch = async () => ({ok:true,blob:async()=>new Blob(['different video'])});
  p.read('showAutomatic(preview);');
  await p.fire('autoLoadVideo');
  assert.match(p.elements.autoStatus.textContent, /视频版本已变化/);
  assert.equal(p.elements.autoReviewed.disabled, true);
  assert.equal(p.elements.autoExport.disabled, true);
  assert.equal(p.read('automaticLoadedKey'), '');
});
test('cancel remains reachable while inputs and manual actions are locked', async () => {
  const p = automaticPage();
  p.read(`calls=[]; request=async(url,data)=>{calls.push([url,data]); return {requested:true}}; autoRunning(true);`);
  assert.equal(p.elements.autoControls.disabled, true);
  assert.equal(p.elements.autoCancel.hidden, false);
  assert.equal(p.elements.manualFlow.inert, true);
  await p.fire('autoCancel');
  assert.equal(p.read('JSON.stringify(calls)'), '[["/api/auto/cancel",{}]]');
  assert.equal(p.elements.autoCancel.disabled, true);
  assert.match(p.elements.autoStatus.textContent, /已请求停止/);
});
