"""Export the exact reviewed recipe on private RAW copies, with resumable receipts.

Numeric preview checks and explicit video approval authorize a render. They do
not claim that a person has visually checked the full-resolution deliverables.
"""
from io import BytesIO
import json
from pathlib import Path
import shutil
import uuid

from PIL import ImageCms, JpegImagePlugin

from . import automatic, joint_feedback as joint, refinement, trajectory_model
from .photoshop import Cancelled
from .project import digest, safe_read

VERSION = 'approved-native-export-v1'
CONFIRM_FIELDS = ('run_id', 'video_sha256', 'selection_sha256', 'target_sha256', 'effective_policy_sha256')


def _hash(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode())


def _read(path, sha, limit=128*1024*1024):
    data = safe_read(Path(path), limit)
    if not isinstance(sha, str) or digest(data) != sha:
        raise ValueError('文件校验失败，不能沿用已确认版本：'+Path(path).name)
    return data


def _inside(root, path):
    root, path = Path(root).resolve(), Path(path)
    if path.is_symlink() or root not in path.resolve().parents:
        raise ValueError('导出文件路径越界或含符号链接')
    return path


def _jpeg(data):
    # Native RAW dimensions may exceed Pillow's default photographic limit.
    # Use a bounded JPEG-only decoder without globally changing Pillow policy.
    with JpegImagePlugin.JpegImageFile(BytesIO(data)) as image:
        if (image.width*image.height > 200_000_000 or min(image.size) < 2
                or max(image.size) > 40000 or image.mode != 'RGB'):
            raise ValueError('全尺寸 JPEG 的像素尺寸或色彩模式无效')
        if image.getexif().get(274, 1) != 1:
            raise ValueError('全尺寸 JPEG 仍含旋转标记')
        profile = image.info.get('icc_profile')
        if profile:
            name = ImageCms.getProfileName(ImageCms.ImageCmsProfile(BytesIO(profile)))
            if 'srgb' not in name.lower():
                raise ValueError('全尺寸 JPEG 必须标记为 sRGB')
        elif image.getexif().get_ifd(34665).get(40961) != 1:
            raise ValueError('全尺寸 JPEG 缺少 sRGB 标记')
        image.load()  # Metadata alone cannot prove a complete, decodable image.
        return list(image.size)


def validate_preview(runner):
    """Revalidate frozen inputs and selected actual renders, never selection claims."""
    result = runner.state.get('result', {})
    if not isinstance(result, dict) or result.get('numeric_conditions_met') is not True:
        raise ValueError('预览数值检查未通过，不能确认全尺寸导出')
    if result.get('run_id') != runner.state.get('id'):
        raise ValueError('预览任务身份不符')
    runner.check_source()
    if (runner.renderer.identity != runner.original['renderer']
            and not (getattr(runner,'legacy_compatible',False) and runner.original['renderer'] in getattr(runner.renderer,'compatible_identities',[]))):
        raise ValueError('显影器已改变，不能沿用已确认预览')
    if automatic.file_hash(result['video']) != result['sha256']:
        raise ValueError('待确认视频已改变')
    plan = json.loads(_read(runner.folder/'trajectory-target.json', runner.state['target_file_sha256']))
    if (plan != runner.plan or result['target_sha256'] != plan.get('plan_sha256')
            or runner.state['target_sha256'] != result['target_sha256']):
        raise ValueError('冻结目标已改变')
    selection = json.loads(_read(runner.folder/'selection.json', result['selection_sha256']))
    policy = json.loads(_read(runner.folder/'effective-tone-policy.json', result['effective_policy_sha256']))
    if result['effective_policy_sha256'] != runner.state['effective_policy_sha256']:
        raise ValueError('有效显影目标已改变')
    manifest = json.loads(_read(result['manifest'], result['manifest_sha256']))
    if (set(selection) != {str(i) for i in range(runner.n)}
            or manifest.get('frame_count') != runner.n or len(manifest.get('frames', [])) != runner.n
            or len(runner.original['snapshot']) != runner.n):
        raise ValueError('预览帧清单不完整或顺序无效')
    runner.histories.clear()
    runner.frames.clear()
    entries = []
    for i, frame in enumerate(manifest['frames']):
        if runner.cancelled():
            raise Cancelled('已停止全尺寸导出前的预览复核')
        chosen = selection[str(i)]
        matches = [entry for entry in runner.history(i)
                   if entry['sha256'] == chosen.get('sha256')
                   and entry['recipe_sha256'] == chosen.get('recipe_sha256')
                   and Path(entry['jpg']).resolve() == Path(chosen['jpg']).resolve()
                   and Path(entry['xmp']).resolve() == Path(chosen['xmp']).resolve()]
        if len(matches) != 1:
            raise ValueError(f'第{i+1}帧缺少唯一可核验的已选显影')
        entry = matches[0]
        expected = {'index': i+1, 'jpg': str(Path(entry['jpg']).resolve()),
                    'sha256': entry['sha256'], 'recipe_sha256': entry['recipe_sha256']}
        if frame != expected:
            raise ValueError(f'第{i+1}帧与已确认视频顺序或配方不符')
        xmp = _read(entry['xmp'], entry['recipe_sha256'], 4*1024*1024)
        data = _read(entry['jpg'], entry['sha256'], 25*1024*1024)
        context = runner.context(i)
        if digest(refinement.recipe(context['base'], entry['controls'])) != digest(xmp):
            raise ValueError('选择配方不是原配方的已验证修正')
        joint.match(xmp, data, context['source']['raw']['name'])
        measured = {**entry, **runner.measure(i, data)}
        if not runner.protected(i, measured):
            raise ValueError(f'第{i+1}帧未通过高光保护检查')
        if list(context['size']) != manifest.get('source_dimensions'):
            raise ValueError('预览尺寸与首次实测尺寸不符')
        entries.append(measured)
    review = runner.review_entries(entries)
    if review.get('numeric_conditions_met') is not True:
        raise ValueError('复核实际预览后数值检查未通过')
    if policy != result.get('effective_tone_policy'):
        raise ValueError('有效显影策略与预览记录不符')
    runner.check_source()
    binding = {'run_id': result['run_id'], 'video_sha256': result['sha256'],
               **{field: result[field] for field in CONFIRM_FIELDS[2:]},
               'manifest_sha256': result['manifest_sha256'],
               'target_file_sha256': runner.state['target_file_sha256'],
               'source_snapshot_sha256': _hash(runner.original['snapshot']),
               'renderer': runner.renderer.identity, 'version': VERSION}
    return result, entries, binding


