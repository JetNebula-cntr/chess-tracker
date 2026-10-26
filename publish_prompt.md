Publish pending database writes to the "Climb to 1500" page. The launchd sync job prepares the batches; this needs an interactive Claude session, because the ArtifactData tool is not available to `claude -p` or to routines. Do not change anything except what these steps describe.

Page URL: https://claude.ai/artifact/SeewjxfWQW8SqntxXyUkYT
Working directory: ~/chess

1. Load the ArtifactData tool with ToolSearch ("select:ArtifactData").
2. List the pending batches: `ls ~/chess/out/batches/` — pending ones are named `NN.json` (ignore `*.hashes.json` and `*.done.json`). If there are none, stop.
3. For each pending batch, in ascending order:
   a. Read the file. It is a JSON array of writes.
   b. Call ArtifactData with action "batch", url as above, and `writes` set to that array exactly as written. Do not edit, reorder, or drop entries.
   c. Only if the call reports the batch committed, run `python3 ~/chess/publish.py commit NN`.
   d. If the call fails, stop immediately: do not commit that batch or run later ones. Print the error and exit.
4. Finish with one line: how many batches were published, or the error.

Never read or print ~/chess/.env.
