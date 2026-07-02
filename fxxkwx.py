#!/usr/bin/env python3
"""Patch Weixin notification audio in embedded Qt RCC data.

The tool discovers the relevant RCC name/tree/data offsets from the DLL before
patching. It looks for UTF-16BE Qt resource names, RIFF/WAVE data entries, and
then matches tree file nodes to data slots through their RCC data offsets.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


TARGET_RESOURCE_NAME = "wechat_notify.wav"
EXPANSION_RESOURCE_NAME = "voip_phone_ringing.wav"
KNOWN_RESOURCE_NAMES = [
    "multimedia",
    "wav",
    "lock_closing.wav",
    TARGET_RESOURCE_NAME,
    "voip_phone_ending.wav",
    EXPANSION_RESOURCE_NAME,
    "lock_opening.wav",
]

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


@dataclass(frozen=True)
class NameRecord:
    name: str
    offset: int
    length: int
    hash_value: int


@dataclass(frozen=True)
class RiffEntry:
    entry_offset: int
    data_start: int
    size: int
    info: WavInfo


@dataclass(frozen=True)
class TreeNode:
    offset: int
    name_rel: int
    flags: int
    is_dir: bool
    country: int | None = None
    language: int | None = None
    data_offset: int | None = None
    child_count: int | None = None
    first_child: int | None = None


@dataclass(frozen=True)
class Layout:
    name_base: int
    data_base: int
    target_node: TreeNode
    expansion_node: TreeNode
    target_data_offset: int
    expansion_data_offset: int
    target_entry_offset: int
    expansion_entry_offset: int
    expansion_data_start: int
    expansion_capacity: int
    target_name_record: NameRecord
    expansion_name_record: NameRecord


def sha256_bytes(data: bytes | bytearray) -> str:
    return hashlib.sha256(data).hexdigest()


def read_be16(data: bytes | bytearray, offset: int) -> int:
    return (data[offset] << 8) | data[offset + 1]


def read_be32(data: bytes | bytearray, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "big")


def write_be32(data: bytearray, offset: int, value: int) -> None:
    data[offset : offset + 4] = value.to_bytes(4, "big")


def wav_info(data: bytes) -> WavInfo:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise PatchError("data is not a RIFF/WAVE file")

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


def find_name_record(blob: bytes, name: str) -> NameRecord:
    encoded = b"".join(ord(ch).to_bytes(2, "big") for ch in name)
    hits: list[NameRecord] = []
    start = 0

    while True:
        hit = blob.find(encoded, start)
        if hit < 0:
            break
        record_offset = hit - 6
        if record_offset >= 0:
            length = read_be16(blob, record_offset)
            if length == len(name):
                hash_value = read_be32(blob, record_offset + 2)
                hits.append(NameRecord(name, record_offset, length, hash_value))
        start = hit + 1

    if not hits:
        raise PatchError(f"resource name not found: {name}")
    if len(hits) > 1:
        # Use the first record; Qt RCC name records are usually unique for this set.
        print(f"warning: multiple name records found for {name}; using {hits[0].offset}", file=sys.stderr)
    return hits[0]


def find_name_records(blob: bytes) -> dict[str, NameRecord]:
    return {name: find_name_record(blob, name) for name in KNOWN_RESOURCE_NAMES}


def find_riff_wave_entries(blob: bytes) -> list[RiffEntry]:
    entries: list[RiffEntry] = []
    seen: set[int] = set()
    start = 0

    while True:
        riff = blob.find(b"RIFF", start)
        if riff < 0:
            break
        start = riff + 1
        if riff < 4 or riff + 12 > len(blob) or blob[riff + 8 : riff + 12] != b"WAVE":
            continue

        chunk_size = int.from_bytes(blob[riff + 4 : riff + 8], "little")
        total_size = chunk_size + 8
        entry_offset = riff - 4
        if entry_offset in seen:
            continue
        if entry_offset < 0 or riff + total_size > len(blob):
            continue
        rcc_size = read_be32(blob, entry_offset)
        if rcc_size != total_size:
            continue

        data = bytes(blob[riff : riff + total_size])
        try:
            info = wav_info(data)
        except Exception:
            continue

        entries.append(RiffEntry(entry_offset, riff, total_size, info))
        seen.add(entry_offset)

    if not entries:
        raise PatchError("no RCC RIFF/WAVE data entries found")
    return sorted(entries, key=lambda entry: entry.entry_offset)


def iter_be32_hits(blob: bytes, value: int, start: int, end: int) -> list[int]:
    pattern = value.to_bytes(4, "big")
    hits: list[int] = []
    pos = start
    while True:
        hit = blob.find(pattern, pos, end)
        if hit < 0:
            return hits
        hits.append(hit)
        pos = hit + 1


def parse_node(blob: bytes, offset: int) -> TreeNode | None:
    if offset < 0 or offset + 22 > len(blob):
        return None

    flags = read_be16(blob, offset + 4)
    name_rel = read_be32(blob, offset)
    if name_rel < 0 or name_rel > 20_000_000:
        return None

    is_dir = bool(flags & 0x0002)
    if is_dir:
        child_count = read_be32(blob, offset + 6)
        first_child = read_be32(blob, offset + 10)
        if child_count > 100_000 or first_child > 1_000_000:
            return None
        return TreeNode(
            offset=offset,
            name_rel=name_rel,
            flags=flags,
            is_dir=True,
            child_count=child_count,
            first_child=first_child,
        )

    country = read_be16(blob, offset + 6)
    language = read_be16(blob, offset + 8)
    data_offset = read_be32(blob, offset + 10)
    if country > 10_000 or language > 10_000 or data_offset > 100_000_000:
        return None
    return TreeNode(
        offset=offset,
        name_rel=name_rel,
        flags=flags,
        is_dir=False,
        country=country,
        language=language,
        data_offset=data_offset,
    )


def collect_nodes_for_base(
    blob: bytes,
    name_base: int,
    records: dict[str, NameRecord],
    *,
    search_start: int,
    search_end: int,
) -> dict[str, list[TreeNode]]:
    nodes: dict[str, list[TreeNode]] = {name: [] for name in records}
    for name, record in records.items():
        rel = record.offset - name_base
        if rel < 0:
            continue
        for hit in iter_be32_hits(blob, rel, search_start, search_end):
            node = parse_node(blob, hit)
            if node is not None:
                nodes[name].append(node)
    return nodes


def score_name_base(nodes: dict[str, list[TreeNode]]) -> int:
    score = 0
    for name in ("multimedia", "wav"):
        score += 2 * sum(1 for node in nodes.get(name, []) if node.is_dir)
    for name in KNOWN_RESOURCE_NAMES:
        if name.endswith(".wav"):
            score += 3 * sum(1 for node in nodes.get(name, []) if not node.is_dir)
    return score


def find_name_base_candidates(blob: bytes, records: dict[str, NameRecord]) -> list[tuple[int, int, dict[str, list[TreeNode]]]]:
    min_record = min(record.offset for record in records.values())
    search_start = min_record
    search_end = min(len(blob), min_record + 2_000_000)
    base_min = max(0, min_record - 8192)
    base_max = min_record

    rel_map: dict[int, list[tuple[int, str]]] = {}
    for name, record in records.items():
        for name_base in range(base_min, base_max + 1):
            rel = record.offset - name_base
            rel_map.setdefault(rel, []).append((name_base, name))

    nodes_by_base: dict[int, dict[str, list[TreeNode]]] = {}
    for offset in range(search_start, search_end - 22):
        rel = read_be32(blob, offset)
        candidates = rel_map.get(rel)
        if not candidates:
            continue
        node = parse_node(blob, offset)
        if node is None:
            continue
        for name_base, name in candidates:
            bucket = nodes_by_base.setdefault(name_base, {resource: [] for resource in records})
            bucket[name].append(node)

    candidates: list[tuple[int, int, dict[str, list[TreeNode]]]] = []
    for name_base, nodes in nodes_by_base.items():
        score = score_name_base(nodes)
        if score >= 5:
            candidates.append((score, name_base, nodes))

    if not candidates:
        raise PatchError("failed to identify Qt RCC name base and tree nodes")
    return sorted(candidates, reverse=True)


def choose_data_base(
    nodes_by_name: dict[str, list[TreeNode]],
    riff_entries: list[RiffEntry],
) -> int:
    wave_entry_offsets = {entry.entry_offset for entry in riff_entries}
    file_nodes = [
        node
        for name in KNOWN_RESOURCE_NAMES
        for node in nodes_by_name.get(name, [])
        if name.endswith(".wav") and not node.is_dir and node.data_offset is not None
    ]
    if not file_nodes:
        raise PatchError("no WAV file tree nodes found")

    scores: dict[int, int] = {}
    for node in file_nodes:
        assert node.data_offset is not None
        for entry in riff_entries:
            data_base = entry.entry_offset - node.data_offset
            scores[data_base] = scores.get(data_base, 0) + sum(
                1
                for other in file_nodes
                if other.data_offset is not None and data_base + other.data_offset in wave_entry_offsets
            )

    if not scores:
        raise PatchError("failed to infer RCC data base")
    data_base, score = max(scores.items(), key=lambda item: item[1])
    if score < 2:
        raise PatchError("RCC data base inference was too weak")
    return data_base


def choose_file_node(
    blob: bytes,
    nodes: list[TreeNode],
    data_base: int,
    *,
    name: str,
    quiet: bool = False,
) -> TreeNode:
    valid: list[TreeNode] = []
    for node in nodes:
        if node.is_dir or node.data_offset is None:
            continue
        entry = data_base + node.data_offset
        if entry < 0 or entry + 16 > len(blob):
            continue
        size = read_be32(blob, entry)
        data_start = entry + 4
        if size > 0 and data_start + size <= len(blob) and blob[data_start : data_start + 4] == b"RIFF":
            valid.append(node)

    if not valid:
        raise PatchError(f"no valid file node found for {name}")
    if len(valid) > 1 and not quiet:
        # Prefer the first matching node in the tree. Duplicates are uncommon in
        # this resource group.
        print(f"warning: multiple valid file nodes found for {name}; using {valid[0].offset}", file=sys.stderr)
    return valid[0]


def discover_layout(blob: bytes) -> Layout:
    records = find_name_records(blob)
    riff_entries = find_riff_wave_entries(blob)
    name_candidates = find_name_base_candidates(blob, records)

    best: tuple[int, int, int, dict[str, list[TreeNode]], int, TreeNode, TreeNode] | None = None
    wave_entry_offsets = {entry.entry_offset for entry in riff_entries}
    for name_score, name_base, nodes_by_name in name_candidates:
        try:
            data_base = choose_data_base(nodes_by_name, riff_entries)
        except PatchError:
            continue

        valid_names = 0
        valid_nodes = 0
        for resource_name in KNOWN_RESOURCE_NAMES:
            if not resource_name.endswith(".wav"):
                continue
            matched = False
            for node in nodes_by_name.get(resource_name, []):
                if node.is_dir or node.data_offset is None:
                    continue
                if data_base + node.data_offset in wave_entry_offsets:
                    valid_nodes += 1
                    matched = True
            if matched:
                valid_names += 1

        try:
            target_node = choose_file_node(
                blob,
                nodes_by_name[TARGET_RESOURCE_NAME],
                data_base,
                name=TARGET_RESOURCE_NAME,
                quiet=True,
            )
            expansion_node = choose_file_node(
                blob,
                nodes_by_name[EXPANSION_RESOURCE_NAME],
                data_base,
                name=EXPANSION_RESOURCE_NAME,
                quiet=True,
            )
        except PatchError:
            continue

        score_tuple = (valid_names, valid_nodes, name_score)
        if best is None or score_tuple > best[:3]:
            best = (*score_tuple, nodes_by_name, data_base, target_node, expansion_node)

    if best is None:
        raise PatchError("failed to connect RCC tree nodes to RIFF/WAVE data entries")

    _, _, _, nodes_by_name, data_base, target_node, expansion_node = best
    name_base = next(
        base
        for _, base, candidate_nodes in name_candidates
        if candidate_nodes is nodes_by_name
    )
    assert target_node.data_offset is not None
    assert expansion_node.data_offset is not None

    expansion_entry = data_base + expansion_node.data_offset
    expansion_data_start = expansion_entry + 4

    candidate_entries = sorted(entry.entry_offset for entry in riff_entries if entry.entry_offset > expansion_entry)
    if not candidate_entries:
        raise PatchError("failed to find the next data entry after expansion slot")
    expansion_capacity = candidate_entries[0] - expansion_data_start

    if expansion_capacity <= 0:
        raise PatchError("invalid expansion capacity")

    return Layout(
        name_base=name_base,
        data_base=data_base,
        target_node=target_node,
        expansion_node=expansion_node,
        target_data_offset=target_node.data_offset,
        expansion_data_offset=expansion_node.data_offset,
        target_entry_offset=data_base + target_node.data_offset,
        expansion_entry_offset=expansion_entry,
        expansion_data_start=expansion_data_start,
        expansion_capacity=expansion_capacity,
        target_name_record=records[TARGET_RESOURCE_NAME],
        expansion_name_record=records[EXPANSION_RESOURCE_NAME],
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
    capacity: int,
) -> bytes:
    max_seconds = (capacity - 44) / (TARGET_RATE * TARGET_CHANNELS * (TARGET_BITS // 8))
    output_wav = work_dir / "wechat_notify_replacement.wav"
    run_ffmpeg(ffmpeg, input_audio, output_wav)
    data = output_wav.read_bytes()

    if len(data) <= capacity:
        return data

    if not trim_to_fit:
        raise PatchError(
            f"converted WAV is too large: {len(data)} > {capacity} bytes. "
            f"Use --trim-to-fit to trim to about {max_seconds:.3f}s."
        )

    run_ffmpeg(ffmpeg, input_audio, output_wav, trim_seconds=max_seconds)
    data = output_wav.read_bytes()
    if len(data) > capacity:
        raise PatchError(f"trimmed WAV is still too large: {len(data)} > {capacity} bytes")
    return data


def make_backup(dll_path: Path, backup_path: Path | None) -> Path:
    if backup_path is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = dll_path.with_name(f"{dll_path.name}.bak_fxxkwx_{stamp}")

    if backup_path.exists():
        raise PatchError(f"backup already exists: {backup_path}")

    shutil.copy2(dll_path, backup_path)
    return backup_path


def print_layout(layout: Layout) -> None:
    print(f"name_base: {layout.name_base}")
    print(f"data_base: {layout.data_base}")
    print(f"{TARGET_RESOURCE_NAME} name record: {layout.target_name_record.offset}")
    print(f"{TARGET_RESOURCE_NAME} tree node: {layout.target_node.offset}")
    print(f"{TARGET_RESOURCE_NAME} data offset field: {layout.target_node.offset + 10}")
    print(f"{TARGET_RESOURCE_NAME} data offset: {layout.target_data_offset}")
    print(f"{TARGET_RESOURCE_NAME} data entry: {layout.target_entry_offset}")
    print(f"{EXPANSION_RESOURCE_NAME} name record: {layout.expansion_name_record.offset}")
    print(f"{EXPANSION_RESOURCE_NAME} tree node: {layout.expansion_node.offset}")
    print(f"{EXPANSION_RESOURCE_NAME} data offset: {layout.expansion_data_offset}")
    print(f"{EXPANSION_RESOURCE_NAME} data entry: {layout.expansion_entry_offset}")
    print(f"expansion data start: {layout.expansion_data_start}")
    print(f"expansion capacity: {layout.expansion_capacity}")


def patch_dll(
    dll_path: Path,
    replacement_wav: bytes,
    *,
    backup_path: Path | None,
    dry_run: bool,
) -> None:
    blob = bytearray(dll_path.read_bytes())
    layout = discover_layout(blob)
    info = wav_info(replacement_wav)

    if (info.channels, info.sample_rate, info.bits) != (TARGET_CHANNELS, TARGET_RATE, TARGET_BITS):
        raise PatchError(
            "converted WAV has unexpected format: "
            f"{info.channels}ch {info.sample_rate}Hz {info.bits}bit"
        )
    if info.size > layout.expansion_capacity:
        raise PatchError(f"replacement too large: {info.size} > {layout.expansion_capacity}")

    print(f"DLL: {dll_path}")
    print_layout(layout)
    print(f"Replacement: {info.channels}ch {info.sample_rate}Hz {info.bits}bit {info.duration:.6f}s")
    print(f"Replacement size: {info.size} bytes")
    print(f"Padding: {layout.expansion_capacity - info.size} bytes")
    print(f"Replacement SHA256: {info.sha256}")

    if dry_run:
        print("Dry run: no files were modified.")
        return

    backup = make_backup(dll_path, backup_path)

    write_be32(blob, layout.expansion_entry_offset, info.size)
    blob[layout.expansion_data_start : layout.expansion_data_start + layout.expansion_capacity] = (
        replacement_wav + b"\x00" * (layout.expansion_capacity - info.size)
    )
    write_be32(blob, layout.target_node.offset + 10, layout.expansion_data_offset)

    dll_path.write_bytes(blob)
    print(f"Backup: {backup}")
    print(f"Patched DLL SHA256: {sha256_bytes(blob)}")


def inspect_dll(dll_path: Path) -> None:
    blob = dll_path.read_bytes()
    layout = discover_layout(blob)
    size = read_be32(blob, layout.target_entry_offset)
    data = bytes(blob[layout.target_entry_offset + 4 : layout.target_entry_offset + 4 + size])
    info = wav_info(data)

    print(f"DLL: {dll_path}")
    print_layout(layout)
    print(f"Resource size: {size} bytes")
    print(f"Audio: {info.channels}ch {info.sample_rate}Hz {info.bits}bit {info.duration:.6f}s")
    print(f"Audio SHA256: {info.sha256}")


def discover_dll(dll_path: Path) -> None:
    blob = dll_path.read_bytes()
    layout = discover_layout(blob)
    riff_entries = find_riff_wave_entries(blob)

    print(f"DLL: {dll_path}")
    print_layout(layout)
    print()
    print("RIFF/WAVE data entries:")
    for entry in riff_entries:
        print(
            f"entry={entry.entry_offset} data_start={entry.data_start} size={entry.size} "
            f"{entry.info.channels}ch {entry.info.sample_rate}Hz {entry.info.bits}bit "
            f"{entry.info.duration:.6f}s sha256={entry.info.sha256[:16]}..."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replace Weixin wechat_notify.wav through discovered Qt RCC data redirection."
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

    discover = sub.add_parser("discover", help="print discovered RCC offsets and RIFF/WAVE slots")
    discover.add_argument("--dll", required=True, type=Path, help="path to Weixin.dll")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "inspect":
            inspect_dll(args.dll)
            return 0

        if args.command == "discover":
            discover_dll(args.dll)
            return 0

        if args.command == "patch":
            if not args.dll.is_file():
                raise PatchError(f"DLL not found: {args.dll}")
            if not args.audio.is_file():
                raise PatchError(f"audio file not found: {args.audio}")

            # Discover first so conversion can use the actual expansion capacity.
            layout = discover_layout(args.dll.read_bytes())
            with tempfile.TemporaryDirectory(prefix="fxxkwx_") as temp:
                replacement = convert_audio(
                    args.audio,
                    ffmpeg=args.ffmpeg,
                    trim_to_fit=args.trim_to_fit,
                    work_dir=Path(temp),
                    capacity=layout.expansion_capacity,
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
