import os
import sqlite3
import requests
import qrcode
import json
import shutil
import re
import logging
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path
from tqdm import tqdm
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from pypdf import PdfReader, PdfWriter

# =========================
# 1. LOGGING SETUP
# =========================
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

file_handler = logging.FileHandler("rueckgabe_prozess.log", encoding="utf-8")
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.WARNING) # Nur Warnungen/Fehler im Terminal

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# =========================
# 2. KONFIGURATION LADEN
# =========================
def load_settings(config_file="config.json"):
    if not os.path.exists(config_file):
        raise FileNotFoundError(f"Konfigurationsdatei '{config_file}' wurde nicht gefunden!")
    with open(config_file, "r", encoding="utf-8") as f:
        return json.load(f)

CONF = load_settings()
DB_PATH = Path(CONF["db_path"])
CACHE_DB_PATH = Path("db/share_cache.sqlite")
OUTPUT_BASE = Path(CONF["output_base"])
PDF_DIR = OUTPUT_BASE / "pdf"
QR_DIR = OUTPUT_BASE / "qr"
PRINT_DIR = OUTPUT_BASE / "print"

NC_USER = CONF["nextcloud"]["user"]
NC_PASS = CONF["nextcloud"]["pass"]
NC_BASE_URL = CONF["nextcloud"]["base_url"]
NC_REMOTE_PREFIX = CONF["nextcloud"]["remote_path_prefix"]

CREATED_NC_DIRS = set()

# =========================
# 3. NETZWERK & CACHE-DB
# =========================
def get_nc_session():
    session = requests.Session()
    session.auth = (NC_USER, NC_PASS)
    session.headers.update({"OCS-APIRequest": "true"})
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session

NC_SESSION = get_nc_session()

def init_cache_db():
    CACHE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(CACHE_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS share_cache (
                sid TEXT PRIMARY KEY,
                share_url TEXT,
                data_hash TEXT,
                remote_path TEXT
            )
        """)

def get_cached_info(sid):
    with sqlite3.connect(CACHE_DB_PATH) as conn:
        res = conn.execute("SELECT share_url, data_hash FROM share_cache WHERE sid = ?", (sid,)).fetchone()
        return {"url": res[0], "hash": res[1]} if res else None

def save_cache_info(sid, url, data_hash, remote_path):
    with sqlite3.connect(CACHE_DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO share_cache VALUES (?, ?, ?, ?)", 
                     (sid, url, data_hash, str(remote_path)))

# =========================
# 4. HILFSFUNKTIONEN & HASHING
# =========================
def ensure_dirs():
    for d in (PDF_DIR, QR_DIR, PRINT_DIR):
        d.mkdir(parents=True, exist_ok=True)

def safe_filename(text):
    if not text: return "unbekannt"
    replacements = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", " ": "_", "/": "_"}
    text = str(text).lower()
    for k, v in replacements.items():
        text = text.replace(k, v)
    return re.sub(r"[^\w\-.]", "", text)

def generate_data_hash(data):
    """Erzeugt einen Hash aus Name, Klasse und sortierten Buch-IDs."""
    book_string = ",".join(sorted([str(b['inv']) for b in data['buecher']]))
    raw_info = f"{data['name']}|{data['klasse']}|{book_string}"
    return hashlib.sha256(raw_info.encode('utf-8')).hexdigest()

def password_for(geburt, sid):
    if geburt and "-" in str(geburt):
        parts = str(geburt).split("-")
        if len(parts) == 3: return f"{parts[2]}{parts[1]}{parts[0]}"
    return f"ID{sid}"

# =========================
# 5. DATENBANK LOGIK
# =========================
def load_data():
    if not DB_PATH.exists(): return {}
    schueler_dict = {}
    query = """
        SELECT l.Buchungsnummer, l.Lesernummer, l.Vorname, l.Nachname, l.Geburtsdatum,
               lug.KurzBez as klasse, t.Haupttitel, t.ISBN, e.Exemplarnummer
        FROM Leser l
        JOIN Verleih v ON v.Leser = l.Buchungsnummer
        JOIN Exemplar e ON v.Exemplar = e.Buchungsnummer
        JOIN Titel t ON e.Titel = t.Buchungsnummer
        LEFT JOIN SchuelerSchuljahr ssj ON ssj.LeserId = l.Buchungsnummer AND ssj.SchuljahrId = ?
        LEFT JOIN Leser_UG lug ON lug.Buchungsnummer = ssj.KlasseId
        WHERE v.Zurückgegeben = 0
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, (CONF["jahres_id"],)).fetchall()
        for row in rows:
            sid = str(row["Lesernummer"])
            if sid not in schueler_dict:
                schueler_dict[sid] = {
                    "name": f"{row['Vorname']} {row['Nachname']}",
                    "name_file": f"{row['Nachname']}_{row['Vorname']}",
                    "klasse": row["klasse"] if row["klasse"] else "undef",
                    "geburt": str(row["Geburtsdatum"]).split(" ")[0] if row["Geburtsdatum"] else None,
                    "buecher": []
                }
            schueler_dict[sid]["buecher"].append({"titel": row["Haupttitel"], "inv": row["Exemplarnummer"], "isbn": row["ISBN"] or "---"})
    return schueler_dict

