#!/usr/bin/env python3
"""Build the reviewed Korean patch from a pristine Steam installation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "work/stage8/build"))
import build  # noqa: E402
import pack  # noqa: E402


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path, help="pristine Steam game directory")
    parser.add_argument("--output", required=True, type=Path, help="new directory for patched files")
    parser.add_argument("--cecil", required=True, type=Path, help="Mono.Cecil.dll path")
    parser.add_argument("--powershell", help="Windows PowerShell executable (WSL default is auto-selected)")
    args = parser.parse_args()

    baseline = args.baseline.resolve()
    output = args.output.resolve()
    cecil = args.cecil.resolve()
    if output == baseline or baseline in output.parents or output in baseline.parents:
        parser.error("--output must be separate from --baseline")
    if output.exists():
        parser.error("--output must not already exist")
    if not cecil.is_file():
        parser.error("--cecil must point to Mono.Cecil.dll")
    os.environ["HIGHROOK_CECIL_PATH"] = str(cecil)
    if args.powershell:
        os.environ["HIGHROOK_POWERSHELL"] = args.powershell

    translations = read_json(ROOT / "localization/translations/i05.json")
    inventory = read_json(ROOT / "work/stage1/file-inventory.json")
    image_doc = read_json(ROOT / "work/stage8/art/encoded-assets.json")
    images = {}
    for entry in image_doc["assets"]:
        path = ROOT / entry["path"]
        images[entry["resource_id"]] = {**entry, "png": path.read_bytes()}
    font = (ROOT / "work/stage8/font-readability/HighrookKorean-Medium.ttf").read_bytes()
    reference = read_json(ROOT / "package-reference.json")

    with tempfile.TemporaryDirectory(prefix="highrook-source-build-") as scratch:
        files, manifest = build.build_full(
            baseline,
            inventory,
            translations,
            mode="test",
            image_assets=images,
            font_bytes=font,
            save_policy="persistent_user_data",
            scratch_dir=Path(scratch),
        )
        expected = {row["path"]: row["output_sha256"] for row in reference["files"]}
        actual = {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}
        if actual != expected:
            raise ValueError("build differs from the pinned patch; refusing to reuse its package identity")
        build.write_outputs(output, files, manifest)
        patch = output / "highrook-ko-test.patch.zip"
        pack.build_package(
            baseline, output, sorted(files), reference["metadata"], patch,
            file_modes={row["path"]: (row["input_mode"], row["mode"]) for row in reference["files"]},
        )
        if pack.sha256_file(patch) != reference["package_sha256"]:
            raise ValueError("rebuilt patch ZIP differs from the pinned package")
    print(json.dumps({"output": str(output), "build_id": manifest["build_id"], "ids": manifest["processed_ids"], "files": len(files), "patch_sha256": reference["package_sha256"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
