"""
Patch script for _RichBlack folders.
Fixes three issues without re-running the full pipeline:

1. LARGE FILES: Color PDFs are too big (10-30MB). Re-render at 150 DPI with Flate compression.
2. INVERTED B&W: Some vectorized files have inverted black/white. Detect and re-process.
3. LRG PROPORTIONS: Generated LRG variants have letterboxing. Stretch to fill artboard.

Run on a _RichBlack folder to patch only the affected files.
"""

import sys
import os
import glob as globmod
import fitz
import numpy as np
from PIL import Image, ImageEnhance
import potrace
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm as MM
from reportlab.lib.utils import ImageReader
import io
import time

# Import core functions from v2
sys.path.insert(0, r"C:\Claude")
from vectorize_v2 import (
    trace_bitmap, curve_signed_area, emit_curve_ops,
    write_bw_vector_pdf, boost_color_image_cmyk, is_color_pdf,
    get_target_size, get_sku, SIZE_REG, SIZE_LRG, SIZE_AW
)


def write_color_pdf_compressed(img, output_path, page_size, dpi=150):
    """Write a CMYK color image as a compressed PDF at reduced DPI."""
    page_w, page_h = page_size

    # Calculate target pixel dimensions for the desired DPI
    target_w = int(page_w / 72 * dpi)
    target_h = int(page_h / 72 * dpi)

    # Resize if larger than target
    if img.size[0] > target_w or img.size[1] > target_h:
        if img.mode == "CMYK":
            img = img.resize((target_w, target_h), Image.LANCZOS)
        else:
            img = img.resize((target_w, target_h), Image.LANCZOS)

    # Save as TIFF with compression for CMYK, or PNG for RGB
    img_buffer = io.BytesIO()
    if img.mode == "CMYK":
        img.save(img_buffer, format='TIFF', compression='tiff_deflate')
    else:
        img.save(img_buffer, format='PNG', optimize=True)
    img_buffer.seek(0)

    c = canvas.Canvas(output_path, pagesize=(page_w, page_h))
    c.drawImage(ImageReader(img_buffer), 0, 0, width=page_w, height=page_h)
    c.showPage()
    c.save()


def is_inverted(original_path, output_path):
    """Check if output has inverted colors compared to original.
    Returns True if the output has significantly more black than the original."""
    try:
        # Render both at low res for quick comparison
        mat = fitz.Matrix(72 / 72, 72 / 72)

        doc_orig = fitz.open(original_path)
        pix_orig = doc_orig[0].get_pixmap(matrix=mat, alpha=False)
        img_orig = Image.frombytes("RGB", [pix_orig.width, pix_orig.height], pix_orig.samples)
        doc_orig.close()

        doc_out = fitz.open(output_path)
        pix_out = doc_out[0].get_pixmap(matrix=mat, alpha=False)
        img_out = Image.frombytes("RGB", [pix_out.width, pix_out.height], pix_out.samples)
        doc_out.close()

        arr_orig = np.array(img_orig.convert("L"))
        arr_out = np.array(img_out.convert("L"))

        # Count dark pixels (< 128)
        orig_dark_pct = (arr_orig < 128).sum() / arr_orig.size * 100
        out_dark_pct = (arr_out < 128).sum() / arr_out.size * 100

        # If output has way more dark pixels, it's inverted
        # Threshold: output has >30% more dark coverage than original
        if out_dark_pct > orig_dark_pct + 30:
            return True, orig_dark_pct, out_dark_pct
        return False, orig_dark_pct, out_dark_pct
    except Exception:
        return False, 0, 0


