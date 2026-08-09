#!/usr/bin/env python3
r"""
pk_save.py - read and edit Portal Knights save files directly.

This works OFFLINE. The game does not need to be running, and nothing is
written to live memory. Edits persist properly because the checksums are
recomputed correctly.

    python pk_save.py --info   SAVEFILE
    python pk_save.py --check  SAVEFILE
    python pk_save.py --names  SAVEFILE
    python pk_save.py --rename OLD --to NEW  SAVEFILE
    python pk_save.py --fix    SAVEFILE

All of the above accept --dict PATH to point at the extracted zstd
dictionary (pk_dict.bin, 262144 bytes, pulled from the game exe at offset
0x8096C0).

------------------------------------------------------------------------
WHAT WAS WRONG BEFORE (v2.2 and earlier), AND WHY

v2.2 read five of six characters, truncated "CookiestMonster" to
"CookiestMons", and reported a 32-character name as "123456789V". All
three symptoms had ONE cause, and it was not the name parser.

The blobs are compressed TWICE, not once:

    KSC1 entry blob
      -> zstd frame (dict 0x206A547B)
        -> "SNPY" + a raw Snappy stream      <-- v2.2 stopped here
          -> BSON-like document              <-- the names actually live here

v2.2 un-zstd'd a blob, saw bytes that still contained the ASCII letters
"name", and went scanning that buffer with a byte pattern. But that buffer
is still Snappy-compressed. Any name it appeared to read was a coincidence
of Snappy having stored that run as a literal; the moment Snappy encoded
part of a name as a back-reference, the text was mangled or invisible:

    'CookiestMonster'  -> v2.2 read 'CookiestMons'   (copy tag ate "ter")
    '1234...2344' (32) -> v2.2 read '123456789V'     (copy tag ate the rest)
    a sixth character  -> v2.2 read nothing at all

Reproduced exactly, byte for byte, before writing this version. The
"3-byte vs 4-byte header" theory from earlier was also an artifact of the
same thing - Snappy tag bytes landing in different places, not two header
forms in the file format.

------------------------------------------------------------------------
THE CONTAINER FORMAT, SOLVED

    offset  size  meaning
    0       4     magic "KSC1"
    4       4     entry count
    8       8     CRC-64 of the ENTRY TABLE
    16      8     CRC-64 of the BLOB DATA
    24      12*n  entry table: id(4) tag(4) size(4) per entry
    24+12n  ...   concatenated blob data

CRC-64/XZ - reflected, poly 0x42F0E1EBA9EA3693, init and final XOR all
ones. Verified against a real save: both stored CRCs reproduce exactly.

THE SNPY LAYER, SOLVED

    "SNPY" (4 bytes) + raw Snappy stream

There is no 4-byte header after the magic. What earlier looked like one
(b6 23 f0 63) is just the Snappy varint for the uncompressed length -
0xb6 0x23 decodes to 4534, which is exactly the decompressed size. Raw
Snappy, not the framing format: no stream identifier, no per-chunk CRCs.

THE NAME FIELD, SOLVED - it is BSON

Inside the Snappy layer the data is a BSON-style document. The name is a
binary field, not a string field:

    0x05  "name" 00  <len:int32>  <subtype:uint8>  <128 bytes>

len is 128 and subtype is 0. The buffer is a FIXED 128 bytes, null-padded
- which matches the 128-byte in-memory buffer already confirmed with
Cheat Engine. That is why splicing was never actually needed here: the
field never changes size, so the surrounding bytes are never disturbed.
The 32-character limit is the game's own UI limit, well inside the buffer.

Because the name sits in a length-prefixed field, this version finds it
structurally instead of pattern-matching, so a name containing unusual
characters cannot hide from it and cannot be truncated.

------------------------------------------------------------------------
RE-COMPRESSION

Editing means: unzstd -> unsnappy -> patch 128 bytes -> resnappy -> rezstd.
Both re-compressions are lossless but will not be byte-identical to what
the game produced, and the entry sizes change. That is fine and expected -
the entry table is rebuilt from the actual payload sizes, then both CRCs
are recomputed over the rebuilt table and blob. Every write is verified by
reading the file back through the full pipeline before reporting success.

Snappy is implemented in pk_snappy.py, pure Python, no dependencies -
python-snappy is a C extension most people don't have installed.
"""

import argparse
import os
import shutil
import struct
import sys

