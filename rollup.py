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
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

import chess
import chess.svg

DIR = Path(__file__).resolve().parent
DB_PATH = DIR / "jetnebula.db"
OUT = DIR / "out" / "docs.json"
BOARDS = DIR / "out" / "boards"
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


SMALL_SAMPLE = 10
MIN_OPENING_GAMES = 8
RECENT = 20


def pct(games):
    """Score as a percentage: wins plus half of draws."""
    if not games:
        return None
    pts = sum(1 if g["result"] == "W" else 0.5 if g["result"] == "D" else 0 for g in games)
    return round(pts / len(games) * 100)


def sample(n):
    return f"{n} games" + (" (small sample, so treat it as a hint)" if n < SMALL_SAMPLE else "")


# --- Boards and game write-ups -------------------------------------------------

BOARD_COLORS = {"square light": "#ECE8F6", "square dark": "#9A8ED6",
                "square light lastmove": "#F3E3B0", "square dark lastmove": "#D2B66A",
                "margin": "#2A2640", "coord": "#ECE8F6"}
PLAYED_ARROW = "#D03A3ACC"
BEST_ARROW = "#22A05ACC"
ENDINGS = {"mate": "checkmate", "resign": "resignation", "outoftime": "time running out",
           "draw": "agreement", "stalemate": "stalemate", "timeout": "time running out"}


def save_board(name, board, white_bottom, arrows=()):
    BOARDS.mkdir(parents=True, exist_ok=True)
    svg = chess.svg.board(board, orientation=chess.WHITE if white_bottom else chess.BLACK,
                          arrows=list(arrows), size=320, colors=BOARD_COLORS)
    (BOARDS / f"{name}.svg").write_text(svg)
    return f"boards/{name}.svg"


def move_label(ply, san):
    n = (ply + 1) // 2
    return f"{n}.{san}" if ply % 2 else f"{n}...{san}"


def move_list(sans):
    return " ".join(f"{(i // 2) + 1}.{s}" if i % 2 == 0 else s for i, s in enumerate(sans))


def win_chance(cp):
    return 2 / (1 + math.exp(-0.00368208 * cp)) - 1


def describe(cp):
    """An evaluation, from my side, in plain words."""
    if cp >= 9000:
        return "a forced mate for you"
    if cp <= -9000:
        return "a forced mate against you"
    p = cp / 100
    if p >= 3:
        return f"winning for you ({p:+.1f})"
    if p >= 1.2:
        return f"better for you ({p:+.1f})"
    if p > -1.2:
        return f"about equal ({p:+.1f})"
    if p > -3:
        return f"worse for you ({p:+.1f})"
    return f"losing for you ({p:+.1f})"


