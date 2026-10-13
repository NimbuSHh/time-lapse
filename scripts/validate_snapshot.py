"""Read-only replay of an exported source snapshot against exact JPEG copies.

No RAW placeholders, input edits or XMP application. Writes diagnostic artifacts
only. The alternative endpoint settings are proposals, not applied edits.
Matplotlib is an optional report-generation dependency, not an app dependency.
"""
import argparse
import csv
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from timelapse import analysis
from timelapse.project import safe_read, digest
from timelapse.xmp import parse


def validate(package, previews, output):
    package, previews, output = map(lambda p: Path(p).resolve(), (package, previews, output))
    if output.exists():
        raise ValueError('验证输出目录必须是新目录，避免覆盖已有报告')
    for source in (package, previews):
        if source == output or source in output.parents:
            raise ValueError('验证输出不能位于输入目录内')
    manifest_data=safe_read(package/'manifest.json',64*1024*1024)
    manifest=json.loads(manifest_data)
    for key in ('raw_dir', 'preview_dir'):
        original=Path(manifest[key]).expanduser().resolve()
        if output==original or original in output.parents:
            raise ValueError('验证输出不能位于原始素材目录内')
    frames=manifest['frames']
    measures=[]; cameras=[]; times=[]; exposures=[]; receipts=[]
    for i,f in enumerate(frames):
        # Manifest filenames are untrusted; only a single filename is allowed.
        if any(Path(f[k]).name!=f[k] or f[k] in ('.','..') for k in ('xmp','preview')):
            raise ValueError('清单包含不安全的文件名')
        jpg=safe_read(previews/f['preview'],25*1024*1024)
        xmp=safe_read(package/'originals'/f['xmp'],4*1024*1024)
        if digest(jpg)!=f['preview_sha256'] or digest(xmp)!=f['source_xmp_sha256']:
            raise ValueError(f'输入与来源快照不匹配: {f["name"]}')
        source=parse(xmp); measured=analysis.measure(jpg,manifest['roi'])
        meta={**measured['metadata'],**source.metadata}
        measures.append(measured);exposures.append(source.exposure)
        cameras.append(analysis.camera_ev(meta));times.append(analysis.capture_time(meta))
        receipts.append({'frame':i+1,'name':f['name'],'jpeg':f['preview'],'xmp':f['xmp'],
                         'jpeg_sha256':digest(jpg),'xmp_sha256':digest(xmp)})
        if (i+1)%100==0: print(f'Measured and hash-checked {i+1}/{len(frames)}',flush=True)
    report=analysis.infer(measures,exposures,cameras,times,manifest['mode'])
    replay_error=float(np.max(np.abs(np.array(report['measured'])-manifest['report']['measured'])))
    if replay_error>1e-9: raise ValueError('重测曲线与原分析快照不一致')
    selected=[e['index'] for e in report['events'] if e['selectable']]
    versions={}
    for name,start,end in [('original_endpoints',0,0),('endpoint_proposal',.5,-.8)]:
        result=analysis.correct(report,exposures,selected,strength=.6,window=15,start=start,end=end)
        residuals=[analysis.event_residual(result['target'],e,report['boundaries']) for e in report['events'] if e['index'] in selected]
        versions[name]={'result':result,'residuals':residuals,'delta_min':min(result['delta']),
                        'delta_max':max(result['delta']),'blocked_count':len(result['blocked'])}
    # Recheck every copied JPEG and original-XMP backup after analysis.
    for f in receipts:
        if digest(safe_read(previews/f['jpeg'],25*1024*1024))!=f['jpeg_sha256'] or digest(safe_read(package/'originals'/f['xmp'],4*1024*1024))!=f['xmp_sha256']:
            raise ValueError('验证期间输入发生变化')
    output.mkdir(parents=True)
    value={'kind':'real-input-predicted-curve-validation','source_package':str(package),
           'source_manifest_sha256':digest(manifest_data),'raw_read_or_written':False,
           'frame_count':len(frames),'roi':manifest['roi'],'same_as_loaded_snapshot_max_error_ev':replay_error,
           'input_hashes_match_before_and_after':True,'receipts':receipts,'report':report,'versions':versions,
           'notice':f'真实{len(frames)}帧JPEG/XMP只读测量与预测曲线验证。未调用ACR重显影，不证明整图高光或最终曝光效果。备选首尾目标未应用到素材。'}
    (output/'validation.json').write_text(json.dumps(value,ensure_ascii=False,indent=2))
    with (output/'curves.csv').open('w',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['frame','name','measured_roi_ev','target_preserve_endpoints_ev','delta_preserve_endpoints_ev','blocked_preserve_endpoints','target_proposal_ev','delta_proposal_ev','blocked_proposal'])
        a=versions['original_endpoints']['result'];b=versions['endpoint_proposal']['result']
        for i,f in enumerate(frames):writer.writerow([i+1,f['name'],report['measured'][i],a['target'][i],a['delta'][i],i in a['blocked'],b['target'][i],b['delta'][i],i in b['blocked']])
    plot(value,output)
    (output/'说明.txt').write_text(value['notice']+f'\n\n原片首尾目标：{len(a["blocked"])}帧超出导出限值。\n备选首尾目标：开头+0.5 EV，结尾-0.8 EV，{len(b["blocked"])}帧超出导出限值。\n两者均未生成或应用XMP修正包。\n完整逐帧数据见curves.csv；来源哈希、分组证据与每次剩余跳变见validation.json。\n')
    print(json.dumps({k:{j:v[j] for j in ('delta_min','delta_max','blocked_count')} for k,v in versions.items()}))
    return value


