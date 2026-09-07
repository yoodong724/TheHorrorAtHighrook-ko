#!/usr/bin/env python3
"""Hash-locked, in-memory reinsertion helpers for Highrook's core corpus.

The module never writes game files.  ``inject_core`` returns complete file bytes
to its caller, which remains responsible for selecting an output copy and
performing the writes.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CORPUS = ROOT / "localization/corpus"
RESOURCE_PATH = "TheHorrorAtHighrook_Data/resources.assets"
RESOURCE_TEXT_PATH_ID = 116
TITLE_PATH = "TheHorrorAtHighrook_Data/sharedassets3.assets"
TITLE_TEXTURE_PATH_ID = 16
TITLE_SOURCE_SHA256 = "3b37352a1a47e2f4107b883819141f4f20c398d37efb7ea34ef5aeda4766581d"
TOKEN_RE = re.compile(r"<[^>]+>|\{[^{}]+\}|\r\n|\r|\n|\v")


class InjectionError(ValueError):
    """The frozen source or proposed translation violates the patch contract."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Corpus:
    source_revision: str
    segments: tuple[dict[str, Any], ...]
    by_id: dict[str, dict[str, Any]]
    sidecar: dict[str, dict[str, Any]]
    input_hashes: dict[str, str]
    resource_script_hash: str