def game_writeup(db, g):
    """Plain-English summary of one game from the engine data, plus a board of its turning point."""
    moves = db.execute("SELECT ply, san, mine, engine_cp, best_san FROM moves WHERE game_id=? ORDER BY ply",
                       (g["id"],)).fetchall()
    if not moves or any(m["engine_cp"] is None for m in moves):
        return None, None, None
    sign = 1 if g["color"] == "white" else -1
    me_side, opp_side = g["color"].capitalize(), ("Black" if g["color"] == "white" else "White")
    before = [20] + [m["engine_cp"] for m in moves[:-1]]  # eval before each ply, White's view

    def swing(i):
        """How much ply i cut the mover's winning chances."""
        mover = 1 if moves[i]["ply"] % 2 else -1
        return win_chance(mover * before[i]) - win_chance(mover * moves[i]["engine_cp"])

    mine = [i for i, m in enumerate(moves) if m["mine"]]
    theirs = [i for i, m in enumerate(moves) if not m["mine"]]
    opening = g["opening"] or "an unnamed opening"
    parts = [f"{opening}, with you as {me_side}."]

    if g["result"] == "W" and theirs:
        key, whose = max(theirs, key=swing), "their"
    elif mine:
        key, whose = max(mine, key=swing), "your"
    else:
        return None, None, None
    m = moves[key]
    if swing(key) < 0.10:
        parts.append("Neither side made a clear error; it was decided by small edges.")
        key = None
    else:
        label = move_label(m["ply"], m["san"])
        was, now = describe(sign * before[key]), describe(sign * m["engine_cp"])
        if whose == "your":
            parts.append(f"The game turned on {label}: the position went from {was} to {now}.")
            if m["best_san"] and m["best_san"] != m["san"]:
                parts.append(f"Stockfish preferred {move_label(m['ply'], m['best_san'])}.")
        else:
            parts.append(f"{opp_side}'s key mistake was {label}, which took the position from {was} to {now}.")
            if m["best_san"] and m["best_san"] != m["san"]:
                parts.append(f"They needed {move_label(m['ply'], m['best_san'])}.")
            slip = max(mine, key=swing) if mine else None
            if slip is not None and swing(slip) >= 0.30:
                parts.append(f"You had your own scare with {move_label(moves[slip]['ply'], moves[slip]['san'])}, "
                             f"but they didn't punish it.")

    blunders = g["blunders"]
    if blunders:
        first = g["first_blunder_move"]
        parts.append(f"You made {blunders} blunder{'s' if blunders != 1 else ''}"
                     + (f", the first on move {first}." if first else "."))
    elif blunders == 0:
        parts.append("You made no blunders.")
    ending = ENDINGS.get(g["status"], g["status"])
    last = (len(moves) + 1) // 2
    result = {"W": "You won", "L": "You lost", "D": "It was drawn"}[g["result"]]
    parts.append(f"{result} by {ending} on move {last}.")

    board = caption = None
    if key is not None:
        pos = chess.Board()
        for mv in moves[:key]:
            pos.push_san(mv["san"])
        played = pos.parse_san(m["san"])
        arrows = [chess.svg.Arrow(played.from_square, played.to_square, color=PLAYED_ARROW)]
        if m["best_san"] and m["best_san"] != m["san"]:
            best = pos.parse_san(m["best_san"])
            arrows.append(chess.svg.Arrow(best.from_square, best.to_square, color=BEST_ARROW))
        board = save_board(f"game-{g['id']}", pos, g["color"] == "white", arrows)
        who = "you" if whose == "your" else "they"
        caption = (f"Before {move_label(m['ply'], m['san'])}. Red is what {who} played"
                   + (", green is what Stockfish preferred." if len(arrows) > 1 else "."))
    return " ".join(parts), board, caption


def opening_board(db, family, games):
    """The family's most common position at the end of its named opening moves."""
    seen = {}
    for g in games:
        raw = json.loads(g["raw_json"])
        ply = (raw.get("opening") or {}).get("ply")
        sans = (g["moves_san"] or "").split()[:ply or 0]
        if not ply or len(sans) < ply:
            continue
        pos = chess.Board()
        for s in sans:
            pos.push_san(s)
        key = pos.board_fen()
        if key not in seen:
            seen[key] = {"count": 0, "sans": sans, "name": raw["opening"]["name"], "white": 0}
        seen[key]["count"] += 1
        seen[key]["white"] += g["color"] == "white"
    if not seen:
        return None, None
    top = max(seen.values(), key=lambda v: v["count"])
    pos = chess.Board()
    for s in top["sans"]:
        pos.push_san(s)
    slug = "".join(c if c.isalnum() else "-" for c in family.lower()).strip("-")
    path = save_board(f"opening-{slug}", pos, top["white"] * 2 >= top["count"])
    return path, f"{top['name']}: {move_list(top['sans'])}"


def day_label(ms):
    return time.strftime("%b %-d", time.localtime(ms / 1000))


