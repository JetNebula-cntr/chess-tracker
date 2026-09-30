#!/usr/bin/env python3
"""Run Stockfish over games in ~/chess/jetnebula.db that have no analysis yet.

Resumable: commits after every game, so it can be stopped and restarted.

    python3 analyze.py                # analyse every unanalysed game
    python3 analyze.py --limit 5      # just a few (for testing)
    python3 analyze.py --redo         # re-analyse everything (e.g. new depth)

Judgments follow Lichess: a move is graded by how much it drops my
winning chances (not raw centipawns), so a slip in an already-won or
already-lost position is not over-counted.
    inaccuracy >= 0.10, mistake >= 0.20, blunder >= 0.30  (on a -1..1 scale)
"""
import math
import sqlite3
import sys
import time
from pathlib import Path

import chess
import chess.engine

DIR = Path(__file__).resolve().parent
DB_PATH = DIR / "jetnebula.db"
ENGINE_PATH = DIR / "bin" / "stockfish"
DEPTH = 14
THREADS = 6
HASH_MB = 256
CP_CAP = 1000
MATE_CP = 10_000
OPENING_LAST_MOVE = 15      # moves 1-15 count as the opening
ENDGAME_MATERIAL = 26       # both sides' non-pawn material (N/B=3, R=5, Q=9) at or below this

PIECE_VALUE = {chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}


def win_chance(cp):
    """Lichess's winning-chances curve, -1..1."""
    return 2 / (1 + math.exp(-0.00368208 * cp)) - 1


def grade(drop):
    if drop >= 0.30:
        return "Blunder"
    if drop >= 0.20:
        return "Mistake"
    if drop >= 0.10:
        return "Inaccuracy"
    return None


def mate_state(cp_me):
    """+1 if I have a forced mate, -1 if I'm getting mated, else 0."""
    if cp_me >= MATE_CP - 500:
        return 1
    if cp_me <= -(MATE_CP - 500):
        return -1
    return 0


def phase(board, fullmove):
    if fullmove <= OPENING_LAST_MOVE:
        return "opening"
    material = sum(PIECE_VALUE.get(p.piece_type, 0) for p in board.piece_map().values())
    return "endgame" if material <= ENDGAME_MATERIAL else "middlegame"


def evaluate(engine, board):
    """Score from White's point of view: (cp, mate) with cp mate-adjusted."""
    info = engine.analyse(board, chess.engine.Limit(depth=DEPTH))
    score = info["score"].white()
    return score.score(mate_score=MATE_CP), score.mate()


def analyse_game(engine, db, game_id, color, moves_san):
    me_white = color == "white"
    board = chess.Board()
    sign = 1 if me_white else -1

    cp_before, _ = evaluate(engine, board)
    move_updates = []
    my_moves = []  # (fullmove, drop, cp_loss, judgment, phase)

    for ply, san in enumerate(moves_san.split(), start=1):
        mover_is_me = (ply % 2 == 1) == me_white
        fullmove = board.fullmove_number
        ph = phase(board, fullmove)
        board.push_san(san)
        if board.is_checkmate():
            cp_after, mate_after = (MATE_CP if board.turn == chess.BLACK else -MATE_CP), 0
        elif board.is_game_over():
            cp_after, mate_after = 0, None
        else:
            cp_after, mate_after = evaluate(engine, board)

        cp_loss = judgment = None
        if mover_is_me:
            before_me = max(-CP_CAP, min(CP_CAP, sign * cp_before))
            after_me = max(-CP_CAP, min(CP_CAP, sign * cp_after))
            cp_loss = max(0, before_me - after_me)
            drop = win_chance(sign * cp_before) - win_chance(sign * cp_after)
            judgment = grade(drop)
            # Lichess's mate rules: allowing a forced mate, or letting one slip,
            # is graded by how good the position was on the other side of it.
            was, now = mate_state(sign * cp_before), mate_state(sign * cp_after)
            if now == -1 and was != -1:
                judgment = "Inaccuracy" if before_me < -999 else "Mistake" if before_me < -700 else "Blunder"
                drop = max(drop, 0.30 if judgment == "Blunder" else 0.20 if judgment == "Mistake" else 0.10)
            elif was == 1 and now != 1:
                judgment = "Inaccuracy" if after_me > 999 else "Mistake" if after_me > 700 else "Blunder"
                drop = max(drop, 0.30 if judgment == "Blunder" else 0.20 if judgment == "Mistake" else 0.10)
            my_moves.append((fullmove, drop, cp_loss, judgment, ph))

        move_updates.append((cp_after, mate_after, cp_loss, judgment, game_id, ply))
        cp_before = cp_after

    db.executemany(
        "UPDATE moves SET engine_cp=?, engine_mate=?, cp_loss=?, judgment=? WHERE game_id=? AND ply=?",
        move_updates,
    )

    counts = {j: sum(1 for m in my_moves if m[3] == j) for j in ("Inaccuracy", "Mistake", "Blunder")}
    first_blunder = next((m[0] for m in my_moves if m[3] == "Blunder"), None)
    worst = max(my_moves, key=lambda m: m[1], default=None)
    acpl = sum(m[2] for m in my_moves) / len(my_moves) if my_moves else None
    db.execute(
        """INSERT OR REPLACE INTO analysis
           (game_id, engine, depth, inaccuracies, mistakes, blunders, first_blunder_move,
            turning_move, turning_phase, acpl, analysed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (game_id, "Stockfish 19", DEPTH, counts["Inaccuracy"], counts["Mistake"], counts["Blunder"],
         first_blunder,
         worst[0] if worst and worst[1] >= 0.10 else None,
         worst[4] if worst and worst[1] >= 0.10 else None,
         round(acpl, 1) if acpl is not None else None, int(time.time() * 1000)),
    )
    db.commit()
    return counts, len(my_moves)


def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    redo = "--redo" in sys.argv

    db = sqlite3.connect(DB_PATH)
    where = "" if redo else "WHERE id NOT IN (SELECT game_id FROM analysis)"
    todo = db.execute(f"SELECT id, color, moves_san FROM games {where} ORDER BY created_at").fetchall()
    if limit:
        todo = todo[:limit]
    print(f"{len(todo)} games to analyse at depth {DEPTH}", flush=True)

    engine = chess.engine.SimpleEngine.popen_uci(str(ENGINE_PATH))
    engine.configure({"Threads": THREADS, "Hash": HASH_MB})
    start = time.time()
    try:
        for i, (gid, color, moves) in enumerate(todo, 1):
            t = time.time()
            counts, n = analyse_game(engine, db, gid, color, moves or "")
            done = time.time() - start
            eta = done / i * (len(todo) - i)
            print(f"[{i}/{len(todo)}] {gid} {n} moves in {time.time() - t:.0f}s  "
                  f"B{counts['Blunder']} M{counts['Mistake']} I{counts['Inaccuracy']}  "
                  f"eta {eta / 60:.0f}m", flush=True)
    finally:
        engine.quit()
    print(f"done in {(time.time() - start) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