def load_corpus(corpus_dir: Path = DEFAULT_CORPUS) -> Corpus:
    """Load and cross-check the canonical core source, sidecar, and manifest."""
    corpus_dir = Path(corpus_dir)
    segments = tuple(
        json.loads(line)
        for line in (corpus_dir / "segments.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    )
    sidecar_doc = json.loads((corpus_dir / "locator-sidecar.json").read_text(encoding="utf-8"))
    manifest = json.loads((corpus_dir / "source-manifest.json").read_text(encoding="utf-8"))
    revisions = {row["source_revision"] for row in segments}
    revisions.update((sidecar_doc["source_revision"], manifest["source_revision"]))
    if len(revisions) != 1:
        raise InjectionError(f"mixed source revisions: {sorted(revisions)}")
    by_id: dict[str, dict[str, Any]] = {}
    for row in segments:
        segment_id = row["id"]
        if segment_id in by_id:
            raise InjectionError(f"duplicate source ID: {segment_id}")
        if sha256(row["source"]["text"].encode("utf-8")) != row["source"]["text_sha256"]:
            raise InjectionError(f"source text hash mismatch: {segment_id}")
        by_id[segment_id] = row
    entries = sidecar_doc["entries"]
    if set(entries) != set(by_id):
        raise InjectionError("source and locator ID sets differ")
    for segment_id, entry in entries.items():
        raw = entry["raw_token"]
        if sha256(raw.encode("utf-8")) != entry["raw_token_sha256"]:
            raise InjectionError(f"sidecar raw token hash mismatch: {segment_id}")
        logical = (
            raw.strip().replace('"', "").replace(r"\n", "\n").replace(r"\v", "\v")
            if entry["container_kind"] == "loose_yaml"
            else raw.strip()
        )
        if logical != by_id[segment_id]["source"]["text"]:
            raise InjectionError(f"sidecar logical decode mismatch: {segment_id}")
    input_hashes: dict[str, str] = {}
    for item in manifest["inputs"]:
        path = item["path"]
        if path in input_hashes:
            raise InjectionError(f"duplicate input manifest path: {path}")
        input_hashes[path] = item["sha256"]
    return Corpus(
        source_revision=revisions.pop(),
        segments=segments,
        by_id=by_id,
        sidecar=entries,
        input_hashes=input_hashes,
        resource_script_hash=manifest["resources_en"]["script_sha256"],
    )


def normalize_translations(
    translations: Mapping[str, str] | Iterable[Mapping[str, str]], corpus: Corpus
) -> dict[str, str]:
    """Accept an id->ko mapping or records and reject duplicate/missing IDs."""
    result: dict[str, str] = {}
    items = translations.items() if isinstance(translations, Mapping) else (
        (item["id"], item["ko"]) for item in translations
    )
    for segment_id, ko in items:
        if segment_id in result:
            raise InjectionError(f"duplicate translation ID: {segment_id}")
        if not isinstance(segment_id, str) or not isinstance(ko, str):
            raise InjectionError("translation IDs and ko values must be strings")
        result[segment_id] = ko
    missing = set(corpus.by_id) - set(result)
    unknown = set(result) - set(corpus.by_id)
    if missing or unknown:
        raise InjectionError(
            f"translation ID set mismatch: missing={len(missing)}, unknown={len(unknown)}"
        )
    for segment_id, ko in result.items():
        _validate_translation(corpus.by_id[segment_id], corpus.sidecar[segment_id], ko)
    return result


def _validate_translation(segment: dict[str, Any], entry: dict[str, Any], ko: str) -> None:
    segment_id = segment["id"]
    constraints = segment["constraints"]
    if not ko and not constraints["allow_empty"]:
        raise InjectionError(f"empty translation forbidden: {segment_id}")
    if TOKEN_RE.findall(ko) != constraints["protected_tokens"]:
        raise InjectionError(f"protected token sequence changed: {segment_id}")
    if entry["container_kind"] == "loose_yaml":
        if ":" in ko or '"' in ko or "\r" in ko:
            raise InjectionError(f"YAML translation contains unrepresentable colon/quote/CR: {segment_id}")
        if "\\n" in ko or "\\v" in ko:
            raise InjectionError(f"YAML translation contains ambiguous literal escape: {segment_id}")
    elif entry["container_kind"] == "unity_textasset":
        if ko != ko.strip() or any(char in ko for char in "\r\n\v"):
            raise InjectionError(f"resources-en translation is not one trimmed row: {segment_id}")
    else:
        raise InjectionError(f"unsupported container kind: {entry['container_kind']}")


def _yaml_raw(ko: str, source: str, raw: str) -> str:
    if ko == source:
        return raw
    encoded = ko.replace("\n", r"\n").replace("\v", r"\v")
    left_ws = raw[: len(raw) - len(raw.lstrip())]
    right_ws = raw[len(raw.rstrip()) :]
    middle = raw[len(left_ws) : len(raw) - len(right_ws) if right_ws else None]
    left_quotes = len(middle) - len(middle.lstrip('"'))
    right_quotes = len(middle) - len(middle.rstrip('"'))
    if left_quotes + right_quotes > len(middle):
        right_quotes = max(0, len(middle) - left_quotes)
    return left_ws + ('"' * left_quotes) + encoded + ('"' * right_quotes) + right_ws


def _textasset_raw(ko: str, source: str, raw: str) -> str:
    if ko == source:
        return raw
    return raw[: len(raw) - len(raw.lstrip())] + ko + raw[len(raw.rstrip()) :]


def _replace_spans(original: bytes, patches: list[tuple[int, int, bytes, str]]) -> bytes:
    ordered = sorted(patches, key=lambda item: (item[0], item[1]))
    prior_end = -1
    for start, end, _replacement, segment_id in ordered:
        if start < 0 or end < start or end > len(original) or start < prior_end:
            raise InjectionError(f"invalid or overlapping patch span: {segment_id}")
        prior_end = end
    result = bytearray(original)
    for start, end, replacement, _segment_id in reversed(ordered):
        result[start:end] = replacement
    return bytes(result)


def _load_unitypy() -> Any:
    import UnityPy
    return UnityPy


def _object(env: Any, path_id: int) -> Any:
    matches = [obj for obj in env.objects if obj.path_id == path_id]
    if len(matches) != 1:
        raise InjectionError(f"Unity object path ID {path_id} matched {len(matches)} times")
    return matches[0]


def _load_font_patch() -> Any:
    path = ROOT / "work/stage2/font/font_patch.py"
    spec = importlib.util.spec_from_file_location("highrook_stage2_font_patch", path)
    if spec is None or spec.loader is None:
        raise InjectionError("cannot load reviewed font patch module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _patch_resources(
    source_bytes: bytes,
    patches: list[tuple[int, int, bytes, str]],
    corpus: Corpus,
    font_bytes: bytes | None,
) -> tuple[bytes, dict[str, Any]]:
    UnityPy = _load_unitypy()
    env = UnityPy.load(source_bytes)
    text = _object(env, RESOURCE_TEXT_PATH_ID).read()
    if text.m_Name != "resources-en":
        raise InjectionError(f"unexpected TextAsset name: {text.m_Name!r}")
    script_bytes = text.m_Script.encode("utf-8")
    if sha256(script_bytes) != corpus.resource_script_hash:
        raise InjectionError("resources-en script hash mismatch")
    for start, end, _replacement, segment_id in patches:
        raw = corpus.sidecar[segment_id]["raw_token"].encode("utf-8")
        if script_bytes[start:end] != raw or sha256(raw) != corpus.sidecar[segment_id]["raw_token_sha256"]:
            raise InjectionError(f"locator/raw token mismatch: {segment_id}")
    rebuilt = _replace_spans(script_bytes, patches)
    text.m_Script = rebuilt.decode("utf-8")
    text.save()
    font_report = None
    if font_bytes is not None:
        font_report = _load_font_patch().patch_korean_font(env, font_bytes)
    output = env.file.save()
    check_env = UnityPy.load(output)
    check_text = _object(check_env, RESOURCE_TEXT_PATH_ID).read()
    if check_text.m_Script.encode("utf-8") != rebuilt:
        raise InjectionError("resources-en changed during Unity serialization")
    return output, {"script_sha256": sha256(rebuilt), "font": font_report}


def patch_title(source_bytes: bytes, mask_png: bytes) -> tuple[bytes, dict[str, Any]]:
    """Optionally apply the already-reviewed white RGB/alpha title-mask recipe."""
    from PIL import Image
    if sha256(source_bytes) != TITLE_SOURCE_SHA256:
        raise InjectionError("title asset source hash mismatch")
    UnityPy = _load_unitypy()
    env = UnityPy.load(source_bytes)
    before = {obj.path_id: obj.get_raw_data() for obj in env.objects}
    texture = _object(env, TITLE_TEXTURE_PATH_ID).read()
    if texture.m_Name != "testtitlePSD" or (texture.m_Width, texture.m_Height) != (1920, 1080):
        raise InjectionError("unexpected title texture identity or dimensions")
    mask = Image.open(io.BytesIO(mask_png)).convert("L").resize((1920, 1080), Image.Resampling.LANCZOS)
    rgba = Image.new("RGBA", (1920, 1080), (255, 255, 255, 255))
    rgba.putalpha(mask)
    texture.set_image(rgba, target_format=texture.m_TextureFormat, mipmap_count=1)
    texture.save()
    output = env.file.save()
    after_env = UnityPy.load(output)
    after = {obj.path_id: obj.get_raw_data() for obj in after_env.objects}
    changed = sorted(path_id for path_id in before if before[path_id] != after[path_id])
    if changed != [TITLE_TEXTURE_PATH_ID]:
        raise InjectionError(f"unexpected title asset changes: {changed}")
    reloaded = _object(after_env, TITLE_TEXTURE_PATH_ID).read()
    if reloaded.m_StreamData.path or reloaded.image.tobytes() != rgba.tobytes():
        raise InjectionError("title texture reload verification failed")
    return output, {"changed_object_ids": changed, "rgba_sha256": sha256(rgba.tobytes())}


def inject_core(
    baseline: Path,
    translations: Mapping[str, str] | Iterable[Mapping[str, str]],
    *,
    source_revision: str,
    corpus_dir: Path = DEFAULT_CORPUS,
    font_bytes: bytes | None = None,
    title_mask_png: bytes | None = None,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Build every core container that has an extracted display segment."""
    baseline = Path(baseline)
    corpus = load_corpus(corpus_dir)
    if source_revision != corpus.source_revision:
        raise InjectionError(
            f"source revision mismatch: got {source_revision!r}, expected {corpus.source_revision!r}"
        )
    values = normalize_translations(translations, corpus)
    source_files: dict[str, bytes] = {}
    for path, expected_hash in corpus.input_hashes.items():
        data = (baseline / path).read_bytes()
        if sha256(data) != expected_hash:
            raise InjectionError(f"baseline source hash mismatch: {path}")
        source_files[path] = data
    grouped: dict[str, list[tuple[int, int, bytes, str]]] = defaultdict(list)
    changed_ids = 0
    for segment_id, segment in corpus.by_id.items():
        entry = corpus.sidecar[segment_id]
        source = segment["source"]["text"]
        ko = values[segment_id]
        changed_ids += ko != source
        if entry["container_kind"] == "loose_yaml":
            start, end = entry["token_byte_span"]
            replacement = _yaml_raw(ko, source, entry["raw_token"])
        else:
            start, end = entry["token_byte_span_in_script"]
            replacement = _textasset_raw(ko, source, entry["raw_token"])
        grouped[entry["container"]].append((start, end, replacement.encode("utf-8"), segment_id))

    output: dict[str, bytes] = {}
    resource_report = None
    for path, patches in grouped.items():
        if path not in corpus.input_hashes:
            raise InjectionError(f"container absent from source manifest: {path}")
        original = source_files[path]
        if path == RESOURCE_PATH:
            output[path], resource_report = _patch_resources(original, patches, corpus, font_bytes)
        else:
            for start, end, _replacement, segment_id in patches:
                raw = corpus.sidecar[segment_id]["raw_token"].encode("utf-8")
                if original[start:end] != raw or sha256(raw) != corpus.sidecar[segment_id]["raw_token_sha256"]:
                    raise InjectionError(f"locator/raw token mismatch: {segment_id}")
            output[path] = _replace_spans(original, patches)

    title_report = None
    if title_mask_png is not None:
        title_source = (baseline / TITLE_PATH).read_bytes()
        output[TITLE_PATH], title_report = patch_title(title_source, title_mask_png)
    if len(corpus.by_id) != 2478:
        raise InjectionError(f"unexpected frozen core size: containers={len(grouped)}, IDs={len(corpus.by_id)}")
    return output, {
        "source_revision": corpus.source_revision,
        "processed_ids": len(corpus.by_id),
        "changed_ids": changed_ids,
        "containers": len(output),
        "verified_inputs": len(source_files),
        "resources": resource_report,
        "title": title_report,
        "runtime_verified": False,
    }


def source_translations(corpus: Corpus) -> dict[str, str]:
    """Return the exact logical source mapping for strict no-change tests."""
    return {segment_id: row["source"]["text"] for segment_id, row in corpus.by_id.items()}
