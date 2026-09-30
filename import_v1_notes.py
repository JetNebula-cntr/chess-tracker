#!/usr/bin/env python3
"""One-off: copy the v1 tracker's game notes and patterns into the v2 database."""
import json, sqlite3, time
from pathlib import Path

DIR = Path(__file__).resolve().parent
block = json.loads((Path.home() / "lichess" / "datablock-2026-09-23.json").read_text())

# Motif tags for the v1 losses and draws, read off each note.
TAGS = {
    "K00GhD3X": ["loose pieces", "endgame slip"],
    "E6CHmg86": ["b1-h7 mate", "pawn grab"],
    "osOT7eWW": ["perpetual allowed", "endgame slip"],
    "dVY6rguP": ["pawn grab", "king exposed"],
    "80cDJ28W": ["offside knight"],
    "K2B7f0Rh": ["f7 fork"],
    "pf7K0931": ["line opened onto own piece"],
    "uRwMmTx6": ["passed pawn ignored", "flagged"],
    "XSOxczp3": ["king exposed", "knight raid"],
    "8lOrNsWj": ["b1-h7 mate"],
    "f2hGBnub": ["pawn grab", "king exposed"],
    "L9YUHzqY": ["back rank / 7th rank"],
    "TojM9o5d": [],
    "Nq5WlwvP": ["line opened onto own piece", "f7 fork"],
}

db = sqlite3.connect(DIR / "jetnebula.db")
now = int(time.time() * 1000)
n = 0
for g in block["games"]:
    if not g.get("id") or not g.get("note"):
        continue
    db.execute("INSERT OR REPLACE INTO notes VALUES (?,?,?,?)",
               (g["id"], g["note"], json.dumps(TAGS.get(g["id"], [])), now))
    n += 1
db.commit()
(DIR / "themes.json").write_text(json.dumps({
    "asOf": "2026-09-22", "source": "v1 tracker",
    "working": block["patterns"]["working"], "watch": block["patterns"]["watch"],
}, indent=1))
print(f"imported {n} notes and {len(block['patterns']['working'])}+{len(block['patterns']['watch'])} themes")