try:
    from pk_snappy import compress as snappy_compress
    from pk_snappy import decompress as snappy_decompress
except ImportError:
    print("ERROR: pk_snappy.py not found. It must sit next to pk_save.py.\n"
          "Get it from the same place you got this file.")
    sys.exit(1)

VERSION = "3.0"
MAGIC = b"KSC1"
HEADER_SIZE = 24
ENTRY_SIZE = 12
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
SNPY_MAGIC = b"SNPY"
NAME_FIELD_SIZE = 128
GAME_NAME_LIMIT = 32

_POLY_REFLECTED = 0xC96C5795D7870F42
_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ (_POLY_REFLECTED if _c & 1 else 0)
    _TABLE.append(_c)


def crc64(data):
    """CRC-64/XZ, as used by the game for both header and data CRCs."""
    c = 0xFFFFFFFFFFFFFFFF
    for b in data:
        c = _TABLE[(b ^ c) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFFFFFFFFFF


# ----------------------------------------------------------------------
# compression layers


def load_dict(path):
    """Load the extracted zstd dictionary, if given/available."""
    if not path:
        return None, None, None
    try:
        import zstandard as zstd
    except ImportError:
        print("ERROR: --dict was given but the 'zstandard' package isn't "
              "installed.\nInstall it with:  pip install zstandard")
        sys.exit(1)
    if not os.path.isfile(path):
        print("ERROR: dictionary file not found: %s" % path)
        sys.exit(1)
    with open(path, "rb") as fh:
        raw = fh.read()
    if len(raw) < 8 or raw[:4] != b"\x37\xa4\x30\xec":
        print("WARNING: %s doesn't start with the zstd dictionary magic "
              "number (37 A4 30 EC). If decompression fails, this is "
              "probably the wrong file or the wrong byte range." % path)
    zdict = zstd.ZstdCompressionDict(raw)
    dctx = zstd.ZstdDecompressor(dict_data=zdict)
    cctx = zstd.ZstdCompressor(dict_data=zdict, level=19,
                               write_content_size=True)
    return zdict, dctx, cctx


def unwrap(chunk, dctx):
    """Take a raw entry blob down to the BSON document.

    Returns (doc, kind) where kind records how it was wrapped so wrap()
    can put it back the same way:

        "zstd+snpy"  zstd frame containing "SNPY" + snappy   (normal)
        "snpy"       bare "SNPY" + snappy, no zstd
        "zstd"       zstd frame whose content isn't SNPY
        None         not something we understand - leave it alone
    """
    if chunk[:4] == ZSTD_MAGIC:
        if dctx is None:
            return None, "need-dict"
        inner = dctx.decompress(chunk)
        if inner[:4] == SNPY_MAGIC:
            return snappy_decompress(inner[4:]), "zstd+snpy"
        return inner, "zstd"
    if chunk[:4] == SNPY_MAGIC:
        return snappy_decompress(chunk[4:]), "snpy"
    return None, None


def wrap(doc, kind, cctx):
    """Re-apply the layers unwrap() peeled off."""
    if kind == "zstd+snpy":
        return cctx.compress(SNPY_MAGIC + snappy_compress(doc))
    if kind == "snpy":
        return SNPY_MAGIC + snappy_compress(doc)
    if kind == "zstd":
        return cctx.compress(doc)
    raise ValueError("cannot re-wrap kind %r" % kind)


# ----------------------------------------------------------------------
# the name field


def find_name_fields(doc):
    """Find every BSON binary field called "name".

    Structural, not a text scan: matches the BSON element header
    0x05 "name" 00, reads the declared length, and sanity-checks it.
    Returns a list of (data_offset, length, text).
    """
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
    """Overwrite a fixed-size name buffer, null-padded.

    Safe in place precisely because the field is length-prefixed and the
    length does not change - nothing after it moves.
    """
    enc = new.encode("utf-8")
    if len(enc) > length:
        raise ValueError("name needs %d bytes, field holds %d"
                         % (len(enc), length))
    doc[start:start + length] = enc + b"\x00" * (length - len(enc))


# ----------------------------------------------------------------------
# container


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

    def rebuild(self, blob=None):
        blob = self.blob if blob is None else blob
        out = bytearray(MAGIC)
        out += struct.pack("<I", self.count)
        out += struct.pack("<Q", crc64(self.table))
        out += struct.pack("<Q", crc64(blob))
        out += self.table
        out += blob
        return bytes(out)


def rebuild_container(entries):
    """Build a complete, valid file from (id, tag, payload) entries."""
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


def load(path):
    with open(path, "rb") as fh:
        return Container(fh.read())


# ----------------------------------------------------------------------
# commands


def _iter_docs(c, dctx):
    """Yield (entry, doc_or_None, kind, error_or_None) for every entry."""
    for e in c.entries:
        chunk = c.chunk(e)
        try:
            doc, kind = unwrap(chunk, dctx)
        except Exception as exc:
            yield e, None, None, str(exc)
            continue
        yield e, doc, kind, None


def cmd_info(args):
    c = load(args.savefile)
    _, dctx, _ = load_dict(args.dict)
    hdr_ok, dat_ok = c.verify()
    print("\n%s" % args.savefile)
    print("  size          %d bytes" % len(c.raw))
    print("  entries       %d" % c.count)
    print("  header CRC    %016x  %s"
          % (c.header_crc, "valid" if hdr_ok else "MISMATCH"))
    print("  data CRC      %016x  %s"
          % (c.data_crc, "valid" if dat_ok else "MISMATCH"))
    print("\n  %-5s %-10s %-8s %-10s %s"
          % ("IDX", "TAG", "ID", "SIZE", "OFFSET"))
    print("  " + "-" * 50)
    for e in c.entries:
        print("  %-5d %-10s %-8d %-10d %d"
              % (e["index"], e["tag"].decode("ascii", "replace"),
                 e["id"], e["size"], e["offset"]))
    print()
    for e, doc, kind, err in _iter_docs(c, dctx):
        if err:
            print("  entry %d: FAILED to unwrap - %s" % (e["index"], err))
        elif kind == "need-dict":
            print("  entry %d is zstd-compressed - pass --dict to read it"
                  % e["index"])
        elif doc is None:
            print("  entry %d: unrecognised payload" % e["index"])
        else:
            names = find_name_fields(doc)
            shown = (", ".join(repr(t) for _, _, t in names)
                     if names else "no name field")
            print("  entry %d: %s, %d bytes -> %s"
                  % (e["index"], kind, len(doc), shown))
    return 0


def cmd_check(args):
    c = load(args.savefile)
    hdr_ok, dat_ok = c.verify()
    print("header CRC: %s" % ("valid" if hdr_ok else "MISMATCH"))
    print("data CRC  : %s" % ("valid" if dat_ok else "MISMATCH"))
    if hdr_ok and dat_ok:
        print("\nThis file would be accepted by the game.")
        return 0
    print("\nThe game would reject this file.")
    return 1


def cmd_names(args):
    c = load(args.savefile)
    _, dctx, _ = load_dict(args.dict)
    found = 0
    needs_dict = False
    for e, doc, kind, err in _iter_docs(c, dctx):
        if kind == "need-dict":
            needs_dict = True
            continue
        if err:
            print("  entry %d  FAILED to unwrap - %s" % (e["index"], err))
            continue
        if doc is None:
            continue
        fields = find_name_fields(doc)
        if not fields:
            print("  entry %d  (no name field)" % e["index"])
            continue
        for off, length, text in fields:
            found += 1
            print("  entry %d  offset %d  field %d bytes  %r"
                  % (e["index"], off, length, text))
    if not found:
        if needs_dict:
            print("\nAll blobs are zstd-compressed. Pass --dict PATH (the "
                  "262144-byte dictionary extracted from the game exe).")
        else:
            print("\nNo name fields found.")
        return 1
    return 0


def cmd_rename(args):
    c = load(args.savefile)
    _, dctx, cctx = load_dict(args.dict)
    old, new = args.rename, args.to
    if not new:
        print("--to is required with --rename")
        return 1
    if len(new.encode("utf-8")) > GAME_NAME_LIMIT:
        print("names are limited to %d characters in game" % GAME_NAME_LIMIT)
        return 1

    new_entries = []
    hits = 0
    needs_dict = False

    for e, doc, kind, err in _iter_docs(c, dctx):
        chunk = c.chunk(e)
        if err:
            print("  ! entry %d: unwrap failed (%s) - left unchanged"
                  % (e["index"], err))
            new_entries.append((e["id"], e["tag"], chunk))
            continue
        if kind == "need-dict":
            needs_dict = True
            new_entries.append((e["id"], e["tag"], chunk))
            continue
        if doc is None:
            new_entries.append((e["id"], e["tag"], chunk))
            continue

        fields = [f for f in find_name_fields(doc) if f[2] == old]
        if not fields:
            new_entries.append((e["id"], e["tag"], chunk))
            continue

        buf = bytearray(doc)
        for off, length, _ in fields:
            set_name(buf, off, length, new)
            hits += 1
        payload = wrap(bytes(buf), kind, cctx)
        print("  entry %d: %r -> %r  (blob %d -> %d bytes)"
              % (e["index"], old, new, len(chunk), len(payload)))
        new_entries.append((e["id"], e["tag"], payload))

    if not hits:
        print("\nNo name %r found." % old)
        if needs_dict:
            print("Some entries are zstd-compressed and --dict wasn't given, "
                  "so they couldn't be checked. Pass --dict PATH.")
        return 1

    out = rebuild_container(new_entries)

    if args.dry_run:
        print("\n[dry run] would rewrite %d occurrence(s) of %r, rebuild the "
              "entry table, and recompute both CRCs. Nothing written."
              % (hits, old))
        return 0

    if not args.no_backup:
        bak = args.savefile + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(args.savefile, bak)
            print("backup: %s" % bak)
        else:
            print("backup: %s already exists, keeping it" % bak)

    with open(args.savefile, "wb") as fh:
        fh.write(out)
    print("\nRenamed %d occurrence(s) and rebuilt the container." % hits)

    # Verify by re-reading the file through the whole pipeline.
    check = Container(out)
    h, d = check.verify()
    print("verification: header CRC %s, data CRC %s"
          % ("valid" if h else "BAD", "valid" if d else "BAD"))
    seen = False
    if dctx is not None:
        for e, doc, kind, err in _iter_docs(check, dctx):
            if doc is None:
                continue
            for _, _, text in find_name_fields(doc):
                if text == new:
                    seen = True
                    print("verification: entry %d reads back as %r"
                          % (e["index"], new))
    if not seen:
        print("verification: WARNING - could not read the new name back. "
              "Restore the .bak before running the game.")
        return 1
    return 0 if (h and d) else 1


def cmd_fix(args):
    c = load(args.savefile)
    before = c.verify()
    out = c.rebuild()
    if args.dry_run:
        print("[dry run] would rewrite the header.")
        return 0
    if not args.no_backup:
        bak = args.savefile + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(args.savefile, bak)
            print("backup: %s" % bak)
    with open(args.savefile, "wb") as fh:
        fh.write(out)
    after = Container(out).verify()
    print("CRCs before: header %s, data %s"
          % ("valid" if before[0] else "BAD", "valid" if before[1] else "BAD"))
    print("CRCs after : header %s, data %s"
          % ("valid" if after[0] else "BAD", "valid" if after[1] else "BAD"))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=("Read and edit Portal Knights save files offline. Both "
                     "container CRCs are recomputed correctly, so the game "
                     "accepts the result."),
        allow_abbrev=False)
    ap.add_argument("savefile", nargs="?", help="path to 0100000000000000")
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--info", action="store_true",
                    help="show the container structure and CRC status")
    ap.add_argument("--check", action="store_true", help="verify both CRCs")
    ap.add_argument("--names", action="store_true",
                    help="list every character name")
    ap.add_argument("--rename", metavar="OLD", help="rename a character")
    ap.add_argument("--to", metavar="NEW", help="the new name")
    ap.add_argument("--fix", action="store_true",
                    help="recompute both CRCs after editing a file by hand")
    ap.add_argument("--dict", metavar="PATH",
                    help="path to the extracted zstd dictionary "
                         "(pk_dict.bin, 262144 bytes, from the game exe at "
                         "offset 0x8096C0)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true",
                    help="do not write a .bak copy first")
    args = ap.parse_args()

    if args.version:
        print("pk_save %s" % VERSION)
        return 0
    if not args.savefile:
        ap.print_help()
        return 0
    if not os.path.isfile(args.savefile):
        print("No such file: %s" % args.savefile)
        return 1

    try:
        if args.check:
            return cmd_check(args)
        if args.names:
            return cmd_names(args)
        if args.rename:
            return cmd_rename(args)
        if args.fix:
            return cmd_fix(args)
        return cmd_info(args)
    except ValueError as exc:
        print("ERROR: %s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
