import os
import sqlite3
import requests
import qrcode
from collections import defaultdict
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from pypdf import PdfReader, PdfWriter
import shutil
from pathlib import Path

import xml.etree.ElementTree as ET

# =========================
# KONFIGURATION
# =========================

DB_PATH = "db/lmf_dump.sqlite"
OUTPUT_BASE = "output"
PDF_DIR = os.path.join(OUTPUT_BASE, "pdf")
QR_DIR = os.path.join(OUTPUT_BASE, "qr")
PRINT_DIR = os.path.join(OUTPUT_BASE, "print")

NEXTCLOUD_BASE_URL = "https://meine-nextcloud.tld"
NEXTCLOUD_WEBDAV_URL = "https://meine-nextcloud.tld/remote.php/dav/files/user/"
NEXTCLOUD_USER = "user" # Nextcloud-User
NEXTCLOUD_PASS = "pass" # App-Passwort

SCHULNAME = "Schule"
SCHULJAHR = "2025/26"

# =========================
# HILFSFUNKTIONEN
# =========================

def ensure_dirs():
    for d in (PDF_DIR, QR_DIR, PRINT_DIR):
        os.makedirs(d, exist_ok=True)

def password_for(geburt, sid):
    """
    Wandelt "YYYY-MM-DD" in "TTMMJJJJ" um.
    Fallback: ID + Schülernummer.
    """
    if geburt:
        # Extrahiere YYYY-MM-DD
        #date_part = str(geburt).split(" ")[0]
        #parts = date_part.split("-")
        parts = str(geburt).split("-")
        if len(parts) == 3:
            # Tag + Monat + Jahr
            return f"{parts[2]}{parts[1]}{parts[0]}"
    return f"ID{sid}"

def safe_filename(text):
    if not text: return "unbekannt"
    return (
        text.replace(" ", "_")
            .replace("/", "_")
            .replace("ä", "ae")
            .replace("ö", "oe")
            .replace("ü", "ue")
            .replace("ß", "ss")
            .lower()
    )

def safe_filename_name(text):
    if not text: return "unbekannt"
    return (
        text.replace(" ", "_")
            .replace("/", "_")
            .replace("__", "_")
            .replace("ä", "ae")
            .replace("ö", "oe")
            .replace("ü", "ue")
            .replace("ß", "ss")
            #.lower()
    )


def get_aktuelle_klasse(cur, buchungsnummer, schuljahr=2025):
    """
    Liefert den Klassennamen (KurzBez) für einen Schüler (Buchungsnummer)
    im angegebenen Schuljahr zurück.
    
    Args:
        cur: sqlite3.Cursor
        buchungsnummer: int oder str, LeserId
        schuljahr: int, Standard 2025
        
    Returns:
        str: Klassennamen (KurzBez) oder None, wenn kein Treffer
    """
    query = """
        SELECT lug.KurzBez
        FROM SchuelerSchuljahr ssj
        JOIN Leser_UG lug
          ON lug.Buchungsnummer = ssj.KlasseId
        WHERE ssj.LeserId = ?
          AND ssj.SchuljahrId = ?
    """
    row = cur.execute(query, (buchungsnummer, schuljahr)).fetchone()
    
    if row:
        return row["KurzBez"]
    else:
        return "undef"

# NEXTCLOUD UPLOAD AND SHARE
import xml.etree.ElementTree as ET
import requests

def upload_and_share_nextcloud(local_file, remote_file, nc_url, username, password, safe_name=True):
    """
    Lädt eine Datei in Nextcloud hoch und erstellt einen öffentlichen Share-Link,
    auch wenn die Datei bereits existiert. Wenn die Datei bereits freigegeben wurde,
    wird der alte Freigabelink verwendet.
    """

    local_file = Path(local_file)
    if not local_file.exists():
        raise FileNotFoundError(local_file)

    # ---- Dateiname vorbereiten ----
    filename = local_file.name
    if safe_name:
        import re
        filename = re.sub(r"[^\w\-.]", "_", filename)

    remote_path = remote_file
    remote_folder = Path(remote_path).parent

    # ---- 1) Ordner in Nextcloud anlegen (WebDAV MKCOL) ----
    parts = Path(remote_folder).parts
    path_accum = ""
    for part in parts:
        path_accum = f"{path_accum}/{part}" if path_accum else part
        url = f"{nc_url}/remote.php/dav/files/{username}/{path_accum}"
        r = requests.request("MKCOL", url, auth=(username, password))
        if r.status_code not in (201, 405):
            raise RuntimeError(f"Ordner '{path_accum}' konnte nicht erstellt werden ({r.status_code}): {r.text}")

    # ---- 2) Datei hochladen (auch wenn sie bereits existiert) ----
    upload_url = f"{nc_url}/remote.php/dav/files/{username}/{remote_path}"
    with open(local_file, "rb") as f:
        r = requests.put(upload_url, data=f, auth=(username, password))

    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"Upload fehlgeschlagen ({r.status_code}): {r.text}")

    # ---- 3) Überprüfen, ob die Datei bereits freigegeben wurde ----
    existing_share_link = get_existing_share_link(nc_url, username, password, remote_path)

    if existing_share_link:
        # Datei ist bereits freigegeben, gib den alten Link zurück
        print(f"Datei {remote_path} ist bereits freigegeben. Verwende den bestehenden Freigabelink.")
        return existing_share_link
    else:
        # Datei wurde noch nicht freigegeben, neuen Share-Link erstellen
        return create_new_share_link(nc_url, username, password, remote_path)


