#!/usr/bin/env python3
"""Patch Weixin 4.1.9.35 notification audio in its embedded Qt RCC data.

This script converts an input audio file to 44.1 kHz stereo 16-bit PCM WAV,
stores it in the larger bundled voip_phone_ringing.wav resource slot, and
redirects wechat_notify.wav to that slot.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


# Offsets observed in Weixin for Windows 4.1.9.35, Weixin.dll.
RCC_DATA_BASE = 118_876_192
WECHAT_NOTIFY_NODE = 128_660_000
WECHAT_NOTIFY_DATA_OFFSET_FIELD = WECHAT_NOTIFY_NODE + 10
ORIGINAL_WECHAT_NOTIFY_DATA_OFFSET = 479_700
EXPANSION_DATA_OFFSET = 714_644  # voip_phone_ringing.wav data entry.
EXPANSION_ENTRY_OFFSET = RCC_DATA_BASE + EXPANSION_DATA_OFFSET
EXPANSION_DATA_START = EXPANSION_ENTRY_OFFSET + 4
EXPANSION_CAPACITY = 546_700
NEXT_DATA_ENTRY_OFFSET = 120_137_540

TARGET_CHANNELS = 2
TARGET_RATE = 44_100
TARGET_BITS = 16
TARGET_CODEC = "pcm_s16le"


class PatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class WavInfo:
    channels: int
    sample_rate: int
    bits: int
    frames: int
    duration: float
    size: int
    sha256: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_be32(data: bytes | bytearray, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "big")


def write_be32(data: bytearray, offset: int, value: int) -> None:
    data[offset : offset + 4] = value.to_bytes(4, "big")


def wav_info(data: bytes) -> WavInfo:
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise PatchError("replacement is not a RIFF/WAVE file")

    import io

    with wave.open(io.BytesIO(data), "rb") as wav:
        channels = wav.getnchannels()
        sample_rate = wav.getframerate()
        bits = wav.getsampwidth() * 8
        frames = wav.getnframes()
        duration = frames / sample_rate

    return WavInfo(
        channels=channels,
        sample_rate=sample_rate,
        bits=bits,
        frames=frames,
        duration=duration,
        size=len(data),
        sha256=sha256_bytes(data),
    )


def run_ffmpeg(
    ffmpeg: str,
    input_audio: Path,
    output_wav: Path,
    *,
    trim_seconds: float | None = None,
) -> None:
    cmd = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-i",
        str(input_audio),
        "-ac",
        str(TARGET_CHANNELS),
        "-ar",
        str(TARGET_RATE),
        "-c:a",
        TARGET_CODEC,
    ]
    if trim_seconds is not None:
        cmd.extend(["-t", f"{trim_seconds:.6f}"])
    cmd.append(str(output_wav))

    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:
        raise PatchError(f"ffmpeg not found: {ffmpeg}") from exc
    except subprocess.CalledProcessError as exc:
        raise PatchError(f"ffmpeg failed with exit code {exc.returncode}") from exc


def convert_audio(
    input_audio: Path,
    *,
    ffmpeg: str,
    trim_to_fit: bool,
    work_dir: Path,
) -> bytes:
    max_seconds = (EXPANSION_CAPACITY - 44) / (TARGET_RATE * TARGET_CHANNELS * (TARGET_BITS // 8))
    output_wav = work_dir / "wechat_notify_replacement.wav"
    run_ffmpeg(ffmpeg, input_audio, output_wav)
    data = output_wav.read_bytes()

    if len(data) <= EXPANSION_CAPACITY:
        return data

    if not trim_to_fit:
        raise PatchError(
            f"converted WAV is too large: {len(data)} > {EXPANSION_CAPACITY} bytes. "
            f"Use --trim-to-fit to trim to about {max_seconds:.3f}s."
        )

    run_ffmpeg(ffmpeg, input_audio, output_wav, trim_seconds=max_seconds)
    data = output_wav.read_bytes()
    if len(data) > EXPANSION_CAPACITY:
        raise PatchError(f"trimmed WAV is still too large: {len(data)} > {EXPANSION_CAPACITY} bytes")
    return data


def validate_layout(blob: bytes | bytearray) -> int:
    if len(blob) <= NEXT_DATA_ENTRY_OFFSET:
        raise PatchError("DLL is smaller than expected for Weixin 4.1.9.35")

    if NEXT_DATA_ENTRY_OFFSET - EXPANSION_DATA_START != EXPANSION_CAPACITY:
        raise PatchError("internal constants are inconsistent")

    current_data_offset = read_be32(blob, WECHAT_NOTIFY_DATA_OFFSET_FIELD)
    if current_data_offset not in (ORIGINAL_WECHAT_NOTIFY_DATA_OFFSET, EXPANSION_DATA_OFFSET):
        raise PatchError(
            f"unexpected wechat_notify data offset {current_data_offset}; "
            "this DLL layout is not recognized"
        )

    expansion_size = read_be32(blob, EXPANSION_ENTRY_OFFSET)
    if not (0 < expansion_size <= EXPANSION_CAPACITY):
        raise PatchError(f"expansion slot has invalid size {expansion_size}")

    expansion_header = bytes(blob[EXPANSION_DATA_START : EXPANSION_DATA_START + 12])
    if expansion_header[:4] != b"RIFF" or expansion_header[8:12] != b"WAVE":
        raise PatchError("expansion slot does not currently contain RIFF/WAVE data")

    return current_data_offset


def make_backup(dll_path: Path, backup_path: Path | None) -> Path:
    if backup_path is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = dll_path.with_name(f"{dll_path.name}.bak_fxxkwx_{stamp}")

    if backup_path.exists():
        raise PatchError(f"backup already exists: {backup_path}")

    shutil.copy2(dll_path, backup_path)
    return backup_path


def patch_dll(
    dll_path: Path,
    replacement_wav: bytes,
    *,
    backup_path: Path | None,
    dry_run: bool,
) -> None:
    info = wav_info(replacement_wav)
    if (info.channels, info.sample_rate, info.bits) != (TARGET_CHANNELS, TARGET_RATE, TARGET_BITS):
        raise PatchError(
            "converted WAV has unexpected format: "
            f"{info.channels}ch {info.sample_rate}Hz {info.bits}bit"
        )
    if info.size > EXPANSION_CAPACITY:
        raise PatchError(f"replacement too large: {info.size} > {EXPANSION_CAPACITY}")

    blob = bytearray(dll_path.read_bytes())
    old_data_offset = validate_layout(blob)

    print(f"DLL: {dll_path}")
    print(f"Current wechat_notify data offset: {old_data_offset}")
    print(f"New wechat_notify data offset: {EXPANSION_DATA_OFFSET}")
    print(f"Expansion entry offset: {EXPANSION_ENTRY_OFFSET}")
    print(f"Replacement: {info.channels}ch {info.sample_rate}Hz {info.bits}bit {info.duration:.6f}s")
    print(f"Replacement size: {info.size} bytes")
    print(f"Expansion capacity: {EXPANSION_CAPACITY} bytes")
    print(f"Padding: {EXPANSION_CAPACITY - info.size} bytes")
    print(f"Replacement SHA256: {info.sha256}")

    if dry_run:
        print("Dry run: no files were modified.")
        return

    backup = make_backup(dll_path, backup_path)

    write_be32(blob, EXPANSION_ENTRY_OFFSET, info.size)
    blob[EXPANSION_DATA_START : EXPANSION_DATA_START + EXPANSION_CAPACITY] = (
        replacement_wav + b"\x00" * (EXPANSION_CAPACITY - info.size)
    )
    write_be32(blob, WECHAT_NOTIFY_DATA_OFFSET_FIELD, EXPANSION_DATA_OFFSET)

    dll_path.write_bytes(blob)
    print(f"Backup: {backup}")
    print(f"Patched DLL SHA256: {sha256_bytes(blob)}")


def inspect_dll(dll_path: Path) -> None:
    blob = dll_path.read_bytes()
    current_data_offset = validate_layout(blob)
    entry = RCC_DATA_BASE + current_data_offset
    size = read_be32(blob, entry)
    data = bytes(blob[entry + 4 : entry + 4 + size])
    info = wav_info(data)

    print(f"DLL: {dll_path}")
    print(f"wechat_notify node offset: {WECHAT_NOTIFY_NODE}")
    print(f"wechat_notify data offset field: {WECHAT_NOTIFY_DATA_OFFSET_FIELD}")
    print(f"wechat_notify data offset: {current_data_offset}")
    print(f"wechat_notify data entry: {entry}")
    print(f"Resource size: {size} bytes")
    print(f"Audio: {info.channels}ch {info.sample_rate}Hz {info.bits}bit {info.duration:.6f}s")
    print(f"Audio SHA256: {info.sha256}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replace Weixin 4.1.9.35 wechat_notify.wav through Qt RCC data redirection."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    patch = sub.add_parser("patch", help="convert an audio file and patch Weixin.dll")
    patch.add_argument("--dll", required=True, type=Path, help="path to Weixin.dll")
    patch.add_argument("--audio", required=True, type=Path, help="input audio file accepted by ffmpeg")
    patch.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable path")
    patch.add_argument("--backup", type=Path, help="backup path; defaults to a timestamped .bak file")
    patch.add_argument("--trim-to-fit", action="store_true", help="trim long audio to fit the expansion slot")
    patch.add_argument("--dry-run", action="store_true", help="validate and print planned changes only")

    inspect = sub.add_parser("inspect", help="inspect the current wechat_notify resource")
    inspect.add_argument("--dll", required=True, type=Path, help="path to Weixin.dll")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "inspect":
            inspect_dll(args.dll)
            return 0

        if args.command == "patch":
            if not args.dll.is_file():
                raise PatchError(f"DLL not found: {args.dll}")
            if not args.audio.is_file():
                raise PatchError(f"audio file not found: {args.audio}")

            with tempfile.TemporaryDirectory(prefix="fxxkwx_") as temp:
                replacement = convert_audio(
                    args.audio,
                    ffmpeg=args.ffmpeg,
                    trim_to_fit=args.trim_to_fit,
                    work_dir=Path(temp),
                )
            patch_dll(args.dll, replacement, backup_path=args.backup, dry_run=args.dry_run)
            return 0

        parser.error(f"unknown command: {args.command}")
        return 2
    except PatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
