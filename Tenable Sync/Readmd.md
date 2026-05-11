# TenableSync

A local web application that syncs and caches assets from Tenable and devices from two JumpCloud accounts, then lets you tag, audit, and manage them from a browser-based dashboard.

---

## Features

- **Tenable asset sync** — Pulls all assets from Tenable via the Export API and caches them in a local SQLite database.
- **JumpCloud sync** — Pulls devices from two separate JumpCloud accounts and caches them locally.
- **Org tagging** — Matches Tenable assets to JumpCloud devices by hostname and applies `Org:Account1` or `Org:Account2` tags in Tenable. Assets found in both accounts are tagged `Org:Both Accounts` to flag mid-migration devices.
- **Department + OS tagging** — Tags Tenable assets with `OS:Mac` or `OS:Windows`, and Windows assets with a department tag (`CSD`, `Sales`, `SET`, `Workspace`, `Corp`) based on agent name patterns.
- **Agent group assignment** — Assigns Tenable agents to scan groups based on their OS and Department tags.
- **Device:Not in JC audit** — Tags Tenable assets that have no matching JumpCloud device, useful for weekly audits.
- **Diff page** — Three-way view showing: Tenable assets not found in JumpCloud, assets tagged as not in JumpCloud, and JumpCloud devices not found in Tenable.
- **Migration tool** — Compare a old Tenable account against the new one. Shows which assets exist in both, only in the old, or only in the new. Lets you select and delete assets from the old account once migrated.
- **Stale cache warnings** — Warns if Tenable or JumpCloud data is more than 24 hours old before you run tagging operations.
- **Secure credential storage** — API keys are stored in Windows Credential Manager via `keyring`, never written to disk as plain text.
- **Run All** — Runs all three tagging steps (Org → Department/OS → Agent Groups) in sequence with one click.

---

## Requirements

- Windows 10 or 11
- Python 3.10+
- Tenable API access key and secret key
- JumpCloud API key(s) and Org ID(s) for up to two accounts

Install Python dependencies:

```bash
pip install -r requirements.txt
```

---

## Project Structure

```
TenableSync/
├── app.py              # FastAPI app, DB logic, Tenable + JumpCloud sync, tagging endpoints
├── app_routes.py       # Diff page logic, asset delete/unlink routes
├── requirements.txt
└── templates/
    ├── layout.html     # Base HTML layout and nav
    ├── index.html      # Home — asset tables and sync controls
    ├── diff.html       # Diff view between Tenable and JumpCloud
    ├── tags.html       # Tagging and agent group tools
    ├── settings.html   # API key configuration
    └── migration.html  # Old-to-new Tenable account migration tool
```

---

## Setup

**1. Install dependencies**

```bash
pip install -r requirements.txt
```

**2. Run the app**

```bash
uvicorn app:app --host 127.0.0.1 --port 8001
```

Then open `http://127.0.0.1:8001` in your browser.

**3. Add your API keys**

Go to **Settings** and enter:

- Tenable Access Key and Secret Key
- JumpCloud API Key and Org ID for Account 1
- JumpCloud API Key and Org ID for Account 2 (if applicable)

Keys are saved to Windows Credential Manager. They are never stored in `C:\TSync` or in code.

---

## Usage

### Syncing data

On the **Home** page:

- Click **Sync from Tenable** to pull all assets into the local cache.
- Click **Sync from JumpCloud** to pull devices from both JumpCloud accounts.

A stale cache warning appears if either cache is over 24 hours old.

### Tagging

On the **Tags** page, run steps individually or use **Run All**:

| Step | What it does |
|---|---|
| 1 — Org tags | Tags assets `Org:Account1`, `Org:Account2`, or `Org:Both Accounts` based on JumpCloud hostname matches |
| 2 — Department + OS tags | Tags assets `OS:Mac` / `OS:Windows` and Windows assets with a Department tag |
| 3 — Assign Agent Groups | Moves agents into Tenable scan groups based on their tags |
| Weekly — Device:Not in JC | Tags any Tenable asset with no JumpCloud match |

### Diff view

The **Diff** page shows three sections:

- Tenable assets with no matching JumpCloud device
- Assets tagged `Device:Not in JC`
- JumpCloud devices with no matching Tenable asset

### Migration

The **Migration** page lets you compare a old Tenable account against your new one by entering the old account's API keys (they are never saved). It shows which assets are in both accounts so you can safely delete the duplicates from the old account.

---

## Data Storage

| What | Where |
|---|---|
| Asset + device cache | `C:\TSync\tsync.db` (SQLite) |
| API keys | Windows Credential Manager |

---

## Screenshots

_Add screenshots here_

---

## License

MIT License — free to use, modify, and distribute.
