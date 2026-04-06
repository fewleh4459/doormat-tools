"""
Vectorize & enhance PDF doormat designs.

Features:
- Auto-detects color vs B&W
- B&W: vectorizes with potrace, sets all fills to rich black (C100/M100/Y100/K100)
- Color: boosts CMYK density, outputs high-res raster PDF
- Generates missing LRG (90x60cm) variants from REG-only SKUs
- Outputs to _RichBlack subfolder

Size conventions (from filename):
  REG (or unknown) = 700mm x 400mm
  LRG/LAR/SMA/SMALL = 900mm x 600mm
"""

import sys
import os
import re
import glob as globmod
import fitz  # PyMuPDF
import numpy as np
from PIL import Image, ImageEnhance
import potrace
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm as MM
from reportlab.lib.utils import ImageReader
import io


# ── Size helpers ──────────────────────────────────────────────────────────────

SIZE_REG = (700 * MM, 400 * MM)
SIZE_LRG = (900 * MM, 600 * MM)
SIZE_AW = (760 * MM, 460 * MM)  # All-weather mats: 76x46cm


def get_target_size(filename, force_size=None):
    """Determine target size from filename or override.
    REG/default → 700x400mm, LRG/LAR/SMA/SMALL → 900x600mm, AW → 760x460mm."""
    if force_size == "AW":
        return SIZE_AW, "AW"
    name = os.path.basename(filename).upper()
    if any(tag in name for tag in ["LRG", "LAR", "SMA", "SMALL"]):
        return SIZE_LRG, "LRG"
    else:
        return SIZE_REG, "REG"


def get_sku(filename):
    """Extract SKU (e.g. 'M520') from filename."""
    base = os.path.basename(filename)
    match = re.match(r'(M_?\w+?)[\s_]', base)
    if match:
        return match.group(1)
    base_no_ext = os.path.splitext(base)[0]
    return base_no_ext.split()[0] if ' ' in base_no_ext else base_no_ext


# ── Color detection ───────────────────────────────────────────────────────────

