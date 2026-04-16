# Doormat Design Tools

Batch processing pipeline for doormat print files. Vectorizes artwork, enforces rich black CMYK values, boosts colour ink levels, and generates missing LRG size variants.

## Architecture

- `vectorize_v2.py` — Core processing: B&W vectorization (potrace) + colour CMYK boost + inversion validation
- `watcher.py` — Local watcher for the office PC (legacy, replaced by Routines)
- `run_all_folders.py` — Batch runner for all folders
- `reprocess_inverted.py` — One-off script to fix inverted files from earlier runs

## Google Drive folder structure

All folders live under: `EMAGINEERED/_New File System 2021/`

The routine watches the **Print parent folders** recursively (all subfolders including new month folders).

### Coir mat Print folders (7 brands)
| Brand | Print parent folder |
|-------|---------------------|
| MO  | `MuckOff (MO)/MO MAT JOB BAGS/MO Print` |
| CB  | `Coir Blimey (CB)/_CB MAT JOB BAGS/_CB Print` |
| EMA | `Emagineered (EMA)/_EMA MAT JOB BAGS/EMA Print` |
| CMD | `Customat (CMD)/_CMD MAT JOB BAGS/CUS Print` |
| YCR | `Your Custom Rug (YCR)/_YCR Mat Job Bag/YCR Print` |
| 2DD | `2 Day Doormats (2DD)/2DD Coir Mat Job Bag/2DD Print` |
| DD  | `Doormats Direct (DD)/DD Coir Mat Job Bag/DD Print` |

### AW Print folders (5 brands)
| Brand | Print parent folder |
|-------|---------------------|
| CMD-AW | `Customat (CMD)/_CMD ALL WEATHER MAT JOB BAGS/CUS AW Print` |
| EMA-AW | `Emagineered (EMA)/EMA ALL WEATHER MAT JOB BAG/EMA AW Print` |
| 2DD-AW | `2 Day Doormats (2DD)/2DD Weatherproof Mat Job Bag/2DD AW Print` |
| YCR-AW | `Your Custom Rug (YCR)/_YRC Weatherproof Mat Job Bag/YCR AW Print` |
| DD-AW  | `Doormats Direct (DD)/DD Weatherproof Mat Job Bag/DD AW Print` |

### Subfolder types (within each Print folder)
- `_Statics` — permanent catalogue designs (LRG generation applies here only)
- `_Express`, `_Reprints`, `_Bulks` — one-off orders (no LRG generation)
- `Etsy`, `Shopify`, `Amazon`, `eBay` — marketplace orders (no LRG generation)
- Monthly folders (e.g., `Jan 2026`) — personalised orders, created as needed

### Pending: Dropbox folder

There is one additional folder on Dropbox that needs monitoring. Add when
Anthropic ships a Dropbox MCP connector (not available as of 2026-04-16).
Alternative: integrate via Dropbox REST API + access token env var.

## Processing rules

- **B&W designs**: Vectorize with potrace, fill rich black (C100/M100/Y100/K100). Clear 2px bitmap border before tracing to prevent edge-following contour bugs. After writing, validate output vs original — if inverted (>30% more black), fall back to raster CMYK.
- **Colour designs**: Render to CMYK raster, boost ink channels 1.5x, force near-black pixels to rich black.
- **Sizes**: REG=700x400mm, LRG=900x600mm, AW=760x460mm. Exact mm values enforced.
- **LRG generation**: For coir folders, auto-generate LRG variants for any SKU that only has a REG file.

## Routines (cloud watchers)

Two separate Routines monitor Google Drive efficiently:

### Routine 1: Etsy Fast Watcher (hourly)
Monitors ONLY the current-month subfolders inside each brand's Etsy folder. Uses Drive's `modifiedTime > [now - 90 min]` filter so each run only sees recently-saved files — minimal token usage.

### Routine 2: Daily Sweep (once per day, e.g. 2am)
Catches everything else: Statics (with LRG generation), Bulks, Reprints, Express, Amazon, Shopify, eBay. Uses `modifiedTime > [now - 26 hours]` so it only looks at the last day's changes.

### Query efficiency
Both routines use Drive's native `search_files` with `modifiedTime > X AND mimeType = 'application/pdf' AND not name contains '_p.pdf'` — never recursively lists entire folder trees. This keeps token usage low regardless of how many files exist historically.

### Skip rules (both routines)
- Any folder with `OLD` in the name
- Any folder named `_RichBlack` (already processed output)
- Any folder containing `TEST` or `print-tests`
- Subfolders for past months/years inside Etsy (only current month matters)

## Dependencies

```
PyMuPDF pillow potracer reportlab svglib opencv-python-headless numpy
```
