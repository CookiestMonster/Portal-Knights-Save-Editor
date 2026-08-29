# Portal Knights Save Format & Live Memory Reference

Companion document for `pk_save_editor.py` Describes the
on-disk save container, filename encoding, compression stack, custom BSON,
world/character structures, TemplateCRC naming, terrain voxels, editor
implementation notes — and, in §21, the live-memory (RAM) side of the
research: enemy stats, the runtime spawn system, and the shared
attribute-hash table that ties the disk and RAM findings together.

(enemy spawn system, decoded from `readable_strings_v3.txt`). The latter two are
combined into the new §21, with cross-references added at the points in
§11/§14/§18 where the disk-format research had left the same questions open.

Last updated: 2026-08-27

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
12. [Terrain and voxels](#12-terrain-and-voxels)
13. [Inventory classification](#13-inventory-classification)
14. [TemplateCRC, NPCs, signs](#14-templatecrc-npcs-signs)
15. [Item table](#15-item-table)
16. [Asset research (core_game)](#16-asset-research-core_game)
17. [pk_save_editor write safety](#17-pk_save_editor-write-safety)
18. [Feature roadmap](#18-feature-roadmap)
19. [Related files](#19-related-files)
20. [Warnings](#20-warnings)
21. [Live memory (RAM): enemies, stats & spawn systems](#21-live-memory-ram-enemies-stats--spawn-systems)
22. [Offline KFC research: entity defs, furniture catalog, drop tables](#22-offline-kfc-research-entity-defs-furniture-catalog-drop-tables)

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

`pk_save_editor.py` (and earlier `pk_save_editor.py`) is a single-file GUI that
discovers these saves, decompresses entries, parses BSON, maps terrain
voxels, and can patch scalars / inventory / NPCs with backup + CRC
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
Sourced from my spreadsheet and `WORLD_LOCATIONS` in `pk_save_editor.py`:

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
| `CIPI` | Creative island roster (`CustomIslandPlanetInfo`) |
| `PTHD` | Planet header (`PlanetHeaderData`, unlock / creative locks) |

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
- Table-driven implementation in `pk_save_editor` (`crc64`).
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
- `pk_save_editor` embeds a pure-Python snappy codec (no native dependency).
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
- `ILHD` entries list islands: seed, generationVersion, dCRC, size, spawn, etc.
- Loading a universe in the tool should filter **worlds** to that universe id
  and can auto-focus the character file.
- Runtime log `[pregame] Missing Universe Header` appears when entering
  universe selection without a usable header binding (often empty/invalid
  `USHD` or a universe slot the menu cannot resolve). Not yet pinned to one
  specific missing BSON field.

### Creative vs Adventure (critical)

**`USHD.gameplayMode`** is the authoritative universe-level flag:

| Value | Meaning |
|-------|---------|
| `'Creative'` | Creative-only universe. Islands are player blueprints / flat templates. |
| (other / absent) | Normal adventure progression (story islands, generated terrain). |

Verified on universe `0300000000000000` (slot 0):

```text
UniverseHeaderData.gameplayMode = 'Creative'
UniverseHeaderData.universeSize = 'Small'
UniverseHeaderData.name         = 'Universe 1'
lastVisitedIsland               = planet 0 / cluster 2 / island 9
```

#### Why world filenames still say “Addermoor (2-11)”

World files are named `04` + universe(8 hex) + location(4 hex), e.g.
`0400000000000209`. The trailing `0209` = **cluster 2, island 9**
(location id 521). The editor’s `WORLD_LOCATIONS` table maps that id to
the **story** name “Addermoor (2-11)”.

In a **Creative** universe that same slot is **not** the story island. It is
whatever the player put there (flat / blueprint). The story name from the
filename alone is **misleading** — always check `USHD.gameplayMode` (and
`CIPI` below) before trusting location names.

#### `CIPI` — CustomIslandPlanetInfo

Present on Creative universes. Roster of island slots:

| Field | Example | Notes |
|-------|---------|--------|
| `islandTemplateId` | `'Homeland'`, `'CreativeB'` | Template class for the slot |
| `islandName.name` | `'Blueprint'` | Player-visible name (64-byte padded) |
| `islandThemeCRC` | `3324483980` (`0xC627998C`) | Theme asset CRC (e.g. Terrain / Balanced-style themes) |
| `islandId` | `9` | Matches the location low bits in the world filename |
| `islandSize` | `0` | Often 0 in CIPI; real size is in that island’s `ILHD` |

Example (same universe, last-visited slot):

```text
clusterId=2, islandId=9
  name             = 'Blueprint'
  islandTemplateId = 'CreativeB'
  islandThemeCRC   = 3324483980
```

Empty / unused slots typically show `islandTemplateId = 'Homeland'` and a
zeroed name.

#### `ILHD` on Creative islands

Same structure as adventure. Example for location 521 on the Creative
universe above:

```text
seed = 1787929990
islandSize = 128 × 128 × 128
playerSpawnPosition ≈ (43, 64, 103)   # matches pad on the world file
```

#### Map / terrain detection (editor)

Do **not** classify Creative vs Story from BKCK chunk-id density alone.
A heavily edited Creative island can have dense sequential BKCK ids and
look “Story/Generated” while `gameplayMode` is still `'Creative'`.

Recommended rule for the offline editor:

1. If parent universe `USHD.gameplayMode == 'Creative'` → treat as
   **Superflat / Creative** → **show BKCK terrain** on the map.
2. Else if BKCK ids are sparse bit-field → Superflat-like → show terrain.
3. Else → Story/Generated → hide BKCK terrain by default (seed mesh is
   not fully stored; BKCK is solid fill / edits only).

#### `PTHD` notes

`PlanetHeaderData.islandClusterStates[].islandStates[]` includes
`isCreativeModeBlocked` / `isCreativePlayTestModeBlocked` per island
(adventure locks). Not a substitute for `USHD.gameplayMode`.

---

## 11. World files and entities

World entities are primarily found by decompressing **`BKCK`** (and related)
chunks and walking BSON for `TemplateCRC` + transform/position fields.

> Regular enemies are conspicuously absent from this list — see **§21.3**
> for why: they are generated at runtime by a spawner system and never
> touch the world file at all.

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
- Landing pad TemplateCRC is **`0x0BCB9932`** (entity). Guitar Pad F
  (`0xDF6A6AFD`) is a different prop (trigger/mining-style), not a sign.

### Creative themes / lighting

Primary theme handle for Creative islands is **`CIPI.islandThemeCRC`** on the
**universe** file (not the world file). Example: `0xC627998C` on a
“Blueprint” / `CreativeB` slot (user-facing theme often described as
Terrain / Balanced-style). World-file ILAS may also carry theme-related
blobs on some islands.

Creative sky moods (Halloween bats, mist, etc.) are **not** reliably stored
as a simple field in the world file’s BKCK/FLCK. Do not assume tag counts
(BKCK×16 / FLCK×64) encode theme. Always pair with `USHD.gameplayMode`.

---

## 12. Terrain and voxels

Player-placed blocks and complex terrain live in **`BKCK` `Chunk.voxelData`**,
not in FLCK.

### Two terrain encodings

| Encoding | Where | When |
|----------|--------|------|
| **columnSet** | FLCK | Simple / flat ground. `columnCount = 1024` (32×32), each column **10 bytes**. Observed fill pattern `00…00 01 00` = solid slab of block id 1. |
| **voxelData** | BKCK Chunk | Real placed blocks / complex terrain. Exactly **32 768 bytes = 32³** (one byte per voxel). |

Flat Creative islands often have only identical FLCK columns and empty
voxelData until the player places blocks. Dense story islands (e.g. Fort
Finch) use voxelData heavily.

### Indexing: Morton (Z-order)

Byte index inside a 32³ chunk is **not** linear `x + 32*y + 1024*z`.
Confirmed by controlled builds (vertical stack, plus shape, material lines):

```text
bits interleaved: bit 3k → x,  3k+1 → y,  3k+2 → z
y = height inside the chunk
```

Linear guesses produce “empty bands” and scrambled lines; Morton recovers
straight pillars and a clean 4×4 landing-pad surface.

### Chunk grid → world

For normal islands with chunk ids 0–63:

```text
world_x ≈ (chunk_id % 8) * 32 + local_x
world_z ≈ (chunk_id // 8) * 32 + local_z
```

→ ~256×256 island from an 8×8 of 32³ chunks.

**Editor note (2026-08-28):** chunk-id → grid is **mode-dependent**:

| BKCK id set | Grid | Local X/Z |
|-------------|------|-----------|
| Sparse bit-field family (unique cells under bits 0+3 / 2+5) | bit-field 4×4 disc | no axis swap |
| Dense sequential 0..N | linear `cid%8` / `cid//8` | Morton local axes swapped onto world XZ |

Landing-pad snap (entity pos vs voxel id **251**) still corrects residual
origin error when a pad is present. **Creative vs Story for map terrain
visibility** must use `USHD.gameplayMode` when the universe is available
(see §10) — dense BKCK alone is not proof of story generation.

Anchor: landing-pad entity at known world pos + the 16 surface voxels of
id **251** in the same chunk lock origin and half-block centering
(+0.5 for block centers).

### Known voxel block ids

| Id | Name |
|---:|------|
| 0 | air |
| 1 | dirt |
| 2 | Soil |
| 7 | Dark Parquet |
| 8 | Parquet |
| 10 | Coal Block |
| 13 | Polished Gray Wood |
| 14 | Polished Dark Wood |
| 15 | Polished Wood |
| 16 | Wood |
| 18 | Polished Bamboo |
| 49 | Sand |
| 50 | Straw |
| 51 | Snowblock |
| 244 | pad-detail / glow (not the 4×4 floor) |
| 251 | **landing-pad surface** (exactly **16** = 4×4×1) |
| 252 | pad-extra |

Ids are **small integers**, unrelated to item-table `II` hashes. Names locked
by placing known materials in a line under an NPC and matching index order
(coal/snow anchors).

### Editor notes

- **Terrain / voxels…** — lists non-air cells by chunk + block id; 2.5D Y-slice
  (Morton X/Z top-down, Y slider 0–31).
- **Map…** — optional terrain layer under NPCs/chests; same Y axis; adaptive
  subsampling for performance.
- Fully **quit the game** after placing blocks or the file may not flush
  voxelData (FLCK may stay unchanged).

---

## 13. Inventory classification

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

## 14. TemplateCRC, NPCs, signs

### Type vs instance

`TemplateCRC` is the **asset type** (blueprint). Many worlds correctly share
the same CRC for the same NPC or furniture mesh. Collect-by-CRC for a catalog
island is correct; it is *not* an error that the same hash appears in two
worlds. Instance data that differs includes position, orientation,
`SpawnerImpactId`, inventory (`II`/`SI`/`SC`), custom text, age/growTime, etc.

### Collect all worlds → catalog island

- Scan all world files → unique TemplateCRCs that look like NPC Control.
- Place one of each on a vacant island grid; **never reuse (x,y,z)**.
- Insert in **batches of ~48** across multiple EntityArrays (emptier first).
- After write: log `positions=N unique=N` and how many NPC Control entities
  the file actually contains (planned + pre-existing).
- Earlier bug: Y hitting island height cap reused XZ cells → only ~64 visible;
  fixed by unique positions + multi-array insert.

### Signs and custom text

| Prop | TemplateCRC (example) | Notes |
|------|------------------------|--------|
| Landing pad | `0x0BCB9932` | 4×4 entity + voxel surface 251 |
| Sign (User Editable String) | e.g. `0x0E582198` | Has `User Editable String Component` |
| Guitar Pad F | `0xDF6A6AFD` | Trigger/mining pad — **not** a sign |

- Custom / flushed text lives on the **entity** (string field). Copying the
  entity can carry that text across worlds.
- `wasEdited = false` → game still uses **default** text from game data; the
  readable line is **not** in the save.
- Opening a sign in-game often sets `wasEdited = true` and copies the default
  line into the save — so “edited” does not only mean “player typed custom text.”
- Editor Signs… lists all User Editable String props; shows stored text or
  “(default game text — not stored in save)”.

### Naming priority in the tool

1. `NPC_TEMPLATES` / `pk_templates.json` hand map
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

> **Independent confirmation (§21.8):** the live-memory research hit the
> same wall from a completely different angle — hashing all 617 known
> *enemy* template asset names (`enemy_slime_base`, etc.) against ~60
> prefix/suffix variants each produced zero matches against the 7 known
> enemy TemplateCRCs. Two unrelated approaches (disk GUID-string mining vs.
> RAM asset-name hashing) landing on the same "the preimage isn't shipped"
> conclusion is a good sign the conclusion is actually correct, not an
> artifact of one method.

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

## 15. Item table

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

## 16. Asset research (core_game)

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

## 17. pk_save_editor write safety

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

## 18. Feature roadmap

### P0

1. Universal verified + atomic writes
2. TemplateCRC **test grid spawn** + external name map load
3. Tk global exception handler

### P1

- Recipe unlock / unlock-all
- Quest state editing
- Batch stack/stat edits
- Landing pad editor (mirror signs)
- Voxel place/delete (write path for BKCK voxelData)
- Full chunk-id → world origin grid for all sparse island layouts

### P2

- Character duplicate / delete / export-import one slot
- Cross-save item search
- World/universe delete housekeeping
- Diff vs backup; persistent edit history
- Remember paths/geometry; version string; `requirements.txt`
- Robust Steam root discovery
- Creative theme / islandThemeCRC dictionary (universe or world)
- ~~Enemy definition HP/loot (EntitySystem component defs — not in save
  body)~~ — **answered by §21**: health/damage are computed at spawn time
  from a scaling formula, not stored anywhere; loot lives in a
  server-only "Server Loot Drop Component" not reachable from disk. Live
  values are readable via the toolkit in §21.11, but writing them back
  is a separate, unstarted problem (they don't persist, so there's
  nothing on disk to patch).

---

## 19. Related files

| File | Role |
|------|------|
| `pk_save_editor.py` | Current GUI editor (single file; supersedes older `pk_save_editor.py` name in practice) |
| `pk_save_editor.py` | Legacy / companion name |
| `pk_dict.bin` | zstd dictionary — prefer **same folder as the .py**; if Program Files is not writable, use `%LOCALAPPDATA%\PortalKnightsSaveEditor\pk_dict.bin` |
| `item_table_merged.json` | Item catalog |
| `pk_templates.json` | NPC / prop name map (user-editable) |
| `pk_world_crcs.json` | Auxiliary world/CRC data |
| `pk_item_crcs.json` | Confirmed hash↔item-name pairings (equip-and-read or chest-labeling) |
| `pk_all_item_hashes.json` | Complete 4,112-entry item hash list read from `server_core.kfc_data` entry 1230 |
| `cracked_templates.json` | Optional GUID↔CRC |
| `unresolved_template_targets.json` | CRC frequency from worlds |
| `format.md` | This document |
| `pk_enemy_catalog.json` | 617-template enemy catalog extracted from strings — see §21.5 |
| `ce_enemy_health.lua` / `ce_av_dump.lua` | Cheat Engine Lua scripts for live enemy/AV scanning — see §21.11 |
| `pk_live_enemies.py`, `pk_enemy_diff.py`, and the rest of the `pk_enemy_*`/`pk_*` live-memory scripts | Live-memory toolkit — full list in §21.11 |

---

## 20. Warnings

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
10. Placed blocks live in **BKCK voxelData** (Morton 32³), not FLCK. Fully
    quit the game after building or the file may not flush.
11. Same TemplateCRC in two worlds is normal (type hash). Do not “dedupe”
    furniture instances by CRC alone if you need every physical copy.
12. Sign/NPC default dialogue is often **not** in the save until opened or
    edited in-game (`wasEdited`).

---

## 21. Live memory (RAM): enemies, stats & spawn systems

This section merges `PK_FINDINGS.md` and `PK_SPAWN_SYSTEM.md` — a separate
research thread from §1-20 above. Everything before this point is about
the **on-disk** save; everything below is about **live process memory**,
read with `pymem` scans and Cheat Engine (CE) Lua, not the KSC1/BSON path.
The two threads share one thing directly: the same `crc32(lowercase(
"Parent.Child"))` attribute-hash convention from §9 is used for entity
stats in RAM as well as for the character's own `AV` block on disk.

### 21.1 Overview — why a live editor at all

§11 already shows that world files carry essentially no regular enemies.
That's not a gap in the disk-format research — it's because enemies
genuinely don't live on disk (see §21.3). A live memory editor is how you
read or change anything about them at all: **save editor = disk
(KSC1→zstd→BSON); live tools = RAM (pymem scan + CE Lua)**, sharing the
same attribute-hash table on both sides. The approach is proven and
working, not speculative — every figure in §21.2 was read from a real,
running game session (Squire's Knoll).

### 21.2 Confirmed enemy stats (measured live, Squire's Knoll)

| Visual type | Max HP | Damage | Speed (AV) | Regen | Notes |
|---|---|---|---|---|---|
| Green Slime | 60.8 | 16.64 | 1.0 | 0 | common |
| Venom Maggot | 60.8 | 16.64 | 1.0 (reads 0) | 0 | underground, never moves |
| Orange Slime | 60.8 | 16.64 | 3.0 | 0 | rarer |
| Parrot | 76.0 | 20.8 | 3.0 (max 4.0) | 0 | flies |
| Fallen Soldier (Skeleton) | 152 | 41.6 | 2.0 (max 5.0) | 0 | placed, respawns on world load |
| Dummy Harrold | 30400 | none | 4.5 (unused) | 0 | two AV blocks (152+30400) |
| Dummy (builder) | 152 | — | — | 0 | |

Green/Maggot/Orange slimes have **identical** stats — only 3 slime
templates exist (`enemy_slime_base`/`slimy`/`tackle`); colour is a model
variant, not a stat variant. The AV block is the shared template.
`MovementSpeed` in the AV block is a default, not observed behaviour: the
**Maggot is the speed-0 group** — it lives underground and never moves,
so its AV speed reads 0 (or the movement code simply never runs for it).
A kill-diff (§21.11) confirms this: killing a maggot drops the speed-0
group's count specifically.

### 21.3 Why enemies are never in world files (solved)

Confirmed from two independent angles:

1. **Disk evidence**: `pk_templates.json` lists exactly **7** "enemy"
   entries, and all 7 are bosses/rift knights (`Hollow King Boss`,
   `Anub'Kraken`, `Hollow Dark Knight (spawn)`, …). A full scan of the
   Squire's Knoll world file found 52 templates, **0 spawners, 0
   enemies** — just props (Wheat Field ×72, vases, lanterns) and voxel
   dirt ×293,699.
2. **String evidence**: the game's own strings say so outright —
   `editor_spawner_event_normal_enemy_unsaved`. The `_unsaved` suffix on
   several spawner template strings (§21.4) is the game marking most
   runtime spawners as **not persisted to the world file**, which is
   exactly why the disk scan in (1) found nothing.

So: **regular enemy info is generated at runtime and lives only in live
memory** — the same memory a 20,110-string dump (used to build §21.4-21.5)
was pulled from. Health floats and the strings that explain them were
sitting in the same process the whole time, just in different tables
(strings in a static name table, health in per-entity AV blocks, §21.6).

### 21.4 Spawn mechanics: spawners, waves, and health scaling

**Spawner templates** (352 strings found) are placed by the dungeon
editor at runtime, seeded from the island seed. Sizes encode the spawn
volume (W×H×D in blocks):

```text
basic_enemy_spawner_1x2x1   1x3x1   1x4x1   1x5x1   3x2x3   5x4x5
```

Dungeon/event spawners:

```text
editor_spawner_dungeon_normal_enemy
editor_spawner_dungeon_small_enemy_special_02
editor_spawner_event_normal_enemy        editor_spawner_event_elite_enemy
editor_spawner_event_normal_enemy_unsaved   <- "unsaved" = NOT persisted!
editor_spawner_melee_enemy               editor_spawner_ranger_enemy
spawner_crystal_rift_phantom_small01     spawner_rift_groundtrap_pool
```

**Wave controllers** drive arena combat via props that *are* stored in
the world (the controller, not the enemies it spawns):

```text
prop_wave_control_arena01 .. arena07 (+ _HM hardmode variants)
wave_controller_arena01 .. arena07
gWaveConfig  gSubWaveCount  gSubWaveCountMax  gSubWaveDelay
spawnSubWave  runSubWave  checkWave  configureRandomWave
Arena_LastWave  sendWaveCompleted  bbIsWaveRunning  bbWaveNumber
```

So a stored `prop_wave_control_arena01` entity is what *triggers*
sub-wave enemy spawns; the enemies themselves still never touch disk.

**Health/damage scaling** — enemy health is computed at spawn time, not
stored, from these string-confirmed knobs:

```text
enemyHealthScaling_PlayerCountMod
enemyBossHealthScaling_PlayerCountMod
enemyArenaHealthScaling_PlayerCountMod
enemyMiniBossHealthScaling_PlayerCountMod
enemyArenaHMHealthScaling_PlayerCountMod
enemyDamageScaling_PlayerCountMod
SizeScalingMultiplier.HealthScalingMultiplier
SizeScalingMultiplier.DamageScalingMultiplier
CRC_EnemyPlayerCountScalingMode_NoScaling
CRC_EnemyPlayerCountScalingMode_MiniBoss
CRC_EnemyPlayerCountScalingMode_HardmodeBoss
getMaxPlayerCount  getPlayerCount  playerCountAttribute
```

Formula (from the scaling-mode enum + mods):

```text
final_health = base_health
             × healthScaling(playerCount)      # per-player-count multiplier
             × SizeScalingMultiplier           # big = more HP
             × levelScaling(island level)      # via CharacterLevel attribute
```

That's why no save ever contains a slime's HP: it depends on how many
players are in the session at spawn time, which isn't a save-time fact.

### 21.5 Enemy catalog (617 templates, 61 types)

Extracted to `pk_enemy_catalog.json`:

| Type | Count | Examples |
|------|------:|----------|
| slime | 52 | `enemy_slime_base`, `enemy_slime_king_add`, `enemy_slime_blackplaque` |
| skeleton | 49 | `enemy_skeleton_armored`, `enemy_steelskeleton_teleport_convoy_elite` |
| dragonman | 30 | `enemy_dragonman_caster_convoy_large_filler` |
| spider | 28 | `enemy_spider_base`, `enemy_spider_crystal01_fire` |
| turtlesoldier | 28 | `enemy_turtlesoldier_*` |
| dekuworm | 27 | `enemy_dekuworm_acidspitter_rift_large` |
| walkingplant | 25 | `enemy_walkingplant_fireblossom_convoy_elite` |
| monkeypriest | 25 | `enemy_monkeypriest_caribbean_event` |
| orcsoldier | 21 | `enemy_orcsoldier_flameking_rift_large` |
| stoneguardian | 21 | `enemy_stoneguardian_*` |
| hollowknight* | 21 | `enemy_hollowknight11/12/13_*` |
| boss | 25 | `enemy_boss_01_dragon`, `enemy_boss_02_dekuworm_desert`, `enemy_boss03_hollowking_*` |
| … | | (61 total types, 617 total templates) |

Suffixes are behaviour modifiers, not separate types: `_convoy` (follows
a leader), `_filler` (mob filler), `_elite`, `_event`, `_rift`, `_quest`,
`_HM` (hardmode), `_aggro`, `_large` (size class), `_add` (summoned add).

### 21.6 Live memory layout (decoded)

**Player**

```text
P+0x0 level, P+0x80 XP, P+0x140 current HP, P+0x144 max HP, P+0x300 mana
```

**Attribute (AV) block** — every entity with health, same hashing rule as
the on-disk `AV` block in §9:

```text
grid stride 0x40: {N at +4 = hash, V at +8 = float}
  MovementSpeed (Speed, .Base, .Multiplier, .Adder, .Max)
  Health x6 (current, .Max, .Multiplier, .Adder, .Base, .Regeneration)
  Damage x4, Outgoing/Incoming_DamageMultiplier, CharacterLevel, Experience
```

**Entity header** (base = anchor − 0x13C):

```text
base+0x00 size/scale float; base+0x04 per-instance value;
base+0x08.. component pointer array (13-15 ptrs: attack/model/schema)
```

**HP-pair stat structure** (per entity, no hashes — a simpler sibling to
the full AV grid above):

```text
HP+0x00 current, HP+0x04 max, HP+0x08 regen, HP+0x0C 1.0, HP+0x10 100.0 (cap)
HP+0x14 1.0 ... (then pointers/ids as garbage-float reads)
```

**Template records** (`item_enemy_*` / `enemy_*`):

- name + "Default Attack Name" + u32 family id (slimes: `0x95BA`/`0x366A`)
- pointers to attack/model/attribute-schema config
- attribute-schema fields: `Health`, `CharacterLevel`, `Health.Max`,
  `Health.Regeneration`, …

**Mirrors**: every entity exists **twice** in memory (client + server
copy). The server copy is a sparse grid (health only); the client copy
is the full grid. Kill-diffs (§21.11) show 2 addresses vanishing per
kill, not 1 — the same client/server duplication §13 documents for a
character's on-disk backpack shows up again here in RAM.

### 21.7 Attribute & combat stat hash reference (merged)

Both research threads independently confirmed the same hashing rule
(§9): `hash = crc32(lowercase("Parent.Child"))`. Where the two overlap
the values agree exactly, which is a good independent sanity check on
both. "✓ both" below means confirmed by the on-disk character-sheet work
(§9) *and* by live enemy-memory reads (§21); everything else in the
combat-stat table is live-memory-only so far.

**Character-sheet stat points** (from §9 — six core attribute keys,
verified against the in-game sheet; these are points *spent*, not the
sheet total — see §9 for the base-10-plus-spent caveat):

| Hash | Attribute |
|---|---|
| `0xA1CCC259` | CON |
| `0x901AAAEA` | STR (`PlayerIncreasedStrength`) |
| `0x9B7CAA14` | WIS |
| `0xEBA0BF47` | INT |
| `0xAFF73420` | AGI |
| `0x4D405C66` | DEX |
| `0xE02CE52F` | unidentified |
| `0xD033A890` | `CharacterLevel` / level — ✓ both |
| `0x9CC8A62A` | `RemainingPlayerIncreasedAttributes` (unspent points; absent, not zero, when none remain) |
| `0xC764ED49` | `Durability` |

**Combat / entity stats** (from live enemy AV blocks, §21.6):

| Hash | Attribute |
|---|---|
| `0xCEDA2313` | `Health` — ✓ both |
| `0x7C323E60` | `Health.Max` — ✓ both |
| `0x7480B8DE` | `Health.Max.Base` |
| `0x6401BFE1` | `Health.Max.Adder` — ✓ both |
| `0x25AB9C28` | `Health.Max.Multiplier` |
| `0x72566DAA` | `Health.Regeneration` |
| `0x11C8546C` | `Damage` |
| `0x1B2F9DF0` | `DamageMelee` |
| `0x1B0DA612` | `CharacterLevel.Max` |
| `0x8A2DE1F7` | `MovementSpeed` |
| `0xBF27FEFC` | `Armor` |
| `0x55A5FEA8` | `DamageSchoolSusceptibility` |
| `0x3BB8DD4B` | `DamageNormal` |
| `0x4D6D1028` | `DamageBlunt` |
| `0xBD0FA501` | `DamageCutting` |
| `0x65BAABA5` | `DamageSharp` |
| `0xE20656E7` | `DamageIce` |
| `0x81211AC1` | `DamageFire` |
| `0x240B263C` | `DamageThunder` |
| `0xE349ACE6` | `DamagePoison` |
| `0xA53372D0` | `DamageHoly` |
| `0xF6C50707` | `DamageDaemonic` |
| `0x5DEFAE8C` | `DamageAstral` |
| `0xD17982FA` | `DamageCursed` |

### 21.8 TemplateCRC / name problem (see also §14)

`crc32(asset_name)` does **not** match a live entity's `TemplateCRC` —
tested exhaustively: all 617 enemy template names (§21.5) × ~60
prefix/suffix combinations against the 7 known enemy CRCs, **zero
matches**. This independently reaches the same conclusion §14 already
drew from disk-side GUID mining: the real preimage is a build-time GUID
string that was never shipped in the retail game.

What *does* work empirically: the in-memory string table holds template
names and their CRCs **adjacent** to each other. `ce_enemy_health.lua`
(§21.11) exploits exactly this — find `enemy_slime_base` in memory, dump
the u32s around it, one of them is its `TemplateCRC`. That builds the
name↔CRC map one island at a time, the same "spawn on a grid and
identify manually" workflow §14 already uses for world-entity CRCs.

### 21.9 Positions — the definitive negative

**Positions are not in or near the AV/HP-pair structure.** A full
±0x100 float dump around a real 60.8-HP entity showed no world
coordinates — only the HP pair, the 100.0 cap, and pointer/ID garbage.
Values seen earlier at `+0x64`/`+0x68`/`+0x6C` (12/16/14) turned out to
be base attributes, not coordinates, on closer inspection.

- Position lives in the **transform component**, behind the entity
  manager, not inline with the stat block.
- A CE pointer scan on the HP value found only value-copies (the
  client/server mirrors from §21.6), no manager path — the entity
  manager uses indirect/relative linkage, not a simple static pointer
  chain.
- The 1,029 transform positions found separately (landing pad, etc.) are
  **world-placed objects in a static pool** — unrelated to live entity
  transforms; don't conflate the two.
- **Conclusion**: per-entity positions need a full entity-manager walk
  (multi-level pointer chain), realistically via CE Pointer Scan plus
  manual disassembly — beyond what a scripted single-level scan can do.

### 21.10 Drop tables & XP

- **XP** is directly measurable live: player XP sits at `P+0x80`
  (§21.6); kill one enemy and the delta is exactly its XP value.
- **Drop tables are not** in the `item_enemy_*` template records — those
  pointers resolve to attack/model/schema config only. The actual
  component is named **Server Loot Drop Component**, and it's
  server-side, reachable only via the (currently unsolved) entity-manager
  walk from §21.9 — the same missing piece blocks both problems.

#### Live AV block is not loot

Following a live Green Slime HP address (`pk_loot_follow.py --record`)
yields **7 heap pointers**, all into the **AV / attribute schema**:

```text
shared:  Health, Damage, DamageMelee, DamageRanged, DamageSpell,
         Multiplier, Adder, Base, Max, Regeneration,
         ExperienceScalingMultiplier, enemy_slime_slimy
per-instance: value tables for that slime only
magic 0xCEDA2313 = Health attribute hash (§21.7)
```

Four simultaneous slimes share the same schema pointers; only value pages
differ. **No item hashes, no weighted drop rows.**

#### Ground-loot item-CRC scan does not work

Scanning process memory for known drop item CRCs (shards, berries, water,
Gold Orb Small Dev `0xCCB510B3`, XP globe small `0x60FDF920`) finds
hundreds of static hits (item table, UI, inventory). Address-set diffs
after a kill produce noise (e.g. `0x10000019…`), not the visible ground
crystals. Ground pickups are **not** durable item-hash slots and are
**not** in the `FFFFFFFF` transform sentinel pool used by static props
(`pk_transform_scan.py` still matches **0** health anchors to positions).

#### Empirical Green Slime drops (playtest)

```text
Blue Portal Stone Shard   ×2   (0xA12385DA)
Water                     ×1   (0xA7528CD9)
Gold Orb Small (Dev)      ×2   (0xCCB510B3)   — yellow cubes on ground
XP globe small            ×1   (0x60FDF920)   — item_name_orb_experience_globe_small
```

Treat enemy drop **lists** as observed-in-game until the entity manager
is solved. Offline weighted tables in §22 are **dungeon prop/chest
filler**, not enemy loot.

### 21.11 Toolkit & live workflow

All of the following are working, proven on real saves/sessions — not
speculative:

| Tool | What it does |
|---|---|
| `pk_live_enemies.py` | scan + group entities by health |
| `pk_enemy_roster.py` | count enemies by stat signature + name (reads JSON) |
| `pk_enemy_diff.py` | before/after kill diff |
| `pk_enemy_deepdump_v3.py` | full AV stat block for one entity |
| `pk_enemy_types.py` | signature classifier + JSON loader |
| `pk_type_fields.py` | find type-identifying field via header diff |
| `pk_string_crcs.py` | find name strings + template IDs in memory |
| `pk_family_scan.py` | scan for family ids / record refs |
| `pk_loot_follow.py` | follow record pointers, resolve item hashes (AV only on client) |
| `pk_ground_loot.py` | live item-CRC scan + address-set diff (noisy; not ground entities) |
| `pk_stat_cache.py` | scan for HP-pair stat structures |
| `pk_entity_manager.py` | probe for the entity manager |
| `pk_find_transform.py` | chase component pointers for position |
| `pk_pos_array.py` | decode the transform position array |
| `pk_transform_scan.py` | sentinel-based position scan |
| `pk_transform_chase.py` | chase transform signature |
| `pk_check_refs.py` | inspect CE pointer-scan results |

Since enemies exist only at runtime, the ground truth has to come from
CE reading live memory — `pymem` alone can't do it (a 299 permission
error; CE's own driver can). The repeatable workflow used throughout
this section:

```text
STEP 1   ce_enemy_health.lua          (snapshot A)
         scans for CEDA2313 (Health) + D033A890 (CharacterLevel)
         → ce_enemies_a.json   (every entity with health + HP + level)

STEP 2   kill ONE enemy
         ce_enemy_health.lua          (snapshot B)
         → ce_enemies_b.json

STEP 3   python pk_enemy_diff.py --before ce_enemies_a.json --after ce_enemies_b.json
         → the address that vanished = YOUR ENEMY

STEP 4   ce_av_dump.lua  (paste that address)
         → full NAMED stat block of the enemy:
              Health.Max.Base      = 84.0
              Health.Max.Multiplier = 1.0
              CharacterLevel       = 8
              Armor                = 0.0
              DamageMelee          = 9.0
              MovementSpeed        = 1.0
           (resolved via the hash table in §21.7)
```

### 21.12 Status: answered vs. still unsolved

**Answered:**

1. A live memory editor works — full toolkit above, proven on real saves.
2. Enemy stats read live: HP, damage, speed, regen, level, per entity.
3. Where enemies spawn: runtime seed/theme spawners, never saved,
   player-count-scaled, respawning placed enemies (soldier/dummies) on
   world load only.
4. Why slimes share identical stats: shared template family, colour is
   a model variant only.
5. Maggot = the speed-0 underground group, confirmed by kill-diff and
   behaviour.
6. AV block layout + hash table decoded, shared with the on-disk save
   format (§9/§21.7).

**Answered (2026-08-27 offline / live follow-up):**

7. Prop/furniture TemplateCRC → internal `prop_*` names offline
   (`pk_furniture_catalog.json`, ~808 entries) — §22.
8. High-frequency world TemplateCRCs mostly named (breakable, chest,
   mannequin, sunstoneblock, etc.) — §22.2.
9. One offline **weighted dungeon junk** drop-table format (tag
   `0x4B2D4FB5`, 72-byte entries) — §22.3; **not** enemy loot.
10. Live slime record = AV stats only (`enemy_slime_slimy`); confirmed
    Green Slime drop list by playtest — §21.10.

**Still unsolved** (needs the entity-manager walk / CE Pointer Scan / a
PDB, per §21.9):

- Per-entity template-id naming for the long tail (workaround: the
  adjacent-string trick in §21.8, one island at a time).
- Per-entity positions (transform component, §21.9). Live health anchors
  and `FFFFFFFF` transform positions remain **decoupled** (0 proximity
  matches).
- **Enemy** drop tables as structured data (Server Loot Drop Component).
  Client AV blocks and item-CRC heap scans do not expose them.
- Harvestable ore drop tables on disk (may use a different schema than
  the dungeon weighted pool).

---

## 22. Offline KFC research: entity defs, furniture catalog, drop tables

Work from 2026-08-26/27 on `core_game*.kfc_*` and `server_core*.kfc_*`.
Complements §16 (archive layout) and §21 (live enemies).

### 22.1 Tools

| Tool | Role |
|------|------|
| `kfc0_parse.py` | Parse `KFC0` dir → JSON index (size buckets, offsets) |
| `kfc0_crc_scan.py` | Extract blobs; search high-frequency TemplateCRCs + nearby strings |
| `pk_entity_catalog.py` | Bulk scan small blobs for entity defs → `pk_furniture_catalog.json` |
| `pk_scan_item_hashes.py` | Find item-hash clusters in `server_core.kfc_data` (skip master table) |
| `pk_parse_drop_tables.py` / one-shot parsers | Decode 2520-byte weighted pools |

Archive sizes of interest:

| Archive | Role |
|---------|------|
| `core_game_small.kfc_*` | Many small entity/prop definition blobs |
| `server_core_small.kfc_*` | Server-side counterparts (similar prop set) |
| `server_core.kfc_data` | Item master table (~`0x185A20`), dungeon loot pools (~`0x7AF120`), large component schema tables |

### 22.2 Entity definition blobs (props / furniture)

Typical **server** entity-def size ~1500–2500 bytes. Header pattern:

```text
+0x00  0x0000219F     marker (client may swap first two dwords)
+0x04  0x00401103     flags-like
+0x08  TemplateCRC    u32 LE   (often repeated later in the blob)
…
ASCII component names + internal asset name (prop_*)
```

Examples (confirmed by dump):

| TemplateCRC | Internal name | Notes |
|-------------|---------------|--------|
| `0x0E8731F4` | `prop_sunstoneblock` | high-freq breakable/harvestable (×520 worlds) |
| `0xF0653E24` | `prop_signpost_editable` | sign family |
| `0xD7045302` | `prop_rift_window02_static` | |
| … | `prop_dungeon_container_01_rangerguild` | container |
| … | `prop_ranger_trap_*` | traps |

**Catalog output:** `pk_furniture_catalog.json` — **~808 unique TemplateCRCs**
with `names[]` (`prop_*` / related) and `components[]` stripped of leading
`<`/`>`. Built from `core_game_small` and `server_core_small` (same count).

Common component strings in defs:

```text
Impact Component
Server Loot Drop Component / ServerLootDropSaveData (dCRC, hasDroppedLoot)
WorldBlockingComponent
Entity Base Server / Entity Base Client
EntityConfigComponent
EntityReplicationStateComponent
Client User Editable String Component   (signs)
Static Model Component / Wiggle Component / Occluder Component
Toggle Component / Trigger Receiver / Trigger Sender
Simple BT Host
```

Large **component schema** tables (e.g. server_core entry ~375, ~200 KB)
repeat the same component type names many times; useful as a dictionary,
not as per-entity data.

### 22.3 Offline weighted drop tables (dungeon junk only)

**Fingerprint:** tag **`0x4B2D4FB5`** appears **281** times in
`server_core.kfc_data`, almost all in one run:

```text
0x7AE990 .. 0x7B3808   (~20 KB, ~280 tags)
```

**Layout (locked):**

| Field | Value |
|-------|--------|
| Entry stride | **72 bytes** |
| Table size | **2520 bytes** ≈ 35 × 72 (sometimes 2664 / 2712) |
| Tag | `0x4B2D4FB5` beside most entries |
| Weights | f32, step ≈ **2.857** (100/35), cumulative toward 100 |

Same 35-item pool is repeated ~18 times (identical item list). Content is
**dungeon filler**, not enemy loot:

```text
Minor Healing/Mana Potion, Gray Rat, Torch, Rocket, Water Bomb, Mini Bomb,
Scroll of Teleportation, Bottle(s), Decorative Box, Books, Scroll Bundle,
Pottery Bowl/Urn/Pot(s), Small/Big Bundle, Candles, Skull variants
```

Export: `pk_drop_tables_dungeon.json`.

**Not a global format:** a whole-file search for `0x4B2D4FB5` finds only
this band. Ore / gear / enemy pools do not use this tag.

Item-hash cluster scans (`pk_scan_item_hashes.py`) also hit the **master
item table** (~`0x185A30`, 16-byte stride) and shop/recipe slices; always
filter master runs before treating a cluster as a drop table.

### 22.4 Architecture reminder (disk + RAM)

```text
Entity Manager (registry)          ← still unsolved (§21.9)
  └─ entity slot → entity base
        ├─ Impact / AV component     ← live HP edits work here
        ├─ Transform component       ← static FFFFFFFF pool ≠ live AV
        └─ Server Loot Drop          ← enemy drops; not on client AV
```

Offline KFC gives **type identity** (TemplateCRC → `prop_*`) and one
**chest/prop** weighted pool. Live gives **stats** and **playtest drops**.
Linking a specific world entity instance to its drop rows still requires
the manager.

### 22.5 Enshrouded community (same studio / KFC packing)

Keen Games ships Enshrouded on the **Holistic** engine with the same
`kfc_dir`/`kfc_data` packaging. Public tooling:

| Project | Notes |
|---------|--------|
| [ndoa/kfc-tools](https://github.com/ndoa/kfc-tools) | Extract Enshrouded KFC; hash/GUID blob names |
| [Ekey/KG.Data.Tool](https://github.com/Ekey/KG.Data.Tool) | Unpack/repack; lists **Portal Knights + Enshrouded** |
| [Brabb3l/kfc-parser](https://github.com/Brabb3l/kfc-parser) | Descriptors, EXE reflection extract, **Impact** disasm/asm; EML mod loader |

What they **do**: offline unpack → patch descriptor numbers (loot
multipliers, stacks, auto-loot **templates**) → repack.

What they **do not** publish: a solved entity-manager walk or a decoded
per-enemy drop-table struct. Wiki enemy drops are playtest-sourced, same
as §21.10. Auto-loot mods explicitly skip enemy gear/chests (“different
interact rules”).

**Takeaway for PK:** expect the same ceiling — offline catalogs and
multipliers are tractable; live component graphs for loot are not
documented for either game. Reflection/Impact extraction against
`portal_knights_x64.exe` is a plausible next offline lead (Impact
Component strings already appear in PK entity defs).

### 22.6 Artifact index (this research pass)

```text
pk_furniture_catalog.json       TemplateCRC → prop_* + components (~808)
pk_furniture_server_small.json  same from server_core_small
pk_drop_tables_dungeon.json     weighted dungeon junk pools
drop_clusters.json / other_clusters.json   item-hash cluster scans (noisy)
core_*_kfc0_index.json          KFC0 directory indexes
item_table_merged.json          item hash → name (~3163)
```

---

## Appendix A — Constants from pk_save_editor

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
- sign (`0xF0653E24` / User Editable String variants)
- landing pad (`0x0BCB9932`; surface voxels id 251)
- guitar / trigger pad (`0xDF6A6AFD` — not a sign)