def _verify_full(runner, i, entry, receipt, approval_sha, root):
    context = runner.context(i)
    if (receipt.get('approval_sha256') != approval_sha
            or receipt.get('source') != context['source']
            or receipt.get('key') != runner.cache_key(i, _read(entry['xmp'], entry['recipe_sha256']), edge=0)
            or receipt.get('recipe_sha256') != entry['recipe_sha256']
            or receipt.get('renderer') != runner.renderer.identity):
        raise ValueError('全尺寸回执来源、批准版本或配方不符')
    jpg, xmp = _inside(root, receipt['jpg']), _inside(root, receipt['xmp'])
    data = _read(jpg, receipt['sha256'])
    recipe = _read(xmp, entry['recipe_sha256'], 4*1024*1024)
    joint.match(recipe, data, context['source']['raw']['name'], full_size=True)
    actual = _jpeg(data)
    evidence = receipt['render_evidence']
    if (evidence.get('edge') != 0 or evidence.get('native_size') != actual
            or evidence.get('output_size') != actual
            or str(_inside(root, evidence['raw']).resolve()) != evidence['raw']):
        raise ValueError('全尺寸显影缺少匹配的原生尺寸证据')
    preview = list(context['size'])
    scale = max(preview)/max(actual)
    if scale > 1 or any(abs(a*scale-p) > 1.1 for a, p in zip(actual, preview)):
        raise ValueError('全尺寸结果与已确认预览的比例不符')
    return receipt


