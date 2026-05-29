#!/usr/bin/env python3
"""
CCARCS Data Updater (Python cross-platform version)
Downloads the latest Canadian Civil Aircraft Register,
rebuilds the SQLite database, and regenerates data.js

Requirements: pip install requests
Usage:        python update_data.py
"""

import csv
import json
import re
import sqlite3
import sys
import zipfile
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("ERROR: 'requests' library not found. Run:  pip install requests")

PROJECT_DIR  = Path(__file__).parent
DATA_DIR     = PROJECT_DIR / 'CCARCS_data'
FAA_DIR      = PROJECT_DIR / 'faa_data'
DB_PATH      = PROJECT_DIR / 'ccarcs.db'
DATA_JS      = PROJECT_DIR / 'data.js'
ADSB_FILE    = PROJECT_DIR / 'adsb_seen.json'
PAGE_URL     = 'https://wwwapps.tc.gc.ca/Saf-Sec-Sur/2/CCARCS-RIACC/DDZip.aspx'
FAA_URL      = 'https://registry.faa.gov/database/ReleasableAircraft.zip'
CADORS_BASE  = 'https://opendatatc.tc.canada.ca'


def build_forsale_lookup(records):
    """
    For-sale detection placeholder.

    Automated scraping doesn't work reliably:
    - Trade-A-Plane / Controller block requests (403)
    - Barnstormers keyword search ignores the keyword parameter on listing.php
    - No accessible site exposes C-registrations in scrapable form

    The _for_sale field and dashboard UI are in place.
    To populate data, either:
      (a) Subscribe to an aviation data API (JetNet, ACAS, AvData)
      (b) Add a forsale_manual.json file: {"C-FABC": {"source": "...", "url": "...", "price": "..."}}
    """
    manual_file = PROJECT_DIR / 'forsale_manual.json'
    if manual_file.exists():
        try:
            data = json.loads(manual_file.read_text(encoding='utf-8'))
            print(f'[ForSale] Loaded {len(data)} manual entries from forsale_manual.json')
            return data
        except Exception as e:
            print(f'[ForSale] Could not read forsale_manual.json: {e}')
    return {}