def build_themes(db, rows):
    """Patterns computed from the whole game history, rebuilt on every run.

    Each candidate gets a weight (roughly: points of score difference, scaled by
    how much evidence is behind it); the strongest few go in each column.
    """
    games = [g for g, _, _ in rows]
    working, watch = [], []
    if len(games) < 10:
        return {"asOf": time.strftime("%Y-%m-%d"), "source": "calculated", "working": [], "watch": []}
    overall = pct(games)

    def add(col, weight, text, board=None, caption=None):
        item = {"text": text}
        if board:
            item.update(board=board, caption=caption)
        col.append((weight, item))

    # Colour.
    white = [g for g in games if g["color"] == "white"]
    black = [g for g in games if g["color"] == "black"]
    w, b = pct(white), pct(black)
    if w is not None and b is not None and abs(w - b) >= 5:
        strong, weak = (("White", w, len(white)), ("Black", b, len(black))) if w > b else (("Black", b, len(black)), ("White", w, len(white)))
        add(working, abs(w - b), f"<b>{strong[0]} is your stronger side.</b> {strong[1]}% as {strong[0]} across {sample(strong[2])}, "
            f"against {weak[1]}% as {weak[0]} across {weak[2]}.")
        add(watch, abs(w - b), f"<b>{weak[0]} is where more of the losses are.</b> {weak[1]}% as {weak[0]}, "
            f"{strong[1] - weak[1]} points behind your {strong[0]} games ({weak[2]} games).")

    # Openings, by family.
    fam = defaultdict(list)
    for g in games:
        fam[opening_family(g["opening"])].append(g)
    fam = {k: v for k, v in fam.items() if len(v) >= MIN_OPENING_GAMES}
    if fam:
        best = max(fam, key=lambda k: pct(fam[k]))
        worst = min(fam, key=lambda k: pct(fam[k]))
        bs, ws = pct(fam[best]), pct(fam[worst])
        if bs - overall >= 5:
            add(working, (bs - overall) * min(1, len(fam[best]) / 20),
                f"<b>{best} is your best-scoring opening.</b> {bs}% across {sample(len(fam[best]))}, against your {overall}% overall.",
                *opening_board(db, best, fam[best]))
        if overall - ws >= 5 and worst != best:
            add(watch, (overall - ws) * min(1, len(fam[worst]) / 20),
                f"<b>{worst} is your weakest opening.</b> {ws}% across {sample(len(fam[worst]))}, against your {overall}% overall. "
                f"Worth studying or swapping out.",
                *opening_board(db, worst, fam[worst]))

    # Session position.
    slots = defaultdict(list)
    for g, pos, _ in rows:
        slots[str(pos) if pos < 5 else "5+"].append(g)
    early = slots["1"] + slots["2"] + slots["3"] + slots["4"]
    late = slots["5+"]
    if late and early:
        e, l = pct(early), pct(late)
        if e - l >= 8:
            add(watch, (e - l) * min(1, len(late) / 15),
                f"<b>Long sessions cost you.</b> {l}% from the fifth game of a session on ({sample(len(late))}), "
                f"against {e}% in games one to four. Stopping after four is the easy fix.")
    if slots["1"] and pct(slots["1"]) - overall >= 4:
        add(working, (pct(slots["1"]) - overall) * min(1, len(slots["1"]) / 30),
            f"<b>Your first game of a session is your best.</b> {pct(slots['1'])}% across {sample(len(slots['1']))}.")

    # Short games.
    short = [g for g in games if math.ceil(g["plies"] / 2) < SHORT_MOVES]
    if len(short) >= 5:
        s, net = pct(short), sum(g["rating_diff"] or 0 for g in short)
        if s < overall - 5 and net < 0:
            add(watch, (overall - s) * min(1, len(short) / 20),
                f"<b>Games that end before move {SHORT_MOVES} are costly.</b> {len(short)} of them, scoring {s}% "
                f"for a net of {net:+d} rating points. Most early losses come from one missed tactic, so slow down in the opening.")

    # Recent form.
    if len(games) >= RECENT * 2:
        last, prev = games[-RECENT:], games[-RECENT * 2:-RECENT]
        lp, pp = pct(last), pct(prev)
        start = last[0]["rating_before"]
        end = last[-1]["rating_after"]
        if start is not None and end is not None:
            change = end - start
            line = f"{lp}% over your last {RECENT} games ({change:+d} rating), against {pp}% in the {RECENT} before."
            if lp - pp >= 8 or change >= 25:
                add(working, max(lp - pp, change / 3), f"<b>Your recent form is up.</b> {line}")
            elif pp - lp >= 8 or change <= -25:
                add(watch, max(pp - lp, -change / 3), f"<b>Your recent form has dipped.</b> {line}")

    # Higher-rated opponents.
    higher = [g for g in games if (g["opp_rating"] or 0) - (g["rating_before"] or 0) >= 50]
    if len(higher) >= 8 and pct(higher) >= 45:
        add(working, (pct(higher) - 40) * min(1, len(higher) / 20),
            f"<b>You hold up against stronger players.</b> {pct(higher)}% against opponents rated 50+ above you ({sample(len(higher))}).")

    # Engine: blunder trend and where losses turn.
    analysed = [g for g in games if g["blunders"] is not None]
    if len(analysed) >= 30:
        recent = analysed[-RECENT:]
        r_avg = sum(g["blunders"] for g in recent) / len(recent)
        all_avg = sum(g["blunders"] for g in analysed) / len(analysed)
        if r_avg <= all_avg * 0.8:
            add(working, (1 - r_avg / all_avg) * 30,
                f"<b>You are blundering less.</b> {r_avg:.1f} blunders a game over your last {len(recent)} games, "
                f"down from {all_avg:.1f} across all {len(analysed)} analysed games.")
        elif r_avg >= all_avg * 1.2:
            add(watch, (r_avg / all_avg - 1) * 30,
                f"<b>Blunders have crept up.</b> {r_avg:.1f} a game over your last {len(recent)} games, "
                f"against {all_avg:.1f} across all {len(analysed)} analysed games.")
        turned = [g["turning_phase"] for g in analysed if g["result"] != "W" and g["turning_phase"]]
        if len(turned) >= 10:
            top = max(set(turned), key=turned.count)
            share = round(turned.count(top) / len(turned) * 100)
            if share >= 45:
                example = next((g for g in reversed(analysed)
                                if g["result"] != "W" and g["turning_phase"] == top), None)
                board = caption = None
                if example is not None:
                    _, board, caption = game_writeup(db, example)
                    if caption:
                        caption = f"Example from {day_label(example['created_at'])} vs {example['opp_name']}. {caption}"
                add(watch, share - 33,
                    f"<b>Most losses turn in the {top}.</b> In {share}% of your losses and draws ({len(turned)} games), "
                    f"the single most costly move came in the {top}.", board, caption)

    # Finishing with mate.
    mates = [g for g in games if g["result"] == "W" and g["status"] == "mate"]
    wins = [g for g in games if g["result"] == "W"]
    if wins and len(mates) / len(wins) >= 0.3:
        add(working, len(mates) / len(wins) * 20,
            f"<b>You finish attacks.</b> {len(mates)} of your {len(wins)} wins ended in checkmate rather than resignation or time.")

    def top(col):
        return [item for _, item in sorted(col, key=lambda x: -x[0])[:5]]

    return {"asOf": time.strftime("%Y-%m-%d"), "source": "calculated", "games": len(games),
            "working": top(working), "watch": top(watch)}


