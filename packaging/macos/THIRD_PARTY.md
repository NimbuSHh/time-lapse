# Bundled software

Time-lapse's application code is MIT licensed. The bundled components retain
their own licenses; their license texts accompany this file.

- **Python 3.12.15**: Python Software Foundation License and the included notices.
  Runtime: https://github.com/astral-sh/python-build-standalone/releases/tag/20261009
  Source: https://www.python.org/downloads/source/
- **NumPy 2.5.4**: BSD license; bundled library notices are in its license files.
  https://github.com/numpy/numpy
- **Pillow 12.3.0**: MIT-CMU license; bundled library notices are in its license files.
  https://github.com/python-pillow/Pillow
- **PyInstaller 6.22.3 bootloader**: GPL with the PyInstaller Bootloader Exception,
  permitting applications to retain their own license.
  https://pyinstaller.org/en/stable/license.html
- **FFmpeg 8.0 and libx264**: GPL version 2 or later, used as separate executables.
  Exact source archives, license texts, build commands and the build script are
  included in `FFmpeg/`. The build does not enable nonfree components or network
  protocols. `pkgconf` is a build tool; its source and license are also included.
  https://ffmpeg.org/legal.html

Photoshop and Camera Raw are not included. Install and license them separately.