def build_adsb_lookup():
    """Query OpenSky for Canadian airspace, accumulate last-seen dates in adsb_seen.json.
    Returns raw-Registration -> last_seen_date dict."""
    print('[ADS-B] Querying OpenSky for Canadian airspace...')

    # Build ICAO24 hex -> raw Registration mapping (skip manufacturer=Other)
    icao_to_reg = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        for reg, mfr, mode_s in conn.execute(
            'SELECT Registration, Manufacturer, MODE_S_TRANSPONDER_BINARY FROM aircraft'
        ):
            mode_s = (mode_s or '').strip()
            if not mode_s or (mfr or '').strip().lower() == 'other':
                continue
            try:
                icao_to_reg[format(int(mode_s, 2), '06x')] = reg
            except ValueError:
                pass
        conn.close()
    except Exception as e:
        print(f'  WARNING: DB query failed: {e}')
        return {}
    print(f'  {len(icao_to_reg):,} ICAO24 codes mapped')

    # Load existing history
    history = {}
    if ADSB_FILE.exists():
        try:
            history = json.loads(ADSB_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass

    today = date.today().isoformat()
    try:
        resp = requests.get(
            'https://opensky-network.org/api/states/all',
            params={'lamin': 41, 'lamax': 84, 'lomin': -141, 'lomax': -52},
            timeout=60,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        if resp.status_code == 200:
            states  = resp.json().get('states') or []
            matched = 0
            for state in states:
                icao24 = (state[0] or '').lower().strip()
                if icao24 in icao_to_reg:
                    history[icao24] = {'reg': icao_to_reg[icao24], 'last_seen': today}
                    matched += 1
            print(f'  {len(states):,} aircraft in snapshot, {matched} matched to C-regs')
        else:
            print(f'  WARNING: OpenSky returned HTTP {resp.status_code}')
    except Exception as e:
        print(f'  WARNING: OpenSky query failed: {e}')

    ADSB_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding='utf-8')
    return {v['reg']: v['last_seen'] for v in history.values()}


def build_cadors_lookup():
    """Download CADORS open data CSVs and return C-reg -> {count, latest} dict.
    Covers occurrences from the last 5 years only."""
    print('[CADORS] Downloading occurrence data...')
    from datetime import date as _date
    cutoff = str(_date.today().year - 5)

    def _get(filename):
        resp = requests.get(f'{CADORS_BASE}/{filename}', timeout=180,
                            headers={'User-Agent': 'Mozilla/5.0'})
        resp.raise_for_status()
        return resp.content.decode('utf-8-sig').splitlines()

    def _col(fieldnames, *candidates):
        """Case-insensitive, underscore-agnostic column lookup."""
        norm = {f.upper().replace('_', ''): f for f in fieldnames}
        for c in candidates:
            key = c.upper().replace('_', '')
            if key in norm:
                return norm[key]
        print(f'  WARNING: none of {candidates} found in cols: {list(fieldnames)[:15]}')
        return None

    try:
        occ_lines = _get('CADORS_Occurrence_Information.csv')
        air_lines = _get('CADORS_Aircraft_Information.csv')
    except Exception as e:
        print(f'  WARNING: CADORS download failed: {e}')
        return {}

    # occurrence number -> {date, type, loc, aero} (keep only recent)
    occ_info = {}
    try:
        reader   = csv.DictReader(occ_lines)
        num_col  = _col(reader.fieldnames, 'CADORSNUMBER', 'CADORS_NUMBER', 'OCCURRENCENUMBER')
        date_col = _col(reader.fieldnames, 'OCCURENCEDATE', 'OCCURRENCEDATE', 'OCCURRENCE_DATE', 'DATE')
        type_col = _col(reader.fieldnames, 'OCCURRENCETYPEDESCRIPTIONE', 'OCCURRENCETYPEDESCRIPTION')
        loc_col  = _col(reader.fieldnames, 'AERODROMELOCATION', 'OCCURRENCELOCATION')
        aero_col = _col(reader.fieldnames, 'AERODROMEID')
        if num_col and date_col:
            for row in reader:
                d = (row.get(date_col) or '').strip()[:10]
                if d >= cutoff:
                    num = (row.get(num_col) or '').strip()
                    occ_info[num] = {
                        'date': d,
                        'type': (row.get(type_col) or '').strip() if type_col else '',
                        'loc':  (row.get(loc_col)  or '').strip() if loc_col  else '',
                        'aero': (row.get(aero_col) or '').strip() if aero_col else '',
                    }
        print(f'  {len(occ_info):,} recent occurrences (since {cutoff})')
    except Exception as e:
        print(f'  WARNING: CADORS occurrence parse failed: {e}')
        return {}

    # C-reg -> {count, latest, latest_num, latest_type, latest_loc, latest_aero}
    result = {}
    try:
        reader   = csv.DictReader(air_lines)
        num_col  = _col(reader.fieldnames, 'CADORSNUMBER', 'CADORS_NUMBER', 'OCCURRENCENUMBER')
        mark_col = _col(reader.fieldnames, 'AIRCRAFTREGISTRATION', 'AIRCRAFT_REGISTRATION', 'MARK', 'REGISTRATION')
        if num_col and mark_col:
            for row in reader:
                num  = (row.get(num_col)  or '').strip()
                mark = (row.get(mark_col) or '').strip().upper()
                # CADORS stores Canadian regs without the 'C-' prefix (e.g. 'GMWT' for C-GMWT)
                if len(mark) == 4 and mark.isalpha():
                    mark = 'C-' + mark
                if not mark.startswith('C-') or num not in occ_info:
                    continue
                occ = occ_info[num]
                d   = occ['date']
                if mark not in result:
                    result[mark] = {'count': 0, 'latest': '', 'latest_num': '',
                                    'latest_type': '', 'latest_loc': '', 'latest_aero': ''}
                result[mark]['count'] += 1
                if d > result[mark]['latest']:
                    result[mark]['latest']      = d
                    result[mark]['latest_num']  = num
                    result[mark]['latest_type'] = occ['type']
                    result[mark]['latest_loc']  = occ['loc']
                    result[mark]['latest_aero'] = occ['aero']
        print(f'  {len(result):,} C-regs with recent incidents')
    except Exception as e:
        print(f'  WARNING: CADORS aircraft parse failed: {e}')

    return result


def normalize_serial(s):
    norm = re.sub(r'[\s\-]', '', (s or '')).upper().lstrip('0')
    return norm if len(norm) >= 3 else ''


def build_faa_lookup():
    print('[FAA] Downloading FAA aircraft registry...')
    FAA_DIR.mkdir(exist_ok=True)
    zip_path = FAA_DIR / 'faa.zip'
    try:
        resp = requests.get(FAA_URL, timeout=300, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.faa.gov/licenses_certificates/aircraft_certification/aircraft_registry/releasable_aircraft_download',
        })
        resp.raise_for_status()
        zip_path.write_bytes(resp.content)
        print(f'  {len(resp.content) / 1_048_576:.0f} MB downloaded')
    except Exception as e:
        print(f'  WARNING: could not download FAA registry ({e}) — skipping cross-reference')
        return {}
    try:
        with zipfile.ZipFile(zip_path) as z:
            with z.open('MASTER.txt') as f:
                raw = f.read()
        if raw.startswith(b'\xef\xbb\xbf'):
            raw = raw[3:]  # strip UTF-8 BOM
        content = raw.decode('latin-1')
    except Exception as e:
        print(f'  WARNING: could not read MASTER.txt ({e}) — skipping cross-reference')
        return {}
    reader = csv.reader(content.splitlines())
    headers = [h.strip() for h in next(reader, [])]
    lookup = {}
    for row in reader:
        if len(row) < 2:
            continue
        d = dict(zip(headers, row))
        serial = normalize_serial(d.get('SERIAL NUMBER', ''))
        n_num  = (d.get('N-NUMBER', '') or '').strip()
        if serial and n_num and serial not in lookup:
            lookup[serial] = 'N' + n_num
    print(f'  {len(lookup):,} US registrations indexed')
    return lookup


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}
        self._submit = None

    def handle_starttag(self, tag, attrs):
        if tag != 'input':
            return
        d = dict(attrs)
        name = d.get('name')
        if not name:
            return
        t = d.get('type', 'text').lower()
        if t == 'submit':
            if self._submit is None:
                self._submit = (name, d.get('value', ''))
        else:
            self.fields[name] = d.get('value', '')