def plot(value,output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    x=np.arange(1,value['frame_count']+1);report=value['report'];y=np.array(report['measured'])
    a=value['versions']['original_endpoints']['result'];b=value['versions']['endpoint_proposal']['result']
    plt.rcParams.update({'font.family':'sans-serif','font.size':10,'axes.spines.top':False,'axes.spines.right':False})
    fig,axes=plt.subplots(3,1,figsize=(13,11),constrained_layout=True)
    axes[0].plot(x,y,color='#839286',label='Original ROI measurement',lw=1.3)
    axes[0].plot(x,a['target'],color='#187757',label='Corrected target: original endpoints',lw=1.7)
    axes[0].plot(x,b['target'],color='#bd7624',label='Alternative target: start +0.5 / end -0.8',lw=1.3)
    axes[0].set(ylabel='ROI brightness (EV)',title=f'All {len(x)} real input frames | predicted curves, not ACR re-rendered images')
    axes[0].legend(loc='lower right',fontsize=9);axes[0].axvspan(338,341,color='#bd7624',alpha=.12)
    for gap in report['gaps']:axes[0].axvline(gap+1,color='#999',ls=':',lw=1)
    axes[1].plot(x,a['delta'],color='#187757',label=f'Original endpoints: {len(a["blocked"])} frames beyond limit')
    axes[1].plot(x,b['delta'],color='#bd7624',label=f'Alternative endpoints: {len(b["blocked"])} frames beyond limit')
    for level in (-2,2):axes[1].axhline(level,color='#a34040',ls='--',lw=1)
    axes[1].set(ylabel='Exposure increment (EV)',title='Export limit remains +/-2 EV; no silent clipping or source edits')
    axes[1].legend(loc='lower right',fontsize=9)
    # Show the new group versus the same anchored algorithm with this group off.
    group=next(e for e in report['events'] if e.get('method')=='dense-cluster' and e['selectable'])
    old=analysis.correct(report,[0]*len(x),[i for i in a['settings']['selected'] if i!=group['index']])
    lo,hi=group['index']-8,group['end_index']+9
    axes[2].plot(x[lo:hi],y[lo:hi],'.-',color='#839286',label='Original measurement')
    axes[2].plot(x[lo:hi],np.array(old['target'])[lo:hi],'.--',color='#a34040',label='Dense group uncorrected')
    axes[2].plot(x[lo:hi],np.array(a['target'])[lo:hi],'.-',color='#187757',label='Dense group corrected')
    axes[2].axvspan(group['index']+1,group['end_index']+1,color='#bd7624',alpha=.12)
    residual=analysis.event_residual(a['target'],group,report['boundaries'])['max_component_jump']
    axes[2].set(xlabel='Frame number',ylabel='ROI brightness (EV)',title=f'Frames {group["index"]+1}-{group["end_index"]+1} | every short plateau corrected; maximum residual step {residual:.4f} EV')
    axes[2].legend(fontsize=9)
    for ax in axes:ax.grid(alpha=.18)
    fig.savefig(output/'complete-curves.png',dpi=160);fig.savefig(output/'complete-curves.svg');plt.close(fig)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package',required=True);parser.add_argument('--previews',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();validate(args.package,args.previews,args.output)
