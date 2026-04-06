"""
Batch runner: process all doormat design folders sequentially.
Run this and leave the PC on - it'll work through everything.
"""
import subprocess
import sys
import time
import os

SCRIPT = r"C:\Claude\vectorize_v2.py"

# Coir mat folders (full process: Pass 1 + LRG generation)
COIR_FOLDERS = [
    # MO Statics - SKIP if already done by the earlier run
    # r"G:\My Drive\EMAGINEERED\_New File System 2021\MuckOff (MO)\MO MAT JOB BAGS\MO Print\_MO Statics",
    r"G:\My Drive\EMAGINEERED\_New File System 2021\Coir Blimey (CB)\_CB MAT JOB BAGS\_CB Print\_CB Statics",
    r"G:\My Drive\EMAGINEERED\_New File System 2021\Emagineered (EMA)\_EMA MAT JOB BAGS\EMA Print\_EMA Statics",
    r"G:\My Drive\EMAGINEERED\_New File System 2021\Customat (CMD)\_CMD MAT JOB BAGS\CUS Print\_CMD Statics",
    r"G:\My Drive\EMAGINEERED\_New File System 2021\Your Custom Rug (YCR)\_YCR Mat Job Bag\YCR Print\_YCR Statics",
    r"G:\My Drive\EMAGINEERED\_New File System 2021\2 Day Doormats (2DD)\2DD Coir Mat Job Bag\2DD Print\_2DD Statics",
]

# All-weather folders (Pass 1 only, fixed 760x460mm size)
AW_FOLDERS = [
    r"G:\My Drive\EMAGINEERED\_New File System 2021\Customat (CMD)\_CMD ALL WEATHER MAT JOB BAGS\CUS AW Print\_CMD AW Statics",
    r"G:\My Drive\EMAGINEERED\_New File System 2021\Emagineered (EMA)\EMA ALL WEATHER MAT JOB BAG\EMA AW Print\_EMA AW Statics",
    r"G:\My Drive\EMAGINEERED\_New File System 2021\2 Day Doormats (2DD)\2DD Weatherproof Mat Job Bag\2DD AW Print\_2DD AW Statics",
]

LOG_FILE = r"C:\Claude\batch_report.log"


def run_folder(folder, flags=None):
    """Run vectorize_v2.py on a folder and log output."""
    cmd = [sys.executable, SCRIPT, folder]
    if flags:
        cmd.extend(flags)

    folder_name = os.path.basename(os.path.dirname(folder)) + "/" + os.path.basename(folder)
    print(f"\n{'='*70}")
    print(f"STARTING: {folder_name}")
    print(f"{'='*70}\n")

    start = time.time()
    result = subprocess.run(cmd, capture_output=False, text=True)
    elapsed = time.time() - start

    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    status = "OK" if result.returncode == 0 else f"ERROR (code {result.returncode})"
    summary = f"[{status}] {folder_name} - {mins}m {secs}s"
    print(f"\n{summary}")

    # Append to log
    with open(LOG_FILE, "a") as f:
        f.write(summary + "\n")

    return result.returncode == 0


if __name__ == "__main__":
    total_start = time.time()

    with open(LOG_FILE, "w") as f:
        f.write("DOORMAT RICH BLACK BATCH PROCESSING REPORT\n")
        f.write(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 60 + "\n\n")

    print("Processing coir mat folders (with LRG generation)...")
    for folder in COIR_FOLDERS:
        run_folder(folder)

    print("\nProcessing all-weather folders (760x460mm, no LRG)...")
    for folder in AW_FOLDERS:
        run_folder(folder, flags=["--aw"])

    total_elapsed = time.time() - total_start
    hours = int(total_elapsed // 3600)
    mins = int((total_elapsed % 3600) // 60)

    with open(LOG_FILE, "a") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"COMPLETE: Total time {hours}h {mins}m\n")
        f.write(f"Finished: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    print(f"\n{'='*70}")
    print(f"ALL FOLDERS COMPLETE - Total time: {hours}h {mins}m")
    print(f"Report saved to: {LOG_FILE}")
    print(f"{'='*70}")
