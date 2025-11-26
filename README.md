# PPV Rechnung Versenden

Automatisiertes System zum Versenden von ZUGFeRD-Rechnungen per E-Mail über Microsoft Graph API.

## 📋 Funktionen

- **ZUGFeRD PDF-Parsing**: Extrahiert Rechnungsdatum und Empfänger-E-Mail aus eingebetteten XML-Daten
- **Microsoft Graph API**: Versendet E-Mails mit PDF-Anhang über Microsoft 365
- **Tägliche Automatisierung**: Konfigurierbare Sendezeit mit APScheduler
- **Web-Oberfläche**: Einstellungen und Protokoll über Browser verwalten
- **Systemd-Integration**: Läuft als Hintergrunddienst mit automatischem Neustart

## 🏗️ Architektur

```
┌─────────────────────────────────────────────────────────────┐
│                    PPV Rechnung Versenden                    │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   FastAPI   │  │  Scheduler  │  │   Graph API Client  │  │
│  │   Web UI    │  │ (APScheduler)│  │      (MSAL)        │  │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘  │
│         │                │                     │             │
│  ┌──────┴────────────────┴─────────────────────┴──────────┐  │
│  │                    SQLite Database                      │  │
│  │              (Einstellungen + E-Mail-Log)               │  │
│  └─────────────────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   pikepdf   │  │    lxml     │  │      pytz           │  │
│  │ (PDF Parse) │  │ (XML Parse) │  │   (Timezone)        │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## 📁 Projektstruktur

```
ppv-rechnung-versenden/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI Application
│   ├── config.py            # Konfiguration (Pydantic)
│   ├── database.py          # SQLite/SQLAlchemy Setup
│   ├── models.py            # Datenbank-Modelle
│   ├── invoice_parser.py    # ZUGFeRD PDF-Parsing
│   ├── mail_service.py      # Microsoft Graph API
│   ├── scheduler.py         # APScheduler Jobs
│   ├── templates/           # Jinja2 HTML-Templates
│   │   ├── base.html
│   │   ├── settings.html
│   │   └── logs.html
│   └── static/
│       └── style.css
├── requirements.txt
├── .env.example
├── ppv-rechnung.service     # Systemd Unit File
├── install.sh               # Installations-Skript
├── update.sh                # Update-Skript
└── README.md
```

## 🚀 Installation

### Voraussetzungen

- Debian/Ubuntu LXC Container auf Proxmox
- Python 3.10+
- Internetzugang für Microsoft Graph API
- Azure AD App Registration (siehe unten)

### Schnellinstallation

```bash
# Repository klonen
git clone https://github.com/Sinthos/PPV-Rechnung-Versenden.git
cd PPV-Rechnung-Versenden

# Installer ausführen (als root)
sudo bash install.sh
```

### Manuelle Installation

```bash
# 1. System-Abhängigkeiten installieren
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git libxml2-dev libxslt1-dev

# 2. Verzeichnis erstellen
sudo mkdir -p /opt/ppv-rechnung
cd /opt/ppv-rechnung

# 3. Dateien kopieren/klonen
git clone https://github.com/Sinthos/PPV-Rechnung-Versenden.git .

# 4. Virtual Environment erstellen
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
deactivate

# 5. Konfiguration erstellen
cp .env.example .env
nano .env  # Credentials eintragen

# 6. Systemd Service installieren
sudo cp ppv-rechnung.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ppv-rechnung
sudo systemctl start ppv-rechnung
```

## ⚙️ Konfiguration

### Azure AD App Registration

1. Gehen Sie zum [Azure Portal](https://portal.azure.com)
2. Navigieren Sie zu **Azure Active Directory** → **App registrations**
3. Klicken Sie auf **New registration**
4. Geben Sie einen Namen ein (z.B. "PPV Rechnung Versenden")
5. Wählen Sie **Accounts in this organizational directory only**
6. Klicken Sie auf **Register**

Nach der Registrierung:

1. Notieren Sie die **Application (client) ID**
2. Notieren Sie die **Directory (tenant) ID**
3. Gehen Sie zu **Certificates & secrets** → **New client secret**
4. Erstellen Sie ein Secret und notieren Sie den Wert

API-Berechtigungen hinzufügen:

1. Gehen Sie zu **API permissions** → **Add a permission**
2. Wählen Sie **Microsoft Graph** → **Application permissions**
3. Suchen Sie nach **Mail.Send** und aktivieren Sie es
4. Klicken Sie auf **Grant admin consent**

### Umgebungsvariablen (.env)

```bash
# Microsoft Graph API
TENANT_ID=your-tenant-id-here
CLIENT_ID=your-client-id-here
CLIENT_SECRET=your-client-secret-here
SENDER_ADDRESS=rechnung@ppv-web.de

# Anwendung
APP_DATA_DIR=/opt/ppv-rechnung/data
DEFAULT_SOURCE_FOLDER=/Dokumente
DEFAULT_TARGET_FOLDER=/Dokumente/RE - Rechnung
DEFAULT_SEND_TIME=09:00