def export(runner, confirmation):
    """Explicit version-bound confirmation is mandatory, including on resume."""
    from .trajectory import PRODUCT_VERSION as TRAJECTORY_VERSION
    native_version = runner.state.get('version') == TRAJECTORY_VERSION and runner.plan.get('version') == trajectory_model.VERSION
    legacy_version = (getattr(runner,'legacy_compatible',False)
                      and runner.state.get('version') == 'whole-sequence-preview-v1'
                      and runner.plan.get('version') == trajectory_model.LEGACY_VERSION)
    if not (native_version or legacy_version):
        raise ValueError('旧版任务只能保留预览；请使用新版通用流程建立任务')
    result = runner.state.get('result', {})
    expected = {'run_id': result.get('run_id'), 'video_sha256': result.get('sha256'),
                **{field: result.get(field) for field in CONFIRM_FIELDS[2:]}}
    if (not isinstance(confirmation, dict) or confirmation.get('confirmed') is not True
            or any(not expected[k] or confirmation.get(k) != expected[k] for k in CONFIRM_FIELDS)):
        raise ValueError('必须由用户确认当前具体视频及其配方版本后才能导出')
    result, entries, binding = validate_preview(runner)
    approval = {**binding, 'confirmed': True}
    approval_sha = _hash(approval)
    approval_path = _inside(runner.folder, runner.folder/'export-approval.json')
    if approval_path.exists():
        if json.loads(safe_read(approval_path, 1024*1024)) != approval:
            raise ValueError('导出确认版本已改变，请保留旧结果并重新建立任务')
    else:
        with approval_path.open('xb') as stream:
            stream.write(json.dumps(approval, ensure_ascii=False, sort_keys=True).encode())
    record = runner.state.get('export')
    if record:
        if record.get('approval_sha256') != approval_sha:
            raise ValueError('不能混用不同批准版本的导出')
        root = _inside(runner.folder, record['path'])
        if not root.is_dir():
            raise ValueError('导出工作目录已丢失')
    else:
        root = runner.folder/('全尺寸导出-'+uuid.uuid4().hex[:12])
        root.mkdir()
        record = {'path': str(root), 'approval_sha256': approval_sha}
        runner.state['export'] = record
    runner.state['confirmation'] = {k: confirmation[k] for k in ('confirmed', *CONFIRM_FIELDS)}
    runner.state['full_export_authorized'] = True
    runner.save()
    for name in ('JPG', 'XMP', 'receipts', 'attempts'):
        _inside(root, root/name).mkdir(exist_ok=True)
    completed = []
    try:
        runner.report('连接 Photoshop · 导出已确认配方的全尺寸图像', 0, runner.n)
        runner.renderer.preflight()
        for i, entry in enumerate(entries):
            runner.report(f'全尺寸导出 · 第{i+1}帧', i, runner.n)
            runner.check_source()
            context = runner.context(i)
            source = context['source']
            name = source['raw']['name']
            stem = Path(name).stem
            rec_path = _inside(root, root/'receipts'/(stem+'.json'))
            jpg = _inside(root, root/'JPG'/(stem+'.jpg'))
            xmp_path = _inside(root, root/'XMP'/(stem+'.xmp'))
            if rec_path.exists():
                rec = json.loads(safe_read(rec_path, 1024*1024))
                if rec.get('jpg') != str(jpg) or rec.get('xmp') != str(xmp_path):
                    raise ValueError('全尺寸回执文件名或顺序不符')
                completed.append(_verify_full(runner, i, entry, rec, approval_sha, root))
                continue
            # Unreceipted published files are never overwritten or adopted.
            # Preserve them with the incomplete attempt before restarting.
            attempt = root/'attempts'/(stem+'-'+uuid.uuid4().hex)
            attempt.mkdir()
            for prior in (jpg, xmp_path):
                if prior.exists():
                    prior.rename(attempt/('incomplete-'+prior.name))
            raw = attempt/name
            automatic.copy_raw(runner.source/name, raw)
            recipe = _read(entry['xmp'], entry['recipe_sha256'], 4*1024*1024)
            (attempt/(stem+'.xmp')).write_bytes(recipe)
            if 'acr' in source:
                aux = source['acr']['name']
                shutil.copyfile(runner.source/aux, attempt/aux)
                _read(attempt/aux, source['acr']['sha256'])
            runner.check_source()
            pending = attempt/'render.jpg'
            runner.renderer.last_render = None
            runner.renderer.render(raw, pending, edge=0)
            evidence = runner.renderer.last_render
            if not isinstance(evidence, dict) or evidence.get('raw') != str(raw.resolve()):
                raise ValueError('显影器未返回本帧的原生尺寸证据')
            data = safe_read(pending, 128*1024*1024)
            rec = {'index': i+1, 'key': runner.cache_key(i, recipe, edge=0), 'source': source,
                   'approval_sha256': approval_sha, 'sha256': digest(data),
                   'recipe_sha256': digest(recipe), 'renderer': runner.renderer.identity,
                   'jpg': str(pending), 'xmp': str(attempt/(stem+'.xmp')),
                   'render_evidence': evidence}
            # Adobe may materialize defaults in its working XMP. Deliver the
            # exact approved recipe; embedded JPEG metadata must match it.
            recipe_file = attempt/'approved.xmp'
            recipe_file.write_bytes(recipe)
            rec['xmp'] = str(recipe_file)
            _verify_full(runner, i, entry, rec, approval_sha, root)
            runner.check_source()
            # Exclusive creates make even late unexpected files non-overwritable.
            with jpg.open('xb') as destination:
                destination.write(data)
            with xmp_path.open('xb') as destination:
                destination.write(recipe)
            rec.update(jpg=str(jpg), xmp=str(xmp_path))
            automatic.atomic_json(rec_path, rec)
            completed.append(rec)
        runner.check_source()
        delivery = {'path': str(root), 'jpgs': str(root/'JPG'), 'xmps': str(root/'XMP'),
                    'count': runner.n, 'approval_sha256': approval_sha, 'frames': completed,
                    'verified': True, 'verification_scope': 'source_recipe_native_dimensions_complete_decode_srgb',
                    'visual_playback_verified': False}
        manifest_path = _inside(root, root/'manifest.json')
        if manifest_path.exists():
            if json.loads(safe_read(manifest_path, 64*1024*1024)) != delivery:
                raise ValueError('已有全尺寸交付清单已改变，不能覆盖')
        else:
            with manifest_path.open('xb') as stream:
                stream.write(json.dumps(delivery, ensure_ascii=False, sort_keys=True).encode())
        final = {**result, 'state': 'done', 'full_export_authorized': True,
                 'preview_user_confirmed': True, 'delivery': delivery,
                 'message': '已按确认预览的同一配方导出全尺寸 JPG 与 XMP；全尺寸视觉效果待查看'}
        runner.state.update(state='done', result=final, phase='全尺寸导出完成', done=runner.n, total=runner.n, error=None)
        runner.save()
        return final
    except Exception as exc:
        runner.state.update(state='stopped' if isinstance(exc, Cancelled) else 'error', error=str(exc))
        runner.save()
        raise
