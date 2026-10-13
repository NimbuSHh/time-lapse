"""Direct RAW preparation -> measured trajectory -> explicit preview approval.

New preparations have their own index. Legacy automatic deliveries, manual
projects and all input files remain untouched. No call here runs the old solver.
"""
import json
from pathlib import Path
import uuid

from . import automatic, joint_feedback as joint, joint_model, trajectory, trajectory_regions
from .photoshop import Cancelled, PhotoshopRenderer
from .project import digest, safe_read
from .workspace import output_path

INDEX = 'preparation-latest.json'


def load_preparation(output):
    output=output_path(output)
    index=output/INDEX
    if not index.exists(): return None
    path=Path(json.loads(safe_read(index,1024*1024))['path']).resolve()
    if path.parent!=output or not path.name.startswith('素材准备-'):
        raise ValueError('素材准备记录路径无效')
    state=json.loads(safe_read(path/'run.json',32*1024*1024))
    if state.get('path')!=str(path) or state.get('workflow')!=trajectory.PRODUCT_VERSION:
        raise ValueError('素材准备记录版本或路径不符')
    return state


def options(regions):
    if regions is None: return None
    if not isinstance(regions,dict) or set(regions)!= {'primary','secondary','check_a','check_b'}:
        raise ValueError('手动测光需要亮部、参考区域及两个检查区域')
    checked={}
    for key,roi in regions.items():
        if not isinstance(roi,list) or len(roi)!=4 or any(type(v) not in (int,float) for v in roi):
            raise ValueError('区域坐标须为四个 0–1 数字')
        joint_model.roi_box(roi,640,640)
        checked[key]=list(roi)
    for i,key in enumerate(checked):
        a=checked[key]
        for other in list(checked)[i+1:]:
            b=checked[other]
            if min(a[2],b[2])>max(a[0],b[0]) and min(a[3],b[3])>max(a[1],b[1]):
                raise ValueError('四个测光区域不能重叠，检查区域应独立于求解区域')
    return checked


class Preparation(automatic.Run):
    def __init__(self,raw_dir,output,regions=None,progress=None,cancelled=None,renderer_factory=PhotoshopRenderer):
        self.source,self.snapshot=automatic.inputs(raw_dir)
        self.output=output_path(output);self.output.mkdir(parents=True,exist_ok=True)
        self.regions=options(regions)
        self.progress=progress;self.cancelled=cancelled or (lambda:False)
        signature=digest(json.dumps([str(self.source),self.snapshot,self.regions,trajectory.PRODUCT_VERSION],sort_keys=True).encode())
        old=load_preparation(self.output)
        self.state=old if old and old.get('signature')==signature else None
        if self.state:
            self.folder=Path(self.state['path'])
            self.renderer=renderer_factory(self.folder,self.cancelled)
            if self.state.get('renderer')!=self.renderer.identity:self.state=None
        if self.state is None:
            self.folder=self.output/('素材准备-'+uuid.uuid4().hex[:12]);self.folder.mkdir()
            self.renderer=renderer_factory(self.folder,self.cancelled)
            self.state={'schema':1,'workflow':trajectory.PRODUCT_VERSION,'id':self.folder.name,
                        'path':str(self.folder),'raw_dir':str(self.source),'snapshot':self.snapshot,
                        'signature':signature,'count':len(self.snapshot),'regions':self.regions,
                        'renderer':self.renderer.identity,'state':'ready','phase':'准备素材','phases':{}}
        self.save();automatic.atomic_json(self.output/INDEX,{'path':str(self.folder)})

    def execute(self):
        if self.state['state']=='prepared':
            self.unchanged()
            self.phase('第 1 轮准备校准',lambda:None)
            return self.state
        try:
            self.report('检查预览编码工具')
            from .preview_video import _tools
            _tools()
            self.report('连接 Photoshop · 准备原始预览');self.renderer.preflight()
            previews=self.render_group('initial',label='生成原始参考预览')
            # Inspect the complete sequence; no scene-specific coordinate defaults.
            stats=[]
            for i,source in enumerate(self.snapshot):
                self.report('选择全序列固定测光区域',i)
                data=safe_read(previews/(Path(source['raw']['name']).stem+'.jpg'),25*1024*1024)
                linear,_=joint_model.pixels(data)
                stats.append(trajectory_regions.statistics(linear))
            contract=trajectory_regions.choose(stats,manual_rois=self.regions)
            def prepare():
                kwargs={'region_mode':'auto','fixed_selection':{
                    'method':'sequence-fixed-grid-v1','rows':contract['rows'],'cols':contract['cols'],
                    'mid_tiles':contract['tiles']['secondary'],'high_tiles':contract['tiles']['primary'],
                    'analysed_frames':len(self.snapshot),
                    'mid_usable_fraction':contract['quality']['secondary']['usable_fraction'],
                    'high_usable_fraction':contract['quality']['primary']['usable_fraction']}} if self.regions is None else {
                    'region_mode':'manual','mid_roi':self.regions['secondary'],'high_roi':self.regions['primary']}
                return joint.prepare(previews,self.folder/'packages',
                    progress=lambda d,n:self.report('冻结参考测量与原配方',d,n),**kwargs)
            self.phase('第 1 轮准备校准',prepare)
            self.unchanged()
            self.state.update(state='prepared',phase='参考准备完成',region_contract=contract)
            self.save();return self.state
        except Exception as exc:
            self.state.update(state='stopped' if isinstance(exc,Cancelled) else 'error',error=str(exc))
            self.save();raise