def get_existing_share_link(nc_url, username, password, remote_path):
    """
    Prüft, ob bereits ein Share-Link für die Datei existiert und gibt diesen zurück.
    """
    share_api = f"{nc_url}/ocs/v2.php/apps/files_sharing/api/v1/shares"
    headers = {"OCS-APIRequest": "true"}
    params = {"path": remote_path}

    r = requests.get(share_api, headers=headers, params=params, auth=(username, password))

    # XML-Antwort parsen
    try:
        result = ET.fromstring(r.text)
        # Überprüfen, ob ein Link existiert
        share_url = result.find('.//url').text
        return share_url
    except Exception as e:
        #print(f"Fehler beim Abrufen des bestehenden Freigabelinks: {e}")
        return None


def create_new_share_link(nc_url, username, password, remote_path):
    """
    Erstellt einen neuen Freigabelink für die Datei, falls sie noch nicht freigegeben wurde.
    """
    share_api = f"{nc_url}/ocs/v2.php/apps/files_sharing/api/v1/shares"
    headers = {"OCS-APIRequest": "true"}
    data = {"path": f"/{remote_path}", "shareType": 3, "permissions": 1}

    r = requests.post(share_api, headers=headers, data=data, auth=(username, password))

    # XML-Antwort parsen
    try:
        result = ET.fromstring(r.text)
        # Extrahiere den neuen Link
        url = result.find('.//url').text
        return url
    except Exception as e:
        raise RuntimeError(f"Share fehlgeschlagen: erhaltene HTML-Antwort oder Parsing-Fehler:\n{r.text[:500]}\nFehler: {e}")




# =========================
# 1️⃣ DATEN AUS SQLITE
# =========================

def load_data():
    if not os.path.exists(DB_PATH):
        print(f"Fehler: Datenbank {DB_PATH} nicht gefunden!")
        return {}

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row 
    cur = conn.cursor()
    
    schueler_dict = {}

    # Alle Leser abrufen
    leser_rows = cur.execute("SELECT Buchungsnummer, Lesernummer, Lesergruppe, Vorname, Nachname, Geburtsdatum FROM Leser").fetchall()

    for l_row in leser_rows:
        sid = str(l_row["Lesernummer"])
        b_nummer = l_row["Buchungsnummer"]
        
        schueler_dict[sid] = {
            "name": f"{l_row['Vorname']} {l_row['Nachname']}",
            "name_file": f"{l_row['Nachname']}_{l_row['Vorname']}",
            "klasse": get_aktuelle_klasse(cur, l_row["Buchungsnummer"]),
            "geburt": l_row["Geburtsdatum"].split(" ")[0] if l_row["Geburtsdatum"] else None,
            "buecher": []
        }

        #print(schueler_dict[sid]["klasse"])

        # Verleih abrufen
        verleih_rows = cur.execute(
            "SELECT Exemplar FROM Verleih WHERE Leser=? AND Zurückgegeben=0", 
            (b_nummer,)
        ).fetchall()

        for v_row in verleih_rows:
            ex_row = cur.execute(
                "SELECT Exemplarnummer, Titel FROM Exemplar WHERE Buchungsnummer=?", 
                (v_row["Exemplar"],)
            ).fetchone()
            
            if ex_row:
                t_row = cur.execute(
                    "SELECT Haupttitel, ISBN FROM Titel WHERE Buchungsnummer=?", 
                    (ex_row["Titel"],)
                ).fetchone()
                
                if t_row:
                    schueler_dict[sid]["buecher"].append({
                        "titel": t_row["Haupttitel"],
                        "inv": ex_row["Exemplarnummer"],
                        "isbn": t_row["ISBN"] or "---"
                    })

        schueler_dict[sid]["buecher"].sort(key=lambda b: b["titel"].lower())

    conn.close()
    return schueler_dict

# =========================
# 2️⃣ PDF ERZEUGEN
# =========================

