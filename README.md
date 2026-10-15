# Happiest Baby Exporter

Export your journal data from the Happiest Baby API to CSV files.

This tool allows you to download and back up your baby's data from Happiest Baby (SNOO) in a structured CSV format for analysis, record-keeping, or migration to other tools.

## Features

- **Multiple journal types**: Exports breastfeeding, bottlefeeding, diaper, solid food, pumping, and sleep data
- **Flexible date ranges**: Specify how many days of data to export (default: 14 days)
- **Sleep tracking**: Provides both detailed and consolidated sleep session data
- **Unit selection**: Choose between imperial or metric measurements
- **Easy authentication**: Uses your Happiest Baby email/password credentials
- **Multiple babies**: Automatically handles accounts with multiple babies

## Installation

### Install uv

This script uses [uv](https://github.com/astral-sh/uv), a fast Python package installer and runner.

**macOS/Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows:**
```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**With pip:**
```bash
pip install uv
```

For more installation options, see the [uv documentation](https://docs.astral.sh/uv/getting-started/installation/).

## Usage

Clone the repository and run the script from it. It needs the `happiest_baby/`
package that sits next to it, so it can't be run from a single downloaded file.

```bash
git clone https://github.com/xkrogen/happiest-baby-exporter.git
cd happiest-baby-exporter
./happiest-baby-exporter.py
```

Or with uv:

```bash
uv run happiest-baby-exporter.py
```

## Authentication

You can provide credentials in four ways:

1. **Interactive prompts** (most secure):
   ```bash
   ./happiest-baby-exporter.py
   ```

2. **Environment variables**:
   ```bash
   export HAPPIESTBABY_EMAIL="your-email@example.com"
   export HAPPIESTBABY_PASSWORD="your-password"
   ./happiest-baby-exporter.py
   ```

3. **Command-line options**:
   ```bash
   ./happiest-baby-exporter.py --email your-email@example.com --password your-password
   ```

4. **Refresh token** (no password needed; see the Claude skill section below):
   ```bash
   ./happiest-baby-exporter.py --credentials-file credentials.json
   # or: HAPPIESTBABY_REFRESH_TOKEN=... ./happiest-baby-exporter.py
   ```

## Use as a Claude skill (Claude desktop, web, and mobile)

You can install this exporter as a skill in Claude, then just ask things like
"pull the last 3 days of Chloe's sleep" or "how many diapers yesterday?".

### How the password is kept safe

Your password never goes to Claude. `build_skill.py` runs on your own computer,
signs in once, and puts a **refresh token** (not your password) into the skill as
`credentials.json`. The token:

- is not your password, so it can't be used to change your password or sign in
  to other services where you've reused it;
- still gives access to your Happiest Baby account, including controlling your SNOO
  and editing your journal, so treat the zip like a password;
- is stored in your private skill, which only your Claude account can see;
- can be revoked at any time without affecting your other logins (see below).

The skill tells Claude never to print the token or ask you for your password.

### 1. Build the skill zip (on your computer)

```bash
git clone https://github.com/mverderese/happiest-baby-exporter.git
cd happiest-baby-exporter
./build_skill.py          # or: uv run build_skill.py
```

Enter your Happiest Baby email and password when prompted. This writes
a new `dist/happiest-baby-<timestamp>.zip` (it never overwrites an older one), which is readable only by you and ignored by git.

### 2. Allow Claude to reach Happiest Baby

Skills run in Claude's code execution sandbox, which blocks most websites by default.
In Claude (web or desktop app):

1. Open **Settings → Capabilities**.
2. Turn on **Code execution and file creation**.
3. Under network access, choose the option that allows additional domains and add:
   - `cognito-idp.us-east-1.amazonaws.com`
   - `api-us-east-1-prod.happiestbaby.com`

   To control the SNOO from Claude, also add your SNOO's AWS IoT host. Find it with
   `./happiest-baby-actions.py snoo devices` (the `mqtt_host` value, something like
   `abc123xyz-ats.iot.us-east-1.amazonaws.com`), or just ask Claude to check the SNOO
   and it will tell you the host it needs.

   Keep package managers allowed too (the skill installs `requests`, `click`,
   `paho-mqtt`, and `PySocks` with pip).

On a Team or Enterprise plan, an admin may need to allow these settings.

### 3. Upload the skill

1. In Claude on the web (claude.ai) or the desktop app, open **Settings → Capabilities**.
2. Under **Skills**, click **Upload skill** and pick the new `dist/happiest-baby-<timestamp>.zip`.
3. Make sure the **happiest-baby** skill is switched on.

Skills belong to your account, so after you upload it once from desktop or web it
also works in the Claude iOS and Android apps. There's nothing to install on your phone.

After uploading, keep the zip somewhere private (for example a password manager).
You need it to revoke the token later.

### 4. Use it

Start a new chat on any device and ask, for example:

- "Pull the last 3 days of Happiest Baby data"
- "Summarize last night's SNOO sleep"
- "Export this week's feeds and diapers as CSV"
- "Put the SNOO on level 2 and hold it"
- "Log a wet and dirty diaper at 3:10"

### Rotating or revoking the token

- **Revoke** a token you've uploaded: `./build_skill.py --revoke dist/happiest-baby-<timestamp>.zip`
  (this only signs out that token; your phone app stays signed in).
- **Replace** it: run `./build_skill.py` again, delete the old skill in Claude, and upload
  the new zip. Then revoke the old zip if you kept it.
- Keep the old zip until you've revoked it. Changing your password doesn't revoke
  refresh tokens that were already issued.

If Claude reports that the token is expired or revoked, build and upload a new zip.

## Use as a Claude connector (remote MCP server with OAuth)

The repo also contains an MCP server you can host on [Vercel](https://vercel.com).
Add it to Claude as a custom connector, and Claude can control the SNOO and use the
journal on the web, desktop, and mobile apps. You don't need code execution, a skill
zip, or network allowlists. This is the most reliable way to control the SNOO from
Claude. The skill above is still handy for CSV exports.

Tools: `snoo_status`, `snoo_start`, `snoo_stop`, `snoo_set_level`, `snoo_white_noise`,
`list_snoos`, `get_sleep_sessions`, `get_journal_entries`, `log_diaper`, and the
experimental `log_journal_entry` and `delete_journal_entry`. Claude asks before using
tools that change something, unless you allow them in the connector's settings.

### How sign-in works

When you connect, Claude sends you to the server's own sign-in page, where you enter
your Happiest Baby email and password. The server signs in to Happiest Baby once and
then forgets the password. It keeps nothing on disk and has no database: the tokens it
gives Claude are encrypted with your `TOKEN_SECRET`, and they carry the Happiest Baby
login token inside them. Anyone with a Happiest Baby account can sign in, and each
person's connection only reaches their own account.

### 1. Deploy to Vercel

1. Push this repo to your GitHub account, then in Vercel choose **Add New → Project**
   and import it. Leave the framework preset as **Other** and the root directory as
   the repo root. `vercel.json` and `requirements.txt` configure the rest.
2. Under **Settings → Environment Variables**, add:

   | Variable | Value |
   | - | - |
   | `TOKEN_SECRET` | Output of `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
   | `DEFAULT_TIMEZONE` | Optional, e.g. `America/Chicago` (default `America/New_York`). Prefills the sign-in page's time zone field when the browser doesn't report one |
   | `PUBLIC_BASE_URL` | Optional. Defaults to your production URL (`https://<project>.vercel.app`). Set it if you use a custom domain |

3. Deploy, then open `https://<project>.vercel.app/` in a browser. You should see a
   short page describing the server.

Keep Vercel's Deployment Protection off for the production domain (the default
"Standard Protection" only covers preview deployments). Claude has to be able to reach
the server.

