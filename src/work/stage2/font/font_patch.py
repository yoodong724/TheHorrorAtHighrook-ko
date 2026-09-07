#!/usr/bin/env python3
"""Strict, in-memory Korean font patch for Highrook ``resources.assets``.

The public ``patch_korean_font`` function mutates only the supplied UnityPy
environment.  It never calls ``env.file.save()`` and never writes a file.  The
caller is the sole owner of full serialization and installation.

This implementation is intentionally pinned to the inspected Steam build.  In
particular, the raw TMP objects must match the known hashes or the function
fails before changing anything.  TMP_Settings path 189 is decoded field by
field in the order declared by the inspected Unity.TextMeshPro.dll; no byte
pattern search or guessed offset is used.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RESOURCE_SHA256 = "5192258df88b9cbc8aaf8f1b8be79c1baf99eb27c32bbf3aa14fcca8a55ce199"
TMP_DLL_SHA256 = "b027292d5ca1dfeaa3e5c2cf928d0924e4b09e6bd47f63e374938f435534e6b1"
FONT120_RAW_SHA256 = "8b2c2e4a2eb5b30370e03eea1642085447301211ecfcdb4d824d4d0eea6a835a"
FONT120_DATA_SHA256 = "e5b0af421ea2bfbc1ac8d251d647268087ae82786234c57f757d1f0b90fa8b49"
TMP185_RAW_SHA256 = "1293965d943a4e4dd52677b47649c84ecbfadab704f1abd8d9af0eb79db8942e"
TMP185_MULTI_ATLAS_RAW_SHA256 = "44712b0fbac2015ff7eabfa8f4e841250ac1d5ae1e11a5d91fb6443a0abf02fc"
TMP189_RAW_SHA256 = "2acf1a6ac4c71124fe9e435bd63a3330bd83a1b6b0813cbdf3d8c56d493c2a1d"
TMP189_PATCHED_RAW_SHA256 = "63e214c2cb1ea91bcce1de98a681897e176753c714876201a32381835a5fee55"
KOREAN_FONT_SHA256 = "137be611e95985ebffe2eeba475aa514e43882b397ea253e07fbb4dc3d4c7179"
MEDIUM_FONT_SHA256 = "16190cc5eb13aac84a65935b393be99eb73fefb365f9311a1263fcc8dd765af6"
KNOWN_KOREAN_FONT_HASHES = frozenset((KOREAN_FONT_SHA256, MEDIUM_FONT_SHA256))

FONT_PATH_ID = 120
DYNAMIC_FALLBACK_PATH_ID = 185
TMP_SETTINGS_PATH_ID = 189
TMP_FONT_ASSET_SCRIPT_PATH_ID = 319
TMP_SETTINGS_SCRIPT_PATH_ID = 102


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class PPtr:
    file_id: int
    path_id: int


class _Reader:
    """Little-endian reader for the exact serialized TMP layout in this build."""

    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def _take(self, size: int) -> bytes:
        end = self.offset + size
        if end > len(self.data):
            raise ValueError(f"truncated TMP object at 0x{self.offset:x}")
        result = self.data[self.offset:end]
        self.offset = end
        return result

    def u32(self) -> int:
        return struct.unpack("<I", self._take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self._take(4))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self._take(4))[0]

    def boolean(self) -> bool:
        value = self.u32()
        if value not in (0, 1):
            raise ValueError(f"invalid aligned boolean {value} at 0x{self.offset - 4:x}")
        return bool(value)

    def pptr(self) -> PPtr:
        file_id, path_id = struct.unpack("<Iq", self._take(12))
        return PPtr(file_id, path_id)

    def string(self) -> str:
        size = self.u32()
        raw = self._take(size)
        try:
            result = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"invalid UTF-8 string at 0x{self.offset - size:x}") from exc
        aligned = (self.offset + 3) & ~3
        padding = self._take(aligned - self.offset)
        if any(padding):
            raise ValueError("non-zero aligned-string padding")
        return result

    def finish(self) -> None:
        if self.offset != len(self.data):
            raise ValueError(
                f"TMP layout consumed {self.offset} of {len(self.data)} bytes"
            )


def _mono_header(reader: _Reader, expected_script: int, expected_name: str) -> dict[str, Any]:
    game_object = reader.pptr()
    enabled = reader.boolean()
    script = reader.pptr()
    name = reader.string()
    if game_object != PPtr(0, 0):
        raise ValueError(f"unexpected MonoBehaviour GameObject ref: {game_object}")
    if not enabled:
        raise ValueError("target MonoBehaviour is disabled")
    if script != PPtr(1, expected_script):
        raise ValueError(f"unexpected MonoBehaviour script ref: {script}")
    if name != expected_name:
        raise ValueError(f"unexpected MonoBehaviour name: {name!r}")
    return {"game_object": game_object, "enabled": enabled, "script": script, "name": name}


def parse_dynamic_fallback(raw: bytes) -> dict[str, Any]:
    """Decode the validated header of TMP_FontAsset 185.

    The field order through ``style_name`` is the order declared in
    Unity.TextMeshPro.dll.  The remaining large tables are kept opaque because
    this patch does not edit them; their integrity is instead pinned by the
    complete-object SHA-256.
    """
    if _sha256(raw) not in (TMP185_RAW_SHA256, TMP185_MULTI_ATLAS_RAW_SHA256):
        raise ValueError("TMP FontAsset 185 raw hash does not match the inspected build")
    reader = _Reader(raw)
    header = _mono_header(
        reader, TMP_FONT_ASSET_SCRIPT_PATH_ID, "LiberationSans SDF - Fallback"
    )
    header.update(
        {
            "name_hash": reader.u32(),
            "material": reader.pptr(),
            "asset_hash": reader.u32(),
            "version": reader.string(),
            "source_font_guid": reader.string(),
            "source_font": reader.pptr(),
            "atlas_population_mode": reader.i32(),
            "face_index": reader.i32(),
            "family_name": reader.string(),
            "style_name": reader.string(),
        }
    )
    header.update(
        {
            "face_point_size": reader.i32(),
            "face_scale": reader.f32(),
            "face_line_height": reader.f32(),
            "face_ascent_line": reader.f32(),
            "face_cap_line": reader.f32(),
            "face_mean_line": reader.f32(),
            "face_baseline": reader.f32(),
            "face_descent_line": reader.f32(),
            "face_superscript_offset": reader.f32(),
            "face_superscript_size": reader.f32(),
            "face_subscript_offset": reader.f32(),
            "face_subscript_size": reader.f32(),
            "face_underline_offset": reader.f32(),
            "face_underline_thickness": reader.f32(),
            "face_strikethrough_offset": reader.f32(),
            "face_strikethrough_thickness": reader.f32(),
            "face_tab_width": reader.f32(),
        }
    )
    header["glyph_table_count"] = reader.u32()
    header["character_table_count"] = reader.u32()
    atlas_texture_count = reader.u32()
    header["atlas_textures"] = [reader.pptr() for _ in range(atlas_texture_count)]
    header["atlas_texture_index"] = reader.i32()
    header["multi_atlas_offset"] = reader.offset
    header["is_multi_atlas_textures_enabled"] = reader.boolean()
    header["clear_dynamic_data_offset"] = reader.offset
    header["clear_dynamic_data_on_build"] = reader.boolean()
    header["validated_prefix_size"] = reader.offset
    if header["source_font"] != PPtr(0, FONT_PATH_ID):
        raise ValueError(f"TMP FontAsset 185 has unexpected source font: {header['source_font']}")
    if header["atlas_population_mode"] != 1:
        raise ValueError("TMP FontAsset 185 is not dynamic (population mode 1)")
    if header["version"] != "1.1.0" or header["face_index"] != 0:
        raise ValueError("TMP FontAsset 185 version/face index changed")
    expected = {
        "glyph_table_count": 0,
        "character_table_count": 0,
        "atlas_textures": [PPtr(0, 107)],
        "atlas_texture_index": 0,
        "multi_atlas_offset": 0x118,
        "clear_dynamic_data_offset": 0x11C,
        "clear_dynamic_data_on_build": True,
        "validated_prefix_size": 0x120,
    }
    for key, value in expected.items():
        if header[key] != value:
            raise ValueError(f"TMP FontAsset 185 field {key} changed: {header[key]!r}")
    return header


def _fallback_with_multi_atlas(raw: bytes) -> bytes:
    parsed = parse_dynamic_fallback(raw)
    if parsed["is_multi_atlas_textures_enabled"]:
        if _sha256(raw) != TMP185_MULTI_ATLAS_RAW_SHA256:
            raise ValueError("TMP FontAsset 185 has multi-atlas enabled but unknown remaining bytes")
        return raw
    if _sha256(raw) != TMP185_RAW_SHA256:
        raise ValueError("TMP FontAsset 185 source bytes are not the reviewed baseline")
    patched = bytearray(raw)
    struct.pack_into("<I", patched, parsed["multi_atlas_offset"], 1)
    result = bytes(patched)
    reparsed = parse_dynamic_fallback(result)
    if not reparsed["is_multi_atlas_textures_enabled"]:
        raise AssertionError("internal TMP multi-atlas serialization failure")
    if _sha256(result) != TMP185_MULTI_ATLAS_RAW_SHA256:
        raise AssertionError("patched TMP FontAsset 185 bytes differ from reviewed layout")
    return result


def parse_tmp_settings(raw: bytes) -> dict[str, Any]:
    """Decode all serialized fields of TMP_Settings 189 for this DLL/build."""
    reader = _Reader(raw)
    result = _mono_header(reader, TMP_SETTINGS_SCRIPT_PATH_ID, "TMP Settings")
    result.update(
        {
            "enable_word_wrapping": reader.boolean(),
            "enable_kerning": reader.boolean(),
            "enable_extra_padding": reader.boolean(),
            "enable_tint_all_sprites": reader.boolean(),
            "enable_parse_escape_characters": reader.boolean(),
            "enable_raycast_target": reader.boolean(),
            "get_font_features_at_runtime": reader.boolean(),
            "missing_glyph_character": reader.u32(),
            "warnings_disabled": reader.boolean(),
            "default_font_asset": reader.pptr(),
            "default_font_asset_path": reader.string(),
            "default_font_size": reader.f32(),
            "default_auto_size_min_ratio": reader.f32(),
            "default_auto_size_max_ratio": reader.f32(),
            "default_text_container_size": (reader.f32(), reader.f32()),
            "default_ui_text_container_size": (reader.f32(), reader.f32()),
            "auto_size_text_container": reader.boolean(),
            "is_text_object_scale_static": reader.boolean(),
        }
    )
    fallback_count_offset = reader.offset
    fallback_count = reader.u32()
    if fallback_count > 4096:
        raise ValueError(f"unreasonable TMP fallback count: {fallback_count}")
    fallback_data_offset = reader.offset
    fallbacks = [reader.pptr() for _ in range(fallback_count)]
    result.update(
        {
            "fallback_count_offset": fallback_count_offset,
            "fallback_data_offset": fallback_data_offset,
            "fallback_font_assets": fallbacks,
            "match_material_preset": reader.boolean(),
            "default_sprite_asset": reader.pptr(),
            "default_sprite_asset_path": reader.string(),
            "enable_emoji_support": reader.boolean(),
            "missing_character_sprite_unicode": reader.u32(),
            "default_color_gradient_presets_path": reader.string(),
            "default_style_sheet": reader.pptr(),
            "style_sheets_resource_path": reader.string(),
            "leading_characters": reader.pptr(),
            "following_characters": reader.pptr(),
            "use_modern_hangul_line_breaking_rules": reader.boolean(),
        }
    )
    reader.finish()

    expected = {
        "default_font_asset": PPtr(0, 186),
        "default_sprite_asset": PPtr(0, 190),
        "default_style_sheet": PPtr(0, 188),
        "leading_characters": PPtr(0, 117),
        "following_characters": PPtr(0, 118),
        "default_font_asset_path": "Fonts & Materials/",
        "default_sprite_asset_path": "Sprite Assets/",
        "default_color_gradient_presets_path": "Color Gradient Presets/",
        "style_sheets_resource_path": "",
    }
    for key, value in expected.items():
        if result[key] != value:
            raise ValueError(f"TMP Settings field {key} changed: {result[key]!r}")
    if result["fallback_count_offset"] != 0x98:
        raise ValueError(
            f"derived fallback count offset changed: 0x{result['fallback_count_offset']:x}"
        )
    return result


def _get_object(env: Any, path_id: int, expected_type: str) -> Any:
    matches = [obj for obj in env.objects if obj.path_id == path_id]
    if len(matches) != 1:
        raise ValueError(f"expected one object at path ID {path_id}, found {len(matches)}")
    obj = matches[0]
    if obj.type.name != expected_type:
        raise ValueError(
            f"path ID {path_id} type is {obj.type.name}, expected {expected_type}"
        )
    return obj


def validate_korean_font(font_bytes: bytes) -> dict[str, Any]:
    """Validate the pinned, licensed PoC font and its dynamic glyph coverage."""
    if _sha256(font_bytes) not in KNOWN_KOREAN_FONT_HASHES:
        raise ValueError("Korean font hash does not match the approved vendor asset")
    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(font_bytes), lazy=False)
    codepoints: set[int] = set()
    for table in font["cmap"].tables:
        if table.isUnicode():
            codepoints.update(table.cmap)
    hangul_syllables = sum(0xAC00 <= cp <= 0xD7A3 for cp in codepoints)
    hangul_jamo = sum(0x1100 <= cp <= 0x11FF for cp in codepoints)
    compatibility_jamo = sum(0x3130 <= cp <= 0x318F for cp in codepoints)
    if hangul_syllables != 11172:
        raise ValueError(f"font has only {hangul_syllables}/11172 Hangul syllables")
    return {
        "sha256": _sha256(font_bytes),
        "bytes": len(font_bytes),
        "unicode_codepoints": len(codepoints),
        "hangul_syllables": hangul_syllables,
        "hangul_jamo": hangul_jamo,
        "hangul_compatibility_jamo": compatibility_jamo,
        "family": font["name"].getDebugName(1),
        "style": font["name"].getDebugName(2),
    }


def _settings_with_global_fallback(raw: bytes) -> bytes:
    parsed = parse_tmp_settings(raw)
    target = PPtr(0, DYNAMIC_FALLBACK_PATH_ID)
    if parsed["fallback_font_assets"] == [target]:
        if _sha256(raw) != TMP189_PATCHED_RAW_SHA256:
            raise ValueError("TMP Settings has target fallback but unknown remaining bytes")
        return raw
    if parsed["fallback_font_assets"]:
        raise ValueError("TMP Settings fallback list is unexpectedly non-empty")
    if _sha256(raw) != TMP189_RAW_SHA256:
        raise ValueError("TMP Settings 189 raw hash does not match the inspected build")

    count_offset = parsed["fallback_count_offset"]
    data_offset = parsed["fallback_data_offset"]
    patched = bytearray(raw[:data_offset])
    struct.pack_into("<I", patched, count_offset, 1)
    patched.extend(struct.pack("<Iq", target.file_id, target.path_id))
    patched.extend(raw[data_offset:])
    result = bytes(patched)
    reparsed = parse_tmp_settings(result)
    if reparsed["fallback_font_assets"] != [target]:
        raise AssertionError("internal TMP fallback serialization failure")
    if _sha256(result) != TMP189_PATCHED_RAW_SHA256:
        raise AssertionError("patched TMP Settings bytes differ from reviewed layout")
    return result


def patch_korean_font(env: Any, font_bytes: bytes) -> dict[str, Any]:
    """Mutate Font120 and TMP Settings189 in memory, without saving ``env``.

    TMP FontAsset185 is validation-only: it already points to Font120 and is in
    dynamic population mode.  Keeping path IDs unchanged preserves that source
    relationship.  TMP Settings189 gains FontAsset185 as its global fallback.
    """
    font_info = validate_korean_font(font_bytes)
    font_obj = _get_object(env, FONT_PATH_ID, "Font")
    fallback_obj = _get_object(env, DYNAMIC_FALLBACK_PATH_ID, "MonoBehaviour")
    settings_obj = _get_object(env, TMP_SETTINGS_PATH_ID, "MonoBehaviour")

    current_fallback = fallback_obj.get_raw_data()
    patched_fallback = _fallback_with_multi_atlas(current_fallback)
    current_settings = settings_obj.get_raw_data()
    patched_settings = _settings_with_global_fallback(current_settings)

    font_asset = font_obj.read()
    current_font_data = bytes(font_asset.m_FontData)
    current_font_sha = _sha256(current_font_data)
    if font_asset.m_Name != "LiberationSans":
        raise ValueError(f"Font120 name changed: {font_asset.m_Name!r}")
    if current_font_sha not in KNOWN_KOREAN_FONT_HASHES | {FONT120_DATA_SHA256}:
        raise ValueError(f"Font120 embedded data has unknown hash: {current_font_sha}")
    if current_font_sha != _sha256(font_bytes):
        if current_font_sha == FONT120_DATA_SHA256 and _sha256(font_obj.get_raw_data()) != FONT120_RAW_SHA256:
            raise ValueError("Font120 raw object hash does not match the inspected build")
        font_asset.m_FontData = font_bytes
        font_asset.save()
        font_changed = True
    else:
        font_changed = False

    settings_changed = patched_settings != current_settings
    fallback_changed = patched_fallback != current_fallback
    if fallback_changed:
        fallback_obj.set_raw_data(patched_fallback)
    if settings_changed:
        settings_obj.set_raw_data(patched_settings)

    # UnityPy stages both edits for the caller's later env.file.save().  Its
    # get_raw_data()/read() accessors continue exposing source bytes until that
    # serialization, so persistence is verified only by self_test's save/reload.
    return {
        "font_path_id": FONT_PATH_ID,
        "dynamic_fallback_path_id": DYNAMIC_FALLBACK_PATH_ID,
        "tmp_settings_path_id": TMP_SETTINGS_PATH_ID,
        "font_changed": font_changed,
        "dynamic_fallback_changed": fallback_changed,
        "multi_atlas_enabled": True,
        "settings_changed": settings_changed,
        "font": font_info,
        "dynamic_source_font": {"file_id": 0, "path_id": FONT_PATH_ID},
        "global_fallback": {"file_id": 0, "path_id": DYNAMIC_FALLBACK_PATH_ID},
    }


def self_test(resources_path: Path, font_path: Path, tmp_dll_path: Path) -> dict[str, Any]:
    """Run byte-identical no-change and patched in-memory round trips."""
    import UnityPy

    source = resources_path.read_bytes()
    dll = tmp_dll_path.read_bytes()
    if _sha256(source) != RESOURCE_SHA256:
        raise ValueError("resources.assets hash does not match the inspected build")
    if _sha256(dll) != TMP_DLL_SHA256:
        raise ValueError("Unity.TextMeshPro.dll hash does not match the inspected build")

    untouched = UnityPy.load(source)
    untouched_bytes = untouched.file.save()
    if untouched_bytes != source:
        raise AssertionError("UnityPy no-change resources.assets round trip is not byte-identical")

    env = UnityPy.load(source)
    mutation = patch_korean_font(env, font_path.read_bytes())
    patched_bytes = env.file.save()
    reloaded = UnityPy.load(patched_bytes)
    font_obj = _get_object(reloaded, FONT_PATH_ID, "Font")
    fallback_obj = _get_object(reloaded, DYNAMIC_FALLBACK_PATH_ID, "MonoBehaviour")
    settings_obj = _get_object(reloaded, TMP_SETTINGS_PATH_ID, "MonoBehaviour")
    fallback = parse_dynamic_fallback(fallback_obj.get_raw_data())
    if not fallback["is_multi_atlas_textures_enabled"]:
        raise AssertionError("patched TMP FontAsset185 multi-atlas flag failed reload verification")
    settings = parse_tmp_settings(settings_obj.get_raw_data())
    if _sha256(bytes(font_obj.read().m_FontData)) != _sha256(font_path.read_bytes()):
        raise AssertionError("patched Font120 failed reload verification")
    if settings["fallback_font_assets"] != [PPtr(0, DYNAMIC_FALLBACK_PATH_ID)]:
        raise AssertionError("patched TMP Settings failed reload verification")
    return {
        "source_sha256": _sha256(source),
        "source_size": len(source),
        "no_change_sha256": _sha256(untouched_bytes),
        "no_change_byte_identical": untouched_bytes == source,
        "patched_sha256": _sha256(patched_bytes),
        "patched_size": len(patched_bytes),
        "input_file_unchanged": _sha256(resources_path.read_bytes()) == RESOURCE_SHA256,
        "mutation": mutation,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("resources", type=Path)
    parser.add_argument("font", type=Path)
    parser.add_argument("tmp_dll", type=Path)
    args = parser.parse_args()
    print(json.dumps(self_test(args.resources, args.font, args.tmp_dll), indent=2))


if __name__ == "__main__":
    main()
