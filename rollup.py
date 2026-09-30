#!/usr/bin/env python3
"""Build the tracker page's documents from ~/chess/jetnebula.db.

Writes out/docs.json: a list of {"path": ..., "data": ...} documents for the
artifact's db. Closed months become frozen summaries of raw counts (so all-time
figures are sums); the current month keeps one document per game.

    python3 rollup.py              # current month = this calendar month
    python3 rollup.py --as-of 2026-10   # simulate a later month (testing)
"""
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

DIR = Path(__file__).resolve().parent
DB_PATH = DIR / "jetnebula.db"
OUT = DIR / "out" / "docs.json"
CONFIG = DIR / "config.json"
SESSION_GAP_MS = 60 * 60 * 1000
SHORT_MOVES = 20
NOTES_SHOWN = 10


# Same groupings as the original tracker (~/lichess/stats.py) so figures stay comparable.
def opening_family(name):
    name = name or "Unknown"
    if "Philidor" in name:
        return "Philidor"
    if "Stonewall Attack" in name:
        return "Stonewall Attack"
    if name == "Queen's Pawn Game: London System, with e6":
        return "London System"
    if name.startswith("Queen's Pawn Game"):
        return "Queen's Pawn (other)"
    for stem in ("Van't Kruijs", "Indian Defense", "Horwitz", "King's Pawn Game", "Englund"):
        if name.startswith(stem):
            return stem
    return name.split(":", 1)[0]


def wld():
    return {"n": 0, "w": 0, "l": 0, "d": 0}


def add(bucket, result):
    bucket["n"] += 1
    bucket[{"W": "w", "L": "l", "D": "d"}[result]] += 1