# Server
HOST=0.0.0.0
PORT=8000
LOG_LEVEL=INFO
```

## 🖥️ Web-Oberfläche

Nach der Installation ist die Web-Oberfläche erreichbar unter:

```
http://<SERVER-IP>:8000
```

### Einstellungen

- **Quellordner**: Pfad zu den RE-*.pdf Dateien
- **Zielordner**: Pfad für versendete Rechnungen
- **Sendezeit**: Tägliche automatische Verarbeitung (HH:MM)
- **E-Mail-Vorlage**: Text der E-Mail

### Protokoll

- Zeigt die letzten 100 versendeten E-Mails
- Status (Gesendet/Fehler)
- Fehlermeldungen bei Problemen

## 📧 Workflow

1. **Täglich zur konfigurierten Zeit** (oder manuell via "Jetzt ausführen"):
2. Scannt den Quellordner nach `RE-*.pdf` Dateien
3. Für jede PDF:
   - Extrahiert ZUGFeRD-XML aus der PDF
   - Liest Rechnungsdatum und Empfänger-E-Mail
   - **Nur wenn Rechnungsdatum = heute**: Sendet E-Mail
   - Verschiebt PDF in den Zielordner
4. Protokolliert alle Aktionen in der Datenbank

## 🔧 Service-Verwaltung

```bash
# Status prüfen
sudo systemctl status ppv-rechnung

# Service starten
sudo systemctl start ppv-rechnung

# Service stoppen
sudo systemctl stop ppv-rechnung

# Service neustarten
sudo systemctl restart ppv-rechnung

# Logs anzeigen
sudo journalctl -u ppv-rechnung -f

# Logs der letzten Stunde
sudo journalctl -u ppv-rechnung --since "1 hour ago"
```

## 🔄 Updates

```bash
cd /opt/ppv-rechnung
sudo bash update.sh
```

Oder manuell:

```bash
cd /opt/ppv-rechnung
sudo systemctl stop ppv-rechnung
git pull
source venv/bin/activate
pip install -r requirements.txt --upgrade
deactivate
sudo systemctl start ppv-rechnung
```

## 📂 Ordner-Berechtigungen

Stellen Sie sicher, dass der Service Zugriff auf die Quell- und Zielordner hat:

```bash
# Beispiel: Ordner erstellen und Berechtigungen setzen
sudo mkdir -p /Dokumente
sudo mkdir -p "/Dokumente/RE - Rechnung"
sudo chown -R root:root /Dokumente
sudo chmod -R 755 /Dokumente
```

Bei Netzlaufwerken (SMB/CIFS):

```bash
# In /etc/fstab eintragen
//server/share /Dokumente cifs credentials=/root/.smbcredentials,uid=0,gid=0 0 0

# Credentials-Datei erstellen
echo "username=user" > /root/.smbcredentials
echo "password=pass" >> /root/.smbcredentials
chmod 600 /root/.smbcredentials

# Mounten
sudo mount -a
```

## 🐛 Fehlerbehebung

### Service startet nicht

```bash
# Logs prüfen
sudo journalctl -u ppv-rechnung -n 50

# Häufige Ursachen:
# - .env Datei fehlt oder ungültig
# - Python-Abhängigkeiten nicht installiert
# - Port 8000 bereits belegt
```

### Graph API Fehler

```bash
# Token-Fehler: Credentials prüfen
# - TENANT_ID, CLIENT_ID, CLIENT_SECRET in .env
# - App Registration in Azure prüfen
# - Mail.Send Permission mit Admin Consent

# 403 Forbidden:
# - Mail.Send Permission fehlt
# - Admin Consent nicht erteilt
# - SENDER_ADDRESS hat keine Mailbox
```

### PDF-Parsing Fehler

```bash
# "No ZUGFeRD XML found":
# - PDF enthält kein eingebettetes XML
# - PDF ist kein ZUGFeRD/Factur-X Format

# XPath-Fehler:
# - XML-Struktur weicht ab
# - Namespaces in invoice_parser.py anpassen
```

### Datenbank zurücksetzen

```bash
# ACHTUNG: Löscht alle Einstellungen und Logs!
sudo systemctl stop ppv-rechnung
rm /opt/ppv-rechnung/data/ppv_rechnung.db
sudo systemctl start ppv-rechnung
```

## 📝 API-Endpunkte

| Endpunkt | Methode | Beschreibung |
|----------|---------|--------------|
| `/` | GET | Redirect zu /settings |
| `/settings` | GET | Einstellungen anzeigen |
| `/settings` | POST | Einstellungen speichern |
| `/logs` | GET | E-Mail-Protokoll anzeigen |
| `/run-now` | POST | Manuelle Verarbeitung starten |
| `/api/health` | GET | Health Check |
| `/api/settings` | GET | Einstellungen als JSON |
| `/api/logs` | GET | Logs als JSON |
| `/api/run` | POST | Verarbeitung via API starten |
| `/api/next-run` | GET | Nächste geplante Ausführung |
| `/api/connection-test` | GET | Graph API Verbindungstest |

## 📄 Lizenz

Proprietär - PPV Medien GmbH

## 👥 Support

Bei Fragen oder Problemen wenden Sie sich an die IT-Abteilung.
