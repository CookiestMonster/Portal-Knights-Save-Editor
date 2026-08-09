# Portal Knights Save Editor

Edit Portal Knights characters, worlds, chests, NPCs, and more.

Double-click `pk_save_editor.py` to open it (a command window may appear behind the app).

If it fails to start, install the one dependency once:

```text
pip install zstandard
```

Needs Python 3 (with `tkinter`).

---

## Setup

**Normal use** — just run the script. It will download the item list from GitHub and cache it next to the program.

**If you want to add or rename NPCs / furniture / templates** — download the program **and** keep a local `pk_templates.json` next to it. All your name changes are saved there.

```text
pk_save_editor.py
pk_templates.json    ← only needed if you edit NPC / prop names
pk_dict.bin          ← optional; extracted from the game if missing
```

---

## Before you edit

1. **Back up** your save folder.
2. **Fully quit** the game (not only main menu), then edit, then start the game again.

---

## What you can do

- **Characters** — rename, gear, bags, stacks, stats  
- **Worlds** — chests, mannequins, map, signs, NPCs  
- **Templates…** — names for NPCs, traders, props (writes `pk_templates.json`)

More technical detail in: [format.md](format.md)
