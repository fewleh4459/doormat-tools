"""
Reprocess only the files identified as inverted by scan_inversions_all.py.
Reads inversion_scan_results.csv, reprocesses each file with the fixed
vectorize_v2.py (edge clearing + inversion validation + raster fallback).
"""

import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from vectorize_v2 import (
    process_pdf, generate_lrg_from_reg, is_color_pdf,
    SIZE_REG, SIZE_LRG, SIZE_AW, get_target_size,
)

# Map brand codes to folder paths
BRAND_FOLDERS = {
    "MO": (
        r"G:\My Drive\EMAGINEERED\_New File System 2021\MuckOff (MO)\MO MAT JOB BAGS\MO Print\_MO Statics",
        None,
    ),
    "CB": (
        r"G:\My Drive\EMAGINEERED\_New File System 2021\Coir Blimey (CB)\_CB MAT JOB BAGS\_CB Print\_CB Statics",
        None,
    ),
    "EMA": (
        r"G:\My Drive\EMAGINEERED\_New File System 2021\Emagineered (EMA)\_EMA MAT JOB BAGS\EMA Print\_EMA Statics",
        None,
    ),
    "CMD": (
        r"G:\My Drive\EMAGINEERED\_New File System 2021\Customat (CMD)\_CMD MAT JOB BAGS\CUS Print\_CMD Statics",
        None,
    ),
    "YCR": (
        r"G:\My Drive\EMAGINEERED\_New File System 2021\Your Custom Rug (YCR)\_YCR Mat Job Bag\YCR Print\_YCR Statics",
        None,
    ),
    "2DD": (
        r"G:\My Drive\EMAGINEERED\_New File System 2021\2 Day Doormats (2DD)\2DD Coir Mat Job Bag\2DD Print\_2DD Statics",
        None,
    ),
    "CMD-AW": (
        r"G:\My Drive\EMAGINEERED\_New File System 2021\Customat (CMD)\_CMD ALL WEATHER MAT JOB BAGS\CUS AW Print\_CMD AW Statics",
        "AW",
    ),
    "EMA-AW": (
        r"G:\My Drive\EMAGINEERED\_New File System 2021\Emagineered (EMA)\EMA ALL WEATHER MAT JOB BAG\EMA AW Print\_EMA AW Statics",
        "AW",
    ),
    "2DD-AW": (
        r"G:\My Drive\EMAGINEERED\_New File System 2021\2 Day Doormats (2DD)\2DD Weatherproof Mat Job Bag\2DD AW Print\_2DD AW Statics",
        "AW",
    ),
}

CSV_PATH = os.path.join(os.path.dirname(__file__), "inversion_scan_results.csv")


def reprocess_all():
    if not os.path.exists(CSV_PATH):
        print(f"ERROR: {CSV_PATH} not found. Run scan_inversions_all.py first.")
        return

    # Read CSV
    with open(CSV_PATH, "r") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    inverted = [r for r in rows if r["type"] == "INVERTED"]
    lrg_inverted = [r for r in rows if r["type"] == "LRG-INVERTED"]
    suspicious = [r for r in rows if r["type"] == "SUSPICIOUS"]

    print(f"Files to reprocess:")
    print(f"  Inverted (orig+output pairs): {len(inverted)}")
    print(f"  LRG-only inverted (generated): {len(lrg_inverted)}")
    print(f"  Suspicious (will also reprocess): {len(suspicious)}")
    total = len(inverted) + len(lrg_inverted) + len(suspicious)
    print(f"  Total: {total}")
    print()

    start = time.time()
    done = 0
    errors = 0

    # Process inverted and suspicious files (have originals)
    for row in inverted + suspicious:
        brand = row["brand"]
        fname = row["filename"]

        if brand not in BRAND_FOLDERS:
            print(f"  SKIP: unknown brand {brand}")
            continue

        orig_dir, force_size = BRAND_FOLDERS[brand]
        rb_dir = os.path.join(orig_dir, "_RichBlack")
        orig_path = os.path.join(orig_dir, fname)
        rb_path = os.path.join(rb_dir, fname)

        if not os.path.exists(orig_path):
            print(f"  SKIP: original missing {fname}")
            continue

        try:
            process_pdf(orig_path, output_path=rb_path, dpi=300, force_size=force_size)
            done += 1
        except Exception as e:
            print(f"  ERROR: {fname}: {e}")
            errors += 1

    # Process LRG-only inverted files (generated from REG originals)
    for row in lrg_inverted:
        brand = row["brand"]
        fname = row["filename"]

        if brand not in BRAND_FOLDERS:
            print(f"  SKIP: unknown brand {brand}")
            continue

        orig_dir, force_size = BRAND_FOLDERS[brand]
        rb_dir = os.path.join(orig_dir, "_RichBlack")
        lrg_path = os.path.join(rb_dir, fname)

        # Find the REG source file
        # LRG filename: "M111 LRG.pdf" → REG: "M111 REG.pdf" or "M111.pdf"
        reg_name = fname.replace(" LRG", " REG").replace(" LAR", " REG")
        reg_path = os.path.join(orig_dir, reg_name)

        if not os.path.exists(reg_path):
            # Try without size tag
            base = reg_name.replace(" REG", "")
            reg_path = os.path.join(orig_dir, base)
            if not os.path.exists(reg_path):
                print(f"  SKIP: no REG source for {fname}")
                continue

        try:
            generate_lrg_from_reg(reg_path, lrg_path, dpi=300)
            done += 1
        except Exception as e:
            print(f"  ERROR: {fname}: {e}")
            errors += 1

    elapsed = time.time() - start
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)

    print(f"\n{'='*60}")
    print(f"REPROCESSING COMPLETE")
    print(f"  Done: {done}")
    print(f"  Errors: {errors}")
    print(f"  Time: {mins}m {secs}s")
    print(f"{'='*60}")


if __name__ == "__main__":
    reprocess_all()
