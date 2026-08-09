Portal Knights Save Editor
GUI tool for viewing and safely editing Portal Knights (Steam AppID 374040) cloud saves: characters, universes, and worlds.
```text
python pk_save_editor.py
```
Keep these files next to the script:
File	Role
`pk_save_editor.py`	This program
`pk_dict.bin`	Zstd dictionary (auto-extracted from the game if missing)
`item_table_merged.json`	Item names / categories (hashes are authoritative)
`pk_templates.json`	Optional TemplateCRC → name map (NPCs, traders, props)
`format.md`	Full technical format reference
Requires: Python 3, `tkinter`, `pip install zstandard`.
---
What it does
Finds saves under Steam Cloud / local / Guest folders
Backs up before writes (`.bak`)
Characters — rename, multi-tab editor (armor, vanity, pets, stats, backpack, hotbar, recipes)
Universes — slot headers, island counts
Worlds — location names, chest/mannequin inventories, map, NPCs, signs, landing pads
Templates… — name TemplateCRCs (NPC / trader / quest / enemy / world) with optional island
Technical details (KSC1 layout, BSON types, CRC64, compression) are in format.md.
---
Quick start
Install dependency: `pip install zstandard`
Put `pk_save_editor.py` and `item_table_merged.json` in a folder (add `pk_dict.bin` if you already have it)
Run `python pk_save_editor.py`
Use Refresh list if files were added while the tool is open
Fully quit the game before loading a world or character you just edited (main menu is not enough — the client keeps data in memory)
---
Characters
File is always `0100000000000000` (all slots). The game’s backup is `0200000000000000`.
Rename, edit equipment / bags / stacks / stats from the Character tabs.
Level is capped at 30 by the game (higher values are reset on load).
Stack count (`SC`) hard cap is 65535. Prefer reasonable values.
Changing an item in a slot
Use Change item… in a chest or bag. That writes a new item hash (`II`) into the save.
Item table hashes are treated as correct — the editor does not rebind or rewrite hashes in `item_table_merged.json`. Only labels/categories in that JSON are maintained offline if you edit the file by hand. Prices in the table may still be imperfect; they are display-only.
---
Worlds
World files are `04…` with a location code in the name (e.g. `0x401` = Portal Knight’s Sanctuary). See the location table in `format.md`.
Tool	Purpose
Inventories…	Chests, mannequins, trader stock — edit items/stacks
Map	Top-down markers; toggle NPCs / chests / signs / pads
NPCs / spawns…	List TemplateCRCs; replace one template; bulk-assign from a list; tag island
Signs…	Edit sign text
NPC identification workflow
Open a rich NPC island → NPCs / spawns… → Copy NPC templates… (NPC Control only)
Open a sparse world with enough NPC slots → Bulk assign from list…
Fully quit the game → load world → walk the grid and name unknowns
Record names in Templates… or `pk_templates.json` (`kind`: `npc` / `trader` / `quest`, optional `island`)
Landing pads are detected by `Server LandingPad Component` or TemplateCRC `0x0BCB9932`.
Trader stock is roughly IBP > 40 slots — not a normal player chest.
---
Safety
Always keep backups — especially character (`0100…`) and universe (`0300…`) files.
Do not invent item hashes or TemplateCRCs; use known values.
Fully quit the game after file edits before loading that save.
Currency-like fields: avoid sentinel values near `0xFFFFFFFF`; keep coins at or below about 999,999,999.
Universe slot is fixed by the filename; display rename does not change the slot.
Writes rebuild the KSC1 container and verify header/data CRC64 on the main paths. Semantic mistakes (invalid item, bad stack on equipment) can still be rejected by the game even when CRCs are valid.
---
File types (summary)
Prefix	Meaning
`00…`	Options / system
`01…`	Characters (all slots)
`02…`	Character backup
`03…N`	Universe slot N
`04…`	World / island
`06…`	Misc system
32-hex names are Steam community / shared content.
---
Related docs
format.md — KSC1, compression, custom BSON (float32 type `0x01`), character/world structures, item table notes, asset research, warnings
