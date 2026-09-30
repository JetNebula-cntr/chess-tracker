#!/usr/bin/env python3
"""Load JetNebula's rated Lichess games into ~/chess/jetnebula.db.

Incremental: fetches only games after the newest one already stored.
Re-running is harmless; games are upserted by Lichess id.

    python3 load.py           # new games since last run
    python3 load.py --full    # re-fetch everything
"""
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

DIR = Path(__file__).resolve().parent
DB_PATH = DIR / "jetnebula.db"
USER = "jetnebula"
PERF = "rapid"
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
  id              TEXT PRIMARY KEY,
  created_at      INTEGER NOT NULL,     -- ms since epoch
  last_move_at    INTEGER,
  month           TEXT NOT NULL,        -- YYYY-MM, local time
  speed           TEXT,
  time_control    TEXT,                 -- e.g. 600+0
  color           TEXT NOT NULL,        -- white | black
  result          TEXT NOT NULL,        -- W | L | D
  status          TEXT,                 -- mate, resign, outoftime, draw, ...
  rating_before   INTEGER,
  rating_after    INTEGER,
  rating_diff     INTEGER,
  provisional     INTEGER DEFAULT 0,
  opp_name        TEXT,
  opp_rating      INTEGER,
  eco             TEXT,
  opening         TEXT,
  opening_family  TEXT,                 -- text before the first ':'
  plies           INTEGER,
  moves_san       TEXT,
  has_lichess_eval INTEGER DEFAULT 0,
  accuracy        INTEGER,              -- Lichess accuracy for my side, if analysed
  raw_json        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS games_month ON games(month);
CREATE INDEX IF NOT EXISTS games_created ON games(created_at);

CREATE TABLE IF NOT EXISTS moves (
  game_id     TEXT NOT NULL REFERENCES games(id),
  ply         INTEGER NOT NULL,         -- 1 = White's first move
  san         TEXT NOT NULL,
  mine        INTEGER NOT NULL,         -- 1 if I played this move
  clock_cs    INTEGER,                  -- clock left after the move, centiseconds
  lichess_cp  INTEGER,                  -- Lichess eval after the move (White POV)
  lichess_mate INTEGER,
  lichess_judgment TEXT,                -- Inaccuracy | Mistake | Blunder
  engine_cp   INTEGER,                  -- filled in later by Stockfish
  engine_mate INTEGER,
  cp_loss     INTEGER,
  judgment    TEXT,
  PRIMARY KEY (game_id, ply)
);

CREATE TABLE IF NOT EXISTS analysis (
  game_id          TEXT PRIMARY KEY REFERENCES games(id),
  engine           TEXT,
  depth            INTEGER,
  inaccuracies     INTEGER,
  mistakes         INTEGER,
  blunders         INTEGER,
  first_blunder_move INTEGER,
  turning_move     INTEGER,
  turning_phase    TEXT,                -- opening | middlegame | endgame
  acpl             REAL,
  analysed_at      INTEGER
);

CREATE TABLE IF NOT EXISTS notes (
  game_id    TEXT PRIMARY KEY REFERENCES games(id),
  note       TEXT,
  tags       TEXT,                      -- JSON array of motif tags
  written_at INTEGER
);