### 2. Add the connector in Claude

1. In Claude (web or desktop), open **Settings → Connectors → Add custom connector**.
2. Name it "Happiest Baby" and enter `https://<project>.vercel.app/mcp`. Leave the
   OAuth client ID and secret empty, since Claude registers itself.
3. Click **Connect**, sign in with your Happiest Baby account, and you're sent back to Claude.
   The sign-in page also asks for your time zone, prefilled from your browser. Times
   like "10:30" are read in that zone, so each person who signs in can use their own.
   To change it, disconnect and connect again.

Like skills, connectors belong to your account, so it also works in the Claude mobile apps.
[docs/claude-connector.md](docs/claude-connector.md) is a step-by-step guide you can share
with anyone who'll connect to your server.
For Claude Code: `claude mcp add --transport http happiest-baby https://<project>.vercel.app/mcp`.

### Disconnecting and revoking

- **One device or connection:** disconnect the connector in Claude.
- **Everything at once:** change `TOKEN_SECRET` in Vercel and redeploy. Every token
  the server has issued stops working, and you sign in again on next use.
- Changing your Happiest Baby password may also end existing sign-ins.

Sign-ins last as long as Happiest Baby's login token does. When it expires, Claude
asks you to reconnect.

### Run it locally

```bash
pip install -r requirements.txt uvicorn pytest
pytest tests                       # OAuth flow and tools, with Happiest Baby mocked
PUBLIC_BASE_URL=http://127.0.0.1:8000 TOKEN_SECRET=... \
  uvicorn api.index:app --port 8000
```

## Controlling the SNOO and logging entries

`happiest-baby-actions.py` is a command-line backup for when the Happiest Baby app
isn't working. It takes the same credential options as the exporter and needs the
`happiest_baby/` package next to it, so run it from a clone of this repo.

