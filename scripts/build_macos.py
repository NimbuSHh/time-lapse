"""Build a self-contained Apple Silicon app; no system-wide installs or signing account."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tomllib
import urllib.request

from build_video_tools import build as build_video, digest

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT/'build/macos'
RUNTIME_URL = ('https://github.com/astral-sh/python-build-standalone/releases/download/20261009/'
               'cpython-3.12.15%2B20261009-aarch64-apple-darwin-install_only_stripped.tar.gz')
RUNTIME_SHA = '233f8e15255b3e1d1fd47ea5dd5230dfed803f371138e896e54d98c60ae19f6f'
BUNDLE_ID = 'io.github.NimbuSHh.time-lapse'


def run(command, **kwargs):
    print('+', ' '.join(map(str, command)), flush=True)
    return subprocess.run(command, check=True, **kwargs)


def python_environment(offline=False):
    downloads = WORK/'downloads'
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads/'python-3.12.15-arm64.tar.gz'
    if not archive.is_file():
        if offline:
            raise ValueError('Missing cached Python runtime')
        temporary = archive.with_suffix('.download')
        try:
            with urllib.request.urlopen(RUNTIME_URL, timeout=120) as source, temporary.open('wb') as target:
                shutil.copyfileobj(source, target)
            if digest(temporary) != RUNTIME_SHA:
                raise ValueError('Python runtime checksum mismatch')
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    if digest(archive) != RUNTIME_SHA:
        raise ValueError('Python runtime checksum mismatch')
    runtime = WORK/'runtime/python'
    if not (runtime/'bin/python3').is_file():
        with tarfile.open(archive) as tar:
            tar.extractall(WORK/'runtime', filter='data')
    python = WORK/'app-env/bin/python'
    if not python.exists():
        run([runtime/'bin/python3', '-m', 'venv', python.parent.parent])
    wheels = downloads/'wheels'
    requirements = ['-r', ROOT/'requirements.txt', '-r', ROOT/'packaging/macos/requirements-build.txt']
    if not offline:
        run([sys.executable, '-m', 'pip', 'download', '--disable-pip-version-check',
             '--only-binary=:all:', '--platform', 'macosx_14_0_arm64', '--python-version', '3.12',
             '--dest', wheels, *requirements])
    run([python, '-m', 'pip', 'install', '--no-index', '--find-links', wheels, *requirements])
    return runtime, python


def audit_binaries(app):
    """Reject hidden Homebrew/other-app dependencies and an understated OS minimum."""
    checked = []
    for path in app.rglob('*'):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as stream:
            magic = stream.read(4)
        if magic not in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xca\xfe\xba\xbe'):
            continue
        links = subprocess.check_output(['/usr/bin/otool', '-L', path], text=True)
        for line in links.splitlines()[1:]:
            dependency = line.strip().split(' (', 1)[0]
            if not dependency.startswith(('/System/Library/', '/usr/lib/', '@rpath/', '@loader_path/', '@executable_path/')):
                raise ValueError(f'External binary dependency in {path.name}: {dependency}')
        build = subprocess.check_output(['/usr/bin/xcrun', 'vtool', '-show-build', path], text=True)
        for line in build.splitlines():
            words = line.split()
            if words and words[0] == 'minos' and tuple(map(int, words[1].split('.')[:2])) > (14, 0):
                raise ValueError(f'{path.name} requires macOS {words[1]}, above declared 14.0')
        checked.append(str(path.relative_to(app)))
    return checked


def build(output, offline=False, replace=False):
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise ValueError('This build recipe currently targets Apple Silicon Macs only')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    app = output/'Time-lapse.app'
    version = tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['version']
    zip_path = output/f'Time-lapse-{version}-macOS-arm64.zip'
    if app.exists() or zip_path.exists():
        marker = app/'Contents/Resources/build-info.json'
        if not replace or not marker.is_file() or json.loads(marker.read_text()).get('builder') != 'timelapse-macos-v1':
            raise ValueError('Output exists. Choose another --output directory; --replace only replaces our generated app.')
    runtime, python = python_environment(offline)
    video = build_video(WORK/'video', WORK/'downloads')
    env = dict(os.environ, MACOSX_DEPLOYMENT_TARGET='14.0',
               PYINSTALLER_CONFIG_DIR=str(WORK/'pyinstaller-cache'))
    run([python, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--console',
         '--name', 'TimeLapseServer', '--target-arch', 'arm64',
         '--distpath', WORK/'frozen', '--workpath', WORK/'pyinstaller', '--specpath', WORK,
         '--paths', ROOT, '--collect-submodules', 'timelapse',
         '--add-data', str(ROOT/'timelapse/static')+':timelapse/static',
         '--add-data', str(ROOT/'timelapse/photoshop-render.jsx')+':timelapse',
         '--add-binary', str(video/'bin/ffmpeg')+':timelapse/bin',
         '--add-binary', str(video/'bin/ffprobe')+':timelapse/bin',
         '--exclude-module', 'tkinter', ROOT/'packaging/macos/entry.py'], env=env, cwd=ROOT)
    stage = WORK/'staging/Time-lapse.app'
    if stage.exists():
        shutil.rmtree(stage)
    contents = stage/'Contents'
    resources = contents/'Resources'
    (contents/'MacOS').mkdir(parents=True)
    resources.mkdir()
    shutil.copytree(WORK/'frozen/TimeLapseServer', resources/'backend', symlinks=True)
    run(['/usr/bin/xcrun', 'swiftc', '-O', '-swift-version', '5', '-target', 'arm64-apple-macos14.0',
         '-module-cache-path', WORK/'swift-cache', ROOT/'packaging/macos/Launcher.swift',
         '-o', contents/'MacOS/Time-lapse'], env=env)
    run(['/usr/bin/xcrun', 'swiftc', '-O', '-module-cache-path', WORK/'swift-cache',
         ROOT/'packaging/macos/Icon.swift', '-o', WORK/'make-icon'], env=env)
    run([WORK/'make-icon', WORK/'Time-lapse.iconset'])
    run(['/usr/bin/iconutil', '-c', 'icns', WORK/'Time-lapse.iconset', '-o', resources/'AppIcon.icns'])
    plist = {'CFBundleName': 'Time-lapse', 'CFBundleDisplayName': 'Time-lapse',
             'CFBundleIdentifier': BUNDLE_ID, 'CFBundleExecutable': 'Time-lapse',
             'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': version,
             'CFBundleVersion': version, 'CFBundleIconFile': 'AppIcon',
             'LSMinimumSystemVersion': '14.0', 'NSHighResolutionCapable': True,
             'NSAppleEventsUsageDescription': '用于调用 Photoshop / Camera Raw 显影你选择的 RAW 工作副本。',
             'NSHumanReadableCopyright': 'Copyright © 2026 NimbuSHh. MIT License.'}
    (contents/'Info.plist').write_bytes(plistlib.dumps(plist))
    notices = resources/'ThirdParty'
    shutil.copytree(video/'share/timelapse-video', notices/'FFmpeg')
    shutil.copy2(runtime/'lib/python3.12/LICENSE.txt', notices/'Python-LICENSE.txt')
    site = python.parent.parent/'lib/python3.12/site-packages'
    for distribution in ('numpy', 'pillow', 'pyinstaller'):
        info = next(site.glob(distribution+'-*.dist-info'))
        destination = notices/distribution
        destination.mkdir()
        for path in info.rglob('*'):
            if path.is_file() and ('license' in path.name.lower() or 'copying' in path.name.lower()):
                relative = path.relative_to(info)
                (destination/relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination/relative)
    shutil.copy2(ROOT/'LICENSE', resources/'LICENSE')
    shutil.copy2(ROOT/'packaging/macos/THIRD_PARTY.md', notices/'README.md')
    report = {'builder': 'timelapse-macos-v1', 'version': version, 'arch': 'arm64', 'minimum_macos': '14.0',
              'python_url': RUNTIME_URL, 'python_sha256': RUNTIME_SHA,
              'binary_audit': audit_binaries(stage)}
    (resources/'build-info.json').write_text(json.dumps(report, indent=2)+'\n')
    run(['/usr/bin/codesign', '--force', '--deep', '--sign', '-', '--identifier', BUNDLE_ID, stage])
    run(['/usr/bin/codesign', '--verify', '--deep', '--strict', stage])
    run([contents/'MacOS/Time-lapse', '--self-test'], env=dict(env, PATH='/usr/bin:/bin:/usr/sbin:/sbin'), cwd='/private/tmp')
    if app.exists():
        shutil.rmtree(app)
    zip_path.unlink(missing_ok=True)
    shutil.copytree(stage, app, symlinks=True)
    run(['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', app, zip_path])
    (zip_path.with_suffix('.zip.sha256')).write_text(digest(zip_path)+'  '+zip_path.name+'\n')
    print('App:', app)
    print('ZIP:', zip_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'dist')
    parser.add_argument('--offline', action='store_true', help='Reuse downloaded runtime and wheels')
    parser.add_argument('--replace', action='store_true', help='Replace a previous app made by this builder')
    args = parser.parse_args()
    build(args.output, args.offline, args.replace)
