"""
Doormat design watcher — monitors static folders for new PDFs,
processes them (vectorize B&W / boost colour), renames with _p suffix,
and deletes the original.

Runs as an always-on service on the office PC.

Flow:
1. Scan watched folders every 30 seconds
2. Find any PDF that doesn't end in _p.pdf and isn't in a subfolder
3. Wait for file to be stable (not still syncing from Google Drive)
4. Process it (rich black for B&W, CMYK boost for colour)
5. Save as filename_p.pdf
6. Delete the original

Usage:
    python watcher.py                  # Run in foreground
    python watcher.py --install        # Install as Windows scheduled task (runs at login)
    python watcher.py --uninstall      # Remove the scheduled task
"""

import sys
import os
import time
import glob as globmod
import logging
from datetime import datetime

sys.path.insert(0, r"C:\Claude")
from vectorize_v2 import (
    process_pdf, get_target_size, is_color_pdf,
    SIZE_REG, SIZE_LRG, SIZE_AW
)
from reportlab.lib.units import mm as MM

# ── Configuration ─────────────────────────────────────────────────────────────

SCAN_INTERVAL = 30  # seconds between scans
STABLE_WAIT = 10    # seconds a file must be unchanged before processing
STABLE_CHECKS = 3   # number of consecutive stable checks required

LOG_FILE = r"C:\Claude\watcher.log"

# All watched folders with their force_size override (None = auto-detect from filename)
WATCHED_FOLDERS = [
    # Coir mats
    (r"G:\My Drive\EMAGINEERED\_New File System 2021\MuckOff (MO)\MO MAT JOB BAGS\MO Print\_MO Statics", None),
    (r"G:\My Drive\EMAGINEERED\_New File System 2021\Coir Blimey (CB)\_CB MAT JOB BAGS\_CB Print\_CB Statics", None),
    (r"G:\My Drive\EMAGINEERED\_New File System 2021\Emagineered (EMA)\_EMA MAT JOB BAGS\EMA Print\_EMA Statics", None),
    (r"G:\My Drive\EMAGINEERED\_New File System 2021\Customat (CMD)\_CMD MAT JOB BAGS\CUS Print\_CMD Statics", None),
    (r"G:\My Drive\EMAGINEERED\_New File System 2021\Your Custom Rug (YCR)\_YCR Mat Job Bag\YCR Print\_YCR Statics", None),
    (r"G:\My Drive\EMAGINEERED\_New File System 2021\2 Day Doormats (2DD)\2DD Coir Mat Job Bag\2DD Print\_2DD Statics", None),
    # All-weather mats
    (r"G:\My Drive\EMAGINEERED\_New File System 2021\Customat (CMD)\_CMD ALL WEATHER MAT JOB BAGS\CUS AW Print\_CMD AW Statics", "AW"),
    (r"G:\My Drive\EMAGINEERED\_New File System 2021\Emagineered (EMA)\EMA ALL WEATHER MAT JOB BAG\EMA AW Print\_EMA AW Statics", "AW"),
    (r"G:\My Drive\EMAGINEERED\_New File System 2021\2 Day Doormats (2DD)\2DD Weatherproof Mat Job Bag\2DD AW Print\_2DD AW Statics", "AW"),
]


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ── File stability check ─────────────────────────────────────────────────────

def is_file_stable(filepath):
    """Check if a file has stopped being written to (Google Drive sync complete).
    Returns True if file size hasn't changed over STABLE_CHECKS checks."""
    try:
        sizes = []
        for _ in range(STABLE_CHECKS):
            sizes.append(os.path.getsize(filepath))
            time.sleep(STABLE_WAIT / STABLE_CHECKS)

        # All sizes must be equal and non-zero
        return len(set(sizes)) == 1 and sizes[0] > 0
    except (OSError, FileNotFoundError):
        return False


def find_unprocessed_pdfs(folder):
    """Find PDFs that haven't been processed yet (no _p suffix)."""
    all_pdfs = globmod.glob(os.path.join(folder, "*.pdf"))
    unprocessed = []
    for pdf in all_pdfs:
        basename = os.path.basename(pdf)
        name_no_ext = os.path.splitext(basename)[0]

        # Skip if already processed (ends with _p)
        if name_no_ext.endswith("_p"):
            continue

        # Skip if a processed version already exists
        processed_path = os.path.join(folder, f"{name_no_ext}_p.pdf")
        if os.path.exists(processed_path):
            continue

        unprocessed.append(pdf)

    return unprocessed