def is_color_pdf(pdf_path, threshold=5, min_colored_pct=0.5):
    """Check if a PDF contains significant color content."""
    doc = fitz.open(pdf_path)
    page = doc[0]
    pix = page.get_pixmap(matrix=fitz.Matrix(72 / 72, 72 / 72), alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    doc.close()

    arr = np.array(img, dtype=np.int16)
    # Color spread: max difference between R, G, B channels per pixel
    spread = arr.max(axis=2) - arr.min(axis=2)
    colored_pixels = (spread > threshold).sum()
    total_pixels = arr.shape[0] * arr.shape[1]
    pct = (colored_pixels / total_pixels) * 100
    return pct > min_colored_pct, pct


# ── PDF rendering ─────────────────────────────────────────────────────────────

def pdf_to_bitmap(pdf_path, dpi=300):
    """Render PDF page to PIL image."""
    doc = fitz.open(pdf_path)
    page = doc[0]
    page_rect = page.rect
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    doc.close()
    return img, (page_rect.width, page_rect.height)


# ── B&W vectorization pipeline ────────────────────────────────────────────────

def trace_bitmap(img, threshold=200):
    """Trace B&W image to vector paths."""
    gray = img.convert("L")
    arr = np.array(gray)
    bw = arr < threshold
    bitmap = potrace.Bitmap(bw)
    path = bitmap.trace(
        turdsize=2, alphamax=1.0, opticurve=True, opttolerance=0.2,
    )
    return path


def curve_signed_area(curve):
    pts = [(curve.start_point.x, curve.start_point.y)]
    for seg in curve.segments:
        pts.append((seg.end_point.x, seg.end_point.y))
    area = 0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        area += (x2 - x1) * (y2 + y1)
    return area / 2


def emit_curve_ops(curve, sx, sy, page_h):
    ops = []
    start = curve.start_point
    ops.append(f"{start.x * sx:.4f} {page_h - start.y * sy:.4f} m")
    for seg in curve.segments:
        if seg.is_corner:
            ops.append(f"{seg.c.x * sx:.4f} {page_h - seg.c.y * sy:.4f} l")
            ops.append(f"{seg.end_point.x * sx:.4f} {page_h - seg.end_point.y * sy:.4f} l")
        else:
            ops.append(
                f"{seg.c1.x * sx:.4f} {page_h - seg.c1.y * sy:.4f} "
                f"{seg.c2.x * sx:.4f} {page_h - seg.c2.y * sy:.4f} "
                f"{seg.end_point.x * sx:.4f} {page_h - seg.end_point.y * sy:.4f} c"
            )
    ops.append("h")
    return ops


def write_bw_vector_pdf(traced_path, output_path, page_size, bitmap_size):
    """Write traced B&W paths as rich black vector PDF."""
    page_w, page_h = page_size
    bmp_w, bmp_h = bitmap_size
    sx = page_w / bmp_w
    sy = page_h / bmp_h

    c = canvas.Canvas(output_path, pagesize=(page_w, page_h))
    curves = list(traced_path)
    areas = [curve_signed_area(cv) for cv in curves]

    ops = ["1 1 1 1 k"]  # Rich black CMYK

    # Skip page boundary (largest CCW rect with <=4 segments)
    skip_idx = None
    min_area = 0
    for i, (cv, area) in enumerate(zip(curves, areas)):
        if area < min_area and len(cv.segments) <= 4:
            min_area = area
            skip_idx = i

    for i, curve in enumerate(curves):
        if i == skip_idx:
            continue
        ops.extend(emit_curve_ops(curve, sx, sy, page_h))

    ops.append("f")  # Non-zero winding fill

    c.saveState()
    for op in ops:
        c._code.append(op)
    c.restoreState()
    c.showPage()
    c.save()


# ── Color enhancement pipeline ────────────────────────────────────────────────

def boost_color_image_cmyk(img, ink_boost=1.5, black_threshold=60):
    """Boost color image directly in CMYK space for accurate print output.

    - Converts RGB to CMYK
    - Near-black pixels → rich black (C=255, M=255, Y=255, K=255)
    - Colour pixels → each CMYK channel boosted by ink_boost factor
    - White pixels left untouched
    - Returns a CMYK PIL image ready for PDF embedding

    ink_boost: multiplier for CMYK channel values (1.5 = 50% more ink)
    """
    # Convert to CMYK
    cmyk = img.convert("CMYK")
    arr = np.array(cmyk, dtype=np.float32)

    # Also check original RGB for near-black detection
    rgb = np.array(img)
    near_black = (rgb[:, :, 0] < black_threshold) & \
                 (rgb[:, :, 1] < black_threshold) & \
                 (rgb[:, :, 2] < black_threshold)

    # Detect white/near-white pixels (leave untouched)
    near_white = (rgb[:, :, 0] > 240) & \
                 (rgb[:, :, 1] > 240) & \
                 (rgb[:, :, 2] > 240)

    # Boost all CMYK channels for colour pixels
    colour_mask = ~near_black & ~near_white
    arr[colour_mask] = np.clip(arr[colour_mask] * ink_boost, 0, 255)

    # Force near-black to rich black (all channels maxed)
    arr[near_black] = [255, 255, 255, 255]

    return Image.fromarray(arr.astype(np.uint8), mode="CMYK")


def write_color_pdf(img, output_path, page_size):
    """Write a CMYK image as a high-res PDF with embedded CMYK data."""
    page_w, page_h = page_size

    # Save CMYK image as TIFF (supports CMYK natively)
    img_buffer = io.BytesIO()
    if img.mode == "CMYK":
        img.save(img_buffer, format='TIFF')
    else:
        img.save(img_buffer, format='PNG')
    img_buffer.seek(0)

    c = canvas.Canvas(output_path, pagesize=(page_w, page_h))
    c.drawImage(ImageReader(img_buffer), 0, 0, width=page_w, height=page_h)
    c.showPage()
    c.save()


# ── Main processing ───────────────────────────────────────────────────────────

def process_pdf(input_path, output_path=None, dpi=300, force_size=None):
    """Process a single PDF: auto-detect color, vectorize or enhance."""
    if output_path is None:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_richblack{ext}"

    basename = os.path.basename(input_path)
    page_size, size_tag = get_target_size(input_path, force_size=force_size)
    w_mm = round(page_size[0] / MM)
    h_mm = round(page_size[1] / MM)

    # Detect color
    has_color, color_pct = is_color_pdf(input_path)

    if has_color:
        print(f"[COLOR] {basename} -> {w_mm}x{h_mm}mm ({size_tag}, {color_pct:.1f}% color)")
        img, _ = pdf_to_bitmap(input_path, dpi=dpi)
        img = boost_color_image_cmyk(img)
        write_color_pdf(img, output_path, page_size)
    else:
        print(f"[B&W]   {basename} -> {w_mm}x{h_mm}mm ({size_tag})")
        img, _ = pdf_to_bitmap(input_path, dpi=dpi)
        traced = trace_bitmap(img)
        write_bw_vector_pdf(traced, output_path, page_size, img.size)

    out_kb = os.path.getsize(output_path) // 1024
    print(f"        -> {os.path.basename(output_path)} ({out_kb} KB)")


def generate_lrg_from_reg(reg_path, output_path, dpi=300):
    """Create an LRG (90x60cm) version from a REG file, centered on artboard."""
    basename = os.path.basename(reg_path)
    page_w, page_h = SIZE_LRG
    w_mm, h_mm = 900, 600

    has_color, _ = is_color_pdf(reg_path)
    img, _ = pdf_to_bitmap(reg_path, dpi=dpi)

    if has_color:
        img = boost_color_image_cmyk(img)
        # Scale to fit within LRG artboard, maintaining aspect ratio
        img_w, img_h = img.size
        scale = min((page_w / MM) / (700), (page_h / MM) / (400))
        new_w = int(img_w * scale)
        new_h = int(img_h * scale)
        img_scaled = img.resize((new_w, new_h), Image.LANCZOS)

        # Create white canvas at LRG resolution in CMYK
        lrg_w = int(900 * dpi / 25.4)
        lrg_h = int(600 * dpi / 25.4)
        # CMYK white = (0, 0, 0, 0)
        canvas_img = Image.new("CMYK", (lrg_w, lrg_h), (0, 0, 0, 0))
        # Center
        x_off = (lrg_w - new_w) // 2
        y_off = (lrg_h - new_h) // 2
        canvas_img.paste(img_scaled, (x_off, y_off))

        print(f"[COLOR-LRG] {basename} -> {w_mm}x{h_mm}mm (centered)")
        write_color_pdf(canvas_img, output_path, SIZE_LRG)
    else:
        # B&W: trace, then scale and center vectors on LRG artboard
        traced = trace_bitmap(img)
        bmp_w, bmp_h = img.size

        # Calculate scaling to center REG content on LRG page
        reg_w, reg_h = SIZE_REG
        # Scale factor to fit REG into LRG maintaining aspect ratio
        scale = min(page_w / reg_w, page_h / reg_h)
        scaled_w = reg_w * scale
        scaled_h = reg_h * scale
        x_offset = (page_w - scaled_w) / 2
        y_offset = (page_h - scaled_h) / 2

        sx = scaled_w / bmp_w
        sy = scaled_h / bmp_h

        c = canvas.Canvas(output_path, pagesize=(page_w, page_h))
        curves = list(traced)
        areas = [curve_signed_area(cv) for cv in curves]

        ops = ["1 1 1 1 k"]

        skip_idx = None
        min_area = 0
        for i, (cv, area) in enumerate(zip(curves, areas)):
            if area < min_area and len(cv.segments) <= 4:
                min_area = area
                skip_idx = i

        # Emit curves with offset for centering
        for i, curve in enumerate(curves):
            if i == skip_idx:
                continue
            sub_ops = []
            start = curve.start_point
            sub_ops.append(f"{start.x * sx + x_offset:.4f} {page_h - (start.y * sy + y_offset):.4f} m")
            for seg in curve.segments:
                if seg.is_corner:
                    sub_ops.append(f"{seg.c.x * sx + x_offset:.4f} {page_h - (seg.c.y * sy + y_offset):.4f} l")
                    sub_ops.append(f"{seg.end_point.x * sx + x_offset:.4f} {page_h - (seg.end_point.y * sy + y_offset):.4f} l")
                else:
                    sub_ops.append(
                        f"{seg.c1.x * sx + x_offset:.4f} {page_h - (seg.c1.y * sy + y_offset):.4f} "
                        f"{seg.c2.x * sx + x_offset:.4f} {page_h - (seg.c2.y * sy + y_offset):.4f} "
                        f"{seg.end_point.x * sx + x_offset:.4f} {page_h - (seg.end_point.y * sy + y_offset):.4f} c"
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

        print(f"[B&W-LRG]   {basename} -> {w_mm}x{h_mm}mm (centered)")

    out_kb = os.path.getsize(output_path) // 1024
    print(f"             -> {os.path.basename(output_path)} ({out_kb} KB)")


def process_folder(folder_path, dpi=300, generate_missing_lrg=True, force_size=None):
    """Process all PDFs: enhance existing + generate missing LRG variants."""
    output_dir = os.path.join(folder_path, "_RichBlack")
    os.makedirs(output_dir, exist_ok=True)

    pdfs = globmod.glob(os.path.join(folder_path, "*.pdf"))
    print(f"Found {len(pdfs)} PDF(s) in {folder_path}")
    print(f"Output dir: {output_dir}\n")

    # ── Pass 1: Process all existing files ──
    print("=" * 60)
    print("PASS 1: Processing existing files")
    print("=" * 60)
    done = 0
    errors = 0
    for pdf in sorted(pdfs):
        basename = os.path.basename(pdf)
        output_path = os.path.join(output_dir, basename)
        try:
            process_pdf(pdf, output_path=output_path, dpi=dpi, force_size=force_size)
            done += 1
        except Exception as e:
            print(f"  ERROR on {basename}: {e}")
            errors += 1

    print(f"\nPass 1 complete: {done} processed, {errors} errors\n")

    # ── Pass 2: Generate missing LRG variants ──
    if generate_missing_lrg:
        print("=" * 60)
        print("PASS 2: Generating missing LRG variants")
        print("=" * 60)

        # Build SKU -> files map
        sku_files = {}
        for pdf in pdfs:
            sku = get_sku(pdf)
            if sku not in sku_files:
                sku_files[sku] = []
            sku_files[sku].append(pdf)

        # Find SKUs with REG but no LRG/LAR
        lrg_generated = 0
        for sku, files in sorted(sku_files.items()):
            names_upper = [os.path.basename(f).upper() for f in files]
            has_lrg = any(any(tag in n for tag in ["LRG", "LAR"]) for n in names_upper)
            has_reg = any("REG" in n or not any(tag in n for tag in ["LRG", "LAR", "SMA", "SMALL"]) for n in names_upper)

            if has_reg and not has_lrg:
                # Get all REG files for this SKU
                reg_files = [f for f in files if "LRG" not in os.path.basename(f).upper()
                             and "LAR" not in os.path.basename(f).upper()
                             and "SMA" not in os.path.basename(f).upper()
                             and "SMALL" not in os.path.basename(f).upper()]
                for reg_path in reg_files:
                    # Create LRG filename
                    reg_name = os.path.basename(reg_path)
                    if "REG" in reg_name.upper():
                        lrg_name = reg_name.replace(" REG", " LRG").replace(" reg", " LRG")
                    else:
                        # No size tag - add LRG before extension
                        base, ext = os.path.splitext(reg_name)
                        lrg_name = f"{base} LRG{ext}"
                    output_path = os.path.join(output_dir, lrg_name)
                    try:
                        generate_lrg_from_reg(reg_path, output_path, dpi=dpi)
                        lrg_generated += 1
                    except Exception as e:
                        print(f"  ERROR generating LRG for {reg_name}: {e}")

        print(f"\nPass 2 complete: {lrg_generated} LRG variants generated\n")

    print("=" * 60)
    print("ALL DONE")
    print("=" * 60)


if __name__ == "__main__":
    dpi = 300
    target = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Olly\Downloads\M520  REG.pdf"

    # Optional flags
    skip_lrg = "--no-lrg" in sys.argv
    lrg_only = "--lrg-only" in sys.argv
    aw_mode = "--aw" in sys.argv  # All-weather: force 760x460mm, no LRG gen
    force_size = "AW" if aw_mode else None

    if os.path.isdir(target):
        if aw_mode:
            process_folder(target, dpi=dpi, generate_missing_lrg=False, force_size="AW")
        elif lrg_only:
            process_folder(target, dpi=dpi, generate_missing_lrg=True)
        else:
            process_folder(target, dpi=dpi, generate_missing_lrg=not skip_lrg)
    else:
        process_pdf(target, dpi=dpi, force_size=force_size)
