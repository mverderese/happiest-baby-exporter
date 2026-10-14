---
name: happiest-baby
description: Pull baby tracking data (sleep, breastfeeding, bottle feeding, diapers, solid food, pumping) from the user's Happiest Baby / SNOO account and summarize it or export it as CSV. Use when the user asks about their baby's sleep, naps, feeds, diapers, or SNOO data, or asks to pull or export Happiest Baby data.
---

# Happiest Baby data

This skill fetches the user's journal data from the Happiest Baby (SNOO) API using
`scripts/happiest-baby-exporter.py` and a refresh token stored in `credentials.json`,
both in this skill's directory.

## Security rules

- `credentials.json` holds a secret. Never print, `cat`, summarize, or quote its
  contents, and never copy it into outputs. Only pass its path to the script.
- Never ask the user to type their Happiest Baby password into the chat. If
  authentication fails, tell them to rebuild the skill (see "Troubleshooting").

## Running

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

## Answering

- Lead with a short summary the user can read on a phone: per-day totals
  (total sleep hours, number of sleeps, longest stretch, feeds and diapers counts),
  then anything notable. Convert seconds to hours/minutes and use local times.
- Note that a sleep session is counted on the day it started, and that the first
  session in the window may be clipped at the window's start.
- If the user wants the files, copy the CSVs to `/mnt/user-data/outputs/` (when that
  directory exists) so they can download them.

## Troubleshooting

- `NotAuthorizedException` / "refresh token is expired or revoked": the stored token
  no longer works. Tell the user to run `build_skill.py` from the
  happiest-baby-exporter repo on their computer and re-upload the new zip.
- Network errors reaching `cognito-idp.us-east-1.amazonaws.com` or
  `api-us-east-1-prod.happiestbaby.com`: those domains must be allowed in the
  user's Claude settings for code execution network access. Tell the user to add
  them; do not try to work around the restriction.