def main():
    as_of = sys.argv[sys.argv.index("--as-of") + 1] if "--as-of" in sys.argv else time.strftime("%Y-%m")
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}

    db = sqlite3.connect(DB_PATH)
    shutil.rmtree(BOARDS, ignore_errors=True)
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

    # The last N games played, with a note where one exists.
    for g, _, num in rows[-NOTES_SHOWN:]:
        n = notes.get(g["id"])
        writeup, board, caption = game_writeup(db, g)
        docs.append({"path": f"recent/{g['id']}", "data": {
            "id": g["id"], "month": g["month"], "num": num, "createdAt": g["created_at"],
            "color": g["color"], "result": g["result"], "delta": g["rating_diff"],
            "rating": g["rating_after"], "opp": g["opp_name"], "oppRating": g["opp_rating"],
            "opening": g["opening"], "note": n["note"] if n else None,
            "writeup": writeup, "board": board, "caption": caption,
            "tags": json.loads(n["tags"] or "[]") if n else [],
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
    docs.append({"path": "themes/current", "data": build_themes(db, rows)})

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(docs, indent=1))
    print(f"{len(docs)} documents -> {OUT} "
          f"({sum(d['path'].startswith('current/') for d in docs)} current-month games, "
          f"{len(all_months)} months, {len(keep)} notes)")


if __name__ == "__main__":
    main()