def fix_inverted_bw(original_path, output_path, page_size, dpi=300):
    """Re-vectorize a B&W file, trying harder to find the correct page boundary.
    Skips ALL simple rectangles (<=4 segments) that are outer contours."""
    doc = fitz.open(original_path)
    page = doc[0]
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    doc.close()

    traced = trace_bitmap(img)
    bmp_w, bmp_h = img.size
    page_w, page_h = page_size
    sx = page_w / bmp_w
    sy = page_h / bmp_h

    curves = list(traced)
    areas = [curve_signed_area(cv) for cv in curves]

    c = canvas.Canvas(output_path, pagesize=(page_w, page_h))
    ops = ["1 1 1 1 k"]  # Rich black CMYK

    # Skip ALL large CCW rectangles (<=4 segments) - these are page boundaries
    # that cause the inversion when filled
    for i, (curve, area) in enumerate(zip(curves, areas)):
        # Skip outer contour rectangles (CCW = negative area, simple shape)
        if area < -1000000 and len(curve.segments) <= 4:
            continue
        ops.extend(emit_curve_ops(curve, sx, sy, page_h))

    ops.append("f")

    c.saveState()
    for op in ops:
        c._code.append(op)
    c.restoreState()
    c.showPage()
    c.save()


def fix_lrg_stretch(original_reg_path, output_lrg_path, dpi=150):
    """Regenerate an LRG variant with full artboard stretch (no letterboxing)."""
    page_w, page_h = SIZE_LRG

    has_color, _ = is_color_pdf(original_reg_path)

    doc = fitz.open(original_reg_path)
    page = doc[0]
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    doc.close()

    if has_color:
        img = boost_color_image_cmyk(img)
        # Stretch to fill LRG dimensions exactly
        target_w = int(page_w / 72 * dpi)
        target_h = int(page_h / 72 * dpi)
        if img.mode == "CMYK":
            img_stretched = img.resize((target_w, target_h), Image.LANCZOS)
        else:
            img_stretched = img.resize((target_w, target_h), Image.LANCZOS)
        write_color_pdf_compressed(img_stretched, output_lrg_path, SIZE_LRG, dpi=dpi)
    else:
        # B&W: re-trace at higher DPI for quality, then stretch vectors
        doc2 = fitz.open(original_reg_path)
        page2 = doc2[0]
        zoom2 = 300 / 72
        mat2 = fitz.Matrix(zoom2, zoom2)
        pix2 = page2.get_pixmap(matrix=mat2, alpha=False)
        img2 = Image.frombytes("RGB", [pix2.width, pix2.height], pix2.samples)
        doc2.close()

        traced = trace_bitmap(img2)
        bmp_w, bmp_h = img2.size

        # Independent scale factors for X and Y (stretch to fill)
        sx = page_w / bmp_w
        sy = page_h / bmp_h

        c = canvas.Canvas(output_lrg_path, pagesize=(page_w, page_h))
        curves = list(traced)
        areas = [curve_signed_area(cv) for cv in curves]

        ops = ["1 1 1 1 k"]

        for i, (curve, area) in enumerate(zip(curves, areas)):
            if area < -1000000 and len(curve.segments) <= 4:
                continue
            sub_ops = []
            start = curve.start_point
            sub_ops.append(f"{start.x * sx:.4f} {page_h - start.y * sy:.4f} m")
            for seg in curve.segments:
                if seg.is_corner:
                    sub_ops.append(f"{seg.c.x * sx:.4f} {page_h - seg.c.y * sy:.4f} l")
                    sub_ops.append(f"{seg.end_point.x * sx:.4f} {page_h - seg.end_point.y * sy:.4f} l")
                else:
                    sub_ops.append(
                        f"{seg.c1.x * sx:.4f} {page_h - seg.c1.y * sy:.4f} "
                        f"{seg.c2.x * sx:.4f} {page_h - seg.c2.y * sy:.4f} "
                        f"{seg.end_point.x * sx:.4f} {page_h - seg.end_point.y * sy:.4f} c"
                    )
            sub_ops.append("h")
            ops.extend(sub_ops)

        ops.append("f")

        c.saveState()
        for op in ops:
            c._code.append(op)
        c.restoreState()
        c.showPage()
        c.save()


def find_original(output_filename, source_folders):
    """Find the original file for a _RichBlack output.
    For LRG files that were generated from REG, find the REG source."""
    basename = output_filename

    # First check all source folders for exact match
    for folder in source_folders:
        candidate = os.path.join(folder, basename)
        if os.path.exists(candidate):
            return candidate

    # If it's a generated LRG, look for the REG version
    if "LRG" in basename.upper():
        reg_name = basename.replace(" LRG", " REG").replace(" lrg", " REG")
        for folder in source_folders:
            candidate = os.path.join(folder, reg_name)
            if os.path.exists(candidate):
                return candidate

    return None


