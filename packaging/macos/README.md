# macOS App

双击 `Time-lapse.app` 自动打开默认浏览器。关闭浏览器不退出 App；点击菜单栏光圈图标，可重新打开工作台或退出。退出时等待当前任务停止并保存进度。

- Apple Silicon，macOS 14 或更新版本；尚未制作 Intel 版本。
- 内置 Python 3.12、NumPy、Pillow、ffmpeg 和 ffprobe，不需要 Homebrew。
- Photoshop / Camera Raw 需自行安装；首次调用时按系统提示允许自动化控制。
- 当前仅做本机 ad-hoc 签名，未做 Apple Developer ID 签名和公证。下载到其他 Mac 后，可能需要在系统设置的“隐私与安全性”中允许打开。

## 构建

构建机需要 Apple Silicon、Python 3.12+（含 pip）和 Xcode Command Line Tools。

```sh
python3 scripts/build_macos.py
```

产物为 `dist/Time-lapse.app` 和 `dist/Time-lapse-0.1.0-macOS-arm64.zip`，附 ZIP 的 SHA-256 校验文件。构建环境和下载缓存只保存在 `build/macos/`，不修改系统 Python。

脚本从固定地址下载并核对 Python 和视频工具源码，在独立环境构建；视频工具只包含本项目需要的编解码功能。对应源码、许可证和编译命令随 App 放在 `Contents/Resources/ThirdParty/`。

已有缓存时可使用 `--offline`；另存产物用 `--output /指定目录`。默认不覆盖已有 App，`--replace` 只允许替换此脚本生成的版本，请先退出旧 App。

## 验证

```sh
dist/Time-lapse.app/Contents/MacOS/Time-lapse --self-test
```

自检实际运行内置 Python、图像分析、视频编码/解码及本地 HTTP 鉴权，只创建临时合成素材，不调用 Adobe。真实素材的显影效果仍应在使用时检查。