def previous_preview(output, prepared, current):
    """Keep a completed same-source preview accessible after preparation stops."""
    if (not prepared or prepared.get('state') not in ('stopped', 'error')
            or not current or current.get('version') != trajectory.VERSION):
        return None
    from .legacy_preview import load
    old = load(output)
    if (not old or not old.get('result', {}).get('video')
            or not old.get('raw_dir')
            or Path(old['raw_dir']).resolve() != Path(prepared['raw_dir']).resolve()):
        return None
    return old


def load_latest(output):
    prepared=load_preparation(output)
    current=trajectory.load_latest(output)
    if prepared:
        if current and current.get('source_run')==prepared['id']:
            return {**current,'regions':prepared.get('regions'),'region_contract':prepared.get('region_contract'),'raw_dir':prepared['raw_dir']}
        old = previous_preview(output, prepared, current)
        if old:
            return {**old, 'phase':'新的素材准备已停止；下方保留上次完成的完整预览',
                    'state':old['state'] if old['result'].get('state')=='done' else prepared['state'],
                    'pending_preparation':prepared['id']}
        return prepared
    if current and current.get('version')==trajectory.VERSION:
        from .legacy_preview import load
        return load(output)
    return current


def run(raw_dir,output,regions=None,progress=None,cancelled=None,renderer_factory=PhotoshopRenderer):
    output=output_path(output);output.mkdir(parents=True,exist_ok=True)
    with automatic.render_lock(output):
        prepared=Preparation(raw_dir,output,regions,progress,cancelled,renderer_factory).execute()
        return trajectory.Trajectory(output,progress,cancelled,renderer_factory,original=prepared).execute()


def resume(output,progress=None,cancelled=None,renderer_factory=PhotoshopRenderer):
    prepared=load_preparation(output)
    if not prepared:
        return trajectory.run(output,progress,cancelled,renderer_factory)
    return run(prepared['raw_dir'],output,prepared.get('regions'),progress,cancelled,renderer_factory)


def export(output,confirmation,progress=None,cancelled=None,renderer_factory=PhotoshopRenderer):
    output=output_path(output)
    with automatic.render_lock(output):
        prepared=load_preparation(output)
        current=trajectory.load_latest(output)
        old=previous_preview(output,prepared,current)
        if not prepared or (old and confirmation.get('run_id')==old['result'].get('run_id')):
            from .legacy_preview import LegacyRunner
            return LegacyRunner(output,progress,cancelled,renderer_factory).export(confirmation)
        if prepared['state']!='prepared':
            raise ValueError('新版预览尚未准备完成；原有预览与结果保留')
        if not current or current.get('source_run')!=prepared['id'] or not current.get('result',{}).get('video'):
            raise ValueError('完整预览尚未生成，不能导出全尺寸')
        runner=trajectory.Trajectory(output,progress,cancelled,renderer_factory,original=prepared)
        return runner.export(confirmation)
