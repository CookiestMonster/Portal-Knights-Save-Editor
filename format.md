# Portal Knights Save Format Reference

Companion document for `pk_manager.py`. Describes the on-disk save container,
filename encoding, compression stack, custom BSON, world/character structures,
TemplateCRC naming, and editor implementation notes.

Last updated: 2026-08-04

---

## Table of contents

1. [Overview](#1-overview)
2. [Steam paths and file types](#2-steam-paths-and-file-types)
3. [Filename encoding](#3-filename-encoding)
4. [World location codes](#4-world-location-codes)
5. [KSC1 container layout](#5-ksc1-container-layout)
6. [CRC64](#6-crc64)
7. [Compression stack](#7-compression-stack)
8. [Custom BSON](#8-custom-bson)
9. [Character files](#9-character-files)
10. [Universe files](#10-universe-files)
11. [World files and entities](#11-world-files-and-entities)
12. [Inventory classification](#12-inventory-classification)
13. [TemplateCRC and NPC names](#13-templatecrc-and-npc-names)
14. [Item table](#14-item-table)
15. [Asset research (core_game)](#15-asset-research-core_game)
16. [pk_manager write safety](#16-pk_manager-write-safety)
17. [Feature roadmap](#17-feature-roadmap)
18. [Related files](#18-related-files)
19. [Warnings](#19-warnings)

---

## 1. Overview

Portal Knights (Steam AppID **374040**) stores cloud saves as binary containers
with magic `KSC1`. Each file is a table of tagged entries; entry payloads are
typically compressed (zstd and/or Snappy) and hold one or more **custom BSON**
documents.

Pipeline:

```text
Steam remote file
  → KSC1 header + entry table + payloads
    → per entry: [optional zstd] → [optional SNPY/snappy] → BSON document(s)
```

`pk_manager.py` is a single-file GUI that discovers these saves, decompresses
entries, parses BSON, and can patch scalars / inventory with backup + CRC
verification on the main write paths.

---

## 2. Steam paths and file types

Typical Windows cloud path:

```text
C:\Program Files (x86)\Steam\userdata\<SteamID3>\374040\remote\
```

Also check non-default Steam libraries, Guest saves, and local copies.

| Filename prefix (first byte) | Constant | Meaning |
|------------------------------|----------|---------|
| `00` | `FILE_TYPE_OPTIONS` | Options / system blob — **not** characters |
| `01` | `FILE_TYPE_CHAR` | Character container (all 9 slots) |
| `02` | `FILE_TYPE_CHAR_BAK` | Game backup of the character file |
| `03` | `FILE_TYPE_UNIVERSE` | Universe; last hex digit = slot |
| `04` | `FILE_TYPE_WORLD` | World / island |
| `06` | `FILE_TYPE_MISC` | Additional system blob |

32-hex filenames are used for **Steam community / shared** worlds and universes.
They do not follow local slot semantics; treat carefully.

---

## 3. Filename encoding

### Character file

```text
0100000000000000
```

- Always this name.
- Holds **all 9 character slots** as separate `CHAR` (or equivalent) entries.
- Editing one slot still rewrites the whole file — **backup first**.
- `0200000000000000` is the game’s backup of the same data.

### Universe file

```text
030000000000000N
```

- `N` = universe **slot** (1-based digit in the last position).
- Slot is fixed by the filename; renaming the display name does **not** change the slot.
- Do not overwrite a universe file with another of the same name unless you intend
  to replace the whole universe (worlds can become orphaned or wiped from the UI).

### World file

```text
04 uu … loc
```

- Type `04`.
- Universe id embedded in the hex name (byte/nibble position used by
  `parse_save_filename`).
- Trailing hex digits encode **location code** (see section 4).
- Example: `040000000100040f` → universe 1, location `0x40F` → Isle of Toblis.

### Other

```text
0000000000000000   options / misc
0600000000000000   misc system
```

---

## 4. World location codes

Last 3–5 hex digits of a `04…` file map to island names.
Sourced from the community spreadsheet and `WORLD_LOCATIONS` in `pk_manager.py`:

https://docs.google.com/spreadsheets/d/1tAr_RdffZ8KrWMcAhmt4W4tay5ArjAxdX5kTEnaxYCk

| Code | Name |
|------|------|
| `0x100` | Squire's Knoll |
| `0x101` | Dusty Junction |
| `0x102` | Fort Finch |
| `0x103` | Shrieking Sands |
| `0x104` | Garnet Peaks |
| `0x105` | Autumn Springs |
| `0x106` | Port Of Caul |
| `0x107` | Orson Orchards |
| `0x108` | Callum's Claim |
| `0x109` | Plains Of Passage |
| `0x163` | Worm Pit |
| `0x200` | Landlubber's Leap |
| `0x201` | Brackenburg |
| `0x202` | Hintertown |
| `0x203` | Witchwater |
| `0x204` | Joren's Outpost |
| `0x205` | Angler's Wharf |
| `0x206` | North Point |
| `0x207` | Ghostlight Mire |
| `0x208` | Mosakola Harbor |
| `0x209` | Addermoor |
| `0x20A` | Broadside Bay |
| `0x20B` | Mount Meridian |
| `0x20C` | Mayyan Delta |
| `0x20D` | Deepest Mosakola |
| `0x20E` | Morello Marshes |
| `0x263` | Dragon's Lair |
| `0x300` | The Great Scar |
| `0x301` | Pockmark Plains |
| `0x302` | Facetta |
| `0x303` | Glimmerglen |
| `0x304` | Lunar Landing |
| `0x305` | Sea of Stalks |
| `0x306` | Farpoint |
| `0x307` | Filia's Folly |
| `0x308` | New Caul |
| `0x309` | Pillars of Parun |
| `0x30A` | The Bone Wastes |
| `0x30B` | Starspires |
| `0x30C` | The Motherlode |
| `0x30D` | Old Hintertown |
| `0x30E` | Great Frontier |
| `0x363` | World's End |
| `0x400` | The Gate |
| `0x401` | Portal Knight's Sanctuary |
| `0x407` | Rainbow Island |
| `0x40E` | Field of Balance |
| `0x40F` | Isle of Toblis |
| `0x50B` | Vacant Grassland Island |
| `0x50C` | Vacant Forest Island |
| `0x50D` | Vacant Fairy Forest Island |
| `0x50E` | Vacant Dry Desert Island |
| `0x50F` | Vacant Oasis Desert Island |
| `0x515` | Vacant Coastal Island |
| `0x516` | Vacant Toxic Island |
| `0x517` | Vacant Swamp Island |
| `0x518` | Vacant Polar Island |
| `0x519` | Vacant Tropical Island |
| `0x51F` | Vacant Crystal Island |
| `0x520` | Vacant Volcano Island |
| `0x521` | Vacant Hollow Island |
| `0x522` | Vacant Asteroid Island |
| `0x523` | Vacant Red Planet Island |
| `0xA01` | Ancient Lair |
| `0xA02` | Ancient Glacier |
| `0xA03` | Ancient Refuge |
| `0xA0B` | Tomb Of C'Thiris |
| `0xA0C` | Kolemis Temple |
| `0xC01` | Morteheim Guildhall |
| `0x20100` | Raven's Den |
| `0x20101` | Farran-Enore |
| `0x20102` | Low Rift Haven |
| `0x20103` | Middle Rift Haven |
| `0x20104` | High Rift Haven |
| `0x2020A` | Low Rift |
| `0x2020B` | Middle Rift |
| `0x2020C` | High Rift |
| `0x20401` | Bitter Root Battlefield |
| `0x20402` | Fallentown Square |
| `0x20500` | Vacant Lunar Furfolk Island |
| `0x20501` | Vacant Lunar Elf Island |
| `0x20600` | Bitter Root Battlefield Hard Mode |
| `0x20601` | Fallentown Square Hard Mode |
| `0x20602` | Temple Mines Hard Mode |
| `0x20700` | Stoutheart Landing |

Vacant / creative islands often use `0x50B`–`0x523` range.
DLC rifts use larger codes such as `0x20100`…

---

## 5. KSC1 container layout

Constants:

```text
MAGIC        = b"KSC1"
HEADER_SIZE  = 24
ENTRY_SIZE   = 12
```

### Header (24 bytes)

| Offset | Size | Field |
|-------:|-----:|-------|
| 0 | 4 | Magic `KSC1` |
| 4 | 4 | Entry count (uint32 LE) |
| 8 | 8 | Header CRC64 of the **entry table** |
| 16 | 8 | Data CRC64 of the **payload blob** |

### Entry table

Each entry is 12 bytes:

| Offset | Size | Field |
|-------:|-----:|-------|
| 0 | 4 | Entry id (uint32) |
| 4 | 4 | Tag (4 ASCII bytes, e.g. `CHAR`, `BKCK`) |
| 8 | 4 | Payload size (uint32) |

Payloads are concatenated immediately after the table in table order.

### Rebuild

```text
out = MAGIC + count + crc64(table) + crc64(blob) + table + blob
```

### Common tags

| Tag | Typical role |
|-----|----------------|
| `CHAR` | Character slot document |
| `USHD` | Universe header (`UniverseHeaderData`) |
| `ILHD` | Island header (`IslandHeaderData`, seed, dCRC, …) |
| `ILAS` | Island asset / large entity-related stream |
| `BKCK` | Block / entity chunk (world placed entities) |
| `FLCK` | Fluid or secondary chunk (often numerous) |
| `PKSS` | Session / misc |

Example world (`040000000100040f`): ~1228 entries, mostly `FLCK`, dozens of
`BKCK`, some `ILAS`.

---

## 6. CRC64

Used for KSC1 header and data integrity (not TemplateCRC).

- Algorithm: **CRC-64/XZ** — polynomial (reflected) `0x42F0E1EBA9EA3693`,
  init and final XOR both all-ones (`0xFFFFFFFFFFFFFFFF`). This reproduces
  the stored header and data CRCs byte for byte.
  > An earlier revision of this document listed the polynomial as
  > `0xC96C5795D7870F42`. That value is wrong — it does not reproduce the
  > container's CRCs. `0x42F0E1EBA9EA3693` is correct.
- Table-driven implementation in `pk_manager` (`crc64`).
- `Container.verify()` → `(header_ok, data_ok)`.
- Independently confirmed: this is the same 64-bit filename-hash routine
  documented and test-vectored by the `ndoa/kfc-tools` project for
  Enshrouded — both games run on Keen Games' "kfc" engine and share the
  identical container hash.

**TemplateCRC** is a separate **CRC32-sized** field inside BSON (type `0x14`),
not this CRC64.

---

## 7. Compression stack

Per-entry payload may be:

| Kind | Detection | Decompress |
|------|-----------|------------|
| `zstd+snpy` | zstd frame contains `SNPY`… | zstd → skip `SNPY` → snappy |
| `zstd` | starts with `28 B5 2F FD` | zstd only |
| `snpy` | starts with `SNPY` | snappy |
| `raw` | otherwise | use as-is |

### Zstd

- Magic: `28 B5 2F FD`
- Optional dictionary: **`pk_dict.bin`** (shipped next to the tool or extracted
  from the game install). Dictionary id `0x206A547B`, 262144 bytes, embedded
  in the game exe at offset `0x8096C0`.
- Requires `pip install zstandard`.

### Snappy

- Framed with magic `SNPY` then raw snappy stream.
- `pk_manager` embeds a pure-Python snappy codec (no native dependency).
- Note: some payloads need care around zero-padding when re-wrapping.

### Re-wrap

Edits must re-compress with the **same kind** the entry had when read
(`wrap(doc, kind, cctx)`), then rebuild the KSC1 container.

Confirmed on a real world save: the game does **not** require the
re-encoded bytes to be byte-identical to its own encoder (a no-op re-wrap
that changed a `BKCK` entry from 15,705 to 17,250 bytes loaded fine). Only
the **kind** (zstd vs zstd+snappy vs raw) and a valid resulting document
matter — a smaller/larger or differently-compressed stream is tolerated.

---

## 8. Custom BSON

Documents use a BSON-like encoding with **Portal Knights-specific type sizes**.

### Critical deviation

**Type `0x01` is float32 (4 bytes), not IEEE float64 (8 bytes).**

Treating it as an 8-byte double shifts the stream and produces bogus type bytes
(historically seen as errors like “custom BSON type 0x00…”).

### Type table (as implemented in `bson_parse`)

| Type | Meaning | Value size |
|-----:|---------|------------|
| `0x01` | **float32** (custom) | 4 |
| `0x02` | string (int32 length + data) | 4 + len |
| `0x03` | embedded document | int32 total size |
| `0x04` | array | int32 total size |
| `0x05` | binary | int32 len + subtype + data |
| `0x07` | ObjectId-like | 12 raw bytes |
| `0x08` | boolean | 1 |
| `0x09` | datetime / int64 | 8 |
| `0x0A` | null | 0 |
| `0x10` | int32 | 4 |
| `0x11` | uint64 (timestamp-style) | 8 |
| `0x12` | int64 | 8 |
| `0x13` | **uint64** (e.g. `ItemIndex`, a per-crafted-item instance handle) | 8 |
| `0x14` | **uint32 — dual use**, see below | 4 |
| `0x16` | **uint8** (level, gender, selection) | 1 |
| `0x18` | **uint16** (`SI` slot, `SC` stack count) | 2 |

Unknown types raise rather than guess lengths (wrong length = silent corruption).

### `0x14` is dual-use

The same type stores two unrelated things, and conflating them caused a
real bug in an earlier build of the editor: `Coins`/`Defender Coins` were
shown as read-only CRC-hash fields, when they are plain writable counters.

| plain uint32 counters | CRC-32 name/asset hashes (never invent values) |
|---|---|
| `playtime`, `price`, `slotId`, `C` (gold), `AC` (Defender Coins), `lastPlayedTime` | `dCRC`, `TemplateCRC`, `raceCRC`, `classCRC`, `effectPackageCRC`, `N`, `II`, `type` |

`dCRC` is **not** a content checksum — two different `SUL` (skill-unlock)
documents have been observed sharing the same `dCRC` value, which rules
that theory out.

`II` (the item hash in `IBP`/`IAB`/etc.) is a **uint32 CRC-32**, not a
uint64 — see §9/§14. Do not confuse it with `ItemIndex` (type `0x13`,
uint64), a separate field that only appears in the `PI` sub-document of
equipped items and is an internal per-instance handle, not an icon or
catalog id.

`ItemIndex` values observed so far sit in the range 32768–65535 (high bit
always set) and only appear on ~14% of items — those with durability/stats
— so it is not a general-purpose item id and should not be relied on for
identification.

### Paths and duplicate keys

- Array elements often have an **empty key**.
- Some documents repeat keys (e.g. two `ICS` fields).
- Paths are disambiguated as `key[index]` so slot selection and patching stay stable.

### Name fields

- Pattern scan: binary element key `name` (`\x05name\x00`).
- Character display names are length-limited (**game UI ~32 chars**);
  storage field may be larger (`NAME_FIELD_SIZE` guidance 128) but UTF-8 byte
  length must fit the existing binary field.

### Common inventory field keys

| Key | Role |
|-----|------|
| `IBP` | Inventory backpack / chest slots array |
| `IAB` | Alternate bag / inventory array |
| `IEQ` | Equipment array (slots 0-5 = Helmet/Chest/Arms/Legs/Cape/Ring; see §9) |
| `VEQ` | Vanity equipment (same slot layout as `IEQ`) |
| `PET` | Pet / mount related slots |
| `SI` | Slot index (uint16) |
| `II` | Item hash — **uint32 CRC-32** (type `0x14`), not uint64. Preimage is `crc32(lowercase-hyphenated GUID string)`, confirmed via known pairings; not derivable from the display name or the community spreadsheet's row id |
| `SC` | Stack count (uint16); hard encodable cap is **65535** |
| `PI` | Extra item field when present (durability, `ItemIndex`, etc.) |
| `TemplateCRC` | Entity prefab type (uint32 / type 0x14) |
| `dCRC` | CRC-32 name-hash field (type `0x14`); **not** a content checksum |

### Binary field display pitfall (GUI implementers)

Most binary fields (`name` aside) are not text. Decoding them as UTF-8 for
display produces mojibake that *looks* like save corruption but is purely
a display artifact — the on-disk bytes are fine. Recommended display
mapping instead of raw UTF-8:

| field | show as |
|---|---|
| `name` | text |
| `modelIds` / `textureIds` / `colorIds` | decimal list, 0-255 (255 = none) |
| `modelCRCs` / `textureCRCs` / `colorCRCs` | hex CRC list |
| `QB` | `"SNPY nested blob, N bytes"` — do not attempt to render |
| anything else (`guid`, `GG`, etc.) | raw hex |

**This is not cosmetic — a naive implementation is destructive.** If the
mojibake display string is pre-filled into an editable box and later
re-encoded as UTF-8 on Apply, every `U+FFFD` replacement character
becomes 3 bytes on write, silently growing the field (one real case
turned a 28-byte `modelCRCs` into 42 bytes, corrupting the document). Only
pre-fill the *display* representation above, never a raw UTF-8 decode of
binary bytes, into anything that can be written back.

### Patching

- Scalar patch: `bson_patch` updates value bytes and ancestor document lengths.
- Insert/remove element helpers exist for equipping items and array surgery.
- After any entry change: re-wrap → rebuild KSC1 → verify CRCs.

---

## 9. Character files

- Single file `0100000000000000` contains up to **9** character documents.
- Editor tabs (tool): Armor, Vanity, Pets, Stats, Backpack, Hotbar, Recipes, Quests.
- Recipes: `knownRecipeIds` (and related) — display exists; unlock/lock is a
  natural extension of array insert.
- **Quests: not actually decoded.** `QB` is a **nested `SNPY` blob** inside
  `Quest Component` — it must be decompressed as its own zstd/snappy
  stream, but its internal quest-state layout has not been mapped. Treat
  it as an opaque round-trippable blob, not an editable field, until it's
  decoded.
- Stats / stacks: prefer **batched** multi-field edits (one container rewrite).

### Level cap

Character level is capped at **30** — confirmed both by community
documentation and by the fact that `talentLineSelection` (below) never has
entries past level 30. Writing `level = 99` produces a file that loads
fine, but the game **resets the level on load**, so the edit does not
stick — it is not a corruption risk, just a no-op in practice.

### Equipment slots

`IEQ` (Armor tab) and `VEQ` (Vanity tab) share the same slot indices,
top-to-bottom in the in-game panel:

```text
0  Helmet
1  Chest
2  Arms
3  Legs
4  Cape
5  Ring
```

`IEQ` has been observed with **7** entries — one past the six visible
slots. That 7th slot's purpose is unidentified; nothing writes to it.
`PET` is a separate array the current game build no longer surfaces in the
UI; it still parses and round-trips, but whether the game acts on edits to
it is untested.

### Talents

`Talent Line Component` → `TLSD.talentLineSelection` is an array of
**7** `{level: uint8, selection: uint8}` entries at levels 2, 5, 10, 15,
20, 25, 30. `selection = 255` means not yet chosen (expected for every
entry on a character below that level).

### Attributes (`AV` block)

`Impact Component` → `AV` is a stat block keyed by **hashed attribute
path**, not a fixed schema. The hash convention is confirmed:

```text
hash = crc32( lowercase( "Parent.Child" ) )
```

Verified against 217/217 real name→hash pairs dumped from the game.
Examples:

```text
crc32(lower("Health"))                  = 0xCEDA2313
crc32(lower("Health.Max"))              = 0x7C323E60
crc32(lower("Health.Max.Adder"))        = 0x6401BFE1
crc32(lower("PlayerIncreasedStrength")) = 0x901AAAEA
crc32(lower("Durability"))              = 0xC764ED49
```

The six core attribute keys, confirmed against the in-game character
sheet (an earlier position-based guess had all six wrong except `level` —
**don't** infer a hashed key's meaning from its position in the document):

| key | attribute |
|---|---|
| `a1ccc259` | CON |
| `901aaaea` | STR (`PlayerIncreasedStrength`) |
| `9b7caa14` | WIS |
| `eba0bf47` | INT |
| `aff73420` | AGI |
| `4d405c66` | DEX |
| `e02ce52f` | unidentified |
| `d033a890` | level |

These six values are points **spent**, not the character-sheet total (a
fresh character reads 0; the sheet shows `base 10 + spent`). Unspent
points *are* stored — as `RemainingPlayerIncreasedAttributes`
(`0x9CC8A62A`) — but that field is **absent** from the document on a
character with none remaining, so its absence in one save is not evidence
it doesn't exist in the format.

### Health / mana stat arrays

`AV.ceda2313` (health) and `AV.60d64632` (mana) each hold six
`{N: hashed-name, V: float32}` pairs. The six roles are consistent between
the two arrays, but **array order is not** — match by value relationship,
not position: current = base + adder, one field is always `1.0`
(multiplier, unmodified), one is a small constant (regen per tick).

### Currency

`C` = **gold**, confirmed against the in-game HUD. `AC` sits beside it
and is still unidentified. Both are plain uint32 counters, type `0x14`
(see §8) — mind the sentinel-value warning in §19 when writing them.

### Customization: parallel arrays

`modelIds[i]` (uint8) is the chosen index within the category identified
by `modelCRCs[i]` (uint32) — same pattern for `textureIds`/`textureCRCs`
and `colorIds`/`colorCRCs`. `255` in an id array means "none". Which
category (hair / beard / ears / eye shape / mouth / skin feature, etc.)
each CRC represents is **not proven** — the reliable way to map them is to
change one thing in the character creator, save, and diff; whichever
index moved names that category. Arrays only hold the categories a given
character actually uses, so **don't rely on index position across
characters — always match on the CRC.**

---

## 10. Universe files

- Tag `USHD` / document type `UniverseHeaderData` holds display naming.
- Prefer **short UI names** when multiple string candidates exist (e.g. `AIW`).
- `ILHD` entries list islands: seed, generationVersion, dCRC, etc.
- Loading a universe in the tool should filter **worlds** to that universe id
  and can auto-focus the character file.

---

## 11. World files and entities

World entities are primarily found by decompressing **`BKCK`** (and related)
chunks and walking BSON for `TemplateCRC` + transform/position fields.

### Example decode summary (`040000000100040f`)

| Metric | Value |
|--------|------:|
| File size | ~528 KB |
| Entries | ~1228 |
| Entities across BKCK | ~1723 |
| Unique TemplateCRC | ~235 |
| Named templates | ~6% |
| Unnamed | ~94% |

### High-count unresolved TemplateCRCs (that world)

| CRC | Count | Role guess |
|-----|------:|------------|
| `0x0E8731F4` | 520 | Breakable / harvestable prop |
| `0x065D3C8B` | 239 | Toggleable prop |
| `0xF79089E6` | 125 | Container / chest |
| `0xE9471111` | 104 | Container / chest |
| `0xFD39732B` | 63 | Wire relay / trigger |
| `0xB84725C2` | 50 | Breakable prop |
| `0x5B83845E` | 33 | Breakable prop |
| `0x246AABE5` | 28 | Breakable prop |
| `0xD7045302` | 26 | Breakable prop |
| `0x580A54FE` | 21 | Toggleable prop |

Resolved examples include Sign `0xF0653E24`, Landing Pad `0x0BCB9932`,
and the NPC table in section 13.

### Map editor conventions

| Kind | Footprint (tool) |
|------|------------------|
| Chest | 2×1 |
| Landing pad | 4×4 (expect one real pad) |
| Sign | 1×1 (or 1×2 in-game art) |
| NPC / other | 1×1 default |

- Sort worlds by **mtime** when listing.
- NPC stored Z may need **display offset −1** vs in-game (verify per build).
- Empty “pad-like” entities should not all render as landing pads.

---

## 12. Inventory classification

`classify_inventory_entity` uses array lengths:

| Heuristic | Label |
|-----------|--------|
| `IBP` count **> 40** | **trader stock** (not a player chest) |
| `IBP` ≥ 5 (and not trader) | chest |
| Equipment-heavy / mannequin patterns | mannequin |
| Pet/mount slot patterns | pet / mount housing |
| No items | empty |

The 40 cutoff isn't arbitrary: confirmed in-game player chest capacity is
exactly **40 slots** — full chests in a real world save cluster hard at
that count. Also note a character's backpack is stored **twice** in a
character file, mirrored byte-for-byte under both `Server Inventory
Component` and `Player Inventory Component` (the server copy is only
meaningful to a dedicated server) — dedupe by container path before
counting, or size-based heuristics will double every figure.

Chest UI should expose item **category / price / damage / defence** when the
merged item table provides them, and copy **hash hex + decimal**.

---

## 13. TemplateCRC and NPC names

### Naming priority in the tool

1. `NPC_TEMPLATES` hand map (below)
2. `cracked_templates.json` GUID label (`guid:…`)
3. Raw `0xXXXXXXXX`

### GUID convention (rule confirmed, corpus partial)

For items (`II`) and `raceCRC`, this hash rule is **solved and verified** —
confirmed against real save hashes via GUID strings mined from the exe:

```text
TemplateCRC = zlib.crc32(lowercase_hyphenated_guid_string) & 0xFFFFFFFF
              e.g. crc32("cc20a395-b1ce-41ad-a824-5370ec6c4091")
```

The same rule most likely covers `TemplateCRC` for world entities too,
since it shares type `0x14` with the confirmed keys, but this has not been
independently verified for NPC/entity CRCs specifically.

**Why most CRCs still can't be named — this is a corpus problem, not an
algorithm problem.** GUID text is only used at build time by the content
pipeline; it is not shipped in the retail game. The archive holds only a
handful of leftover debug GUIDs (recovered from the exe's string table) and
essentially none elsewhere, so scanning harder will not recover strings
that were never shipped. Most TemplateCRCs will need a display-name /
localization path instead, or manual in-game identification.

Localization strings for NPC display names were **not** found within ±512
bytes of those CRC values in unpacked `core_game` data — but a plain-ASCII,
non-hashed **display-name table** (grouped by category, with roughly a
dozen localized copies) has been found elsewhere in `core_game.kfc_data`
for items, so an equivalent table is a reasonable next place to look for
NPC names.

### NPC_TEMPLATES (community list)

| CRC | Name |
|-----|------|
| `0xCD2A9825` | (unknown NPC) |
| `0x65B984CA` | Aj-Kuar |
| `0xD213BEF3` | Anania Ol'faron |
| `0x63744871` | Arietta |
| `0xCBAD6778` | Bayard |
| `0x66878594` | Brother Harry |
| `0xBD7A1AEA` | Brother Orwell |
| `0xC83E3E23` | Carl the Collector |
| `0x9544E991` | Carla the Hunter |
| `0x8F943272` | Cyrill |
| `0xCD0C8557` | Cyrill |
| `0x12CAF442` | Donella |
| `0x6CE9CA3F` | Edna the Farmer (trader) |
| `0xC1D8494B` | El-Auuk |
| `0xC0589055` | El-Ulum |
| `0x29A42166` | Elise |
| `0x44A500F9` | Elise |
| `0xB9FEF619` | Eluni Sorrowsong (trader) |
| `0x7BFB6A38` | Erik the Woodworker (trader) |
| `0xC2BC0D86` | Fenimore |
| `0x68631B9D` | Frank the Farmer (trader) |
| `0x2457CDC4` | Funny Jongo (trader) |
| `0x8DBD2A2D` | Fuzzyscrimps McCuddlefluffs (trader) |
| `0x6FBECED3` | Happy Honey (trader) |
| `0x5344D28F` | Iki the Trader (trader) |
| `0x2D51A3A3` | Janine |
| `0xB6AE9323` | Janine |
| `0xBEB67826` | Jarin (trader) |
| `0x40BC6055` | Larry the Lumberjack |
| `0xA2A12AAE` | Linda the Farmer |
| `0xA0D944A9` | Mama Mirabella (trader) |
| `0x6EA6C041` | Mark the Farmer (trader) |
| `0x20BF992E` | Mike the Miner (trader) |
| `0xD0F80C03` | Monroe the Knight |
| `0x03AC4CAB` | Monroe the Knight |
| `0x91AC03F5` | Morton the Miner |
| `0x9A4C16B4` | Mr. Teeter |
| `0x0FAE7AD7` | Nick the Hunter |
| `0x17BC92DC` | Nina the Farmer (trader) |
| `0x173F668E` | Old Man Karl |
| `0x561661F1` | Philippa the Hunter |
| `0xA5268A06` | Rigolph the Alchemist (trader) |
| `0x1126AA6F` | Robert |
| `0xB7E2B804` | Robert |
| `0x1BC6D28C` | Rupert |
| `0x443063B9` | Rupert |
| `0x66C5F2F9` | Sammy the Smelter |
| `0x330C6F7C` | Shelton |
| `0x79056E9F` | Shelton (trader) |
| `0x20AAF76D` | Spectral Apparition (Dusty Junction) |
| `0x69683D4F` | Stanton the Geologist |
| `0xD83A970B` | Stanton the Geologist |
| `0x23B49904` | Tagus the Wizard |
| `0xFF0BC732` | Taylor Rafalini (trader) |
| `0x33BF829C` | Uko the Gladiator |
| `0xD823D129` | Uldrayus Ol'faron |
| `0xAE681447` | Ulv the adventurer |
| `0x3CE1B242` | Xarif |
| `0x89445452` | Ye the Jade Stone Merchant (trader) |
| `0x312C9942` | Ysmay (trader) |
| `0x61581C01` | Zane the Smith |

### Practical naming for the rest

Spawn unknown TemplateCRCs on a **safe grid** (clone a known entity, set CRC +
position), load in-game, name manually, save a `crc → name` map. Batch 20–50
at a time; always backup the world file.

---

## 14. Item table

- The **authoritative** item list lives in the game's own data, not in any
  community file: `server_core.kfc_data`, entry **1230** — 4,112 fixed
  16-byte records (`u32 item hash, u32 value/price-like, u32 flag, u32
  flag`), in the game's internal definition order (not sorted by hash, not
  spreadsheet order). Of the 4,112, 3,134 are confirmed obtainable (they
  appear in a real "all items" world save); the remaining ~978 are cut
  content with no shipped name or art — they render blank if forced into a
  save, they are not simply "unidentified."
- **`item_table_merged.json`**: a curated/labeled catalog built from the
  community spreadsheet plus confirmed in-game pairings — a friendly view
  for the UI, not the raw authoritative table above.
- **The community spreadsheet's row id is not the in-save `II` hash.** Sheet
  ids are small sequential row numbers; `II` is a 32-bit CRC (see §8/§13).
  Never join on id — pair hash↔item only from confirmed in-game evidence
  (equip the item, read `II` back) or from the archive's own item table.
- If missing or empty, the UI should log a clear warning (otherwise everything
  looks "unnamed").
- Chest editor columns: name, hash (hex+dec), count, category, price, dmg, def.

---

## 15. Asset research (core_game)

Portal Knights and Enshrouded are both built on Keen Games' **"kfc"**
engine. This matters directly: the container CRC64 (§6), the archive
index layout, and the item-hash mechanism (§13/§14) all trace back to
routines shared between the two games' engines, and community tooling
written for Enshrouded (`ndoa/kfc-tools`, `brabb3l/kfc-parser`) is a
useful — though not always exact — starting point.

### `.kfc_dir` / `.kfc_data` archive layout: "KFC0"

Game data ships as paired `<name>.kfc_dir` (index) and `<name>.kfc_data`
(blob) files. Portal Knights' `core_game.kfc_dir` uses a **third** layout
variant, `KFC0`, not documented by the existing third-party tools (which
assume `count == count2` and reject files where it doesn't hold):

```text
offset 0    magic "KFC0"
offset 4    count            u32   — number of hashed entries
offset 8    count2           u32   — number of index PARTS (count2 >= count)
offset 0xC  field_0xc        u32   — 0 in observed files
offset 0x10 data_file_size   u64
offset 0x18 hash_table       count  x u64   CRC-64/XZ filename hashes, sorted ascending
            size_table       count  x 16B   {size0 uncompressed, size1 (dup of size0),
                                             part_index, part_count}
            offset_table     count2 x u64   one entry per PART, not per hashed entry
```

Validated by exact arithmetic: `24 + 8*count + 16*count + 8*count2 ==
file size`. `count2 != count` because some entries are split across
multiple parts (`sum(part_count) == count2`); an entry's data location is
`offset_table[part_index]`, **not** `offset_table[i]` — walk parts, don't
index directly. The hash table being sorted ascending makes it a
binary-search index over 64-bit filename hashes (not the 32-bit item/CRC
ids used inside saves — do not confuse the two).

Blobs pulled from the archive are **not uniformly compressed** — observed
container types include `CRPF` (Keen resource package), `zstd`, `KSC1`,
`DDS`, and plain text/binary tables. Sniff the first bytes before
assuming a compression layer.

### Entity-reference tables (TemplateCRC)

Unpacked `core_game` contains binary tables that **reference** TemplateCRC as
LE uint32 fields. Two layout families were observed:

#### Family A — e.g. `0xB84725C2` (160-byte stride)

| Offset | Field |
|-------:|-------|
| +0x00 | TemplateCRC |
| +0x40 | float extent-like |
| +0x50 | u32 param block |
| +0x88 | field **A** (class/size bucket) |
| +0x98 | field **B** (subtype) |

Example: all instances A=32, B=137; only floats vary.

#### Family B — e.g. `0x065D3C8B`

| Relative to CRC | Field |
|-----------------|-------|
| CRC − 24 | **A** (often 30 or 34) |
| CRC − 8 | **B** (137, 314, 192, …) |
| CRC + 16 | float flag/scale-like |

These tables are **not** name dictionaries. String blobs hold dialogue names
without adjacent TemplateCRC.

### The item table: `server_core.kfc_data`, entry 1230

See §14 — 4,112 fixed 16-byte records, found by scanning for byte-exact
hits of known item hashes at a regular stride, confirmed by arithmetic
(`16 + 4112*16` == file size exactly).

### Texture / icon extraction

~2 GB of `core_game.kfc_data` is BC-compressed, mipmapped texture data,
identified purely by size arithmetic (`4/3 * dim² * bytes-per-pixel` for a
full mip chain). Confirmed slab classes include 1024×1024 BC3/BC1,
512×512 BC3/BC1, and 256×256 BC3 (item icons).

Key findings, since the naive approach failed repeatedly:

- **Per-entry headers are small, not negative.** An early model divided
  the mip chain down to 1×1 and overcounted by 28 bytes at the 256px
  class, making real entries look 28 bytes *short* and producing bogus
  negative "headers." The chain actually bottoms out at **4×4** (one BC
  block). Corrected, every observed slab size resolves to a small
  non-negative header that's a multiple of 4 (values seen: 4, 16, 24, 32,
  40 bytes, varying by size class).
- **Icons are not square.** The 256×256 assumption is wrong for at least
  some entries — a real decoded icon came back as two side-by-side copies
  of the same shape (the signature of decoding at 2x the true width).
  Re-laying at **128px wide** produced a single coherent image. Width is
  not assumed to be constant across all entries — use `pk_tex.py widths`
  to sweep candidate widths (64/96/128/160/192/256/320/512) and pick the
  one where the image renders exactly once; height is derived from the
  actual block count, not forced square.
- **Automatic offset/format detection could not be validated
  synthetically** — every synthetic test fixture encodes the author's own
  assumption about the layout, so it can only confirm what's already
  believed (three separate scoring metrics each passed on broken data for
  exactly this reason). The practical tool (`pk_tex.py sweep`/`survey`)
  decodes every candidate offset into one labelled image grid and leaves
  the choice to a human eye, rather than trusting an unvalidatable score.
- **Icons cannot currently be linked back to items.** The obvious
  candidate reference field, `ItemIndex` (§8), is not an icon id — it only
  appears on ~14% of items (equipment with durability/stats) and has no
  measurable correlation with item-table row position. Extraction and
  item identification remain two separate, unsolved-together problems.

---

## 16. pk_manager write safety

### Strengths

- Backup before mutation (`.bak` first snapshot).
- Full container rebuild in memory.
- Several paths re-parse and verify header/data CRC and round-trip values.

### Verified safe (confirmed on real saves, not just structurally)

- **Re-encoding need not be byte-identical to the game's own encoder.**
  A no-op re-wrap that changed one `BKCK` entry's compressed size by
  +1,545 bytes, with a completely different byte stream, loaded fine in
  game. Only the recorded **kind** (§7) and a structurally valid document
  matter.
- **Swapping to a different, valid item hash is safe**, provided: (a) the
  hash is a real, non-cut-content item (§14 — ~978 of 4,112 table entries
  have no shipped name/art and render blank if forced in), and (b) the
  slot's `SC` stays consistent with that item's stackability — equipment
  slots carry a `PI` sub-document rather than a plain count, and forcing a
  bulk `SC` onto a non-stackable item is very plausibly what an earlier
  "rejected save" incident traced back to, not the edit mechanism itself.
- CRC verification only catches structural corruption, not semantic
  invalidity (e.g. an out-of-range currency value, §19) — a file can
  report both CRCs valid and still be rejected or silently reverted by
  the game. Flag value-level issues explicitly rather than relying on CRC
  checks alone.

### Gaps

| Risk | Mitigation |
|------|------------|
| Some CharacterEditor writes skip verify | Single `write_container(..., verify=)` for all mutations |
| `open(path,"wb")` truncate window | Write temp + `os.replace` |
| Unhandled Tk callback errors | `report_callback_exception` → log + messagebox |
| N full rewrites for batch edits | Multi-patch then one write |
| Sync heavy world scan | Background thread + UI progress |
| Only one `.bak` | Timestamped / last-N backups |

---

## 17. Feature roadmap

### P0

1. Universal verified + atomic writes
2. TemplateCRC **test grid spawn** + external name map load
3. Tk global exception handler

### P1

- Recipe unlock / unlock-all
- Quest state editing
- Batch stack/stat edits
- Landing pad editor (mirror signs)

### P2

- Character duplicate / delete / export-import one slot
- Cross-save item search
- World/universe delete housekeeping
- Diff vs backup; persistent edit history
- Remember paths/geometry; version string; `requirements.txt`
- Robust Steam root discovery

---

## 18. Related files

| File | Role |
|------|------|
| `pk_manager.py` | GUI editor (single file) |
| `pk_dict.bin` | zstd dictionary |
| `item_table_merged.json` | Item catalog |
| `pk_world_crcs.json` | Auxiliary world/CRC data |
| `pk_item_crcs.json` | Confirmed hash↔item-name pairings (equip-and-read or chest-labeling) |
| `pk_all_item_hashes.json` | Complete 4,112-entry item hash list read from `server_core.kfc_data` entry 1230 |
| `cracked_templates.json` | Optional GUID↔CRC |
| `unresolved_template_targets.json` | CRC frequency from worlds |
| `format.md` | This document |

---

## 19. Warnings

1. **Always backup** before writes — especially `0100…` (all characters) and `0300…` (whole universe).
2. Universe **slot digit** is not remappable by display rename.
3. Community **32-hex** files are shared content.
4. Changing `TemplateCRC` without a valid prefab can crash or strip entities on load.
5. Trader inventories (`IBP > 40`) are not normal chests — do not "max stack" them blindly.
6. Type `0x01` is **float32**; never parse as float64.
7. Currency-like `0x14` counters (`C`/Coins, `AC`/Defender Coins, etc.) treat
   `0xFFFFFFFF` and nearby values as an "invalid/uninitialised" sentinel —
   the game silently discards such saves. Keep writes at or below
   `999,999,999`.
8. `SC` (stack count) is uint16; **65535** is the true hard cap. Don't derive
   per-item caps by sampling one world's stacks — that only reflects what
   that world happened to contain, not the real limit.
9. Type `0x14` is dual-use (§8) — don't treat every field of that type as a
   read-only CRC hash; several (`C`, `AC`, `playtime`, `slotId`, `price`,
   `lastPlayedTime`) are plain writable counters.

---

## Appendix A — Constants from pk_manager

```text
STEAM_APPID       = 374040
MAGIC             = KSC1
HEADER_SIZE       = 24
ENTRY_SIZE        = 12
ZSTD_MAGIC        = 28 B5 2F FD
SNPY_MAGIC        = SNPY
NAME_FIELD_SIZE   = 128
GAME_NAME_LIMIT   = 32
CRC64 poly        = 0x42F0E1EBA9EA3693 (reflected, CRC-64/XZ; init/xorout = all-ones)
```

## Appendix B — Compression kind matrix

```text
read:  raw → try zstd → if inner SNPY then zstd+snpy
        else if SNPY then snpy
        else if zstd frame then zstd
        else raw
write: must match recorded kind for that entry
```

## Appendix C — Entity role guesses (decode heuristics)

Used in world decode reports, not authoritative game enums:

- breakable/harvestable prop (no AI)
- toggleable prop / switch-controlled
- container / chest (inventory component)
- wire relay / trigger forwarder
- mining resource node
- creature (AI behavior tree)
- interactive switch/lever/plate
- pacify-able / boss-like
- sign (`0xF0653E24`)
- landing pad (`0x0BCB9932`)
