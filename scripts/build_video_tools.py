"""Build standalone FFmpeg/ffprobe with libx264 from pinned, redistributable sources."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import urllib.request

SOURCES = {
    'ffmpeg-8.0.tar.xz': (
        'https://ffmpeg.org/releases/ffmpeg-8.0.tar.xz',
        'b2751fccb6cc4c77708113cd78b561059b6fa904b24162fa0be2d60273d27b8e'),
    'x264-b35605ace3ddf7c1a5d67a2eb553f034aef41d55.tar.bz2': (
        'https://code.videolan.org/videolan/x264/-/archive/b35605ace3ddf7c1a5d67a2eb553f034aef41d55/x264-b35605ace3ddf7c1a5d67a2eb553f034aef41d55.tar.bz2',
        '6eeb82934e69fd51e043bd8c5b0d152839638d1ce7aa4eea65a3fedcf83ff224'),
    'pkgconf-2.5.1.tar.xz': (
        'https://distfiles.ariadne.space/pkgconf/pkgconf-2.5.1.tar.xz',
        'cd05c9589b9f86ecf044c10a2269822bc9eb001eced2582cfffd658b0a50c243'),
}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def download(name, folder):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder/name
    url, expected = SOURCES[name]
    if not path.exists():
        temporary = path.with_suffix(path.suffix+'.download')
        try:
            with urllib.request.urlopen(url, timeout=120) as source, temporary.open('wb') as target:
                shutil.copyfileobj(source, target)
            if digest(temporary) != expected:
                raise ValueError('Source checksum mismatch: '+name)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    if digest(path) != expected:
        raise ValueError('Source checksum mismatch: '+name)
    return path


def build(work, downloads, jobs=4):
    work, downloads = Path(work).resolve(), Path(downloads).resolve()
    work.mkdir(parents=True, exist_ok=True)
    prefix = work/'prefix'
    stamp = work/'complete.json'
    contract = {'arch': platform.machine(), 'macos': '14.0', 'sources': SOURCES, 'recipe': 1}
    if stamp.is_file():
        saved = json.loads(stamp.read_text())
        if saved['contract'] == json.loads(json.dumps(contract)) and all(
            (prefix/'bin'/name).is_file() and digest(prefix/'bin'/name) == sha
            for name, sha in saved['binaries'].items()
        ):
            return prefix
    sources = {}
    for name in SOURCES:
        archive = download(name, downloads)
        with tarfile.open(archive) as tar:
            top = tar.getnames()[0].split('/')[0]
            source = work/'src'/top
            if not source.is_dir():
                tar.extractall(work/'src', filter='data')
        sources[name.split('-')[0]] = source
    env = dict(os.environ, MACOSX_DEPLOYMENT_TARGET='14.0',
               PATH=str(prefix/'bin')+':/usr/bin:/bin:/usr/sbin:/sbin',
               PKG_CONFIG_PATH=str(prefix/'lib/pkgconfig'),
               PKG_CONFIG_ALLOW_SYSTEM_CFLAGS='1', PKG_CONFIG_ALLOW_SYSTEM_LIBS='1')
    commands = []
    def run(args, folder):
        commands.append({'source': folder.name, 'argv': [str(x) for x in args]})
        print('+', ' '.join(map(str, args)), flush=True)
        subprocess.run(args, cwd=folder, env=env, check=True)
    run(['./configure', '--prefix='+str(prefix), '--disable-shared'], sources['pkgconf'])
    run(['make', '-j'+str(jobs)], sources['pkgconf'])
    run(['make', 'install'], sources['pkgconf'])
    run(['./configure', '--prefix='+str(prefix), '--enable-static', '--enable-pic',
         '--disable-cli', '--disable-opencl', '--bit-depth=8'], sources['x264'])
    run(['make', '-j'+str(jobs)], sources['x264'])
    run(['make', 'install'], sources['x264'])
    run(['./configure', '--prefix='+str(prefix), '--pkg-config='+str(prefix/'bin/pkgconf'),
         '--enable-gpl', '--enable-libx264', '--disable-autodetect', '--disable-shared',
         '--enable-static', '--disable-debug', '--disable-doc', '--disable-ffplay',
         '--disable-network', '--disable-everything', '--enable-ffmpeg', '--enable-ffprobe',
         '--enable-protocol=file,pipe', '--enable-demuxer=image2,mov',
         '--enable-muxer=mp4,rawvideo', '--enable-decoder=mjpeg,h264',
         '--enable-encoder=libx264,rawvideo', '--enable-parser=h264,mjpeg',
         '--enable-filter=scale,format,setsar,sidedata,setparams,null',
         '--enable-swscale', '--enable-avfilter', '--enable-pthreads'], sources['ffmpeg'])
    run(['make', '-j'+str(jobs)], sources['ffmpeg'])
    run(['make', 'install'], sources['ffmpeg'])
    for name in ('ffmpeg', 'ffprobe'):
        subprocess.run([prefix/'bin'/name, '-version'], check=True)
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', prefix/'bin'/name], check=True)
    notices = prefix/'share/timelapse-video'
    notices.mkdir(parents=True, exist_ok=True)
    for component, filename in [('ffmpeg','COPYING.GPLv2'),('x264','COPYING'),('pkgconf','COPYING')]:
        shutil.copy2(sources[component]/filename, notices/(component+'-LICENSE.txt'))
    record = {'contract': contract, 'commands': commands,
              'binaries': {name:digest(prefix/'bin'/name) for name in ('ffmpeg','ffprobe')}}
    (notices/'build.json').write_text(json.dumps(record, indent=2)+'\n')
    # Include matching complete source archives and the build recipe in the app.
    for name in SOURCES:
        shutil.copy2(downloads/name, notices/name)
    shutil.copy2(__file__, notices/'build_video_tools.py')
    stamp.write_text(json.dumps(record, indent=2)+'\n')
    return prefix


if __name__ == '__main__':
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, default=root/'build/macos/video')
    parser.add_argument('--downloads', type=Path, default=root/'build/macos/downloads')
    parser.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args()
    print(build(args.work, args.downloads, args.jobs))
