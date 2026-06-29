# fxxkwx

`fxxkwx` 是一个用于替换 Windows 版微信 `Weixin.dll` 内置提示音的小型
Python 工具。

本项目针对微信 `4.1.9.35` 中观察到的 Qt RCC 资源布局。脚本会把输入音频
转换为 `44100Hz / 双声道 / 16-bit PCM WAV`，写入包内更大的
`voip_phone_ringing.wav` 资源槽位，然后把 `wechat_notify.wav` 重定向到该槽位。
这样可以避免直接把低采样率或低位深音频塞进原提示音槽位后，被微信按固定格式
播放而导致加速的问题。

作者：RuabbitX `<RuabbitX996@outlook.com>`

## 环境要求

- Python 3.10+
- `ffmpeg` 已加入 `PATH`
- 本地拥有 Windows 版微信 `4.1.9.35` 的 `Weixin.dll`

## 使用方法

查看当前提示音资源：

```powershell
python .\fxxkwx.py inspect --dll "C:\path\to\4.1.9.35\Weixin.dll"
```

替换提示音：

```powershell
python .\fxxkwx.py patch --dll "C:\path\to\4.1.9.35\Weixin.dll" --audio "C:\path\to\sound.mp3"
```

只预览将要执行的修改，不写入 DLL：

```powershell
python .\fxxkwx.py patch --dll "C:\path\to\4.1.9.35\Weixin.dll" --audio "C:\path\to\sound.mp3" --dry-run
```

如果转换后的音频超过扩展槽位容量，可以换更短的音频，或允许脚本自动截断：

```powershell
python .\fxxkwx.py patch --dll "C:\path\to\4.1.9.35\Weixin.dll" --audio "C:\path\to\sound.mp3" --trim-to-fit
```

## 修改内容

脚本使用以下版本相关偏移：

- `wechat_notify.wav` 的 Qt tree 节点：`128660000`
- `wechat_notify.wav` 的 data offset 字段：`128660010`
- 原始 `wechat_notify.wav` 的 data offset：`479700`
- 扩展槽位 data offset：`714644`
- 扩展槽位 data entry：`119590836`
- 扩展槽位容量：`546700` 字节

扩展槽位原本属于 `voip_phone_ringing.wav`，因此 patch 后这个资源也会解析到
替换后的音频。

写入前，脚本会在 DLL 同目录创建带时间戳的备份文件。

## 注意事项

本项目不包含微信二进制文件或示例音频文件。请仅对你有权修改的文件使用本工具。
