#!/usr/bin/env python3
"""Display-only mapping for raw hour-tick stat identifiers."""
from __future__ import annotations

import base64
import hashlib
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SOURCE_ASSEMBLY_SHA256 = "ac30086341952ed0179097dd6988173bf6df137990038340f32d04c41cfa542e"
STAT_UI_ID = "scene:level1:2832:m_text"
STAT_UI_SOURCE_SHA256 = "18b29181e33d556b95b58ecf51142cebaaae7962f477a20f44ff6c2182d08d43"
DISEASE_UI_ID = "ui:DiseaseA"
DISEASE_UI_SOURCE_SHA256 = "ebac393d44b1ddbf5797bf37ad9f87da90d934035263017357c53fe32a1f683e"
RAW_STATS = ("Injury", "Fatigue", "Madness", "DiseaseA")
TAG_RE = re.compile(r"<[^>]+>")
LINE_RE = re.compile(r"<color=[^>]+>([^:<>{}\r\n]+?)\s*:\s*[^<]*</color><BR>")


class StatDisplayError(ValueError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stat_labels(source: str, translated: str, disease_source: str, disease_translated: str) -> dict[str, str]:
    """Extract translated labels by position from the frozen stat UI fixture."""
    if sha256(source.encode("utf-8")) != STAT_UI_SOURCE_SHA256:
        raise StatDisplayError("stat UI source hash mismatch")
    if TAG_RE.findall(source) != TAG_RE.findall(translated):
        raise StatDisplayError("stat UI protected tag sequence mismatch")
    source_labels = [match.strip() for match in LINE_RE.findall(source)]
    target_labels = [match.strip() for match in LINE_RE.findall(translated)]
    if source_labels != ["Injury", "Fatigue", "Madness", "Hunger"] or len(target_labels) != 4 or any(not label for label in target_labels):
        raise StatDisplayError("stat UI label layout mismatch")
    if sha256(disease_source.encode("utf-8")) != DISEASE_UI_SOURCE_SHA256 or disease_source != "Diseased":
        raise StatDisplayError("DiseaseA UI source hash mismatch")
    if not disease_translated or disease_translated != disease_translated.strip() or any(character in disease_translated for character in "\r\n\v"):
        raise StatDisplayError("DiseaseA UI label layout mismatch")
    return dict(zip(RAW_STATS, target_labels[:3] + [disease_translated]))


def _windows_path(path: Path) -> str:
    result = subprocess.run(["wslpath", "-w", str(path.resolve())], capture_output=True, text=True, check=True)
    value = result.stdout.strip()
    if not value:
        raise StatDisplayError(f"cannot convert WSL path: {path}")
    return value


def patch_stat_display(assembly: bytes, pristine_assembly: bytes, source: str, translated: str, disease_source: str, disease_translated: str, scratch_dir: Path) -> tuple[bytes, dict[str, object]]:
    """Patch only ShowCardInfo's display stack; raw identifiers remain untouched."""
    if sha256(pristine_assembly) != SOURCE_ASSEMBLY_SHA256:
        raise StatDisplayError("pristine source assembly hash mismatch")
    labels = stat_labels(source, translated, disease_source, disease_translated)
    if all(raw == label for raw, label in labels.items()):
        return assembly, {"applied": False, "byte_identical": True, "labels_reused": list(RAW_STATS)}
    scratch = Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)
    input_path, output_path, pristine_path, map_path = scratch / "stat-input.dll", scratch / "stat-output.dll", scratch / "stat-pristine.dll", scratch / "stat-map.tsv"
    input_path.write_bytes(assembly)
    pristine_path.write_bytes(pristine_assembly)
    encoded = []
    for raw in RAW_STATS:
        encoded.append(base64.b64encode(raw.encode()).decode() + "\t" + base64.b64encode(labels[raw].encode()).decode())
    map_path.write_text("\n".join(encoded) + "\n", encoding="ascii")
    command = [
        os.environ.get("HIGHROOK_POWERSHELL", "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"), "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", _windows_path(Path(__file__).with_suffix(".ps1")),
        "-InputPath", _windows_path(input_path), "-OutputPath", _windows_path(output_path),
        "-PristinePath", _windows_path(pristine_path),
        "-MapTsv", _windows_path(map_path), "-CecilPath", _windows_path(Path(os.environ["HIGHROOK_CECIL_PATH"])),
    ]
    completed = subprocess.run(command, capture_output=True)
    stdout = completed.stdout.decode("utf-8", errors="replace").strip()
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    if completed.returncode:
        raise StatDisplayError("Mono.Cecil stat display patch failed: " + (stderr or stdout))
    output = output_path.read_bytes()
    if output == assembly:
        raise StatDisplayError("stat display patch made no assembly change")
    return output, {"applied": True, "byte_identical": False, "labels_reused": list(RAW_STATS), "cecil": stdout}
