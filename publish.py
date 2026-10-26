#!/usr/bin/env python3
"""Plan the writes that bring the Climb to 1500 page's db in line with out/docs.json.

The page only reads its db, so the ledger (out/published.json) of what was last
published, and at which version, stays accurate between runs. Only documents
that changed are written; documents no longer produced (last month's per-game
docs, notes that rolled off) are deleted.

    python3 publish.py plan            # write out/batches/NN.json, print how many
    python3 publish.py commit NN       # record batch NN as applied
    python3 publish.py reset FILE      # rebuild the ledger from {"coll/id": version}

Each batch is the `writes` argument for one ArtifactData "batch" call.
"""
import hashlib
import json
import shutil
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent
DOCS = DIR / "out" / "docs.json"
DOC_DIR = DIR / "out" / "docs"
BATCH_DIR = DIR / "out" / "batches"
LEDGER = DIR / "out" / "published.json"
BATCH_SIZE = 50


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def load_ledger():
    return json.loads(LEDGER.read_text()) if LEDGER.exists() else {}


def save_ledger(ledger):
    LEDGER.write_text(json.dumps(ledger, indent=1, sort_keys=True))


def plan():
    docs = {d["path"]: d["data"] for d in json.loads(DOCS.read_text())}
    ledger = load_ledger()

    shutil.rmtree(DOC_DIR, ignore_errors=True)
    shutil.rmtree(BATCH_DIR, ignore_errors=True)
    BATCH_DIR.mkdir(parents=True)

    writes = []
    for path, data in sorted(docs.items()):
        h = digest(data)
        known = ledger.get(path)
        if known and known["h"] == h:
            continue
        f = DOC_DIR / f"{path}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data))
        coll, doc_id = path.split("/")
        w = {"op": "set", "collection": coll, "doc_id": doc_id, "file_path": str(f)}
        if known:
            w["if_version"] = known["v"]
        writes.append((w, h))
    for path in sorted(set(ledger) - set(docs)):
        coll, doc_id = path.split("/")
        writes.append(({"op": "delete", "collection": coll, "doc_id": doc_id,
                        "if_version": ledger[path]["v"]}, None))

    n = 0
    for i in range(0, len(writes), BATCH_SIZE):
        chunk = writes[i:i + BATCH_SIZE]
        n += 1
        (BATCH_DIR / f"{n:02d}.json").write_text(json.dumps([w for w, _ in chunk], indent=1))
        (BATCH_DIR / f"{n:02d}.hashes.json").write_text(json.dumps([h for _, h in chunk]))
    sets = sum(w["op"] == "set" for w, _ in writes)
    print(f"{n} batches ({sets} sets, {len(writes) - sets} deletes)" if n else "nothing to publish")


def commit(name):
    writes = json.loads((BATCH_DIR / f"{name}.json").read_text())
    hashes = json.loads((BATCH_DIR / f"{name}.hashes.json").read_text())
    ledger = load_ledger()
    for w, h in zip(writes, hashes):
        path = f"{w['collection']}/{w['doc_id']}"
        if w["op"] == "delete":
            ledger.pop(path, None)
        else:
            ledger[path] = {"v": w.get("if_version", 0) + 1, "h": h}
    save_ledger(ledger)
    (BATCH_DIR / f"{name}.json").rename(BATCH_DIR / f"{name}.done.json")
    print(f"batch {name} recorded; {len(ledger)} documents in ledger")


def reset(versions_file):
    # Hashes are unknown after a reset, so the next plan rewrites every document once.
    versions = json.loads(Path(versions_file).read_text())
    save_ledger({p: {"v": v, "h": None} for p, v in versions.items()})
    print(f"ledger rebuilt with {len(versions)} documents")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "plan":
        plan()
    elif cmd == "commit" and len(sys.argv) == 3:
        commit(sys.argv[2])
    elif cmd == "reset" and len(sys.argv) == 3:
        reset(sys.argv[2])
    else:
        sys.exit(__doc__)
