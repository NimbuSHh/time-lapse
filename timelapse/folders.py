"""Native macOS folder selection; no media are uploaded or modified."""
from pathlib import Path
import subprocess
import sys
from .project import directory

PROMPTS = {
    'exportDirectory': '选择修正 XMP 的保存目录（直接保存，不新建子目录）',
    'raw': '选择 RAW 与 XMP 所在的文件夹',
    'previews': '选择 ACR 导出的 JPG 小图文件夹',
    'verifyPath': '选择 ACR 重新导出的复核小图文件夹',
}
# Paths and prompts are arguments, never interpolated into executable script.
SCRIPT = '''on run argv
    try
        set initialFolder to POSIX file (item 2 of argv) as alias
        activate
        set selectedFolder to choose folder with prompt (item 1 of argv) default location initialFolder
        return POSIX path of selectedFolder
    on error number -128
        return ""
    end try
end run
'''


def choose_folder(field, initial=''):
    if field not in PROMPTS:
        raise ValueError('未知的文件夹用途')
    if not isinstance(initial, str) or len(initial) > 4096 or '\x00' in initial:
        raise ValueError('文件夹路径无效')
    if sys.platform != 'darwin':
        raise ValueError('当前系统暂不支持原生文件夹窗口，请在路径栏输入文件夹路径')
    start = Path(initial).expanduser() if initial else Path.home()
    if not start.is_dir():
        start = start.parent if start.parent.is_dir() else Path.home()
    try:
        result = subprocess.run(['/usr/bin/osascript', '-e', SCRIPT, PROMPTS[field], str(start.resolve())],
                                capture_output=True, text=True, timeout=180, check=False)
    except subprocess.TimeoutExpired as e:
        raise ValueError('文件夹选择已超时，请重新选择') from e
    if result.returncode:
        raise ValueError('未能打开系统文件夹选择窗口，请重试或手动输入路径')
    value = result.stdout.removesuffix('\n')
    if not value:
        return {'cancelled': True, 'path': None}
    return {'cancelled': False, 'path': str(directory(value))}