def create_pdf(sid, data):
    # 1. Den Klassennamen für den Pfad "sicher" machen
    klasse_folder = safe_filename(data['klasse'])
    
    # 2. Den vollen Pfad zum Klassen-Unterordner erstellen
    target_dir = os.path.join(PDF_DIR, klasse_folder)
    
    # 3. Den Ordner erstellen, falls er noch nicht existiert
    os.makedirs(target_dir, exist_ok=True)
    
    # 4. Den endgültigen Dateipfad zusammenbauen
    raw_path = os.path.join(target_dir, safe_filename_name(f"{data['name_file']}_{sid}_raw.pdf"))
    final_path = os.path.join(target_dir, safe_filename_name(f"{data['name_file']}_{sid}.pdf"))

    c = canvas.Canvas(raw_path, pagesize=A4)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, 800, f"{SCHULNAME}")
    c.setFont("Helvetica", 12)
    c.drawString(50, 780, "Rückgabeübersicht Schulbücher")

    c.setFont("Helvetica", 11)
    c.drawString(50, 750, f"Name: {data['name']}")
    c.drawString(50, 730, f"Klasse: {data['klasse']}")
    c.drawString(50, 710, f"Schuljahr: {SCHULJAHR}")

    y = 670
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, y, "Diese Bücher sind zum Schuljahresende zurückzugeben:")
    y -= 25

    if not data["buecher"]:
        c.setFont("Helvetica-Oblique", 11)
        c.drawString(60, y, "Keine offenen Ausleihen gefunden.")
    else:
        for b in data["buecher"]:
            c.setFont("Helvetica", 10)
            titel_kurz = (b['titel'][:75] + '..') if len(b['titel']) > 75 else b['titel']
            c.drawString(60, y, f"• {titel_kurz}")
            
            y -= 14
            c.setFont("Helvetica-Oblique", 9)
            c.drawString(70, y, f"ISBN: {b['isbn']} | Inv-Nr: {b['inv']}")
            
            y -= 20 
            if y < 100:
                c.showPage()
                y = 750

    c.setFont("Helvetica", 8)
    c.drawString(50, 50, "Passwort: Geburtsdatum (TTMMJJJJ) oder ID + Schülernummer.")
    c.save()

    # Verschlüsselung
    reader = PdfReader(raw_path)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    #pw = password_for(data["geburt"], sid)
    #writer.encrypt(user_password=pw)
    if data.get("klasse") not in (None, "undef"):
        pw = password_for(data["geburt"], sid)
        writer.encrypt(user_password=pw)

    with open(final_path, "wb") as f:
        writer.write(f)
    os.remove(raw_path)
    
    return final_path

# =========================
# 3️⃣ WEITERE FUNKTIONEN (QR & Print)
# =========================

def create_qr(link, sid):
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(link)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(os.path.join(QR_DIR, f"{sid}.png"))

def create_print_page(sid, data, link):
    # 1. Den Klassennamen für den Pfad "sicher" machen
    klasse_folder = safe_filename(data['klasse'])
    
    # 2. Den vollen Pfad zum Klassen-Unterordner erstellen
    target_dir = os.path.join(PRINT_DIR, klasse_folder)
    
    # 3. Den Ordner erstellen, falls er noch nicht existiert
    os.makedirs(target_dir, exist_ok=True)
    
    # 4. Den endgültigen Dateipfad zusammenbauen
    output_path = os.path.join(target_dir, safe_filename_name(f"{data['name_file']}_{sid}.pdf"))
    
    # Ab hier wie gehabt:
    c = canvas.Canvas(output_path, pagesize=A4)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, 780, "Schulbuch-Rückgabe – Dein QR-Zugang")
    
    c.setFont("Helvetica", 12)
    c.drawString(50, 750, f"Für: {data['name']} (Klasse {data['klasse']})")
    
    qr_path = os.path.join(QR_DIR, f"{sid}.png")
    if os.path.exists(qr_path):
        c.drawImage(qr_path, 50, 500, 200, 200)

    c.setFont("Helvetica", 11)
    c.drawString(50, 500, f"Link: {link}")
    c.drawString(50, 470, "1. QR-Code scannen")
    c.drawString(50, 450, "2. Passwort eingeben:")
    
    c.setFont("Helvetica-Bold", 11)
    pw_hint = "Geburtsdatum (TTMMJJJJ)" if data["geburt"] else f"ID{sid}"
    c.drawString(65, 430, f"--> {pw_hint}")
    
    #c.setFont("Helvetica", 9)
    #c.drawString(50, 380, "Sollte das Geburtsdatum nicht funktionieren, nutze: ID + deine Schülernummer.")
    
    c.save()
    
    
# =========================
# 🚀 MAIN
# =========================

def main():
    ensure_dirs()
    daten = load_data()
    print(f"{len(daten)} Leser:innen geladen.")

    #max_test = 30
    #count = 0

    for sid, data in daten.items():
        # Wenn es keine Verleihdaten gibt, dann gehen wir direkt zum nächsten Leser.
        if not data.get("buecher"):
            continue  # nur diesen Durchlauf überspringen
        
        print(f"Verarbeite: {data['name']} (Klasse: {data['klasse']}, {data['geburt']})...")
        local_pdf_file = create_pdf(sid, data)
        #print(local_file)
        # Link-Generierung
        link = upload_and_share_nextcloud(
            local_pdf_file,
            f"Buecherrueckgabe_aktuell/{safe_filename(data['klasse'])}/{Path(local_pdf_file).name}",
            NEXTCLOUD_BASE_URL,
            NEXTCLOUD_USER,
            NEXTCLOUD_PASS
        )

        create_qr(link, sid)
        create_print_page(sid, data, link)
        
        #count += 1
        #if count >= max_test:
        #    break

    shutil.rmtree(QR_DIR)
    print("\nFERTIG. Alle Dateien erstellt.")
    print(f"{len(daten)} Leser:innen verarbeitet.")

if __name__ == "__main__":
    main()
