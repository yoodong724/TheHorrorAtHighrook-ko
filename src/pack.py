#!/usr/bin/env python3
"""Create deterministic, hash-locked Highrook byte-delta packages.

Only explicitly allowlisted changed files are read into the package.  The ZIP
contains a manifest and insert payloads; unchanged bytes are represented as
offset-copy operations against the user's verified baseline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


FORMAT = "highrook-byte-delta-v1"
BLOCK = 4096
MASK64 = (1 << 64) - 1
BASE = 257
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
REQUIRED_METADATA = ("game_version", "source_revision", "translation_sha256", "build_id")
FULL_BASELINE_COUNT = 1684
HASH_LENGTH = 64


class PackageError(ValueError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def safe_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise PackageError(f"unsafe package path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise PackageError(f"unsafe package path: {value!r}")
    if ":" in path.parts[0]:
        raise PackageError(f"unsafe package path: {value!r}")
    return path.as_posix()


def _regular_file(root: Path, relative: str, *, required: bool) -> Path | None:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise PackageError(f"root is absent, non-directory, or symlinked: {root}")
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.exists() or current.is_symlink():
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise PackageError(f"symlink forbidden: {relative}")
    if not current.exists():
        if required:
            raise PackageError(f"required file missing: {relative}")
        return None
    if not current.is_file():
        raise PackageError(f"not a regular file: {relative}")
    return current


def _sha256_value(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != HASH_LENGTH or any(character not in "0123456789abcdef" for character in value):
        raise PackageError(f"{label} must be a lowercase SHA-256 hex string")
    return value


def release_inventory(metadata: dict[str, Any]) -> dict[str, str] | None:
    """Validate mode flags and return an explicitly supplied strict baseline."""
    if not isinstance(metadata, dict):
        raise PackageError("package metadata must be an object")
    mode = metadata.get("mode")
    release = metadata.get("release")
    if mode not in ("test", "release") or not isinstance(release, bool):
        raise PackageError("metadata must declare mode=test/release and boolean release")
    if (mode == "release") != release:
        raise PackageError("metadata mode and release flag disagree")
    if mode == "test" and "full_baseline" not in metadata:
        return None

    full = metadata.get("full_baseline")
    if not isinstance(full, dict) or full.get("algorithm") != "sha256" or full.get("file_count") != FULL_BASELINE_COUNT:
        scope = "release metadata" if mode == "release" else "test metadata with full_baseline"
        raise PackageError(f"{scope} requires full_baseline sha256 inventory of {FULL_BASELINE_COUNT} files")
    rows = full.get("files")
    if not isinstance(rows, list) or len(rows) != FULL_BASELINE_COUNT:
        raise PackageError(f"full_baseline must contain exactly {FULL_BASELINE_COUNT} rows")
    inventory: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise PackageError("full_baseline rows must contain exactly path and sha256")
        relative = safe_relative_path(row["path"])
        if relative in inventory:
            raise PackageError(f"duplicate full_baseline path: {relative}")
        inventory[relative] = _sha256_value(row["sha256"], f"full_baseline hash for {relative}")
    return inventory


def declared_new_outputs(metadata: dict[str, Any]) -> dict[str, str]:
    rows = metadata.get("new_outputs", [])
    if not isinstance(rows, list):
        raise PackageError("new_outputs must be a list")
    outputs: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise PackageError("new_outputs rows must contain exactly path and sha256")
        relative = safe_relative_path(row["path"])
        if relative in outputs:
            raise PackageError(f"duplicate new_outputs path: {relative}")
        outputs[relative] = _sha256_value(row["sha256"], f"new output hash for {relative}")
    return outputs


def validate_release_entries(metadata: dict[str, Any], files: list[dict[str, Any]]) -> None:
    inventory = release_inventory(metadata)
    if inventory is None:
        return
    new_outputs = declared_new_outputs(metadata)
    new_in_package: dict[str, str] = {}
    for item in files:
        relative = safe_relative_path(item["path"])
        input_hash = item.get("input_sha256")
        output_hash = _sha256_value(item.get("output_sha256"), f"output hash for {relative}")
        if input_hash is None:
            if relative in inventory:
                raise PackageError(f"baseline path packaged as new output: {relative}")
            new_in_package[relative] = output_hash
        else:
            _sha256_value(input_hash, f"input hash for {relative}")
            if inventory.get(relative) != input_hash:
                raise PackageError(f"packaged input is absent from or disagrees with full_baseline: {relative}")
    if new_in_package != new_outputs:
        raise PackageError("packaged new files must exactly match declared new_outputs paths and hashes")


def verify_release_baseline(metadata: dict[str, Any], baseline: Path) -> None:
    inventory = release_inventory(metadata)
    if inventory is None:
        return
    for relative, expected_hash in inventory.items():
        path = _regular_file(Path(baseline), relative, required=True)
        assert path is not None
        if sha256_file(path) != expected_hash:
            raise PackageError(f"full_baseline source hash mismatch: {relative}")


def _rolling_hash(data: bytes, start: int, size: int) -> int:
    value = 0
    for byte in data[start:start + size]:
        value = ((value * BASE) + byte + 1) & MASK64
    return value


def _common_edges(base: bytes, target: bytes) -> tuple[int, int]:
    limit = min(len(base), len(target))
    prefix = 0
    while prefix < limit and base[prefix] == target[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and base[-1 - suffix] == target[-1 - suffix]:
        suffix += 1
    return prefix, suffix


def make_delta(base: bytes, target: bytes) -> tuple[list[dict[str, list[int]]], bytes]:
    """Return copy/insert operations and a concatenated insert payload."""
    prefix, suffix = _common_edges(base, target)
    ops: list[dict[str, list[int]]] = []
    payload = bytearray()

    def copy(offset: int, length: int) -> None:
        if not length:
            return
        if ops and "copy" in ops[-1]:
            old_offset, old_length = ops[-1]["copy"]
            if old_offset + old_length == offset:
                ops[-1]["copy"][1] += length
                return
        ops.append({"copy": [offset, length]})

    def insert(data: bytes) -> None:
        if not data:
            return
        offset = len(payload)
        payload.extend(data)
        if ops and "insert" in ops[-1]:
            ops[-1]["insert"][1] += len(data)
        else:
            ops.append({"insert": [offset, len(data)]})

    copy(0, prefix)
    target_end = len(target) - suffix
    pos = prefix
    pending = pos
    width = min(BLOCK, max(0, target_end - pos), len(base))
    index: dict[int, list[int]] = {}
    if width >= 32:
        positions = list(range(0, len(base) - width + 1, width))
        last = len(base) - width
        if last >= 0 and (not positions or positions[-1] != last):
            positions.append(last)
        for offset in positions:
            index.setdefault(_rolling_hash(base, offset, width), []).append(offset)
        power = pow(BASE, width - 1, 1 << 64)
        rolling = _rolling_hash(target, pos, width) if pos + width <= target_end else None
        while rolling is not None and pos + width <= target_end:
            best_offset = -1
            best_length = 0
            for offset in index.get(rolling, ())[:64]:
                if base[offset:offset + width] != target[pos:pos + width]:
                    continue
                length = width
                maximum = min(len(base) - offset, target_end - pos)
                while length < maximum and base[offset + length] == target[pos + length]:
                    length += 1
                if length > best_length:
                    best_offset, best_length = offset, length
            if best_length:
                insert(target[pending:pos])
                copy(best_offset, best_length)
                pos += best_length
                pending = pos
                rolling = _rolling_hash(target, pos, width) if pos + width <= target_end else None
                continue
            if pos + width >= target_end:
                break
            old = target[pos] + 1
            new = target[pos + width] + 1
            rolling = (((rolling - (old * power)) & MASK64) * BASE + new) & MASK64
            pos += 1
    insert(target[pending:target_end])
    if suffix:
        copy(len(base) - suffix, suffix)
    return ops, bytes(payload)


def apply_delta(base: bytes, ops: list[dict[str, Any]], payload: bytes, output_size: int) -> bytes:
    result = bytearray()
    for operation in ops:
        if set(operation) == {"copy"}:
            offset, length = operation["copy"]
            source = base
        elif set(operation) == {"insert"}:
            offset, length = operation["insert"]
            source = payload
        else:
            raise PackageError("delta operation must contain exactly one copy or insert")
        if not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or length < 0 or offset + length > len(source):
            raise PackageError("delta operation range is invalid")
        result.extend(source[offset:offset + length])
        if len(result) > output_size:
            raise PackageError("delta exceeds declared output size")
    if len(result) != output_size:
        raise PackageError("delta output size mismatch")
    return bytes(result)


def _zip_entry(name: str, data: bytes) -> tuple[zipfile.ZipInfo, bytes]:
    info = zipfile.ZipInfo(name, FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info, data


def calculate_package_id(core: dict[str, Any]) -> str:
    return sha256(
        canonical(core)
        + b"".join(bytes.fromhex(item["payload_sha256"]) for item in core["files"])
    )[:24]


def build_package(
    baseline: Path,
    patched: Path,
    changed_files: Iterable[str],
    metadata: dict[str, Any],
    output_zip: Path,
    *,
    file_modes: dict[str, tuple[int, int]] | None = None,
) -> dict[str, Any]:
    """Create a deterministic ZIP from an explicit, exact changed-file list."""
    missing_metadata = [key for key in REQUIRED_METADATA if not metadata.get(key)]
    if missing_metadata:
        raise PackageError(f"release metadata missing: {missing_metadata}")
    release_inventory(metadata)
    verify_release_baseline(metadata, Path(baseline))
    paths = [safe_relative_path(path) for path in changed_files]
    if not paths or len(paths) != len(set(paths)):
        raise PackageError("changed-file allowlist must be non-empty and unique")
    paths.sort()
    if file_modes is not None:
        if set(file_modes) != set(paths) or any(
            len(modes) != 2 or any(type(mode) is not int or not 0 <= mode <= 0o777 for mode in modes)
            for modes in file_modes.values()
        ):
            raise PackageError("file_modes must give valid input/output modes for the exact allowlist")
    entries: list[dict[str, Any]] = []
    payloads: list[tuple[str, bytes]] = []
    for index_number, relative in enumerate(paths):
        patched_path = _regular_file(Path(patched), relative, required=True)
        baseline_path = _regular_file(Path(baseline), relative, required=False)
        target = patched_path.read_bytes()
        base = baseline_path.read_bytes() if baseline_path is not None else b""
        if baseline_path is not None and target == base:
            raise PackageError(f"allowlisted file is unchanged: {relative}")
        ops, payload = make_delta(base, target)
        rebuilt = apply_delta(base, ops, payload, len(target))
        if rebuilt != target:
            raise AssertionError(f"internal delta roundtrip failed: {relative}")
        payload_name = f"payload/{index_number:04d}.bin"
        entries.append({
            "path": relative,
            "input_sha256": sha256(base) if baseline_path is not None else None,
            "input_size": len(base) if baseline_path is not None else None,
            "input_mode": (file_modes[relative][0] if file_modes is not None else stat.S_IMODE(baseline_path.stat().st_mode)) if baseline_path is not None else None,
            "output_sha256": sha256(target),
            "output_size": len(target),
            "mode": file_modes[relative][1] if file_modes is not None else stat.S_IMODE(patched_path.stat().st_mode),
            "payload": payload_name,
            "payload_sha256": sha256(payload),
            "payload_size": len(payload),
            "ops": ops,
        })
        payloads.append((payload_name, payload))
    validate_release_entries(metadata, entries)
    core = {"format": FORMAT, "schema_version": "1.0.0", "metadata": metadata, "files": entries}
    package_id = calculate_package_id(core)
    manifest = dict(core, package_id=package_id)
    output_zip = Path(output_zip)
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_zip.with_name(output_zip.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            info, data = _zip_entry("manifest.json", canonical(manifest))
            archive.writestr(info, data, compresslevel=9)
            for name, data in payloads:
                info, data = _zip_entry(name, data)
                archive.writestr(info, data, compresslevel=9)
        os.replace(temporary, output_zip)
    finally:
        if temporary.exists():
            temporary.unlink()
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a deterministic Highrook byte-delta ZIP")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--patched", type=Path, required=True)
    parser.add_argument("--file", action="append", dest="files", required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    manifest = build_package(args.baseline, args.patched, args.files, metadata, args.output)
    print(json.dumps({"package_id": manifest["package_id"], "files": len(manifest["files"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
