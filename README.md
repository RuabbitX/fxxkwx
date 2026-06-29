# fxxkwx

`fxxkwx` is a small Python tool for replacing the bundled Weixin for Windows
notification sound in `Weixin.dll`.

It targets the Qt RCC layout observed in Weixin `4.1.9.35`. The tool converts
an input audio file to `44100Hz / stereo / 16-bit PCM WAV`, writes it into the
larger bundled `voip_phone_ringing.wav` resource slot, and redirects
`wechat_notify.wav` to that slot. This avoids the accelerated playback caused
by replacing the original small notification slot with lower-rate audio.

Author: RuabbitX `<RuabbitX996@outlook.com>`

## Requirements

- Python 3.10+
- `ffmpeg` available in `PATH`
- A local copy of `Weixin.dll` from Weixin for Windows `4.1.9.35`

## Usage

Inspect the current resource:

```powershell
python .\fxxkwx.py inspect --dll "C:\path\to\4.1.9.35\Weixin.dll"
```

Patch the notification sound:

```powershell
python .\fxxkwx.py patch --dll "C:\path\to\4.1.9.35\Weixin.dll" --audio "C:\path\to\sound.mp3"
```

Preview without modifying the DLL:

```powershell
python .\fxxkwx.py patch --dll "C:\path\to\4.1.9.35\Weixin.dll" --audio "C:\path\to\sound.mp3" --dry-run
```

If the converted audio is longer than the expansion slot capacity, either use a
shorter audio file or let the tool trim it:

```powershell
python .\fxxkwx.py patch --dll "C:\path\to\4.1.9.35\Weixin.dll" --audio "C:\path\to\sound.mp3" --trim-to-fit
```

## What It Changes

The script uses these version-specific offsets:

- `wechat_notify.wav` Qt tree node: `128660000`
- `wechat_notify.wav` data offset field: `128660010`
- original `wechat_notify.wav` data offset: `479700`
- expansion data offset: `714644`
- expansion data entry: `119590836`
- expansion capacity: `546700` bytes

The expansion slot originally belongs to `voip_phone_ringing.wav`, so that
resource will also resolve to the replacement audio after patching.

The script creates a timestamped backup beside the DLL before writing changes.

## Notes

This project does not include Weixin binaries or sample audio files. Use it only
against files you are allowed to modify.
