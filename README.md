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

### Option 1: Run directly from GitHub (no checkout required)

You can run the script directly from GitHub without cloning the repository:

```bash
uv run https://raw.githubusercontent.com/xkrogen/happiest-baby-exporter/refs/heads/main/happiest-baby-exporter.py
```

This will prompt you for your email and password interactively.

### Option 2: Clone and run locally

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
- still gives access to your Happiest Baby account, so treat the zip like a password;
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

   Keep package managers allowed too (the skill installs `requests` and `click` with pip).

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

### Rotating or revoking the token

- **Revoke** a token you've uploaded: `./build_skill.py --revoke dist/happiest-baby-<timestamp>.zip`
  (this only signs out that token; your phone app stays signed in).
- **Replace** it: run `./build_skill.py` again, delete the old skill in Claude, and upload
  the new zip. Then revoke the old zip if you kept it.
- Keep the old zip until you've revoked it. Changing your password doesn't revoke
  refresh tokens that were already issued.

If Claude reports that the token is expired or revoked, build and upload a new zip.

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

## License

MIT License - feel free to use and modify as needed.

## Disclaimer

This is an unofficial tool and is not affiliated with or endorsed by Happiest Baby, Inc. Use at your own risk.