def is_generated_lrg(filename, source_folder):
    """Check if this LRG file was generated (doesn't exist in source folder)."""
    return not os.path.exists(os.path.join(source_folder, os.path.basename(filename)))


def patch_folder(richblack_folder, source_folder, force_size=None):
    """Scan a _RichBlack folder and fix affected files."""
    pdfs = globmod.glob(os.path.join(richblack_folder, "*.pdf"))
    print(f"Scanning {len(pdfs)} files in {os.path.basename(os.path.dirname(richblack_folder))}/{os.path.basename(richblack_folder)}")

    fixed_large = 0
    fixed_inverted = 0
    fixed_lrg = 0
    errors = 0

    for pdf_path in sorted(pdfs):
        basename = os.path.basename(pdf_path)
        file_size = os.path.getsize(pdf_path)
        file_size_mb = file_size / 1024 / 1024

        try:
            page_size, size_tag = get_target_size(pdf_path, force_size=force_size)

            # Check if this is a generated LRG (not in source folder)
            generated_lrg = is_generated_lrg(pdf_path, source_folder)

            # FIX 3: Generated LRG with wrong proportions - re-stretch
            if generated_lrg and "LRG" in basename.upper():
                reg_name = basename.replace(" LRG", " REG").replace(" lrg", " REG")
                reg_path = os.path.join(source_folder, reg_name)
                if not os.path.exists(reg_path):
                    # Try without size tag
                    base_no_ext = os.path.splitext(reg_name)[0]
                    # Skip if we can't find the source
                    pass
                else:
                    print(f"  [FIX-LRG] {basename} - re-stretching to fill")
                    fix_lrg_stretch(reg_path, pdf_path)
                    fixed_lrg += 1
                    continue

            # Find original for comparison
            original_path = os.path.join(source_folder, basename)

            # FIX 1: Large color files - recompress
            if file_size_mb > 1.0:
                has_color, _ = is_color_pdf(original_path if os.path.exists(original_path) else pdf_path)
                if has_color:
                    print(f"  [FIX-SIZE] {basename} - {file_size_mb:.1f}MB -> recompressing at 150 DPI")
                    doc = fitz.open(original_path if os.path.exists(original_path) else pdf_path)
                    page = doc[0]
                    zoom = 150 / 72
                    mat = fitz.Matrix(zoom, zoom)
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    doc.close()
                    img = boost_color_image_cmyk(img)
                    write_color_pdf_compressed(img, pdf_path, page_size, dpi=150)
                    new_size = os.path.getsize(pdf_path) / 1024 / 1024
                    print(f"           -> {new_size:.1f}MB")
                    fixed_large += 1
                    continue

            # FIX 2: Inverted B&W files
            if os.path.exists(original_path):
                inverted, orig_pct, out_pct = is_inverted(original_path, pdf_path)
                if inverted:
                    print(f"  [FIX-INV] {basename} - inverted (orig {orig_pct:.0f}% dark, output {out_pct:.0f}% dark)")
                    fix_inverted_bw(original_path, pdf_path, page_size)
                    fixed_inverted += 1
                    continue

        except Exception as e:
            print(f"  [ERROR] {basename}: {e}")
            errors += 1

    print(f"\nDone: {fixed_large} size fixes, {fixed_inverted} inversion fixes, {fixed_lrg} LRG fixes, {errors} errors")
    return fixed_large, fixed_inverted, fixed_lrg, errors