def step1_download():
    print('[1/5] Downloading from Transport Canada...')
    DATA_DIR.mkdir(exist_ok=True)
    session = requests.Session()

    resp = session.get(PAGE_URL, timeout=60)
    resp.raise_for_status()

    parser = _FormParser()
    parser.feed(resp.text)
    fields = dict(parser.fields)
    if parser._submit:
        fields[parser._submit[0]] = parser._submit[1]

    zip_path = DATA_DIR / 'ccarcs_download.zip'
    resp2 = session.post(PAGE_URL, data=fields, timeout=180)
    resp2.raise_for_status()
    zip_path.write_bytes(resp2.content)

    size = zip_path.stat().st_size
    if size < 50_000:
        raise RuntimeError(f'Downloaded file is too small ({size} bytes) — may be an error page.')
    print(f'  OK — {size // 1024} KB downloaded')
    return zip_path


def step2_extract(zip_path):
    print('[2/5] Extracting ZIP...')
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(DATA_DIR)
    for f in ('carscurr.txt', 'carsownr.txt'):
        p = DATA_DIR / f
        print(f'  {f} {"extracted" if p.exists() else "NOT FOUND — check ZIP contents"}')


def step3_convert():
    print('[3/5] Converting encoding (Windows-1252 -> UTF-8)...')
    for f in ('carscurr.txt', 'carsownr.txt'):
        src = DATA_DIR / f
        dst = DATA_DIR / (f + '.utf8')
        dst.write_text(src.read_text(encoding='cp1252'), encoding='utf-8')
        print(f'  {f} converted')


