# Time-lapse

在本机处理 RAW 延时摄影序列，修正曝光跳变与闪烁，预览后导出全尺寸照片。

## 能做什么

- 修正调节相机曝光造成的亮度台阶，平滑短时闪烁。
- 调用 Photoshop / Camera Raw 批量显影，生成整段预览视频。
- 观看确认后，导出全尺寸 JPG 和对应 XMP。
- 支持暂停续跑、手动选区、曝光校准及 XMP 备份恢复。

## 安装

需要 **macOS、Python 3.12+、Photoshop / Camera Raw、ffmpeg 和 ffprobe**。已有 Homebrew 时，可用 `brew install ffmpeg` 安装视频工具。

```sh
git clone https://github.com/NimbuSHh/time-lapse.git
cd time-lapse
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./run.command
```

以后双击 `run.command` 即可启动，页面会自动在浏览器打开。首次使用时，按系统提示允许控制 Photoshop；更新程序后需重新启动。

## 怎么用

1. 保存并关闭 Camera Raw，选择 RAW 素材文件夹。已有同名 XMP 会沿用，没有则使用 Camera Raw 默认设置。
2. 点击 **开始自动调色**，等待生成完整预览。处理期间不要在 Photoshop 中编辑其他照片；需要中断时点击 **停止并保留进度**。
3. 载入并观看完整视频，检查亮度、闪烁和色彩。数值检查通过且效果满意后，勾选 **我已完整观看这版视频**。
4. 点击 **确认并导出全尺寸 JPG 与 XMP**，完成后按页面显示的目录取出结果。

工作文件和结果默认保存在 **素材目录 / Time-lapse工作/**。需要手动调整时，展开首页的 **手动调整与高级工具**；自动选区不合适时可在 **高级：调整测量区域** 中指定。

照片不上传。自动流程使用独立工作副本，不覆盖原 RAW/XMP；手动写回 XMP 时会先备份。当前为早期版本，导出后请检查全尺寸照片。

[MIT License](LICENSE)