# =========================
# 6. PDF & NEXTCLOUD LOGIK
# =========================
def create_pdf(sid, data):
    klasse_slug = safe_filename(data['klasse'])
    target_dir = PDF_DIR / klasse_slug
    target_dir.mkdir(parents=True, exist_ok=True)
    
    file_name = f"{safe_filename(data['name_file'])}_{sid}.pdf"
    raw_path = target_dir / f"raw_{file_name}"
    final_path = target_dir / file_name

    c = canvas.Canvas(str(raw_path), pagesize=A4)
    c.setFont("Helvetica-Bold", 14); c.drawString(50, 800, CONF["schul_name"])
    c.setFont("Helvetica", 12); c.drawString(50, 780, "Rückgabeübersicht Schulbücher")
    c.setFont("Helvetica", 11); c.drawString(50, 750, f"Name: {data['name']} (Klasse: {data['klasse']})")
    y = 670
    for b in data["buecher"]:
        c.setFont("Helvetica", 10); c.drawString(60, y, f"• {b['titel'][:70]}")
        y -= 14
        c.setFont("Helvetica-Oblique", 9); c.drawString(70, y, f"Inv: {b['inv']} | ISBN: {b['isbn']}")
        y -= 20
        if y < 80: c.showPage(); y = 750
    c.save()

    reader = PdfReader(raw_path); writer = PdfWriter()
    for page in reader.pages: writer.add_page(page)
    if data["klasse"] != "undef":
        writer.encrypt(user_password=password_for(data["geburt"], sid))
    with open(final_path, "wb") as f: writer.write(f)
    raw_path.unlink()
    return final_path

def upload_and_share_nextcloud(local_file, remote_file_path):
    # Verzeichnisse erstellen
    parts = remote_file_path.parent.parts
    path_accum = ""
    for part in parts:
        path_accum = f"{path_accum}/{part}" if path_accum else part
        if path_accum not in CREATED_NC_DIRS:
            url = f"{NC_BASE_URL}/remote.php/dav/files/{NC_USER}/{path_accum}"
            NC_SESSION.request("MKCOL", url)
            CREATED_NC_DIRS.add(path_accum)

    # Upload (Überschreiben ist bei WebDAV PUT Standard)
    upload_url = f"{NC_BASE_URL}/remote.php/dav/files/{NC_USER}/{remote_file_path}"
    with open(local_file, "rb") as f:
        r = NC_SESSION.put(upload_url, data=f)
    if r.status_code not in (200, 201, 204): raise RuntimeError(f"Upload Fehler {r.status_code}")

    # Link holen
    share_api = f"{NC_BASE_URL}/ocs/v2.php/apps/files_sharing/api/v1/shares"
    r_check = NC_SESSION.get(share_api, params={"path": str(remote_file_path)})
    try:
        url = ET.fromstring(r_check.text).find('.//url').text
        if url: return url
    except: pass

    data = {"path": f"/{remote_file_path}", "shareType": 3, "permissions": 1}
    r_share = NC_SESSION.post(share_api, data=data)
    return ET.fromstring(r_share.text).find('.//url').text

def create_print_page(sid, data, link):
    klasse_slug = safe_filename(data['klasse'])
    target_dir = PRINT_DIR / klasse_slug
    target_dir.mkdir(parents=True, exist_ok=True)
    output_path = target_dir / f"{safe_filename(data['name_file'])}_{sid}.pdf"
    
    qr = qrcode.make(link); qr_temp = QR_DIR / f"{sid}.png"; qr.save(qr_temp)
    c = canvas.Canvas(str(output_path), pagesize=A4)
    c.setFont("Helvetica-Bold", 18); c.drawString(50, 780, "Schulbuch-Rückgabe – Dein QR-Zugang")
    c.setFont("Helvetica", 12); c.drawString(50, 750, f"Für: {data['name']} (Klasse {data['klasse']})")
    c.drawImage(str(qr_temp), 50, 500, 180, 180)
    pw_hint = "Geburtsdatum (TTMMJJJJ)" if data["geburt"] else f"ID{sid}"
    c.drawString(50, 470, f"Passwort: {pw_hint}"); c.save(); qr_temp.unlink()

# =========================
# 7. MAIN
# =========================
def main():
    ensure_dirs(); init_cache_db()
    logger.warning(f"--- START: Prozess für {CONF['schul_name']} ---")
    
    daten = load_data()
    total = len(daten); errors = []
    
    with tqdm(total=total, desc="Fortschritt", unit="Schüler") as pbar:
        for sid, data in daten.items():
            try:
                current_hash = generate_data_hash(data)
                cached = get_cached_info(sid)
                
                # Pfad-Vorbereitung für Vergleich
                klasse_slug = safe_filename(data['klasse'])
                pdf_name = f"{safe_filename(data['name_file'])}_{sid}.pdf"
                local_pdf = PDF_DIR / klasse_slug / pdf_name
                remote_path = Path(NC_REMOTE_PREFIX) / klasse_slug / pdf_name

                # SKIP-LOGIK: Nur überspringen, wenn Hash passt UND Dateien lokal existieren
                if cached and cached['hash'] == current_hash and local_pdf.exists():
                    logger.info(f"Überspringe {data['name']} (keine Änderung).")
                    share_link = cached['url']
                else:
                    action = "Aktualisiere" if cached else "Erstelle"
                    logger.info(f"{action} Datensatz für {data['name']} (Hash geändert oder neu).")
                    
                    pdf_path = create_pdf(sid, data)
                    share_link = upload_and_share_nextcloud(pdf_path, remote_path)
                    save_cache_info(sid, share_link, current_hash, remote_path)

                create_print_page(sid, data, share_link)

            except Exception as e:
                err_msg = f"FEHLER bei {data.get('name', sid)}: {str(e)}"
                logger.error(err_msg); errors.append(err_msg)
            
            pbar.update(1)

    logger.warning(f"\n{'='*40}\nBERICHT: {total} gesamt, {len(errors)} Fehler.\n{'='*40}")
    if QR_DIR.exists(): shutil.rmtree(QR_DIR)

if __name__ == "__main__":
    main()