def step4_database():
    print('[4/5] Rebuilding database...')
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''CREATE TABLE aircraft (
        Registration TEXT, REGISTRATION_SUB_TYPE_E TEXT, REGISTRATION_SUB_TYPE_F TEXT,
        Manufacturer TEXT, Model TEXT, Serial_No TEXT, MANUFACTURER_SERIAL_COMPRESSED TEXT,
        ID_PLATE_MANUFACTURERS_NAME TEXT, BASIS_FOR_REGISTRATION TEXT, BASIS_FOR_REGISTRATION_F TEXT,
        Type TEXT, AIRCRAFT_CATEGORY_F TEXT, DATE_OF_IMPORT TEXT, ENGINE_MANUF_E TEXT,
        POWERGLIDER_FLAG TEXT, Engine_Type TEXT, ENGINE_CATEGORY_F TEXT,
        No_Of_Engines INTEGER, NUMBER_OF_SEATS INTEGER, Weight REAL,
        SALE_REPORTED TEXT, Latest_Cert_Issued TEXT, Owner_Registered_Since TEXT,
        INEFFECTIVE_DATE TEXT, Reg_Purpose TEXT, REGISTERED_PURPOSE_F TEXT,
        FLIGHT_AUTHORITY_E TEXT, FLIGHT_AUTHORITY_F TEXT, MANUFACTURE_OR_ASSEMBLY TEXT,
        COUNTRY_MANUFACTURE_ASS_E TEXT, COUNTRY_MANUFACTURE_ASS_F TEXT, Year_of_Manu TEXT,
        BASE_OF_OPERATIONS_CTRY_E TEXT, BASE_OF_OPERATIONS_CTRY_F TEXT, Province TEXT,
        BASE_PROVINCE_OR_STATE_F TEXT, City TEXT, TYPE_CERTIFICATE_NUMBER TEXT,
        Reg_Status TEXT, REGISTRATION_AUTH_STATUS_F TEXT, MULTIPLE_OWNER_FLAG TEXT,
        MODIFIED_DATE TEXT, MODE_S_TRANSPONDER_BINARY TEXT, PHYSICAL_FILE_REGION_E TEXT,
        PHYSICAL_FILE_REGION_F TEXT, EX_MILITARY_MARK TEXT, TRIMMED_MARK TEXT
    )''')

    c.execute('''CREATE TABLE owners (
        MARK_LINK TEXT, Owner_Name TEXT, TRADE_NAME TEXT, Owner_Address TEXT,
        Owner_Address2 TEXT, Owner_City TEXT, Owner_Province TEXT, PROVINCE_OR_STATE_F TEXT,
        Postal_Code TEXT, Country TEXT, COUNTRY_F TEXT, Owner_Individual_Entity TEXT,
        TYPE_OF_OWNER_F TEXT, ACTIVE_FLAG TEXT, CARE_OF TEXT, REGION_E TEXT,
        REGION_F TEXT, OWNER_NAME_OLD_FORMAT TEXT, MAIL_RECIPIENT TEXT, TRIMMED_MARK TEXT
    )''')

    def import_csv(table, filepath, ncols):
        with open(filepath, encoding='utf-8', newline='') as f:
            reader = csv.reader(f)
            next(reader, None)  # skip header row
            ph = ','.join('?' * ncols)
            for row in reader:
                if not any(row):
                    continue
                row = row[:ncols] + [''] * max(0, ncols - len(row))
                c.execute(f'INSERT INTO {table} VALUES ({ph})', row[:ncols])

    import_csv('aircraft', DATA_DIR / 'carscurr.txt.utf8', 47)
    import_csv('owners',   DATA_DIR / 'carsownr.txt.utf8', 20)

    for ddl in (
        'CREATE INDEX idx_aircraft_registration ON aircraft(Registration)',
        'CREATE INDEX idx_aircraft_manufacturer  ON aircraft(Manufacturer)',
        'CREATE INDEX idx_aircraft_province      ON aircraft(Province)',
        'CREATE INDEX idx_owners_mark            ON owners(MARK_LINK)',
    ):
        c.execute(ddl)

    conn.commit()
    count = c.execute('SELECT COUNT(*) FROM aircraft').fetchone()[0]
    conn.close()
    print(f'  Database rebuilt — {count:,} aircraft rows')


def step5_generate():
    print('[5/5] Generating data.js...')

    # Preserve history: load previously generated data to find removed registrations
    old_by_reg = {}
    if DATA_JS.exists():
        try:
            content = DATA_JS.read_text(encoding='utf-8')
            marker = 'const AIRCRAFT_DATA = '
            idx = content.index(marker)
            json_str = content[idx + len(marker):].rstrip().rstrip(';')
            for r in json.loads(json_str):
                old_by_reg[r['Registration']] = r
        except Exception:
            pass  # first run or malformed file — no history to preserve

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute('''
        SELECT
            a.Registration,
            TRIM(o.Owner_Name)        AS Owner_Name,
            a.Year_of_Manu,
            TRIM(a.Manufacturer)      AS Manufacturer,
            TRIM(a.Model)             AS Model,
            TRIM(a.Serial_No)         AS Serial_No,
            a.Owner_Registered_Since,
            TRIM(a.Reg_Purpose)       AS Reg_Purpose,
            a.No_Of_Engines,
            TRIM(a.Engine_Type)       AS Engine_Type,
            TRIM(o.Owner_Address)     AS Address,
            TRIM(o.Owner_City)        AS City,
            TRIM(o.Owner_Province)    AS Province,
            TRIM(o.Postal_Code)       AS Postal_Code,
            TRIM(o.Country)           AS Country
        FROM aircraft a
        LEFT JOIN owners o ON a.Registration = o.MARK_LINK
        WHERE o.ACTIVE_FLAG = \'A\' OR o.MARK_LINK IS NULL
    ''').fetchall()
    conn.close()

    today = date.today().isoformat()
    data = [dict(r) for r in rows]
    new_regs = {r['Registration'] for r in data}

    # Rows no longer in the register → carry forward as deregistered
    removed = []
    for reg, old_row in old_by_reg.items():
        if reg not in new_regs:
            row = dict(old_row)
            row['_deleted'] = True
            if not row.get('_deleted_date'):
                row['_deleted_date'] = today
            removed.append(row)

    combined = data + removed

    faa_lookup = build_faa_lookup()
    if faa_lookup:
        for r in combined:
            serial = normalize_serial(r.get('Serial_No', ''))
            if serial and serial in faa_lookup:
                r['_faa_reg'] = faa_lookup[serial]
            else:
                r.pop('_faa_reg', None)
        matches = sum(1 for r in combined if r.get('_faa_reg'))
        print(f'  {matches:,} records cross-referenced with FAA registry')

    forsale = build_forsale_lookup(combined)
    for r in combined:
        # Registration stored as ' XXXX' (leading space); forsale_manual.json uses 'C-XXXX'
        reg_key = 'C-' + r.get('Registration', '').strip()
        if reg_key in forsale:
            new_sale = dict(forsale[reg_key])
            # Preserve first-seen date across runs; set it on first appearance
            old_sale = (old_by_reg.get(r.get('Registration', '')) or {}).get('_for_sale')
            prev_date = (old_sale or {}).get('date')
            new_sale['date'] = prev_date or today
            r['_for_sale'] = new_sale
        else:
            r.pop('_for_sale', None)
    if forsale:
        matched = sum(1 for r in combined if r.get('_for_sale'))
        print(f'  {matched} records matched to for-sale listings')

    adsb = build_adsb_lookup()
    for r in combined:
        last_seen = adsb.get(r.get('Registration', ''))
        if last_seen:
            r['_last_seen'] = last_seen
        else:
            r.pop('_last_seen', None)
    print(f'  {sum(1 for r in combined if r.get("_last_seen")):,} records with ADS-B last-seen data')

    cadors = build_cadors_lookup()
    for r in combined:
        reg_key = 'C-' + r.get('Registration', '').strip()
        if reg_key in cadors:
            r['_cadors'] = cadors[reg_key]
        else:
            r.pop('_cadors', None)
    print(f'  {sum(1 for r in combined if r.get("_cadors")):,} records with CADORS incidents')

    js = f'const DATA_DATE = "{today}";\nconst AIRCRAFT_DATA = {json.dumps(combined, ensure_ascii=False)};'
    DATA_JS.write_text(js, encoding='utf-8')
    size_mb = DATA_JS.stat().st_size / (1024 * 1024)
    print(f'  data.js — {size_mb:.1f} MB, dated {today}, {len(data):,} active + {len(removed):,} deregistered')


if __name__ == '__main__':
    print()
    print('=== CCARCS Data Updater ===')
    print()
    try:
        zip_path = step1_download()
        step2_extract(zip_path)
        step3_convert()
        step4_database()
        step5_generate()
        print()
        print('=== Done! Reload the browser page (F5) to see updated data ===')
        print()
    except Exception as e:
        print(f'\nERROR: {e}', file=sys.stderr)
        sys.exit(1)
