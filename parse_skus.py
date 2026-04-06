import re
from collections import defaultdict

# Read the file list from the folder
import subprocess
result = subprocess.run(
    ['ls', 'G:/My Drive/EMAGINEERED/_New File System 2021/MuckOff (MO)/MO MAT JOB BAGS/MO Print/_MO Statics/'],
    capture_output=True, text=True
)
lines = result.stdout.strip().splitlines()

SIZE_NORM = {
    'REG': 'REG', 'Reg': 'REG', 'reg': 'REG',
    'LRG': 'LRG', 'LAR': 'LRG', 'LARGE': 'LRG',
    'SMA': 'SMA', 'SMALL': 'SMA', 'small': 'SMA',
    'MED': 'MED',
}

sku_sizes = defaultdict(set)
sku_files = defaultdict(lambda: defaultdict(list))
skipped = []

for line in lines:
    fname = line.strip()
    if not fname:
        continue
    if not re.match(r'^M', fname, re.IGNORECASE):
        skipped.append(fname)
        continue
    # Strip extension
    stem = re.sub(r'\.(pdf|jpg|jpeg|png)$', '', fname, flags=re.IGNORECASE)
    # Extract SKU
    sku_match = re.match(r'^(M[A-Za-z0-9_]+)', stem)
    if not sku_match:
        skipped.append(fname)
        continue
    sku = sku_match.group(1).upper()
    remainder = stem[len(sku_match.group(1)):].strip()

    size_found = None
    for tag, norm in SIZE_NORM.items():
        if re.search(r'\b' + re.escape(tag) + r'\b', remainder, re.IGNORECASE):
            size_found = norm
            break

    if size_found is None:
        size_found = 'REG'

    sku_sizes[sku].add(size_found)
    sku_files[sku][size_found].append(fname)

all_skus = sorted(sku_sizes.keys(), key=lambda x: (len(x), x))

print(f"Total unique SKUs: {len(all_skus)}")
print()

reg_only_skus = []
for sku in all_skus:
    sizes = sku_sizes[sku]
    has_reg = 'REG' in sizes
    has_lrg = 'LRG' in sizes
    if has_reg and not has_lrg:
        reg_only_skus.append(sku)

print(f"SKUs with REG but NO LRG/LAR: {len(reg_only_skus)}")
print()
print("=" * 70)
print("REG-ONLY SKUS (need LRG generated) - with their REG filenames:")
print("=" * 70)

for sku in reg_only_skus:
    reg_files = sku_files[sku]['REG']
    other_sizes = sku_sizes[sku] - {'REG'}
    notes = f"  [also has: {', '.join(sorted(other_sizes))}]" if other_sizes else ""
    print(f"\n{sku}{notes}:")
    for f in sorted(reg_files):
        print(f"  {f}")

print()
print("=" * 70)
print("FLAT LIST of all REG filenames for REG-only SKUs:")
print("=" * 70)
all_reg_files = []
for sku in reg_only_skus:
    all_reg_files.extend(sku_files[sku]['REG'])
all_reg_files.sort()
for f in all_reg_files:
    print(f)

print(f"\nTotal REG files needing LRG: {len(all_reg_files)}")
print(f"Total REG-only SKUs: {len(reg_only_skus)}")

lrg_no_reg = [sku for sku in all_skus if 'LRG' in sku_sizes[sku] and 'REG' not in sku_sizes[sku]]
if lrg_no_reg:
    print(f"\nNote: {len(lrg_no_reg)} SKUs have LRG but no REG: {lrg_no_reg}")
