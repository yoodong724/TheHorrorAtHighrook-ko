#!/usr/bin/env python3
"""Hash-locked in-memory reinsertion for Stage 3 extra Unity/IL facts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FACTS = ROOT / "work/stage3/extra/facts.json"
DATA_PREFIX = "TheHorrorAtHighrook_Data/"
ASSEMBLY_REL = DATA_PREFIX + "Managed/Assembly-CSharp.dll"
SOURCE_REVISION = "sha256:c60ced6544a45a98745ca6ebcede78326382713f1a0781e8eca8bcd6dc27efd4"
FIXED_SCRATCH = ROOT / "work/stage3/extra_adapter/test-output"
TITLE_DEBUG_SCRATCH = ROOT / "work/stage8/title-debug/test-output/adapter"
PATCHABLE_CLASSES = {"literal_display_candidate", "inactive_literal_candidate"}
PROTECTED_RE = re.compile(r"<[^>]+>|\{[^{}]+\}|\[[^\[\]]+\]|\r\n|\r|\n|\v|\t")


class ExtraInjectionError(ValueError):
    """A frozen source, locator, or translation violated the adapter contract."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_unitypy() -> Any:
    import UnityPy
    return UnityPy


def _aligned_end(offset: int, size: int) -> int:
    return (offset + 4 + size + 3) & ~3


def _read_string(raw: bytes, offset: int) -> tuple[str, int]:
    if offset < 0 or offset + 4 > len(raw):
        raise ExtraInjectionError("Unity string length is outside the object")
    size = struct.unpack_from("<I", raw, offset)[0]
    end = offset + 4 + size
    padded = _aligned_end(offset, size)
    if end > len(raw) or padded > len(raw) or any(raw[end:padded]):
        raise ExtraInjectionError("invalid Unity aligned string")
    try:
        return raw[offset + 4:end].decode("utf-8", errors="strict"), padded
    except UnicodeDecodeError as exc:
        raise ExtraInjectionError("invalid UTF-8 in Unity string") from exc


def _encoded_string(text: str) -> bytes:
    encoded = text.encode("utf-8", errors="strict")
    field = struct.pack("<I", len(encoded)) + encoded
    return field + b"\0" * ((-len(field)) & 3)


def _mono_base(raw: bytes) -> tuple[int, int]:
    if len(raw) < 32:
        raise ExtraInjectionError("short MonoBehaviour object")
    return struct.unpack_from("<q", raw, 4)[0], struct.unpack_from("<q", raw, 20)[0]


def _edge_whitespace(text: str) -> tuple[str, str]:
    left = re.match(r"^\s*", text).group(0)
    right = re.search(r"\s*$", text).group(0)
    return left, right


def _validate_translation(fact: dict[str, Any], ko: str) -> None:
    fact_id = fact["id"]
    if not isinstance(ko, str):
        raise ExtraInjectionError(f"translation is not a string: {fact_id}")
    source = fact["source"]
    if not ko and source:
        raise ExtraInjectionError(f"empty translation forbidden: {fact_id}")
    if PROTECTED_RE.findall(ko) != PROTECTED_RE.findall(source):
        raise ExtraInjectionError(f"protected tag/control/format token changed: {fact_id}")
    if _edge_whitespace(ko) != _edge_whitespace(source):
        # Verified display-only helper pads English labels to eight characters.
        # Its four Korean labels use two ASCII alignment spaces, reviewed in i05.
        # Keep the strict edge guard for every other location and whitespace kind.
        padded_labels = {
            "il:C_Utilities:GetStringNameForAttribute:5:to:28": "Injury  ",
            "il:C_Utilities:GetStringNameForAttribute:10:to:28": "Fatigue ",
            "il:C_Utilities:GetStringNameForAttribute:15:to:28": "Madness ",
            "il:C_Utilities:GetStringNameForAttribute:20:to:28": "Hunger  ",
        }
        permitted_padding = (
            padded_labels.get(fact_id) == source
            and fact.get("locator", {}).get("kind") == "managed_il_ldstr"
            and _edge_whitespace(ko) == ("", "  ")
        )
        if not permitted_padding:
            raise ExtraInjectionError(f"leading/trailing whitespace changed: {fact_id}")
    source_controls = [c for c in source if ord(c) < 32 and c not in "\t\n\r\v"]
    ko_controls = [c for c in ko if ord(c) < 32 and c not in "\t\n\r\v"]
    if ko_controls != source_controls:
        raise ExtraInjectionError(f"control characters changed: {fact_id}")


