Portal Knights Save Editor

Edit Portal Knights characters, worlds, chests, NPCs, and more.

Needs Python 3 with tkinter (included with most Windows / macOS installs; on Linux install `python3-tk` if needed).
---
Run
Easiest: double-click `pk_save_editor.py`  
(A console window may open behind the GUI — that is normal.)
From a terminal (same folder as the script):
```text
python pk_save_editor.py
```
If `python` is not found, try:
```text
python3 pk_save_editor.py
```
Windows (if double-click does nothing useful):
```text
py -3 pk_save_editor.py
```
---
Install dependencies
Required once:
```text
pip install zstandard
```
Optional (much faster world load / save / scans):
```text
pip install python-snappy
```
If `pip` is not found: `python -m pip install zstandard` or `py -3 -m pip install zstandard`.
The editor still runs without `python-snappy`; it just uses a slower built-in codec.
---
Setup
Normal use — run the script. It can download the item list from GitHub and cache it next to the program.
If you want to add or rename NPCs / furniture / templates — keep a local `pk_templates.json` next to the script. Name changes are saved there (remote download alone is read-only for your custom names).
```text
pk_save_editor.py
pk_templates.json    ← only needed if you edit NPC / prop names
pk_dict.bin          ← optional; extracted from the game if missing
item_table_merged.json  ← optional cache; downloaded if missing
```
---
Before you edit
Back up your save folder.
Fully quit the game (not only the main menu), then edit, then start the game again.
After some world edits the game may still need a full restart to show changes.
Saves are usually under Steam, for example:
```text
…/Steam/userdata/<id>/374040/remote/
```
---
What you can do
Characters — rename, gear, bags, stacks, stats
Worlds — chests, mannequins, map, signs, NPCs
Templates… — names for NPCs, traders, props (writes `pk_templates.json`)
Collect all worlds → here… (NPC dialog) — test tool: copy one of each unique NPC type into the selected world on a safe grid
Log — Copy / Save / Clear at the bottom (nothing is written to disk until you click Save log…)
Note: Character level above 30 can be written in the file, but the game clamps level back to 30 on load. That is a game rule, not the editor.
---
Troubleshooting
Problem	What to try
Won’t start / `No module named zstandard`	`pip install zstandard`
No window / tkinter error (Linux)	`sudo apt install python3-tk`
Worlds look empty / decompress errors	Ensure `pk_dict.bin` can be found or extracted; fully quit the game
Edits don’t show in-game	Quit game completely, edit, start again
More technical detail: format.md