if __name__ == "__main__":
    # All folder pairs: (richblack_folder, source_folder, force_size)
    FOLDER_PAIRS = [
        # Coir mats
        (r"G:\My Drive\EMAGINEERED\_New File System 2021\MuckOff (MO)\MO MAT JOB BAGS\MO Print\_MO Statics\_RichBlack",
         r"G:\My Drive\EMAGINEERED\_New File System 2021\MuckOff (MO)\MO MAT JOB BAGS\MO Print\_MO Statics", None),
        (r"G:\My Drive\EMAGINEERED\_New File System 2021\Coir Blimey (CB)\_CB MAT JOB BAGS\_CB Print\_CB Statics\_RichBlack",
         r"G:\My Drive\EMAGINEERED\_New File System 2021\Coir Blimey (CB)\_CB MAT JOB BAGS\_CB Print\_CB Statics", None),
        (r"G:\My Drive\EMAGINEERED\_New File System 2021\Emagineered (EMA)\_EMA MAT JOB BAGS\EMA Print\_EMA Statics\_RichBlack",
         r"G:\My Drive\EMAGINEERED\_New File System 2021\Emagineered (EMA)\_EMA MAT JOB BAGS\EMA Print\_EMA Statics", None),
        (r"G:\My Drive\EMAGINEERED\_New File System 2021\Customat (CMD)\_CMD MAT JOB BAGS\CUS Print\_CMD Statics\_RichBlack",
         r"G:\My Drive\EMAGINEERED\_New File System 2021\Customat (CMD)\_CMD MAT JOB BAGS\CUS Print\_CMD Statics", None),
        (r"G:\My Drive\EMAGINEERED\_New File System 2021\Your Custom Rug (YCR)\_YCR Mat Job Bag\YCR Print\_YCR Statics\_RichBlack",
         r"G:\My Drive\EMAGINEERED\_New File System 2021\Your Custom Rug (YCR)\_YCR Mat Job Bag\YCR Print\_YCR Statics", None),
        (r"G:\My Drive\EMAGINEERED\_New File System 2021\2 Day Doormats (2DD)\2DD Coir Mat Job Bag\2DD Print\_2DD Statics\_RichBlack",
         r"G:\My Drive\EMAGINEERED\_New File System 2021\2 Day Doormats (2DD)\2DD Coir Mat Job Bag\2DD Print\_2DD Statics", None),
        # All-weather
        (r"G:\My Drive\EMAGINEERED\_New File System 2021\Customat (CMD)\_CMD ALL WEATHER MAT JOB BAGS\CUS AW Print\_CMD AW Statics\_RichBlack",
         r"G:\My Drive\EMAGINEERED\_New File System 2021\Customat (CMD)\_CMD ALL WEATHER MAT JOB BAGS\CUS AW Print\_CMD AW Statics", "AW"),
        (r"G:\My Drive\EMAGINEERED\_New File System 2021\Emagineered (EMA)\EMA ALL WEATHER MAT JOB BAG\EMA AW Print\_EMA AW Statics\_RichBlack",
         r"G:\My Drive\EMAGINEERED\_New File System 2021\Emagineered (EMA)\EMA ALL WEATHER MAT JOB BAG\EMA AW Print\_EMA AW Statics", "AW"),
        (r"G:\My Drive\EMAGINEERED\_New File System 2021\2 Day Doormats (2DD)\2DD Weatherproof Mat Job Bag\2DD AW Print\_2DD AW Statics\_RichBlack",
         r"G:\My Drive\EMAGINEERED\_New File System 2021\2 Day Doormats (2DD)\2DD Weatherproof Mat Job Bag\2DD AW Print\_2DD AW Statics", "AW"),
    ]

    total_start = time.time()
    totals = [0, 0, 0, 0]

    for richblack, source, force in FOLDER_PAIRS:
        if not os.path.exists(richblack):
            print(f"Skipping (not yet created): {richblack}")
            continue
        print(f"\n{'='*60}")
        result = patch_folder(richblack, source, force_size=force)
        for i in range(4):
            totals[i] += result[i]

    elapsed = time.time() - total_start
    mins = int(elapsed // 60)
    print(f"\n{'='*60}")
    print(f"ALL PATCHES COMPLETE ({mins}m)")
    print(f"  Size fixes: {totals[0]}")
    print(f"  Inversion fixes: {totals[1]}")
    print(f"  LRG stretch fixes: {totals[2]}")
    print(f"  Errors: {totals[3]}")