def month_range(first, last):
    y, m = map(int, first.split("-"))
    out = []
    while f"{y:04d}-{m:02d}" <= last:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def main():
    as_of = sys.argv[sys.argv.index("--as-of") + 1] if "--as-of" in sys.argv else time.strftime("%Y-%m")
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    games = db.execute("""
        SELECT g.*, a.blunders, a.mistakes, a.inaccuracies, a.acpl, a.first_blunder_move,
               a.turning_move, a.turning_phase,
               (SELECT COUNT(*) FROM moves m WHERE m.game_id = g.id AND m.mine = 1) AS my_moves
        FROM games g LEFT JOIN analysis a ON a.game_id = g.id
        WHERE g.month <= ? ORDER BY g.created_at""", (as_of,)).fetchall()
    notes = {r["game_id"]: r for r in db.execute("SELECT * FROM notes")}

    # Session position and month game number need the whole ordered history.
    prev_last, pos = None, 0
    month_counter = defaultdict(int)
    rows = []
    for g in games:
        pos = 1 if prev_last is None or g["created_at"] - prev_last > SESSION_GAP_MS else pos + 1
        prev_last = g["last_move_at"]
        month_counter[g["month"]] += 1
        rows.append((g, pos, month_counter[g["month"]]))

    months = defaultdict(lambda: {
        "games": 0, "w": 0, "l": 0, "d": 0, "net": 0,
        "start": None, "end": None, "peak": None, "low": None,
        "color": {"white": wld(), "black": wld()},
        "openings": defaultdict(wld),
        "session": defaultdict(wld),
        "short": {**wld(), "net": 0},
        "termination": defaultdict(int),
        "oppBand": defaultdict(wld),
        "engine": {"games": 0, "moves": 0, "blunders": 0, "mistakes": 0, "inaccuracies": 0,
                   "cpl_sum": 0.0, "lossPhase": defaultdict(int), "firstBlunderSum": 0,
                   "firstBlunderGames": 0},
        "motifs": defaultdict(int),
    })

    docs = []
    for g, pos, num in rows:
        m = months[g["month"]]
        r = g["result"]
        m["games"] += 1
        m[{"W": "w", "L": "l", "D": "d"}[r]] += 1
        m["net"] += g["rating_diff"] or 0
        if m["start"] is None:
            m["start"] = g["rating_before"]
        m["end"] = g["rating_after"]
        if not g["provisional"]:
            m["peak"] = max(m["peak"] or 0, g["rating_after"])
            m["low"] = min(m["low"] or 10_000, g["rating_after"])
        add(m["color"][g["color"]], r)
        add(m["openings"][opening_family(g["opening"])], r)
        add(m["session"][str(pos) if pos < 5 else "5+"], r)
        if math.ceil(g["plies"] / 2) < SHORT_MOVES:
            add(m["short"], r)
            m["short"]["net"] += g["rating_diff"] or 0
        m["termination"][g["status"]] += 1
        gap = (g["opp_rating"] or 0) - (g["rating_before"] or 0)
        band = "higher" if gap >= 50 else "lower" if gap <= -50 else "even"
        add(m["oppBand"][band], r)
        if g["blunders"] is not None:
            e = m["engine"]
            e["games"] += 1
            e["moves"] += g["my_moves"]
            e["blunders"] += g["blunders"]
            e["mistakes"] += g["mistakes"]
            e["inaccuracies"] += g["inaccuracies"]
            e["cpl_sum"] += (g["acpl"] or 0) * g["my_moves"]
            if r != "W" and g["turning_phase"]:
                e["lossPhase"][g["turning_phase"]] += 1
            if g["first_blunder_move"]:
                e["firstBlunderSum"] += g["first_blunder_move"]
                e["firstBlunderGames"] += 1
        note = notes.get(g["id"])
        tags = json.loads(note["tags"]) if note and note["tags"] else []
        if r != "W":
            for t in tags:
                m["motifs"][t] += 1

        if g["month"] == as_of:
            docs.append({"path": f"current/{g['id']}", "data": {
                "id": g["id"], "num": num, "createdAt": g["created_at"],
                "color": g["color"], "result": r, "delta": g["rating_diff"],
                "rating": g["rating_after"], "opp": g["opp_name"], "oppRating": g["opp_rating"],
                "opening": g["opening"], "family": opening_family(g["opening"]),
                "moves": math.ceil(g["plies"] / 2), "status": g["status"], "session": pos,
                "blunders": g["blunders"], "mistakes": g["mistakes"], "inaccuracies": g["inaccuracies"],
                "acpl": g["acpl"], "firstBlunder": g["first_blunder_move"],
                "turningMove": g["turning_move"], "turningPhase": g["turning_phase"],
            }})

    # Month summaries, including empty months (rating carried across the gap).
    all_months = month_range(min(months), as_of) if months else []
    last_end = None
    for mk in all_months:
        m = months.get(mk)
        if m is None:
            summary = {"month": mk, "games": 0, "start": last_end, "end": last_end}
        else:
            summary = json.loads(json.dumps(m))  # defaultdicts -> plain dicts
            summary["month"] = mk
            last_end = m["end"]
        summary["closed"] = mk < as_of
        docs.append({"path": f"months/{mk}", "data": summary})

    # The last N annotated games, kept across month boundaries.
    noted = [(g, num) for g, _, num in rows if g["id"] in notes][-NOTES_SHOWN:]
    keep = {g["id"] for g, _ in noted}
    for g, num in noted:
        n = notes[g["id"]]
        docs.append({"path": f"notes/{g['id']}", "data": {
            "id": g["id"], "month": g["month"], "num": num, "createdAt": g["created_at"],
            "color": g["color"], "result": g["result"], "delta": g["rating_diff"],
            "rating": g["rating_after"], "opp": g["opp_name"], "oppRating": g["opp_rating"],
            "opening": g["opening"], "note": n["note"], "tags": json.loads(n["tags"] or "[]"),
            "blunders": g["blunders"], "turningPhase": g["turning_phase"],
        }})

    last = games[-1] if games else None
    settled = [g["rating_after"] for g in games if not g["provisional"]]
    docs.append({"path": "meta/summary", "data": {
        "user": "JetNebula", "perf": "Lichess 10+0 Rapid",
        "goal": cfg.get("goal", 1500), "targetDate": cfg.get("targetDate", "2026-12-31"),
        "current": last["rating_after"] if last else None,
        "peak": max(settled) if settled else None, "low": min(settled) if settled else None,
        "currentMonth": as_of, "games": len(games),
        "lastGameAt": last["created_at"] if last else None,
        "generatedAt": int(time.time() * 1000), "schemaVersion": 1,
        "notesKept": sorted(keep),
    }})
    themes = DIR / "themes.json"
    if themes.exists():
        docs.append({"path": "themes/current", "data": json.loads(themes.read_text())})

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(docs, indent=1))
    print(f"{len(docs)} documents -> {OUT} "
          f"({sum(d['path'].startswith('current/') for d in docs)} current-month games, "
          f"{len(all_months)} months, {len(keep)} notes)")


if __name__ == "__main__":
    main()