CREATE TABLE IF NOT EXISTS months (
  month      TEXT PRIMARY KEY,          -- YYYY-MM
  closed     INTEGER DEFAULT 0,
  summary    TEXT,                      -- JSON counts
  review     TEXT,
  themes     TEXT,
  closed_at  INTEGER
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def token():
    for line in (DIR / ".env").read_text().splitlines():
        if line.startswith("LICHESS_TOKEN="):
            t = line.split("=", 1)[1].strip()
            if t.startswith("lip_"):
                return t
    sys.exit("No valid LICHESS_TOKEN in .env")


def fetch(since=None):
    params = f"perfType={PERF}&rated=true&opening=true&clocks=true&evals=true&accuracy=true&sort=dateAsc"
    if since:
        params += f"&since={since}"
    req = urllib.request.Request(
        f"https://lichess.org/api/games/user/{USER}?{params}",
        headers={
            "Authorization": f"Bearer {token()}",
            "Accept": "application/x-ndjson",
            "User-Agent": "jetnebula-chess-tracker/2.0 (personal tracker)",
        },
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return [json.loads(l) for l in resp if l.strip()]
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(60)
                continue
            raise


def row_for(g):
    me = "white" if g["players"]["white"].get("user", {}).get("id") == USER else "black"
    opp = "black" if me == "white" else "white"
    p, o = g["players"][me], g["players"][opp]
    winner = g.get("winner")
    result = "D" if winner is None else ("W" if winner == me else "L")
    before = p.get("rating")
    diff = p.get("ratingDiff")
    opening = (g.get("opening") or {}).get("name")
    clock = g.get("clock") or {}
    local = time.localtime(g["createdAt"] / 1000)
    return {
        "id": g["id"],
        "created_at": g["createdAt"],
        "last_move_at": g.get("lastMoveAt"),
        "month": time.strftime("%Y-%m", local),
        "speed": g.get("speed"),
        "time_control": f"{clock.get('initial', 0)}+{clock.get('increment', 0)}" if clock else None,
        "color": me,
        "result": result,
        "status": g.get("status"),
        "rating_before": before,
        "rating_after": before + diff if before is not None and diff is not None else None,
        "rating_diff": diff,
        "provisional": 1 if p.get("provisional") else 0,
        "opp_name": o.get("user", {}).get("name") or o.get("aiLevel") and f"Stockfish L{o['aiLevel']}",
        "opp_rating": o.get("rating"),
        "eco": (g.get("opening") or {}).get("eco"),
        "opening": opening,
        "opening_family": opening.split(":")[0].strip() if opening else None,
        "plies": len(g.get("moves", "").split()),
        "moves_san": g.get("moves"),
        "has_lichess_eval": 1 if g.get("analysis") else 0,
        "accuracy": (p.get("analysis") or {}).get("accuracy"),
        "raw_json": json.dumps(g, separators=(",", ":")),
    }, me


def move_rows(g, me):
    sans = g.get("moves", "").split()
    clocks = g.get("clocks") or []
    evals = g.get("analysis") or []
    my_parity = 1 if me == "white" else 0
    rows = []
    for i, san in enumerate(sans):
        ply = i + 1
        ev = evals[i] if i < len(evals) else {}
        rows.append((
            g["id"], ply, san, 1 if ply % 2 == my_parity else 0,
            clocks[i] if i < len(clocks) else None,
            ev.get("eval"), ev.get("mate"),
            (ev.get("judgment") or {}).get("name"),
        ))
    return rows


def main():
    full = "--full" in sys.argv
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    since = None
    if not full:
        (newest,) = db.execute("SELECT MAX(created_at) FROM games").fetchone()
        since = newest - 60_000 if newest else None  # small overlap; upsert dedupes

    games = [g for g in fetch(since) if g.get("status") != "started"]
    cols = None
    for g in games:
        row, me = row_for(g)
        cols = cols or list(row)
        db.execute(
            f"INSERT OR REPLACE INTO games ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [row[c] for c in cols],
        )
        # Keep any engine columns already computed; refresh the Lichess-sourced ones.
        db.executemany(
            """INSERT INTO moves (game_id, ply, san, mine, clock_cs, lichess_cp, lichess_mate, lichess_judgment)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(game_id, ply) DO UPDATE SET
                 san=excluded.san, mine=excluded.mine, clock_cs=excluded.clock_cs,
                 lichess_cp=excluded.lichess_cp, lichess_mate=excluded.lichess_mate,
                 lichess_judgment=excluded.lichess_judgment""",
            move_rows(g, me),
        )
    db.execute("INSERT OR REPLACE INTO meta VALUES ('last_sync', ?)", (str(int(time.time() * 1000)),))
    db.commit()

    (total,) = db.execute("SELECT COUNT(*) FROM games").fetchone()
    print(f"fetched {len(games)} games (since={since}); {total} in database")


if __name__ == "__main__":
    main()
