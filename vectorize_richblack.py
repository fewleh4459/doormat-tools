"""
Vectorize PDF images and set all blacks to rich black (C100/M100/Y100/K100).

Workflow:
1. Render PDF page to high-res bitmap
2. Threshold to pure black/white
3. Trace bitmap to vector paths using potrace
4. Output new PDF with all paths in rich CMYK black
"""

import sys
import os
import glob as globmod
import fitz  # PyMuPDF
import numpy as np
from PIL import Image
import potrace
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm as MM


def pdf_to_bitmap(pdf_path, dpi=600):
    """Render PDF page to high-res PIL image and return page dimensions."""
    doc = fitz.open(pdf_path)
    page = doc[0]
    page_rect = page.rect
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    doc.close()
    return img, (page_rect.width, page_rect.height)


def get_target_size(filename):
    """Determine exact target size from filename convention.
    REG (or unknown) = 700mm x 400mm
    LRG/LAR/SMA/SMALL = 900mm x 600mm"""
    name = os.path.basename(filename).upper()
    if any(tag in name for tag in ["LRG", "LAR", "SMA", "SMALL"]):
        return (900 * MM, 600 * MM), "LRG"
    else:
        return (700 * MM, 400 * MM), "REG"


def trace_bitmap(img, threshold=200):
    """Convert PIL image to traced vector paths using potrace."""
    gray = img.convert("L")
    arr = np.array(gray)
    # Invert: trace WHITE areas instead of black
    # Potrace fills CCW paths. In original: black = design, white = background.
    # By tracing white, CCW = background (big rect), CW = design elements.
    # With non-zero winding: we want to trace BLACK, where outer contours of
    # black shapes are CCW and inner holes are CW.
    bw = arr < threshold  # True = black pixel
    bitmap = potrace.Bitmap(bw)
    path = bitmap.trace(
        turdsize=2,
        alphamax=1.0,
        opticurve=True,
        opttolerance=0.2,
    )
    return path


def curve_signed_area(curve):
    """Positive = CW (hole), Negative = CCW (outer contour)."""
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
    """Generate PDF path operators for a single curve."""
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


def reverse_curve_ops(curve, sx, sy, page_h):
    """Generate PDF path operators for a curve with REVERSED winding direction."""
    # Collect all points first
    points = [(curve.start_point.x, curve.start_point.y)]
    segments_data = []
    for seg in curve.segments:
        if seg.is_corner:
            segments_data.append(('corner', (seg.c.x, seg.c.y), (seg.end_point.x, seg.end_point.y)))
        else:
            segments_data.append(('bezier', (seg.c1.x, seg.c1.y), (seg.c2.x, seg.c2.y), (seg.end_point.x, seg.end_point.y)))
        points.append((seg.end_point.x, seg.end_point.y))

    # Reverse: start from the last endpoint, go backwards
    ops = []
    last_pt = points[-1]
    ops.append(f"{last_pt[0] * sx:.4f} {page_h - last_pt[1] * sy:.4f} m")

    for i in range(len(segments_data) - 1, -1, -1):
        seg = segments_data[i]
        prev_pt = points[i]  # the start point of original segment
        if seg[0] == 'corner':
            # Original: prev_pt -> seg[1] -> seg[2]
            # Reversed: seg[2] is current pos, go to seg[1] then prev_pt
            ops.append(f"{seg[1][0] * sx:.4f} {page_h - seg[1][1] * sy:.4f} l")
            ops.append(f"{prev_pt[0] * sx:.4f} {page_h - prev_pt[1] * sy:.4f} l")
        else:
            # Original bezier: prev_pt, c1, c2, end
            # Reversed: end, c2, c1, prev_pt
            ops.append(
                f"{seg[2][0] * sx:.4f} {page_h - seg[2][1] * sy:.4f} "
                f"{seg[1][0] * sx:.4f} {page_h - seg[1][1] * sy:.4f} "
                f"{prev_pt[0] * sx:.4f} {page_h - prev_pt[1] * sy:.4f} c"
            )

    ops.append("h")
    return ops