def process_and_replace(filepath, force_size=None):
    """Process a PDF, save with _p suffix, delete original."""
    basename = os.path.basename(filepath)
    folder = os.path.dirname(filepath)
    name_no_ext = os.path.splitext(basename)[0]

    output_filename = f"{name_no_ext}_p.pdf"
    output_path = os.path.join(folder, output_filename)

    try:
        # Process
        logging.info(f"Processing: {basename}")
        process_pdf(filepath, output_path=output_path, dpi=300, force_size=force_size)

        # Verify output exists and is valid
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            # Delete original
            os.remove(filepath)
            logging.info(f"  Done: {output_filename} ({os.path.getsize(output_path) // 1024} KB) — original deleted")
            return True
        else:
            logging.error(f"  Output file missing or empty: {output_path}")
            return False

    except Exception as e:
        logging.error(f"  ERROR processing {basename}: {e}")
        # Clean up failed output if it exists
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        return False


# ── Main loop ─────────────────────────────────────────────────────────────────

def watch_loop():
    """Main watching loop — scans all folders for new files."""
    setup_logging()
    logging.info("=" * 60)
    logging.info("Doormat Design Watcher started")
    logging.info(f"Watching {len(WATCHED_FOLDERS)} folders")
    logging.info(f"Scan interval: {SCAN_INTERVAL}s")
    logging.info("=" * 60)

    while True:
        try:
            for folder, force_size in WATCHED_FOLDERS:
                if not os.path.exists(folder):
                    continue

                unprocessed = find_unprocessed_pdfs(folder)

                for pdf in sorted(unprocessed):
                    basename = os.path.basename(pdf)

                    # Check file is stable (not still syncing)
                    if not is_file_stable(pdf):
                        logging.info(f"Skipping {basename} — still syncing")
                        continue

                    process_and_replace(pdf, force_size=force_size)

        except Exception as e:
            logging.error(f"Scan error: {e}")

        time.sleep(SCAN_INTERVAL)


# ── Windows scheduled task install/uninstall ──────────────────────────────────

TASK_NAME = "BeaudaxDoormatWatcher"


def install_task():
    """Install as a Windows scheduled task that runs at login."""
    import subprocess

    python_exe = sys.executable
    script_path = os.path.abspath(__file__)

    # Create a VBS wrapper to run without a visible window
    vbs_path = os.path.join(os.path.dirname(script_path), "watcher_silent.vbs")
    with open(vbs_path, "w") as f:
        f.write(f'Set WshShell = CreateObject("WScript.Shell")\n')
        f.write(f'WshShell.Run """{python_exe}"" ""{script_path}""", 0, False\n')

    # Create scheduled task
    cmd = [
        "schtasks", "/Create",
        "/TN", TASK_NAME,
        "/TR", f'wscript.exe "{vbs_path}"',
        "/SC", "ONLOGON",
        "/RL", "HIGHEST",
        "/F",  # force overwrite if exists
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"Installed scheduled task: {TASK_NAME}")
        print(f"  Runs at login, silently in background")
        print(f"  Log file: {LOG_FILE}")
        print(f"  To uninstall: python watcher.py --uninstall")
        print(f"  To start now: python watcher.py")

        # Also start it immediately
        subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME],
                      capture_output=True, text=True)
        print(f"  Started!")
    else:
        print(f"Failed to install: {result.stderr}")


def uninstall_task():
    """Remove the scheduled task."""
    import subprocess
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"Removed scheduled task: {TASK_NAME}")
    else:
        print(f"Failed to remove: {result.stderr}")

    # Clean up VBS wrapper
    vbs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watcher_silent.vbs")
    if os.path.exists(vbs_path):
        os.remove(vbs_path)
        print("Cleaned up watcher_silent.vbs")


def status():
    """Check if the watcher is currently running."""
    import subprocess
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(result.stdout)
    else:
        print(f"Task '{TASK_NAME}' not found. Install with: python watcher.py --install")


if __name__ == "__main__":
    if "--install" in sys.argv:
        install_task()
    elif "--uninstall" in sys.argv:
        uninstall_task()
    elif "--status" in sys.argv:
        status()
    else:
        watch_loop()
