#!/usr/bin/env python3
"""
For-sale scraper for CCARCS dashboard.
Uses Playwright with a real browser — must run locally (residential IP).
GitHub Actions servers are blocked by Cloudflare on these sites.

Requirements: pip install playwright && playwright install chromium
Usage:        python scrape_forsale.py
              python update_data.py    (run after to rebuild data.js)
"""
import json
import re
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    import sys
    sys.exit('ERROR: playwright not installed.\n  pip install playwright && playwright install chromium')

PROJECT_DIR  = Path(__file__).parent
FORSALE_FILE = PROJECT_DIR / 'forsale_manual.json'
DELAY        = 1.5   # seconds between detail page fetches
MAX_PAGES    = 50    # safety cap on pagination

SCRAPED_SOURCES = {'trade-a-plane.com', 'controller.com', 'globalair.com'}

GLOBALAIR_PROVINCES = [
    'ontario', 'british-columbia', 'alberta', 'quebec', 'manitoba',
    'saskatchewan', 'nova-scotia', 'new-brunswick', 'newfoundland',
    'prince-edward-island', 'northwest-territories', 'nunavut', 'yukon',
]


# ── Trade-A-Plane ─────────────────────────────────────────────────────────────
# Controller.com is blocked by Cloudflare for automated requests.
# Use the bookmarklet in forsale_extractor.html to collect Controller.com data manually.

def scrape_trade_a_plane(page) -> dict:
    print('[Trade-A-Plane] Starting...')
    BASE    = 'https://www.trade-a-plane.com'
    results = {}

    listing_ids = []
    pg = 1
    while pg <= MAX_PAGES:
        if pg == 1:
            url = BASE + '/filtered/search?s-type=aircraft&s-keyword-search=canada&s-original-search=canada'
        else:
            url = BASE + f'/search?s-type=aircraft&s-keyword-search=canada&s-original-search=canada&s-page={pg}'
        try:
            page.goto(url, timeout=30000)
            time.sleep(3)
        except Exception as e:
            print(f'  Page {pg} load error: {e}')
            break
        html = page.content()
        ids  = list(dict.fromkeys(re.findall(r'listing_id=(\d+)', html)))
        if not ids:
            break
        print(f'  Page {pg}: {len(ids)} listings')
        listing_ids.extend(ids)
        if f's-page={pg + 1}' not in html:
            break
        pg += 1

    listing_ids = list(dict.fromkeys(listing_ids))
    print(f'  Total unique listings: {len(listing_ids)}')

    for i, lid in enumerate(listing_ids):
        detail_url = f'{BASE}/search?listing_id={lid}&s-type=aircraft'
        try:
            page.goto(detail_url, timeout=20000)
            time.sleep(DELAY)
            html = page.content()
            m = re.search(r'\bC-([A-Z]{4})\b', html)
            if m:
                reg     = 'C-' + m.group(1)
                price_m = re.search(r'\$\s*([\d,]+)', html)
                price   = '$' + price_m.group(1) if price_m else None
                results[reg] = {'source': 'trade-a-plane.com', 'url': detail_url, 'price': price}
                print(f'  [{i+1}/{len(listing_ids)}] {reg}  {price or "no price"}')
            else:
                print(f'  [{i+1}/{len(listing_ids)}] no C-reg')
        except Exception as e:
            print(f'  [{i+1}/{len(listing_ids)}] ERROR: {e}')

    print(f'  -> {len(results)} C-registrations found')
    return results


# ── GlobalAir.com ────────────────────────────────────────────────────────────

def scrape_globalair(page) -> dict:
    print('[GlobalAir.com] Starting...')
    BASE    = 'https://www.globalair.com'
    results = {}
    pairs   = {}  # listing_url -> C-reg (extracted from search pages)

    for prov in GLOBALAIR_PROVINCES:
        url = f'{BASE}/aircraft-for-sale/aircraft-in-{prov}'
        try:
            page.goto(url, timeout=30000)
            time.sleep(3)
            html = page.content()
        except Exception as e:
            print(f'  {prov}: load error {e}')
            continue

        # Each listing block: the detail link appears just before the C-reg in the HTML
        segments = re.split(r'(listing-detail/[^\s"\'<>]+)', html)
        found = 0
        for i in range(1, len(segments), 2):
            link  = f'{BASE}/aircraft-for-sale/{segments[i]}'
            chunk = segments[i + 1][:600] if i + 1 < len(segments) else ''
            m     = re.search(r'\bC-[A-Z]{4}\b', chunk)
            if m and link not in pairs:
                pairs[link] = m.group(0)
                found += 1
        if found:
            print(f'  {prov}: {found} listings')

    if not pairs:
        print('  No listings found across all provinces')
        return results

    # GlobalAir challenges detail-page requests after the search crawl (Cloudflare).
    # Skip detail visits — URL and C-reg are enough; price is available on the listing page.
    for detail_url, reg in pairs.items():
        results[reg] = {'source': 'globalair.com', 'url': detail_url, 'price': None}
        print(f'  {reg}  -> {detail_url.split("/")[-2]}')

    print(f'  -> {len(results)} C-registrations found')
    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    existing = {}
    if FORSALE_FILE.exists():
        try:
            existing = json.loads(FORSALE_FILE.read_text(encoding='utf-8'))
            print(f'Loaded {len(existing)} existing entries from forsale_manual.json\n')
        except Exception as e:
            print(f'WARNING: could not read forsale_manual.json: {e}')

    all_new          = {}
    scraped_ok_sources = set()  # only sources that succeeded — stale removal is scoped to these

    with sync_playwright() as p:
        # headless=False avoids most Cloudflare bot-detection fingerprinting
        browser = p.chromium.launch(headless=False)
        pg      = browser.new_context().new_page()

        for scraper, source_name in [
            (scrape_trade_a_plane, 'trade-a-plane.com'),
            (scrape_globalair,     'globalair.com'),
        ]:
            try:
                found = scraper(pg)
                all_new.update(found)
                scraped_ok_sources.add(source_name)
            except Exception as e:
                print(f'  SCRAPER FAILED: {e}')
            print()

        # Controller.com is blocked by Cloudflare for Playwright — use the bookmarklet instead
        # (see forsale_extractor.html)

        browser.close()

    # Merge: scraped data updates existing, but preserve manually-added entries
    # and carry forward the first-seen date on unchanged listings.
    merged = dict(existing)
    for reg, entry in all_new.items():
        prev_date     = (merged.get(reg) or {}).get('date')
        entry['date'] = prev_date  # date will be set by update_data.py on first run
        merged[reg]   = entry

    # Drop stale entries only from sources we successfully scraped this run
    stale = [r for r, v in merged.items()
             if v.get('source') in scraped_ok_sources and r not in all_new]
    for r in stale:
        del merged[r]

    FORSALE_FILE.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding='utf-8')

    print(f'forsale_manual.json updated:')
    print(f'  {len(all_new)} scraped  |  {len(stale)} stale removed  |  {len(merged)} total entries')
    print()
    print('Next step: run  python update_data.py  to rebuild data.js')


if __name__ == '__main__':
    main()