def paths_to_pdf(traced_path, output_path, page_size, bitmap_size):
    """Write traced paths to PDF with rich black fill.

    Strategy: Skip the outermost page-boundary contour, then use non-zero
    winding rule. Outer contours of shapes are CCW (fill), holes are CW (subtract).
    We reverse the winding so CCW becomes CW and vice versa, since potrace's
    coordinate system is Y-down but PDF is Y-up (the Y flip reverses winding).
    """
    page_w, page_h = page_size
    bmp_w, bmp_h = bitmap_size
    sx = page_w / bmp_w
    sy = page_h / bmp_h

    c = canvas.Canvas(output_path, pagesize=(page_w, page_h))

    curves = list(traced_path)
    areas = [curve_signed_area(cv) for cv in curves]

    # Debug: print winding info
    for i, (cv, area) in enumerate(zip(curves[:10], areas[:10])):
        direction = "CCW (outer)" if area < 0 else "CW (hole)"
        print(f"    Curve {i}: segs={len(cv.segments)}, area={area:.0f}, {direction}")
    if len(curves) > 10:
        print(f"    ... and {len(curves) - 10} more curves")

    ops = []
    ops.append("1 1 1 1 k")  # Rich black CMYK fill

    # The Y-flip (page_h - y) reverses winding direction.
    # Potrace: CCW = outer contour, CW = hole
    # After Y-flip: CCW becomes CW, CW becomes CCW
    # Non-zero winding rule: CW fills, CCW subtracts (or vice versa)
    #
    # So after flip: original outers (CCW) → CW → fill
    #                original holes (CW) → CCW → subtract
    # This is exactly what we want! Just use non-zero winding (f).
    #
    # BUT: the very first curve (Curve 0) is a huge outer rectangle that
    # encompasses the entire design. After flip it becomes CW → fills the
    # whole page black. We need to skip it.

    # Find the page boundary: simple rectangle (few segments, large CCW area)
    skip_idx = None
    min_area = 0
    for i, (cv, area) in enumerate(zip(curves, areas)):
        if area < min_area and len(cv.segments) <= 4:
            min_area = area
            skip_idx = i

    if skip_idx is not None:
        print(f"  Skipping curve {skip_idx} (page boundary, area={min_area:.0f})")

    for i, curve in enumerate(curves):
        if i == skip_idx:
            continue
        ops.extend(emit_curve_ops(curve, sx, sy, page_h))

    # Non-zero winding fill
    ops.append("f")

    c.saveState()
    for op in ops:
        c._code.append(op)
    c.restoreState()
    c.showPage()
    c.save()


def process_pdf(input_path, output_path=None, dpi=600):
    if output_path is None:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_richblack{ext}"

    print(f"Processing: {os.path.basename(input_path)}")
    print(f"  Rendering at {dpi} DPI...")
    img, source_size = pdf_to_bitmap(input_path, dpi=dpi)
    print(f"  Bitmap: {img.size[0]}x{img.size[1]}")

    # Use exact target size from filename
    page_size, size_tag = get_target_size(input_path)
    w_mm = round(page_size[0] / MM)
    h_mm = round(page_size[1] / MM)
    print(f"  Target size: {w_mm}mm x {h_mm}mm ({size_tag})")

    print("  Tracing vectors...")
    traced = trace_bitmap(img)
    print(f"  Found {len(traced)} vector paths")

    print("  Writing vector PDF (rich black C100/M100/Y100/K100)...")
    paths_to_pdf(traced, output_path, page_size, img.size)

    out_size = os.path.getsize(output_path)
    print(f"  Done! -> {output_path} ({out_size // 1024} KB)")
    return output_path


def process_folder(folder_path, dpi=600):
    output_dir = os.path.join(folder_path, "_RichBlack")
    os.makedirs(output_dir, exist_ok=True)

    pdfs = globmod.glob(os.path.join(folder_path, "*.pdf"))
    print(f"Found {len(pdfs)} PDF(s) in {folder_path}")
    print(f"Output dir: {output_dir}\n")

    done = 0
    errors = 0
    for pdf in sorted(pdfs):
        basename = os.path.basename(pdf)
        output_path = os.path.join(output_dir, basename)
        try:
            process_pdf(pdf, output_path=output_path, dpi=dpi)
            done += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            errors += 1
        print()

    print(f"=== COMPLETE: {done} processed, {errors} errors ===")


if __name__ == "__main__":
    dpi = 300  # 300 DPI is sufficient for UV printer (720dpi) on doormats
    target = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Olly\Downloads\M520  REG.pdf"
    if os.path.isdir(target):
        process_folder(target, dpi=dpi)
    else:
        process_pdf(target, dpi=dpi)
