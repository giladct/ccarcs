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

PROJECT_DIR = Path(__file__).parent
DATA_DIR    = PROJECT_DIR / 'CCARCS_data'
DB_PATH     = PROJECT_DIR / 'ccarcs.db'
DATA_JS     = PROJECT_DIR / 'data.js'
PAGE_URL    = 'https://wwwapps.tc.gc.ca/Saf-Sec-Sur/2/CCARCS-RIACC/DDZip.aspx'


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

    data = [dict(r) for r in rows]
    today = date.today().isoformat()
    js = f'const DATA_DATE = "{today}";\nconst AIRCRAFT_DATA = {json.dumps(data, ensure_ascii=False)};'
    DATA_JS.write_text(js, encoding='utf-8')
    size_mb = DATA_JS.stat().st_size / (1024 * 1024)
    print(f'  data.js — {size_mb:.1f} MB, dated {today}')


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
