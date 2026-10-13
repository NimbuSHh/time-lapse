"""All generated files live in the selected working directory, never in source code."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORK_FOLDER = 'Time-lapse工作'


def output_path(value):
    path = Path(value).expanduser().resolve()
    if path == PROJECT_ROOT or PROJECT_ROOT in path.parents:
        raise ValueError('输出不能写入程序项目目录，请选择素材所在的工作目录')
    return path


def owner(value):
    """Recognise a generated package without creating another nested workspace."""
    path = Path(value).expanduser().resolve()
    for parent in (path, *path.parents):
        if parent.name == WORK_FOLDER:
            return parent.parent
    return path


def output_root(work_dir):
    return output_path(owner(work_dir) / WORK_FOLDER)


def safe_child_output(output, source):
    """Only our dedicated child is allowed beneath a top-level media directory.

    Media scanning is nonrecursive. This never permits publishing into an input
    folder itself, or into an existing package/render directory.
    """
    output, source = Path(output).resolve(), Path(source).resolve()
    child = source / WORK_FOLDER
    return output == child or child in output.parents
