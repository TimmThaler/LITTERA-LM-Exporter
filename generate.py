import os
import sqlite3
import requests
import qrcode
import json
import shutil
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from pypdf import PdfReader, PdfWriter

# =========================
# KONFIGURATION LADEN
# =========================

def load_settings(config_file="config.json"):
    if not os.path.exists(config_file):
        raise FileNotFoundError(f"Konfigurationsdatei '{config_file}' wurde nicht gefunden!")
    with open(config_file, "r", encoding="utf-8") as f:
        return json.load(f)

CONF = load_settings()

DB_PATH = Path(CONF["db_path"])
OUTPUT_BASE = Path(CONF["output_base"])
PDF_DIR = OUTPUT_BASE / "pdf"
QR_DIR = OUTPUT_BASE / "qr"
PRINT_DIR = OUTPUT_BASE / "print"

NC_USER = CONF["nextcloud"]["user"]
NC_PASS = CONF["nextcloud"]["pass"]
NC_BASE_URL = CONF["nextcloud"]["base_url"]
NC_REMOTE_PREFIX = CONF["nextcloud"]["remote_path_prefix"]

SCHULNAME = CONF["schul_name"]
SCHULJAHR = CONF["schuljahr"]
JAHRESID = CONF["jahres_id"]

# Cache für bereits erstellte Nextcloud-Ordner, um Requests zu sparen
CREATED_NC_DIRS = set()

# =========================
# HILFSFUNKTIONEN
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

def password_for(geburt, sid):
    if geburt and "-" in str(geburt):
        parts = str(geburt).split("-")
        if len(parts) == 3:
            return f"{parts[2]}{parts[1]}{parts[0]}"
    return f"ID{sid}"

# =========================
# OPTIMIERTE DATENBANK LOGIK
# =========================

