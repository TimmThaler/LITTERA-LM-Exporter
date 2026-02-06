import os
import sqlite3
import requests
import qrcode
import json
import shutil
import re
import logging
import hashlib
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
import customtkinter as ctk
from tkinter import filedialog
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import sys

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from pypdf import PdfReader, PdfWriter

# =========================
# GLOBALE HILFSFUNKTIONEN (Wichtig für EXE)
# =========================
def get_base_path():
    """ Ermittelt den Pfad, in dem die EXE (oder das Skript) liegt """
    if hasattr(sys, 'frozen'):
        # Pfad der .exe Datei
        return Path(sys.executable).parent
    # Pfad des .py Skripts
    return Path(__file__).parent

# =========================
# LOGGING HANDLER (GUI)
# =========================
class TextHandler(logging.Handler):
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        def append():
            self.text_widget.configure(state="normal")
            self.text_widget.insert("end", msg + "\n")
            self.text_widget.see("end")
            self.text_widget.configure(state="disabled")
        self.text_widget.after(0, append)

# =========================
# HAUPT-APP
# =========================
class RueckgabeApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # Basis-Pfad für Config festlegen
        self.base_path = get_base_path()
        self.CONFIG_FILE = self.base_path / "config.json"
        
        self.load_settings()
        self.stop_requested = False

        # Fenster Setup
        self.title("LMF Export-Manager")
        self.geometry("1100x850")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # Layout: Tabs
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(padx=20, pady=20, fill="both", expand=True)

        self.tab_process = self.tabview.add("Verarbeitung")
        self.tab_settings = self.tabview.add("Einstellungen")

        self.setup_process_tab()
        self.setup_settings_tab()
        self.setup_logging()

    # ----------------------------------------------------------------
    # UI: TAB EINSTELLUNGEN
    # ----------------------------------------------------------------
    def setup_settings_tab(self):
        self.tab_settings.grid_columnconfigure(1, weight=1)
        self.entries = {}
        fields = [
            ("Schulname", "schul_name", None),
            ("Schuljahr", "schuljahr", None),
            ("Jahres ID", "jahres_id", None),
            ("Datenbank Pfad", "db_path", "file"),
            ("Ausgabe Ordner", "output_base", "dir"),
            ("Nextcloud URL", "nextcloud_base_url", None),
            ("Nextcloud User", "nextcloud_user", None),
            ("Nextcloud Pass", "nextcloud_pass", None),
            ("Nextcloud Pfad", "nextcloud_path", None)
        ]

        for i, (label_text, key, browse_type) in enumerate(fields):
            lbl = ctk.CTkLabel(self.tab_settings, text=label_text + ":")
            lbl.grid(row=i, column=0, padx=20, pady=8, sticky="w")
            
            entry = ctk.CTkEntry(self.tab_settings)
            if "pass" in key: entry.configure(show="*")
            entry.grid(row=i, column=1, padx=(20, 5), pady=8, sticky="ew")
            entry.insert(0, str(self.get_config_value(key)))
            self.entries[key] = entry

            if browse_type == "file":
                btn = ctk.CTkButton(self.tab_settings, text="Datei wählen", width=100, 
                                    command=lambda k=key: self.browse_file(k))
                btn.grid(row=i, column=2, padx=(5, 20), pady=8)
            elif browse_type == "dir":
                btn = ctk.CTkButton(self.tab_settings, text="Ordner wählen", width=100, 
                                    command=lambda k=key: self.browse_directory(k))
                btn.grid(row=i, column=2, padx=(5, 20), pady=8)

        self.save_cfg_button = ctk.CTkButton(self.tab_settings, text="EINSTELLUNGEN SPEICHERN", 
                                             command=self.save_settings, fg_color="#2FA572", 
                                             hover_color="#106A43", font=("", 14, "bold"), height=40)
        self.save_cfg_button.grid(row=len(fields), column=0, columnspan=3, padx=20, pady=30, sticky="ew")

    def browse_file(self, key):
        path = filedialog.askopenfilename(filetypes=[("SQLite Datenbank", "*.sqlite"), ("Alle Dateien", "*.*")])
        if path:
            self.entries[key].delete(0, "end")
            self.entries[key].insert(0, path)

    def browse_directory(self, key):
        path = filedialog.askdirectory()
        if path:
            self.entries[key].delete(0, "end")
            self.entries[key].insert(0, path)

    # ----------------------------------------------------------------
    # UI: TAB VERARBEITUNG
    # ----------------------------------------------------------------
    def setup_process_tab(self):
        self.tab_process.grid_columnconfigure(0, weight=1)
        self.tab_process.grid_rowconfigure(1, weight=1)
        self.progress_frame = ctk.CTkFrame(self.tab_process)
        self.progress_frame.grid(row=0, column=0, padx=10, pady=10, sticky="ew")
        self.progress_frame.grid_columnconfigure(0, weight=1)
        self.progress_label = ctk.CTkLabel(self.progress_frame, text="Bereit zum Start.")
        self.progress_label.grid(row=0, column=0, pady=(10, 5))
        self.progress_bar = ctk.CTkProgressBar(self.progress_frame)
        self.progress_bar.grid(row=1, column=0, padx=20, pady=(0, 15), sticky="ew")
        self.progress_bar.set(0)
        self.log_textbox = ctk.CTkTextbox(self.tab_process, state="disabled", font=("Consolas", 12))
        self.log_textbox.grid(row=1, column=0, padx=10, pady=10, sticky="nsew")
        self.action_frame = ctk.CTkFrame(self.tab_process, fg_color="transparent")
        self.action_frame.grid(row=2, column=0, padx=10, pady=10, sticky="ew")
        self.action_frame.grid_columnconfigure((0, 1), weight=1)
        self.start_button = ctk.CTkButton(self.action_frame, text="PROZESS STARTEN", command=self.start_process_thread, height=50, font=("", 16, "bold"))
        self.start_button.grid(row=0, column=0, padx=(0, 5), sticky="ew")
        self.stop_button = ctk.CTkButton(self.action_frame, text="ABBRECHEN", command=self.request_stop, height=50, font=("", 16, "bold"), fg_color="#A83232", hover_color="#7A2424", state="disabled")
        self.stop_button.grid(row=0, column=1, padx=(5, 0), sticky="ew")

    # ----------------------------------------------------------------
    # LOGIK: CONFIG & SETTINGS
    # ----------------------------------------------------------------
    def load_settings(self):
        if not self.CONFIG_FILE.exists():
            self.config = {
                "schul_name": "Meine Schule", "schuljahr": "2024/25", "jahres_id": 2024,
                "db_path": str(self.base_path / "db/lmf.sqlite"), "output_base": str(self.base_path / "output"),
                "nextcloud": {"base_url": "", "user": "", "pass": "", "remote_path_prefix": "LMF"}
            }
        else:
            with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                self.config = json.load(f)

    def get_config_value(self, key):
        if key.startswith("nextcloud_"):
            nc_key = key.replace("nextcloud_", "")
            if nc_key == "path": nc_key = "remote_path_prefix"
            return self.config["nextcloud"].get(nc_key, "")
        return self.config.get(key, "")

    def save_settings(self):
        try:
            self.config["schul_name"] = self.entries["schul_name"].get()
            self.config["schuljahr"] = self.entries["schuljahr"].get()
            self.config["jahres_id"] = int(self.entries["jahres_id"].get())
            self.config["db_path"] = self.entries["db_path"].get()
            self.config["output_base"] = self.entries["output_base"].get()
            self.config["nextcloud"]["base_url"] = self.entries["nextcloud_base_url"].get()
            self.config["nextcloud"]["user"] = self.entries["nextcloud_user"].get()
            self.config["nextcloud"]["pass"] = self.entries["nextcloud_pass"].get()
            self.config["nextcloud"]["remote_path_prefix"] = self.entries["nextcloud_path"].get()
            with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4)
            logger.warning("Einstellungen erfolgreich gespeichert!")
        except Exception as e:
            logger.error(f"Fehler beim Speichern: {e}")

    def setup_logging(self):
        global logger
        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        if logger.hasHandlers(): logger.handlers.clear()
        gh = TextHandler(self.log_textbox)
        gh.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
        logger.addHandler(gh)

    def start_process_thread(self):
        self.stop_requested = False
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        threading.Thread(target=self.run_main_logic, daemon=True).start()

    def request_stop(self):
        self.stop_requested = True
        self.stop_button.configure(state="disabled")
        logger.warning("Abbruch eingeleitet...")

    # ----------------------------------------------------------------
    # HAUPT LOGIK
    # ----------------------------------------------------------------
    def run_main_logic(self):
        try:
            out_base = Path(self.config["output_base"])
            pdf_dir, qr_dir, print_dir = out_base/"pdf", out_base/"qr", out_base/"print"
            for d in (pdf_dir, qr_dir, print_dir): d.mkdir(parents=True, exist_ok=True)
            
            cache_db = out_base / "cache_db/share_cache.sqlite"
            cache_db.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(cache_db) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS share_cache (sid TEXT PRIMARY KEY, share_url TEXT, data_hash TEXT, remote_path TEXT)")

            session = requests.Session()
            session.auth = (self.config["nextcloud"]["user"], self.config["nextcloud"]["pass"])
            session.headers.update({"OCS-APIRequest": "true"})
            retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
            session.mount('https://', HTTPAdapter(max_retries=retries))

            daten = self.fetch_db_data()
            total = len(daten)
            if total == 0:
                logger.warning("Keine Daten gefunden."); self.reset_ui(); return

            logger.warning(f"Starte Prozess für {total} Schüler...")
            created_dirs = set()
            count, errors = 0, 0

            for sid, data in daten.items():
                if self.stop_requested: break
                try:
                    curr_hash = self.gen_hash(data)
                    with sqlite3.connect(cache_db) as conn:
                        res = conn.execute("SELECT share_url, data_hash FROM share_cache WHERE sid = ?", (sid,)).fetchone()
                    
                    kl_slug = self.slug(data['klasse'])
                    f_name = f"{self.slug(data['name_file'])}_{sid}.pdf"
                    loc_pdf = pdf_dir / kl_slug / f_name
                    rem_p = Path(self.config["nextcloud"]["remote_path_prefix"]) / kl_slug / f_name
                    rem_p_posix = rem_p.as_posix() # Garantiert "/"

                    if res and res[1] == curr_hash and loc_pdf.exists():
                        link = res[0]
                        logger.info(f"OK: {data['name']}")
                    else:
                        pdf_path = self.make_pdf(sid, data, pdf_dir)
                        link = self.upload_nc(session, pdf_path, rem_p, created_dirs)
                        with sqlite3.connect(cache_db) as conn:
                            conn.execute("INSERT OR REPLACE INTO share_cache VALUES (?, ?, ?, ?)", (sid, link, curr_hash, str(rem_p)))
                        logger.info(f"AKTUALISIERT: {data['name']}")

                    self.make_print(sid, data, link, print_dir, qr_dir)
                except Exception as e:
                    logger.error(f"Fehler bei {data['name']}: {e}")
                    errors += 1
                
                count += 1
                prog = count / total
                self.progress_bar.set(prog)
                self.progress_label.configure(text=f"{count} / {total} ({int(prog*100)}%)")

            logger.warning(f"PROZESS BEENDET. Erfolg: {count-errors}, Fehler: {errors}")
            if qr_dir.exists(): shutil.rmtree(qr_dir)
        except Exception as e:
            logger.error(f"Systemfehler: {e}")
        self.reset_ui()

    def reset_ui(self):
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

    def fetch_db_data(self):
        d = {}
        db_path = Path(self.config["db_path"])
        if not db_path.exists():
            logger.error(f"DB nicht gefunden: {db_path}")
            return d
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT l.Buchungsnummer, l.Lesernummer, l.Vorname, l.Nachname, l.Geburtsdatum,
                lug.KurzBez as klasse, t.Haupttitel, t.ISBN, e.Exemplarnummer
                FROM Leser l
                JOIN Verleih v ON v.Leser = l.Buchungsnummer
                JOIN Exemplar e ON v.Exemplar = e.Buchungsnummer
                JOIN Titel t ON e.Titel = t.Buchungsnummer
                LEFT JOIN SchuelerSchuljahr ssj ON ssj.LeserId = l.Buchungsnummer AND ssj.SchuljahrId = ?
                LEFT JOIN Leser_UG lug ON lug.Buchungsnummer = ssj.KlasseId
                WHERE v.Zurückgegeben = 0
            """, (self.config["jahres_id"],)).fetchall()
            for r in rows:
                sid = str(r["Lesernummer"])
                if sid not in d:
                    d[sid] = {"name": f"{r['Vorname']} {r['Nachname']}", "name_file": f"{r['Nachname']}_{r['Vorname']}",
                              "klasse": r["klasse"] or "undef", "geburt": str(r["Geburtsdatum"]).split(" ")[0] if r["Geburtsdatum"] else None,
                              "buecher": []}
                d[sid]["buecher"].append({"titel": r["Haupttitel"], "inv": r["Exemplarnummer"], "isbn": r["ISBN"] or "---"})
        return d

    def gen_hash(self, data):
        b_str = ",".join(sorted([str(b['inv']) for b in data['buecher']]))
        return hashlib.sha256(f"{data['name']}|{data['klasse']}|{b_str}".encode()).hexdigest()

    def slug(self, t):
        t = str(t).lower()
        for k, v in {"ä":"ae","ö":"oe","ü":"ue","ß":"ss"," ":"_","/":"_"}.items(): t = t.replace(k, v)
        return re.sub(r"[^\w\-.]", "", t)

    def make_pdf(self, sid, data, p_dir):
        kl_dir = p_dir / self.slug(data['klasse'])
        kl_dir.mkdir(parents=True, exist_ok=True)
        fn = f"{self.slug(data['name_file'])}_{sid}.pdf"
        rp, fp = kl_dir/f"raw_{fn}", kl_dir/fn
        c = canvas.Canvas(str(rp), pagesize=A4)
        c.setFont("Helvetica-Bold", 14); c.drawString(50, 800, self.config["schul_name"])
        c.setFont("Helvetica", 11); c.drawString(50, 750, f"Name: {data['name']} (Klasse: {data['klasse']})")
        y = 680
        for b in data["buecher"]:
            c.setFont("Helvetica", 10); c.drawString(60, y, f"• {b['titel'][:70]}")
            y -= 15; c.setFont("Helvetica-Oblique", 9); c.drawString(70, y, f"Inv: {b['inv']} | ISBN: {b['isbn']}")
            y -= 20
        c.save()
        writer = PdfWriter(); reader = PdfReader(rp)
        for p in reader.pages: writer.add_page(p)
        g = data["geburt"]
        pw = f"{g[8:10]}{g[5:7]}{g[0:4]}" if g and len(g) >= 10 else f"ID{sid}"
        writer.encrypt(pw)
        with open(fp, "wb") as f: writer.write(f)
        rp.unlink(); return fp

    def upload_nc(self, sess, loc, rem, c_dirs):
        parts = rem.parent.parts
        p_acc = ""
        for p in parts:
            p_acc = f"{p_acc}/{p}" if p_acc else p
            if p_acc not in c_dirs:
                url = f"{self.config['nextcloud']['base_url']}/remote.php/dav/files/{self.config['nextcloud']['user']}/{p_acc}"
                sess.request("MKCOL", url)
                c_dirs.add(p_acc)
        url = f"{self.config['nextcloud']['base_url']}/remote.php/dav/files/{self.config['nextcloud']['user']}/{rem}"
        with open(loc, "rb") as f: sess.put(url, data=f)
        
        api = f"{self.config['nextcloud']['base_url']}/ocs/v2.php/apps/files_sharing/api/v1/shares"
        r = sess.post(api, data={"path": f"/{rem}", "shareType": 3, "permissions": 1})
        
        # --- FEHLERBEHEBUNG FÜR NONE-TYPE OBJEKT ---
        try:
            tree = ET.fromstring(r.text)
            url_element = tree.find('.//url')
            if url_element is not None:
                return url_element.text
            else:
                # Falls kein URL Tag gefunden wurde (z.B. Fehler von Nextcloud)
                status_msg = tree.find('.//message')
                msg = status_msg.text if status_msg is not None else "Unbekannter API Fehler"
                raise RuntimeError(f"Nextcloud Share-Link konnte nicht erstellt werden: {msg}")
        except ET.ParseError:
            raise RuntimeError(f"Ungültige Antwort von Nextcloud (Kein XML). Status: {r.status_code}")

    def make_print(self, sid, data, link, pr_dir, qr_d):
        kl_dir = pr_dir / self.slug(data['klasse'])
        kl_dir.mkdir(parents=True, exist_ok=True)
        out = kl_dir / f"{self.slug(data['name_file'])}_{sid}.pdf"
        q_p = qr_d / f"{sid}.png"; qrcode.make(link).save(q_p)
        c = canvas.Canvas(str(out), pagesize=A4)
        c.setFont("Helvetica-Bold", 18); c.drawString(50, 780, "Schulbuch-Rückgabe")
        c.drawImage(str(q_p), 50, 500, 180, 180)
        c.setFont("Helvetica", 12); c.drawString(50, 480, f"Für: {data['name']}")
        g = data["geburt"]
        pw = f"Dein Geburtsdatum im Format TTMMJJJJ" if g and len(g) >= 10 else f"ID{sid}"
        c.setFont("Helvetica-Bold", 12); c.drawString(50, 460, f"Passwort: {pw}")
        c.save(); q_p.unlink()

if __name__ == "__main__":
    app = RueckgabeApp()
    app.mainloop()