```bash
./happiest-baby-actions.py snoo status
./happiest-baby-actions.py snoo level 2 --hold     # baseline, 1, 2, 3, or 4
./happiest-baby-actions.py snoo start
./happiest-baby-actions.py snoo stop
./happiest-baby-actions.py snoo white-noise on --minutes 30

./happiest-baby-actions.py journal diaper --pee --poo --at 03:10 --note "blowout"
./happiest-baby-actions.py journal recent --type breastfeeding
./happiest-baby-actions.py journal delete <id>
```

Every change shows what it will do and asks first. Pass `--yes` (before the
subcommand) to skip the prompt, or `--dry-run` to only show the plan. SNOO
commands wait for the SNOO to report its new state. If it doesn't report in time,
the script exits with code 3.

How it works: SNOO commands are sent over AWS IoT MQTT (WebSocket on port 443), the
same channel the app uses. Journal entries go to the REST API. Both are undocumented
and were worked out by the [python-snoo](https://pypi.org/project/python-snoo/)
project, so they may break if Happiest Baby changes them.

- **Confirmed:** SNOO start, stop, level, hold, sticky white noise, and status;
  logging diapers.
- **Experimental:** `journal add` (feeds, pumping, solid food) and `journal delete`.
  For `journal add`, copy the `data` object from `journal recent` for the same type
  so the new entry looks like the app's.

## Command-Line Options

```
Options:
  --email TEXT               Email address (can use HAPPIESTBABY_EMAIL env var;
                             interactive prompt if not provided)
  --password TEXT            Password (can use HAPPIESTBABY_PASSWORD env var;
                             interactive prompt if not provided)
  --refresh-token TEXT       Refresh token to use instead of email/password
                             (can use HAPPIESTBABY_REFRESH_TOKEN env var)
  --credentials-file FILE    JSON file containing a refresh_token (as created
                             by build_skill.py)
  --baby-name TEXT           Baby name to fetch data for (if not provided,
                             interactive selection will be provided, unless
                             there is only a single baby)
  --lookback-days INTEGER    Number of days to look back from today [default: 14]
  --output-dir DIRECTORY     Directory to save CSV files [default: .]
  --journal-type TEXT        Comma-separated list of journal types to export
                             (breastfeeding,bottlefeeding,diaper,solidfood,
                             pumping,sleep; if not specified, exports all types)
  --units [imperial|metric]  Unit system to use for measurements [default: imperial]
  --timezone TEXT            Timezone for sleep session data
                             [default: America/New_York]
  --help                     Show this message and exit.
```

## Examples

**Export all data for the last 30 days:**
```bash
./happiest-baby-exporter.py --lookback-days 30
```

**Export only sleep and diaper data:**
```bash
./happiest-baby-exporter.py --journal-type sleep,diaper
```

**Export with metric units:**
```bash
./happiest-baby-exporter.py --units metric
```

**Export to a specific directory:**
```bash
./happiest-baby-exporter.py --output-dir ~/backup/baby-data
```

**Export for a specific baby (if you have multiple):**
```bash
./happiest-baby-exporter.py --baby-name SnoobyDoo
```

## Output Files

The script creates separate CSV files for each journal type:

- `journals_breastfeeding.csv` - Breastfeeding sessions
- `journals_bottlefeeding.csv` - Bottle feeding sessions
- `journals_diaper.csv` - Diaper changes
- `journals_solidfood.csv` - Solid food feedings
- `journals_pumping.csv` - Pumping sessions
- `journals_sleep.csv` - Consolidated sleep sessions (grouped by session ID)
- `journals_sleep_detailed.csv` - Detailed sleep data (all state changes)

## CSV Structure

All CSV files include:
- Baby name (except pumping journals)
- Start/end times where applicable
- Type-specific data (amounts, durations, etc.)
- Creation and update timestamps

Fields are ordered for easy reading: baby name first, followed by time fields, then data fields, and finally audit fields (createdAt, updatedAt).

## Privacy & Security

- Your credentials are only used to authenticate with the Happiest Baby API
- No data is sent to any third parties
- All data is saved locally to CSV files on your machine
- The script uses AWS Cognito for authentication (same as the official app)

## Requirements

- Python 3.12 or later
- Internet connection to access the Happiest Baby API

Dependencies are automatically managed by uv:
- `requests>=2.32.0` - HTTP library for API calls
- `click>=8.1.0` - Command-line interface framework
- `paho-mqtt>=2.0.0` and `PySocks>=1.7.1` - SNOO control over MQTT (`happiest-baby-actions.py` only)

The MCP server's dependencies are in `requirements.txt`.

## License

MIT License - feel free to use and modify as needed.

## Disclaimer

This is an unofficial tool and is not affiliated with or endorsed by Happiest Baby, Inc. Use at your own risk.