def load_data():
    """Lädt hocheffizient nur Schüler mit Büchern inkl. Klassennamen."""
    if not DB_PATH.exists():
        print(f"CRITICAL: Datenbank unter {DB_PATH} nicht gefunden!")
        return {}

    schueler_dict = {}
    
    # Ein einziger großer JOIN um Leser, Klasse und Bücher zu finden
    # Wir filtern direkt im SQL auf Zurückgegeben = 0 und das Schuljahr
    query = """
        SELECT 
            l.Buchungsnummer as leser_id, l.Lesernummer, l.Vorname, l.Nachname, l.Geburtsdatum,
            lug.KurzBez as klasse,
            t.Haupttitel, t.ISBN, e.Exemplarnummer
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
        cur = conn.cursor()
        rows = cur.execute(query, (JAHRESID,)).fetchall()

        for row in rows:
            sid = str(row["Lesernummer"])
            
            # Wenn Schüler noch nicht im Dict, neu anlegen
            if sid not in schueler_dict:
                schueler_dict[sid] = {
                    "name": f"{row['Vorname']} {row['Nachname']}",
                    "name_file": f"{row['Nachname']}_{row['Vorname']}",
                    "klasse": row["klasse"] if row["klasse"] else "undef",
                    "geburt": str(row["Geburtsdatum"]).split(" ")[0] if row["Geburtsdatum"] else None,
                    "buecher": []
                }
            
            # Buch zum Schüler hinzufügen
            schueler_dict[sid]["buecher"].append({
                "titel": row["Haupttitel"],
                "inv": row["Exemplarnummer"],
                "isbn": row["ISBN"] or "---"
            })

    # Bücher sortieren
    for sid in schueler_dict:
        schueler_dict[sid]["buecher"].sort(key=lambda b: b["titel"].lower())

    return schueler_dict

# =========================
# OPTIMIERTE NEXTCLOUD LOGIK
# =========================

def upload_and_share_nextcloud(local_file, remote_file_path):
    """Lädt Datei hoch. Reduziert Ordner-Requests auf ein Minimum."""
    
    # 1. Verzeichnisse erstellen (nur wenn in dieser Sitzung noch nicht geschehen)
    parts = remote_file_path.parent.parts
    path_accum = ""
    for part in parts:
        path_accum = f"{path_accum}/{part}" if path_accum else part
        if path_accum not in CREATED_NC_DIRS:
            url = f"{NC_BASE_URL}/remote.php/dav/files/{NC_USER}/{path_accum}"
            # MKCOL wirft 405 wenn Ordner existiert -> einfach ignorieren
            requests.request("MKCOL", url, auth=(NC_USER, NC_PASS))
            CREATED_NC_DIRS.add(path_accum)

    # 2. Upload
    upload_url = f"{NC_BASE_URL}/remote.php/dav/files/{NC_USER}/{remote_file_path}"
    with open(local_file, "rb") as f:
        r = requests.put(upload_url, data=f, auth=(NC_USER, NC_PASS))
    
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"Upload fehlgeschlagen: {r.status_code}")

    # 3. Share-Link (Prüfen ob existiert, sonst neu)
    share_api = f"{NC_BASE_URL}/ocs/v2.php/apps/files_sharing/api/v1/shares"
    
    r_check = requests.get(share_api, headers={"OCS-APIRequest": "true"}, 
                           params={"path": str(remote_file_path)}, auth=(NC_USER, NC_PASS))
    try:
        existing_url = ET.fromstring(r_check.text).find('.//url').text
        if existing_url: return existing_url
    except:
        pass

    data = {"path": f"/{remote_file_path}", "shareType": 3, "permissions": 1}
    r_share = requests.post(share_api, headers={"OCS-APIRequest": "true"}, data=data, auth=(NC_USER, NC_PASS))
    return ET.fromstring(r_share.text).find('.//url').text

# =========================
# PDF & QR ERZEUGUNG (Bleibt stabil)
# =========================

def create_pdf(sid, data):
    klasse_slug = safe_filename(data['klasse'])
    target_dir = PDF_DIR / klasse_slug
    target_dir.mkdir(parents=True, exist_ok=True)
    
    file_name = f"{safe_filename(data['name_file'])}_{sid}.pdf"
    raw_path = target_dir / f"raw_{file_name}"
    final_path = target_dir / file_name

    c = canvas.Canvas(str(raw_path), pagesize=A4)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, 800, SCHULNAME)
    c.setFont("Helvetica", 12)
    c.drawString(50, 780, "Rückgabeübersicht Schulbücher")
    c.setFont("Helvetica", 11)
    c.drawString(50, 750, f"Name: {data['name']} (Klasse: {data['klasse']})")
    
    y = 670
    for b in data["buecher"]:
        c.setFont("Helvetica", 10)
        c.drawString(60, y, f"• {b['titel'][:70]}")
        y -= 14
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(70, y, f"Inv: {b['inv']} | ISBN: {b['isbn']}")
        y -= 20
        if y < 80: # Seitenschutz
            c.showPage()
            y = 750
    c.save()

    reader = PdfReader(raw_path)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    
    if data["klasse"] != "undef":
        writer.encrypt(user_password=password_for(data["geburt"], sid))

    with open(final_path, "wb") as f:
        writer.write(f)
    raw_path.unlink()
    return final_path

def create_print_page(sid, data, link):
    klasse_slug = safe_filename(data['klasse'])
    target_dir = PRINT_DIR / klasse_slug
    target_dir.mkdir(parents=True, exist_ok=True)
    
    output_path = target_dir / f"PRINT_{safe_filename(data['name_file'])}_{sid}.pdf"
    
    qr = qrcode.make(link)
    qr_temp = QR_DIR / f"{sid}.png"
    qr.save(qr_temp)

    c = canvas.Canvas(str(output_path), pagesize=A4)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, 780, "Schulbuch-Rückgabe – Dein QR-Zugang")
    c.setFont("Helvetica", 12)
    c.drawString(50, 750, f"Für: {data['name']} (Klasse {data['klasse']})")
    c.drawImage(str(qr_temp), 50, 500, 180, 180)
    
    c.setFont("Helvetica-Bold", 11)
    pw_hint = "Geburtsdatum (TTMMJJJJ)" if data["geburt"] else f"ID{sid}"
    c.drawString(50, 470, f"Passwort: {pw_hint}")
    c.save()
    qr_temp.unlink()

# =========================
# MAIN EXECUTION
# =========================

def main():
    ensure_dirs()
    print("Suche Schüler mit offenen Ausleihen...")
    daten = load_data()
    
    total = len(daten)
    print(f"{total} Schüler:innen mit offenen Ausleihen gefunden.")

    processed = 0
    for sid, data in daten.items():
        try:
            processed += 1
            print(f"[{processed}/{total}] {data['name']} (Klasse: {data['klasse']})")
            
            # 1. PDF erstellen
            local_pdf = create_pdf(sid, data)
            
            # 2. Upload Pfad bestimmen & Hochladen
            remote_path = Path(NC_REMOTE_PREFIX) / safe_filename(data['klasse']) / local_pdf.name
            share_link = upload_and_share_nextcloud(local_pdf, remote_path)
            
            # 3. QR & Druck-Seite
            create_print_page(sid, data, share_link)
            
        except Exception as e:
            print(f" !!! Fehler bei Schüler {sid} ({data['name']}): {e}")

    if QR_DIR.exists():
        shutil.rmtree(QR_DIR)
        
    print(f"\nFERTIG. {processed} Datensätze erfolgreich verarbeitet.")

if __name__ == "__main__":
    main()
