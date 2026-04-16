"""
Scan ALL _RichBlack folders for inverted B&W designs.
Compares original vs output black pixel ratios.
Inverted = output is dramatically darker than original (background filled black).
"""

import fitz
import os
import glob
import numpy as np
import csv
import time

ZOOM = 0.75  # low zoom for speed

FOLDER_PAIRS = [
    # (brand, orig_dir, richblack_dir)
    ("MO",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\MuckOff (MO)\MO MAT JOB BAGS\MO Print\_MO Statics",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\MuckOff (MO)\MO MAT JOB BAGS\MO Print\_MO Statics\_RichBlack"),
    ("CB",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\Coir Blimey (CB)\_CB MAT JOB BAGS\_CB Print\_CB Statics",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\Coir Blimey (CB)\_CB MAT JOB BAGS\_CB Print\_CB Statics\_RichBlack"),
    ("EMA",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\Emagineered (EMA)\_EMA MAT JOB BAGS\EMA Print\_EMA Statics",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\Emagineered (EMA)\_EMA MAT JOB BAGS\EMA Print\_EMA Statics\_RichBlack"),
    ("CMD",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\Customat (CMD)\_CMD MAT JOB BAGS\CUS Print\_CMD Statics",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\Customat (CMD)\_CMD MAT JOB BAGS\CUS Print\_CMD Statics\_RichBlack"),
    ("YCR",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\Your Custom Rug (YCR)\_YCR Mat Job Bag\YCR Print\_YCR Statics",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\Your Custom Rug (YCR)\_YCR Mat Job Bag\YCR Print\_YCR Statics\_RichBlack"),
    ("2DD",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\2 Day Doormats (2DD)\2DD Coir Mat Job Bag\2DD Print\_2DD Statics",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\2 Day Doormats (2DD)\2DD Coir Mat Job Bag\2DD Print\_2DD Statics\_RichBlack"),
    ("CMD-AW",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\Customat (CMD)\_CMD ALL WEATHER MAT JOB BAGS\CUS AW Print\_CMD AW Statics",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\Customat (CMD)\_CMD ALL WEATHER MAT JOB BAGS\CUS AW Print\_CMD AW Statics\_RichBlack"),
    ("EMA-AW",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\Emagineered (EMA)\EMA ALL WEATHER MAT JOB BAG\EMA AW Print\_EMA AW Statics",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\Emagineered (EMA)\EMA ALL WEATHER MAT JOB BAG\EMA AW Print\_EMA AW Statics\_RichBlack"),
    ("2DD-AW",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\2 Day Doormats (2DD)\2DD Weatherproof Mat Job Bag\2DD AW Print\_2DD AW Statics",
     r"G:\My Drive\EMAGINEERED\_New File System 2021\2 Day Doormats (2DD)\2DD Weatherproof Mat Job Bag\2DD AW Print\_2DD AW Statics\_RichBlack"),
]


def render_grayscale(pdf_path, zoom=ZOOM):
    try:
        doc = fitz.open(pdf_path)
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        doc.close()
        return arr
    except Exception:
        return None


def black_ratio(arr, threshold=128):
    if arr is None:
        return None
    return float(np.sum(arr < threshold)) / arr.size


def scan_folder(brand, orig_dir, rb_dir):
    """Scan one folder pair, return list of inverted/suspicious results."""
    results = []

    if not os.path.isdir(orig_dir) or not os.path.isdir(rb_dir):
        print(f"  SKIP {brand}: folder missing")
        return results

    orig_pdfs = glob.glob(os.path.join(orig_dir, "*.pdf"))
    count = 0

    for orig_path in sorted(orig_pdfs):
        fname = os.path.basename(orig_path)
        rb_path = os.path.join(rb_dir, fname)

        if not os.path.exists(rb_path):
            continue

        count += 1
        orig_arr = render_grayscale(orig_path)
        rb_arr = render_grayscale(rb_path)

        orig_r = black_ratio(orig_arr)
        rb_r = black_ratio(rb_arr)

        if orig_r is None or rb_r is None:
            continue

        change = rb_r - orig_r

        # Inversion: output MUCH darker than original
        inverted = (change > 0.30) or (orig_r < 0.15 and rb_r > 0.70)
        suspicious = (not inverted) and (change > 0.15)

        if inverted or suspicious:
            results.append({
                "brand": brand,
                "fname": fname,
                "orig_r": orig_r,
                "rb_r": rb_r,
                "change": change,
                "inverted": inverted,
                "suspicious": suspicious,
            })

    return results, count


