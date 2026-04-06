# Doormat Design Tools

Batch processing pipeline for doormat print files. Vectorizes artwork, enforces rich black CMYK values, boosts colour ink levels, and generates missing size variants.

Built for Beaudax Enterprise's UV printer workflow.

---

## The Problem

Source PDF design files have two issues that cause poor print quality:

1. **Weak blacks** — Text and graphics use different black values (some K-only, some RGB black) instead of rich black. This prints faded/grey on the UV printer.
2. **Rasterized elements** — Some text is embedded as bitmap images rather than vectors, causing blurry output especially on larger mats.

## What This Does

### For B&W designs (majority of files)
- Renders the PDF at 300 DPI
- Traces all artwork to **vector paths** using potrace (bitmap-to-vector)
- Sets every fill to **rich black: C100 / M100 / Y100 / K100**
- Outputs a fully vectorized PDF that prints sharp at any size

### For colour designs (auto-detected)
- Renders to high-res CMYK raster (300 DPI)
- **Near-black pixels** (R,G,B all < 60) are forced to pure black (0,0,0)
- **Colour pixels** get CMYK channel values boosted by 1.5x for richer ink coverage
- **White pixels** left untouched
- Outputs a CMYK raster PDF — printer driver gets exact ink values with no RGB conversion

### Missing LRG variant generation
- Identifies SKUs that only have a REG file but no LRG/LAR equivalent
- Creates LRG (90x60cm) versions with artwork centered on the larger artboard
- B&W: vector paths are scaled and repositioned
- Colour: image is scaled proportionally and centered on CMYK canvas

---

## Size Conventions

Detected from filename:

| Tag in filename | Size | Use |
|---|---|---|
| `REG` (or no tag) | 700mm x 400mm | Standard coir doormat |
| `LRG` / `LAR` | 900mm x 600mm | Large coir doormat |
| `SMA` / `SMALL` | 900mm x 600mm | Same ratio as LRG |
| `--aw` flag | 760mm x 460mm | All-weather mats (one size only) |

Dimensions are forced to exact mm values so the printer software doesn't crash on fractional sizes.

---

## Files

| File | Purpose |
|---|---|
| `vectorize_v2.py` | Main processing script — handles single files or entire folders |
| `vectorize_richblack.py` | Original v1 script (B&W only, kept for reference) |
| `run_all_folders.py` | Batch runner — processes all 8+ brand folders sequentially |
| `parse_skus.py` | SKU analysis — identifies which designs need LRG variants |
| `batch_report.log` | Generated after a full run with timing and error summary |

---

## Usage

### Single file
```bash
python vectorize_v2.py "G:\My Drive\...\M520 REG.pdf"
```

### Entire folder (with LRG generation)
```bash
python vectorize_v2.py "G:\My Drive\...\MO Print\_MO Statics"
```
Creates a `_RichBlack` subfolder with all processed files.

### Folder without LRG generation
```bash
python vectorize_v2.py "G:\My Drive\...\folder" --no-lrg
```

### All-weather mats (fixed 760x460mm, no LRG)
```bash
python vectorize_v2.py "G:\My Drive\...\AW Statics" --aw
```

### All folders at once
```bash
python run_all_folders.py
```
Processes all brand folders sequentially. Output and report saved to `batch_report.log`.

---

## Folders Being Processed

### Coir mats (full process + LRG generation)

| Brand | Folder | PDF count |
|---|---|---|
| MuckOff (MO) | `_MO Statics` | 493 |
| Coir Blimey (CB) | `_CB Statics` | 79 |
| Emagineered (EMA) | `_EMA Statics` | 1,624 |
| Customat (CMD) | `_CMD Statics` | 1,094 |
| Your Custom Rug (YCR) | `_YCR Statics` | 101 |
| 2 Day Doormats (2DD) | `_2DD Statics` | 434 |

### All-weather mats (process only, no LRG, 760x460mm)

| Brand | Folder | PDF count |
|---|---|---|
| Customat (CMD) | `_CMD AW Statics` | 34 |
| Emagineered (EMA) | `_EMA AW Statics` | 243 |
| 2 Day Doormats (2DD) | `_2DD AW Statics` | 58 |

**Total: ~4,160 PDFs**

---

## Dependencies

```bash
pip install PyMuPDF pillow potracer reportlab svglib opencv-python-headless numpy
```

---

## How It Works (Technical)

1. **Colour detection**: Renders PDF at 72 DPI, checks RGB channel spread per pixel. If >0.5% of pixels have spread >5, it's colour.

2. **B&W vectorization**: Uses [potrace](http://potrace.sourceforge.net/) via the `potracer` Python binding. Potrace traces bitmap contours into bezier curves. The script handles winding direction (CCW = outer contour, CW = hole) and uses PDF's non-zero winding fill rule to correctly render letterforms with holes (e, a, d, etc). The outermost page-boundary rectangle is detected and skipped.

3. **CMYK colour boost**: Converts RGB to CMYK via PIL, then directly multiplies each ink channel by 1.5x (clamped to 255). Near-black pixels are set to C255/M255/Y255/K255. This gives the printer driver exact CMYK values rather than relying on RGB-to-CMYK conversion.

4. **Size enforcement**: Page dimensions are forced to exact mm values using reportlab's mm constant, preventing fractional sizing that crashes the printer software.
