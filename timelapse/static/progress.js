'use strict';
// Stable 12-batch budget: initial + up to two five-batch rounds + final.
// Optional rounds are skipped when unnecessary. This is workflow, not time.
var TimelapseProgress = (() => {
  const stages = ['原始预览', '第 1 轮 · 基准', '第 1 轮 · 曝光', '第 1 轮 · 高光', '第 1 轮 · 白色色阶',
    '第 1 轮 · 修正与复核', '第 2 轮 · 基准', '第 2 轮 · 曝光', '第 2 轮 · 高光', '第 2 轮 · 白色色阶',
    '第 2 轮 · 修正与复核', '全尺寸导出'];
  const note = '总进度按最多两轮的流程计算；无需第二轮时会跳过相应阶段。不是耗时百分比。';
  function describe(task) {
    const phase = task.phase || '连接 Photoshop';
    const pair = task.progress || [task.done, task.total || task.count];
    const total = Number(pair[1]), done = Number(pair[0]);
    const fraction = total > 0 && Number.isFinite(done) ? Math.max(0, Math.min(1, done / total)) : 0;
    let units = 0, slot = 0, rendering = false, known = true;
    const round = /^第 ([12]) 轮/.exec(phase);
    if (phase === '自动生成原始预览') { rendering = true; }
    else if (round) {
      const base = 1 + (Number(round[1])-1)*5;
      const group = ['基准', '曝光', '高光', '白色色阶'].findIndex(label => phase.endsWith('自动显影 · '+label));
      if (group >= 0) { slot = base + group; rendering = true; }
      else if (phase.endsWith('自动复核显影')) { slot = base + 4; rendering = true; }
      else if (phase.endsWith('准备校准')) slot = base;
      else if (phase.endsWith('计算修正')) slot = base + 4;
      else if (phase.endsWith('检查效果')) slot = base + 5;
      else known = false;
    } else if (phase === '导出全尺寸 JPG') { slot = 11; rendering = true; }
    else if (phase === '整理结果' || phase === '完成') slot = 12;
    else if (!['连接 Photoshop','准备'].includes(phase)) known = false;
    units = slot + (rendering ? fraction : 0);
    const percent = task.state === 'done' ? 100 : known ? Math.min(99.9, units / 12 * 100) : null;
    const stageText = rendering ? `${phase} · ${Math.min(total, Math.max(0, done))} / ${total} 帧` : phase;
    const activeSlot = phase.endsWith('检查效果') ? Math.max(0,slot-1) : Math.min(slot,11);
    return {percent, phase, stageText, note, slot, rendering,
      stages: stages.map((title, index) => ({title,
        status: task.state === 'done' ? (index>=6 && index<=10 && task.result?.rounds===1 ? '已跳过' : '已完成')
          : index < activeSlot ? (index>=6 && index<=10 && slot>=11 ? '已结束 / 按需跳过' : '已完成')
          : index === activeSlot ? '当前阶段' : index>=6 && index<=10 ? '按需执行' : '待处理'}))};
  }
  return {describe, note};
})();
if (typeof module !== 'undefined') module.exports = TimelapseProgress;
