# Replit Reserved VM Setup

This guide walks through deploying `drive_watcher.py` on a Replit Reserved VM
so the doormat design pipeline runs 24/7 with no PC dependency.

## What you get

- `drive_watcher.py` polls Google Drive **every 60 seconds** for new PDFs
- Only queries files modified since the last scan (very low resource usage)
- Processes each new PDF through `vectorize_v2.py` (same output quality as local)
- Uploads result as `<name>_p.pdf` and trashes the original
- Sends an email summary only when files are processed or errors occur

## Prerequisites

You'll need:
1. A Google Cloud service account with Drive access
2. A Replit account (Core plan or higher for Reserved VMs)
3. The Gmail app password you're already using

---

## Part 1 — Create a Google service account

One-time setup, takes about 5 minutes.

1. Go to <https://console.cloud.google.com>
2. Create a new project (or reuse one): **"Beaudax Drive Watcher"**
3. Enable the **Google Drive API**:
   - APIs & Services → Library → search "Google Drive API" → Enable
4. Create a service account:
   - IAM & Admin → Service Accounts → **Create Service Account**
   - Name: `doormat-watcher`
   - Skip the optional role granting
5. Generate a JSON key:
   - Click the new service account → Keys tab → **Add Key → Create new key** → JSON → Download
   - Save the downloaded JSON file somewhere safe temporarily
6. Note the service account email address (looks like `doormat-watcher@<project>.iam.gserviceaccount.com`)
7. **Share each watched Drive folder with the service account** as Editor:
   - In Drive, right-click each of the 12 Print folders → Share → paste the service account email → give **Editor** access
   - The 12 folders are listed in `CLAUDE.md`
   - Sharing ONLY the top-level Print folder is enough — it inherits to all subfolders

---

## Part 2 — Create the Replit Reserved VM

1. Go to Replit → **Create** → choose **"Import from GitHub"**
2. Enter `fewleh4459/doormat-tools`
3. On the "Configure" step, choose **Reserved VM** under deployment / hosting
4. Wait for the import to finish

---

## Part 3 — Install system dependencies

Replit uses Nix for system packages. Create (or edit) `replit.nix` in the workspace root:

```nix
{ pkgs }: {
  deps = [
    pkgs.python311
    pkgs.potrace                # required by potracer Python binding
    pkgs.libGL                  # required by opencv-python-headless
    pkgs.glibcLocales
  ];
}
```

Then in the Shell tab, run:

```bash
pip install -r requirements.txt
```

---

## Part 4 — Add secrets

Click the **Secrets** (🔒 padlock) tab in the left sidebar and add:

| Key | Value |
|-----|-------|
| `GOOGLE_CREDENTIALS_JSON` | Paste the **entire contents** of the service account JSON file |
| `GMAIL_USER` | `oliver@beaudax.co.uk` |
| `GMAIL_APP_PASSWORD` | `ralw udcr mwov irmm` |
| `NOTIFY_TO` | `oliver@beaudax.co.uk` |

Optional:

| Key | Default | Meaning |
|-----|---------|---------|
| `POLL_INTERVAL_SECONDS` | `60` | How often to poll Drive |
| `STATE_FILE` | `last_scan.json` | Where to persist the last-scan timestamp |

---

## Part 5 — Set the run command

Open `.replit` in the workspace root and set:

```toml
run = "python drive_watcher.py"
```

Or, if you want to run multiple services on the same VM (see next section),
use a start script:

```toml
run = "bash start.sh"
```

---

## Part 6 — Test it

Click the green **Run** button. You should see:

```
Doormat Drive Watcher starting
  Poll interval: 60s
  State file:    last_scan.json
  Watched roots: 12 folders
Drive auth OK; entering poll loop
```

Drop a test PDF into any of the watched folders (e.g.
`2 Day Doormats (2DD)/2DD Coir Mat Job Bag/2DD Print/2DD Etsy/2026/SMALL/`).
Within 60 seconds you should see a log line like:

```
cycle: found=1 kept=1 processed=1 errors=0
  ✓ testfile.pdf (0.1 MB) → testfile_p.pdf
```

And you'll receive a summary email.

---

## Part 7 — Running multiple services on the same VM

If you want to run this alongside mousekeeping and the UPS Gmail Watcher on a
single Reserved VM, use an orchestrator script. Example `start.sh`:

```bash
#!/bin/bash
set -e

# Start the doormat Drive watcher in background
python drive_watcher.py > doormat_watcher.log 2>&1 &

# Start mousekeeping in the foreground
cd mousekeeping && npm start
```

The UPS Gmail Watcher can run as a scheduled job inside mousekeeping
(see `mousekeeping/server/jobs/ups-gmail-watcher.ts`) — no separate process
needed for that one.

---

## Troubleshooting

**"Drive auth failed"** — service account JSON is malformed or missing. Check the `GOOGLE_CREDENTIALS_JSON` secret starts with `{` and ends with `}`.

**"Query returned 0 files, but I just dropped one in"** — service account needs Editor access on the folder. Re-share the folder with the service account email.

**"potracer import error"** — `potrace` system package missing; make sure `replit.nix` includes `pkgs.potrace` and run `pip install -r requirements.txt` again.

**Files are processed but originals still visible in Drive** — originals go to the service account's Trash, not yours. They will remain in Trash for 30 days then be permanently deleted. This is expected.

**High RAM** — normal peak is ~300 MB during processing. If you see >1 GB, something's looping; check `drive_watcher.log`.

---

## Resource expectations

| Metric | Typical |
|--------|---------|
| CPU (idle) | < 1% |
| CPU (processing a PDF) | 30–60% briefly |
| RAM | ~200–300 MB |
| Drive API calls | ~60/hour for polling + N per file processed |
| Network | a few MB/day when quiet, ~100 MB/day during busy periods |
| Cost | Replit Reserved VM: ~$20/month (flat) |
