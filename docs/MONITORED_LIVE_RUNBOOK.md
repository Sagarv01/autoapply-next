# Monitored live batch runbook

Use this before any unattended live batch. Two terminals. The app does the
submitting; this runbook is the human-eye verification that the persistence
chain, the verifier, and the cross-run guard all work against a real
submission. The script in `scripts/watch_live_batch.py` is read-only on
`jobs.db` and never imports anything that can submit.

Outcome of a successful monitored run: for every URL you submit, the row
in `jobs.db` transitions `queued -> in_progress -> applied` (or
`submitted_uncertain` if the verifier couldn't confirm; the script flags
that explicitly) within a few seconds of the verifier returning, and the
cross-run eligibility check at the end shows PASS.

## Prereqs

- No app instance running. `ps -ef | grep -E "python.*autoapply_next" | grep -v grep` returns nothing.
- No stale Chrome holding the engine session. `ps -ef | grep "seek_chrome_profile" | grep -v grep | wc -l` returns 0. If it doesn't, find the parent pytest/playwright PID and `kill` it.
- A valid Seek session at `engine_workdir/sessions/seek_chrome_profile/`. If you're not sure, launch the app, open the **Seek session** screen, and click **Re-check existing session** before quitting.
- One or two carefully-picked Seek quick-apply URLs that you actually intend to apply to. **Read the JD first.** This is the first real submission since the verifier fix; pick well.

## Step 1: pick the URLs

Open the app, scrape and prepare a small batch, eyeball the cover letter
and screening Q&A in the Results-style preview pane on the Batch screen.
**Do not click Submit yet.** Note the 1 or 2 URLs you are going to submit
(canonical form is `https://au.seek.com/job/<digits>`, no query string,
no `/apply`).

## Step 2: start the watcher (terminal 1)

```bash
cd /Users/sagarverma/Pictures/Claude-experiments/autoapply-next
PYTHONPATH=src .venv/bin/python scripts/watch_live_batch.py \
    --interval 2 \
    https://au.seek.com/job/<id-1> \
    https://au.seek.com/job/<id-2>
```

It will print a baseline line per URL (`status='queued'`) and then sit
quietly until something changes. Leave it running for the whole batch.

If `$AUTOAPPLY_NEXT_ENGINE_WORKDIR` is set in your environment, the script
honors it; otherwise it uses the platform default
(`~/Library/Application Support/AutoApply Next/engine` on macOS unless you
override). Pass `--workdir <path>` to be explicit.

## Step 3: submit the batch (terminal 2)

Launch the app, navigate to **Settings**, tick "I understand. Allow real
submission." (confirm #1), go to **Batch**, tick the rows you decided in
step 1, click **Submit N (LIVE)** (confirm #2 spells out the count and
mode). Watch the Run progress in the app AND the watcher in terminal 1.

For each URL you should see, in terminal 1:

```
[ts] TRANSITION 'queued' -> 'in_progress'   ...
[ts] TRANSITION 'in_progress' -> 'applied'  ...
```

The gap between the two transitions is the apply form fill + the verifier
poll window (about 30 to 90 seconds end-to-end for a typical job). If the
second transition is `submitted_uncertain` instead of `applied`, that is
the verifier's "I clicked submit but I could not confirm" state; do not
panic, do not re-queue it, manually open
https://au.seek.com/my-activity/applied-jobs and check whether the
application is there.

## Step 4: untick the gate

Back in the app: Settings, untick the box (no confirmation needed for the
off path). Status bar badge goes green DRY-RUN. Do this BEFORE running
anything else.

## Step 5: cross-run-guard confirmation (terminal 1)

After all the URLs you submitted have settled (no transitions for 30 s),
stop the watcher (Ctrl+C) and run the one-shot eligibility check:

```bash
PYTHONPATH=src .venv/bin/python scripts/watch_live_batch.py \
    --check-eligibility \
    https://au.seek.com/job/<id-1> \
    https://au.seek.com/job/<id-2>
```

Expected output ends with:

```
[ts] PASS: none of the N url(s) are eligible
[ts] the next batch prepare will NOT include them; cross-run guard holds
```

Exit code is 0 on PASS, 2 on FAIL. If FAIL: do not run another batch.
Open the Results screen and inspect each URL by hand; the persistence
chain didn't write what it should have, and a future batch would
re-submit. Surface to the engineer.

## Step 6: open Seek's Applied Jobs page

In your normal browser:
`https://au.seek.com/my-activity/applied-jobs`. Each URL you just
submitted should appear as a card. Match by hand to be sure. This is the
ultimate ground truth.

## What success looks like (the green light to walk away)

- Each URL: `queued -> in_progress -> applied` in the watcher.
- One or zero `submitted_uncertain` (manually verified on Seek if so).
- `--check-eligibility` returns PASS, exit code 0.
- Seek's Applied Jobs page shows each card.

After this confirmation, the resilience layer from ADR-0007 + ADR-0008
is empirically validated end-to-end and unattended batches are in scope.
