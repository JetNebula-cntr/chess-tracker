# Climb to 1500

A rating tracker for JetNebula's Lichess rapid games, with a goal of 1500 by the end of 2026.

Every 3 hours a GitHub Actions job (`.github/workflows/sync.yml`) does the following:

1. `load.py` fetches new rated rapid games into `jetnebula.db` (SQLite).
2. `analyze.py` runs Stockfish over each new game and grades the moves.
3. `rollup.py` builds `docs.json`, which `page/climb.html` reads.
4. The job deploys the page to GitHub Pages.

The database is stored as an asset on the `data` release, not in git. The Lichess token is the `LICHESS_TOKEN` Actions secret.

To run it locally, put `LICHESS_TOKEN=lip_...` in `.env` and Stockfish at `bin/stockfish` (or set `STOCKFISH`), then run `python3 load.py && python3 analyze.py && python3 rollup.py`.