if __name__ == "__main__":
    start = time.time()
    all_inverted = []
    all_suspicious = []
    total_scanned = 0

    # Also check for LRG-only files in _RichBlack (generated, no original)
    lrg_only = []

    for brand, orig_dir, rb_dir in FOLDER_PAIRS:
        print(f"\nScanning {brand}...")
        results, count = scan_folder(brand, orig_dir, rb_dir)
        total_scanned += count

        inv = [r for r in results if r["inverted"]]
        sus = [r for r in results if r["suspicious"]]
        all_inverted.extend(inv)
        all_suspicious.extend(sus)

        print(f"  {count} pairs checked, {len(inv)} inverted, {len(sus)} suspicious")
        for r in inv:
            print(f"  ** INVERTED: {r['fname']}  orig={r['orig_r']*100:.1f}%  out={r['rb_r']*100:.1f}%  change={r['change']*100:+.1f}%")

        # Check for generated LRG files (no original) that might be inverted
        if os.path.isdir(rb_dir):
            rb_pdfs = glob.glob(os.path.join(rb_dir, "*.pdf"))
            for rb_path in rb_pdfs:
                fname = os.path.basename(rb_path)
                orig_path = os.path.join(orig_dir, fname)
                if not os.path.exists(orig_path) and ("LRG" in fname.upper() or "LAR" in fname.upper()):
                    # This is a generated LRG — check if it looks inverted on its own
                    arr = render_grayscale(rb_path)
                    r = black_ratio(arr)
                    if r is not None and r > 0.70:
                        lrg_only.append({
                            "brand": brand,
                            "fname": fname,
                            "rb_r": r,
                        })
                        print(f"  ** LRG-ONLY INVERTED: {fname}  black={r*100:.1f}%")

    elapsed = time.time() - start

    print(f"\n{'='*70}")
    print(f"SCAN COMPLETE — {total_scanned} pairs in {elapsed:.0f}s")
    print(f"{'='*70}")
    print(f"Inverted: {len(all_inverted)}")
    print(f"Suspicious: {len(all_suspicious)}")
    print(f"LRG-only inverted: {len(lrg_only)}")

    if all_inverted:
        print(f"\n--- INVERTED FILES ---")
        for r in sorted(all_inverted, key=lambda x: -x["change"]):
            print(f"  [{r['brand']}] {r['fname']:<50} orig={r['orig_r']*100:5.1f}%  out={r['rb_r']*100:5.1f}%  change={r['change']*100:+6.1f}%")

    if all_suspicious:
        print(f"\n--- SUSPICIOUS FILES ---")
        for r in sorted(all_suspicious, key=lambda x: -x["change"]):
            print(f"  [{r['brand']}] {r['fname']:<50} orig={r['orig_r']*100:5.1f}%  out={r['rb_r']*100:5.1f}%  change={r['change']*100:+6.1f}%")

    if lrg_only:
        print(f"\n--- LRG-ONLY (generated, possibly inverted) ---")
        for r in lrg_only:
            print(f"  [{r['brand']}] {r['fname']:<50} black={r['rb_r']*100:5.1f}%")

    # Save to CSV
    csv_path = os.path.join(os.path.dirname(__file__), "inversion_scan_results.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["brand", "filename", "type", "orig_black%", "output_black%", "change%"])
        for r in all_inverted:
            w.writerow([r["brand"], r["fname"], "INVERTED", f"{r['orig_r']*100:.1f}", f"{r['rb_r']*100:.1f}", f"{r['change']*100:.1f}"])
        for r in all_suspicious:
            w.writerow([r["brand"], r["fname"], "SUSPICIOUS", f"{r['orig_r']*100:.1f}", f"{r['rb_r']*100:.1f}", f"{r['change']*100:.1f}"])
        for r in lrg_only:
            w.writerow([r["brand"], r["fname"], "LRG-INVERTED", "", f"{r['rb_r']*100:.1f}", ""])
    print(f"\nResults saved to: {csv_path}")
