"""Debug: Check slide_deck artifact raw data for page image URLs"""
import json
import os
import re
import sys

os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from notebooklm_client import NotebookLMClient

client = NotebookLMClient()
notebooks = client.list_notebooks()

# Find the notebook
target = None
for nb in notebooks:
    if "OpenClaw Installation Guide" in nb["title"]:
        target = nb
        break

if not target:
    print("Notebook not found")
    sys.exit(1)

print(f'Notebook: {target["title"]}')
notebook_id = target["id"]

# Get raw artifact data
result = client._batchexecute(
    "gArtLc",
    [[2], notebook_id, 'NOT artifact.status = "ARTIFACT_STATUS_SUGGESTED"'],
    source_path=f"/notebook/{notebook_id}",
)

if not result or not result[0]:
    print("No artifacts")
    sys.exit(1)

for art_raw in result[0]:
    art_id = art_raw[0]
    title = art_raw[1]
    type_code = art_raw[2]
    print(f"\nArtifact: {title} (type_code={type_code})")
    print(f"Raw data length: {len(art_raw)} elements")

    # Print structure overview
    for i, item in enumerate(art_raw):
        if item is None:
            print(f"  [{i}] None")
        elif isinstance(item, str):
            s = item[:80] + "..." if len(item) > 80 else item
            print(f"  [{i}] str({len(item)}): {s}")
        elif isinstance(item, (int, float, bool)):
            print(f"  [{i}] {type(item).__name__}: {item}")
        elif isinstance(item, list):
            print(f"  [{i}] list({len(item)})")
        elif isinstance(item, dict):
            print(f"  [{i}] dict({len(item)})")

    # For slide_deck (type_code=8), dump detailed structure
    if type_code == 8:
        print(f"\n  --- Slide Deck details ---")

        # Check art[16] (known PDF URL location)
        if len(art_raw) > 16 and art_raw[16]:
            print(f"  art[16]: list({len(art_raw[16])})")
            for j, sub in enumerate(art_raw[16]):
                if sub is None:
                    print(f"    [16][{j}] None")
                elif isinstance(sub, str):
                    s = sub[:100] + "..." if len(sub) > 100 else sub
                    print(f"    [16][{j}] str: {s}")
                elif isinstance(sub, list):
                    print(f"    [16][{j}] list({len(sub)})")
                elif isinstance(sub, (int, float)):
                    print(f"    [16][{j}] {sub}")

        # Search ALL indices for image URLs
        print(f"\n  --- Searching for image URLs in each index ---")
        for i in range(len(art_raw)):
            if art_raw[i] is None:
                continue
            chunk = json.dumps(art_raw[i])
            img_urls = re.findall(r'https://[^\s\])"\'\\,]+', chunk)
            img_urls = [u for u in img_urls if any(x in u for x in [
                "googleusercontent", "lh3.", "lh5.", ".png", ".jpg", "encrypted", "usercontent"
            ])]
            if img_urls:
                print(f"  [{i}] {len(img_urls)} image URLs:")
                for u in img_urls[:10]:
                    print(f"      {u[:150]}")

        # Dump art[16] fully if it contains slide data
        if len(art_raw) > 16 and art_raw[16]:
            print(f"\n  --- Full art[16] dump ---")
            print(json.dumps(art_raw[16], indent=2, ensure_ascii=False)[:3000])

        # Check if there are per-page image references anywhere
        # Look for patterns like arrays of URLs or image references
        full_json = json.dumps(art_raw, ensure_ascii=False)
        all_urls = re.findall(r'https://[^\s\])"\'\\,]+', full_json)
        print(f"\n  Total URLs in artifact data: {len(all_urls)}")
        for u in sorted(set(all_urls)):
            print(f"    {u[:150]}")