def load_patchable_facts(path: Path = DEFAULT_FACTS) -> tuple[str, dict[str, dict[str, Any]]]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("source_revision") != SOURCE_REVISION:
        raise ExtraInjectionError("extra facts source revision mismatch")
    selected: dict[str, dict[str, Any]] = {}
    for fact in doc.get("entries", []):
        if fact.get("classification") not in PATCHABLE_CLASSES:
            continue
        fact_id = fact.get("id")
        if not isinstance(fact_id, str) or fact_id in selected:
            raise ExtraInjectionError(f"invalid or duplicate fact ID: {fact_id!r}")
        source = fact.get("source")
        if not isinstance(source, str) or sha256(source.encode("utf-8")) != fact.get("source_sha256"):
            raise ExtraInjectionError(f"fact source hash mismatch: {fact_id}")
        locator = fact.get("locator", {})
        if locator.get("kind") not in {"unity_mono_behaviour_string", "managed_il_ldstr"}:
            raise ExtraInjectionError(f"unsupported patchable locator: {fact_id}")
        selected[fact_id] = fact
    unity_count = sum(x["locator"]["kind"] == "unity_mono_behaviour_string" for x in selected.values())
    il_count = sum(x["locator"]["kind"] == "managed_il_ldstr" for x in selected.values())
    if (unity_count, il_count) != (192, 4):
        raise ExtraInjectionError(f"unexpected frozen patchable fact counts: unity={unity_count}, il={il_count}")
    return doc["source_revision"], selected


