---
name: happiest-baby
description: Read and write the user's Happiest Baby / SNOO account. Pulls baby tracking data (sleep, breastfeeding, bottle feeding, diapers, solid food, pumping) and summarizes or exports it as CSV; controls the SNOO (start, stop, set level 1-4 or baseline, hold, sticky white noise, status); and logs or deletes journal entries such as diaper changes. Use when the user asks about their baby's sleep, naps, feeds, diapers, or SNOO data, asks to pull or export Happiest Baby data, asks to change or check the SNOO, or asks to log a diaper or feed.
---

# Happiest Baby data

This skill talks to the Happiest Baby (SNOO) API with a refresh token stored in
`credentials.json` in this skill's directory, using two scripts:

- `scripts/happiest-baby-exporter.py` reads journal data (see "Reading data").
- `scripts/happiest-baby-actions.py` controls the SNOO and writes journal entries
  (see "Controlling the SNOO and logging entries"). The user relies on this when the
  Happiest Baby app is not working, so be quick and clear.

If this conversation has the Happiest Baby connector's tools (such as `snoo_status` and
`log_diaper`), use those for SNOO control and logging instead of the scripts. They don't
need code execution or network settings.

## Security rules

- `credentials.json` holds a secret. Never print, `cat`, summarize, or quote its
  contents, and never copy it into outputs. Only pass its path to the script.
- Never ask the user to type their Happiest Baby password into the chat. If
  authentication fails, tell them to rebuild the skill (see "Troubleshooting").

## Reading data

1. Find this skill's directory (the one containing this SKILL.md), referred to as
   `$SKILL_DIR` below.
2. Make sure dependencies are present:
   `pip install --quiet requests click` (skip if already importable).
3. Run the exporter, choosing `--lookback-days` from the user's request
   (default to 3 if they don't say; "today" = 1, "this week" = 7):

   ```bash
   mkdir -p /tmp/happiest-baby
   python3 "$SKILL_DIR/scripts/happiest-baby-exporter.py" \
     --credentials-file "$SKILL_DIR/credentials.json" \
     --lookback-days 3 \
     --output-dir /tmp/happiest-baby
   ```

   Optional flags:
   - `--journal-type sleep,diaper` to fetch only some types
     (breastfeeding, bottlefeeding, diaper, solidfood, pumping, sleep)
   - `--baby-name NAME` if the account has more than one baby (the script exits
     with the list of names if it is needed)
   - `--units metric`, `--timezone <IANA zone>` (default America/New_York)

4. Read the CSVs it wrote:
   - `journals_consolidated.csv` — every entry across types, one row each, sorted by time
     (durations in seconds, amounts in oz unless metric)
   - `journals_sleep.csv` — one row per sleep session (`stateDuration` in seconds)
   - `journals_<type>.csv` — full detail per journal type
   Types with no entries in the window produce no file.

## Controlling the SNOO and logging entries

Install the extra dependencies once: `pip install --quiet requests click paho-mqtt PySocks`.
Every command below starts with:

```bash
python3 "$SKILL_DIR/scripts/happiest-baby-actions.py" \
  --credentials-file "$SKILL_DIR/credentials.json" [--yes] <command>
```

Global options go before the command: `--yes` (skip the confirmation prompt; required
when running non-interactively), `--dry-run` (print the plan, change nothing),
`--timezone <IANA zone>` (default America/New_York).

SNOO commands (add `--device NAME` only if the script says there is more than one SNOO):

- `snoo status`: current level, hold, time left, white noise, safety clip warning
- `snoo start`: start at baseline
- `snoo stop`
- `snoo level {baseline,1,2,3,4} [--hold]`: change level; `--hold` keeps it there
- `snoo white-noise {on,off} [--minutes 15|30|60]`: sound without motion
- `snoo devices`: list SNOOs (also shows each one's `mqtt_host`, see Troubleshooting)

Journal commands (add `--baby-name NAME` only if the script asks for it):

- `journal diaper --pee|--poo [--at TIME] [--note TEXT]`: confirmed to work
- `journal recent --type TYPE [--days 2] [--limit 5]`: raw JSON of recent entries, with ids
- `journal add --type TYPE --data JSON [--at TIME] [--end-at TIME] [--note TEXT]`:
  EXPERIMENTAL, for feeds, pumping, and solid food. First run `journal recent` for that
  type and copy the shape of an existing entry's `data` object exactly (field names,
  units, nesting). If there are no existing entries to copy, tell the user you can't
  log that type reliably yet instead of guessing.
- `journal delete ID`: EXPERIMENTAL; get the id from `journal recent`

TIME is `now` (default), `15m` or `2h` (ago), `HH:MM` (most recent such time, in the
timezone), or ISO 8601.

Rules for anything that changes the SNOO or the journal:

- When the user's message fully specifies the action ("put the SNOO on level 2",
  "log a wet diaper just now"), run it with `--yes`. If you had to infer anything
  (which baby or SNOO, the time, pee vs. poo, the level), or the command is
  `journal add` or `journal delete`, run it with `--dry-run` first, show the user the
  "Plan:" line, and only run with `--yes` after they agree.
- Never retry a write that failed or timed out without first checking state
  (`snoo status` or `journal recent`), so the entry isn't logged twice.
- Report the script's final line plainly ("Done. SNOO is now: LEVEL2, hold on").
  Exit code 3 means the command was sent but the SNOO didn't confirm in time: say so,
  and offer to check `snoo status`. Always repeat a "safety clip not attached" warning.
- Don't change the SNOO's level or hold unless the user asked to; reading status is fine.

## Answering

- Lead with a short summary the user can read on a phone: per-day totals
  (total sleep hours, number of sleeps, longest stretch, feeds and diapers counts),
  then anything notable. Convert seconds to hours/minutes and use local times.
- Note that a sleep session is counted on the day it started, and that the first
  session in the window may be clipped at the window's start.
- If the user wants the files, copy the CSVs to `/mnt/user-data/outputs/` (when that
  directory exists) so they can download them.

## Troubleshooting

- "refresh token is expired or revoked" (a `NotAuthorizedException`): the stored token
  no longer works. Tell the user to run `build_skill.py` from the
  happiest-baby-exporter repo on their computer and re-upload the new zip.
- Other errors (throttling, 5xx, timeouts) are usually temporary: retry once
  before telling the user. Don't tell them to rebuild the skill for these.
- A warning that Cognito "rotated the refresh token" means the stored token will
  stop working soon; tell the user to rebuild the skill when convenient.
- Network errors reaching `cognito-idp.us-east-1.amazonaws.com` or
  `api-us-east-1-prod.happiestbaby.com`: those domains must be allowed in the
  user's Claude settings for code execution network access. Tell the user to add
  them; do not try to work around the restriction.
- SNOO commands failing with "Could not reach <host>", a proxy 403, or a connection
  timeout: the SNOO's AWS IoT host (shown as `mqtt_host` by `snoo devices`, ending in
  `.iot.us-east-1.amazonaws.com`) must also be allowed. Give the user that exact host
  to add. "Connection closed ... the login may have been rejected" usually means the
  token is expired; see the refresh token item above.
