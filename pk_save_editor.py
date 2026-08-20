#!/usr/bin/env python3
r"""
pk_save_editor.py - Portal Knights Save Editor (GUI)

One self-contained program that:
  - finds every save file in Steam Cloud / local / Guest folders
    (characters, universes, and worlds — not just character slots)
  - backs them up before touching anything
  - Characters tab:
      list every character, rename safely, multi-tab Character Editor
      (Armor / Vanity / Pets / Stats / Backpack / Hotbar / Recipes / Quests)
  - Universes tab:
      list universe slots, show names from UniverseHeaderData (USHD),
      island counts from ILHD entries
  - Worlds tab:
      decode location codes (Squire's Knoll, Vacant islands, DLC rifts…),
      show which universe each world belongs to, browse chest inventories
      stored in BKCK chunks (Server Inventory Component)
  - searchable item picker backed by item_table_merged.json

File naming (16 hex digits):
  0000000000000000  — options / system (NOT characters)
  0100000000000000  — character file (all 9 slots; always this name)
  0200000000000000  — game backup of the character file
  03……………N          — universe; N = slot (cannot be remapped by rename)
  04……U0…LOC        — world; U = universe id, LOC = location code
  0600000000000000  — misc system blob

Needs: pip install zstandard
       optional: pip install python-snappy  (much faster load/save)
       (tkinter is part of standard Python on Windows / most Linux)

Run it with:   python pk_save_editor.py

Keep item_table_merged.json and (optionally) pk_dict.bin next to this
script, or let the tool auto-find / extract the dictionary from the game.
"""

import glob
import json
import os
import queue
import re
import shutil
import struct
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

# ----------------------------------------------------------------------
# Snappy (raw format) codec.
# Prefer python-snappy (C) when installed; pure-Python fallback otherwise.
# Both use the same raw Snappy block format (not framed).

_snappy_c = None
try:
    import snappy as _snappy_mod
    # Real python-snappy exposes compress/decompress. The unrelated
    # "SnapPy" topology package also occupies the name "snappy".
    if hasattr(_snappy_mod, "compress") and hasattr(_snappy_mod, "decompress"):
        _probe = _snappy_mod.compress(b"pk")
        if _snappy_mod.decompress(_probe) == b"pk":
            _snappy_c = _snappy_mod
except Exception:
    _snappy_c = None
if _snappy_c is None:
    try:
        import cramjam
        class _CramjamSnappyRaw(object):
            @staticmethod
            def compress(data):
                return bytes(cramjam.snappy.compress_raw(data))
            @staticmethod
            def decompress(data):
                return bytes(cramjam.snappy.decompress_raw(data))
        _probe = _CramjamSnappyRaw.compress(b"pk")
        if _CramjamSnappyRaw.decompress(_probe) == b"pk":
            _snappy_c = _CramjamSnappyRaw
    except Exception:
        _snappy_c = None


def snappy_decompress(buf):
    """Decompress a raw Snappy block (no framing)."""
    if _snappy_c is not None:
        try:
            return _snappy_c.decompress(buf)
        except Exception:
            pass
    return _snappy_decompress_pure(buf)


def snappy_compress(data):
    """Compress to a raw Snappy block (no framing)."""
    if _snappy_c is not None:
        try:
            return _snappy_c.compress(data)
        except Exception:
            pass
    return _snappy_compress_pure(data)


def _snappy_decompress_pure(buf):
    p = 0
    shift = 0
    ulen = 0
    while True:
        if p >= len(buf):
            raise ValueError("truncated snappy length varint")
        b = buf[p]
        p += 1
        ulen |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            break
    out = bytearray()
    n = len(buf)
    # Stop once the declared length is reached, not when the buffer runs
    # out. A well-formed snappy stream is self-terminating at ulen bytes
    # of output; anything after that (e.g. the zero-padding a fixed-size
    # BSON binary field gets when a shorter recompressed blob is written
    # back into it) is not part of the stream and must NOT be fed to the
    # decoder - a stray 0x00 byte is a valid 1-byte-literal tag, so
    # padding silently produced extra garbage output and a length
    # mismatch instead of stopping cleanly.
    while p < n and len(out) < ulen:
        tag = buf[p]
        p += 1
        t = tag & 3
        if t == 0:
            ln = tag >> 2
            if ln < 60:
                ln += 1
            else:
                extra = ln - 59
                ln = int.from_bytes(buf[p:p + extra], "little") + 1
                p += extra
            out += buf[p:p + ln]
            p += ln
        else:
            if t == 1:
                ln = 4 + ((tag >> 2) & 7)
                off = ((tag >> 5) << 8) | buf[p]
                p += 1
            elif t == 2:
                ln = (tag >> 2) + 1
                off = int.from_bytes(buf[p:p + 2], "little")
                p += 2
            else:
                ln = (tag >> 2) + 1
                off = int.from_bytes(buf[p:p + 4], "little")
                p += 4
            if off == 0 or off > len(out):
                raise ValueError("bad snappy copy offset %d" % off)
            start = len(out) - off
            for i in range(ln):
                out.append(out[start + i])
    if len(out) != ulen:
        raise ValueError("snappy length mismatch: got %d, header said %d"
                          % (len(out), ulen))
    return bytes(out)


def _put_varint(out, v):
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return


def _emit_literal(out, data, s, ln):
    n = ln - 1
    if n < 60:
        out.append(n << 2)
    elif n < 256:
        out.append(60 << 2)
        out.append(n)
    elif n < 65536:
        out.append(61 << 2)
        out += n.to_bytes(2, "little")
    elif n < 1 << 24:
        out.append(62 << 2)
        out += n.to_bytes(3, "little")
    else:
        out.append(63 << 2)
        out += n.to_bytes(4, "little")
    out += data[s:s + ln]


def _emit_copy(out, off, ln):
    while ln >= 68:
        out.append(2 | (63 << 2))
        out += off.to_bytes(2, "little")
        ln -= 64
    if ln > 64:
        out.append(2 | (59 << 2))
        out += off.to_bytes(2, "little")
        ln -= 60
    if 4 <= ln <= 11 and off < 2048:
        out.append(1 | ((ln - 4) << 2) | ((off >> 8) << 5))
        out.append(off & 0xFF)
    elif ln > 0:
        out.append(2 | ((ln - 1) << 2))
        out += off.to_bytes(2, "little")


def _snappy_compress_pure(data):
    out = bytearray()
    _put_varint(out, len(data))
    for bstart in range(0, len(data), 65536):
        _compress_block(out, data, bstart, min(bstart + 65536, len(data)))
    return bytes(out)


def _compress_block(out, data, lo, hi):
    n = hi - lo
    if n == 0:
        return
    if n < 16:
        _emit_literal(out, data, lo, n)
        return
    table = {}
    ip = lo
    next_emit = lo
    limit = hi - 4
    while ip <= limit:
        key = data[ip:ip + 4]
        cand = table.get(key, -1)
        table[key] = ip
        if cand < 0 or ip - cand >= 65536 or data[cand:cand + 4] != key:
            ip += 1
            continue
        if next_emit < ip:
            _emit_literal(out, data, next_emit, ip - next_emit)
        mlen = 4
        while (ip + mlen < hi and mlen < 64
               and data[cand + mlen] == data[ip + mlen]):
            mlen += 1
        _emit_copy(out, ip - cand, mlen)
        ip += mlen
        next_emit = ip
    if next_emit < hi:
        _emit_literal(out, data, next_emit, hi - next_emit)


# ----------------------------------------------------------------------
# save file format (KSC1 container -> zstd -> SNPY/snappy -> BSON)

MAGIC = b"KSC1"
HEADER_SIZE = 24
ENTRY_SIZE = 12
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
SNPY_MAGIC = b"SNPY"
NAME_FIELD_SIZE = 128
GAME_NAME_LIMIT = 32
STEAM_APPID = "374040"
SLOT_FILES = ["0%d00000000000000" % n for n in range(1, 10)]

# Save-file type prefixes (first byte of the 16-hex-digit filename)
#   00  small system / options blob (not characters — do not treat as CHAR)
#   01  characters container (always 0100000000000000 — all 9 slots)
#   02  game backup of the character file
#   03  universe (last hex digit = universe slot 1..9; cannot be freely remapped)
#   04  world / island (digit 9 / byte 4 = universe id; trailing hex = location code)
#   06  additional system blob seen in some installs (treat as misc)
FILE_TYPE_OPTIONS = 0x00
FILE_TYPE_CHAR = 0x01
FILE_TYPE_CHAR_BAK = 0x02
FILE_TYPE_UNIVERSE = 0x03
FILE_TYPE_WORLD = 0x04
FILE_TYPE_SERVER_SESSION = 0x05  # dedicated server session; auto-generated
FILE_TYPE_MISC = 0x06

# Island / world location codes (last 3–5 hex digits of a 04xxxxxxxxxxxxxxxx file).
# Sourced from community spreadsheet:
# https://docs.google.com/spreadsheets/d/1tAr_RdffZ8KrWMcAhmt4W4tay5ArjAxdX5kTEnaxYCk
WORLD_LOCATIONS = {
    # Area 1
    0x100: "Squire's Knoll (1-01)",
    0x101: "Dusty Junction (1-02)",
    0x102: "Fort Finch (1-03)",
    0x103: "Shrieking Sands (1-04)",
    0x104: "Garnet Peaks (1-05)",
    0x105: "Autumn Springs (1-06)",
    0x106: "Port Of Caul (1-07)",
    0x107: "Orson Orchards (1-08)",
    0x108: "Callum's Claim (1-09)",
    0x109: "Plains Of Passage (1-10)",
    0x163: "Worm Pit (2-01)",
    # Area 2
    0x200: "Landlubber's Leap (2-02)",
    0x201: "Brackenburg (2-03)",
    0x202: "Hintertown (2-04)",
    0x203: "Witchwater (2-05)",
    0x204: "Joren's Outpost (2-06)",
    0x205: "Angler's Wharf (2-07)",
    0x206: "North Point (2-08)",
    0x207: "Ghostlight Mire (2-09)",
    0x208: "Mosakola Harbor (2-10)",
    0x209: "Addermoor (2-11)",
    0x20a: "Broadside Bay (2-12)",
    0x20b: "Mount Meridian (2-13)",
    0x20c: "Mayyan Delta (2-14)",
    0x20d: "Deepest Mosakola (2-15)",
    0x20e: "Morello Marshes (2-16)",
    0x263: "Dragon's Lair (Fire Queen Boss)",
    # Area 3
    0x300: "The Great Scar (3-01)",
    0x301: "Pockmark Plains (3-02)",
    0x302: "Facetta (3-03)",
    0x303: "Glimmerglen (3-04)",
    0x304: "Lunar Landing (3-05)",
    0x305: "Sea of Stalks (3-06)",
    0x306: "Farpoint (3-07)",
    0x307: "Filia's Folly (3-08)",
    0x308: "New Caul (3-09)",
    0x309: "Pillars of Parun (3-11)",
    0x30a: "The Bone Wastes (3-12)",
    0x30b: "Starspires (3-13)",
    0x30c: "The Motherlode (3-14)",
    0x30d: "Old Hintertown (3-15)",
    0x30e: "Great Frontier (3-16)",
    0x363: "World's End (Hollow King Boss)",
    # Hub / special
    0x400: "The Gate",
    0x401: "Portal Knight's Sanctuary",
    0x407: "Rainbow Island (DLC)",
    0x40e: "Field of Balance (Spring Event)",
    0x40f: "Isle of Toblis (Bell Event)",
    # Vacant / title-deed islands
    0x50b: "Vacant Grassland Island",
    0x50c: "Vacant Forest Island",
    0x50d: "Vacant Fairy Forest Island",
    0x50e: "Vacant Dry Desert Island",
    0x50f: "Vacant Oasis Desert Island",
    0x515: "Vacant Coastal Island",
    0x516: "Vacant Toxic Island",
    0x517: "Vacant Swamp Island",
    0x518: "Vacant Polar Island",
    0x519: "Vacant Tropical Island",
    0x51f: "Vacant Crystal Island",
    0x520: "Vacant Volcano Island",
    0x521: "Vacant Hollow Island",
    0x522: "Vacant Asteroid Island",
    0x523: "Vacant Red Planet Island",
    # Events / bosses / seasonal
    0x004: "All Hallow's Land (Halloween)",
    0x005: "All Hallow's Land (Halloween)",
    0x006: "All Hallow's Land (Halloween)",
    0x00b: "Peach Tree Fields (Spring Event)",
    0x00c: "Mountain Temple (Spring Event)",
    0xa01: "Ancient Lair (Electro Worm)",
    0xa02: "Ancient Glacier (Ice Queen)",
    0xa03: "Ancient Refuge (King of Light)",
    0xa0b: "Tomb Of C'Thiris (Tesseract)",
    0xa0c: "Kolemis Temple (Bell Trials)",
    0xc01: "Morteheim Guildhall (Ghost Event)",
    # DLC / 5-digit location codes (full trailing value)
    0x20100: "Raven's Den (Elves/Rogues DLC)",
    0x20101: "Farran-Enore (Elves/Rogues DLC)",
    0x20102: "Low Rift Haven (Elves/Rogues DLC)",
    0x20103: "Middle Rift Haven (Elves/Rogues DLC)",
    0x20104: "High Rift Haven (Elves/Rogues DLC)",
    0x2020a: "Low Rift (Elves/Rogues DLC)",
    0x2020b: "Middle Rift (Elves/Rogues DLC)",
    0x2020c: "High Rift (Elves/Rogues DLC)",
    0x20401: "Bitter Root Battlefield (Druids DLC)",
    0x20402: "Fallentown Square (Druids DLC)",
    0x20500: "Vacant Lunar Furfolk Island (DLC)",
    0x20501: "Vacant Lunar Elf Island (DLC)",
    0x20600: "Bitter Root Battlefield Hard Mode",
    0x20601: "Fallentown Square Hard Mode",
    0x20602: "Temple Mines Hard Mode",
    0x20700: "Stoutheart Landing (Druids DLC)",
}

# Only structural world objects stay built-in. NPCs, enemies, props, blocks
# live in pk_templates.json (edit there or via Templates… in the UI).
# Chests are not a TemplateCRC list — they are detected by inventory components.
_BUILTIN_NPC_TEMPLATES = {}

_BUILTIN_WORLD_TEMPLATES = {
    0xF0653E24: "Sign",
    0x0BCB9932: "Landing Pad",
}

_BUILTIN_ENEMY_CRCS = set()

TEMPLATE_JSON_FILE = "pk_templates.json"

# Live merged maps (builtins + user JSON). Mutated by load/save helpers.
NPC_TEMPLATES = dict(_BUILTIN_NPC_TEMPLATES)
WORLD_TEMPLATES = dict(_BUILTIN_WORLD_TEMPLATES)
ENEMY_TEMPLATE_CRCS = set(_BUILTIN_ENEMY_CRCS)
TRADER_TEMPLATE_CRCS = {
    crc for crc, name in NPC_TEMPLATES.items()
    if "(trader)" in name.lower()
}
# kind per user entry: "npc" | "world" | "enemy" | "trader"
_USER_TEMPLATE_META = {}  # crc -> {"name", "kind"}


def _template_json_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        TEMPLATE_JSON_FILE)


def _reload_template_maps():
    """Rebuild NPC/WORLD/ENEMY maps from builtins + pk_templates.json."""
    global NPC_TEMPLATES, WORLD_TEMPLATES, ENEMY_TEMPLATE_CRCS
    global TRADER_TEMPLATE_CRCS, _USER_TEMPLATE_META
    NPC_TEMPLATES = dict(_BUILTIN_NPC_TEMPLATES)
    WORLD_TEMPLATES = dict(_BUILTIN_WORLD_TEMPLATES)
    ENEMY_TEMPLATE_CRCS = set(_BUILTIN_ENEMY_CRCS)
    _USER_TEMPLATE_META = {}
    path = _template_json_path()
    if not os.path.isfile(path):
        fetch_remote_data_file(TEMPLATE_JSON_FILE)
        path = _template_json_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    entries = data.get("templates") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        # also accept flat { "0x..": "Name" } or { "123": {"name", "kind"} }
        if isinstance(data, dict) and "templates" not in data:
            entries = []
            for k, v in data.items():
                if k.startswith("_"):
                    continue
                try:
                    crc = int(k, 0) & 0xFFFFFFFF
                except (TypeError, ValueError):
                    continue
                if isinstance(v, dict):
                    entries.append({"hash": crc, "name": v.get("name"),
                                    "kind": v.get("kind") or "world"})
                else:
                    entries.append({"hash": crc, "name": str(v),
                                    "kind": "world"})
        else:
            entries = entries or []
    for rec in entries:
        if not isinstance(rec, dict):
            continue
        h = rec.get("hash", rec.get("crc", rec.get("template")))
        name = rec.get("name") or rec.get("label")
        if h is None or not name:
            continue
        try:
            crc = int(h, 0) if isinstance(h, str) else int(h)
            crc &= 0xFFFFFFFF
        except (TypeError, ValueError):
            continue
        kind = (rec.get("kind") or "world").lower().strip()
        # Prefer single "island" string (user format); also accept islands[]
        islands = []
        if rec.get("island"):
            islands = [str(rec.get("island"))]
        elif rec.get("islands"):
            raw = rec.get("islands")
            if isinstance(raw, str):
                islands = [raw]
            else:
                islands = list(raw)
        _USER_TEMPLATE_META[crc] = {
            "name": str(name), "kind": kind,
            "islands": islands,
            "island": islands[0] if islands else ""}
        if kind in ("npc", "trader", "quest"):
            NPC_TEMPLATES[crc] = str(name)
        elif kind == "enemy":
            WORLD_TEMPLATES[crc] = str(name)
            ENEMY_TEMPLATE_CRCS.add(crc)
        else:
            WORLD_TEMPLATES[crc] = str(name)
    TRADER_TEMPLATE_CRCS = {
        crc for crc, name in NPC_TEMPLATES.items()
        if "(trader)" in name.lower() or
        _USER_TEMPLATE_META.get(crc, {}).get("kind") == "trader"
    }


def save_user_template(crc, name, kind="world", island=None):
    """Add/update one TemplateCRC in pk_templates.json and live maps.

    kind: npc | trader | enemy | world | quest
    island: optional location string (stored as "island" field).
    Returns the path written.
    """
    crc = int(crc) & 0xFFFFFFFF
    name = (name or "").strip() or ("0x%08X" % crc)
    kind = (kind or "world").lower().strip()
    if kind not in ("npc", "trader", "enemy", "world", "quest"):
        kind = "world"
    path = _here(TEMPLATE_JSON_FILE)
    data = {"templates": []}
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict) and isinstance(loaded.get("templates"), list):
            data = loaded
        elif isinstance(loaded, dict):
            for k, v in loaded.items():
                if k in ("templates",) or str(k).startswith("_"):
                    continue
                try:
                    c = int(k, 0) & 0xFFFFFFFF
                except (TypeError, ValueError):
                    continue
                if isinstance(v, dict):
                    data["templates"].append({
                        "hash": c, "name": v.get("name"),
                        "kind": v.get("kind") or "world",
                        "island": v.get("island") or ""})
                else:
                    data["templates"].append({
                        "hash": c, "name": str(v), "kind": "world"})
    except Exception:
        pass
    found = False
    for rec in data["templates"]:
        try:
            h = rec.get("hash", rec.get("crc"))
            c = int(h, 0) if isinstance(h, str) else int(h)
            c &= 0xFFFFFFFF
        except (TypeError, ValueError):
            continue
        if c == crc:
            rec["hash"] = crc
            rec["name"] = name
            rec["kind"] = kind
            rec["hash_hex"] = "0x%08X" % crc
            if island:
                # Merge into slash-separated island string if already set
                prev = (rec.get("island") or "").strip()
                if not prev:
                    rec["island"] = island
                elif island not in prev:
                    rec["island"] = prev + " / " + island
            # drop legacy islands[] if present
            rec.pop("islands", None)
            found = True
            break
    if not found:
        rec = {
            "hash": crc,
            "hash_hex": "0x%08X" % crc,
            "name": name,
            "kind": kind,
        }
        if island:
            rec["island"] = island
        data["templates"].append(rec)
    if "_comment" not in data:
        data["_comment"] = (
            "TemplateCRC names for pk_save_editor. "
            "kind: world|enemy|npc|trader|quest. "
            "island: home island(s) for this template.")
    ordered = {
        "_comment": data.get("_comment", ""),
        "templates": data["templates"],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(ordered, fh, indent=2, sort_keys=False)
        fh.write("\n")
    _reload_template_maps()
    return path


def template_islands(crc):
    """Island/world name(s) recorded for this TemplateCRC."""
    if crc is None:
        return []
    crc = int(crc) & 0xFFFFFFFF
    meta = _USER_TEMPLATE_META.get(crc) or {}
    out = []
    if meta.get("island"):
        out.append(meta["island"])
    for name in meta.get("islands") or []:
        if name and name not in out:
            out.append(name)
    return out



def all_known_templates():
    """crc -> name for every known template (npc + world)."""
    out = dict(WORLD_TEMPLATES)
    out.update(NPC_TEMPLATES)
    return out


def npc_name_for_template(crc):
    if crc is None:
        return None
    crc = int(crc) & 0xFFFFFFFF
    return NPC_TEMPLATES.get(crc) or WORLD_TEMPLATES.get(crc)


def template_label(crc, with_island=False):
    """Display name for any TemplateCRC, or a hex fallback."""
    if crc is None:
        return "?"
    crc = int(crc) & 0xFFFFFFFF
    name = npc_name_for_template(crc)
    if not name:
        name = "0x%08X" % crc
    if with_island:
        islands = template_islands(crc)
        if islands:
            name = "%s  [%s]" % (name, ", ".join(islands[:3]))
    return name


# Populate live maps once at import (pk_templates.json if present)
try:
    _reload_template_maps()
except Exception:
    TRADER_TEMPLATE_CRCS = {
        crc for crc, name in NPC_TEMPLATES.items()
        if "(trader)" in name.lower()
    }


_POLY_REFLECTED = 0xC96C5795D7870F42
_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ (_POLY_REFLECTED if _c & 1 else 0)
    _TABLE.append(_c)


def crc64(data):
    """ECMA-182 reflected CRC-64 (byte table; matches game header digests)."""
    c = 0xFFFFFFFFFFFFFFFF
    for b in data:
        c = _TABLE[(b ^ c) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFFFFFFFFFF


def load_dict(path):
    import zstandard as zstd
    with open(path, "rb") as fh:
        raw = fh.read()
    zdict = zstd.ZstdCompressionDict(raw)
    dctx = zstd.ZstdDecompressor(dict_data=zdict)
    cctx = zstd.ZstdCompressor(dict_data=zdict, level=19,
                                write_content_size=True)
    return dctx, cctx


def unwrap(chunk, dctx):
    if chunk[:4] == ZSTD_MAGIC:
        if dctx is None:
            return None, "need-dict"
        inner = dctx.decompress(chunk)
        if inner[:4] == SNPY_MAGIC:
            return snappy_decompress(inner[4:]), "zstd+snpy"
        return inner, "zstd"
    if chunk[:4] == SNPY_MAGIC:
        return snappy_decompress(chunk[4:]), "snpy"
    if len(chunk) >= 5:
        declared_len = struct.unpack_from("<i", chunk, 0)[0]
        if declared_len == len(chunk) and chunk[-1] == 0:
            return chunk, "raw"
    return None, None


def wrap(doc, kind, cctx):
    if kind == "zstd+snpy":
        return cctx.compress(SNPY_MAGIC + snappy_compress(doc))
    if kind == "snpy":
        return SNPY_MAGIC + snappy_compress(doc)
    if kind == "zstd":
        return cctx.compress(doc)
    if kind == "raw":
        return doc
    raise ValueError("cannot re-wrap kind %r" % kind)


def find_name_fields(doc):
    out = []
    pos = 0
    pat = b"\x05name\x00"
    while True:
        i = doc.find(pat, pos)
        if i == -1:
            return out
        pos = i + 1
        if i + 11 > len(doc):
            continue
        length, subtype = struct.unpack_from("<IB", doc, i + 6)
        if subtype != 0 or not 0 < length <= 4096:
            continue
        start = i + 11
        if start + length > len(doc):
            continue
        raw = doc[start:start + length]
        text = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
        out.append((start, length, text))


def set_name(doc, start, length, new):
    enc = new.encode("utf-8")
    if len(enc) > length:
        raise ValueError("name needs %d bytes, field holds %d"
                          % (len(enc), length))
    doc[start:start + length] = enc + b"\x00" * (length - len(enc))


# ----------------------------------------------------------------------
# generic BSON tree walk - lets us find and edit ANY scalar field, not
# just the one "name" pattern above. Each node records:
#   key, type, value, path (tuple of keys from the document root),
#   vstart/vend (byte range of the value, for patching),
#   chain (byte offsets of every enclosing document/array's own int32
#   length prefix, root-first) so a size-changing edit (e.g. a longer
#   string) can bump every ancestor's length field, not just the
#   immediate parent's.
#
# Unsupported BSON types raise rather than guess a length, since a wrong
# guess would silently corrupt everything after it in the buffer.

# NOTE ON TYPE 0x01 - it is a 4-byte FLOAT32, *not* BSON's 8-byte double.
#
# This is the single most important deviation from standard BSON and it
# was the cause of the "custom BSON type 0x00, key '\xf0A\x01z'" error.
# There was never an unknown type there: reading 'x' as 8 bytes swallowed
# 'y', so the parser resumed mid-field and read garbage as a type byte.
#
# Proof from a real save (Position, declared length 26):
#     01 78 00 <4 bytes>   x
#     01 79 00 <4 bytes>   y
#     01 7A 00 <4 bytes>   z
#     00                   terminator
#     4 + 7 + 7 + 7 + 1 = 26   <- matches exactly
# As doubles it would need 4 + 11 + 11 + 11 + 1 = 38.
# Orientation (x,y,z,w) declares 33 = 4 + 4*7 + 1. Same conclusion.
#
# Other non-standard types, all confirmed by making a whole real 4534-byte
# document parse to exactly its declared length with zero bytes left over:
#     0x13  8-byte uint64   (ItemIndex)
#     0x14  4-byte uint32   (a CRC-32 hash - see CRC_NAMES below)
#     0x16  1-byte uint8    (level, gender, selection, effectPackageIndex)
#     0x18  2-byte uint16   (SI/SC inventory slot index and stack count)
TYPE_NAMES = {
    0x01: "float32", 0x02: "string", 0x03: "document", 0x04: "array",
    0x05: "binary", 0x07: "objectid", 0x08: "bool", 0x09: "datetime(ms)",
    0x0A: "null", 0x10: "int32", 0x11: "timestamp(u64)", 0x12: "int64",
    0x13: "uint64", 0x14: "crc32 (hash)", 0x16: "uint8",
    0x18: "uint16",
}
# Only scalar types we know how to re-encode safely. Binary is included
# but can never grow (see bson_encode_scalar) - only shrink-or-equal,
# same rule the existing set_name() has always used for the name buffer.
#
# 0x14 is DUAL-USE, which is why it needs a key check rather than a blanket
# rule. In one real save it carries both:
#     plain counters   playtime 647, price 0, slotId 0, C 0, AC 0
#     name hashes      dCRC, TemplateCRC, raceCRC, classCRC, N, II, type
# Editing a counter is fine. Editing a hash to an arbitrary number points
# the game at an asset that does not exist, so those keys are blocked.
EDITABLE_TYPES = {0x01, 0x02, 0x05, 0x08, 0x09, 0x10, 0x11, 0x12, 0x13,
                  0x14, 0x16, 0x18}

# 0x14 keys that are hashes, not numbers - refuse to edit these.
HASH_KEYS = {"dCRC", "TemplateCRC", "templateCRC", "raceCRC", "classCRC",
             "effectPackageCRC", "N", "type"}
# II (item hash) is editable so equipment/inventory can be changed.
# Other name-hashes stay blocked - wrong values hard-crash asset lookup.


def is_editable(node):
    """Whether this specific node may be edited (type AND key)."""
    if node["type"] not in EDITABLE_TYPES:
        return False
    if node["type"] == 0x14 and node["key"] in HASH_KEYS:
        return False
    return True

# CRC-32 (zlib, standard) of a plain ASCII name. Recovered by brute force
# against a real save: crc32("health") == 0xCEDA2313 exactly, which also
# finally explains the "CEDA2313" marker used by the Cheat Engine tables -
# it was never a magic constant, it is just the hashed string "health".


# Binary fields are mostly NOT text. Decoding them as UTF-8 produced rows of
# mojibake in the tree ("{\ufffd\ufffdld..") which looked like corrupt save
# data but was purely a display artifact - the bytes were always fine.
#
# Worse, that same mojibake string was pre-filled into the edit box, so
# opening a binary field and pressing Apply re-encoded U+FFFD as 3 bytes
# each and destroyed the field. Verified on real modelCRCs bytes:
#     original   28 bytes  01 fd 01 57 c4 ed 4a ...
#     round-trip 42 bytes  01 ef bf bd 01 57 ef bf bd ...
# So text is only used where the field really is text.

TEXT_BINARY_KEYS = {"name"}

# Fields that are arrays of fixed-width little-endian integers, shown and
# edited as comma-separated numbers instead of raw hex.
UINT8_ARRAY_KEYS = {"modelIds", "textureIds", "colorIds"}
UINT32_ARRAY_KEYS = {"modelCRCs", "textureCRCs", "colorCRCs"}


def binary_preview(key, value):
    """Readable one-line preview of a binary field."""
    if key in TEXT_BINARY_KEYS:
        return value.split(b"\x00", 1)[0].decode("utf-8", "replace")
    if not value:
        return "(empty)"
    if key in UINT8_ARRAY_KEYS:
        return ", ".join(str(b) for b in value)
    if key in UINT32_ARRAY_KEYS and len(value) % 4 == 0:
        return ", ".join("%08X" % v for v in
                         struct.unpack("<%dI" % (len(value) // 4), value))
    if value[:4] == SNPY_MAGIC:
        return "SNPY nested blob, %d bytes" % len(value)
    head = value[:16].hex(" ")
    return "%s%s  (%d bytes)" % (head, " ..." if len(value) > 16 else "",
                                 len(value))


def binary_edit_text(key, value):
    """What to pre-fill the edit box with - must round-trip exactly."""
    if key in TEXT_BINARY_KEYS:
        return value.split(b"\x00", 1)[0].decode("utf-8", "replace")
    if key in UINT8_ARRAY_KEYS:
        return ", ".join(str(b) for b in value)
    if key in UINT32_ARRAY_KEYS and len(value) % 4 == 0:
        return ", ".join("%08X" % v for v in
                         struct.unpack("<%dI" % (len(value) // 4), value))
    return value.hex(" ")


def parse_binary_edit(key, raw, old_len):
    """Turn edit-box text back into bytes for a binary field."""
    if key in TEXT_BINARY_KEYS:
        return raw.encode("utf-8")
    parts = [p for p in raw.replace(",", " ").split() if p]
    if key in UINT8_ARRAY_KEYS:
        out = bytearray()
        for p in parts:
            v = int(p, 0)
            if not 0 <= v <= 255:
                raise ValueError("%s takes values 0-255, got %d" % (key, v))
            out.append(v)
        if len(out) != old_len:
            raise ValueError(
                "%s has %d entries and the count can't change here - "
                "you gave %d" % (key, old_len, len(out)))
        return bytes(out)
    if key in UINT32_ARRAY_KEYS:
        out = bytearray()
        for p in parts:
            out += struct.pack("<I", int(p, 16) & 0xFFFFFFFF)
        if len(out) != old_len:
            raise ValueError(
                "%s holds %d CRCs and the count can't change here - "
                "you gave %d" % (key, old_len // 4, len(out) // 4))
        return bytes(out)
    # generic: raw hex
    try:
        return bytes.fromhex(raw.replace(",", " "))
    except ValueError:
        raise ValueError(
            "this is a raw binary field - enter it as hex bytes, e.g. "
            "\"01 fd 01 57\". Got %r" % raw)


def binary_hint(key, value):
    """One-line instruction shown above the edit box for binary fields."""
    if key in TEXT_BINARY_KEYS:
        return "plain text, up to %d bytes" % len(value)
    if key in UINT8_ARRAY_KEYS:
        return ("%d comma-separated numbers, 0-255 (255 = none). "
                "The count can't change." % len(value))
    if key in UINT32_ARRAY_KEYS:
        return ("%d comma-separated hex CRCs. The count can't change."
                % (len(value) // 4))
    return "raw hex bytes, e.g. 01 fd 01 57 (max %d bytes)" % len(value)


# ----------------------------------------------------------------------
# Human-readable names
#
# The game hashes names with plain zlib CRC-32. Recovered by brute force
# against a real save - these are exact matches, not guesses:
#     crc32("health")     == 0xCEDA2313
#     crc32("mana")       == 0x60D64632
#     crc32("experience") == 0x0590C103
#     crc32("durability") == 0xC764ED49
# That also finally explains the "CEDA2313" constant in the Cheat Engine
# tables: it was never magic, it is just the hashed string "health".
#
# The remaining AV keys did not fall to a ~13,000-word brute force, so they
# are left as raw hex rather than mislabelled with a plausible guess.

def _c(name):
    return __import__("zlib").crc32(name.encode()) & 0xFFFFFFFF


CRC_NAMES = {_c(n): n for n in ("health", "mana", "experience",
                                 "durability")}

# Equipment slot order, matching the in-game Armor/Vanity panels top to
# bottom. IEQ is the Armor tab, VEQ is the Vanity tab; both use the same
# slot indices. Confirmed against the in-game UI:
#     Helmet, Chest, Arms, Legs, Cape, Ring
# PET is a separate array the current game build no longer surfaces.
#
# Slot 6, "Extra Head", is a confirmed real slot, not a guess: this save's
# IEQ[6] holds II=333644537, which item_table_merged.json resolves to
# "Halo" (category Vanity) - a cosmetic overlay worn on top of the
# helmet, exactly matching the "2nd hat" behaviour described for it. It
# sits inside the *Armor* array (IEQ) even though the item itself is a
# Vanity-category item - the slot is cosmetic-only regardless of which
# array carries it, so the picker for it always searches Vanity items.
EQUIP_SLOT_NAMES = ["Helmet", "Chest", "Arms", "Legs", "Cape", "Ring",
                     "Extra Head"]

# Which container arrays hold equipment (SI is a slot index into the list
# above) versus plain storage (SI is just a bag position).
EQUIP_ARRAYS = {"IEQ": "Armor", "VEQ": "Vanity"}
BAG_ARRAYS = {"IBP": "Backpack", "IAB": "Action bar", "ITP": "ITP",
              "ICO": "ICO", "ICS": "ICS", "PET": "Pets"}

# Game rules worth enforcing so an edit doesn't get silently rejected on
# load. Character level cap is 30 - corroborated two ways: the community
# has documented a hard cap of 30 since launch, and this save's own
# talentLineSelection array stops at level 30 (2,5,10,15,20,25,30).
# Limits disabled by request — write whatever the BSON type can encode.
FIELD_LIMITS = {}
FIELD_ADVISORIES = {}


def field_limit(node):
    """(lo, hi, why) for a hard range - values outside are refused."""
    return FIELD_LIMITS.get(node["key"])


def field_advisory(node, value):
    """Warning text if value is outside a soft range, else None."""
    adv = FIELD_ADVISORIES.get(node["key"])
    if not adv:
        return None
    lo, hi, why = adv
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return None if lo <= v <= hi else why


def slot_label(array_key, slot_index):
    """Name for an SI value inside a known array, or None."""
    if array_key in EQUIP_ARRAYS and 0 <= slot_index < len(EQUIP_SLOT_NAMES):
        return EQUIP_SLOT_NAMES[slot_index]
    return None


def crc_label(value):
    """Human name for a 0x14 hash, if we know it."""
    return CRC_NAMES.get(value)

# ----------------------------------------------------------------------
# Item database
#
# item_table_merged.json replaces the old pk_items.json / pk_item_crcs.json
# pair entirely. It's entry 1230 of the game's own server_core.kfc_data -
# the authoritative 4,112-item table - and critically its "hash" field IS
# the II value saves actually use. That is a direct CRC32 -> item lookup:
# no more "teach the tool a pairing by equipping it in game first" step,
# and nothing persisted next to the script anymore.
#
# Every row also carries a "confidence" flag for how its name/category
# were recovered: exact/high (trustworthy), medium/low (best-effort,
# shown with a "?" so a shaky guess is never mistaken for a fact), gap-
# filled (interpolated, no direct evidence), or unmatched (977 rows - no
# reliable identity at all, category is None). Unmatched rows are cut
# content, dev/debug entries, or hashes over things the table's
# rank-interpolation just couldn't reach. They're excluded from every
# item picker so nothing gets placed that isn't really an obtainable
# item - but if a save happens to already reference one, it still shows
# up (tagged "(unidentified)") rather than looking unexplained.
ITEM_TABLE_FILE = "item_table_merged.json"

_ITEM_TABLE = None
_ITEM_TABLE_PATH = None    # absolute path last successfully read/written
_ITEM_BY_HASH = None       # hash -> preferred single record
_ITEM_BY_HASH_ALL = None   # hash -> list of all records (dup hashes)


def _here(filename):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


# GitHub raw data (cached next to the script after first download)
GITHUB_RAW_BASE = (
    "https://raw.githubusercontent.com/CookiestMonster/"
    "Portal-Knights-Save-Editor/refs/heads/main/"
)
REMOTE_DATA_FILES = (
    "item_table_merged.json",
    "pk_templates.json",
)


def fetch_remote_data_file(filename, force=False, log_fn=None):
    """Download a data file from GitHub into the script directory.

    If the local file already exists and force is False, leave it alone
    (fast startup). Returns (path, status) where status is
    'cached' | 'downloaded' | 'failed'.
    """
    def _log(msg):
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    filename = os.path.basename(filename)
    if filename not in REMOTE_DATA_FILES:
        return _here(filename), "skipped"
    path = _here(filename)
    if os.path.isfile(path) and not force:
        return path, "cached"
    url = GITHUB_RAW_BASE + filename
    try:
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "pk_save_editor/1.0"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        if not data or len(data) < 2:
            raise ValueError("empty response")
        # Basic sanity: JSON should start with { or [
        head = data.lstrip()[:1]
        if head not in (b"{", b"["):
            raise ValueError("response is not JSON")
        tmp = path + ".download"
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _log("Downloaded %s (%d bytes) from GitHub" % (filename, len(data)))
        return path, "downloaded"
    except Exception as ex:
        _log("GitHub fetch failed for %s: %s" % (filename, ex))
        if os.path.isfile(path):
            return path, "cached"
        return path, "failed"


def ensure_remote_data(force=False, log_fn=None):
    """Ensure item table + templates are present (download if missing)."""
    results = {}
    for name in REMOTE_DATA_FILES:
        _path, status = fetch_remote_data_file(
            name, force=force, log_fn=log_fn)
        results[name] = status
    return results


def item_table_path():
    """Always use item_table_merged.json next to pk_save_editor.py.

    Never fall back to cwd — that was writing a different file than the
    one the user was watching in Explorer.
    """
    global _ITEM_TABLE_PATH
    path = os.path.abspath(_here(ITEM_TABLE_FILE))
    _ITEM_TABLE_PATH = path
    return path


def item_table():
    """The full item table (list of dicts), or [] if the file is missing."""
    global _ITEM_TABLE, _ITEM_TABLE_PATH
    if _ITEM_TABLE is None:
        path = item_table_path()
        if not os.path.isfile(path):
            fetch_remote_data_file(ITEM_TABLE_FILE)
            path = item_table_path()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                _ITEM_TABLE = json.load(fh)
            _ITEM_TABLE_PATH = os.path.abspath(path)
        except Exception:
            _ITEM_TABLE = []
        # Sanitize bad JSON values (hash_hex: inf, "null", etc.)
        if isinstance(_ITEM_TABLE, list):
            for rec in _ITEM_TABLE:
                if not isinstance(rec, dict):
                    continue
                h = _as_u32(rec.get("hash"))
                hx = rec.get("hash_hex")
                if h is not None:
                    rec["hash"] = h
                    rec["hash_hex"] = "%08x" % h
                else:
                    rec["hash"] = None
                    if not isinstance(hx, str) or hx.lower() in (
                            "null", "none", "nan", "inf", ""):
                        rec["hash_hex"] = None
                for fld in ("name", "category", "description", "confidence"):
                    v = rec.get(fld)
                    if v is not None and not isinstance(v, str):
                        rec[fld] = _s(v)
    return _ITEM_TABLE


def _table_index_of(rec):
    """table_index from a row, or None if missing."""
    v = rec.get("table_index")
    if v is None:
        v = rec.get("index")
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_u32(value):
    """Coerce a hash / II / CRC to uint32, or None if missing/invalid."""
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = value.strip()
            if not value or value.lower() in ("none", "null", "nan", "inf",
                                              "+inf", "-inf"):
                return None
            return int(value, 0) & 0xFFFFFFFF
        if isinstance(value, float):
            import math
            if not math.isfinite(value):
                return None
            value = int(value)
        return int(value) & 0xFFFFFFFF
    except (TypeError, ValueError, OverflowError):
        return None


def _s(value):
    """Safe string for display / .lower() — never crashes on float/None."""
    if value is None:
        return ""
    if isinstance(value, float):
        import math
        if not math.isfinite(value):
            return ""
    return str(value)


def _prefer_item_record(records):
    """Pick one row when several share the same hash.

    Prefer table_index == 0, then the lowest table_index, then first seen.
    Duplicate hashes in item_table_merged.json are intentional (same II,
    different table slots) — not a merge error.
    """
    if not records:
        return None
    if len(records) == 1:
        return records[0]
    with_idx = [( _table_index_of(r), r) for r in records]
    # exact 0 first
    zeros = [r for i, r in with_idx if i == 0]
    if zeros:
        return zeros[0]
    # then lowest non-None index
    numbered = sorted(
        ((i, r) for i, r in with_idx if i is not None),
        key=lambda t: t[0])
    if numbered:
        return numbered[0][1]
    return records[0]


def item_by_hash_all():
    """int(hash) -> list of all table rows with that hash (may be >1)."""
    global _ITEM_BY_HASH_ALL
    if _ITEM_BY_HASH_ALL is None:
        _ITEM_BY_HASH_ALL = {}
        for rec in item_table():
            key = _as_u32(rec.get("hash"))
            if key is None:
                continue
            _ITEM_BY_HASH_ALL.setdefault(key, []).append(rec)
    return _ITEM_BY_HASH_ALL


def item_by_hash():
    """int(hash) -> preferred item record (table_index 0 wins on ties).

    Save II values are this hash. When the table has two rows with the
    same hash (e.g. Titanium Masterwork fists of war), we keep both in
    item_by_hash_all() and resolve display via table_index.
    """
    global _ITEM_BY_HASH
    if _ITEM_BY_HASH is None:
        _ITEM_BY_HASH = {}
        for h, recs in item_by_hash_all().items():
            _ITEM_BY_HASH[h] = _prefer_item_record(recs)
    return _ITEM_BY_HASH


# Confidence tags used to be shown in the UI ("??", "gap-filled"). Those
# are research notes, not useful to players — names are shown plain.
CONFIDENCE_TAG = {
    "exact": "", "high": "", "medium": "", "low": "",
    "gap-filled": "", "unmatched": "",
}

# Race / class CRCs confirmed against CharacterSetup.raceCRC / classCRC.
RACE_NAMES = {
    3491209201: "Elf",
    2958828689: "Human",
    2533362399: "Furfolk",
}
CLASS_NAMES = {
    1360421256: "Ranger",
    3531629805: "Warrior",
    978865954: "Mage",
    866883432: "Rogue",
    2313804472: "Druid",
}
RACE_BY_NAME = {v: k for k, v in RACE_NAMES.items()}
CLASS_BY_NAME = {v: k for k, v in CLASS_NAMES.items()}
GENDER_NAMES = {0: "Male", 1: "Female"}
GENDER_BY_NAME = {"Male": 0, "Female": 1}

# Category groups offered by each placement picker. Ring and Capes are
# not class- or panel-specific in game - the same item works in either
# the Armor or Vanity copy of that slot - so both the Armor and Vanity
# pickers include them.
ARMOR_CATEGORIES = ["Warrior Armor", "Archer Armor", "Mage Armor",
                     "Rogue Armor", "Druid Armor", "Other Armor"]
CAPE_CATEGORIES = ["Capes"]
RING_CATEGORIES = ["Ring"]
VANITY_CATEGORIES = ["Vanity", "Vanity DLC"]
PET_CATEGORIES = ["Pets"]
MOUNT_CATEGORIES = ["Mounts"]
RECIPE_CATEGORIES = ["Recipes"]

# Per Armor/Vanity slot index (0-6, see EQUIP_SLOT_NAMES): which
# categories that slot's picker searches by default. Slot 6 (Extra Head)
# is cosmetic-only in both arrays - confirmed against this save's IEQ[6].
SLOT_DEFAULT_CATEGORIES = {
    0: ARMOR_CATEGORIES, 1: ARMOR_CATEGORIES,
    2: ARMOR_CATEGORIES, 3: ARMOR_CATEGORIES,
    4: CAPE_CATEGORIES, 5: RING_CATEGORIES,
    6: VANITY_CATEGORIES,
}


def item_record_for_crc(crc, table_index=None, category_hint=None):
    """Preferred row for a save II hash.

    If table_index is given, prefer that row. Else if category_hint is a
    category string or iterable of categories (e.g. from chest context),
    prefer a row whose category matches. Else table_index 0 / lowest.
    """
    key = _as_u32(crc)
    if key is None:
        return None
    alts = item_by_hash_all().get(key, [])
    if not alts:
        return None
    if table_index is not None:
        try:
            want = int(table_index)
        except (TypeError, ValueError):
            want = None
        if want is not None:
            for rec in alts:
                if _table_index_of(rec) == want:
                    return rec
    if category_hint:
        if isinstance(category_hint, str):
            hints = {category_hint}
        else:
            try:
                hints = set(category_hint)
            except TypeError:
                hints = {category_hint}
        hints = {h for h in hints if h}
        if hints:
            matched = [r for r in alts
                       if (r.get("category") or "") in hints]
            if matched:
                return _prefer_item_record(matched)
    return item_by_hash().get(key) or _prefer_item_record(alts)


def item_records_for_crc(crc):
    """All table rows that share this hash (empty list if none)."""
    key = _as_u32(crc)
    if key is None:
        return []
    return list(item_by_hash_all().get(key, []))


def chest_context_categories(crcs):
    """Majority categories among unambiguous items in a chest.

    Only rows with a unique name for their hash vote — so shared-hash
    noise does not dominate. Returns a list of categories sorted by
    frequency (ties keep insertion order).
    """
    from collections import Counter
    votes = Counter()
    for crc in crcs:
        alts = item_records_for_crc(crc)
        if not alts:
            continue
        names = {(r.get("name") or "") for r in alts}
        if len(names) != 1:
            continue  # ambiguous — don't vote
        cat = alts[0].get("category")
        if cat:
            votes[cat] += 1
    if not votes:
        return []
    # most common first
    return [c for c, _n in votes.most_common()]


def item_name_for_crc(crc, table_index=None, category_hint=None):
    """Readable name for a save's II hash.

    category_hint: optional category or list of categories (chest context)
    used to pick among shared-hash rows.
    """
    rec = item_record_for_crc(crc, table_index=table_index,
                              category_hint=category_hint)
    if not rec:
        return None
    name = rec.get("name") or "(unnamed)"
    name = name + CONFIDENCE_TAG.get(rec.get("confidence"), "")
    if table_index is not None:
        return name
    alts = item_records_for_crc(crc)
    if len(alts) <= 1:
        return name
    names = {(r.get("name") or "(unnamed)") for r in alts}
    if category_hint and len(names) > 1:
        # Context resolved it — only note if still ambiguous among matches
        return name
    idxs = sorted(
        {_table_index_of(r) for r in alts if _table_index_of(r) is not None})
    if len(names) > 1:
        return "%s  [hash shared ×%d, using idx %s]" % (
            name, len(alts),
            _table_index_of(rec) if _table_index_of(rec) is not None else "?")
    if idxs:
        return "%s  [idx %s; hash ×%d]" % (
            name,
            _table_index_of(rec) if _table_index_of(rec) is not None else "?",
            len(alts))
    return name


def invalidate_item_cache():
    """Drop cached table maps so the next lookup reloads from disk."""
    global _ITEM_TABLE, _ITEM_BY_HASH, _ITEM_BY_HASH_ALL
    _ITEM_TABLE = None
    _ITEM_BY_HASH = None
    _ITEM_BY_HASH_ALL = None
    # keep _ITEM_TABLE_PATH so we rewrite the same file


def _item_table_write_report(path, crc, name):
    """Human-readable proof a JSON write landed on disk."""
    try:
        mtime = os.path.getmtime(path)
        import time as _time
        mt = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(mtime))
        sz = os.path.getsize(path)
    except Exception as ex:
        return "Wrote %s\n(could not stat: %s)" % (path, ex)
    return (
        "ITEM TABLE JSON UPDATED (not the save file)\n\n"
        "File:\n  %s\n\n"
        "Modified: %s\nSize: %d bytes\n\n"
        "0x%08X = %s\n\n"
        "A stamp file was also written next to it:\n"
        "  pk_item_table_last_write.txt"
        % (path, mt, sz, crc, name)
    )


def update_item_table_entry(crc, name=None, category=None,
                            confidence="exact", table_index=None,
                            log_fn=None):
    """Correct name/category for a hash in item_table_merged.json.

    Returns the absolute path written. Progress goes to log_fn when given.
    """
    global _ITEM_TABLE_PATH
    lines = []

    def L(msg):
        lines.append(msg)
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    try:
        crc = _as_u32(crc)
        L("=== update_item_table_entry ===")
        L("__file__ = %s" % globals().get("__file__", "?"))
        L("script dir = %s" % os.path.dirname(os.path.abspath(
            globals().get("__file__", ".") or ".")))
        L("cwd = %s" % os.getcwd())
        L("crc arg raw processed = %s" % (None if crc is None
                                          else "0x%08X (%d)" % (crc, crc)))
        L("name=%r category=%r confidence=%r" % (name, category, confidence))
        if crc is None:
            raise ValueError("No item hash to update (II is empty/null)")
        name = (str(name).strip() if name is not None else None)
        if name is not None and not name:
            raise ValueError("Name is empty")

        item_table()
        path = item_table_path()
        L("item_table_path() = %s" % path)
        L("exists before = %s" % os.path.isfile(path))
        if os.path.isfile(path):
            st = os.stat(path)
            L("size before = %d  mtime before = %s" % (
                st.st_size, __import__("time").ctime(st.st_mtime)))

        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            raise ValueError("Cannot read %s: %s" % (path, exc))
        if not isinstance(data, list):
            raise ValueError("item_table_merged.json is not a list (got %s)"
                             % type(data).__name__)
        if not data:
            raise ValueError("item_table_merged.json is empty at %s" % path)
        L("rows loaded = %d" % len(data))

        want_idx = None
        if table_index is not None:
            try:
                want_idx = int(table_index)
            except (TypeError, ValueError):
                want_idx = None

        target = None
        how = None
        if name is not None:
            for rec in data:
                if (rec.get("name") or "").strip().lower() != name.lower():
                    continue
                target = rec
                how = "matched existing name row (hash was %r)" % rec.get("hash")
                break
        if target is None:
            for rec in data:
                if _as_u32(rec.get("hash")) != crc:
                    continue
                if want_idx is not None and _table_index_of(rec) != want_idx:
                    continue
                target = rec
                how = "matched existing hash row name=%r" % rec.get("name")
                break
        if target is None:
            if name is None:
                raise ValueError("No table row for hash 0x%08X" % crc)
            idxs = [i for i in (_table_index_of(r) for r in data)
                    if i is not None]
            new_idx = (max(idxs) + 1) if idxs else len(data)
            target = {
                "table_index": new_idx,
                "hash": crc,
                "hash_hex": "%08x" % crc,
                "price": 0,
                "flag1": 0,
                "flag2": 0,
                "name": name,
                "category": (str(category).strip() if category else None),
                "description": "",
                "item_id": None,
                "max_stack": 1,
                "stackable": False,
                "confidence": confidence or "exact",
            }
            data.append(target)
            how = "appended new row table_index=%d" % new_idx
        L("target: %s" % how)

        old_name = target.get("name")
        if name is not None:
            target["name"] = name
        if category is not None:
            target["category"] = str(category).strip() or None
        if confidence is not None:
            target["confidence"] = confidence
        target["hash"] = crc
        target["hash_hex"] = "%08x" % crc
        L("target name %r -> %r  hash=0x%08X" % (old_name, target.get("name"), crc))

        cleared = 0
        for rec in data:
            if rec is target:
                continue
            if _as_u32(rec.get("hash")) == crc:
                L("clearing hash from other row name=%r idx=%s" % (
                    rec.get("name"), _table_index_of(rec)))
                rec["hash"] = None
                rec["hash_hex"] = None
                cleared += 1
        L("cleared other rows = %d" % cleared)

        tmp = path + ".tmp"
        L("writing tmp = %s" % tmp)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            tmp_size = os.path.getsize(tmp)
            L("tmp size = %d" % tmp_size)
            os.replace(tmp, path)
            L("os.replace OK")
        except Exception as exc:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            L("WRITE FAILED: %s: %s" % (type(exc).__name__, exc))
            raise ValueError("Cannot write %s: %s" % (path, exc))

        if not os.path.isfile(path):
            raise ValueError("After replace, file missing: %s" % path)
        st = os.stat(path)
        L("size after = %d  mtime after = %s" % (
            st.st_size, __import__("time").ctime(st.st_mtime)))

        _ITEM_TABLE_PATH = os.path.abspath(path)
        invalidate_item_cache()
        check = item_record_for_crc(crc)
        L("lookup after reload: name=%r hash=%r" % (
            (check or {}).get("name"), (check or {}).get("hash")))
        if not check or (name and (check.get("name") or "").strip() != name):
            raise ValueError(
                "Write appeared to succeed but lookup still wrong for 0x%08X "
                "(got %r). File: %s"
                % (crc, (check or {}).get("name"), path))

        L("SUCCESS")
        # Log trail stays in the app Log panel only (no sidecar files).
        if log_fn:
            for line in lines:
                try:
                    log_fn(line)
                except Exception:
                    pass
        return _ITEM_TABLE_PATH
    except Exception:
        # Re-raise after optional log; L() already captured context
        if log_fn:
            for line in lines:
                try:
                    log_fn(line)
                except Exception:
                    pass
        raise


def parse_item_description(desc):
    """Split the jammed item-table description into defence/damage + affixes.

    Example source text:
      'Defence 53 +12% Mining Damage Workbench II 6x Copper Bar Class - None'
      'Damage 40 Type Phys …'

    Returns dict with keys:
      defence, damage (int|None), affixes, notes, raw
    """
    raw = (desc or "").strip()
    out = {"defence": None, "damage": None, "affixes": [], "notes": [],
           "raw": raw}
    if not raw:
        return out

    text = raw
    # Defence 53 / Defence None
    m = re.match(r"^Defence\s+(None|\d+)\s*", text, re.I)
    if m:
        out["defence"] = None if m.group(1).lower() == "none" else int(m.group(1))
        text = text[m.end():]
    # Damage 40 (weapons often lead with this)
    m = re.match(r"^Damage\s+(\d+)\s*", text, re.I)
    if m:
        out["damage"] = int(m.group(1))
        text = text[m.end():]
    else:
        # Sometimes buried: "Damage 40 Type Phys"
        m = re.search(r"\bDamage\s+(\d+)\b", text, re.I)
        if m:
            out["damage"] = int(m.group(1))

    # Pull class requirement off the end
    cm = re.search(r"\s*Class\s*-\s*(\S+)\s*$", text, re.I)
    if cm:
        cls = cm.group(1)
        if cls.lower() != "none":
            out["notes"].append("Class %s" % cls)
        text = text[:cm.start()]

    # Affixes: +N% Name / +N Name (greedy name until next + or end)
    for am in re.finditer(
            r"\+(\d+%?)\s+([A-Za-z][A-Za-z0-9/' \-]*?)"
            r"(?=\s*\+\d|\s*Found\s|\s*Dropped\s|\s*Workbench|\s*Requirements|\s*$)",
            text):
        out["affixes"].append("+%s %s" % (am.group(1), am.group(2).strip()))

    # Leftover source / craft notes
    leftover = text
    for a in out["affixes"]:
        leftover = leftover.replace(a, " ", 1)
    leftover = re.sub(r"\s+", " ", leftover).strip(" -")
    if leftover:
        # split common noise phrases into notes
        for part in re.split(r"\s{2,}|\s+(?=Found |\s*Dropped |\s*Workbench |\s*Requirements )", leftover):
            part = part.strip(" -")
            if part and part not in out["affixes"]:
                out["notes"].append(part)

    return out


def item_stats_for_crc(crc):
    """Parsed combat stats from the item table description, if any."""
    rec = item_record_for_crc(crc)
    if not rec:
        return None, parse_item_description("")
    return rec, parse_item_description(rec.get("description") or "")


# Stack cap
#
# SC is a uint16 (BSON type 0x18), so 65535 is the hard encodable
# ceiling - but it is not the real cap for any given item. The table's
# per-item max_stack (observed in-game caps: 10/16/20/32/50/99/...) is
# what "max stacks" should actually fill to; a flat 65535 just produces
# a stack the game clamps back down on load. DEFAULT_MAX_STACK is only
# the fallback for a hash the table doesn't cover.
DEFAULT_MAX_STACK = 999


def item_max_stack(crc, default=DEFAULT_MAX_STACK):
    rec = item_record_for_crc(crc)
    if rec and rec.get("max_stack"):
        try:
            return int(rec["max_stack"])
        except (TypeError, ValueError):
            pass
    return default


def item_is_placeable(rec):
    """False for unmatched / no-category / no-hash rows.

    Rows with hash null cannot be written into a save (II needs a real
    CRC), so they must not appear in item pickers.
    """
    if rec is None:
        return False
    if rec.get("category") is None:
        return False
    if rec.get("confidence") == "unmatched":
        return False
    if _as_u32(rec.get("hash")) is None:
        return False
    return True


def item_search(query="", categories=None, limit=200):
    """Search the item table by name, category, or hash (decimal or hex).

    categories, if given, restricts results to that set of category
    strings (e.g. ARMOR_CATEGORIES). An empty query with categories set
    lists every item in those categories - used to populate a picker
    before the user types anything. Unmatched/no-category rows are
    always excluded (see item_is_placeable).
    """
    q = (query or "").strip().lower()
    out = []
    for rec in item_table():
        if not item_is_placeable(rec):
            continue
        if categories and rec.get("category") not in categories:
            continue
        if q:
            name = _s(rec.get("name")).lower()
            cat = _s(rec.get("category")).lower()
            hh = _s(rec.get("hash_hex")).lower()
            if hh in ("null", "none", "nan", "inf"):
                hh = ""
            if q not in name and q not in cat and \
                    q != str(rec.get("hash") or "") and \
                    q != hh and q != hh.lstrip("0"):
                continue
        out.append(rec)

    def sort_key(rec):
        name = _s(rec.get("name")).lower()
        rank = 0 if name == q else (1 if q and name.startswith(q) else 2)
        # Prefer table_index 0 when several rows share a hash/name
        ti = _table_index_of(rec)
        ti_key = 0 if ti == 0 else (1 if ti is None else 2, ti or 0)
        return (rank, rec.get("category") or "", name, ti_key)
    out.sort(key=sort_key)
    return out[:limit]


# ----------------------------------------------------------------------
# Inventory / character navigation helpers for the multi-tab editor
# ----------------------------------------------------------------------

def _walk(nodes):
    """Yield every node in a tree depth-first.

    Accepts either a list of node dicts (the usual bson_parse root) or a
    single node dict (e.g. a component returned by find_component). A bare
    dict must not be iterated as a sequence of keys - that produced the
    TypeError: string indices must be integers when opening Character Editor.
    """
    if nodes is None:
        return
    if isinstance(nodes, dict):
        yield nodes
        children = nodes.get("children")
        if children:
            yield from _walk(children)
        return
    for n in nodes:
        if not isinstance(n, dict):
            continue
        yield n
        if n.get("children"):
            yield from _walk(n["children"])


def find_named_array(nodes, array_key):
    """Return the array node whose key is array_key, or None."""
    for n in _walk(nodes):
        if n.get("key") == array_key and n.get("children") is not None:
            return n
    return None


def find_component(nodes, component_name):
    """Return the document node for a named component, or None."""
    for n in _walk(nodes):
        if n.get("key") == component_name and n.get("children") is not None:
            return n
    return None


def _entity_position(entity_node):
    """Return (x,y,z) floats from Entity/CreationParameter/Position, or None."""
    if not entity_node or not entity_node.get("children"):
        return None
    cp = None
    for ch in entity_node["children"]:
        if ch["key"] == "CreationParameter" and ch.get("children") is not None:
            cp = ch
            break
    if cp is None:
        return None
    pos = None
    for ch in cp["children"]:
        if ch["key"] == "Position" and ch.get("children") is not None:
            pos = ch
            break
    if pos is None:
        return None
    xyz = {}
    for ch in pos["children"]:
        if ch["key"] in ("x", "y", "z") and ch.get("value") is not None:
            xyz[ch["key"]] = float(ch["value"])
    if len(xyz) == 3:
        return (xyz["x"], xyz["y"], xyz["z"])
    return None


def _entity_template_crc(entity_node):
    if not entity_node or not entity_node.get("children"):
        return None
    for ch in entity_node["children"]:
        if ch["key"] == "TemplateCRC" and ch.get("value") is not None:
            return int(ch["value"]) & 0xFFFFFFFF
    return None


def iter_entities(nodes):
    """Yield Entity document nodes under any EntityArray."""
    for n in _walk(nodes):
        if n["key"] == "EntityArray" and n.get("children") is not None:
            for ent in n["children"]:
                if ent.get("children") is not None:
                    yield ent


def extract_world_chests(nodes):
    """List chests: dicts with pos, inv arrays, entity node, item_count."""
    out = []
    for ent in iter_entities(nodes):
        inv_comp = None
        for ch in ent.get("children") or []:
            if (ch["key"] == "ComponentData" and ch.get("children") is not None):
                # Inventory may be nested under ComponentData or deeper
                inv_comp = find_component(ch["children"],
                                          "Server Inventory Component")
                if inv_comp:
                    break
            if ch["key"] == "Server Inventory Component" and ch.get("children") is not None:
                inv_comp = ch
                break
        if inv_comp is None:
            # broader search under this entity only
            for n in _walk([ent]):
                if n["key"] == "Server Inventory Component" and n.get("children") is not None:
                    inv_comp = n
                    break
        if inv_comp is None:
            continue
        invs = []
        item_count = 0
        for n in _walk([inv_comp]):
            if n["key"] in ("IBP", "IAB", "IEQ", "VEQ", "PET") and n.get("children") is not None:
                invs.append(n)
                item_count += len(inventory_slot_map(n))
        if not invs:
            continue
        out.append({
            "entity": ent,
            "pos": _entity_position(ent),
            "template": _entity_template_crc(ent),
            "invs": invs,
            "item_count": item_count,
        })
    return out



def _decode_ues_text(value):
    """Decode User Editable String payload (BSON string or binary)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        # binary field may include a trailing NUL
        if b"\x00" in raw:
            raw = raw.split(b"\x00")[0]
        return raw.decode("utf-8", "replace")
    return str(value)


def _ues_from_entity(ent):
    """Return (comp_node, text, text_node, was_edited) or (None, "", None, None)."""
    sign_comp = None
    for n in _walk([ent]):
        if n.get("key") == "User Editable String Component" and n.get("children") is not None:
            sign_comp = n
            break
    if sign_comp is None:
        return None, "", None, None
    text_node = None
    was_edited = None
    text = ""
    for ch in sign_comp.get("children") or []:
        k = ch.get("key")
        if k == "wasEdited":
            was_edited = ch.get("value")
        if k in ("string", "text", "Text", "value") and ch.get("value") is not None:
            text_node = ch
            text = _decode_ues_text(ch.get("value"))
    # Fallback: any string/binary child under the component
    if text_node is None:
        for ch in sign_comp.get("children") or []:
            if ch.get("type") in (0x02, 0x05) and ch.get("value") is not None:
                text_node = ch
                text = _decode_ues_text(ch.get("value"))
                break
    return sign_comp, text, text_node, was_edited


def extract_world_signs(nodes):
    """List sign-like entities (User Editable String Component).

    When wasEdited is False the save often stores no string — the game shows
    default text for that TemplateCRC. Those signs are still listed so you
    can find them; text may be empty until edited in-game or here.
    NPCs that also carry the component are skipped (see extract_world_npcs).
    """
    out = []
    for ent in iter_entities(nodes):
        has_npc = False
        for n in _walk([ent]):
            if n.get("key") == "NPC Control Component":
                has_npc = True
                break
        if has_npc:
            continue
        sign_comp, text, text_node, was_edited = _ues_from_entity(ent)
        if sign_comp is None:
            continue
        out.append({
            "entity": ent,
            "pos": _entity_position(ent),
            "template": _entity_template_crc(ent),
            "text": text or "",
            "text_node": text_node,
            "was_edited": was_edited,
            "component": sign_comp,
        })
    return out


def extract_world_npcs(nodes):
    """Entities with NPC Control Component: pos, template, optional custom text."""
    out = []
    for ent in iter_entities(nodes):
        has_npc = False
        for n in _walk([ent]):
            if n.get("key") == "NPC Control Component":
                has_npc = True
                break
        if not has_npc:
            continue
        _comp, text, text_node, was_edited = _ues_from_entity(ent)
        out.append({
            "entity": ent,
            "pos": _entity_position(ent),
            "template": _entity_template_crc(ent),
            "text": text or "",
            "text_node": text_node,
            "was_edited": was_edited,
        })
    return out



def extract_world_all_templates(nodes):
    """Every entity that has a TemplateCRC (props, enemies, pads, …)."""
    out = []
    for ent in iter_entities(nodes):
        tmpl = _entity_template_crc(ent)
        if tmpl is None:
            continue
        out.append({
            "entity": ent,
            "pos": _entity_position(ent),
            "template": int(tmpl) & 0xFFFFFFFF,
        })
    return out



# Fallback safe XZ range, used only when a target island's real
# width/depth can't be determined (e.g. no matching universe/ILHD found).
# Positions in real 128-size saves cluster roughly 30-100; 20-110 is
# conservative for that specific island size.
_NPC_SAFE_XZ_MIN = 20.5
_NPC_SAFE_XZ_MAX = 110.5
_NPC_GRID_SPACING = 3.0
_NPC_LAYER_DY = 4.0  # stack layers upward when the grid is full

# When real width/height/depth ARE known (from the island's ILHD header),
# inset this many blocks from each edge/ceiling rather than using the
# fixed band above. Islands are generally void/water near their bounding
# box edges, so a fixed inset scales better across 128 / 256 / non-cube
# island sizes than a fixed absolute band tuned for one size.
_NPC_EDGE_MARGIN = 20.0
_NPC_CEILING_MARGIN = 12.0


def npc_safe_xz_band(width=None, depth=None):
    """Safe (x_min, x_max, z_min, z_max) for NPC placement.

    Uses an edge inset of the island's real width/depth when both are
    known; otherwise falls back to the fixed band tuned for 128 islands.
    """
    if width and width > 2 * _NPC_EDGE_MARGIN:
        x_min, x_max = _NPC_EDGE_MARGIN, width - _NPC_EDGE_MARGIN
    else:
        x_min, x_max = _NPC_SAFE_XZ_MIN, _NPC_SAFE_XZ_MAX
    if depth and depth > 2 * _NPC_EDGE_MARGIN:
        z_min, z_max = _NPC_EDGE_MARGIN, depth - _NPC_EDGE_MARGIN
    else:
        z_min, z_max = _NPC_SAFE_XZ_MIN, _NPC_SAFE_XZ_MAX
    return x_min, x_max, z_min, z_max


def npc_max_layers(origin_y, height=None):
    """How many +Y layers fit before hitting the island's ceiling.

    Returns None (no cap) when height isn't known.
    """
    if not height:
        return None
    headroom = height - _NPC_CEILING_MARGIN - float(origin_y)
    return max(1, int(headroom // _NPC_LAYER_DY) + 1)


def _clamp_xz(x, z, x_min=_NPC_SAFE_XZ_MIN, x_max=_NPC_SAFE_XZ_MAX,
              z_min=_NPC_SAFE_XZ_MIN, z_max=_NPC_SAFE_XZ_MAX):
    return (min(x_max, max(x_min, float(x))),
            min(z_max, max(z_min, float(z))))


def _npc_grid_positions(n, origin_x, origin_y, origin_z,
                         x_min=_NPC_SAFE_XZ_MIN, x_max=_NPC_SAFE_XZ_MAX,
                         z_min=_NPC_SAFE_XZ_MIN, z_max=_NPC_SAFE_XZ_MAX,
                         max_layers=None):
    """Lay out n points with unique (x,z) — never stack two on the same cell.

    Fills a compact grid around origin inside the safe band, then +Y layers.
    If the island height caps layers before all NPCs fit, spacing is tightened
    so every index still gets a distinct XZ (game + map both collapse
    identical positions into one visible NPC).
    """
    ox, oz = _clamp_xz(origin_x, origin_z, x_min, x_max, z_min, z_max)
    oy = float(origin_y)
    x_span = max(1.0, float(x_max) - float(x_min))
    z_span = max(1.0, float(z_max) - float(z_min))
    layers = max_layers if (max_layers and max_layers > 0) else 256
    layers = max(1, int(layers))

    # Start with preferred spacing; shrink until n fits with unique XZ cells.
    spacing = float(_NPC_GRID_SPACING)
    positions = []
    for _attempt in range(12):
        max_cols = max(1, int(x_span / spacing) + 1)
        max_rows = max(1, int(z_span / spacing) + 1)
        per_layer = max_cols * max_rows
        capacity = per_layer * layers
        if capacity >= n or spacing <= 1.0:
            cols = min(max_cols, max(1, int((min(n, per_layer)) ** 0.5) + 1))
            rows = max(1, min(max_rows, (min(n, per_layer) + cols - 1) // cols))
            per_layer = cols * rows
            positions = []
            used = set()
            for i in range(n):
                layer = min(i // per_layer, layers - 1)
                rem = i % per_layer
                # If past capacity, walk unique slots with a linear probe
                if i >= per_layer * layers:
                    rem = i % (cols * rows)
                    layer = layers - 1
                col = rem % cols
                row = rem // cols
                gx = ox + (col - (cols - 1) / 2.0) * spacing
                gz = oz + (row - (rows - 1) / 2.0) * spacing
                gx, gz = _clamp_xz(gx, gz, x_min, x_max, z_min, z_max)
                gy = oy + layer * _NPC_LAYER_DY
                key = (round(gx, 2), round(gz, 2), round(gy, 2))
                # Probe along +X then +Z for a free cell inside the band
                probe = 0
                while key in used and probe < 10000:
                    probe += 1
                    gx2 = gx + (probe % max_cols) * max(1.0, spacing * 0.5)
                    gz2 = gz + (probe // max_cols) * max(1.0, spacing * 0.5)
                    gx2, gz2 = _clamp_xz(gx2, gz2, x_min, x_max, z_min, z_max)
                    key = (round(gx2, 2), round(gz2, 2), round(gy, 2))
                    if key not in used:
                        gx, gz = gx2, gz2
                        break
                if key in used:
                    # last resort: nudge Y slightly so the triple is unique
                    gy = oy + layer * _NPC_LAYER_DY + (probe * 0.05)
                    key = (round(gx, 2), round(gz, 2), round(gy, 2))
                used.add(key)
                positions.append((gx, gy, gz))
            break
        spacing = max(1.0, spacing * 0.75)

    if len(positions) != n:
        # Absolute fallback: line along X at origin Z
        positions = []
        for i in range(n):
            gx = x_min + (i % max(1, int(x_span))) * 1.0
            gz = z_min + (i // max(1, int(x_span))) * 1.0
            gx, gz = _clamp_xz(gx, gz, x_min, x_max, z_min, z_max)
            gy = oy + (i // max(1, int(x_span * z_span))) * _NPC_LAYER_DY
            positions.append((gx, gy, gz))
    return positions



def _patch_entity_position_bytes(entity_doc_bytes, x, y, z):
    """Return a copy of an Entity document with Position x/y/z set."""
    buf = bytearray(entity_doc_bytes)
    nodes, _ = bson_parse(buf, 0)
    pos_node = None
    for n in _walk(nodes):
        if n.get("key") == "Position" and n.get("children"):
            pos_node = n
            break
    if pos_node is None:
        return bytes(entity_doc_bytes)
    for ch in pos_node.get("children") or []:
        if ch.get("key") in ("x", "y", "z") and ch.get("type") == 0x01:
            val = {"x": x, "y": y, "z": z}[ch["key"]]
            struct.pack_into("<f", buf, ch["vstart"], float(val))
    return bytes(buf)


def _encode_array_entity_element(index, entity_doc_bytes):
    """BSON array element: type 0x03 + key + 0 + document."""
    key = str(index).encode("utf-8")
    return bytes([0x03]) + key + b"\x00" + entity_doc_bytes


def extract_world_landing_pads(nodes):
    """List landing pads: Server LandingPad Component OR Landing Pad template.

    Many adventure islands only store TemplateCRC 0x0BCB9932 (no
    Server LandingPad Component string in the BKCK), so component-only
    detection misses them and the map looks empty of pads.
    """
    out = []
    seen = set()
    pad_crcs = landing_pad_template_crcs()
    for ent in iter_entities(nodes):
        has = False
        for n in _walk([ent]):
            if n["key"] == "Server LandingPad Component":
                has = True
                break
        tmpl = _entity_template_crc(ent)
        tcrc = (int(tmpl) & 0xFFFFFFFF) if tmpl is not None else None
        if not has and tcrc not in pad_crcs:
            continue
        pos = _entity_position(ent)
        key = (round(pos[0], 1), round(pos[2], 1)) if pos else id(ent)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "entity": ent,
            "pos": pos,
            "template": tmpl,
        })
    return out


def count_inventory_entities_in_doc(doc):
    """How many Entity nodes under this BKCK doc have Server Inventory."""
    if not doc or b"Server Inventory Component" not in doc:
        return 0
    # Fast path: count occurrences of the component name; each chest entity
    # has one. Over-counts slightly if the string appears outside entities,
    # but matches what the chest browser lists far better than "1 per BKCK".
    return doc.count(b"Server Inventory Component")


def classify_inventory_entity(chest):
    """Guess entity role from filled arrays + item categories.

    Returns (kind_label, counts_dict).
    Labels: mannequin, pet/mount box, pet stand, chest, trader stock,
    container, empty.

    Note: weapon stands and single-item IBP display props are both called
    "mannequin" — the game treats them the same way visually.
    """
    counts = {}
    cats = []
    for arr in chest.get("invs") or []:
        slots = inventory_slot_map(arr)
        counts[arr["key"]] = len(slots)
        for _si, entry in slots.items():
            fields = item_entry_fields(entry)
            ii = fields.get("II")
            if not ii:
                continue
            crc = ii["value"] if isinstance(ii, dict) else ii
            rec = item_record_for_crc(crc)
            cat = (rec or {}).get("category") or ""
            if cat:
                cats.append(cat)

    ibp = counts.get("IBP", 0)
    ieq = counts.get("IEQ", 0)
    veq = counts.get("VEQ", 0)
    pet_n = counts.get("PET", 0)
    total = sum(counts.values())

    n_mount = sum(1 for c in cats if c in ("Mounts", "Pets"))
    n_weapon = sum(1 for c in cats
                   if "Weapon" in c or c in ("Tools", "Spells/Skills"))
    n_armor = sum(1 for c in cats
                  if "Armor" in c or c in ("Vanity", "Vanity DLC", "Capes",
                                           "Ring"))

    # Inventory attached to a trader NPC entity → always trader stock
    tmpl = chest.get("template")
    if tmpl is not None and (int(tmpl) & 0xFFFFFFFF) in TRADER_TEMPLATE_CRCS:
        return "trader stock", counts

    # Armor/vanity display mannequin (multi-slot equip / vanity)
    if ieq >= 2 or veq >= 2 or (ieq >= 1 and n_armor >= 1):
        return "mannequin", counts
    # Single weapon / tool / small display prop (was "weapon stand") —
    # treat as mannequin. Also covers 1× IBP props that aren't real chests.
    if total <= 2 and n_weapon >= 1 and n_mount == 0:
        return "mannequin", counts
    if total == 1 and ibp == 1 and n_mount == 0 and ieq == 0:
        # Lone IBP(1) is almost always a display stand, not a player chest
        return "mannequin", counts
    if pet_n >= 1 and ibp == 0 and ieq == 0:
        return "pet stand", counts
    # Only call it pet/mount box when items are actually mounts/pets
    if n_mount >= 1 and ieq == 0 and ibp <= 8:
        return "pet/mount box", counts
    # Player chests ~40 IBP max; larger stacks are NPC trader stock
    if ibp > 40:
        return "trader stock", counts
    if ibp >= 5:
        return "chest", counts
    if ibp >= 1 and n_mount == 0 and ieq == 0:
        return "chest", counts
    if total == 0:
        return "empty", counts
    return "container", counts


def treeview_enable_sort(tree, numeric_cols=None):
    """Click a heading to toggle ascending/descending sort.

    numeric_cols: set of column ids to sort as numbers (not text).
    """
    numeric_cols = set(numeric_cols or ())
    state = {"col": None, "desc": False}

    def sort_by(col):
        rows = [(tree.set(k, col), k) for k in tree.get_children("")]
        if state["col"] == col:
            state["desc"] = not state["desc"]
        else:
            state["col"] = col
            state["desc"] = False
        desc = state["desc"]

        def key_fn(item):
            val = item[0]
            if col in numeric_cols:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return float("inf") if not desc else float("-inf")
            return _s(val).lower()

        rows.sort(key=key_fn, reverse=desc)
        for i, (_v, k) in enumerate(rows):
            tree.move(k, "", i)
        # Arrow in heading
        for c in tree["columns"]:
            base = tree.heading(c, "text")
            base = base.replace(" ▲", "").replace(" ▼", "")
            if c == col:
                base = base + (" ▼" if desc else " ▲")
            tree.heading(c, text=base)

    for col in tree["columns"]:
        tree.heading(col, command=lambda c=col: sort_by(c))


def inventory_slot_map(array_node):
    """Map SI -> item-entry node for an IBP/IAB/IEQ/VEQ/PET array."""
    out = {}
    if not array_node or not array_node.get("children"):
        return out
    for entry in array_node["children"]:
        if not entry.get("children"):
            continue
        si = None
        for ch in entry["children"]:
            if ch["key"] == "SI":
                si = ch["value"]
                break
        if si is not None:
            out[int(si)] = entry
    return out


def item_entry_fields(entry_node):
    """Return dict with keys II, SC, PI, SI nodes (present ones only)."""
    fields = {}
    if not entry_node or not entry_node.get("children"):
        return fields
    for ch in entry_node["children"]:
        if ch["key"] in ("II", "SC", "PI", "SI"):
            fields[ch["key"]] = ch
    return fields


def parse_recipe_ids(binary_value):
    """knownRecipeIds is a packed sequence of little-endian CRC32s."""
    if not binary_value:
        return []
    data = bytes(binary_value)
    ids = []
    for i in range(0, len(data) - 3, 4):
        ids.append(struct.unpack_from("<I", data, i)[0])
    return ids


def _snappy_try(raw):
    """Return (decompressed_bytes, offset_used) or raise."""
    if raw[:4] == b"SNPY":
        payload = raw[4:]
        last_err = None
        for off in (0, 4, 8, 12):
            if off >= len(payload):
                break
            try:
                return snappy_decompress(payload[off:]), off
            except Exception as ex:
                last_err = ex
        if last_err:
            raise last_err
    return snappy_decompress(raw), 0


def _collect_quest_entries(nodes, out, path=""):
    """Walk a parsed QB BSON tree and collect quest-like records.

    Looks for documents that carry a QID (quest id hash) and optional
    QS / QuestState, location strings, and any human-readable strings.
    """
    if not nodes:
        return
    for n in nodes:
        key = n.get("key")
        children = n.get("children")
        if children is not None:
            # Is this document itself a quest entry?
            fields = {c["key"]: c for c in children if isinstance(c, dict)}
            if "QID" in fields or (key in ("QA", "") and "QS" in fields):
                entry = {"path": path + "/" + (key or "")}
                qid = fields.get("QID")
                if qid is not None and qid.get("value") is not None:
                    entry["qid"] = int(qid["value"]) & 0xFFFFFFFF
                # Quest state: QS is often a doc {T, V} with V = "Finalized" etc.
                qs = fields.get("QS")
                if qs and qs.get("children"):
                    t = v = None
                    for ch in qs["children"]:
                        if ch["key"] == "T":
                            t = ch.get("value")
                        elif ch["key"] == "V":
                            v = ch.get("value")
                    if isinstance(v, (bytes, bytearray)):
                        v = bytes(v).split(b"\x00")[0].decode("utf-8", "replace")
                    entry["state"] = v or t
                # Flatten any string children as labels
                strings = []
                for ch in children:
                    val = ch.get("value")
                    if ch.get("type") == 0x02 and isinstance(val, str):
                        strings.append(val)
                    elif isinstance(val, (bytes, bytearray)):
                        try:
                            s = bytes(val).split(b"\x00")[0].decode("utf-8")
                            if s and s.isprintable():
                                strings.append(s)
                        except Exception:
                            pass
                if strings:
                    entry["strings"] = strings
                # location
                loc = fields.get("location")
                if loc is not None and isinstance(loc.get("value"), str):
                    entry["location"] = loc["value"]
                out.append(entry)
            _collect_quest_entries(children, out, path + "/" + (key or ""))


def decode_quest_blob(qb_binary):
    """Decompress and parse the Quest Component QB field.

    Returns (ok, info_dict) where info_dict includes:
      raw_len, magic, decompressed_len, offset, quests (list of dicts),
      and on failure: error.
    """
    if not qb_binary:
        return False, {"error": "empty"}
    raw = bytes(qb_binary)
    info = {"raw_len": len(raw), "magic": raw[:4], "quests": []}
    try:
        dec, off = _snappy_try(raw)
        info["decompressed_len"] = len(dec)
        info["offset"] = off
        info["preview"] = dec[:64].hex()
        # Decompressed payload is a BSON document
        try:
            nodes, total = bson_parse(dec)
            info["bson_ok"] = True
            info["bson_total"] = total
            quests = []
            _collect_quest_entries(nodes, quests)
            info["quests"] = quests
            # Also collect every readable string as a fallback index
            strings = []
            for n in _walk(nodes):
                val = n.get("value")
                if n.get("type") == 0x02 and isinstance(val, str) and len(val) > 1:
                    strings.append(val)
            info["strings"] = strings
        except Exception as ex:
            info["bson_ok"] = False
            info["bson_error"] = str(ex)
        return True, info
    except Exception as ex:
        info["error"] = str(ex)
        return False, info


# Class-specific talent trees. selection index 0..n-1 matches the order
# listed here (same order the game presents them).
TALENT_TREES = {
    "Ranger": {
        2: ["Bow Specialization", "Crossbow Specialization", "Sling Specialization"],
        5: ["Sentry Stance", "Fading"],
        10: ["Dodge Chance", "Evasive Maneuver"],
        15: ["Potion Mastery"],
        20: ["Orb Thief", "Dodger", "Unstable"],
        25: ["Sharp Shooter", "Cheat Death", "In the Face of Evil"],
        30: ["Combo Swing", "Exploit Weakness", "Survival Instincts"],
    },
    "Warrior": {
        2: ["Axe Specialization", "Hammer Specialization", "Sword Specialization"],
        5: ["Eye for an Eye", "Fortification", "Commander"],
        10: ["Adrenaline Rush", "Last Stand"],
        15: ["Aggression", "Determination"],
        20: ["Chain Attacks", "Echo"],
        25: ["Finishing Blow", "Tank Mode", "Empowered Lung"],
        30: ["Combo Swing", "Hardened Armor", "Eternal Rage"],
    },
    "Mage": {
        2: ["Staff Specialization", "Wand Specialization", "Scythe Specialization"],
        5: ["Mana Shield", "Frost Armor"],
        10: ["Master of Elements", "Trascendence"],
        15: ["Spell Rush", "Spell Crush"],
        20: ["Meditation", "Summoning of the Orbs", "Mana Thief"],
        25: ["Magic Armor", "Impact Armor", "Refreshment"],
        30: ["Arcane Concentration", "Illusion", "Blood Magic"],
    },
    "Rogue": {
        2: ["Dagger Expert", "Claws Expert", "Blade-staff Expert"],
        5: ["Stealth Master", "Covering Mist"],
        10: ["Poisoned Blades", "Blood Magic", "Void Magic"],
        15: ["Envenom", "From the Shadows", "Sunder Armor"],
        20: ["Critical Perception", "Self-Care"],
        25: ["Cornered Prey", "Robust Constitution", "Cheat Death"],
        30: ["Fleet of Foot", "Wall of Knives", "Bushwhacker"],
    },
    "Druid": {
        2: ["Sickle Specialization", "Staff Specialization", "Scythe Specialization"],
        5: ["Beast of Prey", "Warden of the Woods", "Unleash the Beast"],
        10: ["Ferocity", "Predatory Bite", "Pact With Nature"],
        15: ["Prowl", "Thick Hide", "Pack Heal"],
        20: ["Feline Focus", "Fang of Elysia", "Unmerciful Tide"],
        25: ["Animal Frenzy", "Agile Predator", "Entangle"],
        30: ["Apex Predator", "Threat Display", "Spiritual Calm"],
    },
}


# Categories for vanity slot pickers (full set of cosmetic pieces).
VANITY_SLOT_CATEGORIES = {
    0: VANITY_CATEGORIES,  # Head
    1: VANITY_CATEGORIES,  # Torso
    2: VANITY_CATEGORIES,  # Gloves
    3: VANITY_CATEGORIES,  # Legs
    4: CAPE_CATEGORIES + VANITY_CATEGORIES,  # Cape
    5: RING_CATEGORIES + VANITY_CATEGORIES,  # Ring
    6: VANITY_CATEGORIES,  # Extra Head (cosmetic 2nd hat/hair)
}

# Armor slots 0–5 only. Extra Head is shown on the Vanity tab.
ARMOR_SLOT_CATEGORIES = {
    0: ARMOR_CATEGORIES,
    1: ARMOR_CATEGORIES,
    2: ARMOR_CATEGORIES,
    3: ARMOR_CATEGORIES,
    4: CAPE_CATEGORIES,
    5: RING_CATEGORIES,
}

# Placeable into backpack / hotbar / pets: almost everything identified
# that isn't pure recipe/unobtainable noise.
BAG_PLACE_CATEGORIES = None  # None = all placeable items
PET_PLACE_CATEGORIES = PET_CATEGORIES + MOUNT_CATEGORIES


# ----------------------------------------------------------------------
# Attribute names - SOLVED, from the game's own attribute dump
#
# The user supplied attr_log.txt: 217 name -> hash pairs straight out of
# the game. Every single one is reproducible, which proves the convention:
#
#     hash = crc32( lowercase( "Parent.Child" ) )
#
# Verified 218/218. Examples:
#     crc32(lower("Health"))               = 0xCEDA2313
#     crc32(lower("Health.Max"))           = 0x7C323E60
#     crc32(lower("Health.Max.Adder"))     = 0x6401BFE1
#     crc32(lower("PlayerIncreasedStrength")) = 0x901AAAEA
#
# That last one also explains why ~50,000 brute-force candidates failed
# earlier: the real key is "PlayerIncreasedStrength", not "Strength" or
# "STR" or any dotted variant of them. The prefix was unguessable.
#
# It independently confirms the user's in-game corrections (CON/STR/WIS/
# INT/AGI/DEX) and our arithmetic-derived roles for the stat arrays -
# what I called "factor" is really "Multiplier".
#
# pk_attr_names.json holds all 218. Loaded at runtime so the file can be
# extended without touching code.
#
# Two parsing traps, both hit while resolving the tree:
#   * The log repeats itself - 1303 lines, only 218 distinct hashes.
#   * '_' is part of a name component ("Mount_MovementSpeed"), NOT a
#     separator. Splitting on it left six entries unresolved.
# All 218 names are EMBEDDED below rather than loaded from a side file.
#
# They were previously read from pk_attr_names.json, which existed in the
# development workspace but was never shipped to the user - so every
# lookup silently fell back to the 12 hand-entered names and the other 206
# vanished with no error. A tool that degrades silently when a data file
# is missing is worse than one that fails loudly; embedding removes the
# failure mode entirely.
#
# An optional pk_attr_names.json is still honoured if present, so new
# names (e.g. an item-side dump) can be added without editing code.
ATTR_NAME_FILE = "pk_attr_names.json"

ATTR_NAMES_BUILTIN = {
    0x04F72BDC: "DamageSpell",
    0x055C0711: "Agility.Adder",
    0x0590C103: "Experience",
    0x0AC0EE58: "Incoming_Healingmultiplier",
    0x0EBC6C84: "Cooldown_CheatDeath",
    0x1155C7AA: "Incoming_ManaRestore_Multiplier",
    0x116A5CF3: "Cooldown_LowHealthIlluision",
    0x11C44C67: "Mana.Regeneration",
    0x11C8546C: "Damage",
    0x11F20F99: "Cooldown_LowHealthArmor",
    0x139E0EB6: "Armor.Adder",
    0x1475F38F: "Outgoing_Healingmultiplier",
    0x15674706: "GravityMod.Min",
    0x179646E7: "Mana.Max.Multiplier",
    0x17BFD33B: "Cooldown_Skill_Shapeshift_Modifier",
    0x1907AB1C: "Experience.Required",
    0x1A80FCDC: "Constitution.Adder",
    0x1B0DA612: "CharacterLevel.Max",
    0x1B2F9DF0: "DamageMelee",
    0x1E884FAB: "Cooldown_Spell_Astral",
    0x1F33EF0C: "FallDamage",
    0x20967DF3: "Mount_MovementSpeed.Adder",
    0x237B2D10: "Cooldown_Flask",
    0x23EC451C: "LowHealthHeal",
    0x240B263C: "DamageSchoolSusceptibility.DamageThunder",
    0x249B5A22: "Cooldown_Flask_Modifier",
    0x24D22962: "MaxFallSpeedMod",
    0x25AB9C28: "Health.Max.Multiplier",
    0x25B3DF63: "Shout_Manacost_Reduction",
    0x26A6833A: "Cooldown_Shapeshift_Ability2",
    0x27772F52: "Outgoing_DamageMultiplier",
    0x28159627: "DamageMultiplier",
    0x29F2DAC5: "Oxygen.Max",
    0x2C52E277: "Stealth_Duration.Multiplier",
    0x2C5BCC81: "Leech.Chance",
    0x2CEBB457: "DamageOverTime_CriticalStrike",
    0x2EE3FBE4: "Intelligence.Adder",
    0x2F306AC1: "Mount_State",
    0x2F55D9AA: "Cooldown_Mount",
    0x311901A6: "Cooldown_Rogue_Skills",
    0x31B6EA3A: "DamageRanged.Base",
    0x31E03C56: "MovementSpeed.Multiplier",
    0x31FCFDFF: "Armor_Enemy_Damage.Chance",
    0x3275DA8F: "Armor_Enemy_Damage",
    0x34217C49: "Multistrike.Chance",
    0x34E30709: "Cooldown_Spell_Modifier",
    0x3572D58D: "Manabarcontainer",
    0x3972D02A: "DamageMelee.Multiplier",
    0x39DD5800: "MovementSpeed_Percentage",
    0x39FCBE30: "LowManaRestore",
    0x3AFD70D3: "CheatDeath",
    0x3B2AC149: "Wisdom.Adder",
    0x3BB8DD4B: "DamageSchoolSusceptibility.DamageNormal",
    0x3C0D4867: "MovementSpeed.Base",
    0x3C58F6E2: "DamageSpell.Multiplier",
    0x3FBA3BEB: "Cooldown_LowHealthDodge",
    0x4036A4C9: "GravityMod",
    0x42C79B00: "Agility.Base",
    0x44105BF8: "Mount_MovementSpeed.Max",
    0x44BF38D3: "Constitution.Base",
    0x4961FCBA: "Intelligence.Base",
    0x49BCB141: "DamageRanged.Multiplier",
    0x4A36C689: "Cooldown_Rogue_Shadowstep",
    0x4BE05598: "Stealth",
    0x4D405C66: "PlayerIncreasedDexterity",
    0x4D6D1028: "DamageSchoolSusceptibility.DamageBlunt",
    0x4D787E5E: "ExperienceBonusMultiplier",
    0x4F847974: "DamageTypeSusceptibility.DamageMelee",
    0x4F8F4950: "Shapeshift_State",
    0x505CCF58: "DamageTypeSusceptibility.DamageSpell",
    0x5199DD12: "HealthRegenerationAbsolute",
    0x51A1B3AC: "Cooldown_Shapeshift_Ability3",
    0x52E440C9: "Cooldown_Spell_Fire",
    0x55A5FEA8: "DamageSchoolSusceptibility",
    0x587C995B: "DamageSusceptibility",
    0x59CD7F73: "Intelligence.Multiplier",
    0x5A7618E7: "Wisdom.Base",
    0x5ABC7540: "Stealth_MovementSpeed",
    0x5C319F5C: "Shapeshift_Duration.Display",
    0x5D89597E: "Shapeshift_DamageMultiplier",
    0x5DEFAE8C: "DamageSchoolSusceptibility.DamageAstral",
    0x5E7B0A62: "Ambush_DamageMultiplier",
    0x5F9086E6: "ManaSteal.Chance",
    0x5FF90C39: "DamageOverTime_Duration_Multiplier",
    0x60D64632: "Mana",
    0x612A63D6: "Shout_Multiplier",
    0x617771FB: "DamageTypeSusceptibility.DamageRanged",
    0x62100ADE: "Miss",
    0x6394EAB5: "ManaSteal",
    0x63B74BD5: "GravityMod.Adder",
    0x63F8BEE4: "WeaponType",
    0x6401BFE1: "Health.Max.Adder",
    0x65027F8F: "Agility",
    0x65BAABA5: "DamageSchoolSusceptibility.DamageSharp",
    0x66249156: "Mount_MovementSpeed",
    0x66AE3312: "Dexterity.Adder",
    0x68ED562C: "Oxygen",
    0x6A214C83: "MiningDamage.Adder",
    0x6CDEBE1A: "BombMultiplier",
    0x6DA2C7F9: "Dexterity.Multiplier",
    0x6E747F0C: "Shapeshift_Duration_Multiplier",
    0x72566DAA: "Health.Regeneration",
    0x73AEBB44: "Cooldown_Skill_Shapeshift",
    0x7480B8DE: "Health.Max.Base",
    0x76F628D8: "Cooldown_Spell_Holy",
    0x778A2FFD: "Strength.Adder",
    0x7B64715E: "Cooldown_Spell_Electric",
    0x7B99548C: "Stealth_Duration.Display",
    0x7C323E60: "Health.Max",
    0x7C725BB8: "JumpHeight",
    0x7E529719: "LowHealthIlluision",
    0x81211AC1: "DamageSchoolSusceptibility.DamageFire",
    0x814CA57E: "MiningSpeed",
    0x85D1AEAD: "Dexterity",
    0x865230D4: "Cooldown_HealingReceived",
    0x87C7DB12: "Stealth_Duration.Base",
    0x88883771: "DurabilityLossMultiplier",
    0x8A2DE1F7: "MovementSpeed",
    0x8A85E17C: "Multistrike.AdditionalStrikes",
    0x8BAC6AD2: "ElementMatch_Multiplier",
    0x8BF69D26: "DamageRanged",
    0x8DA4EF23: "Cooldown_Compass",
    0x8E302D99: "ElementMatch_Override",
    0x8F865A33: "Mount_MovementSpeed.Multiplier",
    0x8FFBE8D6: "MiningDamage.Base",
    0x901AAAEA: "PlayerIncreasedStrength",
    0x916FCA8E: "Dodge",
    0x9251AE98: "Mana.Max.Adder",
    0x9289E5A3: "DamageOverTime_Fire_DamageMultiplier",
    0x9388F2C6: "Strength.Base",
    0x938F2B5A: "Criticalstrike.Chance",
    0x939890C0: "Incoming_Damagemultiplier",
    0x945AD2A6: "Armor_Percentage",
    0x9630DD5B: "Cooldown_Rogue_Shadowstep_Modifier",
    0x9AC2E867: "LowHealthArmor",
    0x9B7CAA14: "PlayerIncreasedWisdom",
    0x9C67B6F0: "Armor.Multiplier",
    0x9CC8A62A: "RemainingPlayerIncreasedAttributes",
    0x9D478CCE: "DamageOverTime_Poison_DamageMultiplier",
    0x9D4858D2: "MovementSpeed.Max",
    0x9DB49BDD: "Constitution",
    0x9DB79A0B: "Mana.Max.Base",
    0xA10B1E03: "DamageMelee.Adder",
    0xA1CCC259: "PlayerIncreasedConstitution",
    0xA22B849C: "Stealth_Duration",
    0xA36E8B7F: "DamageMelee.BonusMod",
    0xA4E1FF1E: "Criticalstrike",
    0xA4F03203: "Constitution.Multiplier",
    0xA53372D0: "DamageSchoolSusceptibility.DamageHoly",
    0xADB35146: "Cooldown_LowHealthHeal",
    0xADDB4D05: "Agility.Multiplier",
    0xAFAD5348: "Fly_State",
    0xAFB095DA: "GravityMod.Base",
    0xAFF73420: "PlayerIncreasedAgility",
    0xB174B2FC: "Strength.Multiplier",
    0xB236C9AA: "Cooldown_Food",
    0xB2CC59CE: "Cooldown_LowManaRestore",
    0xB2F6D957: "ManaRegenerationAbsolute",
    0xB48ADC15: "LowHealthDodge",
    0xB6539797: "Heartcontainer",
    0xB6688613: "Shapeshift_Duration_Adder",
    0xB69F411D: "Dexterity.Base",
    0xB6F25D33: "DamageSpell.Base",
    0xB77B3E13: "Mana.Max",
    0xBA7853C1: "DamageSpell.Adder",
    0xBD0FA501: "DamageSchoolSusceptibility.DamageCutting",
    0xBD9ED884: "DamageSpell.BonusMod",
    0xBF27FEFC: "Armor",
    0xBFAFD280: "Cooldown_Shapeshift_Ability1",
    0xC323AFD2: "DamageRanged.Adder",
    0xC387368E: "Cooldown_Spell_Curse",
    0xC41AE466: "DamageMelee.Base",
    0xC47B3E9D: "Experience.Lifetime",
    0xC4F83765: "Cooldown_Totem",
    0xC583EE24: "Cooldown_Spell",
    0xCB7AF4C2: "ManaShield",
    0xCEDA2313: "Health",
    0xD033A890: "CharacterLevel",
    0xD0C14680: "Multistrike",
    0xD112A18D: "Cooldown_Shout_Modifier",
    0xD17982FA: "DamageSchoolSusceptibility.DamageCursed",
    0xD4F58FE5: "Wisdom",
    0xD5BDB2A5: "Leech",
    0xD5C63D33: "Shapeshift_Duration",
    0xD5D525CC: "Cooldown_Shout",
    0xD6B9E1D8: "Strength",
    0xD6F43939: "MovementSpeed.Adder",
    0xD7D8E6C3: "Intelligence",
    0xD8819F6E: "Cooldown_Trap",
    0xDB3BC80E: "Criticalstrike.DamageMultiplier",
    0xDE7DB7D3: "Shapeshift_Manacost_Reduction",
    0xDF4E6535: "AmbushOverride",
    0xE02CE52F: "Cooldown_RiftStability",
    0xE20656E7: "DamageSchoolSusceptibility.DamageIce",
    0xE30B6A70: "Cooldown_Rogue_Skills_Modifier",
    0xE349ACE6: "DamageSchoolSusceptibility.DamagePoison",
    0xE5A917C2: "Cooldown_Potion",
    0xE727774A: "Wisdom.Multiplier",
    0xE74EE319: "DamageRanged.BonusMod",
    0xE76FD9B2: "Mount_MovementSpeed.Base",
    0xE7AD2408: "WeaponSpecializationMultiplier",
    0xE882D62F: "Cooldown_Spell_Nature",
    0xE8D6AE27: "DoubleJump",
    0xEBA0BF47: "PlayerIncreasedIntelligence",
    0xEC0179DF: "Armor.Base",
    0xED186484: "BackstabMultiplier",
    0xEF2E32BC: "DamageTypeSusceptibility",
    0xF18A93ED: "Cooldown_Potion_Modifier",
    0xF1A8A580: "Cooldown_Spell_Ice",
    0xF2253525: "MiningDamage",
    0xF4446A22: "MiningDamage.Multiplier",
    0xF6207619: "Stealth_Duration.Adder",
    0xF69C17FB: "GravityMod.Multiplier",
    0xF6C50707: "DamageSchoolSusceptibility.DamageDaemonic",
    0xFB72BE86: "SelfInflicted_Healingmultiplier",
    0xFD64FB03: "WeaponSpecializationMultiplier.WeaponSpecialization_Type",
    0xFF0B7DCD: "JumpHeight.Max",
    0xFF6310DE: "Execute",
    0x0FB35231: "Armor_Enemy_DamageMultiplier",
    0x114E3F33: "DamageSharp",
    0x197F4704: "ExperienceScalingMultiplier",
    0x22A01FAE: "DamageNormal",
    0x390326F8: "SizeScalingMultiplier",
    0x399984BE: "DamageBlunt",
    0x44F76C69: "DamageAstral",
    0x50490F12: "Armor_Enemy",
    0x6D76AB34: "DamageCutting",
    0x7B56A0CA: "DamageIce",
    0x804FC7FB: "Armor_Enemy.Max",
    0x8839EE99: "Durability",
    0xA0A6BA2A: "DamageDaemonic",
    0xC0A60B47: "EnemyArmor_DamageMultiplier",
    0xC4671642: "DamageFire",
    0xC861401F: "DamageCursed",
    0xD9DAE4A8: "BonusMod",
    0xE0757E53: "DamageHoly",
    0xEC99EC53: "DamageScalingMultiplier",
    0xF4722809: "DamageThunder",
    0xFA516E03: "DamagePoison",
}

_ATTR_NAMES = None


def attr_names():
    """hash -> "Parent.Child" name, for all known attribute keys."""
    global _ATTR_NAMES
    if _ATTR_NAMES is None:
        _ATTR_NAMES = dict(ATTR_NAMES_BUILTIN)
        try:
            with open(_here(ATTR_NAME_FILE), "r", encoding="utf-8") as fh:
                _ATTR_NAMES.update({int(k): v
                                    for k, v in json.load(fh).items()})
        except Exception:
            pass
    return _ATTR_NAMES


# Names confirmed by other means, kept as a fallback when the JSON is
# missing. "durability" is corroborated by the dump's own vocabulary:
# crc32(lower("Durability")) == 0xC764ED49.
AV_KNOWN = {
    "ceda2313": "Health",
    "60d64632": "Mana",
    "0590c103": "Experience",
    "c764ed49": "Durability",
    "d033a890": "CharacterLevel",
    "a1ccc259": "PlayerIncreasedConstitution",
    "901aaaea": "PlayerIncreasedStrength",
    "9b7caa14": "PlayerIncreasedWisdom",
    "eba0bf47": "PlayerIncreasedIntelligence",
    "aff73420": "PlayerIncreasedAgility",
    "4d405c66": "PlayerIncreasedDexterity",
    "e02ce52f": "Cooldown_RiftStability",
}

# Short labels for the six attributes and the points pool, so the tree
# reads "STR" rather than "PlayerIncreasedStrength".
ATTR_SHORT = {
    "PlayerIncreasedConstitution": "CON",
    "PlayerIncreasedStrength": "STR",
    "PlayerIncreasedWisdom": "WIS",
    "PlayerIncreasedIntelligence": "INT",
    "PlayerIncreasedAgility": "AGI",
    "PlayerIncreasedDexterity": "DEX",
    "RemainingPlayerIncreasedAttributes": "attribute points",
    "CharacterLevel": "level",
}


def av_label(hexkey):
    """Readable label for a hashed attribute key, or None."""
    k = hexkey.lower()
    full = attr_names().get(int(k, 16)) or AV_KNOWN.get(k)
    if not full:
        return None
    short = ATTR_SHORT.get(full)
    return "%s  [%s]" % (short, full) if short else full


def stat_field_label(crc):
    """Name for an N field inside a stat array."""
    full = attr_names().get(int(crc))
    if full:
        return ATTR_SHORT.get(full, full)
    return AV_KNOWN.get("%08x" % int(crc))


# ----------------------------------------------------------------------
# Plain-English field names
#
# The format uses two- and three-letter keys everywhere. Confirmed
# meanings, with how each was established:
#
#   C   Coins           - the save read C = 1598 while the HUD showed
#                         1,598 gold. Direct observation.
#   AC  Defender Coins  - the second currency, confirmed by the user.
#   II  Item            - holds an item hash.
#   SC  Stack           - how many, a uint16 so 0-65535.
#   SI  Slot            - the slot index; hidden by default because the
#                         row label already says which slot it is.
#
# Both currencies are uint32 (BSON 0x14), so 99,986,140 and 99,999,999
# fit with room to spare. They were never missing - just unlabelled, and
# easy to scroll past as "C" and "AC".
FIELD_LABELS = {
    "C": "Coins",
    "AC": "Defender Coins",
    "II": "Item",
    "SC": "Stack",
    "SI": "Slot",
    "PI": "Item data",
    "N": "Stat",
    "V": "Value",
    "dCRC": "type id",
    "slotId": "character slot",
    "guid": "unique id",
    "TLSD": "Talents",
    "SUL": "Skills",
    "IBP": "Backpack",
    "IAB": "Action bar",
    "IEQ": "Armor",
    "VEQ": "Vanity",
    "PET": "Pets",
    "QB": "Quest data",
    "GG": "GG",
}


def field_label(key):
    """Plain-English name for a short engine key, or None."""
    return FIELD_LABELS.get(key)


# Sections that exist in the format but do nothing useful for a
# single-player save. Flagged in the tree so time isn't wasted on them.
SECTION_NOTES = {
    "Server Inventory Component":
        "mirror of Player Inventory, only used by a dedicated server",
    "Quest Component":
        "nested SNPY blob, not decoded",
    "price":
        "cosmetic cost record, editing it has no effect in game",
}

# Sections worth collapsing by default: bulky, rarely edited, or mirrors
# of data shown elsewhere.
COLLAPSE_BY_DEFAULT = {
    "Server Inventory Component",   # mirrors Player Inventory Component
    "CreationParameter",            # spawn position/orientation
    "PlayerCustomizationSelectorCRCs",
    "Quest Component",
    "Server Crafting Component",
    "RecipeKnowledgeList",
}
# NOT collapsed: "Impact Component" (shown as "Player Stats") holds the
# attributes. Collapsing it last turn hid the very labels that had just
# been decoded - they rendered correctly but were never on screen.

# Friendlier names for engine-internal component keys.
SECTION_RENAMES = {
    "Impact Component": "Player Stats",
    "AV": "Attributes",
}

# The six attributes in the order the game's own character sheet lists
# them. Anything not in this list keeps document order and sorts after.
ATTRIBUTE_ORDER = ["CON", "STR", "AGI", "DEX", "WIS", "INT"]

# Fields that carry no useful information in the tree.
HIDE_KEYS = {"SI"}          # slot index - already shown in the row label


ARRAY_LABELS = dict(EQUIP_ARRAYS)
ARRAY_LABELS.update(BAG_ARRAYS)

# AV stat blocks are stored in whatever order the engine serialised them,
# which reads as random noise in a tree view. Sorting by resolved name puts
# the ones we can name (health, mana, ...) first and alphabetises the rest,
# so the same stat is always in the same place between characters.
SORTED_CONTAINERS = {"AV"}


def display_order(nodes, array_key=None):
    """Row order for a container's children.

    Equipment and inventory arrays stay in SLOT order (SI), because slot
    number is meaningful - Helmet is always slot 0. Everything else in a
    stat block is sorted by name. Document order is preserved otherwise:
    reordering the file itself would change the bytes for no reason.
    """
    if array_key in EQUIP_ARRAYS or array_key in BAG_ARRAYS:
        def slot_of(n):
            if n["children"]:
                for ch in n["children"]:
                    if ch["key"] == "SI":
                        return ch["value"]
            return 1 << 30
        return sorted(nodes, key=slot_of)
    if nodes and all(is_hex_key(n["key"]) for n in nodes):
        # Attribute block order, requested explicitly:
        #   DEX, STR, WIS, CON, INT first, then everything else, with
        #   Cooldown_RiftStability pinned last because it is unrelated
        #   noise that happens to live in the same document.
        def rank(n):
            full = attr_names().get(int(n["key"], 16), "")
            short = ATTR_SHORT.get(full, "")
            if short in ATTRIBUTE_ORDER:
                return (0, ATTRIBUTE_ORDER.index(short), "")
            if full == "Cooldown_RiftStability":
                return (3, 0, "")
            label = crc_label(int(n["key"], 16)) or full
            return (1 if label else 2, 0, label or n["key"])
        return sorted(nodes, key=rank)
    return nodes


def is_hex_key(key):
    return (len(key) == 8
            and all(c in "0123456789abcdefABCDEF" for c in key))


def pretty_path(path):
    """Human-readable path. Internal paths carry a positional suffix so
    every node is unique (see bson_parse); strip it for display, keeping
    the index only where it carries meaning (array elements)."""
    out = []
    for step in path:
        m = re.match(r"^(.*)\[(\d+)\]$", step)
        if not m:
            out.append(step)
            continue
        key, idx = m.group(1), m.group(2)
        out.append(key if key else "[%s]" % idx)
    return ".".join(out)


def row_label(node, array_key=None):
    """Field-column text: resolve hashed keys and slot numbers to names."""
    key = node["key"]
    if is_hex_key(key):
        name = crc_label(int(key, 16)) or av_label(key)
        if name:
            return "%s  (%s)" % (name, key)
    # An array element. Real saves store these with an EMPTY key; some
    # arrays use "0","1",... instead. Handle both - checking only for the
    # empty case silently skipped slot naming on digit-keyed arrays.
    if node["children"] and (not key or key.isdigit()):
        si = ii = None
        for ch in node["children"]:
            if ch["key"] == "SI":
                si = ch["value"]
            elif ch["key"] == "II":
                ii = ch["value"]
        # A {N, V} pair inside a stat array - name it from N so the row
        # reads "health base = 680" instead of "(item) (2 fields)".
        nn = next((c["value"] for c in node["children"]
                   if c["key"] == "N"), None)
        vv = next((c["value"] for c in node["children"]
                   if c["key"] == "V"), None)
        if nn is not None:
            lbl = stat_field_label(nn)
            if lbl:
                return ("%s = %g" % (lbl, vv)) if vv is not None else lbl
        if si is not None:
            nm = slot_label(array_key, si)
            head = "[%d] %s" % (si, nm) if nm else "slot %d" % si
            item = item_name_for_crc(ii) if ii is not None else None
            return "%s - %s" % (head, item) if item else head
        return key or "(item)"
    if key in SECTION_RENAMES:
        return "%s  [%s]" % (SECTION_RENAMES[key], key)
    note = SECTION_NOTES.get(key)
    if note:
        return "%s  -- %s" % (key, note)
    plain = field_label(key)
    if plain:
        return "%s  [%s]" % (plain, key) if plain != key else key
    if key in ARRAY_LABELS:
        return "%s  (%s)" % (key, ARRAY_LABELS[key])
    return key



class BsonUnknownType(ValueError):
    def __init__(self, etype, key, offset):
        self.etype = etype
        self.key = key
        self.offset = offset
        super().__init__(
            "unsupported BSON type 0x%02X for key %r at offset %d - "
            "refusing to guess its length" % (etype, key, offset))


def bson_parse(buf, base=0, chain=None, path=()):
    """Parse one BSON document embedded in buf starting at offset base.
    Returns (nodes, total_len). Recurses into embedded documents/arrays."""
    if chain is None:
        chain = [base]
    total_len = struct.unpack_from("<i", buf, base)[0]
    pos = base + 4
    end_of_doc = base + total_len
    nodes = []
    while pos < end_of_doc - 1:
        etype = buf[pos]
        key_start = pos + 1
        nul = buf.index(b"\x00", key_start)
        key = buf[key_start:nul].decode("utf-8", "replace")
        vstart = nul + 1
        # Path component must be unique among siblings. Two things break
        # that: array elements have an EMPTY key, and some documents repeat
        # a key outright (a real save has "ICS" twice in the same parent).
        # Both get the element's position appended, so a path always
        # identifies exactly one node.
        seen_key = key if key else ""
        step = "%s[%d]" % (seen_key, len(nodes))
        # estart marks the type-byte of this element so it can be removed
        # whole from its parent array/document.
        node = {"key": key, "type": etype, "chain": list(chain),
                "index": len(nodes), "path": path + (step,),
                "estart": pos}

        if etype in (0x03, 0x04):  # embedded document / array
            sublen = struct.unpack_from("<i", buf, vstart)[0]
            vend = vstart + sublen
            # Array elements are stored with an EMPTY key, so every sibling
            # would otherwise share one path. That made "find this node
            # again" ambiguous: after an edit the tree restored selection to
            # the LAST path match, which is why clicking slot 3 threw you
            # down to slot 6. Disambiguate with the element's position.
            node["children"], _ = bson_parse(buf, vstart, chain + [vstart],
                                               path + (step,))
            node["value"] = None
        elif etype == 0x05:  # binary
            blen = struct.unpack_from("<i", buf, vstart)[0]
            node["subtype"] = buf[vstart + 4]
            vend = vstart + 5 + blen
            node["value"] = bytes(buf[vstart + 5:vend])
            node["children"] = None
        elif etype == 0x02:  # string
            slen = struct.unpack_from("<i", buf, vstart)[0]
            vend = vstart + 4 + slen
            node["value"] = bytes(buf[vstart + 4:vend - 1]).decode(
                "utf-8", "replace")
            node["children"] = None
        elif etype == 0x01:  # float32, NOT an 8-byte double - see notes
            vend = vstart + 4
            node["value"] = struct.unpack_from("<f", buf, vstart)[0]
            node["children"] = None
        elif etype == 0x08:
            vend = vstart + 1
            node["value"] = bool(buf[vstart])
            node["children"] = None
        elif etype == 0x0A:  # null - no bytes to it
            vend = vstart
            node["value"] = None
            node["children"] = None
        elif etype == 0x09:
            vend = vstart + 8
            node["value"] = struct.unpack_from("<q", buf, vstart)[0]
            node["children"] = None
        elif etype == 0x10:
            vend = vstart + 4
            node["value"] = struct.unpack_from("<i", buf, vstart)[0]
            node["children"] = None
        elif etype == 0x12:
            vend = vstart + 8
            node["value"] = struct.unpack_from("<q", buf, vstart)[0]
            node["children"] = None
        elif etype == 0x11:
            vend = vstart + 8
            node["value"] = struct.unpack_from("<Q", buf, vstart)[0]
            node["children"] = None
        elif etype == 0x07:  # objectid, 12 raw bytes
            vend = vstart + 12
            node["value"] = bytes(buf[vstart:vend]).hex()
            node["children"] = None
        elif etype == 0x13:  # uint64 (ItemIndex)
            vend = vstart + 8
            node["value"] = struct.unpack_from("<Q", buf, vstart)[0]
            node["children"] = None
        elif etype == 0x16:  # uint8 (level, gender, selection)
            vend = vstart + 1
            node["value"] = buf[vstart]
            node["children"] = None
        elif etype == 0x18:  # uint16 (SI slot index, SC stack count)
            vend = vstart + 2
            node["value"] = struct.unpack_from("<H", buf, vstart)[0]
            node["children"] = None
        elif etype == 0x14:  # confirmed via hex inspection: fixed 4 bytes,
            # looks like a CRC32 (key seen so far: "TemplateCRC"). Not in
            # EDITABLE_TYPES - the game likely validates this against the
            # data it covers, so changing it without recomputing the
            # correct checksum would probably fail that check.
            vend = vstart + 4
            node["value"] = struct.unpack_from("<I", buf, vstart)[0]
            node["children"] = None
        else:
            raise BsonUnknownType(etype, key, pos)

        node["vstart"], node["vend"] = vstart, vend
        nodes.append(node)
        pos = vend
    return nodes, total_len


def bson_find(nodes, path):
    """Find a node by its path tuple, searching recursively."""
    for n in nodes:
        if n["path"] == path:
            return n
        if n["children"]:
            hit = bson_find(n["children"], path)
            if hit:
                return hit
    return None


def bson_encode_scalar(node, new_value):
    etype = node["type"]
    if etype == 0x01:
        return struct.pack("<f", float(new_value))
    if etype == 0x02:
        enc = str(new_value).encode("utf-8") + b"\x00"
        return struct.pack("<i", len(enc)) + enc
    if etype == 0x05:
        raw = (new_value if isinstance(new_value, (bytes, bytearray))
               else str(new_value).encode("utf-8"))
        old_len = node["vend"] - node["vstart"] - 5
        if len(raw) > old_len:
            raise ValueError(
                "this is a fixed-size buffer (%d bytes) - %r needs %d "
                "bytes and buffers can't grow, only strings can"
                % (old_len, new_value, len(raw)))
        return (struct.pack("<i", old_len) + bytes([node["subtype"]])
                + raw + b"\x00" * (old_len - len(raw)))
    if etype == 0x08:
        return bytes([1 if new_value else 0])
    if etype == 0x09:
        return struct.pack("<q", int(new_value))
    if etype == 0x10:
        v = int(new_value)
        if not -2**31 <= v < 2**31:
            raise ValueError("int32 out of range: %d" % v)
        return struct.pack("<i", v)
    if etype == 0x12:
        v = int(new_value)
        if not -2**63 <= v < 2**63:
            raise ValueError("int64 out of range: %d" % v)
        return struct.pack("<q", v)
    if etype == 0x11:
        v = int(new_value)
        if not 0 <= v < 2**64:
            raise ValueError("timestamp out of range: %d" % v)
        return struct.pack("<Q", v)
    if etype == 0x13:
        v = int(new_value)
        if not 0 <= v < 2**64:
            raise ValueError("uint64 out of range: %d" % v)
        return struct.pack("<Q", v)
    if etype == 0x14:
        # uint32: coins, playtime, slotId, price, and also II-style hashes
        # when written through the generic field editor. Full 0..2^32-1.
        v = int(new_value)
        if v < 0:
            raise ValueError("uint32 cannot be negative")
        if v > 0xFFFFFFFF:
            raise ValueError("this field is a 32-bit unsigned integer: "
                             "0–4294967295, got %d" % v)
        return struct.pack("<I", v)
    if etype == 0x16:
        v = int(new_value)
        if not 0 <= v <= 255:
            raise ValueError("this field is one byte: 0-255, got %d" % v)
        return bytes([v])
    if etype == 0x18:
        v = int(new_value)
        if not 0 <= v <= 65535:
            raise ValueError("this field is two bytes: 0-65535, got %d" % v)
        return struct.pack("<H", v)
    raise ValueError("editing BSON type 0x%02X isn't supported" % etype)


def node_expected_value(node, new_value):
    """What node["value"] should read back as after bson_patch() writes
    new_value into this node, mirroring bson_encode_scalar's encoding
    and bson_parse's decoding for each type.

    This is NOT the same as new_value itself: a float32 round-trip can
    change the exact bits, a fixed-size binary buffer gets zero-padded
    to its old length, and numeric fields get coerced to int/bool. A
    write-verification step that compared hit["value"] directly against
    the raw new_value would report false failures on any of those.
    """
    etype = node["type"]
    if etype == 0x01:  # float32 - round-trip through the same precision
        return struct.unpack("<f", struct.pack("<f", float(new_value)))[0]
    if etype == 0x02:  # string - encode_scalar just does str(new_value)
        return str(new_value)
    if etype == 0x05:  # fixed-size binary buffer - zero-padded to old_len
        raw = (new_value if isinstance(new_value, (bytes, bytearray))
               else str(new_value).encode("utf-8"))
        old_len = node["vend"] - node["vstart"] - 5
        return bytes(raw) + b"\x00" * (old_len - len(raw))
    if etype == 0x08:  # bool
        return bool(new_value)
    if etype in (0x09, 0x10, 0x11, 0x12, 0x13, 0x14, 0x16, 0x18):
        return int(new_value)
    return new_value


def bson_patch(buf, node, new_value):
    """Mutate buf in place: write new_value into node's slot and, if the
    encoded size changed, bump every enclosing document/array's own
    length-prefix field so the whole tree stays internally consistent.
    Returns the size delta (0 for same-size edits like numbers)."""
    new_bytes = bson_encode_scalar(node, new_value)
    old_len = node["vend"] - node["vstart"]
    delta = len(new_bytes) - old_len
    buf[node["vstart"]:node["vend"]] = new_bytes
    if delta:
        for dstart in node["chain"]:
            cur = struct.unpack_from("<i", buf, dstart)[0]
            struct.pack_into("<i", buf, dstart, cur + delta)
    return delta


def bson_insert_float(buf, parent_node, hexkey, value):
    """Add a new float32 element to a document that doesn't have it yet.

    The save is SPARSE - it stores only the stats a character actually
    has. A level-3 character's AV document holds 11 of the 218 known
    fields; the rest are absent, meaning "default", not "zero". So there
    is no row to click for Armor or Leech until one is created.

    Inserts just before the parent document's terminating 0x00 and grows
    every enclosing length prefix, exactly as bson_patch does for a
    size-changing edit.
    """
    element = b"\x01" + hexkey.encode("ascii") + b"\x00" + \
        struct.pack("<f", float(value))
    # parent_node["vstart"] is the document's own int32 length prefix.
    doc_start = parent_node["vstart"]
    doc_len = struct.unpack_from("<i", buf, doc_start)[0]
    insert_at = doc_start + doc_len - 1        # before the 0x00 terminator
    buf[insert_at:insert_at] = element
    delta = len(element)
    # This document's own length, then every ancestor's.
    struct.pack_into("<i", buf, doc_start, doc_len + delta)
    for dstart in parent_node["chain"]:
        if dstart == doc_start:
            continue
        cur = struct.unpack_from("<i", buf, dstart)[0]
        struct.pack_into("<i", buf, dstart, cur + delta)
    return delta


def bson_insert_element(buf, parent_node, element_bytes):
    """Generalized version of bson_insert_float: splice a fully-encoded
    raw element (type byte + key + \\0 + value bytes) into parent_node (a
    document or array) just before its terminating 0x00, and grow every
    enclosing length prefix to match. Returns the size delta.

    Verified against a real save: inserting a new {SI,II,SC} array
    element this way, then re-parsing, re-wrapping (zstd+snappy) and
    rebuilding the container, produces byte-identical round-trips with
    valid header/data CRCs.
    """
    doc_start = parent_node["vstart"]
    doc_len = struct.unpack_from("<i", buf, doc_start)[0]
    insert_at = doc_start + doc_len - 1        # before the 0x00 terminator
    buf[insert_at:insert_at] = element_bytes
    delta = len(element_bytes)
    struct.pack_into("<i", buf, doc_start, doc_len + delta)
    for dstart in parent_node["chain"]:
        if dstart == doc_start:
            continue
        cur = struct.unpack_from("<i", buf, dstart)[0]
        struct.pack_into("<i", buf, dstart, cur + delta)
    return delta


def bson_remove_element(buf, parent_node, child_node):
    """Remove one child element from a document/array parent.

    Shrinks parent_node and every enclosing length prefix by the element's
    full byte span (type + key + value). child_node must carry estart/vend
    from bson_parse. Returns the (negative) size delta.
    """
    estart = child_node.get("estart")
    vend = child_node.get("vend")
    if estart is None or vend is None:
        raise ValueError("child node missing estart/vend — re-parse first")
    if not (parent_node["vstart"] <= estart < vend <= parent_node["vend"]):
        raise ValueError("child element is not inside parent document")
    delta = estart - vend  # negative
    del buf[estart:vend]
    doc_start = parent_node["vstart"]
    doc_len = struct.unpack_from("<i", buf, doc_start)[0]
    struct.pack_into("<i", buf, doc_start, doc_len + delta)
    for dstart in parent_node["chain"]:
        if dstart == doc_start:
            continue
        # Ancestors above the splice point need their length fixed; any
        # chain entry at/after estart has shifted — chain stores starts
        # from the pre-edit buffer, so only touch starts < estart.
        if dstart < estart:
            cur = struct.unpack_from("<i", buf, dstart)[0]
            struct.pack_into("<i", buf, dstart, cur + delta)
    return delta


def bson_encode_array_element(etype, key, value_bytes):
    """type byte + key + \\0 + value, ready to hand to bson_insert_element.
    Array elements use an empty key (key="")."""
    return bytes([etype]) + key.encode("ascii") + b"\x00" + value_bytes


def bson_encode_plain_item_body(si, ii_hash, sc_count):
    """The inner {SI,II,SC} document body for a plain (non-equipment,
    no PI sub-document) item entry - the shape every backpack/hotbar/
    pet slot uses when it just holds a stack of something, no unique
    per-instance state. Returns the full length-prefixed document bytes,
    ready to wrap with bson_encode_array_element(0x03, "", ...)."""
    inner = (b"\x18SI\x00" + struct.pack("<H", si & 0xFFFF)
             + b"\x14II\x00" + struct.pack("<I", ii_hash & 0xFFFFFFFF)
             + b"\x18SC\x00" + struct.pack("<H", sc_count & 0xFFFF))
    doclen = 4 + len(inner) + 1
    return struct.pack("<i", doclen) + inner + b"\x00"


def bson_insert_plain_item(buf, array_node, si, ii_hash, sc_count):
    """Insert a brand-new {SI,II,SC} entry into a bag-style array (IBP,
    IAB, PET, or an equip array missing a slot index outright) that
    doesn't have this slot yet. Not for slots that need a PI
    sub-document (unique-instance gear, pets with individual state) -
    that needs a TemplateCRC/ItemIndex this tool has no source for
    beyond copying an existing populated PI elsewhere in the same save.
    """
    body = bson_encode_plain_item_body(si, ii_hash, sc_count)
    element = bson_encode_array_element(0x03, "", body)
    return bson_insert_element(buf, array_node, element)


def bson_insert_document(buf, parent_node, key, field_bytes_list):
    """Insert a brand-new sub-document element (key + a document made of
    the given already-encoded child elements) into parent_node. Used for
    things like adding a QS {T,V} state document from scratch."""
    inner = b"".join(field_bytes_list)
    doclen = 4 + len(inner) + 1
    body = struct.pack("<i", doclen) + inner + b"\x00"
    element = bytes([0x03]) + key.encode("ascii") + b"\x00" + body
    return bson_insert_element(buf, parent_node, element)


def bson_try_parse(buf, base, guess_type, width_fn, chain=None):
    """Like bson_parse, but every occurrence of guess_type has its byte
    width decided by width_fn(buf, vstart) -> vend, instead of raising.
    width_fn may itself raise to reject an implausible guess (e.g. a
    length-prefix hypothesis reading a nonsense length). Used to test
    whether a candidate encoding for an unknown type is consistent with
    the document's own declared length. Returns count of fields guessed."""
    if chain is None:
        chain = [base]
    total_len = struct.unpack_from("<i", buf, base)[0]
    pos = base + 4
    end_of_doc = base + total_len
    guessed = 0
    while pos < end_of_doc - 1:
        etype = buf[pos]
        key_start = pos + 1
        nul = buf.index(b"\x00", key_start)
        key = buf[key_start:nul].decode("utf-8", "replace")
        vstart = nul + 1
        if etype == guess_type:
            vend = width_fn(buf, vstart)
            guessed += 1
        elif etype in (0x03, 0x04):
            sublen = struct.unpack_from("<i", buf, vstart)[0]
            vend = vstart + sublen
            guessed += bson_try_parse(buf, vstart, guess_type, width_fn,
                                       chain + [vstart])
        elif etype == 0x05:
            blen = struct.unpack_from("<i", buf, vstart)[0]
            vend = vstart + 5 + blen
        elif etype == 0x02:
            slen = struct.unpack_from("<i", buf, vstart)[0]
            vend = vstart + 4 + slen
        elif etype == 0x01:
            vend = vstart + 4
        elif etype == 0x08:
            vend = vstart + 1
        elif etype == 0x0A:
            vend = vstart
        elif etype == 0x09:
            vend = vstart + 8
        elif etype == 0x10:
            vend = vstart + 4
        elif etype == 0x12:
            vend = vstart + 8
        elif etype == 0x11:
            vend = vstart + 8
        elif etype == 0x07:
            vend = vstart + 12
        elif etype == 0x13:
            vend = vstart + 8
        elif etype == 0x14:
            vend = vstart + 4
        elif etype == 0x16:
            vend = vstart + 1
        elif etype == 0x18:
            vend = vstart + 2
        else:
            raise ValueError("hit a DIFFERENT unknown type 0x%02X for key "
                              "%r at offset %d while probing"
                              % (etype, key, pos))
        pos = vend
        if pos > end_of_doc - 1 or pos < vstart:
            raise ValueError("ran past the document terminator")
    if pos != end_of_doc - 1 or buf[pos] != 0:
        raise ValueError("did not land cleanly on the document terminator")
    return guessed


def bson_probe(buf, base, guess_type):
    """Try a spread of hypotheses for an unknown type's encoding and
    report which ones let the WHOLE document parse cleanly to its own
    declared end. Returns a list of (label, sample_bytes_or_None, count)."""
    hits = []

    for cand in range(1, 41):
        def f(b, v, w=cand):
            return v + w
        try:
            n = bson_try_parse(buf, base, guess_type, f)
            hits.append(("fixed %d bytes" % cand, cand, n))
        except Exception:
            continue

    # length-prefixed hypotheses: int32 length header (+ maybe 1 subtype
    # byte, like Binary) followed by that many bytes.
    for extra, label in ((0, "int32-length-prefixed (no subtype byte)"),
                          (1, "int32-length-prefixed + 1 subtype byte")):
        def f(b, v, extra=extra):
            L = struct.unpack_from("<i", b, v)[0]
            if not 0 <= L <= 8192:
                raise ValueError("implausible length %d" % L)
            return v + 4 + extra + L
        try:
            n = bson_try_parse(buf, base, guess_type, f)
            hits.append((label, None, n))
        except Exception:
            continue

    return hits


def hex_dump(data, base_offset=0):
    lines = []
    for row in range(0, len(data), 16):
        seg = data[row:row + 16]
        hexs = " ".join("%02X" % b for b in seg)
        asc = "".join(chr(b) if 32 <= b <= 126 else "." for b in seg)
        lines.append("  %06X  %-47s  %s" % (base_offset + row, hexs, asc))
    return "\n".join(lines)


class Container:
    def __init__(self, raw):
        if len(raw) < HEADER_SIZE or raw[:4] != MAGIC:
            raise ValueError("not a KSC1 save file")
        self.raw = raw
        self.count = struct.unpack_from("<I", raw, 4)[0]
        self.header_crc = struct.unpack_from("<Q", raw, 8)[0]
        self.data_crc = struct.unpack_from("<Q", raw, 16)[0]
        end = HEADER_SIZE + self.count * ENTRY_SIZE
        if end > len(raw):
            raise ValueError("entry table runs past the end of the file")
        self.table = raw[HEADER_SIZE:end]
        self.entries = []
        pos = end
        for i in range(self.count):
            o = i * ENTRY_SIZE
            ident, tag, size = struct.unpack_from("<I4sI", self.table, o)
            self.entries.append({"index": i, "id": ident, "tag": tag,
                                  "size": size, "offset": pos})
            pos += size
        self.blob = raw[end:]

    def chunk(self, e):
        return self.raw[e["offset"]:e["offset"] + e["size"]]

    def verify(self):
        return (crc64(self.table) == self.header_crc,
                crc64(self.blob) == self.data_crc)


def rebuild_container(entries):
    table = bytearray()
    blob = bytearray()
    for ident, tag, payload in entries:
        table += struct.pack("<I4sI", ident, tag, len(payload))
        blob += payload
    out = bytearray(MAGIC)
    out += struct.pack("<I", len(entries))
    out += struct.pack("<Q", crc64(bytes(table)))
    out += struct.pack("<Q", crc64(bytes(blob)))
    out += table
    out += blob
    return bytes(out)


def load_container(path):
    with open(path, "rb") as fh:
        return Container(fh.read())


def iter_docs(c, dctx):
    for e in c.entries:
        chunk = c.chunk(e)
        try:
            doc, kind = unwrap(chunk, dctx)
        except Exception as exc:
            yield e, None, None, str(exc)
            continue
        yield e, doc, kind, None


# ----------------------------------------------------------------------
# locating save files


def candidate_roots():
    userprofile = os.environ.get("USERPROFILE", "")
    steam_userdata = r"C:\Program Files (x86)\Steam\userdata"
    if os.path.isdir(steam_userdata):
        pattern = os.path.join(steam_userdata, "*", STEAM_APPID, "remote")
        for path in glob.glob(pattern):
            steam_id = path.split(os.sep)[-3]
            yield ("Steam Cloud (id %s)" % steam_id, path)
    if userprofile:
        local = os.path.join(userprofile, "Saved Games", "portal_knights")
        if os.path.isdir(local):
            yield ("Local (Steam Cloud off)", local)
        guest = os.path.join(local, "Guest")
        if os.path.isdir(guest):
            yield ("Guest / Player 2", guest)


def parse_save_filename(fname):
    """Classify a Portal Knights remote/save filename.

    Local saves are 16 hex digits (8 bytes). Steam community / shared
    universe+world files are often 32 hex digits (16 bytes), e.g.
      030000000000000061f77bab00000000
      0400000004000205287f3c7400000000
    with a Steam-id-like hash in the middle.

    Returns dict with keys:
      type, universe, location, location_name, label, community (bool)
    """
    base = os.path.basename(fname).lower()
    if "." in base:
        base = base.split(".")[0]
    if len(base) not in (16, 32) or any(c not in "0123456789abcdef" for c in base):
        return {"type": "unknown", "universe": None, "location": None,
                "location_name": None, "label": base, "community": False}
    try:
        raw = bytes.fromhex(base)
    except ValueError:
        return {"type": "unknown", "universe": None, "location": None,
                "location_name": None, "label": base, "community": False}
    community = len(raw) == 16  # 32 hex digits
    ftype = raw[0]
    if ftype == FILE_TYPE_OPTIONS and not community:
        return {"type": "options", "universe": None, "location": None,
                "location_name": None, "community": False,
                "label": "Options / system (0000…) — not characters"}
    if ftype == FILE_TYPE_CHAR and not community:
        return {"type": "character", "universe": None, "location": None,
                "location_name": None, "community": False,
                "label": "Characters (all 9 slots)"}
    if ftype == FILE_TYPE_CHAR_BAK and not community:
        return {"type": "character_backup", "universe": None, "location": None,
                "location_name": None, "community": False,
                "label": "Characters backup (0200…)"}
    if ftype == FILE_TYPE_UNIVERSE:
        # Local: last byte is slot. Community: slot still near the start;
        # trailing bytes are a share hash (e.g. 61f77bab).
        if community:
            slot = raw[7]  # same offset as local 8-byte layout for first half
            share = base[16:24] if len(base) >= 24 else ""
            return {"type": "universe", "universe": slot, "location": None,
                    "location_name": None, "community": True,
                    "label": "Community universe slot %d (%s)" % (slot, share)}
        slot = raw[7]
        return {"type": "universe", "universe": slot, "location": None,
                "location_name": None, "community": False,
                "label": "Universe slot %d" % slot}
    if ftype == FILE_TYPE_WORLD:
        # Byte 4 = universe id on local files. Community layout keeps that
        # in the first 8 bytes; location codes still trail.
        universe = raw[4]
        # Location: try trailing 5 then 3 hex digits of the *first* 16 chars
        # for local, or of the whole name for community.
        head = base[:16]
        trailing5 = int(head[-5:], 16)
        trailing3 = int(head[-3:], 16)
        if trailing5 in WORLD_LOCATIONS:
            loc = trailing5
        elif trailing3 in WORLD_LOCATIONS:
            loc = trailing3
        else:
            loc = trailing3
        name = WORLD_LOCATIONS.get(loc)
        tag = "Community world" if community else "World"
        label = "%s U%d · %s" % (tag, universe, name or ("0x%X" % loc))
        return {"type": "world", "universe": universe, "location": loc,
                "location_name": name, "label": label, "community": community}
    if ftype == FILE_TYPE_SERVER_SESSION and not community:
        return {"type": "server_session", "universe": None, "location": None,
                "location_name": None, "community": False,
                "label": "Dedicated server session (0500…) — auto-generated"}
    if ftype == FILE_TYPE_MISC and not community:
        return {"type": "misc", "universe": None, "location": None,
                "location_name": None, "community": False,
                "label": "Misc system file (0600…)"}
    return {"type": "unknown", "universe": None, "location": None,
            "location_name": None, "community": community,
            "label": "Unknown type 0x%02X (%s)" % (ftype, base)}


def find_saves():
    """Scan every file in known save roots (not just character slots).

    Returns list of (source_label, root, fname, full_path, info, mtime)
    where info is the dict from parse_save_filename and mtime is st_mtime
    (0 if unavailable).
    """
    found = []
    seen = set()
    for label, root in candidate_roots():
        if not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for fname in names:
            full = os.path.join(root, fname)
            if not os.path.isfile(full):
                continue
            # Skip obvious non-saves
            if fname.startswith(".") or fname.endswith(
                    (".bak", ".json", ".txt", ".bin", ".py", ".md", ".png",
                     ".jpg", ".log")):
                continue
            # Local = 16 hex digits; community/shared = 32 hex digits
            base = fname.lower()
            if len(base) not in (16, 32) or any(
                    c not in "0123456789abcdef" for c in base):
                continue
            key = os.path.normcase(os.path.abspath(full))
            if key in seen:
                continue
            seen.add(key)
            info = parse_save_filename(fname)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                mtime = 0.0
            found.append((label, root, fname, full, info, mtime))
    # Default order: newest first (helps Worlds tab and the combo)
    found.sort(key=lambda r: r[5], reverse=True)
    return found


def find_saves_of_type(type_name):
    """Convenience filter over find_saves()."""
    return [s for s in find_saves() if s[4]["type"] == type_name]


def extract_universe_name(doc):
    """Pull the display name out of a UniverseHeaderData (USHD) BSON doc.

    The binary name field is often packed as:
        short_code \\0 alt_path \\0 padding
    e.g. b'AIW\\x00y/Glitched Mannequin World\\x00…'

    The in-game list shows the *short* code (AIW, More Trophies, …), not the
    longer path-like segment. Prefer the first non-empty segment; if a later
    segment looks more like a title (spaces, no slash) use that instead.
    """
    if not doc:
        return None

    def pick_best(parts):
        if not parts:
            return None
        # Prefer first segment when it is a short display code
        first = parts[0]
        if len(first) <= 24 and "/" not in first:
            return first
        # Otherwise prefer a segment with spaces (human title) over path-like
        titled = [p for p in parts if " " in p and "/" not in p]
        if titled:
            titled.sort(key=len, reverse=True)
            return titled[0]
        # Fall back to longest
        parts = sorted(parts, key=len, reverse=True)
        return parts[0]

    for start, length, text in find_name_fields(doc):
        raw = doc[start:start + length]
        parts = [p.decode("utf-8", "replace").strip()
                 for p in raw.split(b"\x00") if p.strip()]
        best = pick_best(parts)
        if best:
            return best
    pos = 0
    while True:
        i = doc.find(b"\x02name\x00", pos)
        if i < 0:
            break
        pos = i + 1
        if i + 9 > len(doc):
            continue
        slen = struct.unpack_from("<I", doc, i + 6)[0]
        sstart = i + 10
        if sstart + slen > len(doc) or slen < 1:
            continue
        text = doc[sstart:sstart + slen - 1].decode("utf-8", "replace").strip()
        if text:
            return text
    return None


def island_location_from_entry_id(entry_id):
    """Map an ILHD entry id to a WORLD_LOCATIONS name.

    Low 16 bits of the entry id are the location code
    (0x01000100 → 0x0100 Squire's Knoll). DLC may need low 20 bits.
    """
    lo16 = entry_id & 0xFFFF
    lo20 = entry_id & 0xFFFFF
    if lo20 in WORLD_LOCATIONS:
        return lo20, WORLD_LOCATIONS[lo20]
    if lo16 in WORLD_LOCATIONS:
        return lo16, WORLD_LOCATIONS[lo16]
    return lo16, None


def extract_island_seed_and_size(doc):
    """Best-effort seed / size readout from IslandHeaderData (ILHD).

    Confirmed types from real saves:
      seed               0x13 uint64
      width/height/depth 0x14 uint32
      generationVersion  0x14 uint32
      islandSize         0x03 sub-document (use width instead)
    """
    out = {}
    if not doc:
        return out
    specs = (
        (b"seed", 0x13, "<Q"),
        (b"width", 0x14, "<I"),
        (b"height", 0x14, "<I"),
        (b"depth", 0x14, "<I"),
        (b"generationVersion", 0x14, "<I"),
        (b"seed", 0x12, "<q"),
        (b"seed", 0x10, "<i"),
        (b"width", 0x10, "<i"),
        (b"height", 0x10, "<i"),
        (b"depth", 0x10, "<i"),
    )
    for key, typ, fmt in specs:
        k = key.decode()
        if k in out:
            continue
        pat = bytes([typ]) + key + b"\x00"
        i = doc.find(pat)
        if i < 0:
            continue
        off = i + 1 + len(key) + 1
        try:
            val = struct.unpack_from(fmt, doc, off)[0]
        except struct.error:
            continue
        out[k] = val
    if "width" in out and "islandSize" not in out:
        out["islandSize"] = out["width"]
    return out


def program_dir():
    """Folder containing this script (where pk_dict.bin should live)."""
    return os.path.dirname(os.path.abspath(__file__))


def default_dict_path():
    """Preferred path: pk_dict.bin next to the program."""
    return os.path.join(program_dir(), "pk_dict.bin")


def guess_dict_path():
    """Find an existing pk_dict.bin — prefer next to the script."""
    userprofile = os.environ.get("USERPROFILE", "")
    candidates = [
        default_dict_path(),
        os.path.join(userprofile, "Desktop", "pk_dict.bin"),
        os.path.join(userprofile, "Downloads", "pk_dict.bin"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return ""


# The zstd dictionary lives inside the game's own executable - it isn't
# something the game "gives" you, it has to be cut out of the .exe once.
# This automates that so nobody has to do it by hand.
GAME_EXE_NAME = "portal_knights_x64.exe"
DICT_OFFSET = 0x8096C0
DICT_SIZE = 262144
ZSTD_DICT_MAGIC = b"\x37\xa4\x30\xec"


def find_game_exe():
    """Look for the game's exe in the usual Steam library locations."""
    found = []
    roots = []
    steam_root = r"C:\Program Files (x86)\Steam"
    roots.append(os.path.join(steam_root, "steamapps", "common"))

    # Steam can span multiple drives/libraries, listed in libraryfolders.vdf
    vdf = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
    if os.path.isfile(vdf):
        try:
            with open(vdf, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
            import re
            for m in re.finditer(r'"path"\s*"([^"]+)"', text):
                p = m.group(1).replace("\\\\", "\\")
                roots.append(os.path.join(p, "steamapps", "common"))
        except Exception:
            pass

    for root in roots:
        candidate = os.path.join(root, "Portal Knights", GAME_EXE_NAME)
        if os.path.isfile(candidate):
            found.append(candidate)

    # De-duplicate: libraryfolders.vdf lists Steam's own default library
    # path alongside any extra drives, so the default install can get
    # added once manually above and once again from the VDF parse.
    seen = set()
    deduped = []
    for path in found:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            deduped.append(path)
    return deduped


def extract_dict_from_exe(exe_path, dest_path):
    """Cut the zstd dictionary out of the game exe and save it to dest_path.

    Raises ValueError with a clear reason if the bytes don't look right,
    rather than silently writing something broken.
    """
    with open(exe_path, "rb") as fh:
        fh.seek(DICT_OFFSET)
        data = fh.read(DICT_SIZE)
    if len(data) != DICT_SIZE:
        raise ValueError(
            "only read %d of %d expected bytes at offset 0x%X - this exe "
            "may be a different version/build than the offset was taken "
            "from" % (len(data), DICT_SIZE, DICT_OFFSET))
    if data[:4] != ZSTD_DICT_MAGIC:
        raise ValueError(
            "bytes at offset 0x%X don't start with the zstd dictionary "
            "magic number (37 A4 30 EC) - got %s instead. This exe is "
            "probably a different version than the offset was taken from."
            % (DICT_OFFSET, data[:4].hex()))
    tmp_path = dest_path + ".tmp-%d" % os.getpid()
    with open(tmp_path, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, dest_path)
    return dest_path


# ----------------------------------------------------------------------
# GUI


def character_slot_id(doc):
    """The character's slotId, i.e. the order the game lists them in.

    Returns None when the field is absent, so callers can keep such rows
    at the end instead of pretending they are slot 0.
    """
    try:
        nodes, _ = bson_parse(doc)
    except Exception:
        return None

    def hunt(ns):
        for n in ns:
            if n["key"] == "slotId" and n["children"] is None:
                return n["value"]
            if n["children"]:
                got = hunt(n["children"])
                if got is not None:
                    return got
        return None

    return hunt(nodes)


def _iter_all(nodes):
    for n in nodes:
        yield n
        if n["children"]:
            for c in _iter_all(n["children"]):
                yield c



class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Portal Knights Save Editor")
        self.geometry("900x640")
        self.minsize(720, 520)

        self.dctx = None
        self.cctx = None
        self.container = None
        self.savefile_path = tk.StringVar()
        self.dictfile_path = tk.StringVar(value=guess_dict_path())
        self.entries_by_row = {}  # tree row id -> (entry, doc, kind, name_field)
        self._all_saves = []      # full find_saves() results
        self._save_lookup = {}
        self._current_info = None  # parse_save_filename result for loaded file

        # Heavy-scan (world list, quick=False) threading state. The scan
        # itself runs on a worker thread so decompressing every BKCK
        # chunk in a large save collection doesn't freeze the window;
        # results come back through this queue and are only ever
        # applied to widgets on the main thread via self.after().
        self._world_scan_thread = None
        self._world_scan_queue = queue.Queue()
        self._world_scan_cancel = threading.Event()
        # zstandard decompression contexts aren't safe to use from two
        # threads at once. The worker thread holds this while
        # decompressing; buttons that trigger decompression on the main
        # thread are disabled for the duration of a scan (see
        # _set_scanning_state) so the two can never overlap.
        self._dctx_lock = threading.Lock()

        self._build_widgets()
        self.refresh_saves()
        self.auto_extract_dict_if_needed()
        self.after(100, self._ensure_github_data)
        # Characters should appear without requiring a manual "Refresh list"
        # click. Defer slightly so the window is up and the dict is ready.
        self.after(200, self._autoload_characters)
        # Kick off an incremental chest-count scan in the background once
        # the dict is available — only worlds that are new or changed.
        self.after(400, self._autoload_world_scan)

    def report_callback_exception(self, exc, val, tb):
        """Backstop for anything that raises inside a Tkinter callback
        without being caught by a local except block. Without this,
        Tkinter's default behaviour is to print the traceback to stderr
        and otherwise say nothing - invisible to anyone running this as
        a bundled/double-clicked app. This does NOT replace the
        deliberate per-action try/except blocks elsewhere (those give
        better, action-specific messages); it only catches whatever
        isn't covered yet."""
        import traceback
        msg = "".join(traceback.format_exception(exc, val, tb))
        try:
            self.log("UNHANDLED ERROR:\n" + msg)
        except Exception:
            pass
        try:
            messagebox.showerror(
                "Unexpected error",
                "%s\n\nUse Copy log / Save log… at the bottom if you want "
                "to share the full log." % val)
        except Exception:
            pass

    def _ensure_github_data(self, force=False):
        """Pull item_table + templates from GitHub if missing (or force)."""
        def work():
            results = ensure_remote_data(force=force, log_fn=self.log)
            def done():
                if force or any(s == "downloaded" for s in results.values()):
                    invalidate_item_cache()
                    try:
                        _reload_template_maps()
                    except Exception:
                        pass
                parts = ["%s=%s" % (k, v) for k, v in results.items()]
                self.log("Data files: " + ", ".join(parts))
                if force:
                    messagebox.showinfo(
                        "Data refresh",
                        "GitHub data:\n  " + "\n  ".join(parts) +
                        "\n\nCached next to the script for offline use.",
                        parent=self)
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def auto_extract_dict_if_needed(self):
        """If pk_dict.bin isn't already sitting somewhere we'd find it,
        try to pull it from the game exe automatically - no button click
        needed. Only falls back to asking the user if that doesn't work."""
        if self.dictfile_path.get():
            return  # guess_dict_path() already found one - nothing to do
        exes = find_game_exe()
        if not exes:
            self.log("No pk_dict.bin found, and couldn't find the game "
                      "exe automatically. Use 'Extract from game...' to "
                      "point at it manually.")
            return
        exe_path = exes[0]
        dest = default_dict_path()
        try:
            extract_dict_from_exe(exe_path, dest)
        except Exception as exc:
            self.log("Auto-extraction from %s failed: %s" % (exe_path, exc))
            return
        self.dictfile_path.set(dest)
        self.log("Auto-extracted dictionary from %s -> %s" % (exe_path, dest))

    # -- layout --------------------------------------------------------

    def _build_widgets(self):
        pad = {"padx": 8, "pady": 6}

        # ---- save file row ----
        f1 = ttk.LabelFrame(self, text="Save folder / file")
        f1.pack(fill="x", **pad)

        filter_row = ttk.Frame(f1)
        filter_row.grid(row=0, column=0, columnspan=4, sticky="we", padx=6, pady=(6, 0))
        ttk.Label(filter_row, text="Show:").pack(side="left")
        self.filter_var = tk.StringVar(value="All")
        for text in ("All", "Characters", "Universes", "Worlds"):
            ttk.Radiobutton(
                filter_row, text=text, value=text,
                variable=self.filter_var, command=self._apply_save_filter
            ).pack(side="left", padx=4)

        self.save_combo = ttk.Combobox(f1, textvariable=self.savefile_path,
                                        state="readonly", width=80)
        self.save_combo.grid(row=1, column=0, columnspan=3, sticky="we",
                              padx=6, pady=6)
        f1.columnconfigure(0, weight=1)

        ttk.Button(f1, text="Refresh", command=self.refresh_saves)\
            .grid(row=2, column=0, padx=6, pady=(0, 6), sticky="w")
        ttk.Button(f1, text="Browse...", command=self.browse_savefile)\
            .grid(row=2, column=1, padx=6, pady=(0, 6), sticky="w")
        ttk.Button(f1, text="Backup this file", command=self.backup_current)\
            .grid(row=2, column=2, padx=6, pady=(0, 6), sticky="w")

        # ---- dict file row ----
        f2 = ttk.LabelFrame(self, text="Dictionary (pk_dict.bin, from the game exe)")
        f2.pack(fill="x", **pad)
        ttk.Entry(f2, textvariable=self.dictfile_path).grid(
            row=0, column=0, sticky="we", padx=6, pady=6)
        f2.columnconfigure(0, weight=1)
        ttk.Button(f2, text="Browse...", command=self.browse_dictfile)\
            .grid(row=0, column=1, padx=6, pady=6)
        ttk.Button(f2, text="Extract from game...", command=self.extract_dict)\
            .grid(row=0, column=2, padx=6, pady=6)

        # ---- load / actions ----
        f3 = ttk.Frame(self)
        f3.pack(fill="x", **pad)
        self.load_btn = ttk.Button(f3, text="Load selected",
                                   command=self.load_selected)
        self.load_btn.pack(side="left")
        ttk.Button(f3, text="File naming help", command=self.show_naming_help)\
            .pack(side="left", padx=8)
        ttk.Button(f3, text="Debug dump", command=self.debug_dump)\
            .pack(side="left", padx=8)
        ttk.Button(f3, text="Restore character backup",
                   command=self.restore_character_backup)\
            .pack(side="left", padx=8)
        self.file_info_var = tk.StringVar(value="")
        ttk.Label(f3, textvariable=self.file_info_var, foreground="#444")\
            .pack(side="left", padx=8)
        self._debug = False

        # ---- content notebook (characters / universe / world) ----
        self.content_nb = ttk.Notebook(self)
        self.content_nb.pack(fill="both", expand=True, **pad)

        # Characters tab — max 10 slots, keep the list compact and put
        # actions *under* it (pack bottom first so side=left tree can't
        # shove the buttons off to the right).
        self.tab_chars = ttk.Frame(self.content_nb)
        self.content_nb.add(self.tab_chars, text="Characters")

        char_actions = ttk.Frame(self.tab_chars)
        char_actions.pack(side="bottom", fill="x", padx=6, pady=(2, 2))
        ttk.Button(char_actions, text="Refresh list",
                   command=self.load_characters).pack(side="left", padx=(0, 8))
        ttk.Label(char_actions, text="New name:").pack(side="left")
        self.new_name = tk.StringVar()
        ttk.Entry(char_actions, textvariable=self.new_name, width=28)\
            .pack(side="left", padx=4)
        ttk.Button(char_actions, text="Rename",
                   command=self.rename_selected).pack(side="left", padx=4)

        char_actions2 = ttk.Frame(self.tab_chars)
        char_actions2.pack(side="bottom", fill="x", padx=6, pady=(0, 6))
        ttk.Button(char_actions2, text="Character Editor…",
                   command=self.open_character_editor).pack(side="left",
                                                           padx=(0, 6))
        ttk.Button(char_actions2, text="Edit fields…",
                   command=self.open_field_editor).pack(side="left", padx=4)
        ttk.Label(char_actions2,
                  text="Select a character, then Edit fields…",
                  foreground="#555").pack(side="left", padx=8)

        list_frame = ttk.Frame(self.tab_chars)
        list_frame.pack(side="top", fill="both", expand=True, padx=6, pady=6)
        columns = ("slot", "name", "index", "size")
        # height=11 shows all 10 slots + header without a giant empty box
        self.tree = ttk.Treeview(list_frame, columns=columns,
                                  show="headings", selectmode="browse",
                                  height=11)
        heads = {"slot": "Slot", "name": "Name", "index": "Entry",
                 "size": "Size"}
        for col, w in zip(columns, (60, 300, 60, 90)):
            self.tree.heading(col, text=heads[col])
            self.tree.column(col, width=w, anchor="w")
        vsb = ttk.Scrollbar(list_frame, orient="vertical",
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        # Universes tab
        self.tab_univ = ttk.Frame(self.content_nb)
        self.content_nb.add(self.tab_univ, text="Universes")
        ucols = ("slot", "name", "islands", "size", "file")
        self.univ_tree = ttk.Treeview(self.tab_univ, columns=ucols,
                                       show="headings", selectmode="browse",
                                       height=10)
        for col, text, w in (("slot", "Slot", 50),
                             ("name", "Universe name", 260),
                             ("islands", "Islands", 70),
                             ("size", "Size", 90),
                             ("file", "File", 180)):
            self.univ_tree.heading(col, text=text)
            self.univ_tree.column(col, width=w, anchor="w")
        self.univ_tree.pack(fill="both", expand=True, padx=6, pady=6)
        univ_actions = ttk.Frame(self.tab_univ)
        univ_actions.pack(fill="x", padx=6, pady=(0, 6))
        self.univ_refresh_btn = ttk.Button(
            univ_actions, text="Refresh list",
            command=self._refresh_universes_from_disk)
        self.univ_refresh_btn.pack(side="left")
        ttk.Button(univ_actions, text="Open selected file",
                   command=self.open_selected_universe).pack(side="left", padx=6)
        ttk.Label(
            univ_actions,
            text="Warning: universe slot is baked into the filename — "
                 "do not rename 0300… files to change slots.",
            foreground="#a60",
        ).pack(side="left", padx=8)

        # Worlds tab
        self.tab_world = ttk.Frame(self.content_nb)
        self.content_nb.add(self.tab_world, text="Worlds")
        world_search_fr = ttk.Frame(self.tab_world)
        world_search_fr.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Label(world_search_fr, text="Search:").pack(side="left")
        self.world_search_var = tk.StringVar()
        self.world_search_entry = ttk.Entry(
            world_search_fr, textvariable=self.world_search_var, width=36)
        self.world_search_entry.pack(side="left", padx=4)
        self.world_search_entry.bind("<KeyRelease>",
                                     lambda _e: self._filter_world_tree())
        ttk.Button(world_search_fr, text="Clear",
                   command=lambda: (self.world_search_var.set(""),
                                    self._filter_world_tree())
                   ).pack(side="left", padx=2)
        self.world_search_status = tk.StringVar(value="")
        ttk.Label(world_search_fr, textvariable=self.world_search_status,
                  foreground="#444").pack(side="left", padx=8)
        wcols = ("univ", "location", "name", "chunks", "chests", "size", "file")
        self.world_tree = ttk.Treeview(self.tab_world, columns=wcols,
                                        show="headings", selectmode="browse",
                                        height=10)
        for col, text, w in (("univ", "U#", 40),
                             ("location", "Code", 60),
                             ("name", "Island / World", 220),
                             ("chunks", "Chunks", 70),
                             ("chests", "Inv*", 70),
                             ("size", "Size", 90),
                             ("file", "File", 160)):
            self.world_tree.heading(col, text=text)
            self.world_tree.column(col, width=w, anchor="w")
        self.world_tree.pack(fill="both", expand=True, padx=6, pady=6)
        world_actions = ttk.Frame(self.tab_world)
        world_actions.pack(fill="x", padx=6, pady=(0, 2))
        world_actions2 = ttk.Frame(self.tab_world)
        world_actions2.pack(fill="x", padx=6, pady=(0, 6))
        self.world_refresh_btn = ttk.Button(
            world_actions, text="Refresh list",
            command=self._refresh_worlds_from_disk)
        self.world_refresh_btn.pack(side="left")
        self.world_full_scan_btn = ttk.Button(
            world_actions, text="Rescan all (chest counts)",
            command=lambda: self.refresh_world_list_full(
                incremental=False, silent=False))
        self.world_full_scan_btn.pack(side="left", padx=4)
        self.world_scan_cancel_btn = ttk.Button(
            world_actions, text="Cancel scan",
            command=self.cancel_world_scan, state="disabled")
        self.world_scan_cancel_btn.pack(side="left", padx=4)
        self.world_show_all_btn = ttk.Button(
            world_actions, text="Show all universes",
            command=self.clear_world_universe_filter)
        self.world_show_all_btn.pack(side="left", padx=4)
        self.world_open_btn = ttk.Button(
            world_actions, text="Open selected world",
            command=self.open_selected_world)
        self.world_open_btn.pack(side="left", padx=6)
        self.world_inv_btn = ttk.Button(
            world_actions2, text="Inventories…",
            command=self.open_world_chests)
        self.world_inv_btn.pack(side="left")
        self.world_signs_btn = ttk.Button(
            world_actions2, text="Signs…", command=self.open_world_signs)
        self.world_signs_btn.pack(side="left", padx=4)
        self.world_npcs_btn = ttk.Button(
            world_actions2, text="NPCs / spawns…",
            command=self.open_world_npcs)
        self.world_npcs_btn.pack(side="left", padx=4)
        self.world_map_btn = ttk.Button(
            world_actions2, text="Map…", command=self.open_world_map)
        self.world_map_btn.pack(side="left", padx=4)
        ttk.Button(world_actions2, text="Templates…",
                   command=self.open_template_editor).pack(side="left", padx=4)
        ttk.Button(
            world_actions2, text="Refresh data…",
            command=lambda: self._ensure_github_data(force=True),
        ).pack(side="left", padx=4)
        ttk.Label(
            world_actions2,
            text="*Chests = inventory entities. Map = top-down X/Z.",
            foreground="#555",
        ).pack(side="left", padx=8)
        # Buttons that trigger decompression on the main thread - all
        # disabled for the duration of a background full scan so the
        # shared zstd dictionary context is never touched from two
        # threads at once (see _dctx_lock).
        self._scan_lock_widgets = [
            self.load_btn, self.world_refresh_btn, self.world_open_btn,
            self.world_inv_btn, self.world_signs_btn, self.world_npcs_btn,
            self.world_map_btn, self.univ_refresh_btn,
            self.world_show_all_btn,
        ]
        self.world_scan_status = tk.StringVar(value="")
        ttk.Label(self.tab_world, textvariable=self.world_scan_status,
                 foreground="#a60").pack(anchor="w", padx=6)

        # ---- log ----
        f6 = ttk.LabelFrame(self, text="Log")
        f6.pack(fill="both", **pad)
        log_btns = ttk.Frame(f6)
        log_btns.pack(fill="x", padx=6, pady=(4, 0))
        ttk.Button(log_btns, text="Copy log",
                   command=self.copy_log).pack(side="left")
        ttk.Button(log_btns, text="Save log…",
                   command=self.save_log).pack(side="left", padx=6)
        ttk.Button(log_btns, text="Clear log",
                   command=self.clear_log).pack(side="left")
        ttk.Label(
            log_btns,
            text="Only written to disk when you click Save log…",
            foreground="#666",
        ).pack(side="left", padx=10)
        self.log_text = tk.Text(f6, height=6, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

    def log(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def get_log_text(self):
        """Full log contents (in-memory only until Save log…)."""
        try:
            return self.log_text.get("1.0", "end-1c")
        except Exception:
            return ""

    def copy_log(self):
        text = self.get_log_text()
        if not text.strip():
            messagebox.showinfo("Log", "Log is empty.")
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
            messagebox.showinfo("Log", "Log copied to clipboard.")
        except Exception as ex:
            messagebox.showerror("Clipboard", str(ex))

    def save_log(self):
        text = self.get_log_text()
        if not text.strip():
            messagebox.showinfo("Log", "Log is empty.")
            return
        path = filedialog.asksaveasfilename(
            title="Save log",
            defaultextension=".txt",
            initialfile="pk_save_editor_log.txt",
            filetypes=[("Text", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
                if not text.endswith("\n"):
                    fh.write("\n")
            messagebox.showinfo("Log", "Saved:\n%s" % path)
        except Exception as ex:
            messagebox.showerror("Save failed", str(ex))

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # -- save file discovery --------------------------------------------

    def refresh_saves(self):
        self._all_saves = find_saves()
        counts = {}
        for row in self._all_saves:
            info = row[4]
            counts[info["type"]] = counts.get(info["type"], 0) + 1
        self.log(
            "Found %d save file(s): %d character, %d universe, %d world, "
            "%d options/misc/other."
            % (len(self._all_saves),
               counts.get("character", 0) + counts.get("character_backup", 0),
               counts.get("universe", 0),
               counts.get("world", 0),
               counts.get("options", 0) + counts.get("misc", 0)
               + counts.get("unknown", 0)))
        try:
            n_items = len(item_table())
            self.log("Item table: %d rows from %s"
                     % (n_items, item_table_path()))
        except Exception as ex:
            self.log("Item table: not loaded (%s)" % ex)
        self._apply_save_filter()
        self.save_combo.bind("<<ComboboxSelected>>", self._on_save_selected)
        # Populate universe / world overview lists
        self.refresh_universe_list(quick=True)
        self.refresh_world_list(quick=True)

    def _apply_save_filter(self):
        filt = (self.filter_var.get() if hasattr(self, "filter_var")
                else "All")
        type_map = {
            "Characters": {"character", "character_backup"},
            "Universes": {"universe"},
            "Worlds": {"world"},
        }
        allowed = type_map.get(filt)
        values = []
        self._save_lookup = {}
        # Prefer newest first (already sorted in find_saves)
        for label, root, fname, full, info, mtime in self._all_saves:
            if allowed is not None and info["type"] not in allowed:
                continue
            display = "[%s] %s — %s" % (label, info["label"], fname)
            values.append(display)
            self._save_lookup[display] = full
        self.save_combo["values"] = values
        if values:
            # When filtering Characters, prefer the real 01… file over backup
            pick = 0
            if filt == "Characters":
                for i, v in enumerate(values):
                    if "0100000000000000" in v.lower() or "Characters (all 9" in v:
                        pick = i
                        break
                self.log("Characters filter: select 0100000000000000, then "
                         "Load selected. (0000… and 0300… are not characters.)")
            self.save_combo.current(pick)
            self.savefile_path.set(self._save_lookup[values[pick]])
            self._update_file_info_label()
        else:
            self.savefile_path.set("")
            self.file_info_var.set("No files match this filter.")

    def _on_save_selected(self, _event=None):
        sel = self.save_combo.get()
        full = self._save_lookup.get(sel)
        if full:
            self.savefile_path.set(full)
            self._update_file_info_label()

    def _update_file_info_label(self):
        path = self.savefile_path.get()
        if not path:
            self.file_info_var.set("")
            return
        info = parse_save_filename(path)
        self._current_info = info
        bits = [info["label"]]
        if info.get("universe") is not None and info["type"] == "world":
            bits.append("universe %d" % info["universe"])
        if info.get("location") is not None:
            bits.append("loc 0x%X" % info["location"])
        try:
            sz = os.path.getsize(path)
            bits.append("%s bytes" % "{:,}".format(sz))
        except OSError:
            pass
        self.file_info_var.set(" · ".join(bits))

    def show_naming_help(self):
        msg = (
            "Portal Knights save filename conventions\n"
            "========================================\n\n"
            "All files live in the Steam Cloud remote folder (or local\n"
            "Saved Games\\portal_knights) and are 16 hex digits long.\n\n"
            "00 000000 00000000\n"
            "  Options / system blob. Small file. NOT characters — loading\n"
            "  it on the Characters tab will correctly show no names.\n\n"
            "01 000000 00000000\n"
            "  Character file — ALWAYS this exact name. Holds all 9\n"
            "  character slots. Editing it affects every character.\n"
            "  Always backup before changing (use the Backup button).\n\n"
            "02 000000 00000000\n"
            "  Automatic backup of the character file created by the game.\n\n"
            "03 000000 0000000N\n"
            "  Universe file. N (last digit) is the universe SLOT (1–9).\n"
            "  The slot is part of the filename and cannot be changed by\n"
            "  renaming — the game will ignore a mismatched slot. Overwriting\n"
            "  a universe that already has the same name will wipe its worlds.\n\n"
            "04 000000 U0 LLLLLL\n"
            "  World / island file.\n"
            "  · Digit at position 10 (0-based index 9) / byte 4 = universe id\n"
            "  · Last 3 (or 5 for DLC) hex digits = location code\n"
            "  Examples: …0100 = Squire's Knoll, …050b = Vacant Grassland,\n"
            "  …040f = Isle of Toblis.\n"
            "  Full location list is built into this tool (WORLD_LOCATIONS).\n\n"
            "06 000000 00000000\n"
            "  Misc system file seen on some installs.\n\n"
            "Tip: use the filter radio buttons and the Universes / Worlds\n"
            "tabs to browse everything in the folder at once.\n"
            "Use Debug dump to inspect the currently selected file."
        )
        messagebox.showinfo("Save file naming", msg)

    def debug_dump(self):
        """Write a FULL inspection of the selected file to the log AND a .txt file.

        Every entry is listed (no 12-entry cap). The text file is written next
        to the save (or to Desktop) so large world dumps are easy to share.
        """
        path = self.savefile_path.get()
        if not path or not os.path.isfile(path):
            messagebox.showerror("No file", "Pick a file first.")
            return
        self._debug = True
        lines = []

        def emit(msg):
            lines.append(msg)
            self.log(msg)

        info = parse_save_filename(path)
        emit("=" * 60)
        emit("DEBUG DUMP: %s" % path)
        try:
            st = os.stat(path)
            emit("  size=%d  mtime=%s"
                 % (st.st_size,
                    datetime.fromtimestamp(st.st_mtime).isoformat(
                        sep=" ", timespec="seconds")))
        except OSError as ex:
            emit("  stat failed: %s" % ex)
        emit("  classified as: type=%s universe=%s location=%s (%s)"
             % (info.get("type"), info.get("universe"),
                ("0x%X" % info["location"]) if info.get("location") is not None
                else None,
                info.get("location_name")))
        emit("  label: %s" % info.get("label"))

        try:
            raw = open(path, "rb").read()
        except OSError as ex:
            emit("  read failed: %s" % ex)
            return
        emit("  head16: %s" % raw[:16].hex(" "))
        if raw[:4] != MAGIC:
            emit("  NOT a KSC1 container (magic=%r)" % raw[:4])
            emit("=" * 60)
            return
        try:
            c = Container(raw)
        except Exception as ex:
            emit("  Container parse failed: %s" % ex)
            emit("=" * 60)
            return
        hdr_ok, dat_ok = c.verify()
        emit("  entries=%d  header_crc=%s  data_crc=%s"
             % (c.count, "ok" if hdr_ok else "BAD",
                "ok" if dat_ok else "BAD"))
        tags = {}
        for e in c.entries:
            tags[e["tag"]] = tags.get(e["tag"], 0) + 1
        emit("  tags: " + ", ".join(
            "%s×%d" % (t.decode("ascii", "replace"), n)
            for t, n in sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))))

        has_dict = self.dctx is not None or self._ensure_dict()
        # ALL entries — no cap
        for e in c.entries:
            chunk = c.chunk(e)
            tag_s = e["tag"].decode("ascii", "replace")
            line = "  [%d] id=%08x tag=%s size=%d head=%s" % (
                e["index"], e["id"], tag_s, e["size"], chunk[:8].hex())
            if has_dict:
                try:
                    doc, kind = unwrap(chunk, self.dctx)
                except Exception as ex:
                    line += "  unwrap-ERR=%s" % ex
                    emit(line)
                    continue
                if doc is None:
                    line += "  kind=None"
                    emit(line)
                    continue
                line += "  kind=%s doclen=%d" % (kind, len(doc))
                names = find_name_fields(doc)
                if names:
                    # Show all name segments (short code + display name)
                    shown = []
                    for start, length, text in names:
                        rawn = doc[start:start + length]
                        parts = [p.decode("utf-8", "replace")
                                 for p in rawn.split(b"\x00") if p]
                        shown.append("/".join(parts) if parts else text)
                    line += "  names=%s" % repr(shown)
                strs = []
                for m in re.finditer(rb"[\x20-\x7e]{4,40}", doc[:600]):
                    strs.append(m.group().decode("ascii", "replace"))
                    if len(strs) >= 4:
                        break
                if strs:
                    line += "  strs=%s" % strs
                # For CHAR entries, also report slot if present
                if e["tag"] == b"CHAR":
                    slot = character_slot_id(doc)
                    if slot is not None:
                        line += "  slotId=%s" % slot
            emit(line)
        emit("=" * 60)

        # Write full dump to a text file the user can share
        base = os.path.basename(path)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_name = "pk_debug_%s_%s.txt" % (base, stamp)
        candidates = [
            os.path.join(os.path.dirname(path), out_name),
            os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")),
                         "Desktop", out_name),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), out_name),
        ]
        out_path = None
        for cand in candidates:
            try:
                with open(cand, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines))
                    fh.write("\n")
                out_path = cand
                break
            except OSError:
                continue
        if out_path:
            self.log("Full debug written to: %s" % out_path)
            messagebox.showinfo(
                "Debug dump",
                "Full dump of all %d entries written to:\n\n%s\n\n"
                "(Also printed in the log panel.)"
                % (c.count, out_path))
        else:
            messagebox.showinfo(
                "Debug dump",
                "Dump printed to the log (could not write a file).")

    def browse_savefile(self):
        path = filedialog.askopenfilename(title="Select a Portal Knights save file")
        if path:
            self.savefile_path.set(path)
            self.save_combo.set(path)

    def browse_dictfile(self):
        path = filedialog.askopenfilename(
            title="Select pk_dict.bin",
            filetypes=[("Dictionary file", "*.bin"), ("All files", "*.*")])
        if path:
            self.dictfile_path.set(path)

    def extract_dict(self):
        exes = find_game_exe()
        exe_path = None
        if exes:
            exe_path = exes[0]
        else:
            self.log("Couldn't find %s automatically - pick it manually."
                      % GAME_EXE_NAME)
            exe_path = filedialog.askopenfilename(
                title="Select portal_knights_x64.exe",
                filetypes=[("Portal Knights exe", GAME_EXE_NAME),
                           ("All files", "*.*")])
            if not exe_path:
                return

        dest = default_dict_path()
        try:
            extract_dict_from_exe(exe_path, dest)
        except Exception as exc:
            self.log("Dictionary extraction failed: %s" % exc)
            messagebox.showerror("Extraction failed", str(exc))
            return

        self.dictfile_path.set(dest)
        self.log("Extracted dictionary from %s -> %s" % (exe_path, dest))
        messagebox.showinfo("Extracted",
                             "Dictionary extracted to:\n%s" % dest)

    # -- backup -----------------------------------------------------------

    def backup_current(self):
        path = self.savefile_path.get()
        if not path or not os.path.isfile(path):
            messagebox.showerror("No file", "Pick a valid save file first.")
            return
        userprofile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_dir = os.path.join(userprofile, "Downloads", "pk_backups", stamp)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(path))
        shutil.copy2(path, dest)
        self.log("Backed up to: %s" % dest)
        messagebox.showinfo("Backed up", "Saved a copy to:\n%s" % dest)

    # -- load / list --------------------------------------------------------

    def _find_char_backup(self, char_path):
        """Locate 0200000000000000 next to the character file, and any .bak."""
        folder = os.path.dirname(char_path)
        candidates = []
        game_bak = os.path.join(folder, "0200000000000000")
        if os.path.isfile(game_bak):
            candidates.append(("game backup 0200000000000000", game_bak))
        bak = char_path + ".bak"
        if os.path.isfile(bak):
            candidates.append(("tool .bak", bak))
        # Also any pk_backups copies
        return candidates

    def restore_character_backup(self):
        """Copy the game backup (0200…) or a .bak over 0100000000000000."""
        path = self.savefile_path.get()
        info = parse_save_filename(path) if path else {}
        # Always target the real character file in the same folder
        if info.get("type") in ("character", "character_backup") and path:
            folder = os.path.dirname(path)
        else:
            # Fall back to first known character save
            folder = None
            for row in self._all_saves:
                if row[4]["type"] == "character":
                    folder = row[1]
                    path = row[3]
                    break
        if not folder:
            messagebox.showerror("No folder", "Could not find the character save folder.")
            return
        target = os.path.join(folder, "0100000000000000")
        candidates = self._find_char_backup(target)
        if not candidates:
            messagebox.showerror(
                "No backup found",
                "Neither 0200000000000000 nor a .bak was found next to:\n%s"
                % target)
            return
        # Prefer the backup with more CHAR entries
        best = None
        best_count = -1
        details = []
        for label, bak_path in candidates:
            try:
                c = load_container(bak_path)
                n = sum(1 for e in c.entries if e["tag"] == b"CHAR")
                details.append("%s — %d CHAR entr(y/ies), %d bytes"
                               % (label, n, os.path.getsize(bak_path)))
                if n > best_count:
                    best_count = n
                    best = (label, bak_path, n)
            except Exception as ex:
                details.append("%s — unreadable (%s)" % (label, ex))
        if best is None:
            messagebox.showerror("Backup unreadable", "\n".join(details))
            return
        label, bak_path, n = best
        # Compare with current
        cur_n = 0
        if os.path.isfile(target):
            try:
                cur_n = sum(1 for e in load_container(target).entries
                            if e["tag"] == b"CHAR")
            except Exception:
                pass
        if not messagebox.askyesno(
                "Restore character backup?",
                "Current 0100000000000000 has %d CHAR entr(y/ies).\n"
                "Best backup: %s has %d CHAR entr(y/ies).\n\n"
                "%s\n\n"
                "This will OVERWRITE the current character file.\n"
                "A safety copy will be written first as .pre_restore.\n\n"
                "Continue?"
                % (cur_n, label, n, "\n".join(details))):
            return
        try:
            safety = target + ".pre_restore"
            if os.path.isfile(target):
                shutil.copy2(target, safety)
            shutil.copy2(bak_path, target)
            self.log("Restored characters from %s → %s (safety: %s)"
                     % (bak_path, target, safety))
            messagebox.showinfo(
                "Restored",
                "Character file restored from:\n%s\n\n"
                "Safety copy of the previous file:\n%s\n\n"
                "Click Refresh, then Characters filter, then Load selected."
                % (bak_path, safety))
            self.refresh_saves()
            self.savefile_path.set(target)
            self._update_file_info_label()
            self.content_nb.select(self.tab_chars)
            self.load_characters()
        except Exception as ex:
            messagebox.showerror("Restore failed", str(ex))

    def _warn_if_shrunk(self, path, count):
        """Optional shrink warning — no longer persists pk_seen_entries.json."""
        return


    def _ensure_dict(self):
        dictpath = self.dictfile_path.get()
        if not dictpath or not os.path.isfile(dictpath):
            messagebox.showerror(
                "No dictionary",
                "Pick pk_dict.bin first (the 262144-byte file extracted "
                "from the game exe).")
            return False
        if self.dctx is not None and self.cctx is not None:
            return True
        try:
            self.dctx, self.cctx = load_dict(dictpath)
            return True
        except ImportError:
            messagebox.showerror(
                "Missing package",
                "The 'zstandard' package isn't installed.\n\n"
                "Run:  pip install zstandard")
            return False
        except Exception as exc:
            messagebox.showerror("Dictionary error", str(exc))
            return False

    def load_selected(self):
        """Load whatever is currently selected — character, universe, or world."""
        path = self.savefile_path.get()
        if not path or not os.path.isfile(path):
            messagebox.showerror("No file", "Pick a valid save file first.")
            return
        info = parse_save_filename(path)
        self._current_info = info
        if info["type"] in ("character", "character_backup"):
            self.content_nb.select(self.tab_chars)
            self.load_characters()
        elif info["type"] == "universe":
            self.content_nb.select(self.tab_univ)
            self.load_universe_file(path)
        elif info["type"] == "world":
            self.content_nb.select(self.tab_world)
            self.load_world_file(path)
        elif info["type"] in ("options", "misc", "unknown"):
            # Don't pretend these are characters — dump and explain
            self.log("Selected %s (%s). Use Debug dump for details."
                     % (os.path.basename(path), info["label"]))
            self.debug_dump()
            messagebox.showinfo(
                "Not a character / universe / world",
                "%s\n\nThis is not the character file.\n"
                "Character data is in 0100000000000000.\n"
                "A debug dump was written to the log."
                % info["label"])
        else:
            self.debug_dump()

    def _refresh_universes_from_disk(self):
        """Re-scan disk then rebuild the Universes tab (same issue as Worlds)."""
        self._all_saves = find_saves()
        self._apply_save_filter()
        self.refresh_universe_list(quick=False)
        n = sum(1 for r in self._all_saves if r[4].get("type") == "universe")
        self.log("Universes on disk: %d" % n)

    def refresh_universe_list(self, quick=False):
        """Fill the Universes tab from discovered 03… files."""
        for row in self.univ_tree.get_children():
            self.univ_tree.delete(row)
        self._univ_rows = {}
        # Newest first already from find_saves; keep that order
        for label, root, fname, full, info, mtime in self._all_saves:
            if info["type"] != "universe":
                continue
            name = "(not loaded)"
            islands = "?"
            size = "?"
            try:
                size = "{:,}".format(os.path.getsize(full))
            except OSError:
                pass
            # Island count is available from the entry table without decompress
            try:
                c = load_container(full)
                islands = sum(1 for e in c.entries if e["tag"] == b"ILHD")
                if not quick and self.dctx is not None:
                    for e in c.entries:
                        if e["tag"] == b"USHD":
                            try:
                                doc, kind = unwrap(c.chunk(e), self.dctx)
                            except Exception:
                                doc = None
                            if doc:
                                n = extract_universe_name(doc)
                                if n:
                                    name = n
                            break
            except Exception as ex:
                name = "(error: %s)" % ex
            iid = self.univ_tree.insert(
                "", "end",
                values=(info.get("universe") or "?", name, islands, size, fname))
            self._univ_rows[iid] = full

    def open_selected_universe(self):
        sel = self.univ_tree.selection()
        if not sel:
            return
        path = self._univ_rows.get(sel[0])
        if path:
            self.savefile_path.set(path)
            self._update_file_info_label()
            self.load_universe_file(path)

    def load_universe_file(self, path):
        if not self._ensure_dict():
            return
        try:
            self.container = load_container(path)
        except Exception as exc:
            messagebox.showerror("Save file error", str(exc))
            return
        hdr_ok, dat_ok = self.container.verify()
        self.log("Loaded universe %s — %d entries, header %s, data %s"
                 % (path, self.container.count,
                    "ok" if hdr_ok else "BAD", "ok" if dat_ok else "BAD"))
        # Universe display name
        for e in self.container.entries:
            if e["tag"] == b"USHD":
                try:
                    doc, _k = unwrap(self.container.chunk(e), self.dctx)
                except Exception:
                    doc = None
                if doc:
                    uname = extract_universe_name(doc)
                    if uname:
                        self.log("  universe name: %s" % uname)
                break
        # Recount with names
        self.refresh_universe_list(quick=False)
        # Log islands with location names (from entry id) + seed/size
        n_named = 0
        for e in self.container.entries:
            if e["tag"] != b"ILHD":
                continue
            loc_code, loc_name = island_location_from_entry_id(e["id"])
            try:
                doc, kind = unwrap(self.container.chunk(e), self.dctx)
            except Exception:
                doc = None
            meta = extract_island_seed_and_size(doc) if doc else {}
            label = loc_name or ("0x%X" % loc_code)
            if loc_name:
                n_named += 1
            self.log("  island %-28s  id=%08x  seed=%s  size=%s"
                     % (label, e["id"], meta.get("seed"),
                        meta.get("islandSize")))
        self.log("  %d / %d islands matched known location names"
                 % (n_named,
                    sum(1 for e in self.container.entries
                        if e["tag"] == b"ILHD")))
        # Filter Worlds tab to this universe's slot only
        info = parse_save_filename(path)
        slot = info.get("universe")
        self._world_universe_filter = slot
        self.refresh_world_list(quick=True)
        if slot is not None:
            self.log("  Worlds tab filtered to universe slot %s" % slot)
            try:
                self.content_nb.select(self.tab_world)
            except Exception:
                pass

    def clear_world_universe_filter(self):
        """Show worlds from every universe again."""
        self._world_universe_filter = None
        self.refresh_world_list(quick=True)
        self.log("Worlds tab: showing all universes")

    def _filter_world_tree(self):
        """Show/hide world rows by the Search box (name, code, file, univ)."""
        q = (self.world_search_var.get() if hasattr(self, "world_search_var")
             else "") or ""
        q = q.strip().lower()
        all_iids = list(getattr(self, "_world_rows", {}).keys())
        if not all_iids:
            if hasattr(self, "world_search_status"):
                self.world_search_status.set("")
            return
        # Allow matching "105", "0105", "0x105", "autumn", "1-06"
        q_hex = q[2:] if q.startswith("0x") else q
        shown = 0
        for iid in all_iids:
            vals = (self.world_tree.item(iid, "values")
                    if self.world_tree.exists(iid) else ())
            path = self._world_rows.get(iid) or ""
            blob = " ".join(str(v) for v in vals).lower() + " " + path.lower()
            # also expand location code forms: 105, 0105, 0x105
            if len(vals) >= 2:
                loc = str(vals[1]).lower()
                blob += " %s 0%s 0x%s" % (loc, loc, loc)
                # zero-stripped / padded variants
                try:
                    n = int(loc, 16)
                    blob += " %x %03x %04x" % (n, n, n)
                except ValueError:
                    pass
            ok = (not q) or (q in blob) or (q_hex and q_hex in blob)
            if ok:
                try:
                    if not self.world_tree.exists(iid):
                        self.world_tree.reattach(iid, "", "end")
                    shown += 1
                except Exception:
                    try:
                        self.world_tree.move(iid, "", "end")
                        shown += 1
                    except Exception:
                        pass
            else:
                try:
                    self.world_tree.detach(iid)
                except Exception:
                    pass
        if hasattr(self, "world_search_status"):
            if q:
                self.world_search_status.set(
                    "%d / %d shown" % (shown, len(all_iids)))
            else:
                self.world_search_status.set("")

    def _refresh_worlds_from_disk(self):
        """Re-scan save folders on disk, then rebuild the Worlds list.

        The old Refresh only re-drew from a cached _all_saves list, so new
        04… files never appeared. This always calls find_saves() first.
        """
        before = sum(1 for r in getattr(self, "_all_saves", [])
                     if r[4].get("type") == "world")
        self._all_saves = find_saves()
        after = sum(1 for r in self._all_saves if r[4].get("type") == "world")
        # Keep the top combo in sync too
        self._apply_save_filter()
        self.refresh_universe_list(quick=True)
        self.refresh_world_list(quick=True)
        added = after - before
        msg = "Worlds on disk: %d" % after
        if added > 0:
            msg += "  (+%d new)" % added
        elif added < 0:
            msg += "  (%d removed)" % (-added)
        else:
            msg += "  (no change)"
        self.log(msg)
        if hasattr(self, "world_search_status"):
            self.world_search_status.set(msg)
        # Optional background chest-count pass for new files only
        self.after(100, lambda: self.refresh_world_list_full(
            incremental=True, silent=True))

    def refresh_world_list(self, quick=True):
        """Fill the Worlds tab from discovered 04… files.

        Sorted by last-modified (newest first). Chunk counts come from the
        KSC1 entry table (no decompress). Chest counts need decompress and
        are only filled when quick=False (after Load / Refresh with dict).
        When _world_universe_filter is set, only that universe's worlds show.
        """
        for row in self.world_tree.get_children():
            self.world_tree.delete(row)
        self._world_rows = {}
        # Collect then sort by mtime desc so newest worlds appear at top
        rows = []
        univ_filt = getattr(self, "_world_universe_filter", None)
        for label, root, fname, full, info, mtime in self._all_saves:
            if info["type"] != "world":
                continue
            if univ_filt is not None and info.get("universe") != univ_filt:
                continue
            rows.append((mtime, label, root, fname, full, info))
        rows.sort(key=lambda r: r[0], reverse=True)

        for mtime, label, root, fname, full, info in rows:
            chunks = "?"
            chests = "?"
            size = "?"
            try:
                size = "{:,}".format(os.path.getsize(full))
            except OSError:
                pass
            # Tag counts never need the zstd dictionary
            try:
                c = load_container(full)
                n_flck = sum(1 for e in c.entries if e["tag"] == b"FLCK")
                n_bkck = sum(1 for e in c.entries if e["tag"] == b"BKCK")
                n_ilas = sum(1 for e in c.entries if e["tag"] == b"ILAS")
                chunks = n_flck + n_bkck
                # BKCK entries large enough to possibly hold inventories
                # (shown even in quick mode as a lower-bound hint)
                big_bkck = sum(1 for e in c.entries
                               if e["tag"] == b"BKCK" and e["size"] >= 400)
                if quick:
                    # Lower-bound hint: BKCK large enough to hold inventory
                    chests = "~%d?" % big_bkck if big_bkck else "0"
                elif self.dctx is not None:
                    # Count inventory *entities*, not BKCK chunks
                    chest_n = 0
                    for e in c.entries:
                        if e["tag"] != b"BKCK":
                            continue
                        try:
                            doc, _k = unwrap(c.chunk(e), self.dctx)
                        except Exception:
                            continue
                        if doc:
                            chest_n += count_inventory_entities_in_doc(doc)
                    chests = chest_n
                if n_ilas and chunks == 0:
                    chunks = "ILAS×%d" % n_ilas
            except Exception as ex:
                chunks = "err"
                if getattr(self, "_debug", False):
                    self.log("world scan %s: %s" % (fname, ex))
            loc = info.get("location")
            loc_s = ("%X" % loc) if loc is not None else "?"
            # Prefer cached custom name from a previous Open / scan
            meta = getattr(self, "_world_meta", {}).get(full) or {}
            base_name = info.get("location_name") or "(unknown)"
            if meta.get("display_name"):
                display_name = meta["display_name"]
            else:
                display_name = base_name
            if meta.get("chests") is not None and not quick:
                chests = meta["chests"]
            iid = self.world_tree.insert(
                "", "end",
                values=(info.get("universe") if info.get("universe") is not None
                        else "?",
                        loc_s,
                        display_name,
                        chunks, chests, size, fname))
            self._world_rows[iid] = full
        self._filter_world_tree()

    def open_template_editor(self, preset_crc=None, preset_name=None):
        """Browse / add TemplateCRC names (saved to pk_templates.json)."""
        dlg = tk.Toplevel(self)
        dlg.title("TemplateCRC dictionary")
        dlg.geometry("720x480")
        ttk.Label(
            dlg,
            text="Names come from pk_templates.json (NPCs, enemies, props…). "
                 "Only Sign + Landing Pad are hard-coded. Edit JSON or add below.",
        ).pack(anchor="w", padx=8, pady=6)

        search_fr = ttk.Frame(dlg)
        search_fr.pack(fill="x", padx=8)
        ttk.Label(search_fr, text="Filter:").pack(side="left")
        fvar = tk.StringVar()
        ttk.Entry(search_fr, textvariable=fvar, width=28).pack(
            side="left", padx=4)

        cols = ("hex", "dec", "name", "kind", "source")
        tree = ttk.Treeview(dlg, columns=cols, show="headings", height=14)
        for col, text, w in (("hex", "Hash (hex)", 110),
                             ("dec", "Hash (dec)", 110),
                             ("name", "Name", 240),
                             ("kind", "Kind", 70),
                             ("source", "Source", 70)):
            tree.heading(col, text=text)
            tree.column(col, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=8, pady=4)

        def refill(_evt=None):
            q = (fvar.get() or "").strip().lower()
            tree.delete(*tree.get_children())
            rows = []
            for crc, name in sorted(all_known_templates().items(),
                                    key=lambda kv: (kv[1].lower(), kv[0])):
                kind = "npc"
                if crc in ENEMY_TEMPLATE_CRCS:
                    kind = "enemy"
                elif crc in WORLD_TEMPLATES and crc not in NPC_TEMPLATES:
                    kind = "world"
                if crc in NPC_TEMPLATES and "(trader)" in name.lower():
                    kind = "trader"
                meta = _USER_TEMPLATE_META.get(crc)
                source = "user" if meta else "built-in"
                if meta:
                    kind = meta.get("kind") or kind
                blob = ("%s %s %s %s" % (name, kind, crc, "0x%08X" % (int(crc) & 0xFFFFFFFF))).lower()
                if q and q not in blob:
                    continue
                rows.append((crc, name, kind, source))
            for crc, name, kind, source in rows:
                tree.insert("", "end", values=(
                    "0x%08X" % crc, crc, name, kind, source))

        fvar.trace_add("write", lambda *_: refill())
        refill()

        add_fr = ttk.LabelFrame(dlg, text="Add / update")
        add_fr.pack(fill="x", padx=8, pady=6)
        ttk.Label(add_fr, text="CRC (hex or dec):").grid(
            row=0, column=0, sticky="w", padx=4, pady=2)
        crc_var = tk.StringVar(
            value=("0x%08X" % (int(preset_crc) & 0xFFFFFFFF)
                   if preset_crc is not None else ""))
        ttk.Entry(add_fr, textvariable=crc_var, width=18).grid(
            row=0, column=1, sticky="w", padx=4, pady=2)
        ttk.Label(add_fr, text="Name:").grid(
            row=0, column=2, sticky="w", padx=4, pady=2)
        name_var = tk.StringVar(value=preset_name or "")
        ttk.Entry(add_fr, textvariable=name_var, width=28).grid(
            row=0, column=3, sticky="w", padx=4, pady=2)
        ttk.Label(add_fr, text="Kind:").grid(
            row=1, column=0, sticky="w", padx=4, pady=2)
        kind_var = tk.StringVar(value="world")
        ttk.Combobox(add_fr, textvariable=kind_var, width=12, state="readonly",
                     values=("world", "enemy", "npc", "trader", "quest")).grid(
            row=1, column=1, sticky="w", padx=4, pady=2)

        def do_add():
            raw = (crc_var.get() or "").strip()
            nm = (name_var.get() or "").strip()
            if not raw or not nm:
                messagebox.showerror("Missing", "CRC and name required.",
                                     parent=dlg)
                return
            try:
                crc = int(raw, 0) & 0xFFFFFFFF
            except ValueError:
                messagebox.showerror("Bad CRC", "Use hex (0x…) or decimal.",
                                     parent=dlg)
                return
            try:
                path = save_user_template(crc, nm, kind_var.get())
            except Exception as ex:
                messagebox.showerror("Save failed", str(ex), parent=dlg)
                return
            self.log("Template saved: 0x%08X → %s (%s) in %s"
                     % (crc, nm, kind_var.get(), path))
            refill()
            messagebox.showinfo(
                "Saved",
                "0x%08X = %s\n\nWritten to:\n%s" % (crc, nm, path),
                parent=dlg)

        ttk.Button(add_fr, text="Save to JSON", command=do_add).grid(
            row=1, column=3, sticky="e", padx=4, pady=4)

        def use_selected():
            sel = tree.selection()
            if not sel:
                return
            vals = tree.item(sel[0], "values")
            crc_var.set(vals[0])
            name_var.set(vals[2])
            kind_var.set(vals[3] if vals[3] in (
                "world", "enemy", "npc", "trader") else "world")

        tree.bind("<<TreeviewSelect>>", lambda _e: use_selected())
        bf = ttk.Frame(dlg)
        bf.pack(fill="x", padx=8, pady=6)
        ttk.Button(bf, text="Close", command=dlg.destroy).pack(side="right")

    def _autoload_world_scan(self):
        """Background chest-count scan after startup / refresh.

        Only scans worlds that have no cached count yet, or whose file
        mtime is newer than the last scan. Avoids re-decompressing every
        world on every launch.
        """
        if self.dctx is None:
            return
        if self._world_scan_thread is not None and \
                self._world_scan_thread.is_alive():
            return
        self.refresh_world_list_full(incremental=True, silent=True)

    def refresh_world_list_full(self, incremental=False, silent=False):
        """Full scan: populate the list immediately (quick pass, no
        decompression), then fill in accurate chest counts as they're
        computed on a background thread.

        This is the slow path - it decompresses every BKCK chunk in
        every world save - so it never runs on the UI thread. Results
        stream back through _world_scan_queue and are applied to the
        Treeview from _poll_world_scan_queue, which always runs on the
        main thread via self.after(); the worker thread itself never
        touches a Tkinter widget.

        incremental=True: only scan worlds missing a cached chest count
        or whose file mtime is newer than the last scan.
        silent=True: don't pop error dialogs (used by auto-start).
        """
        if self._world_scan_thread is not None and \
                self._world_scan_thread.is_alive():
            return  # a scan is already running
        if self.dctx is None:
            if not silent:
                messagebox.showerror(
                    "No dictionary",
                    "Load a zstd dictionary first (see the Dictionary row "
                    "above) - chest counts need it to decompress chunks.")
            return

        # Fast pass first so the list is populated immediately; the
        # quick lower-bound chest hint shows until the real count for
        # that row comes back from the worker.
        self.refresh_world_list(quick=True)
        all_targets = list(self._world_rows.items())  # [(iid, full_path), ...]
        if not all_targets:
            return

        if not hasattr(self, "_world_meta"):
            self._world_meta = {}

        if incremental:
            targets = []
            for iid, full in all_targets:
                meta = self._world_meta.get(full) or {}
                need = meta.get("chests") is None
                if not need:
                    try:
                        mtime = os.path.getmtime(full)
                        scanned_at = meta.get("scanned_mtime")
                        if scanned_at is None or mtime > scanned_at + 0.5:
                            need = True
                    except OSError:
                        need = True
                if need:
                    targets.append((iid, full))
            if not targets:
                # Everything already cached — just paint cached counts
                for iid, full in all_targets:
                    meta = self._world_meta.get(full) or {}
                    if meta.get("chests") is not None and \
                            self.world_tree.exists(iid):
                        self.world_tree.set(iid, "chests", meta["chests"])
                return
        else:
            targets = all_targets

        self._world_scan_cancel.clear()
        self._set_scanning_state(True, total=len(targets))
        dctx = self.dctx

        def worker():
            for i, (iid, full) in enumerate(targets):
                if self._world_scan_cancel.is_set():
                    self._world_scan_queue.put(("cancelled", None, None))
                    return
                chest_n = "err"
                scanned_mtime = None
                try:
                    # Only one thread may use the shared zstd context at
                    # a time; the main-thread buttons that could also
                    # decompress are disabled for the scan's duration
                    # (see _scan_lock_widgets), so this lock only ever
                    # guards against another scan, not real contention.
                    with self._dctx_lock:
                        try:
                            scanned_mtime = os.path.getmtime(full)
                        except OSError:
                            scanned_mtime = None
                        c = load_container(full)
                        n = 0
                        for e in c.entries:
                            if e["tag"] != b"BKCK":
                                continue
                            try:
                                doc, _k = unwrap(c.chunk(e), dctx)
                            except Exception:
                                continue
                            if doc:
                                n += count_inventory_entities_in_doc(doc)
                        chest_n = n
                except Exception:
                    chest_n = "err"
                self._world_scan_queue.put(
                    ("row", iid, (chest_n, full, scanned_mtime)))
                self._world_scan_queue.put(("progress", i + 1, len(targets)))
            self._world_scan_queue.put(("done", None, None))

        self._world_scan_thread = threading.Thread(target=worker,
                                                    daemon=True)
        self._world_scan_thread.start()
        self.after(50, self._poll_world_scan_queue)

    def cancel_world_scan(self):
        if self._world_scan_thread is not None and \
                self._world_scan_thread.is_alive():
            self._world_scan_cancel.set()
            self.world_scan_status.set("Cancelling…")

    def _set_scanning_state(self, scanning, total=None):
        state = "disabled" if scanning else "normal"
        self.world_full_scan_btn.configure(state=state)
        for w in self._scan_lock_widgets:
            w.configure(state=state)
        self.world_scan_cancel_btn.configure(
            state=("normal" if scanning else "disabled"))
        self.world_scan_status.set(
            "Scanning %d world(s) for chest counts…" % (total or 0)
            if scanning else "")

    def _poll_world_scan_queue(self):
        """Runs on the main thread via self.after(). Drains whatever the
        worker thread has queued and applies it to the Treeview - the
        only place scan results ever touch a widget."""
        try:
            while True:
                kind, a, b = self._world_scan_queue.get_nowait()
                if kind == "row":
                    iid = a
                    # Worker may send plain chest_n or
                    # (chest_n, full_path, scanned_mtime) for caching.
                    if isinstance(b, tuple):
                        chest_n, full, scanned_mtime = b
                        if not hasattr(self, "_world_meta"):
                            self._world_meta = {}
                        meta = self._world_meta.get(full) or {}
                        meta["chests"] = chest_n
                        if scanned_mtime is not None:
                            meta["scanned_mtime"] = scanned_mtime
                        self._world_meta[full] = meta
                    else:
                        chest_n = b
                    if self.world_tree.exists(iid):
                        self.world_tree.set(iid, "chests", chest_n)
                elif kind == "progress":
                    done, total = a, b
                    self.world_scan_status.set(
                        "Scanning… %d / %d world(s)" % (done, total))
                elif kind in ("done", "cancelled"):
                    self._set_scanning_state(False)
                    self._world_scan_thread = None
                    self.log("World scan cancelled." if kind == "cancelled"
                             else "World scan complete.")
                    return
        except queue.Empty:
            pass
        self.after(50, self._poll_world_scan_queue)

    def open_selected_world(self):
        """Explicitly load the highlighted world (selection alone does nothing)."""
        sel = self.world_tree.selection()
        if not sel:
            messagebox.showinfo("No selection",
                                "Highlight a world row first.")
            return
        path = self._world_rows.get(sel[0])
        if path:
            self.savefile_path.set(path)
            self._update_file_info_label()
            self.load_world_file(path)

    def _guess_custom_world_name(self, container):
        """Pick a human title from sign texts (e.g. All Items World banners).

        Prefers signs that look like welcome / version banners; strips Unity
        rich-text tags like <style=bold>.
        """
        if self.dctx is None:
            return None
        candidates = []
        for e in container.entries:
            if e["tag"] != b"BKCK":
                continue
            try:
                doc, _k = unwrap(container.chunk(e), self.dctx)
            except Exception:
                continue
            if not doc or b"User Editable String Component" not in doc:
                continue
            try:
                nodes, _ = bson_parse(bytearray(doc))
            except Exception:
                continue
            for s in extract_world_signs(nodes):
                text = (s.get("text") or "").strip()
                if not text or len(text) < 8:
                    continue
                # Strip simple rich-text tags
                clean = re.sub(r"<[^>]+>", "", text).strip()
                if not clean or len(clean) < 8:
                    continue
                score = 0
                low = clean.lower()
                if "welcome" in low:
                    score += 5
                if "all items" in low:
                    score += 8
                if "world" in low:
                    score += 3
                if re.search(r"v(?:ersion)?\s*\d", low) or re.search(
                        r"\d+\.\d+", low):
                    score += 4
                if "updated" in low:
                    score += 2
                # Prefer mid-length titles over huge instruction walls
                if 12 <= len(clean) <= 80:
                    score += 2
                elif len(clean) > 120:
                    score -= 3
                candidates.append((score, clean))
        if not candidates:
            return None
        candidates.sort(key=lambda t: (-t[0], len(t[1])))
        best_score, best = candidates[0]
        if best_score < 3:
            return None
        # Truncate for the list column
        if len(best) > 60:
            best = best[:57] + "..."
        return best

    def _update_world_row(self, path):
        """Refresh chest count + custom name for one world row only."""
        if not hasattr(self, "_world_meta"):
            self._world_meta = {}
        try:
            c = self.container if (
                self.container and self.savefile_path.get() == path
            ) else load_container(path)
        except Exception:
            return
        chest_n = 0
        if self.dctx is not None:
            for e in c.entries:
                if e["tag"] != b"BKCK":
                    continue
                try:
                    doc, _k = unwrap(c.chunk(e), self.dctx)
                except Exception:
                    continue
                if doc:
                    chest_n += count_inventory_entities_in_doc(doc)
        custom = self._guess_custom_world_name(c)
        info = parse_save_filename(path)
        base = info.get("location_name") or "(unknown)"
        display = ("%s — %s" % (custom, base)) if custom else base
        try:
            scanned_mtime = os.path.getmtime(path)
        except OSError:
            scanned_mtime = None
        self._world_meta[path] = {
            "chests": chest_n,
            "custom_name": custom,
            "display_name": display,
            "scanned_mtime": scanned_mtime,
        }
        # Patch the tree row if present
        for iid, full in list(getattr(self, "_world_rows", {}).items()):
            if full != path:
                continue
            vals = list(self.world_tree.item(iid, "values"))
            # columns: univ, location, name, chunks, chests, size, file
            if len(vals) >= 5:
                vals[2] = display
                vals[4] = chest_n
                self.world_tree.item(iid, values=vals)
            break
        if custom:
            self.log("  custom name: %s" % custom)
        self.log("  chest entities: %d" % chest_n)

    def load_world_file(self, path):
        if not self._ensure_dict():
            return
        try:
            self.container = load_container(path)
        except Exception as exc:
            messagebox.showerror("Save file error", str(exc))
            return
        hdr_ok, dat_ok = self.container.verify()
        info = parse_save_filename(path)
        self.log("Loaded world %s — %s — %d entries, header %s, data %s"
                 % (path, info.get("label"), self.container.count,
                    "ok" if hdr_ok else "BAD", "ok" if dat_ok else "BAD"))
        tags = {}
        for e in self.container.entries:
            tags[e["tag"]] = tags.get(e["tag"], 0) + 1
        self.log("  tags: " + ", ".join(
            "%s×%d" % (t.decode("ascii", "replace"), n)
            for t, n in sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))))
        # Only update THIS world's row (do NOT rescan every world — that was
        # the multi-second stall on Open selected world).
        self._update_world_row(path)

    def _resolve_world_path(self):
        """Return (path, info) for the currently targeted world file, or (None, {}).

        Prefer the Worlds-tab selection so Map/Inventories always match the
        highlighted row, even if the top combo still points at another file.
        """
        sel = ()
        try:
            sel = self.world_tree.selection()
        except Exception:
            sel = ()
        if sel:
            path = self._world_rows.get(sel[0])
            info = parse_save_filename(path) if path else {}
            if path and info.get("type") == "world":
                return path, info
        path = self.savefile_path.get()
        info = parse_save_filename(path) if path else {}
        if path and info.get("type") == "world":
            return path, info
        return None, {}

    def _scan_world_bkck(self, path):
        """Yield (entry, doc, kind, nodes, container) for every BKCK that parses."""
        container = load_container(path)
        for e in container.entries:
            if e["tag"] != b"BKCK":
                continue
            try:
                doc, kind = unwrap(container.chunk(e), self.dctx)
            except Exception:
                continue
            if not doc:
                continue
            try:
                nodes, _total = bson_parse(bytearray(doc))
            except Exception:
                continue
            yield e, doc, kind, nodes, container

    def open_world_chests(self):
        """List chests (Server Inventory) with positions; allow item/stack edits."""
        path, info = self._resolve_world_path()
        if not path:
            messagebox.showinfo(
                "No world",
                "Select a world file first (filter → Worlds, or the Worlds tab).")
            return
        if not self._ensure_dict():
            return
        try:
            scanned = list(self._scan_world_bkck(path))
        except Exception as exc:
            messagebox.showerror("Save file error", str(exc))
            return

        # Flatten to per-chest rows (entity-level)
        rows = []  # (e, doc, kind, chest_dict, container)
        for e, doc, kind, nodes, container in scanned:
            if b"Server Inventory Component" not in doc:
                continue
            for chest in extract_world_chests(nodes):
                rows.append((e, doc, kind, chest, container))

        dlg = tk.Toplevel(self)
        dlg.title("Inventories — %s" % (info.get("location_name") or
                                        os.path.basename(path)))
        dlg.geometry("920x560")
        ttk.Label(
            dlg,
            text="%d inventory entity(ies) (chests, mannequins, pet boxes…). "
                 "Click column headers to sort. Double-click to edit. "
                 "Empty IBP is normal for mannequins (items live in IEQ)."
                 % len(rows),
        ).pack(anchor="w", padx=8, pady=6)

        # --- search bar: find which chests hold an item, show total count ---
        # Uses item_table_merged.json for names + "not in world" checks.
        search_fr = ttk.Frame(dlg)
        search_fr.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(search_fr, text="Find item:").pack(side="left")
        search_var = tk.StringVar()
        search_entry = ttk.Entry(search_fr, textvariable=search_var, width=28)
        search_entry.pack(side="left", padx=4)
        search_status = tk.StringVar(value="")
        ttk.Label(search_fr, textvariable=search_status,
                  foreground="#444").pack(side="left", padx=6)

        cols = ("idx", "kind", "x", "y", "z", "items", "arrays", "chunk")
        tree = ttk.Treeview(dlg, columns=cols, show="headings", height=12,
                            selectmode="browse")
        for col, text, w in (("idx", "#", 40),
                             ("kind", "Kind", 100),
                             ("x", "X", 70), ("y", "Y", 70), ("z", "Z", 70),
                             ("items", "Items", 60),
                             ("arrays", "Arrays", 180),
                             ("chunk", "Chunk id", 90)):
            tree.heading(col, text=text)
            tree.column(col, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=8, pady=4)
        treeview_enable_sort(tree, numeric_cols={"idx", "x", "y", "z", "items"})

        # Pre-index every (crc, stack, name) per chest for fast search
        row_data = {}
        chest_item_index = []  # parallel to rows: list of (crc, stack, name)

        def _chest_items(chest):
            out = []
            for arr in chest.get("invs") or []:
                for _si, entry in inventory_slot_map(arr).items():
                    fields = item_entry_fields(entry)
                    ii = fields.get("II")
                    sc = fields.get("SC")
                    if not ii:
                        continue
                    crc = ii["value"] if isinstance(ii, dict) else ii
                    stack = sc["value"] if sc is not None else 1
                    name = item_name_for_crc(crc) or ""
                    out.append((int(crc) & 0xFFFFFFFF, int(stack or 1), name))
            return out

        for i, (e, doc, kind, chest, container) in enumerate(rows):
            pos = chest.get("pos") or (None, None, None)
            kind_label, counts = classify_inventory_entity(chest)
            # Only show arrays that actually have items (less noise)
            arr_names = ["%s(%d)" % (k, n) for k, n in counts.items() if n]
            if not arr_names:
                arr_names = ["(empty)"]
            iid = tree.insert("", "end", values=(
                i + 1,
                kind_label,
                ("%.1f" % pos[0]) if pos[0] is not None else "?",
                ("%.1f" % pos[1]) if pos[1] is not None else "?",
                ("%.1f" % pos[2]) if pos[2] is not None else "?",
                chest["item_count"],
                ", ".join(arr_names),
                "%08X" % (int(e.get("id", 0)) & 0xFFFFFFFF),
            ))
            row_data[iid] = (e, doc, kind, chest, container)
            chest_item_index.append(_chest_items(chest))

        all_iids = list(tree.get_children(""))

        def apply_item_search(_evt=None):
            q = (search_var.get() or "").strip().lower()
            # Restore everything when the query is empty
            if not q:
                for iid in all_iids:
                    if not tree.exists(iid):
                        tree.reattach(iid, "", "end")
                search_status.set("")
                return
            # Match by name substring or hex/decimal crc
            q_crc = None
            try:
                q_crc = int(q, 0) & 0xFFFFFFFF
            except ValueError:
                pass
            match_iids = []
            total_stack = 0
            chests_with = 0
            for iid, items in zip(all_iids, chest_item_index):
                hit_stack = 0
                for crc, stack, name in items:
                    if q_crc is not None and crc == q_crc:
                        hit_stack += stack
                    elif q and q in (name or "").lower():
                        hit_stack += stack
                    elif q and q in ("%08x" % crc):
                        hit_stack += stack
                if hit_stack:
                    match_iids.append(iid)
                    total_stack += hit_stack
                    chests_with += 1
            # Detach non-matches, keep matches visible
            for iid in all_iids:
                if iid in match_iids:
                    if not tree.exists(iid):
                        tree.reattach(iid, "", "end")
                else:
                    try:
                        tree.detach(iid)
                    except Exception:
                        pass
            if match_iids:
                search_status.set(
                    "Found in %d chest(s) · total count %d"
                    % (chests_with, total_stack))
            else:
                # Cross-check item_table_merged.json so "missing" is clear
                table_hits = item_search(q, limit=8) if q_crc is None else []
                if q_crc is not None:
                    nm = item_name_for_crc(q_crc)
                    if nm:
                        search_status.set(
                            "Missing in world · 0 in chests · "
                            "item_table: %s" % nm)
                    else:
                        search_status.set(
                            "Missing in world · unknown CRC 0x%08X "
                            "(not in item_table_merged.json)" % q_crc)
                elif table_hits:
                    names = ", ".join(
                        (r.get("name") or "?") for r in table_hits[:3])
                    search_status.set(
                        "Missing in world · 0 in chests · "
                        "item_table matches: %s" % names)
                else:
                    search_status.set(
                        "No match in chests or item_table_merged.json")

        def show_missing_from_table():
            """List items from item_table_merged.json that match the query
            (or a broad category slice) but are not present in any chest
            in this world. Shows name + hash so you can add them."""
            q = (search_var.get() or "").strip()
            present = set()
            for items in chest_item_index:
                for crc, _stack, _name in items:
                    present.add(crc)
            # Query table: empty query → warn (table is huge)
            if not q:
                messagebox.showinfo(
                    "Missing in world",
                    "Type a name fragment or category keyword in "
                    "Find item first (e.g. 'sword', 'helm', 'rift'), "
                    "then click Missing…\n\n"
                    "That filters item_table_merged.json and lists "
                    "matches that are not in any chest here.",
                    parent=dlg)
                return
            table_hits = item_search(q, limit=400)
            if not table_hits:
                messagebox.showinfo(
                    "Missing in world",
                    "No items in item_table_merged.json match %r."
                    % q, parent=dlg)
                return
            missing = []
            for rec in table_hits:
                h = rec.get("hash")
                if h is None:
                    continue
                crc = int(h) & 0xFFFFFFFF
                if crc not in present:
                    missing.append(rec)
            md = tk.Toplevel(dlg)
            md.title("Missing in world — %r (%d of %d table hits)"
                     % (q, len(missing), len(table_hits)))
            md.geometry("720x420")
            ttk.Label(
                md,
                text="Items in item_table_merged.json matching %r that "
                     "are NOT in any chest / mannequin in this world. "
                     "%d present, %d missing."
                     % (q, len(table_hits) - len(missing), len(missing)),
            ).pack(anchor="w", padx=8, pady=6)
            cols = ("name", "category", "idx", "hash", "hash_hex")
            mt = ttk.Treeview(md, columns=cols, show="headings", height=16)
            for col, text, w in (("name", "Name", 240),
                                 ("category", "Category", 120),
                                 ("idx", "table_index", 80),
                                 ("hash", "Hash (dec)", 100),
                                 ("hash_hex", "Hash (hex)", 100)):
                mt.heading(col, text=text)
                mt.column(col, width=w, anchor="w")
            mt.pack(fill="both", expand=True, padx=8, pady=4)
            for rec in missing:
                h = int(rec.get("hash") or 0) & 0xFFFFFFFF
                ti = _table_index_of(rec)
                mt.insert("", "end", values=(
                    rec.get("name") or "?",
                    rec.get("category") or "",
                    ti if ti is not None else "",
                    h,
                    "0x%08X" % h,
                ))
            if not missing:
                ttk.Label(md, text="All matching table items are present "
                                   "in this world.",
                          foreground="#060").pack(anchor="w", padx=8)
            bf = ttk.Frame(md)
            bf.pack(fill="x", padx=8, pady=6)

            def copy_missing():
                lines = ["name\tcategory\ttable_index\thash\thash_hex"]
                for rec in missing:
                    h = int(rec.get("hash") or 0) & 0xFFFFFFFF
                    ti = _table_index_of(rec)
                    lines.append("%s\t%s\t%s\t%d\t0x%08X" % (
                        rec.get("name") or "?",
                        rec.get("category") or "",
                        "" if ti is None else ti,
                        h, h))
                md.clipboard_clear()
                md.clipboard_append("\n".join(lines))
                messagebox.showinfo("Copied",
                                    "Copied %d missing item(s)." % len(missing),
                                    parent=md)

            ttk.Button(bf, text="Copy list", command=copy_missing).pack(
                side="left")
            ttk.Button(bf, text="Close", command=md.destroy).pack(
                side="right")

        search_entry.bind("<Return>", apply_item_search)
        search_entry.bind("<KeyRelease>", lambda e: (
            apply_item_search() if not search_var.get().strip()
            else None))
        ttk.Button(search_fr, text="Search",
                   command=apply_item_search).pack(side="left", padx=2)
        ttk.Button(search_fr, text="Missing…",
                   command=show_missing_from_table).pack(side="left", padx=2)
        ttk.Button(search_fr, text="Clear",
                   command=lambda: (search_var.set(""),
                                    apply_item_search())
                   ).pack(side="left")

        detail = tk.Text(dlg, height=12, wrap="word", font=("Courier New", 9))
        detail.pack(fill="both", expand=True, padx=8, pady=4)

        def show_detail(iid):
            detail.configure(state="normal")
            detail.delete("1.0", "end")
            if iid not in row_data:
                detail.configure(state="disabled")
                return
            e, doc, kind, chest, container = row_data[iid]
            pos = chest.get("pos")
            kind_label, _c = classify_inventory_entity(chest)
            detail.insert("end", "Chunk %08X  kind=%s  template=%s\n"
                          % (e["id"], kind_label,
                             ("0x%08X" % chest["template"])
                             if chest.get("template") else "?"))
            if pos:
                detail.insert("end", "Position  x=%.3f  y=%.3f  z=%.3f\n"
                              % pos)
            for arr in chest["invs"]:
                slots = inventory_slot_map(arr)
                detail.insert("end", "\n%s — %d item(s)\n"
                              % (arr["key"], len(slots)))
                for si in sorted(slots):
                    entry = slots[si]
                    fields = item_entry_fields(entry)
                    ii = fields.get("II")
                    sc = fields.get("SC")
                    crc = ii["value"] if ii else 0
                    stack = sc["value"] if sc is not None else 1
                    label = item_name_for_crc(crc) or (
                        "0x%08X" % (crc & 0xFFFFFFFF))
                    detail.insert("end", "  slot %2d  x%-4s  %s\n"
                                  % (si + 1, stack, label))
            detail.configure(state="disabled")

        def on_select(_evt=None):
            sel = tree.selection()
            if sel:
                show_detail(sel[0])

        def edit_selected(_evt=None):
            sel = tree.selection()
            if not sel or sel[0] not in row_data:
                return
            e, doc, kind, chest, container = row_data[sel[0]]
            self._edit_chest_inventory(path, e, doc, kind, chest, container,
                                       on_done=lambda: (
                                           dlg.destroy(),
                                           self.open_world_chests()))

        tree.bind("<<TreeviewSelect>>", on_select)
        tree.bind("<Double-1>", edit_selected)
        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=8, pady=6)
        ttk.Button(btns, text="Edit selected chest…",
                   command=edit_selected).pack(side="left")
        ttk.Button(btns, text="Close", command=dlg.destroy).pack(side="right")

    def _edit_chest_inventory(self, path, e, doc, kind, chest, container,
                              on_done=None):
        """Dialog to change item / stack in one chest's inventory arrays."""
        # Load this file as the active container so commit_bson_edit works
        self.savefile_path.set(path)
        self.container = container
        self._update_file_info_label()

        dlg = tk.Toplevel(self)
        pos = chest.get("pos")
        pos_s = (" @ %.1f, %.1f, %.1f" % pos) if pos else ""
        kind_label, _counts = classify_inventory_entity(chest)
        dlg.title("Edit %s%s" % (kind_label, pos_s))
        dlg.geometry("900x520")

        nb = ttk.Notebook(dlg)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        meta = {}  # iid -> (arr_key, si, entry, fields, arr_node)

        # Prefer opening a tab that actually has items (mannequins → IEQ)
        tab_index = 0
        best_tab = 0
        best_count = -1
        # Stable order: IBP, IAB, IEQ, VEQ, PET, then any others
        order = {"IBP": 0, "IAB": 1, "IEQ": 2, "VEQ": 3, "PET": 4}
        invs_sorted = sorted(
            chest["invs"],
            key=lambda a: order.get(a["key"], 50))

        trees = []
        trees_by_key = {}

        # Chest context: majority categories from unambiguous items, used
        # to pick among shared-hash rows (same hash_hex, different names).
        ctx_crcs = []
        for arr in invs_sorted:
            for _si, entry in inventory_slot_map(arr).items():
                fields = item_entry_fields(entry)
                ii = fields.get("II")
                if ii and ii.get("value") is not None:
                    c = _as_u32(ii.get("value"))
                    if c is not None:
                        ctx_crcs.append(c)
        chest_cats = chest_context_categories(ctx_crcs)

        for arr in invs_sorted:
            frame = ttk.Frame(nb)
            slots = inventory_slot_map(arr)
            nslots = len(slots)
            nb.add(frame, text="%s (%d)" % (arr["key"], nslots))
            if nslots > best_count:
                best_count = nslots
                best_tab = tab_index
            tab_index += 1
            cols = ("slot", "name", "category", "price", "stat", "hash",
                    "stack", "cap")
            tree = ttk.Treeview(frame, columns=cols, show="headings",
                                height=14, selectmode="browse")
            for col, text, w in (("slot", "Slot", 45),
                                 ("name", "Item", 200),
                                 ("category", "Category", 100),
                                 ("price", "Price", 50),
                                 ("stat", "Dmg/Def", 65),
                                 ("hash", "Hash (hex / dec)", 150),
                                 ("stack", "Stack", 50),
                                 ("cap", "Max", 45)):
                tree.heading(col, text=text)
                tree.column(col, width=w, anchor="w")
            tree.pack(fill="both", expand=True, padx=4, pady=4)
            trees.append(tree)
            trees_by_key[arr["key"]] = tree
            treeview_enable_sort(
                tree, numeric_cols={"slot", "price", "stack", "cap"})
            for si in sorted(slots):
                entry = slots[si]
                fields = item_entry_fields(entry)
                ii = fields.get("II")
                sc = fields.get("SC")
                crc = ii["value"] if ii else 0
                stack = sc["value"] if sc is not None else 1
                cap = item_max_stack(crc) if ii else ""
                label = item_name_for_crc(
                    crc, category_hint=chest_cats) or "(unknown)"
                if ii:
                    crc32 = int(crc) & 0xFFFFFFFF
                    hash_s = "0x%08X (%d)" % (crc32, crc32)
                else:
                    hash_s = ""
                rec = item_record_for_crc(
                    crc, category_hint=chest_cats) if ii else None
                cat = (rec or {}).get("category") or ""
                price = (rec or {}).get("price")
                price_s = "" if price in (None, 0) else str(price)
                stats = parse_item_description((rec or {}).get("description"))
                if stats.get("damage") is not None:
                    stat_s = "Dmg %d" % stats["damage"]
                elif stats.get("defence") is not None:
                    stat_s = "Def %d" % stats["defence"]
                else:
                    stat_s = ""
                iid = tree.insert("", "end", values=(
                    si + 1, label, cat, price_s, stat_s, hash_s, stack, cap))
                meta[iid] = (arr["key"], si, entry, fields, arr)

            def apply_slot_display(arr_key, si, crc, fields):
                """Update one tree row after an II change; no decompress."""
                if fields.get("II") is not None:
                    fields["II"]["value"] = crc
                label = item_name_for_crc(crc, category_hint=chest_cats) or "(unknown)"
                rec = item_record_for_crc(crc, category_hint=chest_cats)
                cat = (rec or {}).get("category") or ""
                price = (rec or {}).get("price")
                price_s = "" if price in (None, 0) else str(price)
                stats = parse_item_description(
                    (rec or {}).get("description"))
                if stats.get("damage") is not None:
                    stat_s = "Dmg %d" % stats["damage"]
                elif stats.get("defence") is not None:
                    stat_s = "Def %d" % stats["defence"]
                else:
                    stat_s = ""
                hash_s = "0x%08X (%d)" % (crc, crc)
                cap = item_max_stack(crc)
                for iid, (ak, s, entry, flds, arr) in list(meta.items()):
                    if ak != arr_key or s != si:
                        continue
                    for tw in trees:
                        if not tw.exists(iid):
                            continue
                        vals = list(tw.item(iid, "values"))
                        # slot, name, category, price, stat, hash, stack, cap
                        if len(vals) >= 8:
                            vals[1] = label
                            vals[2] = cat
                            vals[3] = price_s
                            vals[4] = stat_s
                            vals[5] = hash_s
                            vals[7] = cap
                            tw.item(iid, values=vals)
                        meta[iid] = (ak, s, entry, fields, arr)
                        return

            def refresh_labels():
                """Re-resolve names from item table without closing the dialog."""
                for iid, (ak, si, entry, fields, arr) in list(meta.items()):
                    # find which tree owns this iid
                    for tw in trees:
                        if tw.exists(iid):
                            vals = list(tw.item(iid, "values"))
                            ii = fields.get("II")
                            crc = _as_u32(ii.get("value")) if ii else None
                            if crc is None:
                                break
                            label = item_name_for_crc(
                                crc, category_hint=chest_cats) or "(unknown)"
                            rec = item_record_for_crc(
                                crc, category_hint=chest_cats)
                            cat = (rec or {}).get("category") or ""
                            if len(vals) >= 3:
                                vals[1] = label
                                vals[2] = cat
                                tw.item(iid, values=vals)
                            break

            def reload_from_disk():
                """Re-read entity from container and refresh all tree rows."""
                try:
                    fresh_doc, fresh_kind = unwrap(
                        self.container.chunk(e), self.dctx)
                    fresh_nodes, _ = bson_parse(bytearray(fresh_doc))
                except Exception as ex:
                    self.log("reload_from_disk parse failed: %s" % ex)
                    return
                # rebuild meta + tree contents
                for tw in trees:
                    for iid in tw.get_children():
                        tw.delete(iid)
                meta.clear()
                for arr_n in _walk(fresh_nodes):
                    if arr_n["key"] not in ("IBP", "IAB", "IEQ", "VEQ", "PET"):
                        continue
                    if not arr_n.get("children"):
                        continue
                    slots = inventory_slot_map(arr_n)
                    # find matching tree by tab text prefix
                    target_tree = None
                    for tw in trees:
                        # match by stored arr key on first meta... use notebook
                        pass
                    # simpler: match tree order to invs_sorted keys stored on trees
                # attach arr key to each tree via trees_by_key
                for key, tw in trees_by_key.items():
                    arr_node = None
                    for arr_n in _walk(fresh_nodes):
                        if arr_n["key"] == key and arr_n.get("children") is not None:
                            arr_node = arr_n
                            break
                    if arr_node is None:
                        continue
                    slots = inventory_slot_map(arr_node)
                    for si in sorted(slots):
                        entry = slots[si]
                        fields = item_entry_fields(entry)
                        ii = fields.get("II")
                        sc = fields.get("SC")
                        crc = _as_u32(ii.get("value")) if ii else None
                        stack = sc["value"] if sc is not None else 1
                        cap = item_max_stack(crc) if crc is not None else ""
                        label = (item_name_for_crc(
                            crc, category_hint=chest_cats) if crc is not None
                                 else None) or "(unknown)"
                        hash_s = ("0x%08X (%d)" % (crc, crc)) if crc is not None else ""
                        rec = item_record_for_crc(
                            crc, category_hint=chest_cats) if crc is not None else None
                        cat = (rec or {}).get("category") or ""
                        price = (rec or {}).get("price")
                        price_s = "" if price in (None, 0) else str(price)
                        stats = parse_item_description(
                            (rec or {}).get("description"))
                        if stats.get("damage") is not None:
                            stat_s = "Dmg %d" % stats["damage"]
                        elif stats.get("defence") is not None:
                            stat_s = "Def %d" % stats["defence"]
                        else:
                            stat_s = ""
                        iid = tw.insert("", "end", values=(
                            si + 1, label, cat, price_s, stat_s, hash_s,
                            stack, cap))
                        meta[iid] = (key, si, entry, fields, arr_node)

            def make_change(tree_w):
                def change_item():
                    sel = tree_w.selection()
                    if not sel or sel[0] not in meta:
                        return
                    _ak, si, entry, fields, _arr = meta[sel[0]]
                    ii = fields.get("II")
                    if ii is None:
                        messagebox.showinfo("No II", "No item hash on this slot.",
                                            parent=dlg)
                        return

                    # Current slot hash — used to bind null-hash names
                    cur_crc = _as_u32(ii.get("value"))

                    def on_pick(rec):
                        crc = _as_u32((rec or {}).get("hash"))
                        if crc is None:
                            messagebox.showerror(
                                "No hash",
                                "That table row has no item hash.",
                                parent=dlg)
                            return
                        # Re-parse fresh doc from container for safe edit
                        try:
                            fresh_doc, fresh_kind = unwrap(
                                self.container.chunk(e), self.dctx)
                            fresh_nodes, _ = bson_parse(bytearray(fresh_doc))
                        except Exception as ex:
                            messagebox.showerror("Reload failed", str(ex),
                                                 parent=dlg)
                            return
                        target_ii = None
                        for arr_n in _walk(fresh_nodes):
                            if arr_n["key"] != _ak or not arr_n.get("children"):
                                continue
                            sm = inventory_slot_map(arr_n)
                            if si not in sm:
                                continue
                            f2 = item_entry_fields(sm[si])
                            if "II" in f2:
                                target_ii = f2["II"]
                                break
                        if target_ii is None:
                            messagebox.showerror(
                                "Not found",
                                "Could not re-locate the slot after reload.",
                                parent=dlg)
                            return
                        # Same hash already in slot — nothing to write
                        if cur_crc is not None and crc == cur_crc:
                            self.log("Slot already has 0x%08X — no save write."
                                     % crc)
                            return
                        if self.commit_bson_edit(e, fresh_doc, fresh_kind,
                                                 target_ii, crc):
                            # Update this row in-place (avoid re-decompress;
                            # container can be mid-scan / dctx busy).
                            apply_slot_display(_ak, si, crc, fields)

                    # item picker
                    pick = tk.Toplevel(dlg)
                    pick.title("Pick item for slot %d" % (si + 1))
                    pick.geometry("640x420")
                    qvar = tk.StringVar()
                    ttk.Entry(pick, textvariable=qvar).pack(fill="x", padx=8,
                                                            pady=6)
                    ttk.Label(
                        pick,
                        text="Pick an item with a known hash. "
                             "to that name in the JSON (label fix).",
                        foreground="#555",
                    ).pack(anchor="w", padx=8)
                    lb = tk.Listbox(pick, font=("Courier New", 9))
                    lb.pack(fill="both", expand=True, padx=8, pady=4)
                    found_rows = []

                    def refresh(*_a):
                        lb.delete(0, "end")
                        del found_rows[:]
                        # Search without placeable filter so null-hash names show
                        q = (qvar.get() or "").strip().lower()
                        for rec in item_table():
                            if rec.get("category") is None:
                                continue
                            if rec.get("confidence") == "unmatched":
                                continue
                            name = (rec.get("name") or "")
                            cat = (rec.get("category") or "")
                            if q and q not in _s(name).lower() and q not in _s(cat).lower():
                                hh = (rec.get("hash_hex") or "")
                                if q not in _s(hh).lower() and q not in str(rec.get("hash") or ""):
                                    continue
                            found_rows.append(rec)
                            h = _as_u32(rec.get("hash"))
                            if h is None:
                                lb.insert("end", "%-36s  %-12s  [no hash]" % (
                                    name[:36], cat[:12]))
                            else:
                                lb.insert("end", "%-36s  %-12s  0x%08X" % (
                                    name[:36], cat[:12], h))
                            if len(found_rows) >= 250:
                                break

                    def do_pick(_evt=None):
                        sel2 = lb.curselection()
                        if not sel2:
                            return
                        rec = found_rows[sel2[0]]
                        pick.destroy()
                        on_pick(rec)

                    qvar.trace_add("write", refresh)
                    lb.bind("<Double-1>", do_pick)
                    ttk.Button(pick, text="Use selected",
                               command=do_pick).pack(pady=6)
                    refresh()

                def edit_stack():
                    sel = tree_w.selection()
                    if not sel or sel[0] not in meta:
                        return
                    _ak, si, entry, fields, _arr = meta[sel[0]]
                    sc = fields.get("SC")
                    if sc is None:
                        messagebox.showinfo(
                            "No stack",
                            "This slot has no stack count field.",
                            parent=dlg)
                        return
                    ii = fields.get("II")
                    crc = ii["value"] if ii else 0
                    cap = item_max_stack(crc)
                    sd = tk.Toplevel(dlg)
                    sd.title("Stack slot %d (cap %d)" % (si + 1, cap))
                    var = tk.StringVar(value=str(sc["value"]))
                    ttk.Entry(sd, textvariable=var, width=12).pack(padx=10,
                                                                   pady=10)

                    def apply():
                        try:
                            v = int(var.get().strip(), 0)
                        except ValueError:
                            messagebox.showerror("Bad value",
                                                 "Enter an integer.",
                                                 parent=sd)
                            return
                        try:
                            fresh_doc, fresh_kind = unwrap(
                                self.container.chunk(e), self.dctx)
                            fresh_nodes, _ = bson_parse(bytearray(fresh_doc))
                        except Exception as ex:
                            messagebox.showerror("Reload failed", str(ex),
                                                 parent=sd)
                            return
                        target_sc = None
                        for arr_n in _walk(fresh_nodes):
                            if arr_n["key"] != _ak or not arr_n.get("children"):
                                continue
                            sm = inventory_slot_map(arr_n)
                            if si not in sm:
                                continue
                            f2 = item_entry_fields(sm[si])
                            if "SC" in f2:
                                target_sc = f2["SC"]
                                break
                        if target_sc is None:
                            messagebox.showerror("Not found",
                                                 "Slot lost after reload.",
                                                 parent=sd)
                            return
                        if self.commit_bson_edit(e, fresh_doc, fresh_kind,
                                                 target_sc, v):
                            sd.destroy()
                            dlg.destroy()
                            if on_done:
                                on_done()

                    ttk.Button(sd, text="Apply", command=apply).pack(pady=6)

                return change_item, edit_stack

            change_item, edit_stack = make_change(tree)

            def copy_hash(tree_w=tree, quiet=False):
                sel = tree_w.selection()
                if not sel or sel[0] not in meta:
                    return
                _ak, _si, _entry, fields, _arr = meta[sel[0]]
                ii = fields.get("II")
                if not ii:
                    if not quiet:
                        messagebox.showinfo("No hash", "No II on this slot.",
                                            parent=dlg)
                    return
                crc = int(ii["value"]) & 0xFFFFFFFF
                hex_s = "0x%08X" % crc
                dec_s = str(crc)
                # Both forms: hex + decimal (tab-separated for paste into sheets)
                text = "%s\t%s" % (hex_s, dec_s)
                try:
                    dlg.clipboard_clear()
                    dlg.clipboard_append(text)
                    dlg.update_idletasks()
                    if not quiet:
                        messagebox.showinfo(
                            "Hash copied",
                            "Copied:\n  %s\n  %s (decimal)" % (hex_s, dec_s),
                            parent=dlg)
                except Exception as ex:
                    if not quiet:
                        messagebox.showerror("Clipboard", str(ex), parent=dlg)

            def on_ctrl_c(evt, tree_w=tree):
                copy_hash(tree_w, quiet=True)
                return "break"

            tree.bind("<Control-c>", on_ctrl_c)
            tree.bind("<Control-C>", on_ctrl_c)
            # Double-click row also copies hash (quiet)
            tree.bind("<Double-Button-1>",
                      lambda e, tw=tree: copy_hash(tw, quiet=True))

            bf = ttk.Frame(frame)
            bf.pack(fill="x", padx=4, pady=4)
            ttk.Button(bf, text="Change item…",
                       command=change_item).pack(side="left")
            ttk.Button(bf, text="Edit stack…",
                       command=edit_stack).pack(side="left", padx=6)
            ttk.Button(bf, text="Copy hash",
                       command=copy_hash).pack(side="left", padx=6)

        # Jump to the array that actually has items (e.g. IEQ on mannequins)
        try:
            if best_count > 0:
                nb.select(best_tab)
        except Exception:
            pass

        ttk.Button(dlg, text="Close", command=dlg.destroy).pack(pady=6)

    def open_world_signs(self):
        """List and edit User Editable String Component texts (signs)."""
        path, info = self._resolve_world_path()
        if not path:
            messagebox.showinfo(
                "No world",
                "Select a world file first (filter → Worlds, or the Worlds tab).")
            return
        if not self._ensure_dict():
            return
        try:
            scanned = list(self._scan_world_bkck(path))
        except Exception as exc:
            messagebox.showerror("Save file error", str(exc))
            return

        rows = []
        for e, doc, kind, nodes, container in scanned:
            if b"User Editable String Component" not in doc:
                continue
            for sign in extract_world_signs(nodes):
                rows.append((e, doc, kind, sign, container))

        dlg = tk.Toplevel(self)
        dlg.title("Signs — %s" % (info.get("location_name") or
                                  os.path.basename(path)))
        dlg.geometry("780x480")
        ttk.Label(
            dlg,
            text="%d sign(s) with User Editable String Component. "
                 "Double-click to edit text (length-preserving)."
                 % len(rows),
        ).pack(anchor="w", padx=8, pady=6)

        cols = ("idx", "x", "y", "z", "text", "chunk")
        tree = ttk.Treeview(dlg, columns=cols, show="headings", height=16,
                            selectmode="browse")
        for col, text, w in (("idx", "#", 40),
                             ("x", "X", 70), ("y", "Y", 70), ("z", "Z", 70),
                             ("text", "Sign text", 360),
                             ("chunk", "Chunk id", 90)):
            tree.heading(col, text=text)
            tree.column(col, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=8, pady=4)

        row_data = {}
        for i, (e, doc, kind, sign, container) in enumerate(rows):
            pos = sign.get("pos") or (None, None, None)
            tmpl = sign.get("template")
            tname = template_label(tmpl) if tmpl is not None else "Sign"
            raw = (sign.get("text") or "").strip()
            if raw:
                text = raw[:80]
            elif sign.get("was_edited") is False:
                text = "(default game text)"
            else:
                text = "(no text in save)"
            iid = tree.insert("", "end", values=(
                i + 1,
                tname,
                ("%.1f" % pos[0]) if pos[0] is not None else "?",
                ("%.1f" % pos[1]) if pos[1] is not None else "?",
                ("%.1f" % pos[2]) if pos[2] is not None else "?",
                text,
                "%08X" % (int(e.get("id", 0)) & 0xFFFFFFFF),
            ))
            row_data[iid] = (e, doc, kind, sign, container)

        def edit_sign(_evt=None):
            sel = tree.selection()
            if not sel or sel[0] not in row_data:
                return
            e, doc, kind, sign, container = row_data[sel[0]]
            node = sign.get("text_node")
            if node is None:
                messagebox.showerror("No text node",
                                     "Could not locate the string field.",
                                     parent=dlg)
                return
            self.savefile_path.set(path)
            self.container = container

            sd = tk.Toplevel(dlg)
            sd.title("Edit sign text")
            cur = sign.get("text") or ""
            # Max length: for string type use current encoded size;
            # for binary name-style, use field length if known.
            max_len = 128
            if node.get("type") == 0x02 and isinstance(node.get("value"), str):
                # BSON string can grow via commit_bson_edit
                max_len = max(len(cur.encode("utf-8")) + 64, 128)
            elif node.get("type") == 0x05:
                max_len = len(node.get("value") or b"") or 64

            ttk.Label(sd, text="Sign text (keep under ~%d bytes):"
                      % max_len).pack(anchor="w", padx=10, pady=(10, 2))
            var = tk.StringVar(value=cur)
            ent = ttk.Entry(sd, textvariable=var, width=64)
            ent.pack(padx=10, pady=4)
            ent.focus_set()

            def apply():
                new = var.get()
                # Reload fresh
                try:
                    fresh_doc, fresh_kind = unwrap(
                        self.container.chunk(e), self.dctx)
                    fresh_nodes, _ = bson_parse(bytearray(fresh_doc))
                except Exception as ex:
                    messagebox.showerror("Reload failed", str(ex), parent=sd)
                    return
                # Find matching sign by current text or position
                target = None
                for s in extract_world_signs(fresh_nodes):
                    if s.get("text") == cur or (
                            s.get("pos") == sign.get("pos") and
                            s.get("text_node") is not None):
                        target = s["text_node"]
                        if s.get("text") == cur:
                            break
                if target is None:
                    messagebox.showerror(
                        "Not found",
                        "Could not re-locate the sign after reload.",
                        parent=sd)
                    return
                # For binary fixed fields, pad/truncate
                if target.get("type") == 0x05:
                    old = target.get("value") or b""
                    enc = new.encode("utf-8")
                    if len(enc) > len(old):
                        messagebox.showerror(
                            "Too long",
                            "This sign field holds %d bytes; your text needs "
                            "%d. Shorten it." % (len(old), len(enc)),
                            parent=sd)
                        return
                    new_val = enc + b"\x00" * (len(old) - len(enc))
                    # commit as bytes
                    if self.commit_bson_edit(e, fresh_doc, fresh_kind,
                                             target, new_val):
                        sd.destroy()
                        dlg.destroy()
                        self.open_world_signs()
                else:
                    if self.commit_bson_edit(e, fresh_doc, fresh_kind,
                                             target, new):
                        sd.destroy()
                        dlg.destroy()
                        self.open_world_signs()

            ttk.Button(sd, text="Save", command=apply).pack(pady=8)

        tree.bind("<Double-1>", edit_sign)
        bf = ttk.Frame(dlg)
        bf.pack(fill="x", padx=8, pady=6)
        ttk.Button(bf, text="Edit selected…",
                   command=edit_sign).pack(side="left")
        ttk.Button(bf, text="Close", command=dlg.destroy).pack(side="right")


    def open_world_npcs(self):
        """List NPCs / spawns; allow replacing an NPC TemplateCRC (test)."""
        path, info = self._resolve_world_path()
        if not path:
            messagebox.showinfo(
                "No world",
                "Select a world file first (filter → Worlds, or the Worlds tab).")
            return
        if not self._ensure_dict():
            return
        try:
            scanned = list(self._scan_world_bkck(path))
        except Exception as exc:
            messagebox.showerror("Save file error", str(exc))
            return

        # (e, npc_dict, kind_tag, container, doc, kind)
        npc_rows = []
        seen_pos = set()
        for e, doc, kind, nodes, container in scanned:
            if b"NPC Control Component" in doc:
                for npc in extract_world_npcs(nodes):
                    pos = npc.get("pos")
                    key = (round(pos[0], 1), round(pos[2], 1)) if pos else None
                    if key:
                        seen_pos.add(key)
                    npc_rows.append((e, npc, "npc", container, doc, kind))
            for o in extract_world_all_templates(nodes):
                tmpl = o.get("template")
                if tmpl is None:
                    continue
                tcrc = int(tmpl) & 0xFFFFFFFF
                if tcrc not in ENEMY_TEMPLATE_CRCS and tcrc not in WORLD_TEMPLATES:
                    continue
                if tcrc in NPC_TEMPLATES:
                    continue
                pos = o.get("pos")
                key = (round(pos[0], 1), round(pos[2], 1)) if pos else None
                if key and key in seen_pos:
                    continue
                if key:
                    seen_pos.add(key)
                tag = "enemy" if tcrc in ENEMY_TEMPLATE_CRCS else "world"
                npc_rows.append((e, o, tag, container, doc, kind))

        dlg = tk.Toplevel(self)
        dlg.title("NPCs / spawns — %s" % (info.get("location_name") or
                                          os.path.basename(path)))
        dlg.geometry("900x520")
        ttk.Label(
            dlg,
            text="%d spawn(s) on this island. NPCs only for copy/tag. "
                 "TemplateCRC (test). Use a known NPC template."
                 % len(npc_rows),
        ).pack(anchor="w", padx=8, pady=6)

        island_name = (info.get("location_name") or
                       info.get("label") or os.path.basename(path))
        cols = ("idx", "name", "kind", "islands", "x", "y", "z", "text", "template", "chunk")
        tree = ttk.Treeview(dlg, columns=cols, show="headings", height=16,
                            selectmode="extended")
        for col, text, w in (("idx", "#", 36),
                             ("name", "Name", 160),
                             ("kind", "Kind", 50),
                             ("islands", "Seen on", 110),
                             ("x", "X", 52), ("y", "Y", 48), ("z", "Z", 52),
                             ("text", "Custom text", 160),
                             ("template", "TemplateCRC", 100),
                             ("chunk", "Chunk id", 72)):
            tree.heading(col, text=text)
            tree.column(col, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=8, pady=4)
        treeview_enable_sort(
            tree, numeric_cols={"idx", "x", "y", "z"})

        def _fmt_f(v):
            try:
                return "%.1f" % float(v)
            except (TypeError, ValueError):
                return "?"

        def _fmt_u32(v):
            try:
                return int(v) & 0xFFFFFFFF
            except (TypeError, ValueError):
                return None

        row_data = {}  # iid -> full row payload
        row_tmpl = {}
        for i, (e, npc, kind_tag, container, doc, kind) in enumerate(npc_rows):
            pos = npc.get("pos") if isinstance(npc, dict) else None
            if not (isinstance(pos, (tuple, list)) and len(pos) >= 3):
                pos = (None, None, None)
            tmpl = npc.get("template") if isinstance(npc, dict) else None
            tmpl_u = _fmt_u32(tmpl)
            tmpl_s = ("0x%08X" % tmpl_u) if tmpl_u is not None else "?"
            nname = str(template_label(tmpl_u if tmpl_u is not None else tmpl))
            islands = template_islands(tmpl_u)
            islands = [str(x) for x in islands if x is not None]
            seen_disp = ", ".join(islands) if islands else ""
            iname = str(island_name) if island_name else ""
            if iname and iname not in islands and iname not in seen_disp:
                seen_disp = iname if not seen_disp else (seen_disp + ", " + iname)
            eid_u = _fmt_u32(e.get("id") if isinstance(e, dict) else None)
            eid_s = ("%08X" % eid_u) if eid_u is not None else "?"
            raw_txt = ""
            if isinstance(npc, dict):
                raw_txt = (npc.get("text") or "").strip()
                if not raw_txt and npc.get("was_edited") is False:
                    raw_txt = ""  # default dialogue not in save
            text_disp = (raw_txt[:60] + ("…" if len(raw_txt) > 60 else "")) if raw_txt else ""
            iid = tree.insert("", "end", values=(
                i + 1,
                nname,
                str(kind_tag),
                seen_disp,
                _fmt_f(pos[0]) if pos[0] is not None else "?",
                _fmt_f(pos[1]) if pos[1] is not None else "?",
                _fmt_f(pos[2]) if pos[2] is not None else "?",
                text_disp,
                tmpl_s,
                eid_s,
            ))
            row_tmpl[iid] = "%s\t%s\t%s\t%s" % (
                tmpl_s, nname, str(kind_tag), iname)
            row_data[iid] = {
                "e": e, "npc": npc, "kind_tag": kind_tag,
                "container": container, "doc": doc, "kind": kind,
            }

        def copy_selected():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("Nothing selected",
                                    "Select one or more NPC rows.",
                                    parent=dlg)
                return
            try:
                lines = [row_tmpl[i] for i in sel if i in row_tmpl]
                dlg.clipboard_clear()
                dlg.clipboard_append("\n".join(lines))
                dlg.update_idletasks()
                messagebox.showinfo(
                    "Copied",
                    "Copied %s template(s)." % len(lines), parent=dlg)
            except Exception as ex:
                import traceback
                self.log("copy_selected failed: %s\n%s" % (
                    ex, traceback.format_exc()))
                messagebox.showerror("Clipboard", str(ex), parent=dlg)

        def copy_all():
            # NPC Control only — props/paintings tagged "world" are excluded
            try:
                seen = []
                seen_set = set()
                total_npc = 0
                for iid in tree.get_children(""):
                    data = row_data.get(iid)
                    if not data or data.get("kind_tag") != "npc":
                        continue
                    total_npc += 1
                    line = row_tmpl.get(iid) or ""
                    parts = line.split("\t")
                    h = parts[0] if parts else ""
                    if not h or h in seen_set:
                        continue
                    seen_set.add(h)
                    name = parts[1] if len(parts) > 1 else ""
                    seen.append("%s\t%s" % (h, name))
                iname = str(island_name) if island_name else "?"
                lines = [
                    "# NPC templates only — %s" % iname,
                    "# island=%s" % iname,
                    "# unique=%s  total_npc_instances=%s" % (
                        len(seen), total_npc),
                    "# (world props / enemies excluded)",
                ]
                lines.extend(seen)
                dlg.clipboard_clear()
                dlg.clipboard_append("\n".join(lines))
                dlg.update_idletasks()
                messagebox.showinfo(
                    "Copied all",
                    "Copied %s unique NPC template(s)." % len(seen),
                    parent=dlg)
            except Exception as ex:
                import traceback
                self.log("copy_all failed: %s\n%s" % (
                    ex, traceback.format_exc()))
                messagebox.showerror("Copy failed", str(ex), parent=dlg)

        def replace_template():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("Select", "Select one NPC / spawn row.",
                                    parent=dlg)
                return
            iid = sel[0]
            data = row_data.get(iid)
            if not data:
                return
            e = data["e"]
            container = data["container"]
            kind_tag = data["kind_tag"]
            old_tmpl = data["npc"].get("template")
            old_s = ("0x%08X" % (int(old_tmpl) & 0xFFFFFFFF)
                     if old_tmpl is not None else "?")

            # Picker of known NPC templates (+ allow raw hex)
            pd = tk.Toplevel(dlg)
            pd.title("Replace TemplateCRC")
            pd.geometry("520x420")
            ttk.Label(
                pd,
                text="Current: %s  (%s)\n"
                     "Pick a known NPC template, or type hex/decimal."
                     % (template_label(old_tmpl), old_s),
            ).pack(anchor="w", padx=8, pady=6)
            qvar = tk.StringVar()
            ttk.Entry(pd, textvariable=qvar).pack(fill="x", padx=8, pady=4)
            lb = tk.Listbox(pd, font=("Courier New", 9))
            lb.pack(fill="both", expand=True, padx=8, pady=4)
            choices = []  # list of (crc, label)

            def refresh(*_a):
                lb.delete(0, "end")
                del choices[:]
                q = (qvar.get() or "").strip().lower()
                items = sorted(NPC_TEMPLATES.items(),
                               key=lambda kv: (kv[1] or "").lower())
                for crc, name in items:
                    crc = int(crc) & 0xFFFFFFFF
                    label = "%-40s  0x%08X" % ((name or "?")[:40], crc)
                    if q and q not in label.lower() and q not in (
                            "%d" % crc):
                        continue
                    choices.append((crc, name or "?"))
                    mark = "  ← current" if (
                        old_tmpl is not None and
                        (int(old_tmpl) & 0xFFFFFFFF) == crc) else ""
                    lb.insert("end", label + mark)
                if not choices and q:
                    # allow typed hex as custom
                    try:
                        if q.startswith("0x"):
                            c = int(q, 16) & 0xFFFFFFFF
                        else:
                            c = int(q) & 0xFFFFFFFF
                        choices.append((c, "custom 0x%08X" % c))
                        lb.insert("end", "custom 0x%08X (%d)" % (c, c))
                    except ValueError:
                        pass

            def do_replace(_evt=None):
                sel2 = lb.curselection()
                if not sel2 and not (qvar.get() or "").strip():
                    return
                if sel2:
                    new_crc, new_name = choices[sel2[0]]
                else:
                    raw = (qvar.get() or "").strip()
                    try:
                        new_crc = int(raw, 0) & 0xFFFFFFFF
                    except ValueError:
                        messagebox.showerror(
                            "Bad CRC", "Enter hex (0x…) or decimal.",
                            parent=pd)
                        return
                    new_name = template_label(new_crc)
                if old_tmpl is not None and (
                        int(old_tmpl) & 0xFFFFFFFF) == new_crc:
                    messagebox.showinfo("Same", "Already that template.",
                                        parent=pd)
                    return
                if not messagebox.askyesno(
                        "Confirm replace",
                        "Replace TemplateCRC on this %s?\n\n"
                        "  %s\n  →  %s  (0x%08X)\n\n"
                        "A .bak of the world file is made first.\n\n"
                        "IMPORTANT: fully quit Portal Knights (not only\n"
                        "main menu) before loading this world, or the game\n"
                        "shows a save error until restart. Main menu still\n"
                        "keeps world data in memory."
                        % (kind_tag,
                           template_label(old_tmpl),
                           new_name, new_crc),
                        parent=pd):
                    return
                # Activate container and patch TemplateCRC in this entity
                self.savefile_path.set(path)
                self.container = container
                self._update_file_info_label()
                try:
                    fresh_doc, fresh_kind = unwrap(
                        container.chunk(e), self.dctx)
                    fresh_nodes, _ = bson_parse(bytearray(fresh_doc))
                except Exception as ex:
                    messagebox.showerror("Reload failed", str(ex), parent=pd)
                    return
                # Locate TemplateCRC on the entity at the same position
                target = None
                target_pos = data["npc"].get("pos")
                for ent in iter_entities(fresh_nodes):
                    pos = _entity_position(ent)
                    if target_pos and pos:
                        if (abs(pos[0] - target_pos[0]) > 0.05 or
                                abs(pos[2] - target_pos[2]) > 0.05):
                            continue
                    elif target_pos:
                        continue
                    # Prefer entities that still look like NPCs when replacing NPCs
                    if kind_tag == "npc":
                        has_npc = any(
                            n["key"] == "NPC Control Component"
                            for n in _walk([ent]))
                        if not has_npc:
                            continue
                    for n in _walk([ent]):
                        if n["key"] == "TemplateCRC" and n.get("value") is not None:
                            target = n
                            break
                    if target is not None:
                        break
                if target is None:
                    messagebox.showerror(
                        "Not found",
                        "Could not re-locate TemplateCRC for this spawn.",
                        parent=pd)
                    return
                ok = self.commit_bson_edit(
                    e, fresh_doc, fresh_kind, target, new_crc)
                if ok:
                    self.log(
                        "NPC TemplateCRC %s → 0x%08X (%s) at (%.1f, %.1f, %.1f)"
                        % (old_s, new_crc, new_name,
                           *(target_pos or (0, 0, 0))))
                    messagebox.showinfo(
                        "Replaced",
                        "TemplateCRC written and verified.\n"
                        "0x%08X → 0x%08X (%s)\n\n"
                        "Load the world in-game to test."
                        % ((int(old_tmpl) & 0xFFFFFFFF) if old_tmpl else 0,
                           new_crc, new_name),
                        parent=pd)
                    pd.destroy()
                    dlg.destroy()
                    # Re-open list so names refresh
                    self.open_world_npcs()

            qvar.trace_add("write", refresh)
            lb.bind("<Double-1>", do_replace)
            bf2 = ttk.Frame(pd)
            bf2.pack(fill="x", padx=8, pady=6)
            ttk.Button(bf2, text="Replace", command=do_replace).pack(
                side="left")
            ttk.Button(bf2, text="Cancel", command=pd.destroy).pack(
                side="right")
            refresh()

        def parse_crc_list(text):
            """Parse hex/decimal CRCs from a pasted unique-template list."""
            skip = landing_pad_template_crcs()
            out = []
            seen = set()
            for line in (text or "").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # first token only
                tok = line.split()[0].split("\t")[0].strip().rstrip(",")
                try:
                    if tok.lower().startswith("0x"):
                        c = int(tok, 16) & 0xFFFFFFFF
                    else:
                        c = int(tok, 0) & 0xFFFFFFFF
                except ValueError:
                    continue
                if c in skip:
                    continue
                if c in seen:
                    continue
                seen.add(c)
                out.append(c)
            return out

        def bulk_assign():
            """Assign a pasted list of TemplateCRCs onto NPC slots in this world.

            Workflow: copy unique NPC templates from Sanctuary → open an
            empty/simple world → Bulk assign → walk the line in-game.
            Fully quit the game before loading the edited world.
            """
            npc_only = [
                (iid, row_data[iid]) for iid in tree.get_children("")
                if row_data.get(iid, {}).get("kind_tag") == "npc"
            ]
            if not npc_only:
                messagebox.showerror(
                    "No NPCs",
                    "This world has no NPC Control entities to replace.\n"
                    "Use a world that already has NPC slots (or copy one).",
                    parent=dlg)
                return

            bd = tk.Toplevel(dlg)
            bd.title("Bulk assign NPC templates")
            bd.geometry("560x420")
            ttk.Label(
                bd,
                text="Paste unique TemplateCRC list (one per line, hex ok).\n"
                     "Landing Pad CRCs are skipped. Assigns onto the %d NPC "
                     "slot(s) in this world (sorted by X then Z)."
                     % len(npc_only),
            ).pack(anchor="w", padx=8, pady=6)
            txt = tk.Text(bd, height=14, font=("Courier New", 9))
            txt.pack(fill="both", expand=True, padx=8, pady=4)
            # Prefill from clipboard if it looks like CRC list
            try:
                clip = bd.clipboard_get()
                if "0x" in clip or "unique=" in clip:
                    txt.insert("1.0", clip)
            except Exception:
                pass
            layout_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                bd,
                text="Also lay out in a compact grid (optional — off = keep positions)",
                variable=layout_var,
            ).pack(anchor="w", padx=8)

            def run():
                try:
                    crcs = parse_crc_list(txt.get("1.0", "end"))
                    if not crcs:
                        messagebox.showerror(
                            "No CRCs",
                            "No TemplateCRC values found in the paste.",
                            parent=bd)
                        return
                    # Sort NPC slots by position for a stable walk order
                    def pos_key(item):
                        pos = item[1]["npc"].get("pos") or (0, 0, 0)
                        try:
                            return (float(pos[0]), float(pos[2]), float(pos[1]))
                        except Exception:
                            return (0.0, 0.0, 0.0)
                    slots = sorted(npc_only, key=pos_key)
                    n = min(len(slots), len(crcs))
                    if len(crcs) > len(slots):
                        extra = len(crcs) - len(slots)
                        if not messagebox.askyesno(
                                "Not enough NPC slots",
                                "List has %s templates, world has %s NPC slots.\n"
                                "Only the first %s will be assigned (%s left over).\n\n"
                                "Continue?" % (len(crcs), len(slots), n, extra),
                                parent=bd):
                            return
                    elif len(slots) > len(crcs):
                        if not messagebox.askyesno(
                                "Extra NPC slots",
                                "World has %s NPCs, list has %s templates.\n"
                                "First %s slots will be replaced; the rest stay."
                                % (len(slots), len(crcs), n),
                                parent=bd):
                            return
                    if not messagebox.askyesno(
                            "Confirm bulk assign",
                            "Assign %s TemplateCRC(s) onto NPC slots in\n  %s\n\n"
                            "A .bak is made on the first write.\n"
                            "Fully quit the game before loading this world."
                            % (n, os.path.basename(path)),
                            parent=bd):
                        return

                    # Grid origin: prefer landing pad, else median NPC pos.
                    # Never start from a lone outlier at the map edge.
                    def _median(vals):
                        s = sorted(vals)
                        return s[len(s) // 2]
                    pad_pos = None
                    try:
                        cont0 = load_container(path)
                        for ee in cont0.entries:
                            if ee.get("tag") != b"BKCK":
                                continue
                            try:
                                d0, _k0 = unwrap(cont0.chunk(ee), self.dctx)
                                n0, _ = bson_parse(bytearray(d0))
                            except Exception:
                                continue
                            pads = extract_world_landing_pads(n0)
                            if pads:
                                pad_pos = pads[0].get("pos")
                                break
                    except Exception:
                        pad_pos = None
                    xs, ys, zs = [], [], []
                    for _iid, _data in slots:
                        pp = (_data.get("npc") or {}).get("pos")
                        if not pp:
                            continue
                        try:
                            xs.append(float(pp[0])); ys.append(float(pp[1])); zs.append(float(pp[2]))
                        except Exception:
                            pass
                    if pad_pos:
                        try:
                            ox, oy, oz = float(pad_pos[0]), float(pad_pos[1]), float(pad_pos[2])
                        except Exception:
                            ox = _median(xs) if xs else 64.0
                            oy = _median(ys) if ys else 60.0
                            oz = _median(zs) if zs else 64.0
                    elif xs:
                        ox, oy, oz = _median(xs), _median(ys), _median(zs)
                    else:
                        ox, oy, oz = 64.0, 60.0, 64.0
                    # Compact grid near origin (not map edge)
                    cols = min(8, max(4, int(n ** 0.5) + 1))
                    spacing = 2.0
                    self.log(
                        "bulk grid origin=(%.1f, %.1f, %.1f) cols=%s spacing=%s"
                        % (ox, oy, oz, cols, spacing))

                    ok_n = 0
                    fail = []
                    # Always re-load container from disk so multi-NPC BKCK
                    # chunks see previous writes in this batch.
                    for i in range(n):
                        iid, data = slots[i]
                        e = data["e"]
                        new_crc = int(crcs[i]) & 0xFFFFFFFF
                        target_pos = data["npc"].get("pos")
                        try:
                            container = load_container(path)
                            self.savefile_path.set(path)
                            self.container = container
                            # re-find entry by id (offsets change after rewrite)
                            eid = e.get("id") if isinstance(e, dict) else None
                            entry = None
                            for ent in container.entries:
                                if ent.get("id") == eid:
                                    entry = ent
                                    break
                            if entry is None:
                                fail.append((i, "entry id %s not found after reload" % eid))
                                continue
                            fresh_doc, fresh_kind = unwrap(
                                container.chunk(entry), self.dctx)
                            fresh_nodes, _ = bson_parse(bytearray(fresh_doc))
                        except Exception as ex:
                            fail.append((i, "reload/parse: %s" % ex))
                            continue
                        target_crc_node = None
                        target_pos_nodes = None
                        for ent in iter_entities(fresh_nodes):
                            has_npc = any(
                                n["key"] == "NPC Control Component"
                                for n in _walk([ent]))
                            if not has_npc:
                                continue
                            pos = _entity_position(ent)
                            if target_pos and pos:
                                try:
                                    if (abs(float(pos[0]) - float(target_pos[0])) > 0.05 or
                                            abs(float(pos[2]) - float(target_pos[2])) > 0.05):
                                        continue
                                except Exception:
                                    continue
                            elif target_pos:
                                continue
                            for n in _walk([ent]):
                                if (n["key"] == "TemplateCRC"
                                        and n.get("value") is not None):
                                    target_crc_node = n
                            if layout_var.get():
                                for n in _walk([ent]):
                                    if n["key"] == "Position" and n.get("children"):
                                        xyz = {}
                                        for ch in n["children"]:
                                            if ch["key"] in ("x", "y", "z"):
                                                xyz[ch["key"]] = ch
                                        if len(xyz) == 3:
                                            target_pos_nodes = xyz
                                        break
                            break
                        if target_crc_node is None:
                            fail.append((i, "TemplateCRC not found"))
                            continue
                        edits = [(target_crc_node, new_crc)]
                        if layout_var.get() and target_pos_nodes:
                            col = i % cols
                            row = i // cols
                            nx = ox + col * spacing
                            nz = oz + row * spacing
                            # Keep everyone on the same surface height as origin
                            # so repeats cannot drift under the map.
                            ny = oy
                            try:
                                edits.append((target_pos_nodes["x"], float(nx)))
                                edits.append((target_pos_nodes["y"], float(ny)))
                                edits.append((target_pos_nodes["z"], float(nz)))
                            except Exception as ex:
                                fail.append((i, "pos nodes: %s" % ex))
                                continue
                            self.log(
                                "  layout slot %s -> (%.1f, %.1f, %.1f)"
                                % (i + 1, nx, ny, nz))
                        try:
                            ok = self.commit_bson_edits(
                                entry, fresh_doc, fresh_kind, edits,
                                verify_label="bulk NPC assign")
                        except Exception as ex:
                            fail.append((i, "commit: %s" % ex))
                            continue
                        if ok:
                            ok_n += 1
                            try:
                                label = str(template_label(new_crc))
                            except Exception:
                                label = "?"
                            self.log(
                                "Bulk NPC [%s] → 0x%08X (%s)"
                                % (i + 1, new_crc, label))
                        else:
                            fail.append((i, "write/verify failed"))

                    msg = "Assigned %s / %s templates." % (ok_n, n)
                    if fail:
                        msg += "\n\nFailures (%s):\n" % len(fail)
                        bits = []
                        for item in fail[:8]:
                            try:
                                idx, err = item
                                bits.append("  #%s: %s" % (idx + 1, err))
                            except Exception:
                                bits.append("  %s" % (item,))
                        msg += "\n".join(bits)
                    messagebox.showinfo("Bulk assign done", msg, parent=bd)
                    bd.destroy()
                    dlg.destroy()
                    self.open_world_npcs()
                except Exception as ex:
                    import traceback
                    self.log("bulk_assign failed: %s\n%s" % (
                        ex, traceback.format_exc()))
                    messagebox.showerror("Bulk assign failed", str(ex), parent=bd)

            bf3 = ttk.Frame(bd)
            bf3.pack(fill="x", padx=8, pady=6)
            ttk.Button(bf3, text="Assign to NPC slots", command=run).pack(
                side="left")
            ttk.Button(bf3, text="Cancel", command=bd.destroy).pack(
                side="right")


        def tag_island():
            """Record current island on selected (or all NPC) templates."""
            sel = tree.selection()
            targets = list(sel) if sel else [
                iid for iid in tree.get_children("")
                if row_data.get(iid, {}).get("kind_tag") == "npc"
            ]
            if not targets:
                messagebox.showinfo("Nothing", "No NPC rows to tag.", parent=dlg)
                return
            n = 0
            for iid in targets:
                data = row_data.get(iid)
                if not data:
                    continue
                tmpl = data["npc"].get("template")
                if tmpl is None:
                    continue
                crc = int(tmpl) & 0xFFFFFFFF
                name = template_label(crc)
                kind = "npc" if data.get("kind_tag") == "npc" else (
                    "enemy" if data.get("kind_tag") == "enemy" else "world")
                meta = _USER_TEMPLATE_META.get(crc) or {}
                # Preserve richer kinds already in the JSON
                if meta.get("kind") in ("trader", "quest"):
                    kind = meta["kind"]
                if meta.get("name") and not meta["name"].startswith("0x"):
                    name = meta["name"]
                try:
                    save_user_template(crc, name, kind=kind, island=island_name)
                    n += 1
                except Exception as ex:
                    self.log("tag island failed 0x%08X: %s" % (crc, ex))
            messagebox.showinfo(
                "Tagged",
                "Recorded island %r on %d template(s) in pk_templates.json."
                % (island_name, n),
                parent=dlg)
            dlg.destroy()
            self.open_world_npcs()

        bf = ttk.Frame(dlg)
        bf.pack(fill="x", padx=8, pady=6)
        ttk.Button(bf, text="Replace template…",
                   command=replace_template).pack(side="left")
        ttk.Button(bf, text="Bulk assign from list…",
                   command=bulk_assign).pack(side="left", padx=4)
        ttk.Button(bf, text="Tag island on NPCs",
                   command=tag_island).pack(side="left", padx=4)
        ttk.Button(bf, text="Copy selected",
                   command=copy_selected).pack(side="left", padx=4)
        ttk.Button(bf, text="Copy NPC templates…",
                   command=copy_all).pack(side="left", padx=4)
        ttk.Button(bf, text="Collect all worlds → here…",
                   command=lambda: self.collect_npcs_into_world(path)).pack(
                       side="left", padx=4)
        ttk.Button(bf, text="Close", command=dlg.destroy).pack(side="right")


    def _lookup_target_island_dims(self, target_path):
        """(width, height, depth) for target_path's island, or all-None.

        Matches the world file to its parent universe by save root +
        universe slot + community flag (siblings from find_saves()), then
        matches the island itself inside that universe by comparing
        location codes: the world file's own location (from its
        filename) against each ILHD entry's location (derived from its
        entry id via island_location_from_entry_id). On a match, reads
        real width/height/depth from that ILHD via
        extract_island_seed_and_size.

        Best-effort: returns (None, None, None) if the world's location
        can't be determined, no sibling universe file is found, or no
        ILHD entry in it matches - callers should fall back to the
        fixed safe band in that case, not treat this as an error.
        """
        rows = getattr(self, "_all_saves", None) or find_saves()
        target_row = next((r for r in rows if r[3] == target_path), None)
        if target_row is None:
            return None, None, None
        info = target_row[4]
        if info.get("type") != "world" or info.get("location") is None:
            return None, None, None
        target_root = target_row[1]
        target_slot = info.get("universe")
        target_community = info.get("community")

        for row in rows:
            uinfo = row[4]
            if (uinfo.get("type") != "universe"
                    or uinfo.get("universe") != target_slot
                    or uinfo.get("community") != target_community
                    or row[1] != target_root):
                continue
            try:
                ucontainer = load_container(row[3])
            except Exception:
                continue
            for e in ucontainer.entries:
                if e.get("tag") != b"ILHD":
                    continue
                loc, _name = island_location_from_entry_id(e["id"])
                if loc != info["location"]:
                    continue
                try:
                    doc, _kind = unwrap(ucontainer.chunk(e), self.dctx)
                except Exception:
                    continue
                dims = extract_island_seed_and_size(doc)
                w, h, d = dims.get("width"), dims.get("height"), dims.get("depth")
                if w and h and d:
                    return w, h, d
            # Right universe file, but no ILHD matched this location -
            # other rows can't be a better match, so stop here.
            break
        return None, None, None

    def collect_npcs_into_world(self, target_path=None):
        """TEST: copy unique NPC entities from every world into target.

        Dedupes by TemplateCRC (one of each type). Places them on a flat
        Y grid around the landing pad / existing NPCs, clamped to a safe
        XZ band so they don't walk off typical islands. Overflow stacks
        upward in +Y layers.
        """
        if not target_path:
            target_path, _info = self._resolve_world_path()
        if not target_path:
            messagebox.showinfo(
                "No world",
                "Select a target world first (Worlds tab / filter).")
            return
        if not self._ensure_dict():
            return

        if not messagebox.askyesno(
                "Collect NPCs",
                "Scan every world on disk, take one copy of each unique "
                "NPC TemplateCRC, and insert them into:\n\n%s\n\n"
                "Positions: flat grid at existing NPC/landing-pad height, "
                "XZ clamped to ~20–110 (safe band), extra layers go UP.\n\n"
                "Backup is made first. Continue?" % target_path):
            return

        # --- gather unique NPC bodies from all worlds ---
        by_crc = {}  # crc -> (entity_doc_bytes, src_name, label)
        world_rows = []
        for row in getattr(self, "_all_saves", None) or find_saves():
            info = row[4]
            if info.get("type") != "world":
                continue
            world_rows.append(row)

        self.log("Collect NPCs: scanning %d world file(s)…" % len(world_rows))
        for row in world_rows:
            wpath = row[3]
            wname = (row[4].get("location_name") or row[2] or wpath)
            try:
                for e, doc, kind, nodes, _container in self._scan_world_bkck(wpath):
                    for npc in extract_world_npcs(nodes):
                        tmpl = npc.get("template")
                        if tmpl is None:
                            continue
                        tcrc = int(tmpl) & 0xFFFFFFFF
                        if tcrc in by_crc:
                            continue
                        if tcrc in landing_pad_template_crcs():
                            continue
                        ent = npc.get("entity")
                        if not ent or ent.get("vstart") is None:
                            continue
                        body = bytes(doc[ent["vstart"]:ent["vend"]])
                        label = template_label(tcrc)
                        by_crc[tcrc] = (body, wname, label)
            except Exception as ex:
                self.log("  skip %s: %s" % (wname, ex))

        if not by_crc:
            messagebox.showinfo("Collect NPCs", "No NPCs found in any world.")
            return

        items = sorted(by_crc.items(), key=lambda kv: kv[0])
        self.log("Collect NPCs: %d unique TemplateCRC(s)" % len(items))

        # --- load target, find EntityArray to host copies ---
        try:
            container = load_container(target_path)
        except Exception as ex:
            messagebox.showerror("Load failed", str(ex))
            return
        self.savefile_path.set(target_path)
        self.container = container

        # All BKCK EntityArrays we can append to (entry, doc, kind, child_n)
        hosts = []
        origin = None
        pad_y = None
        for e in container.entries:
            if e.get("tag") != b"BKCK":
                continue
            try:
                doc, kind = unwrap(container.chunk(e), self.dctx)
                nodes, _ = bson_parse(bytearray(doc))
            except Exception:
                continue
            for pad in extract_world_landing_pads(nodes):
                if pad.get("pos"):
                    origin = pad["pos"]
                    pad_y = pad["pos"][1]
            npc_pos = []
            for npc in extract_world_npcs(nodes):
                if npc.get("pos"):
                    npc_pos.append(npc["pos"])
            if npc_pos and origin is None:
                xs = [p[0] for p in npc_pos]
                ys = [p[1] for p in npc_pos]
                zs = [p[2] for p in npc_pos]
                origin = (sorted(xs)[len(xs)//2],
                          sorted(ys)[len(ys)//2],
                          sorted(zs)[len(zs)//2])
            earr = None
            for n in _walk(nodes):
                if n.get("key") == "EntityArray" and n.get("children") is not None:
                    earr = n
                    break
            if earr is None:
                continue
            child_n = len(earr.get("children") or [])
            hosts.append((e, doc, kind, child_n))

        if not hosts:
            messagebox.showerror(
                "No EntityArray",
                "Target world has no EntityArray to insert into.\n"
                "Open a world that already has at least one entity.")
            return

        # Prefer emptier arrays first so we don't bloat one huge BKCK.
        hosts.sort(key=lambda h: h[3])

        if origin is None:
            origin = (64.5, 60.0, 64.5)
        ox, oy, oz = float(origin[0]), float(origin[1]), float(origin[2])
        if pad_y is not None:
            oy = float(pad_y)

        # Island-aware safe band when we can resolve dims
        width = height = depth = None
        try:
            width, height, depth = self._lookup_target_island_dims(target_path)
        except Exception:
            pass
        if width or depth:
            x_min, x_max, z_min, z_max = npc_safe_xz_band(width, depth)
        else:
            x_min = z_min = _NPC_SAFE_XZ_MIN
            x_max = z_max = _NPC_SAFE_XZ_MAX
        try:
            max_layers = npc_max_layers(oy, height)
        except Exception:
            max_layers = None

        positions = _npc_grid_positions(
            len(items), ox, oy, oz,
            x_min, x_max, z_min, z_max, max_layers)
        uniq = len(set((round(p[0], 2), round(p[1], 2), round(p[2], 2))
                       for p in positions))
        self.log(
            "Collect NPCs: origin=(%.1f, %.1f, %.1f)  "
            "XZ [%.0f..%.0f]×[%.0f..%.0f]  dims=%s×%s×%s  "
            "positions=%d unique=%d"
            % (ox, oy, oz, x_min, x_max, z_min, z_max,
               width, height, depth, len(positions), uniq))

        # Split across hosts: at most MAX_NEW per EntityArray
        MAX_NEW = 48
        remaining = list(enumerate(items))  # (pos_index, (tcrc, (body,wname,label)))
        total_inserted = 0
        host_i = 0
        # Reload container from disk before each write so offsets stay valid
        while remaining and host_i < len(hosts) * 4:
            host_entry, _doc0, kind, child_n = hosts[host_i % len(hosts)]
            host_i += 1
            try:
                container = load_container(target_path)
                self.container = container
            except Exception as ex:
                self.log("  reload failed: %s" % ex)
                break
            # re-find this entry by id
            e = None
            for ee in container.entries:
                if ee.get("id") == host_entry.get("id") and ee.get("tag") == b"BKCK":
                    e = ee
                    break
            if e is None:
                continue
            try:
                doc, kind = unwrap(container.chunk(e), self.dctx)
            except Exception as ex:
                self.log("  unwrap host: %s" % ex)
                continue
            batch = remaining[:MAX_NEW]
            remaining = remaining[MAX_NEW:]
            buf = bytearray(doc)
            try:
                nodes2, _ = bson_parse(buf)
            except Exception as ex:
                self.log("  parse host: %s" % ex)
                remaining = batch + remaining
                continue
            earr2 = None
            for n in _walk(nodes2):
                if n.get("key") == "EntityArray" and n.get("children") is not None:
                    earr2 = n
                    break
            if earr2 is None:
                remaining = batch + remaining
                continue
            next_idx = len(earr2.get("children") or [])
            blob = bytearray()
            for j, (pos_i, (tcrc, (body, wname, label))) in enumerate(batch):
                x, y, z = positions[pos_i]
                try:
                    body2 = _patch_entity_position_bytes(body, x, y, z)
                except Exception as ex:
                    self.log("  skip 0x%08X position patch: %s" % (tcrc, ex))
                    body2 = body
                blob += _encode_array_entity_element(next_idx + j, body2)
                self.log(
                    "  + 0x%08X %-28s from %s -> (%.1f, %.1f, %.1f)"
                    % (tcrc, (label or "")[:28], wname, x, y, z))
            if not blob:
                continue
            try:
                bson_insert_element(buf, earr2, bytes(blob))
                bson_parse(bytearray(buf))  # verify
            except Exception as ex:
                self.log("  insert failed: %s" % ex)
                remaining = batch + remaining
                continue
            try:
                self.write_container(
                    e["id"],
                    wrap(bytes(buf), kind, self.cctx),
                    verify_label="collect NPCs batch (%d)" % len(batch))
                total_inserted += len(batch)
            except Exception as ex:
                self.log("  write failed: %s" % ex)
                remaining = batch + remaining
                break

        # Count how many NPC Control entities are actually in the file now
        final_n = 0
        try:
            for _e, _d, _k, nodes, _c in self._scan_world_bkck(target_path):
                final_n += len(extract_world_npcs(nodes))
        except Exception:
            pass

        msg = (
            "Inserted %d unique NPC template(s) into:\n%s\n\n"
            "NPC Control entities now in file: %d\n"
            "Unique grid positions planned: %d\n\n"
            "If the map shows fewer, the game may still cull duplicates "
            "or NPCs outside loaded chunks — check the Log.\n"
            "Restart the game to see them."
            % (total_inserted, target_path, final_n, uniq))
        if remaining:
            msg += "\n\n%d left over (no host / write failed)." % len(remaining)
        messagebox.showinfo("Collect NPCs", msg)
        self.log("Collect NPCs: wrote %d unique NPCs into %s (file now has %d NPC Control)"
                 % (total_inserted, target_path, final_n))



    def open_world_map(self):
        """Top-down X/Z map with zoom, pan, and right-click to edit.

        No textures — rectangles for chests / signs / NPCs / landing pad.
        Mouse wheel = zoom, drag = pan, right-click or double-click = open.
        """
        path, info = self._resolve_world_path()
        if not path:
            messagebox.showinfo(
                "No world",
                "Select a world file first (filter → Worlds, or the Worlds tab).")
            return
        if not self._ensure_dict():
            return
        try:
            scanned = list(self._scan_world_bkck(path))
        except Exception as exc:
            messagebox.showerror("Save file error", str(exc))
            return

        # Keep BKCK entry context so right-click can open editors
        chests, signs, npcs, pads, others = [], [], [], [], []
        # Positions already claimed by a specialized extractor (avoid dup markers)
        claimed = set()

        def _pos_key(pos):
            if not pos:
                return None
            return (round(pos[0], 1), round(pos[2], 1))

        for e, doc, kind, nodes, container in scanned:
            if b"Server Inventory Component" in doc:
                for c in extract_world_chests(nodes):
                    if c.get("pos"):
                        c = dict(c)
                        c["_e"] = e
                        c["_doc"] = doc
                        c["_kind"] = kind
                        c["_container"] = container
                        chests.append(c)
                        claimed.add(_pos_key(c["pos"]))
            if b"User Editable String Component" in doc:
                for s in extract_world_signs(nodes):
                    if s.get("pos"):
                        s = dict(s)
                        s["_e"] = e
                        s["_doc"] = doc
                        s["_kind"] = kind
                        s["_container"] = container
                        signs.append(s)
                        claimed.add(_pos_key(s["pos"]))
            if b"NPC Control Component" in doc:
                for n in extract_world_npcs(nodes):
                    if n.get("pos"):
                        npcs.append(n)
                        claimed.add(_pos_key(n["pos"]))
            # Landing pads: component and/or TemplateCRC (see extractor)
            for p in extract_world_landing_pads(nodes):
                if p.get("pos"):
                    pads.append(p)
                    claimed.add(_pos_key(p["pos"]))
            # Everything else with a TemplateCRC (enemies, blocks, props…)
            for o in extract_world_all_templates(nodes):
                pk = _pos_key(o.get("pos"))
                if pk is None or pk in claimed:
                    continue
                others.append(o)
                claimed.add(pk)

        pts = []
        for group in (chests, signs, npcs, pads, others):
            for it in group:
                x, _y, z = it["pos"]
                pts.append((x, z))
        if not pts:
            messagebox.showinfo(
                "Empty map",
                "No positioned entities found.\n\n"
                "Check that the world file is selected, pk_dict.bin is "
                "loaded, and the file actually has BKCK chunks.")
            return

        xs = [p[0] for p in pts]
        zs = [p[1] for p in pts]
        min_x, max_x = min(xs), max(xs)
        min_z, max_z = min(zs), max(zs)
        pad = max(8.0, 0.05 * max(max_x - min_x, max_z - min_z, 1.0))
        min_x -= pad
        max_x += pad
        min_z -= pad
        max_z += pad
        world_w = max(max_x - min_x, 1.0)
        world_h = max(max_z - min_z, 1.0)

        dlg = tk.Toplevel(self)
        world_label = (info.get("location_name") or
                       info.get("label") or "World")
        fname = os.path.basename(path)
        dlg.title("Map — %s  ·  %s" % (world_label, fname))
        dlg.geometry("920x720")

        hdr = ttk.Frame(dlg)
        hdr.pack(fill="x", padx=8, pady=4)
        ttk.Label(
            hdr,
            text="%s  ·  %s" % (world_label, fname),
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left")
        zoom_var = tk.StringVar(value="100%")
        ttk.Label(hdr, textvariable=zoom_var, width=6).pack(side="right")
        ttk.Label(
            hdr,
            text="chests=%d  signs=%d  NPCs=%d  pads=%d  other=%d"
                 % (len(chests), len(signs), len(npcs), len(pads),
                    len(others)),
            foreground="#444",
        ).pack(side="right", padx=12)

        # Layer toggles — uncheck to hide that marker type
        layers = ttk.Frame(dlg)
        layers.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Label(layers, text="Show:").pack(side="left")
        show_chests = tk.BooleanVar(value=True)
        show_signs = tk.BooleanVar(value=True)
        show_npcs = tk.BooleanVar(value=True)
        show_enemies = tk.BooleanVar(value=True)
        show_other = tk.BooleanVar(value=True)
        show_pads = tk.BooleanVar(value=True)
        layer_vars = (
            ("Chests", show_chests),
            ("Signs", show_signs),
            ("NPCs", show_npcs),
            ("Enemies", show_enemies),
            ("Other/prop", show_other),
            ("Pad", show_pads),
        )
        for text, var in layer_vars:
            ttk.Checkbutton(
                layers, text=text, variable=var,
                command=lambda: redraw()).pack(side="left", padx=4)
        ttk.Button(
            layers, text="NPCs+chests only",
            command=lambda: (
                show_chests.set(True), show_npcs.set(True),
                show_signs.set(False), show_enemies.set(False),
                show_other.set(False), show_pads.set(True),
                redraw())
        ).pack(side="left", padx=8)
        ttk.Button(
            layers, text="Show all",
            command=lambda: (
                show_chests.set(True), show_signs.set(True),
                show_npcs.set(True), show_enemies.set(True),
                show_other.set(True), show_pads.set(True),
                redraw())
        ).pack(side="left", padx=2)

        legend = ttk.Frame(dlg)
        legend.pack(fill="x", padx=8)

        def _legend_icon(parent, color, bw, bh, oval=False):
            # bw/bh = footprint blocks; scale for legend
            cw, ch = max(int(bw * 5), 5), max(int(bh * 5), 5)
            c = tk.Canvas(parent, width=cw + 4, height=ch + 4,
                          highlightthickness=0)
            c.pack(side="left")
            if oval:
                c.create_oval(2, 2, 2 + cw, 2 + ch, fill=color, outline="#111")
            else:
                c.create_rectangle(2, 2, 2 + cw, 2 + ch, fill=color,
                                   outline="#111")

        for color, bw, bh, label, oval in (
                ("#4a90d9", 2, 1, "Chest 2×1", False),
                ("#9b59b6", 1, 1, "Mannequin", False),
                ("#2ecc71", 1, 1, "Pet/mount", False),
                ("#e6c84b", 1, 1, "Sign", False),
                ("#d94a4a", 1, 1, "NPC", True),
                ("#c0392b", 1, 1, "Enemy", True),
                ("#7f8c8d", 1, 1, "Other/prop", False),
                ("#cccccc", 4, 4, "Pad 4×4", False)):
            f = ttk.Frame(legend)
            f.pack(side="left", padx=4)
            _legend_icon(f, color, bw, bh, oval=oval)
            ttk.Label(f, text=label).pack(side="left", padx=2)

        wrap = ttk.Frame(dlg)
        wrap.pack(fill="both", expand=True, padx=8, pady=4)
        canvas = tk.Canvas(wrap, bg="#1a1a1a", highlightthickness=0)
        hbar = ttk.Scrollbar(wrap, orient="horizontal", command=canvas.xview)
        vbar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)
        hbar.pack(side="bottom", fill="x")
        vbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        # Base drawing size at zoom=1.0
        BASE = 700.0
        if world_w >= world_h:
            base_w, base_h = BASE, max(80.0, BASE * world_h / world_w)
        else:
            base_h, base_w = BASE, max(80.0, BASE * world_w / world_h)
        margin = 24.0

        state = {
            "zoom": 1.0,
            "hit": [],       # list of dicts with world x/z + payload
            "drag": None,    # (canvasx, canvasy) at press
        }

        def world_to_canvas(x, z, zoom=None):
            zmul = state["zoom"] if zoom is None else zoom
            pw = base_w * zmul
            ph = base_h * zmul
            m = margin * max(1.0, zmul * 0.5)
            cx = m + (x - min_x) / world_w * pw
            cy = m + (max_z - z) / world_h * ph
            return cx, cy

        def redraw():
            canvas.delete("all")
            state["hit"] = []
            zmul = state["zoom"]
            pw = base_w * zmul
            ph = base_h * zmul
            m = margin * max(1.0, zmul * 0.5)
            total_w = pw + 2 * m
            total_h = ph + 2 * m
            canvas.config(scrollregion=(0, 0, total_w, total_h))

            # Frame + grid
            canvas.create_rectangle(m, m, m + pw, m + ph,
                                    outline="#444", width=1)
            grid_n = 8 if zmul < 2 else (16 if zmul < 4 else 32)
            for i in range(grid_n + 1):
                gx = m + pw * i / grid_n
                gz = m + ph * i / grid_n
                canvas.create_line(gx, m, gx, m + ph, fill="#2a2a2a")
                canvas.create_line(m, gz, m + pw, gz, fill="#2a2a2a")

            canvas.create_text(m, m - 6, text="Z↑  X→", fill="#888",
                               anchor="sw", font=("Segoe UI", 8))
            canvas.create_text(m, m + ph + 10,
                               text="(%.0f, %.0f)" % (min_x, min_z),
                               fill="#666", anchor="nw", font=("Segoe UI", 8))
            canvas.create_text(m + pw, m + ph + 10,
                               text="(%.0f, %.0f)" % (max_x, max_z),
                               fill="#666", anchor="ne", font=("Segoe UI", 8))

            # Footprint in game blocks (width X × depth Z), scaled to map.
            # Pixels per world unit on each axis:
            unit_x = pw / world_w
            unit_z = ph / world_h

            def add_footprint(x, z, bw, bh, color, kind, label, payload,
                              oval=False, outline="#111"):
                """Draw a bw×bh block footprint centered on (x,z)."""
                cx, cy = world_to_canvas(x, z)
                # Minimum 4px so markers stay clickable when zoomed out
                hw = max(bw * unit_x * 0.5, 4.0)
                hh = max(bh * unit_z * 0.5, 4.0)
                if oval:
                    iid = canvas.create_oval(
                        cx - hw, cy - hh, cx + hw, cy + hh,
                        fill=color, outline=outline, width=1, tags=("mark",))
                else:
                    iid = canvas.create_rectangle(
                        cx - hw, cy - hh, cx + hw, cy + hh,
                        fill=color, outline=outline, width=1, tags=("mark",))
                state["hit"].append({
                    "iid": iid, "kind": kind, "label": label,
                    "x": x, "z": z, "payload": payload,
                    "hw": hw, "hh": hh,
                })
                return iid

            # Footprints: draw props first, NPCs/pads last so they stay on top.
            inv_fp = {
                "chest":          (2, 1, "#4a90d9", False),
                "trader stock":    (2, 1, "#e67e22", False),
                "mannequin":      (1, 1, "#9b59b6", False),
                "pet/mount box":  (1, 1, "#2ecc71", False),
                "pet stand":      (1, 1, "#27ae60", False),
                "container":      (1, 1, "#5dade2", False),
            }
            last_pos = getattr(self, "_map_last_opened_pos", None)

            # 1) Other / enemy props (bottom layer)
            if show_other.get() or show_enemies.get():
                for o in others:
                    x, y, z = o["pos"]
                    tmpl = o.get("template")
                    tcrc = ((int(tmpl) & 0xFFFFFFFF)
                            if tmpl is not None else None)
                    is_enemy = tcrc in ENEMY_TEMPLATE_CRCS
                    if is_enemy and not show_enemies.get():
                        continue
                    if (not is_enemy) and not show_other.get():
                        continue
                    label = template_label(tmpl)
                    color = "#c0392b" if is_enemy else "#7f8c8d"
                    add_footprint(
                        x, z, 1, 1, color, "other",
                        "%s  %s  (%.1f, %.1f, %.1f)"
                        % (label,
                           ("0x%08X" % tcrc) if tcrc is not None else "?",
                           x, y, z),
                        o, oval=is_enemy)

            # 2) Chests / inventories
            # Game entity Position is often 1 block "north" of the visual
            # footprint for multi-block furniture (same issue NPCs had —
            # they already use z-1). Without this, chests sit one tile too
            # high vs mannequins/props in the top-down map.
            if show_chests.get():
                for c in chests:
                    x, y, z = c["pos"]
                    n = c.get("item_count") or 0
                    klabel, counts = classify_inventory_entity(c)
                    if klabel == "empty" or n <= 0:
                        continue
                    if klabel == "trader stock":
                        near_trader = False
                        for n_ent in npcs:
                            tmpl = n_ent.get("template")
                            if tmpl is None:
                                continue
                            if ((int(tmpl) & 0xFFFFFFFF)
                                    not in TRADER_TEMPLATE_CRCS):
                                continue
                            nx, _ny, nz = n_ent["pos"]
                            if abs(nx - x) < 1.5 and abs(nz - z) < 1.5:
                                near_trader = True
                                break
                        if near_trader:
                            continue
                    bw, bh, color, oval = inv_fp.get(
                        klabel, (1, 1, "#5dade2", False))
                    is_last = (last_pos is not None and
                               abs(last_pos[0] - x) < 0.25 and
                               abs(last_pos[1] - z) < 0.25)
                    if is_last:
                        color = {
                            "#4a90d9": "#1a3a66",
                            "#e67e22": "#6b3a0e",
                            "#9b59b6": "#3d1f4d",
                            "#2ecc71": "#0e5c32",
                            "#27ae60": "#0d4a2a",
                            "#5dade2": "#1a4a66",
                        }.get(color, "#222222")
                    add_footprint(
                        x, z, bw, bh, color, "chest",
                        "%s  items=%d  (%.1f, %.1f, %.1f)%s"
                        % (klabel, n, x, y, z,
                           "  [last opened]" if is_last else ""),
                        c, oval=oval,
                        outline=("#ffcc00" if is_last else "#111"))

            # 3) Signs
            if show_signs.get():
                for s in signs:
                    x, y, z = s["pos"]
                    text = (s.get("text") or "").strip()
                    tmpl = s.get("template")
                    tname = template_label(tmpl) if tmpl else "Sign"
                    if text:
                        tip = "Sign  %s  %r  (%.1f, %.1f, %.1f)" % (
                            tname, text[:50], x, y, z)
                    elif s.get("was_edited") is False:
                        tip = "Sign  %s  (default game text)  (%.1f, %.1f, %.1f)" % (
                            tname, x, y, z)
                    else:
                        tip = "Sign  %s  (no text in save)  (%.1f, %.1f, %.1f)" % (
                            tname, x, y, z)
                    add_footprint(
                        x, z, 1, 1, "#e6c84b", "sign", tip, s)

            # 4) NPCs on top of props (so they are not covered by gray)
            if show_npcs.get():
                for n in npcs:
                    x, y, z = n["pos"]
                    tmpl = n.get("template")
                    nname = template_label(tmpl) if tmpl else "NPC"
                    is_trader = tmpl is not None and (
                        int(tmpl) & 0xFFFFFFFF) in TRADER_TEMPLATE_CRCS
                    color = "#e67e22" if is_trader else "#d94a4a"
                    add_footprint(
                        x, z, 1, 1, color, "npc",
                        "%s  %s  (%.1f, %.1f, %.1f)"
                        % (nname,
                           ("0x%08X" % tmpl) if tmpl else "?",
                           x, y, z),
                        n, oval=True, outline="#ffffff")

            # 5) Landing pad
            if show_pads.get() and pads:
                best = pads[0]
                if len(pads) > 1:
                    mx = sum(p["pos"][0] for p in pads) / len(pads)
                    mz = sum(p["pos"][2] for p in pads) / len(pads)
                    best = min(pads, key=lambda p: (
                        (p["pos"][0] - mx) ** 2 + (p["pos"][2] - mz) ** 2))
                x, y, z = best["pos"]
                add_footprint(
                    x, z, 4, 4, "#222222", "pad",
                    "Landing pad  4×4  (%.1f, %.1f, %.1f)%s"
                    % (x, y, z,
                       ("  [%d components, showing 1]" % len(pads)
                        if len(pads) > 1 else "")),
                    best, outline="#eeeeee")

            zoom_var.set("%d%%" % int(round(zmul * 100)))

        tip = tk.StringVar(
            value="Hover · right/double-click: edit chest/sign · "
                  "copy CRC for NPC/enemy/gray prop")
        ttk.Label(dlg, textvariable=tip, foreground="#444").pack(
            anchor="w", padx=8, pady=(0, 2))

        def find_nearest(evt, radius=18):
            """Hit-test: prefer NPCs over props when footprints overlap."""
            cx = canvas.canvasx(evt.x)
            cy = canvas.canvasy(evt.y)
            # Later markers were drawn on top — scan reverse
            priority = {"npc": 0, "pad": 1, "chest": 2, "sign": 3,
                        "other": 4}
            best = None
            best_key = None
            for h in reversed(state["hit"]):
                px, py = world_to_canvas(h["x"], h["z"])
                hw = h.get("hw", 6)
                hh = h.get("hh", 6)
                inside = ((px - hw) <= cx <= (px + hw) and
                          (py - hh) <= cy <= (py + hh))
                d = (px - cx) ** 2 + (py - cy) ** 2
                if not inside and d > radius ** 2:
                    continue
                pri = priority.get(h.get("kind"), 5)
                # lower key is better: priority, then distance
                key = (0 if inside else 1, pri, d)
                if best_key is None or key < best_key:
                    best_key = key
                    best = h
            return best

        def on_motion(evt):
            h = find_nearest(evt)
            tip.set(h["label"] if h else
                    "Hover · right/double-click: edit chest/sign · "
                    "copy CRC for NPC/enemy/gray prop")

        def open_hit(h):
            if not h:
                return
            kind = h["kind"]
            payload = h["payload"]
            if kind == "chest":
                e = payload.get("_e")
                doc = payload.get("_doc")
                kind_w = payload.get("_kind")
                container = payload.get("_container")
                if e is None:
                    return
                # Remember + flash the marker so "last opened" is obvious
                pos = payload.get("pos")
                if pos:
                    self._map_last_opened_pos = (pos[0], pos[2])
                try:
                    iid = h.get("iid")
                    if iid is not None:
                        # Brief gold flash, then redraw darker as last-opened
                        canvas.itemconfigure(iid, fill="#ffcc00",
                                             outline="#ffffff", width=2)
                        def _after_flash():
                            try:
                                redraw()
                            except Exception:
                                pass
                        dlg.after(180, _after_flash)
                except Exception:
                    pass
                self.savefile_path.set(path)
                self.container = container
                self._update_file_info_label()
                self._edit_chest_inventory(
                    path, e, doc, kind_w, payload, container, on_done=None)
            elif kind == "sign":
                # Reuse sign edit flow via a tiny local dialog
                e = payload.get("_e")
                container = payload.get("_container")
                node = payload.get("text_node")
                if e is None or node is None:
                    return
                self.savefile_path.set(path)
                self.container = container
                cur = payload.get("text") or ""
                sd = tk.Toplevel(dlg)
                sd.title("Edit sign text")
                var = tk.StringVar(value=cur)
                ttk.Label(sd, text="Sign text:").pack(anchor="w", padx=10,
                                                      pady=(10, 2))
                ttk.Entry(sd, textvariable=var, width=64).pack(padx=10, pady=4)

                def apply():
                    new = var.get()
                    try:
                        fresh_doc, fresh_kind = unwrap(
                            self.container.chunk(e), self.dctx)
                        fresh_nodes, _ = bson_parse(bytearray(fresh_doc))
                    except Exception as ex:
                        messagebox.showerror("Reload failed", str(ex),
                                             parent=sd)
                        return
                    target = None
                    for s in extract_world_signs(fresh_nodes):
                        if s.get("text") == cur or s.get("pos") == payload.get("pos"):
                            target = s["text_node"]
                            if s.get("text") == cur:
                                break
                    if target is None:
                        messagebox.showerror(
                            "Not found",
                            "Could not re-locate the sign.", parent=sd)
                        return
                    if target.get("type") == 0x05:
                        old = target.get("value") or b""
                        enc = new.encode("utf-8")
                        if len(enc) > len(old):
                            messagebox.showerror(
                                "Too long",
                                "Field holds %d bytes; text needs %d."
                                % (len(old), len(enc)), parent=sd)
                            return
                        new_val = enc + b"\x00" * (len(old) - len(enc))
                        ok = self.commit_bson_edit(
                            e, fresh_doc, fresh_kind, target, new_val)
                    else:
                        ok = self.commit_bson_edit(
                            e, fresh_doc, fresh_kind, target, new)
                    if ok:
                        payload["text"] = new
                        sd.destroy()
                        tip.set("Saved sign text.")

                ttk.Button(sd, text="Save", command=apply).pack(pady=8)
            elif kind in ("npc", "other"):
                # Copy template CRC + optional "add to dictionary" dialog
                payload = h.get("payload") or {}
                tmpl = payload.get("template")
                if tmpl is not None:
                    tcrc = int(tmpl) & 0xFFFFFFFF
                    nname = template_label(tmpl)
                    text = "0x%08X\t%s\t%d" % (tcrc, nname, tcrc)
                    try:
                        dlg.clipboard_clear()
                        dlg.clipboard_append(text)
                        dlg.update_idletasks()
                    except Exception:
                        pass
                    known = npc_name_for_template(tcrc)
                    if known and not known.startswith("0x"):
                        tip.set("Copied %s" % text)
                    else:
                        tip.set("Copied %s — opening name dialog…" % text)
                        # Prompt to name unknown CRC → pk_templates.json
                        def _ask():
                            self.open_template_editor(
                                preset_crc=tcrc, preset_name="")
                        dlg.after(50, _ask)
                else:
                    text = h.get("label") or ""
                    try:
                        dlg.clipboard_clear()
                        dlg.clipboard_append(text)
                        dlg.update_idletasks()
                        tip.set("Copied label")
                    except Exception:
                        tip.set(text)
            elif kind == "pad":
                messagebox.showinfo(
                    "Landing pad",
                    h["label"] + "\n\nThis is the player spawn / landing pad.",
                    parent=dlg)

        def on_right_click(evt):
            open_hit(find_nearest(evt))

        def on_double(evt):
            open_hit(find_nearest(evt))

        # Zoom on mouse wheel (Windows / Linux / macOS)
        def on_zoom(evt):
            # delta: Windows uses evt.delta (120 steps); Linux Button-4/5
            if hasattr(evt, "delta") and evt.delta:
                factor = 1.15 if evt.delta > 0 else (1 / 1.15)
            elif getattr(evt, "num", None) == 4:
                factor = 1.15
            elif getattr(evt, "num", None) == 5:
                factor = 1 / 1.15
            else:
                return
            old = state["zoom"]
            new = max(0.25, min(12.0, old * factor))
            if abs(new - old) < 1e-6:
                return
            # Zoom around cursor
            cx = canvas.canvasx(evt.x)
            cy = canvas.canvasy(evt.y)
            # World point under cursor before zoom
            # Invert world_to_canvas at old zoom
            pw_old = base_w * old
            ph_old = base_h * old
            m_old = margin * max(1.0, old * 0.5)
            wx = min_x + (cx - m_old) / max(pw_old, 1e-6) * world_w
            wz = max_z - (cy - m_old) / max(ph_old, 1e-6) * world_h
            state["zoom"] = new
            redraw()
            # Re-center so same world point stays under cursor
            nx, ny = world_to_canvas(wx, wz)
            # Move scroll so (nx,ny) appears at evt position
            # Approximate by xview/yview fractions
            sr = canvas.cget("scrollregion").split()
            if len(sr) == 4:
                try:
                    x0, y0, x1, y1 = map(float, sr)
                    tw = max(x1 - x0, 1)
                    th = max(y1 - y0, 1)
                    # Desired top-left so cursor world maps to evt
                    left = nx - evt.x
                    top = ny - evt.y
                    canvas.xview_moveto(max(0, min(1, left / tw)))
                    canvas.yview_moveto(max(0, min(1, top / th)))
                except Exception:
                    pass

        def on_press(evt):
            # Always a 6-tuple: (screen_x, screen_y, xview0, yview0, tw, th)
            sr = canvas.cget("scrollregion").split()
            try:
                x0, y0, x1, y1 = map(float, sr)
                tw = max(x1 - x0, 1.0)
                th = max(y1 - y0, 1.0)
            except Exception:
                tw, th = 1.0, 1.0
            state["drag"] = (
                evt.x, evt.y,
                canvas.xview()[0], canvas.yview()[0],
                tw, th,
            )

        def on_drag(evt):
            if not state["drag"]:
                return
            lx, ly, xv0, yv0, tw, th = state["drag"]
            dx = evt.x - lx
            dy = evt.y - ly
            # Mouse right → content moves right → xview decreases
            canvas.xview_moveto(max(0.0, min(1.0, xv0 - dx / tw)))
            canvas.yview_moveto(max(0.0, min(1.0, yv0 - dy / th)))

        def on_release(evt):
            state["drag"] = None

        canvas.bind("<Motion>", on_motion)
        canvas.bind("<Button-3>", on_right_click)          # right-click
        canvas.bind("<Double-Button-1>", on_double)        # double left
        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        # Zoom
        canvas.bind("<MouseWheel>", on_zoom)               # Windows
        canvas.bind("<Button-4>", on_zoom)                 # Linux up
        canvas.bind("<Button-5>", on_zoom)                 # Linux down
        # Also bind on dialog so wheel works when focus is odd
        dlg.bind("<MouseWheel>", on_zoom)

        bf = ttk.Frame(dlg)
        bf.pack(fill="x", padx=8, pady=4)

        def zoom_btn(factor):
            state["zoom"] = max(0.25, min(12.0, state["zoom"] * factor))
            redraw()

        ttk.Button(bf, text="Zoom −", command=lambda: zoom_btn(1 / 1.25))\
            .pack(side="left")
        ttk.Button(bf, text="Zoom +", command=lambda: zoom_btn(1.25))\
            .pack(side="left", padx=4)
        ttk.Button(bf, text="Reset 100%",
                   command=lambda: (state.update(zoom=1.0), redraw()))\
            .pack(side="left", padx=4)
        ttk.Button(bf, text="Close", command=dlg.destroy).pack(side="right")

        redraw()

    def _autoload_characters(self):
        """Populate the Characters tab on startup without a button press."""
        try:
            # Only auto-load when the tree is still empty so we don't
            # stomp a selection the user already made.
            if self.tree.get_children():
                return
            dictpath = self.dictfile_path.get()
            if not dictpath or not os.path.isfile(dictpath):
                # Dict not ready yet — try again shortly (auto-extract
                # may still be finishing). Don't pop an error dialog.
                self.after(800, self._autoload_characters)
                return
            if self.dctx is None:
                # Quietly prepare the dict without the error dialog path
                if not self._ensure_dict():
                    return
            self.load_characters()
        except Exception as ex:
            self.log("Auto-load characters skipped: %s" % ex)

    def load_characters(self):
        path = self.savefile_path.get()
        # If current selection is not a character file (e.g. universe/world
        # still selected after browsing those tabs), auto-switch to the
        # local 0100000000000000 character file when we can find it.
        info = parse_save_filename(path) if path else {"type": "unknown"}
        if not path or not os.path.isfile(path) or info["type"] not in (
                "character", "character_backup"):
            found = None
            # Prefer same directory as current selection, then any known root
            roots = []
            if path:
                roots.append(os.path.dirname(path))
            for _label, root in candidate_roots():
                if root not in roots:
                    roots.append(root)
            for root in roots:
                for name in ("0100000000000000", "0200000000000000"):
                    cand = os.path.join(root, name)
                    if os.path.isfile(cand):
                        found = cand
                        break
                if found:
                    break
            if not found:
                # Last resort: scan already-discovered saves
                for _l, _r, _f, full, i, _m in getattr(self, "_all_saves", []):
                    if i["type"] == "character":
                        found = full
                        break
                    if found is None and i["type"] == "character_backup":
                        found = full
            if not found:
                messagebox.showerror(
                    "No character file",
                    "Could not find 0100000000000000.\n\n"
                    "Select it under Show → Characters, then Load selected.")
                return
            path = found
            self.savefile_path.set(path)
            self._update_file_info_label()
            self.log("Auto-selected character file: %s" % path)
            info = parse_save_filename(path)
        if not self._ensure_dict():
            return

        try:
            self.container = load_container(path)
        except Exception as exc:
            messagebox.showerror("Save file error", str(exc))
            return

        hdr_ok, dat_ok = self.container.verify()
        self.log("Loaded %s - %d entries, header CRC %s, data CRC %s"
                  % (path, self.container.count,
                     "valid" if hdr_ok else "MISMATCH",
                     "valid" if dat_ok else "MISMATCH"))

        # Warn if this file has fewer characters than last time. A save
        # silently dropping from 6 entries to 1 is the single most
        # important thing to notice, and it is easy to miss in a log line
        # - valid CRCs say the file is well-formed, not that it is
        # intact.
        self._warn_if_shrunk(path, self.container.count)

        for row in self.tree.get_children():
            self.tree.delete(row)
        self.entries_by_row = {}

        # Collect first, then sort by the character's own slotId. The
        # container's entry order is storage order and does not match the
        # order the game shows characters in.
        rows = []
        for e, doc, kind, err in iter_docs(self.container, self.dctx):
            tag = e.get("tag")
            tag_s = tag.decode("ascii", "replace") if isinstance(tag, bytes) else str(tag)
            if err:
                rows.append((None, e, None, None, None, None,
                             "(failed: %s)" % err))
                self.log("  entry %d tag=%s ERROR: %s" % (e["index"], tag_s, err))
                continue
            if kind == "need-dict":
                rows.append((None, e, None, None, None, None,
                             "(zstd, dictionary problem)"))
                continue
            if doc is None:
                rows.append((None, e, None, None, None, None,
                             "(unrecognised payload, tag=%s)" % tag_s))
                self.log("  entry %d tag=%s unrecognised payload head=%s"
                         % (e["index"], tag_s,
                            self.container.chunk(e)[:8].hex()))
                continue
            slot = character_slot_id(doc)
            fields = find_name_fields(doc)
            if not fields:
                # Debug: show what we *did* find so "no name field" is actionable
                preview = []
                for m in re.finditer(rb"[\x20-\x7e]{4,32}", doc[:800]):
                    preview.append(m.group().decode("ascii", "replace"))
                    if len(preview) >= 6:
                        break
                hint = ", ".join(preview) if preview else "no ascii strings"
                rows.append((slot, e, doc, kind, None, None,
                             "(no name field; tag=%s; %s)" % (tag_s, hint[:60])))
                self.log("  entry %d tag=%s size=%d kind=%s — no \\x05name\\x00 field"
                         % (e["index"], tag_s, e["size"], kind))
                continue
            for off, length, text in fields:
                rows.append((slot, e, doc, kind, off, length, text))
                self.log("  entry %d tag=%s slot=%s name=%r"
                         % (e["index"], tag_s, slot, text))

        # Unknown slot sinks to the bottom rather than pretending to be 0.
        rows.sort(key=lambda r: (r[0] is None,
                                 r[0] if r[0] is not None else 0,
                                 r[1]["index"]))

        for slot, e, doc, kind, off, length, text in rows:
            row = self.tree.insert(
                "", "end",
                values=("" if slot is None else slot, text,
                        e["index"], e["size"]))
            if off is not None:
                self.entries_by_row[row] = (e, doc, kind, off, length)

        n_ok = sum(1 for r in rows if r[4] is not None)
        self.log("Characters listed: %d (of %d container entr(y/ies))"
                 % (n_ok, self.container.count))
        if self.container.count <= 1:
            backups = self._find_char_backup(path)
            if backups:
                self.log(
                    "Only %d character entr(y/ies) in this file. "
                    "If you expected more, click 'Restore character backup' "
                    "(found: %s)."
                    % (self.container.count,
                       ", ".join(l for l, _p in backups)))
            else:
                self.log(
                    "Only %d character entr(y/ies) in this file. "
                    "No 0200000000000000 / .bak found to restore from."
                    % self.container.count)

    # -- generic field editor ----------------------------------------------

    def doc_for_entry_id(self, entry_id):
        """Re-read one entry from the currently loaded container.

        Used by the field editor to refresh itself after a write without
        closing. Returns (entry, doc, kind, nodes) or None.
        """
        for e, doc, kind, err in iter_docs(self.container, self.dctx):
            if e["id"] != entry_id or doc is None:
                continue
            try:
                nodes, _ = bson_parse(doc)
            except Exception:
                return None
            return e, doc, kind, nodes
        return None

    def write_container(self, target_id, payload, verify_fn=None,
                        verify_label="edit"):
        """Single, shared write path for every mutation that replaces
        exactly one entry's payload in the currently loaded container
        (which is every mutation this tool makes - rename, stat edit,
        stat insert, equip, bag add/remove all touch one CHAR entry).

        Handles, uniformly, for every caller:
          - backup-before-first-write (.bak holds the original state)
          - an ATOMIC write: the full buffer is built in memory first,
            then written to a temp file in the same folder and swapped
            into place with os.replace(), which is atomic on both
            Windows and POSIX. This closes the (small but nonzero)
            window where a crash or power loss mid-write could leave a
            half-written, corrupt save on disk.
          - CRC round-trip verification (header + data) against what
            was actually written back out of memory
          - an optional content-level check via verify_fn(check), for
            confirming the specific value/name/field this edit was
            supposed to change actually reads back correctly - not just
            that the container's checksums are internally consistent

        verify_fn receives the freshly re-parsed Container and should
        return True/False. If it raises, that counts as verification
        failure rather than propagating (a bug in a verify check should
        never look like a corrupted save).

        Returns (ok, check) where check is the Container reflecting
        what's now on disk (only meaningful data on success — callers
        should still treat a False ok as "don't trust this further").
        On success self.container is updated to check.
        """
        path = self.savefile_path.get()
        new_entries = []
        for entry in self.container.entries:
            chunk = self.container.chunk(entry)
            new_entries.append(
                (entry["id"], entry["tag"],
                 payload if entry["id"] == target_id else chunk))
        out = rebuild_container(new_entries)

        bak = path + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
            self.log("Backup written: %s" % bak)

        tmp_path = path + ".tmp-%d" % os.getpid()
        try:
            with open(tmp_path, "wb") as fh:
                fh.write(out)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        check = Container(out)
        h_ok, d_ok = check.verify()
        content_ok = True
        if verify_fn is not None:
            try:
                content_ok = bool(verify_fn(check))
            except Exception as vexc:
                self.log("Verification check itself raised: %s" % vexc)
                content_ok = False

        ok = h_ok and d_ok and content_ok
        if ok:
            self.container = check
        else:
            self.log(
                "WARNING: %s wrote but verification failed (header CRC "
                "%s, data CRC %s, content check %s). Restore %s before "
                "running the game." % (verify_label, h_ok, d_ok,
                                        content_ok, bak))
            messagebox.showwarning(
                "Verification failed",
                "The file was written but didn't verify correctly.\n"
                "Restore %s before running the game." % bak)
        return ok, check

    def commit_bson_edits(self, e, doc, kind, edits,
                          verify_label="batch edit"):
        """Apply several (node, new_value) edits to one character's
        document with a single backup/write/verify, instead of one
        full rebuild+write+verify per field.

        A single "max all stacks" click can touch 20+ fields; a single
        write instead of 20 is both faster (one rebuild/CRC/disk write)
        and safer (one CRC check covering the whole batch instead of
        N separate ones each briefly leaving the file at an
        intermediate state).

        Restriction: every field in the batch must round-trip to the
        SAME byte width it started at (true for all the fixed-width
        numeric/bool BSON types - stack counts, item ids, levels,
        coins, and so on). A size-changing edit (e.g. a string getting
        longer or shorter) shifts every offset after it in the buffer,
        which would silently invalidate the other pending edits'
        pre-computed offsets if applied in the same pass. Rather than
        try to track that shift, a size-changing edit here is rejected
        with a clear error - callers should send those through
        commit_bson_edit one at a time instead.
        """
        try:
            buf = bytearray(doc)
            applied = []
            for node, new_value in edits:
                delta = bson_patch(buf, node, new_value)
                if delta != 0:
                    raise ValueError(
                        "batch edit only supports fixed-width fields; "
                        "%r changed size by %d byte(s) - edit it on its "
                        "own instead" % (pretty_path(node["path"]), delta))
                applied.append((node, new_value))

            payload = wrap(bytes(buf), kind, self.cctx)
            target_id = e["id"]

            def verify_fn(check):
                for ee, ddoc, kkind, eerr in iter_docs(check, self.dctx):
                    if ee["id"] != target_id or ddoc is None:
                        continue
                    try:
                        fresh_nodes, _ = bson_parse(ddoc)
                    except Exception:
                        return False
                    for node, new_value in applied:
                        hit = bson_find(fresh_nodes, node["path"])
                        if (hit is None
                                or hit["value"] != node_expected_value(
                                    node, new_value)):
                            return False
                    return True
                return False

            ok, _check = self.write_container(
                target_id, payload, verify_fn=verify_fn,
                verify_label=verify_label)
            if ok:
                self.log("%s: %s field(s) set in one write. Verified: "
                         "CRCs valid, all values read back correctly."
                         % (verify_label, len(applied)))
            return ok
        except Exception as exc:
            self.log("ERROR during %s: %s" % (verify_label, exc))
            messagebox.showerror("Edit failed", str(exc))
            return False

    def commit_stat_insert(self, e, doc, kind, parent_node, hexkey, value,
                           label):
        """Write a brand-new stat field, then verify it reads back."""
        try:
            buf = bytearray(doc)
            bson_insert_float(buf, parent_node, hexkey, value)
            # Prove the document is still well-formed before writing.
            fresh_nodes, total = bson_parse(buf)
            if total != len(buf):
                raise ValueError("document length %d != buffer %d after "
                                 "insert" % (total, len(buf)))
            payload = wrap(bytes(buf), kind, self.cctx)
            target_id = e["id"]

            def verify_fn(check):
                for ee, ddoc, _k, _err in iter_docs(check, self.dctx):
                    if ee["id"] != target_id or ddoc is None:
                        continue
                    try:
                        nn, _ = bson_parse(ddoc)
                    except Exception:
                        continue
                    for n in _iter_all(nn):
                        if n["key"] == hexkey:
                            return True
                return False

            ok, _check = self.write_container(
                target_id, payload, verify_fn=verify_fn,
                verify_label="stat insert")
            if ok:
                self.log("Added %s = %g. Verified: CRCs valid, field reads "
                         "back." % (label, value))
                self.load_characters()
            return ok
        except Exception as exc:
            self.log("ERROR adding stat: %s" % exc)
            messagebox.showerror("Add failed", str(exc))
            return False

    def open_character_editor(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showerror("No selection", "Select a character in the "
                                  "list first.")
            return
        row = sel[0]
        if row not in self.entries_by_row:
            messagebox.showerror("Can't edit", "This row has no readable "
                                  "document (see the list for why).")
            return
        e, doc, kind, _off, _length = self.entries_by_row[row]
        try:
            nodes, _ = bson_parse(doc)
        except Exception as exc:
            messagebox.showerror("Parse failed", str(exc))
            return
        CharacterEditor(self, e, doc, kind, nodes)

    def open_field_editor(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showerror("No selection", "Select a character in the "
                                  "list first.")
            return
        row = sel[0]
        if row not in self.entries_by_row:
            messagebox.showerror("Can't edit", "This row has no readable "
                                  "document (see the list for why).")
            return
        e, doc, kind, _off, _length = self.entries_by_row[row]
        try:
            nodes, _ = bson_parse(doc)
        except BsonUnknownType as exc:
            hits = bson_probe(doc, 0, exc.etype)
            lines = ["This document has a custom BSON type this tool "
                     "doesn't know yet:", "",
                     "  type 0x%02X, key %r, offset %d"
                     % (exc.etype, exc.key, exc.offset), ""]
            if not hits:
                lines.append("Tried fixed widths 1-40 bytes and two "
                              "length-prefixed encodings - none let the "
                              "rest of the document parse cleanly.")
            else:
                lines.append("Hypothesis/hypotheses that parse the rest "
                              "of the document cleanly:")
                vstart = exc.offset + 1 + len(exc.key) + 1
                for label, width, count in hits:
                    if width is not None:
                        sample = doc[vstart:vstart + width]
                        lines.append("  %s (%d field(s)) - first value "
                                      "hex: %s" % (label, count,
                                                    sample.hex()))
                    else:
                        L = struct.unpack_from("<i", doc, vstart)[0]
                        lines.append("  %s (%d field(s)) - first "
                                      "declared length: %d" % (label,
                                                                count, L))
                if len(hits) == 1:
                    lines.append("")
                    lines.append("Only one hypothesis worked, which is "
                                  "strong evidence it's correct. Send me "
                                  "this info and I'll add 0x%02X as a "
                                  "known type." % exc.etype)
                else:
                    lines.append("")
                    lines.append("More than one worked - ambiguous from "
                                  "structure alone, send me this info.")
            vstart_dump = exc.offset + 1 + len(exc.key) + 1
            dump_start = max(0, vstart_dump - 16)
            dump = doc[dump_start:vstart_dump + 96]
            lines.append("")
            lines.append("Raw bytes around this field (value starts at "
                          "offset %d, marked with >>):" % vstart_dump)
            for row in range(0, len(dump), 16):
                seg = dump[row:row + 16]
                addr = dump_start + row
                hexs = " ".join("%02X" % b for b in seg)
                asc = "".join(chr(b) if 32 <= b <= 126 else "."
                               for b in seg)
                marker = " >>" if addr <= vstart_dump < addr + 16 else ""
                lines.append("  %06X  %-47s  %s%s"
                              % (addr, hexs, asc, marker))
            lines.append("")
            lines.append("Paste this back and I'll figure out the width "
                          "from the byte pattern.")

            msg = "\n".join(lines)
            self.log(msg)
            messagebox.showinfo(
                "Unknown field type",
                "Details written to the Log panel below - scroll down "
                "and copy the hex dump from there.")
            return
        except Exception as exc:
            messagebox.showerror(
                "Can't parse this document",
                "This entry contains a problem this tool doesn't "
                "recognise, so it's refusing to edit it rather than "
                "risk corrupting it:\n\n%s" % exc)
            return
        FieldEditor(self, e, doc, kind, nodes)

    def open_field_editor_for(self, e, doc, kind):
        """Open FieldEditor for a known entry (e.g. from Character Editor)."""
        try:
            nodes, _ = bson_parse(doc)
        except BsonUnknownType as exc:
            messagebox.showerror(
                "Unknown field type",
                "Custom BSON type 0x%02X key %r — see Log for details."
                % (exc.etype, exc.key))
            return
        except Exception as exc:
            messagebox.showerror("Can't parse", str(exc))
            return
        FieldEditor(self, e, doc, kind, nodes)

    def commit_bson_edit(self, e, doc, kind, node, new_value):
        """Shared write path for the generic field editor. Same safety
        flow as rename_selected: backup, write, reload, verify."""
        try:
            buf = bytearray(doc)
            bson_patch(buf, node, new_value)
            payload = wrap(bytes(buf), kind, self.cctx)
            # Match by entry id — identity fails after the container is
            # reloaded from disk (Character Editor keeps the old entry
            # dict around across edits).
            target_id = e["id"]

            def verify_fn(check):
                for ee, ddoc, kkind, eerr in iter_docs(check, self.dctx):
                    if ee["id"] != target_id or ddoc is None:
                        continue
                    try:
                        fresh_nodes, _ = bson_parse(ddoc)
                    except Exception:
                        continue
                    hit = bson_find(fresh_nodes, node["path"])
                    if (hit is not None
                            and hit["value"] == node_expected_value(
                                node, new_value)):
                        return True
                return False

            ok, _check = self.write_container(
                target_id, payload, verify_fn=verify_fn,
                verify_label="field edit")
            if ok:
                self.log("Set %s -> %r. Verified: CRCs valid, new value "
                          "reads back correctly." % (pretty_path(node["path"]),
                                                       new_value))
            return ok
        except Exception as exc:
            self.log("ERROR during field edit: %s" % exc)
            messagebox.showerror("Edit failed", str(exc))
            return False

    def rename_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showerror("No selection", "Select a character in the "
                                  "list first.")
            return
        row = sel[0]
        if row not in self.entries_by_row:
            messagebox.showerror("Can't rename", "This row has no editable "
                                  "name field (see the list for why).")
            return
        new = self.new_name.get().strip()
        if not new:
            messagebox.showerror("No name", "Type a new name first.")
            return
        if len(new.encode("utf-8")) > GAME_NAME_LIMIT:
            messagebox.showerror("Too long", "Names are limited to %d "
                                  "characters in game." % GAME_NAME_LIMIT)
            return

        if not messagebox.askyesno(
                "Confirm rename",
                "Rename this character to %r?\n\n"
                "A .bak copy of the current file will be made first."
                % new):
            return

        e, doc, kind, off, length = self.entries_by_row[row]
        old_text = self.tree.item(row, "values")[1]

        try:
            target_id = e["id"]
            buf = bytearray(doc)
            set_name(buf, off, length, new)
            payload = wrap(bytes(buf), kind, self.cctx)

            def verify_fn(check):
                for ee, ddoc, kkind, eerr in iter_docs(check, self.dctx):
                    if ddoc is None:
                        continue
                    for _, _, text in find_name_fields(ddoc):
                        if text == new:
                            return True
                return False

            ok, _check = self.write_container(
                target_id, payload, verify_fn=verify_fn,
                verify_label="rename")
            if ok:
                self.log("Renamed %r -> %r. Verified: CRCs valid, new name "
                          "reads back correctly." % (old_text, new))
                messagebox.showinfo("Done", "Renamed %r to %r." % (old_text, new))
                self.load_characters()
        except Exception as exc:
            self.log("ERROR during rename: %s" % exc)
            messagebox.showerror("Rename failed", str(exc))


class FieldEditor(tk.Toplevel):
    """Shows the full BSON document for one character as a nested tree.
    Double-click any editable leaf to change it. Container types
    (document/array) are shown collapsed/expandable but aren't directly
    editable - only their scalar leaves are."""

    def __init__(self, app, e, doc, kind, nodes):
        super().__init__(app)
        self.app = app
        self.e = e
        self.doc = doc
        self.kind = kind
        self.title("Fields - entry %d" % e["index"])
        self.geometry("620x500")

        cols = ("type", "value")
        self.tv = ttk.Treeview(self, columns=cols, show="tree headings")
        self.tv.heading("#0", text="Field")
        self.tv.heading("type", text="Type")
        self.tv.heading("value", text="Value")
        self.tv.column("#0", width=220)
        self.tv.column("type", width=110)
        self.tv.column("value", width=260)
        self.tv.pack(fill="both", expand=True, padx=8, pady=8)
        self.tv.bind("<Double-1>", self._on_double_click)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(0, 4))
        self.show_all = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="show hidden fields (SI, mirrors)",
                        variable=self.show_all,
                        command=self.reload_from_app).pack(side="left")
        ttk.Button(bar, text="Add stat...",
                   command=self.add_stat).pack(side="right", padx=4)
        ttk.Button(bar, text="Max stacks",
                   command=self.max_all_stacks).pack(side="right", padx=4)
        ttk.Button(bar, text="Expand all",
                   command=lambda: self._set_open(True)).pack(side="right")
        ttk.Button(bar, text="Collapse all",
                   command=lambda: self._set_open(False)).pack(side="right",
                                                               padx=4)
        ttk.Label(self, text="Double-click a value to edit it. "
                  "Documents/arrays and unrecognised types aren't "
                  "directly editable.").pack(padx=8, pady=(0, 8), anchor="w")

        self._node_by_iid = {}
        self._insert_nodes("", nodes)

    def add_stat(self):
        """Create a stat that isn't in this save yet.

        The format is sparse: a character stores only the stats it has.
        This save holds 11 of the 218 known fields, so the other 207 have
        no row to edit - they must be inserted first.
        """
        nodes = list(self._node_by_iid.values())
        av = [n for n in nodes if n["key"] == "AV" and n["children"]]
        if not av:
            messagebox.showerror(
                "No stat block",
                "No 'Attributes [AV]' document found in this character.")
            return
        target = av[0]
        present = {int(c["key"], 16) for c in target["children"]
                   if is_hex_key(c["key"])}
        missing = sorted(((h, n) for h, n in attr_names().items()
                          if h not in present), key=lambda t: t[1])

        dlg = tk.Toplevel(self)
        dlg.title("Add stat")
        dlg.geometry("620x460")
        ttk.Label(dlg, text="%d stat(s) not present in this character."
                  % len(missing),
                  font=("TkDefaultFont", 10, "bold")).pack(anchor="w",
                                                           padx=10,
                                                           pady=(10, 0))
        ttk.Label(dlg, foreground="#555", text=(
            "The save only stores stats a character actually has; the rest "
            "default at\nruntime. Adding one writes a new field, after "
            "which it can be edited\nlike any other.")).pack(
            anchor="w", padx=10, pady=(2, 6))

        qvar = tk.StringVar()
        ttk.Entry(dlg, textvariable=qvar).pack(fill="x", padx=10)
        lb = tk.Listbox(dlg, font=("Courier New", 9))
        lb.pack(fill="both", expand=True, padx=10, pady=6)
        rows = []

        def refresh(*_a):
            q = qvar.get().strip().lower()
            lb.delete(0, "end")
            del rows[:]
            for h, name in missing:
                if q and q not in _s(name).lower():
                    continue
                rows.append((h, name))
                lb.insert("end", "%-44s %08x" % (name, h))
        qvar.trace_add("write", refresh)

        vf = ttk.Frame(dlg)
        vf.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(vf, text="value:").pack(side="left")
        vval = tk.StringVar(value="0")
        ttk.Entry(vf, textvariable=vval, width=14).pack(side="left", padx=6)

        def add(_evt=None):
            sel = lb.curselection()
            if not sel:
                return
            h, name = rows[sel[0]]
            try:
                val = float(vval.get())
            except ValueError:
                messagebox.showerror("Bad value",
                                     "Enter a number.", parent=dlg)
                return
            if not messagebox.askyesno(
                    "Add stat",
                    "Add %s = %g to this character?\n\nA .bak is written "
                    "first." % (name, val), parent=dlg):
                return
            dlg.destroy()
            self.app.commit_stat_insert(self.e, self.doc, self.kind,
                                        target, "%08x" % h, val, name)
            self.reload_from_app()

        lb.bind("<Double-1>", add)
        ttk.Button(dlg, text="Add", command=add).pack(pady=(0, 8))
        refresh()

    def max_all_stacks(self):
        """Set every stackable SC in this character to that item's own
        cap, per item_table_merged.json - not a flat 65535.

        SC is a uint16, so 65535 is the largest value that could be
        encoded, but it isn't the real cap for any actual item; the game
        just clamps it back down on load. Per-item caps (10/16/20/32/
        50/99/...) are what the table's max_stack field records from
        real observed play, so filling to that number is a stack that
        actually sticks. Slots holding equipment are skipped - they
        carry a PI sub-document instead of a count, and forcing a stack
        there would corrupt the record.
        """
        nodes = list(self._node_by_iid.values())
        targets = []
        for n in nodes:
            if n["key"] != "SC":
                continue
            parent = next((m for m in nodes
                           if m["children"] and n in m["children"]), None)
            if parent is None:
                continue
            # An entry with a PI sub-document is a unique item, not a stack.
            if any(c["key"] == "PI" for c in parent["children"]):
                continue
            crc = next((c["value"] for c in parent["children"]
                        if c["key"] == "II"), None)
            if crc is None:
                continue
            cap = item_max_stack(crc)
            if n["value"] >= cap:
                continue
            targets.append((n, crc, cap))

        if not targets:
            messagebox.showinfo("Nothing to do",
                                "Every stack here is already at its cap.")
            return

        preview = "\n".join(
            "  %-28s %d -> %d" % (item_name_for_crc(c) or c, n["value"], cap)
            for n, c, cap in targets[:12])
        more = ("\n  ... and %d more" % (len(targets) - 12)
                if len(targets) > 12 else "")
        if not messagebox.askyesno(
                "Max stacks",
                "Set %d stack(s) to their item's cap?\n\n%s%s\n\nA .bak "
                "is written first." % (len(targets), preview, more)):
            return

        done = 0
        edits = [(n, cap) for n, _c, cap in targets]
        if self.app.commit_bson_edits(self.e, self.doc, self.kind, edits,
                                      verify_label="max all stacks"):
            done = len(edits)
            fresh = self.app.doc_for_entry_id(self.e["id"])
            if fresh is not None:
                self.e, self.doc, self.kind, _nodes2 = fresh
        self.app.log("Max stacks: %d of %d set to their per-item cap."
                     % (done, len(targets)))
        self.reload_from_app()

    def _set_open(self, state):
        for iid in self._node_by_iid:
            try:
                self.tv.item(iid, open=state)
            except tk.TclError:
                pass

    # -- refresh --------------------------------------------------------

    def _tree_state(self):
        """Remember which rows are open and which is selected, by path."""
        open_paths = set()
        for iid, node in self._node_by_iid.items():
            try:
                if self.tv.item(iid, "open"):
                    open_paths.add(node["path"])
            except tk.TclError:
                pass
        sel = self.tv.selection()
        sel_path = None
        if sel and sel[0] in self._node_by_iid:
            sel_path = self._node_by_iid[sel[0]]["path"]
        return open_paths, sel_path

    def _restore_state(self, open_paths, sel_path):
        for iid, node in self._node_by_iid.items():
            if node["path"] in open_paths:
                self.tv.item(iid, open=True)
            if sel_path is not None and node["path"] == sel_path:
                self.tv.selection_set(iid)
                self.tv.focus(iid)
                self.tv.see(iid)

    def reload_from_app(self):
        """Re-read this entry from the app's reloaded container and rebuild
        the tree, preserving expansion and selection."""
        open_paths, sel_path = self._tree_state()
        fresh = self.app.doc_for_entry_id(self.e["id"])
        if fresh is None:
            self.destroy()
            return
        e, doc, kind, nodes = fresh
        self.e, self.doc, self.kind = e, doc, kind
        for iid in self.tv.get_children(""):
            self.tv.delete(iid)
        self._node_by_iid = {}
        self._insert_nodes("", nodes)
        self._restore_state(open_paths, sel_path)

    def _insert_nodes(self, parent_iid, nodes, array_key=None):
        for n in display_order(nodes, array_key):
            # SI is the slot index, already shown in the parent row's
            # label ("[0] Helmet"), so the row is pure noise.
            if n["key"] in HIDE_KEYS and not self.show_all.get():
                continue
            tname = TYPE_NAMES.get(n["type"], "0x%02X" % n["type"])
            if n["children"] is not None:
                preview = "(%d field%s)" % (len(n["children"]),
                                             "" if len(n["children"]) == 1
                                             else "s")
            elif n["type"] == 0x05:
                preview = binary_preview(n["key"], n["value"])
            elif n["type"] == 0x14:
                label = crc_label(n["value"])
                if label is None and n["key"] == "N":
                    label = stat_field_label(n["value"])
                if label is None and n["key"] == "II":
                    label = item_name_for_crc(n["value"])
                preview = ("%d  (%s)" % (n["value"], label) if label
                           else str(n["value"]))
            else:
                preview = str(n["value"])
            iid = self.tv.insert(parent_iid, "end",
                                  text=row_label(n, array_key),
                                  values=(tname, preview))
            self._node_by_iid[iid] = n
            if n["children"] is not None:
                sub = n["key"] if n["key"] in ARRAY_LABELS else array_key
                self._insert_nodes(iid, n["children"], sub)
                if n["key"] not in COLLAPSE_BY_DEFAULT:
                    self.tv.item(iid, open=True)

    def _on_double_click(self, _event):
        iid = self.tv.focus()
        node = self._node_by_iid.get(iid)
        if node is None:
            return
        if not is_editable(node):
            if node["type"] == 0x14:
                messagebox.showinfo(
                    "Not editable",
                    "%r is a CRC-32 name hash, not a number.\n\n"
                    "The game looks assets up by this hash. Writing an "
                    "arbitrary value points it at something that doesn't "
                    "exist. To change what this refers to, paste a hash "
                    "the game already uses."
                    % node["key"])
                return
            messagebox.showinfo(
                "Not editable",
                "%r is a %s field - this tool doesn't edit that type."
                % (node["key"], TYPE_NAMES.get(node["type"],
                                                "0x%02X" % node["type"])))
            return
        if node["type"] == 0x14 and node["key"] == "II":
            self._edit_item_hash(node)
            return
        self._edit_dialog(node)

    def _edit_item_hash(self, node):
        """Change an equipment/inventory II (item CRC-32) value.

        Accepts decimal or 0x-hex, or search-and-pick an item by name -
        item_table_merged.json resolves the hash directly, no "teach it a
        pairing" step needed anymore. Does not bypass in-game license
        checks - only writes the save field.
        """
        crc = int(node["value"]) & 0xFFFFFFFF
        known = item_name_for_crc(crc)

        dlg = tk.Toplevel(self)
        dlg.title("Edit item hash (II)")
        dlg.geometry("600x480")
        ttk.Label(dlg, text="Current hash %d (0x%08X)" % (crc, crc),
                  font=("TkDefaultFont", 10, "bold")).pack(
            anchor="w", padx=10, pady=(10, 0))
        ttk.Label(dlg, text=("Currently: %s" % known) if known
                  else "Unknown hash (not in the item table).").pack(
            anchor="w", padx=10)
        ttk.Label(dlg, foreground="#555", text=(
            "Enter a new item CRC (decimal or 0xHEX), or search and pick "
            "an item below.\n"
            "Note: license-locked vanity may still refuse to equip "
            "in-game even if the save has the hash.")).pack(
            anchor="w", padx=10, pady=(4, 6))

        val_var = tk.StringVar(value="0x%08X" % crc)
        row = ttk.Frame(dlg)
        row.pack(fill="x", padx=10)
        ttk.Label(row, text="New hash:").pack(side="left")
        ttk.Entry(row, textvariable=val_var, width=24).pack(
            side="left", padx=6)

        qvar = tk.StringVar()
        ttk.Label(dlg, text="Search items:").pack(anchor="w", padx=10,
                                                   pady=(8, 0))
        ttk.Entry(dlg, textvariable=qvar).pack(fill="x", padx=10)
        lb = tk.Listbox(dlg, font=("Courier New", 9))
        lb.pack(fill="both", expand=True, padx=10, pady=6)
        results = []  # list of item records

        def refresh(*_a):
            lb.delete(0, "end")
            del results[:]
            try:
                for rec in item_search(qvar.get()):
                    results.append(rec)
                    lb.insert("end", "%-42s [%s]"
                              % (rec.get("name", "?"),
                                 rec.get("category", "")))
            except Exception as ex:
                lb.insert("end", "(search error: %s)" % ex)
        qvar.trace_add("write", refresh)

        def pick_from_list(_evt=None):
            sel = lb.curselection()
            if not sel:
                return
            rec = results[sel[0]]
            h = _as_u32(rec.get("hash"))
            if h is None:
                messagebox.showerror(
                    "No hash",
                    "That table row has no hash — cannot use it as II.",
                    parent=dlg)
                return
            val_var.set("0x%08X" % h)

        def apply_hash():
            raw = val_var.get().strip().replace("_", "")
            try:
                if raw.lower().startswith("0x"):
                    new_crc = int(raw, 16) & 0xFFFFFFFF
                else:
                    new_crc = int(raw, 0) & 0xFFFFFFFF
            except ValueError:
                messagebox.showerror("Bad hash",
                                     "Enter decimal or 0x-hex CRC32.",
                                     parent=dlg)
                return
            nm = item_name_for_crc(new_crc) or "(not in the item table)"
            msg = ("Set II to %d (0x%08X)\n%s\n\n"
                   "A .bak is made before writing if missing.\n"
                   "License-locked items may still be rejected in-game."
                   % (new_crc, new_crc, nm))
            if not messagebox.askyesno("Confirm edit", msg, parent=dlg):
                return
            ok = self.app.commit_bson_edit(self.e, self.doc, self.kind,
                                            node, new_crc)
            dlg.destroy()
            if ok:
                self.app.log("Set II = %d (0x%08X) %s"
                             % (new_crc, new_crc, nm))
                self.reload_from_app()

        lb.bind("<Double-1>", pick_from_list)
        bf = ttk.Frame(dlg)
        bf.pack(pady=6)
        ttk.Button(bf, text="Use selected item's hash",
                   command=pick_from_list).pack(side="left", padx=4)
        ttk.Button(bf, text="Apply hash",
                   command=apply_hash).pack(side="left", padx=4)
        refresh()

    def _edit_dialog(self, node):
        dlg = tk.Toplevel(self)
        dlg.title("Edit %s" % pretty_path(node["path"]))
        dlg.geometry("560x170")
        cur = node["value"]
        if node["type"] == 0x05:
            cur = binary_edit_text(node["key"], cur)
        ttk.Label(dlg, text="%s (%s):" % (pretty_path(node["path"]),
                  TYPE_NAMES.get(node["type"], "?"))).pack(
            anchor="w", padx=8, pady=(10, 2))
        if node["type"] == 0x05:
            ttk.Label(dlg, text=binary_hint(node["key"], node["value"]),
                      foreground="#555").pack(anchor="w", padx=8)
        var = tk.StringVar(value=str(cur))
        entry = ttk.Entry(dlg, textvariable=var, width=40)
        entry.pack(padx=8, pady=4, fill="x")
        entry.focus_set()
        entry.select_range(0, "end")

        def apply(_evt=None):
            raw = var.get()
            try:
                new_value = self._coerce(node, raw)
            except Exception as exc:
                messagebox.showerror("Invalid value", str(exc), parent=dlg)
                return
            warn = field_advisory(node, new_value)
            msg = ("Set %s to %r?\n\nA .bak copy is made before writing "
                   "if one doesn't already exist."
                   % (pretty_path(node["path"]), new_value))
            if warn:
                msg = "NOTE: %s\n\n%s" % (warn, msg)
            if not messagebox.askyesno("Confirm edit", msg, parent=dlg):
                return
            ok = self.app.commit_bson_edit(self.e, self.doc, self.kind,
                                            node, new_value)
            dlg.destroy()
            if ok:
                # Refresh in place. This used to call self.destroy(), which
                # slammed the whole field window shut after every single
                # edit - you had to reopen and re-expand the tree to change
                # the next field. The document HAS moved on (offsets shift
                # when a value changes size), so the tree is rebuilt from
                # the freshly parsed document, but the window stays open
                # with the same rows expanded and the same row selected.
                self.reload_from_app()

        ttk.Button(dlg, text="Apply", command=apply).pack(pady=8)
        dlg.bind("<Return>", apply)

    @staticmethod
    def _coerce(node, raw):
        etype = node["type"]
        if etype == 0x02:
            return raw
        if etype == 0x05:
            old_len = node["vend"] - node["vstart"] - 5
            return parse_binary_edit(node["key"], raw, old_len)
        if etype == 0x08:
            low = raw.strip().lower()
            if low in ("1", "true", "yes", "on"):
                return True
            if low in ("0", "false", "no", "off"):
                return False
            raise ValueError("expected true/false, got %r" % raw)
        if etype == 0x01:
            return float(raw)
        if etype in (0x09, 0x10, 0x11, 0x12, 0x13, 0x14, 0x16, 0x18):
            v = int(raw.strip(), 0)
            lim = field_limit(node)
            if lim:
                lo, hi, why = lim
                if not lo <= v <= hi:
                    raise ValueError("%s must be %d-%d: %s"
                                     % (node["key"], lo, hi, why))
            return v
        raise ValueError("don't know how to parse a value for this type")


class CharacterEditor(tk.Toplevel):
    """Multi-tab character editor: Armor, Vanity, Pets/Mounts, Extra Head,
    Player Stats, Backpack/Hotbar, Recipes, Quests.

    Builds on the same verified BSON patch path as FieldEditor. Empty-slot
    insertion for backpack/hotbar/pets is supported; equipment slots that
    already exist are swapped in place. Brand-new equipment slots are
    inserted as plain (SI, II, SC) records (best-effort - PI-bearing unique
    items keep their PI when only the hash is changed).
    """

    def __init__(self, app, e, doc, kind, nodes):
        super().__init__(app)
        self.app = app
        self.e = e
        self.doc = doc
        self.kind = kind
        self.nodes = nodes
        self.title("Character Editor")
        self.geometry("920x640")
        self.minsize(780, 520)

        # Prefer the player-side inventory mirror when present.
        self.inv_root = (find_component(nodes, "Player Inventory Component")
                         or find_component(nodes, "Server Inventory Component")
                         or nodes)

        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Button(top, text="Reload from save",
                   command=self.reload).pack(side="left")
        ttk.Button(top, text="Max all stacks",
                   command=self.max_all_stacks).pack(side="left", padx=6)
        ttk.Button(top, text="Edit raw fields…",
                   command=self._open_raw_fields).pack(side="left", padx=6)
        ttk.Label(top, text="Uses item_table_merged.json · unmatched "
                            "items hidden from pickers",
                  foreground="#555").pack(side="right")

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # Equipment (Armor / Vanity / Pets) share one top-level tab with
        # an inner notebook — same pattern as Backpack / Hotbar.
        self._tab_equip = ttk.Frame(self.nb)
        self._tab_stats = ttk.Frame(self.nb)
        self._tab_bags = ttk.Frame(self.nb)
        self._tab_recipes = ttk.Frame(self.nb)
        self._tab_quests = ttk.Frame(self.nb)

        self.nb.add(self._tab_equip, text="Equipment")
        self.nb.add(self._tab_stats, text="Player Stats")
        self.nb.add(self._tab_bags, text="Backpack / Hotbar")
        self.nb.add(self._tab_recipes, text="Recipes")
        self.nb.add(self._tab_quests, text="Quests")

        self._equip_nb = None
        self._bags_nb = None
        self._rebuild_all_tabs()

    def _open_raw_fields(self):
        """Open the generic BSON field editor for this character entry."""
        # Ensure the main app still points at the same character row data
        try:
            nodes, _ = bson_parse(self.doc)
        except Exception as exc:
            messagebox.showerror("Parse failed", str(exc), parent=self)
            return
        # FieldEditor lives on the App; reuse its dialog with current entry
        if not hasattr(self.app, "open_field_editor_for"):
            # Fallback: select matching row if possible, then call main editor
            self.app.open_field_editor()
            return
        self.app.open_field_editor_for(self.e, self.doc, self.kind)


    def _rebuild_all_tabs(self):
        """Destroy and rebuild every tab body (preserves notebook objects)."""
        for tab in (self._tab_equip, self._tab_stats, self._tab_bags,
                    self._tab_recipes, self._tab_quests):
            for child in tab.winfo_children():
                child.destroy()

        # Equipment sub-tabs
        self._equip_nb = ttk.Notebook(self._tab_equip)
        self._equip_nb.pack(fill="both", expand=True, padx=4, pady=4)
        self._tab_armor = ttk.Frame(self._equip_nb)
        self._tab_vanity = ttk.Frame(self._equip_nb)
        self._tab_pets = ttk.Frame(self._equip_nb)
        self._equip_nb.add(self._tab_armor, text="Armor")
        self._equip_nb.add(self._tab_vanity, text="Vanity")
        self._equip_nb.add(self._tab_pets, text="Pets / Mounts")

        self._build_equip_tab(self._tab_armor, "IEQ", ARMOR_SLOT_CATEGORIES,
                              include_extra_head=False)
        # Extra Head is stored on IEQ[6], not VEQ — show it under Vanity
        # by overlaying that one slot from the Armor array.
        self._build_equip_tab(self._tab_vanity, "VEQ", VANITY_SLOT_CATEGORIES,
                              include_extra_head=True,
                              extra_head_from="IEQ")
        self._build_pets_tab()
        self._build_stats_tab()
        self._build_bags_tab()
        self._build_recipes_tab()
        self._build_quests_tab()

    # -- shared refresh ------------------------------------------------

    def reload(self):
        """Re-read the save file from disk and refresh every tab.

        Preserves which main tab / equipment sub-tab / bag sub-tab was
        selected so adding an item doesn't kick you back to Armor.
        """
        path = self.app.savefile_path.get()
        if not path or not os.path.isfile(path):
            messagebox.showerror("No save", "No save file loaded.",
                                 parent=self)
            return

        # Remember tab positions before rebuild
        main_idx = bag_idx = equip_idx = 0
        try:
            main_idx = self.nb.index(self.nb.select())
        except Exception:
            pass
        try:
            if self._equip_nb is not None:
                equip_idx = self._equip_nb.index(self._equip_nb.select())
        except Exception:
            pass
        try:
            if self._bags_nb is not None:
                bag_idx = self._bags_nb.index(self._bags_nb.select())
        except Exception:
            pass

        entry_id = self.e["id"] if self.e else None
        try:
            self.app.container = load_container(path)
        except Exception as ex:
            messagebox.showerror("Reload failed",
                                 "Could not re-read save file:\n%s" % ex,
                                 parent=self)
            return

        fresh = self.app.doc_for_entry_id(entry_id) if entry_id is not None \
            else None
        if fresh is None:
            messagebox.showerror(
                "Reload failed",
                "Character entry %r not found after re-reading the save. "
                "Close this window and use Load characters."
                % entry_id, parent=self)
            return

        self.e, self.doc, self.kind, self.nodes = fresh
        self.inv_root = (find_component(self.nodes,
                                        "Player Inventory Component")
                         or find_component(self.nodes,
                                           "Server Inventory Component")
                         or self.nodes)
        self._rebuild_all_tabs()

        # Restore tab selection
        try:
            self.nb.select(main_idx)
        except Exception:
            pass
        try:
            if self._equip_nb is not None:
                self._equip_nb.select(equip_idx)
        except Exception:
            pass
        try:
            if self._bags_nb is not None:
                self._bags_nb.select(bag_idx)
        except Exception:
            pass

    def max_all_stacks(self):
        """Delegate to the same per-item-cap logic as FieldEditor."""
        # Temporarily open a FieldEditor-style walk over current nodes.
        targets = []
        for n in _walk(self.nodes):
            if n["key"] != "SC":
                continue
            # find parent
            parent = None
            for p in _walk(self.nodes):
                if p.get("children") and n in p["children"]:
                    parent = p
                    break
            if parent is None:
                continue
            if any(c["key"] == "PI" for c in parent["children"]):
                continue
            crc = next((c["value"] for c in parent["children"]
                        if c["key"] == "II"), None)
            if crc is None:
                continue
            cap = item_max_stack(crc)
            if n["value"] >= cap:
                continue
            targets.append((n, crc, cap))
        if not targets:
            messagebox.showinfo("Nothing to do",
                                "Every stack is already at its cap.",
                                parent=self)
            return
        preview = "\n".join(
            "  %-28s %d -> %d" % (item_name_for_crc(c) or c, n["value"], cap)
            for n, c, cap in targets[:12])
        more = ("\n  ... and %d more" % (len(targets) - 12)
                if len(targets) > 12 else "")
        if not messagebox.askyesno(
                "Max stacks",
                "Set %d stack(s) to their item's cap?\n\n%s%s"
                % (len(targets), preview, more), parent=self):
            return
        edits = [(n, cap) for n, _c, cap in targets]
        self.app.commit_bson_edits(self.e, self.doc, self.kind, edits,
                                   verify_label="max all stacks")
        fresh = self.app.doc_for_entry_id(self.e["id"])
        if fresh is not None:
            self.e, self.doc, self.kind, self.nodes = fresh
        self.reload()

    # -- equipment tabs (Armor / Vanity) --------------------------------

    def _build_equip_tab(self, parent, array_key, slot_cats,
                         include_extra_head=False, extra_head_from=None):
        arr = find_named_array(self.inv_root, array_key)
        # Fall back to whole document search
        if arr is None:
            arr = find_named_array(self.nodes, array_key)
        slots = inventory_slot_map(arr)

        # Extra Head lives on IEQ[6] even when shown under Vanity.
        extra_slots = {}
        extra_array_key = array_key
        if include_extra_head:
            src_key = extra_head_from or array_key
            extra_arr = find_named_array(self.inv_root, src_key) or \
                find_named_array(self.nodes, src_key)
            extra_slots = inventory_slot_map(extra_arr)
            extra_array_key = src_key

        max_slot = 6 if include_extra_head else 5

        hdr = ttk.Frame(parent)
        hdr.pack(fill="x", padx=8, pady=6)
        ttk.Label(hdr, text="%s — double-click a row or use Change to "
                            "pick a new item."
                            % ARRAY_LABELS.get(array_key, array_key)
                  ).pack(side="left")

        cols = ("slot", "name", "defence", "affixes")
        tree = ttk.Treeview(parent, columns=cols, show="headings",
                            height=10, selectmode="browse")
        for col, text, w in (("slot", "Slot", 110),
                             ("name", "Item", 260),
                             ("defence", "Defence", 70),
                             ("affixes", "Affixes", 280)):
            tree.heading(col, text=text)
            tree.column(col, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=8, pady=4)

        # iid -> (si, entry_node or None, write_array_key)
        row_meta = {}
        total_def = 0

        for si in range(0, max_slot + 1):
            name = EQUIP_SLOT_NAMES[si] if si < len(EQUIP_SLOT_NAMES) \
                else str(si)
            if si == 6 and include_extra_head:
                entry = extra_slots.get(6)
                write_key = extra_array_key
            else:
                entry = slots.get(si)
                write_key = array_key
            if entry:
                fields = item_entry_fields(entry)
                ii = fields.get("II")
                crc = ii["value"] if ii else 0
                rec, stats = item_stats_for_crc(crc)
                label = (rec or {}).get("name") or item_name_for_crc(crc) \
                    or "(unknown item)"
                def_s = "" if stats["defence"] is None else str(stats["defence"])
                if stats["defence"] is not None:
                    total_def += stats["defence"]
                aff = ", ".join(stats["affixes"][:4])
                iid = tree.insert("", "end", values=(name, label, def_s, aff))
            else:
                iid = tree.insert("", "end", values=(name, "(empty)", "", ""))
            row_meta[iid] = (si, entry, write_key)

        ttk.Label(hdr, text="  Total defence: %d" % total_def,
                  foreground="#333").pack(side="right")

        btns = ttk.Frame(parent)
        btns.pack(fill="x", padx=8, pady=6)

        def change_selected():
            sel = tree.selection()
            if not sel:
                return
            si, entry, write_key = row_meta[sel[0]]
            cats = slot_cats.get(si, ARMOR_CATEGORIES)
            bind = None
            if entry is not None:
                ii = item_entry_fields(entry).get("II")
                bind = _as_u32(ii.get("value")) if ii else None
            self._item_picker(
                title="Pick item for %s" % EQUIP_SLOT_NAMES[si],
                categories=cats,
                on_pick=lambda rec, wk=write_key, s=si, e=entry:
                    self._set_equip_slot(wk, s, e, rec))

        def clear_selected():
            sel = tree.selection()
            if not sel:
                return
            si, entry, _write_key = row_meta[sel[0]]
            if entry is None:
                return
            fields = item_entry_fields(entry)
            ii = fields.get("II")
            if ii is None:
                return
            if not messagebox.askyesno(
                    "Clear slot",
                    "Clear %s? (sets item hash to 0)"
                    % EQUIP_SLOT_NAMES[si], parent=self):
                return
            ok = self.app.commit_bson_edit(self.e, self.doc, self.kind,
                                           ii, 0)
            if ok:
                self.reload()


        ttk.Button(btns, text="Change item…",
                   command=change_selected).pack(side="left")
        ttk.Button(btns, text="Clear slot",
                   command=clear_selected).pack(side="left", padx=6)
        tree.bind("<Double-1>", lambda _e: change_selected())

        # Detail panel: parsed defence / affixes from the description field
        detail = ttk.LabelFrame(parent, text="Selected item")
        detail.pack(fill="x", padx=8, pady=(0, 8))
        detail_txt = tk.Text(detail, height=7, wrap="word",
                             font=("TkDefaultFont", 9))
        detail_txt.pack(fill="x", padx=6, pady=4)
        detail_txt.configure(state="disabled")

        def on_select(_evt=None):
            sel = tree.selection()
            detail_txt.configure(state="normal")
            detail_txt.delete("1.0", "end")
            if not sel:
                detail_txt.configure(state="disabled")
                return
            si, entry, _write_key = row_meta[sel[0]]
            if entry is None:
                detail_txt.insert("end", "(empty slot)")
                detail_txt.configure(state="disabled")
                return
            fields = item_entry_fields(entry)
            ii = fields.get("II")
            crc = ii["value"] if ii else 0
            rec, stats = item_stats_for_crc(crc)
            if not rec:
                detail_txt.insert("end", "No item-table entry for this item.")
            else:
                lines = [rec.get("name") or "(unnamed)",
                         "Category: %s" % (rec.get("category") or "—")]
                if stats["defence"] is not None:
                    lines.append("Defence: %d" % stats["defence"])
                elif "Defence" in (stats["raw"] or ""):
                    lines.append("Defence: —")
                if stats["affixes"]:
                    lines.append("Affixes:")
                    for a in stats["affixes"]:
                        lines.append("  %s" % a)
                if stats["notes"]:
                    lines.append("Notes: %s" % "; ".join(stats["notes"][:4]))
                detail_txt.insert("end", "\n".join(lines))
            detail_txt.configure(state="disabled")

        tree.bind("<<TreeviewSelect>>", on_select)

    def _set_equip_slot(self, array_key, si, entry, rec):
        crc = _as_u32((rec or {}).get("hash"))
        if crc is None:
            messagebox.showerror(
                "No hash",
                "That item has no hash in the item table and cannot be "
                "written into the save.",
                parent=self)
            return
        if entry is not None:
            fields = item_entry_fields(entry)
            ii = fields.get("II")
            if ii is None:
                messagebox.showerror("No II", "Slot has no item field.",
                                     parent=self)
                return
            edits = [(ii, crc)]
            if fields.get("SC") is not None:
                # keep stack at 1 for equip
                edits.append((fields["SC"], 1))
            ok = self.app.commit_bson_edits(self.e, self.doc, self.kind,
                                            edits, verify_label="equip")
            if ok:
                self.reload()
            return
        # Empty slot – insert a plain item document into the array
        arr = find_named_array(self.inv_root, array_key) or \
            find_named_array(self.nodes, array_key)
        if arr is None:
            messagebox.showerror(
                "Missing array",
                "This character has no %s array to insert into."
                % array_key, parent=self)
            return
        try:
            buf = bytearray(self.doc)
            bson_insert_plain_item(buf, arr, si, crc, 1)
            fresh_nodes, total = bson_parse(buf)
            if total != len(buf):
                raise ValueError("length mismatch after insert")
            payload = wrap(bytes(buf), self.kind, self.app.cctx)
            target_id = self.e["id"]

            def verify_fn(check):
                for ee, ddoc, kkind, eerr in iter_docs(check, self.app.dctx):
                    if ee["id"] != target_id or ddoc is None:
                        continue
                    try:
                        fresh = bson_parse(ddoc)[0]
                    except Exception:
                        return False
                    fresh_arr = find_named_array(fresh, array_key)
                    entry_now = inventory_slot_map(fresh_arr).get(si)
                    if entry_now is None:
                        return False
                    ii = item_entry_fields(entry_now).get("II")
                    return (ii is not None
                            and (int(ii["value"]) & 0xFFFFFFFF) == crc)
                return False

            ok, _check = self.app.write_container(
                target_id, payload, verify_fn=verify_fn,
                verify_label="equip insert")
            if ok:
                self.app.log("Inserted %s into %s slot %d. Verified: "
                             "CRCs valid, item reads back."
                             % (rec.get("name"), array_key, si))
                self.reload()
        except Exception as ex:
            messagebox.showerror("Insert failed", str(ex), parent=self)

    # -- Pets / Mounts -------------------------------------------------

    def _build_pets_tab(self):
        parent = self._tab_pets
        arr = find_named_array(self.inv_root, "PET") or \
            find_named_array(self.nodes, "PET")
        slots = inventory_slot_map(arr)

        ttk.Label(parent, text="Pets and Mounts live in the PET array. "
                               "The live game hides this panel, but the "
                               "data still round-trips.").pack(
            anchor="w", padx=8, pady=6)

        cols = ("slot", "name", "kind", "stack")
        tree = ttk.Treeview(parent, columns=cols, show="headings",
                            height=10, selectmode="browse")
        for col, text, w in (("slot", "Slot", 60),
                             ("name", "Pet / Mount", 360),
                             ("kind", "Category", 140),
                             ("stack", "Stack", 70)):
            tree.heading(col, text=text)
            tree.column(col, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=8, pady=4)

        row_meta = {}
        if slots:
            for si in sorted(slots):
                entry = slots[si]
                fields = item_entry_fields(entry)
                ii = fields.get("II")
                sc = fields.get("SC")
                crc = ii["value"] if ii else 0
                rec = item_record_for_crc(crc)
                label = item_name_for_crc(crc) or "(unknown)"
                cat = (rec or {}).get("category") or ""
                stack = sc["value"] if sc is not None else 1
                iid = tree.insert("", "end", values=(
                    si + 1, label, cat, stack))
                row_meta[iid] = (si, entry)
        else:
            tree.insert("", "end", values=("", "(no pets)", "", ""))

        btns = ttk.Frame(parent)
        btns.pack(fill="x", padx=8, pady=6)

        def change_selected():
            sel = tree.selection()
            if not sel or sel[0] not in row_meta:
                # add into next free slot
                used = set(slots)
                si = 0
                while si in used:
                    si += 1
                self._item_picker(
                    title="Add pet / mount",
                    categories=PET_PLACE_CATEGORIES,
                    on_pick=lambda rec: self._set_equip_slot(
                        "PET", si, None, rec))
                return
            si, entry = row_meta[sel[0]]
            self._item_picker(
                title="Change pet / mount",
                categories=PET_PLACE_CATEGORIES,
                on_pick=lambda rec: self._set_equip_slot(
                    "PET", si, entry, rec))

        def add_new():
            used = set(slots)
            si = 0
            while si in used:
                si += 1
            self._item_picker(
                title="Add pet / mount (slot %d)" % si,
                categories=PET_PLACE_CATEGORIES,
                on_pick=lambda rec: self._set_equip_slot(
                    "PET", si, None, rec))

        ttk.Button(btns, text="Change selected…",
                   command=change_selected).pack(side="left")
        ttk.Button(btns, text="Add pet / mount…",
                   command=add_new).pack(side="left", padx=6)
        tree.bind("<Double-1>", lambda _e: change_selected())

    # -- Player stats --------------------------------------------------

    def _build_stats_tab(self):
        parent = self._tab_stats
        # Scrollable body so name/race/class/attrs/health all fit.
        canvas = tk.Canvas(parent, highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        outer = ttk.Frame(canvas)
        outer.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=outer, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        ctrl = find_component(self.nodes, "ServerPlayerControlComponent")
        setup = None
        custom = None
        coins_node = ac_node = None
        if ctrl:
            for ch in ctrl.get("children") or []:
                if ch["key"] == "CharacterSetup":
                    setup = ch
                elif ch["key"] == "C":
                    coins_node = ch
                elif ch["key"] == "AC":
                    ac_node = ch

        level_node = gender_node = race_node = class_node = None
        name_text = ""
        name_field = None  # (off, length) in doc if found
        if setup:
            for ch in setup.get("children") or []:
                if ch["key"] == "level":
                    level_node = ch
                elif ch["key"] == "gender":
                    gender_node = ch
                elif ch["key"] == "customization":
                    custom = ch
            # race/class live under customization
            if custom:
                for ch in custom.get("children") or []:
                    if ch["key"] == "raceCRC":
                        race_node = ch
                    elif ch["key"] == "classCRC":
                        class_node = ch
            # character name is a fixed binary field under setup
            for ch in setup.get("children") or []:
                if ch["key"] == "name" and isinstance(ch.get("value"),
                                                     (bytes, bytearray)):
                    raw = bytes(ch["value"])
                    # null-terminated display string
                    name_text = raw.split(b"\x00", 1)[0].decode(
                        "utf-8", errors="replace")
                    name_field = ch
                    break

        def apply_node(node, label, raw_value, coerce_fn=None):
            if node is None:
                messagebox.showerror("Missing", "%s not on this character."
                                     % label, parent=self)
                return
            try:
                if coerce_fn:
                    new_v = coerce_fn(raw_value)
                else:
                    new_v = FieldEditor._coerce(node, raw_value)
            except Exception as ex:
                messagebox.showerror("Invalid", str(ex), parent=self)
                return
            warn = field_advisory(node, new_v)
            msg = "Set %s to %r?" % (label, new_v)
            if warn:
                msg = "NOTE: %s\n\n%s" % (warn, msg)
            if not messagebox.askyesno("Confirm", msg, parent=self):
                return
            if self.app.commit_bson_edit(self.e, self.doc, self.kind,
                                         node, new_v):
                self.reload()

        def add_entry_row(frame, label, node, width=18):
            row = ttk.Frame(frame)
            row.pack(fill="x", padx=6, pady=2)
            ttk.Label(row, text=label + ":", width=18).pack(side="left")
            var = tk.StringVar(value="" if node is None else str(node["value"]))
            ttk.Entry(row, textvariable=var, width=width).pack(side="left")
            ttk.Button(
                row, text="Apply",
                command=lambda: apply_node(node, label, var.get())
            ).pack(side="left", padx=6)
            return var

        def add_combo_row(frame, label, node, names_by_crc, by_name):
            row = ttk.Frame(frame)
            row.pack(fill="x", padx=6, pady=2)
            ttk.Label(row, text=label + ":", width=18).pack(side="left")
            cur = ""
            if node is not None:
                cur = names_by_crc.get(int(node["value"]) & 0xFFFFFFFF, "")
                if not cur:
                    cur = str(node["value"])
            var = tk.StringVar(value=cur)
            cb = ttk.Combobox(row, textvariable=var, width=16,
                              values=list(by_name.keys()), state="readonly")
            cb.pack(side="left")

            def apply():
                name = var.get()
                if name not in by_name:
                    messagebox.showerror("Unknown", "Pick a listed %s."
                                         % label, parent=self)
                    return
                apply_node(node, label, str(by_name[name]))

            ttk.Button(row, text="Apply", command=apply).pack(
                side="left", padx=6)

        def add_gender_row(frame, node):
            row = ttk.Frame(frame)
            row.pack(fill="x", padx=6, pady=2)
            ttk.Label(row, text="Gender:", width=18).pack(side="left")
            cur = "Male"
            if node is not None:
                cur = GENDER_NAMES.get(int(node["value"]), "Male")
            var = tk.StringVar(value=cur)
            cb = ttk.Combobox(row, textvariable=var, width=16,
                              values=["Male", "Female"], state="readonly")
            cb.pack(side="left")

            def apply():
                apply_node(node, "Gender", str(GENDER_BY_NAME[var.get()]))

            ttk.Button(row, text="Apply", command=apply).pack(
                side="left", padx=6)
            ttk.Label(row, text="(may be driven by model; visual can ignore)",
                      foreground="#666").pack(side="left", padx=4)

        # ---- Character identity ----
        info = ttk.LabelFrame(outer, text="Character")
        info.pack(fill="x", padx=8, pady=(8, 4))

        # Name
        nrow = ttk.Frame(info)
        nrow.pack(fill="x", padx=6, pady=2)
        ttk.Label(nrow, text="Name:", width=18).pack(side="left")
        name_var = tk.StringVar(value=name_text)
        ttk.Entry(nrow, textvariable=name_var, width=28).pack(side="left")

        def apply_name():
            new = name_var.get().strip()
            if not new:
                messagebox.showerror("Empty", "Name cannot be empty.",
                                     parent=self)
                return
            if name_field is None:
                messagebox.showerror("Missing", "No name field found.",
                                     parent=self)
                return
            # Prefer the dedicated rename path (pads/truncates the binary).
            fields = find_name_fields(self.doc)
            if not fields:
                messagebox.showerror("Missing", "Could not locate name "
                                     "bytes in the document.", parent=self)
                return
            off, length, _old = fields[0]
            if not messagebox.askyesno(
                    "Rename", "Set name to %r?" % new, parent=self):
                return
            try:
                target_id = self.e["id"]
                buf = bytearray(self.doc)
                set_name(buf, off, length, new)
                payload = wrap(bytes(buf), self.kind, self.app.cctx)

                def verify_fn(check):
                    for ee, ddoc, kkind, eerr in iter_docs(
                            check, self.app.dctx):
                        if ee["id"] != target_id or ddoc is None:
                            continue
                        for _, _, text in find_name_fields(ddoc):
                            if text == new:
                                return True
                    return False

                ok, _check = self.app.write_container(
                    target_id, payload, verify_fn=verify_fn,
                    verify_label="rename")
                if ok:
                    self.app.log("Renamed character to %r. Verified: "
                                 "CRCs valid, new name reads back." % new)
                    self.reload()
            except Exception as ex:
                messagebox.showerror("Rename failed", str(ex), parent=self)

        ttk.Button(nrow, text="Apply", command=apply_name).pack(
            side="left", padx=6)

        add_combo_row(info, "Race", race_node, RACE_NAMES, RACE_BY_NAME)
        add_combo_row(info, "Class", class_node, CLASS_NAMES, CLASS_BY_NAME)
        add_entry_row(info, "Level", level_node)
        add_entry_row(info, "Coins", coins_node)
        add_entry_row(info, "Defender Coins", ac_node)
        add_gender_row(info, gender_node)

        # ---- Attributes + vitals from Impact Component / AV ----
        impact = find_component(self.nodes, "Impact Component")
        av = None
        if impact:
            for ch in impact.get("children") or []:
                if ch["key"] == "AV":
                    av = ch
                    break

        # Index AV children by resolved short name / full name
        av_by_short = {}
        av_arrays = {}  # "health" / "mana" -> array node
        if av and av.get("children"):
            for n in av["children"]:
                if n.get("children") is not None:
                    # nested health/mana arrays — key is hex of Health/Mana
                    label = av_label(n["key"]) if is_hex_key(n["key"]) else None
                    if label and "health" in label.lower():
                        av_arrays["health"] = n
                    elif label and "mana" in label.lower():
                        av_arrays["mana"] = n
                    else:
                        # try attr name for array keys
                        full = attr_names().get(int(n["key"], 16), "") \
                            if is_hex_key(n["key"]) else ""
                        if full.lower() == "health":
                            av_arrays["health"] = n
                        elif full.lower() == "mana":
                            av_arrays["mana"] = n
                    continue
                if is_hex_key(n["key"]):
                    full = attr_names().get(int(n["key"], 16)) or \
                        AV_KNOWN.get(n["key"].lower())
                    if full:
                        short = ATTR_SHORT.get(full, full)
                        av_by_short[short] = n
                        av_by_short[full] = n

        attrs = ttk.LabelFrame(outer, text="Attributes")
        attrs.pack(fill="x", padx=8, pady=4)
        for short in ("CON", "STR", "AGI", "DEX", "WIS", "INT"):
            node = av_by_short.get(short)
            add_entry_row(attrs, short, node, width=12)
        # level + experience from AV if present
        if av_by_short.get("level"):
            add_entry_row(attrs, "AV Level", av_by_short["level"], width=12)
        exp_node = av_by_short.get("experience")
        if exp_node is None and av:
            for n in av.get("children") or []:
                if is_hex_key(n["key"]) and n.get("children") is None:
                    full = attr_names().get(int(n["key"], 16)) or \
                        AV_KNOWN.get(n["key"].lower())
                    if full and "experience" in full.lower():
                        exp_node = n
                        break
        if exp_node is not None:
            add_entry_row(attrs, "Experience", exp_node, width=12)

        def pack_vital_group(title, array_node):
            box = ttk.LabelFrame(outer, text=title)
            box.pack(fill="x", padx=8, pady=4)
            if not array_node or not array_node.get("children"):
                ttk.Label(box, text="(not present)").pack(anchor="w", padx=6)
                return
            for entry in array_node["children"]:
                if not entry.get("children"):
                    continue
                n_node = v_node = None
                for ch in entry["children"]:
                    if ch["key"] == "N":
                        n_node = ch
                    elif ch["key"] == "V":
                        v_node = ch
                if n_node is None or v_node is None:
                    continue
                full = attr_names().get(int(n_node["value"])) or \
                    stat_field_label(n_node["value"]) or \
                    ("0x%08X" % (int(n_node["value"]) & 0xFFFFFFFF))
                add_entry_row(box, str(full), v_node, width=14)

        pack_vital_group("Health", av_arrays.get("health"))
        pack_vital_group("Mana", av_arrays.get("mana"))

        # ---- Talents (class-specific names) ----
        class_name = ""
        if class_node is not None:
            class_name = CLASS_NAMES.get(
                int(class_node["value"]) & 0xFFFFFFFF, "")
        tree_for_class = TALENT_TREES.get(class_name, {})

        talent_comp = find_component(self.nodes, "Talent Line Component")
        tbox = ttk.LabelFrame(
            outer,
            text="Talent line%s" % (" — %s" % class_name if class_name else ""))
        tbox.pack(fill="x", padx=8, pady=4)
        tls = None
        if talent_comp:
            for n in _walk(talent_comp):
                if n.get("key") == "talentLineSelection" and \
                        n.get("children") is not None:
                    tls = n
                    break
        if not tls or not tls.get("children"):
            ttk.Label(tbox, text="(no talent selections)").pack(
                anchor="w", padx=6, pady=4)
        else:
            if not tree_for_class:
                ttk.Label(
                    tbox,
                    text="Set Class above to load named talents "
                         "(Ranger / Warrior / Mage / Rogue / Druid).",
                    foreground="#666",
                ).pack(anchor="w", padx=6, pady=2)
            for entry in tls["children"]:
                if not entry.get("children"):
                    continue
                lvl = sel = None
                for ch in entry["children"]:
                    if ch["key"] == "level":
                        lvl = ch
                    elif ch["key"] == "selection":
                        sel = ch
                if lvl is None or sel is None:
                    continue
                level = int(lvl["value"])
                options = tree_for_class.get(level, [])
                row = ttk.Frame(tbox)
                row.pack(fill="x", padx=6, pady=2)
                ttk.Label(row, text="Level %d:" % level,
                          width=12).pack(side="left")
                cur_idx = int(sel["value"])
                if options:
                    cur_name = options[cur_idx] if 0 <= cur_idx < len(options) \
                        else options[0]
                    var = tk.StringVar(value=cur_name)
                    cb = ttk.Combobox(
                        row, textvariable=var, width=36,
                        values=options, state="readonly")
                    cb.pack(side="left")

                    def apply_talent(n=sel, v=var, opts=options, lv=level):
                        name = v.get()
                        if name not in opts:
                            return
                        idx = opts.index(name)
                        apply_node(n, "Talent L%d" % lv, str(idx))

                    ttk.Button(row, text="Apply",
                               command=apply_talent).pack(
                        side="left", padx=6)
                else:
                    var = tk.StringVar(value=str(cur_idx))
                    ttk.Entry(row, textvariable=var, width=8).pack(side="left")
                    ttk.Button(
                        row, text="Apply",
                        command=lambda n=sel, v=var: apply_node(
                            n, "Talent selection", v.get())
                    ).pack(side="left", padx=6)

        # Catch-all: any other scalar AV fields not already shown
        other = ttk.LabelFrame(outer, text="Other stats")
        other.pack(fill="x", padx=8, pady=(4, 12))
        shown = set()
        for short in ("CON", "STR", "AGI", "DEX", "WIS", "INT", "level",
                      "attribute points"):
            n = av_by_short.get(short)
            if n:
                shown.add(id(n))
        if exp_node is not None:
            shown.add(id(exp_node))
        if av and av.get("children"):
            for n in display_order(av["children"]):
                if n.get("children") is not None:
                    continue
                if id(n) in shown:
                    continue
                label = av_label(n["key"]) if is_hex_key(n["key"]) else n["key"]
                if not label:
                    label = n["key"]
                # skip pure cooldown noise at the bottom of the list unless useful
                add_entry_row(other, label, n, width=14)
        else:
            ttk.Label(other, text="(no Impact/AV block)").pack(
                anchor="w", padx=6)

    # -- Backpack / Hotbar ---------------------------------------------

    def _build_bags_tab(self):
        parent = self._tab_bags
        self._bags_nb = ttk.Notebook(parent)
        self._bags_nb.pack(fill="both", expand=True, padx=4, pady=4)

        for array_key, title_base, limit in (
                ("IBP", "Backpack", 40),
                ("IAB", "Hotbar", 8)):
            arr = find_named_array(self.inv_root, array_key) or \
                find_named_array(self.nodes, array_key)
            filled = len(inventory_slot_map(arr))
            frame = ttk.Frame(self._bags_nb)
            self._bags_nb.add(frame, text="%s (%d/%d)" % (
                title_base, filled, limit))
            self._build_bag_list(frame, array_key, slot_limit=limit)

    def _remove_bag_entries(self, array_key, entries):
        """Delete one or more inventory entries from a bag array."""
        if not entries:
            return False
        arr = find_named_array(self.inv_root, array_key) or \
            find_named_array(self.nodes, array_key)
        if arr is None:
            messagebox.showerror("Missing", "No %s array." % array_key,
                                 parent=self)
            return False
        try:
            slots_before = inventory_slot_map(arr)
            expected_filled = len(slots_before) - len(entries)

            buf = bytearray(self.doc)
            # Remove highest estart first so earlier offsets stay valid.
            ordered = sorted(
                entries,
                key=lambda n: n.get("estart", 0),
                reverse=True)
            for child in ordered:
                bson_remove_element(buf, arr, child)
            fresh_nodes, total = bson_parse(buf)
            if total != len(buf):
                raise ValueError("length mismatch after remove")
            payload = wrap(bytes(buf), self.kind, self.app.cctx)
            target_id = self.e["id"]

            def verify_fn(check):
                for ee, ddoc, kkind, eerr in iter_docs(check, self.app.dctx):
                    if ee["id"] != target_id or ddoc is None:
                        continue
                    try:
                        fresh = bson_parse(ddoc)[0]
                    except Exception:
                        return False
                    fresh_arr = find_named_array(fresh, array_key)
                    return len(inventory_slot_map(fresh_arr)) == \
                        expected_filled
                return False

            ok, _check = self.app.write_container(
                target_id, payload, verify_fn=verify_fn,
                verify_label="bag delete")
            if ok:
                self.app.log("Removed %d item(s) from %s. Verified: CRCs "
                             "valid, slot count matches."
                             % (len(entries), array_key))
                self.reload()
            return ok
        except Exception as ex:
            messagebox.showerror("Delete failed", str(ex), parent=self)
            return False

    def _build_bag_list(self, parent, array_key, slot_limit=40):
        arr = find_named_array(self.inv_root, array_key) or \
            find_named_array(self.nodes, array_key)
        slots = inventory_slot_map(arr)
        filled = len(slots)

        hdr = ttk.Frame(parent)
        hdr.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Label(
            hdr,
            text="%d item%s · capacity %d  ·  Ctrl+click multi-select · "
                 "Del removes"
                 % (filled, "" if filled == 1 else "s", slot_limit),
        ).pack(side="left")

        cols = ("slot", "name", "category", "stack", "cap")
        tree = ttk.Treeview(parent, columns=cols, show="headings",
                            height=16, selectmode="extended")
        for col, text, w in (("slot", "Slot", 50),
                             ("name", "Item", 320),
                             ("category", "Category", 140),
                             ("stack", "Stack", 70),
                             ("cap", "Max", 60)):
            tree.heading(col, text=text)
            tree.column(col, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=6, pady=4)

        row_meta = {}
        # Only list occupied slots — empties are not items.
        for si in sorted(slots):
            if si >= slot_limit:
                continue
            entry = slots[si]
            fields = item_entry_fields(entry)
            ii = fields.get("II")
            sc = fields.get("SC")
            crc = _as_u32(ii.get("value")) if ii else None
            if sc is not None:
                stack = sc["value"]
            else:
                stack = 1
            cap = item_max_stack(crc) if crc is not None else ""
            rec = item_record_for_crc(crc) if crc is not None else None
            label = (item_name_for_crc(crc) if crc is not None else None) \
                or "(unknown item)"
            cat = (rec or {}).get("category") or ""
            iid = tree.insert("", "end", values=(
                si + 1, label, cat, stack, cap))
            row_meta[iid] = (si, entry, fields)

        btns = ttk.Frame(parent)
        btns.pack(fill="x", padx=6, pady=4)

        def change_item():
            sel = tree.selection()
            if not sel or sel[0] not in row_meta:
                return
            si, entry, fields = row_meta[sel[0]]
            ii = fields.get("II")
            bind = _as_u32(ii.get("value")) if ii else None
            self._item_picker(
                title="Change item in slot %d" % (si + 1),
                categories=None,
                on_pick=lambda rec: self._set_bag_item(
                    array_key, si, entry, fields, rec))

        def edit_stack():
            sel = tree.selection()
            if not sel or sel[0] not in row_meta:
                return
            si, entry, fields = row_meta[sel[0]]
            sc = fields.get("SC")
            if sc is None:
                messagebox.showinfo("No stack",
                                    "This slot holds a unique item, "
                                    "not a stack count.", parent=self)
                return
            ii = fields.get("II")
            crc = ii["value"] if ii else 0
            cap = item_max_stack(crc)
            dlg = tk.Toplevel(self)
            dlg.title("Stack slot %d (cap %d)" % (si + 1, cap))
            var = tk.StringVar(value=str(sc["value"]))
            ttk.Label(dlg, text="New stack (1–%d recommended):" % cap)\
                .pack(padx=10, pady=(10, 2))
            ttk.Entry(dlg, textvariable=var, width=12).pack(padx=10)

            def apply():
                try:
                    v = int(var.get().strip(), 0)
                except ValueError:
                    messagebox.showerror("Bad value", "Enter an integer.",
                                         parent=dlg)
                    return
                if v < 0 or v > 65535:
                    messagebox.showerror("Out of range",
                                         "Stack is uint16 (0–65535).",
                                         parent=dlg)
                    return
                if v > cap:
                    if not messagebox.askyesno(
                            "Above cap",
                            "Item cap is %d; the game may clamp this. "
                            "Write %d anyway?" % (cap, v), parent=dlg):
                        return
                if self.app.commit_bson_edit(self.e, self.doc, self.kind,
                                             sc, v):
                    dlg.destroy()
                    self.reload()

            ttk.Button(dlg, text="Apply", command=apply).pack(pady=8)

        def delete_selected(_evt=None):
            sel = [s for s in tree.selection() if s in row_meta]
            if not sel:
                return
            entries = [row_meta[s][1] for s in sel if row_meta[s][1] is not None]
            if not entries:
                return
            if not messagebox.askyesno(
                    "Delete items",
                    "Remove %d item(s) from the inventory?\n"
                    "This deletes the slot entry (not just zero the stack)."
                    % len(entries), parent=self):
                return
            self._remove_bag_entries(array_key, entries)

        def select_all(_evt=None):
            tree.selection_set(tree.get_children())
            return "break"

        def add_item():
            used = set(slots)
            si = 0
            while si in used and si < slot_limit:
                si += 1
            if si >= slot_limit:
                messagebox.showinfo(
                    "Full",
                    "All %d slots are full." % slot_limit, parent=self)
                return
            self._item_picker(
                title="Add item to slot %d" % (si + 1),
                categories=None,
                on_pick=lambda rec: self._set_equip_slot(
                    array_key, si, None, rec))


        ttk.Button(btns, text="Change item…",
                   command=change_item).pack(side="left")
        ttk.Button(btns, text="Edit stack…",
                   command=edit_stack).pack(side="left", padx=6)
        ttk.Button(btns, text="Delete selected",
                   command=delete_selected).pack(side="left", padx=6)
        ttk.Button(btns, text="Add into empty slot…",
                   command=add_item).pack(side="left", padx=6)
        tree.bind("<Double-1>", lambda _e: change_item())
        tree.bind("<Delete>", delete_selected)
        tree.bind("<BackSpace>", delete_selected)
        tree.bind("<Control-a>", select_all)
        tree.bind("<Control-A>", select_all)

    def _set_bag_item(self, array_key, si, entry, fields, rec):
        crc = _as_u32((rec or {}).get("hash"))
        if crc is None:
            messagebox.showerror(
                "No hash",
                "That item has no hash in the item table and cannot be "
                "written into the save.",
                parent=self)
            return
        ii = fields.get("II")
        if ii is None:
            return
        edits = [(ii, crc)]
        sc = fields.get("SC")
        if sc is not None and sc["value"] == 0:
            # leave stack, or set to 1 if zero
            edits.append((sc, 1))
        ok = self.app.commit_bson_edits(self.e, self.doc, self.kind, edits,
                                        verify_label="set bag item")
        if ok:
            self.reload()

    # -- Recipes -------------------------------------------------------

    def _build_recipes_tab(self):
        parent = self._tab_recipes
        ttk.Label(
            parent,
            text="Known recipe IDs on this character. IDs use the game's "
                 "internal recipe CRC space (not always the same as item "
                 "table hashes). Matching Recipes-category items are shown "
                 "when the ID lines up; otherwise the CRC is listed so you "
                 "can still see what is unlocked."
        ).pack(anchor="w", padx=8, pady=6)

        recipe_bin = None
        for n in _walk(self.nodes):
            if n["key"] == "knownRecipeIds" and n.get("value") is not None:
                recipe_bin = n
                break

        cols = ("idx", "name", "category")
        tree = ttk.Treeview(parent, columns=cols, show="headings",
                            height=16, selectmode="browse")
        for col, text, w in (("idx", "#", 40),
                             ("name", "Recipe", 420),
                             ("category", "Category", 140)):
            tree.heading(col, text=text)
            tree.column(col, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=8, pady=4)

        if recipe_bin is None:
            tree.insert("", "end", values=("", "(no knownRecipeIds field)",
                                           ""))
            return

        ids = parse_recipe_ids(recipe_bin["value"])
        if not ids:
            tree.insert("", "end", values=("", "(no recipes unlocked)", ""))
        for i, crc in enumerate(ids):
            rec = item_record_for_crc(crc)
            if rec:
                label = rec.get("name") or "(unnamed)"
                cat = rec.get("category") or ""
            else:
                # Internal recipe CRC — not in the item hash table
                label = "Recipe 0x%08X" % (crc & 0xFFFFFFFF)
                cat = "internal id"
            tree.insert("", "end", values=(i + 1, label, cat))

        ttk.Label(
            parent,
            text="%d unlocked recipe id(s). Browse every craftable recipe "
                 "below (item table, Recipes category)."
                 % len(ids),
            foreground="#555",
        ).pack(anchor="w", padx=8, pady=(4, 2))

        # Full recipe catalogue from item table for reference / search
        cat_frame = ttk.LabelFrame(parent, text="All recipes (item table)")
        cat_frame.pack(fill="both", expand=True, padx=8, pady=6)
        qvar = tk.StringVar()
        ttk.Entry(cat_frame, textvariable=qvar).pack(fill="x", padx=6, pady=4)
        cat_tree = ttk.Treeview(cat_frame, columns=("name", "desc"),
                                show="headings", height=8)
        cat_tree.heading("name", text="Name")
        cat_tree.heading("desc", text="Description")
        cat_tree.column("name", width=260)
        cat_tree.column("desc", width=360)
        cat_tree.pack(fill="both", expand=True, padx=6, pady=4)

        def refresh_cat(*_a):
            cat_tree.delete(*cat_tree.get_children())
            q = (qvar.get() or "").strip().lower()
            for rec in item_search(q, categories=RECIPE_CATEGORIES, limit=400):
                cat_tree.insert(
                    "", "end",
                    values=(rec.get("name") or "",
                            (rec.get("description") or "")[:80]))

        qvar.trace_add("write", refresh_cat)
        refresh_cat()

    # -- Quests --------------------------------------------------------

    def _build_quests_tab(self):
        parent = self._tab_quests
        ttk.Label(
            parent,
            text="Quests from Quest Component (QB). Parsed from the "
                 "nested SNPY → BSON blob. Names appear when the blob "
                 "stores readable strings; otherwise the quest id is shown.",
        ).pack(anchor="w", padx=8, pady=6)

        qb_node = None
        for n in _walk(self.nodes):
            if n["key"] == "QB" and isinstance(n.get("value"),
                                               (bytes, bytearray)):
                qb_node = n
                break

        cols = ("state", "title", "location", "qid")
        tree = ttk.Treeview(parent, columns=cols, show="headings",
                            height=14, selectmode="browse")
        for col, text, w in (("state", "State", 100),
                             ("title", "Quest / detail", 340),
                             ("location", "Location", 140),
                             ("qid", "ID", 100)):
            tree.heading(col, text=text)
            tree.column(col, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=8, pady=4)

        detail = tk.Text(parent, height=8, wrap="word",
                         font=("Courier New", 9))
        detail.pack(fill="x", padx=8, pady=(0, 6))

        if qb_node is None:
            tree.insert("", "end", values=("", "(no QB field)", "", ""))
            detail.insert("end", "No Quest Component / QB on this character.\n")
            detail.configure(state="disabled")
            return

        ok, info = decode_quest_blob(qb_node["value"])
        if not ok:
            tree.insert("", "end", values=("", "(decompress failed)", "", ""))
            detail.insert("end", "Error: %s\n" % info.get("error"))
            detail.configure(state="disabled")
            return

        quests = info.get("quests") or []
        if not quests:
            # Fall back to listing interesting strings from the blob
            strings = [s for s in (info.get("strings") or [])
                       if s not in ("Character", "QuestState", "Finalized",
                                    "Active", "CTX", "QA")]
            if strings:
                for s in strings:
                    tree.insert("", "end", values=("", s, "", ""))
            else:
                tree.insert("", "end", values=(
                    "", "(no quest entries parsed)", "", ""))
        else:
            for q in quests:
                state = q.get("state") or ""
                title = ""
                strs = q.get("strings") or []
                # Prefer a string that looks like a title over technical keys
                for s in strs:
                    if s not in ("QuestState", "Finalized", "Active",
                                 "Character", "CTX"):
                        title = s
                        break
                if not title and q.get("qid") is not None:
                    title = "Quest 0x%08X" % q["qid"]
                loc = q.get("location") or ""
                qid = ("0x%08X" % q["qid"]) if q.get("qid") is not None else ""
                tree.insert("", "end", values=(state, title, loc, qid))

        # Summary footer
        detail.insert(
            "end",
            "Decompressed %d bytes · %d quest record(s) · bson=%s\n"
            % (info.get("decompressed_len", 0),
               len(quests),
               info.get("bson_ok")))
        if info.get("strings"):
            detail.insert("end", "Strings in blob: %s\n"
                          % ", ".join(info["strings"][:20]))
        detail.configure(state="disabled")

    # -- shared item picker --------------------------------------------

    def _item_picker(self, title, categories, on_pick, bind_crc=None):
        """Searchable item list. Hashes are read-only; only rows with a hash are shown.

        bind_crc is ignored (item-table hash binding was removed — hashes are authoritative).
        """
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.geometry("520x420")
        ttk.Label(
            dlg,
            text="Select an item (hash is fixed in the item table). "
                 "Double-click or Use selected.",
        ).pack(anchor="w", padx=8, pady=6)
        qvar = tk.StringVar()
        ttk.Entry(dlg, textvariable=qvar).pack(fill="x", padx=8, pady=4)
        lb = tk.Listbox(dlg, font=("Courier New", 9))
        lb.pack(fill="both", expand=True, padx=8, pady=4)
        rows = []

        def refresh(*_a):
            lb.delete(0, "end")
            del rows[:]
            q = (qvar.get() or "").strip().lower()
            n = 0
            cat_filter = None
            if categories and categories != ("*",):
                try:
                    cat_filter = set(categories)
                except TypeError:
                    cat_filter = None
            for rec in item_table():
                if not isinstance(rec, dict):
                    continue
                h = _as_u32(rec.get("hash"))
                if h is None:
                    continue
                name = str(rec.get("name") or "")
                cat = str(rec.get("category") or "")
                if cat_filter is not None and cat not in cat_filter:
                    continue
                blob = ("%s %s %s %08x" % (name, cat, h, h)).lower()
                if q and q not in blob:
                    continue
                rows.append(rec)
                lb.insert("end", "%-36s  %-14s  0x%08X" % (
                    name[:36], cat[:14], h))
                n += 1
                if n >= 400:
                    break

        def pick(_evt=None):
            sel = lb.curselection()
            if not sel:
                return
            rec = rows[sel[0]]
            h = _as_u32(rec.get("hash"))
            if h is None:
                messagebox.showerror(
                    "No hash",
                    "This table row has no hash.",
                    parent=dlg)
                return
            dlg.destroy()
            try:
                on_pick(rec)
            except Exception as ex:
                messagebox.showerror("Place failed", str(ex), parent=self)

        qvar.trace_add("write", refresh)
        lb.bind("<Double-1>", pick)
        ttk.Button(dlg, text="Use selected", command=pick).pack(pady=8)
        refresh()




def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