def normalize_translations(
    translations: Mapping[str, str] | Iterable[Mapping[str, str]],
    facts: Mapping[str, dict[str, Any]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    items = translations.items() if isinstance(translations, Mapping) else (
        (item["id"], item["ko"]) for item in translations
    )
    for fact_id, ko in items:
        if fact_id in result:
            raise ExtraInjectionError(f"duplicate translation ID: {fact_id}")
        if not isinstance(fact_id, str):
            raise ExtraInjectionError("translation ID is not a string")
        result[fact_id] = ko
    missing = set(facts) - set(result)
    unknown = set(result) - set(facts)
    if missing or unknown:
        raise ExtraInjectionError(
            f"translation ID set mismatch: missing={len(missing)}, unknown={len(unknown)}"
        )
    for fact_id, ko in result.items():
        _validate_translation(facts[fact_id], ko)
    return result


def _patch_unity_container(
    source: bytes, facts: list[dict[str, Any]], values: Mapping[str, str]
) -> tuple[bytes, dict[str, Any]]:
    container_hashes = {x["locator"]["container_sha256"] for x in facts}
    if len(container_hashes) != 1 or sha256(source) != next(iter(container_hashes)):
        raise ExtraInjectionError("Unity container hash mismatch")
    changed = [x for x in facts if values[x["id"]] != x["source"]]
    if not changed:
        return source, {"changed_ids": 0, "changed_object_ids": [], "byte_identical": True}
    UnityPy = _load_unitypy()
    env = UnityPy.load(source)
    objects = {obj.path_id: obj for obj in env.objects}
    before = {path_id: obj.get_raw_data() for path_id, obj in objects.items()}
    by_object: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        by_object[fact["locator"]["path_id"]].append(fact)
    expected_raw: dict[int, bytes] = {}
    expected_offsets: dict[str, int] = {}
    for path_id, object_facts in by_object.items():
        if path_id not in objects or objects[path_id].type.name != "MonoBehaviour":
            raise ExtraInjectionError(f"MonoBehaviour locator mismatch: {path_id}")
        original = before[path_id]
        object_hashes = {x["locator"]["object_sha256"] for x in object_facts}
        if len(object_hashes) != 1 or sha256(original) != next(iter(object_hashes)):
            raise ExtraInjectionError(f"Unity object hash mismatch: {path_id}")
        go, script = _mono_base(original)
        for fact in object_facts:
            loc = fact["locator"]
            if (go, script) != (loc["game_object_path_id"], loc["script_path_id"]):
                raise ExtraInjectionError(f"script/GameObject locator mismatch: {fact['id']}")
            actual, end = _read_string(original, loc["field_offset"])
            if actual != fact["source"] or end - loc["field_offset"] != loc["field_encoded_size"]:
                raise ExtraInjectionError(f"Unity field locator/source mismatch: {fact['id']}")
        raw = original
        prior_offsets: list[tuple[int, int]] = []
        for fact in sorted(object_facts, key=lambda x: x["locator"]["field_offset"]):
            loc = fact["locator"]
            expected_offsets[fact["id"]] = loc["field_offset"] + sum(delta for _, delta in prior_offsets)
            replacement_size = len(_encoded_string(values[fact["id"]]))
            prior_offsets.append((loc["field_offset"], replacement_size - loc["field_encoded_size"]))
        for fact in sorted(object_facts, key=lambda x: x["locator"]["field_offset"], reverse=True):
            loc = fact["locator"]
            replacement = _encoded_string(values[fact["id"]])
            raw = raw[:loc["field_offset"]] + replacement + raw[loc["field_offset"] + loc["field_encoded_size"]:]
        if raw != original:
            objects[path_id].set_raw_data(raw)
            expected_raw[path_id] = raw
    output = env.file.save()
    check = UnityPy.load(output)
    after = {obj.path_id: obj.get_raw_data() for obj in check.objects}
    if set(after) != set(before):
        raise ExtraInjectionError("Unity object ID set changed during serialization")
    changed_objects = sorted(path_id for path_id in before if before[path_id] != after[path_id])
    if changed_objects != sorted(expected_raw):
        raise ExtraInjectionError(f"unexpected Unity object changes: {changed_objects}")
    for path_id, expected in expected_raw.items():
        if after[path_id] != expected:
            raise ExtraInjectionError(f"target object changed unexpectedly on reload: {path_id}")
    for fact in facts:
        actual, _ = _read_string(after[fact["locator"]["path_id"]], expected_offsets[fact["id"]])
        if actual != values[fact["id"]]:
            raise ExtraInjectionError(f"reloaded Unity field mismatch: {fact['id']}")
    return output, {
        "changed_ids": len(changed),
        "changed_object_ids": changed_objects,
        "byte_identical": output == source,
    }


def _patch_il(
    source: bytes,
    facts: list[dict[str, Any]],
    values: Mapping[str, str],
    scratch_dir: Path,
) -> tuple[bytes, dict[str, Any]]:
    assembly_hashes = {x["locator"]["assembly_sha256"] for x in facts}
    if len(assembly_hashes) != 1 or sha256(source) != next(iter(assembly_hashes)):
        raise ExtraInjectionError("managed assembly hash mismatch")
    changed = [x for x in facts if values[x["id"]] != x["source"]]
    if not changed:
        return source, {"changed_ids": 0, "byte_identical": True, "cecil_executed": False}
    scratch_dir = Path(scratch_dir)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    input_path = scratch_dir / "il-input.dll"
    output_path = scratch_dir / "il-output.dll"
    patch_path = scratch_dir / "il-patches.json"
    input_path.write_bytes(source)
    patch_path.write_text(json.dumps([
        {
            "id": x["id"], "type": x["locator"]["type"], "method": x["locator"]["method"],
            "method_token": x["locator"]["method_token"],
            "instruction_occurrence": x["locator"]["instruction_occurrence"],
            "instruction_offset": x["locator"]["instruction_offset"],
            "source": x["source"], "source_sha256": x["source_sha256"], "ko": values[x["id"]],
        } for x in changed
    ], ensure_ascii=False, indent=2), encoding="utf-8")
    def windows_path(path: Path) -> str:
        converted = subprocess.run(
            ["wslpath", "-w", str(path.resolve())], capture_output=True, text=True, check=True
        ).stdout.strip()
        if not converted:
            raise ExtraInjectionError(f"cannot convert WSL path: {path}")
        return converted

    command = [
        os.environ.get("HIGHROOK_POWERSHELL", "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"), "-NoProfile",
        "-ExecutionPolicy", "Bypass", "-File", windows_path(Path(__file__).with_name("patch_il.ps1")),
        "-InputPath", windows_path(input_path), "-OutputPath", windows_path(output_path),
        "-PatchJson", windows_path(patch_path),
        "-CecilPath", windows_path(Path(os.environ["HIGHROOK_CECIL_PATH"])),
    ]
    completed = subprocess.run(command, capture_output=True)
    def decode_output(data: bytes) -> str:
        for encoding in ("utf-8", "cp949", "utf-16-le"):
            try:
                return data.decode(encoding).strip()
            except UnicodeDecodeError:
                pass
        return data.decode("utf-8", errors="replace").strip()
    stdout = decode_output(completed.stdout)
    stderr = decode_output(completed.stderr)
    if completed.returncode:
        raise ExtraInjectionError(f"Mono.Cecil patch failed: {stderr or stdout}")
    output = output_path.read_bytes()
    verified_ids = _verify_patched_il_strings(output, changed, values)
    return output, {
        "changed_ids": len(changed), "byte_identical": output == source, "cecil_executed": True,
        "unicode_verified_ids": verified_ids, "stdout": stdout,
    }


def _verify_patched_il_strings(
    assembly: bytes,
    changed: list[dict[str, Any]],
    values: Mapping[str, str],
) -> list[str]:
    """Reload output IL and compare each target operand to the Python value.

    The Cecil helper validates against its TSV input.  That cannot detect a
    transport decode error that corrupts JSON before the TSV is produced, so
    this independent check keeps the original Python Unicode strings as the
    authority.
    """
    try:
        import dnfile
        from dncil.cil.body.reader import read_method_body_from_bytes
    except ImportError as exc:
        raise ExtraInjectionError("managed IL reload verifier is unavailable") from exc

    pe = dnfile.dnPE(data=assembly)
    verified: list[str] = []
    try:
        for fact in changed:
            loc = fact["locator"]
            types = [row for row in pe.net.mdtables.TypeDef.rows
                     if str(row.TypeName) == loc["type"]]
            if len(types) != 1:
                raise ExtraInjectionError(f"managed IL output type mismatch: {fact['id']}")
            # Cecil can renumber TypeDefOrRef tokens embedded in a signature
            # blob (ShowCardInfo changes 1280a0 -> 1280cc) while preserving the
            # semantic method.  The pristine source signature is already a
            # pre-write guard in the Cecil helper; output identity therefore
            # uses the unique declaring type/name plus stable IL occurrence.
            methods = [index.row for index in types[0].MethodList
                       if str(index.row.Name) == loc["method"] and index.row.Rva]
            if len(methods) != 1:
                raise ExtraInjectionError(f"managed IL output method mismatch: {fact['id']}")
            instructions = read_method_body_from_bytes(pe.get_data(methods[0].Rva)).instructions
            occurrence = loc["instruction_occurrence"]
            if occurrence >= len(instructions):
                raise ExtraInjectionError(f"managed IL output occurrence mismatch: {fact['id']}")
            instruction = instructions[occurrence]
            reader_base = instructions[0].offset if instructions else 0
            if (instruction.opcode.name != "ldstr"
                    or instruction.offset - reader_base != loc["instruction_offset"]):
                raise ExtraInjectionError(f"managed IL output locator mismatch: {fact['id']}")
            actual = pe.net.user_strings.get(instruction.operand.rid).value
            if actual != values[fact["id"]]:
                raise ExtraInjectionError(
                    f"managed IL output Unicode mismatch: {fact['id']}: {actual!r}"
                )
            verified.append(fact["id"])
    finally:
        pe.close()
    return sorted(verified)


def inject_extra(
    baseline: Path,
    translations: Mapping[str, str] | Iterable[Mapping[str, str]],
    *,
    source_revision: str,
    facts_path: Path = DEFAULT_FACTS,
    scratch_dir: Path | None = None,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Return complete patched container bytes without writing the game copy."""
    if source_revision != SOURCE_REVISION:
        raise ExtraInjectionError("requested source revision mismatch")
    frozen_revision, facts = load_patchable_facts(facts_path)
    if frozen_revision != source_revision:
        raise ExtraInjectionError("facts/request source revision mismatch")
    values = normalize_translations(translations, facts)
    baseline = Path(baseline)
    baseline_resolved = baseline.resolve()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in facts.values():
        loc = fact["locator"]
        rel = ASSEMBLY_REL if loc["kind"] == "managed_il_ldstr" else DATA_PREFIX + loc["container"]
        grouped[rel].append(fact)
    output: dict[str, bytes] = {}
    reports: dict[str, Any] = {}
    for rel, group in sorted(grouped.items()):
        source = (baseline / rel).read_bytes()
        if rel == ASSEMBLY_REL:
            if scratch_dir is None and any(values[x["id"]] != x["source"] for x in group):
                raise ExtraInjectionError("scratch_dir is required for a changed IL patch")
            allowed_scratch = {FIXED_SCRATCH.resolve(), TITLE_DEBUG_SCRATCH.resolve()}
            if scratch_dir is not None and Path(scratch_dir).resolve() not in allowed_scratch:
                raise ExtraInjectionError("IL scratch_dir is not an approved adapter test-output directory")
            output[rel], reports[rel] = _patch_il(source, group, values, Path(scratch_dir or "."))
        else:
            output[rel], reports[rel] = _patch_unity_container(source, group, values)
    return output, {
        "source_revision": source_revision,
        "processed_ids": len(facts),
        "changed_ids": sum(values[k] != facts[k]["source"] for k in facts),
        "unity_ids": sum(x["locator"]["kind"] == "unity_mono_behaviour_string" for x in facts.values()),
        "il_ids": sum(x["locator"]["kind"] == "managed_il_ldstr" for x in facts.values()),
        "containers": len(output),
        "files": reports,
        "runtime_verified": False,
    }


def source_translations(facts_path: Path = DEFAULT_FACTS) -> dict[str, str]:
    """Return exact source values for no-change tests."""
    return {fact_id: fact["source"] for fact_id, fact in load_patchable_facts(facts_path)[1].items()}
