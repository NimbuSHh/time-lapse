const {test}=require('node:test');
const assert=require('node:assert/strict');
const {describe}=require('../../timelapse/static/progress.js');
const task=(phase,done=0,state='running')=>({phase,done,total:810,state});
test('overall progress never resets across all phases and two rounds',()=>{
  const phases=['连接 Photoshop','自动生成原始预览','第 1 轮准备校准',
    '第 1 轮自动显影 · 基准','第 1 轮自动显影 · 曝光','第 1 轮自动显影 · 高光',
    '第 1 轮自动显影 · 白色色阶','第 1 轮计算修正','第 1 轮自动复核显影','第 1 轮检查效果',
    '第 2 轮准备校准','第 2 轮自动显影 · 基准','第 2 轮自动显影 · 曝光','第 2 轮自动显影 · 高光',
    '第 2 轮自动显影 · 白色色阶','第 2 轮计算修正','第 2 轮自动复核显影','第 2 轮检查效果','导出全尺寸 JPG','整理结果','完成'];
  let previous=0;
  for(const phase of phases) for(const done of [0,405,810]) {
    const value=describe(task(phase,done)).percent;
    assert.ok(value>=previous,`${phase}: ${value} < ${previous}`); assert.ok(value<100); previous=value;
  }
  assert.equal(describe(task('完成',810,'done')).percent,100);
});
test('one-round completion skips optional stages without going backwards',()=>{
  const checked=describe(task('第 1 轮检查效果'));
  const final=describe(task('导出全尺寸 JPG'));
  assert.ok(final.percent>checked.percent);
  const complete=describe({...task('完成',810,'done'),result:{rounds:1}});
  assert.equal(complete.percent,100);
  assert.deepEqual(complete.stages.slice(6,11).map(s=>s.status),Array(5).fill('已跳过'));
  assert.equal(checked.stages[5].status,'当前阶段');
  assert.equal(checked.stages[6].status,'按需执行');
});
test('current stage count remains separate from overall progress and survives reload',()=>{
  const expected=describe(task('第 1 轮自动显影 · 高光',405));
  assert.equal(expected.percent,3.5/12*100);
  assert.match(expected.stageText,/405 \/ 810 帧/);
  assert.deepEqual(describe({phase:'第 1 轮自动显影 · 高光',progress:[405,810],state:'running'}),expected);
  assert.equal(describe(task('第 1 轮计算修正')).stageText,'第 1 轮计算修正');
});
test('unknown, failed, and stopped states cannot claim completion',()=>{
  assert.equal(describe(task('future-phase')).percent,null);
  for(const state of ['error','stopped','running']) assert.ok(describe(task('整理结果',810,state)).percent<100);
});
