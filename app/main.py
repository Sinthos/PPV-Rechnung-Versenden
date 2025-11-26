"""
FastAPI Application for PPV Rechnung Versenden.
Provides web UI for settings and logs, plus API endpoints.
"""

import logging
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from io import BytesIO

import os
from fastapi import FastAPI, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import init_db, get_db
from app.models import AppSettings, EmailLog
from app.filesystem import get_filesystem
from app.scheduler import (
    start_scheduler,
    stop_scheduler,
    reschedule_daily_job,
    run_now,
    get_next_run_time,
    TIMEZONE
)
from app.invoice_parser import parse_invoice, ZUGFeRDParseError
from app.mail_service import get_mail_service, GraphMailService, GraphMailError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup and shutdown."""
    # Startup
    logger.info("Starting PPV Rechnung Versenden application")
    
    # Initialize database
    init_db()
    
    # Initialize default settings
    from app.database import get_db_session
    with get_db_session() as db:
        AppSettings.initialize_defaults(db)
    
    # Start the scheduler
    start_scheduler()
    
    logger.info("Application startup complete")
    
    yield
    
    # Shutdown
    logger.info("Shutting down application")
    stop_scheduler()
    logger.info("Application shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="PPV Rechnung Versenden",
    description="Automated invoice email sending system",
    version="1.0.0",
    lifespan=lifespan
)

# Get the app directory for templates and static files
APP_DIR = Path(__file__).parent

# Mount static files
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

# Setup Jinja2 templates
templates = Jinja2Templates(directory=APP_DIR / "templates")


# Custom template filters
def format_datetime(value: Optional[datetime]) -> str:
    """Format datetime for display."""
    if value is None:
        return "-"
    return value.strftime("%d.%m.%Y %H:%M:%S")


templates.env.filters["format_datetime"] = format_datetime


# ============================================================================
# Web UI Routes
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Redirect to settings page."""
    return RedirectResponse(url="/settings", status_code=302)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    """Display settings page."""
    settings = AppSettings.get_all_settings(db)
    next_run = get_next_run_time()
    
    # Test Graph API connection using DB settings
    connection_status = None
    ms_settings = AppSettings.get_microsoft_settings(db)
    
    # Check if credentials are configured
    if ms_settings['tenant_id'] and ms_settings['client_id'] and ms_settings['client_secret']:
        try:
            mail_service = GraphMailService(
                tenant_id=ms_settings['tenant_id'],
                client_id=ms_settings['client_id'],
                client_secret=ms_settings['client_secret'],
                sender_address=ms_settings['sender_address']
            )
            connection_status = mail_service.test_connection()
        except GraphMailError as e:
            connection_status = {"status": "error", "message": str(e)}
        except Exception as e:
            connection_status = {"status": "error", "message": f"Configuration error: {e}"}
    else:
        connection_status = {"status": "not_configured", "message": "Microsoft Graph API Zugangsdaten nicht konfiguriert"}
    
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "settings": settings,
            "next_run": next_run,
            "connection_status": connection_status,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        }
    )


@app.post("/settings", response_class=HTMLResponse)
async def save_settings(
    request: Request,
    source_folder: str = Form(...),
    target_folder: str = Form(...),
    send_time: str = Form(...),
    email_template: str = Form(...),
    send_past_dates: bool = Form(False),
    # SMB Settings
    storage_type: str = Form("local"),
    smb_host: str = Form(""),
    smb_share: str = Form(""),
    smb_username: str = Form(""),
    smb_password: str = Form(""),
    smb_domain: str = Form(""),
    # Graph Settings
    tenant_id: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    sender_address: str = Form(""),
    db: Session = Depends(get_db)
):
    """Save settings from form."""
    errors = []
    
    # Validate folders (depends on storage type)
    if storage_type == "local":
        source_path = Path(source_folder)
        if not source_path.is_absolute():
            errors.append("Quellordner muss ein absoluter Pfad sein (bei lokaler Speicherung)")
        
        target_path = Path(target_folder)
        if not target_path.is_absolute():
            errors.append("Zielordner muss ein absoluter Pfad sein (bei lokaler Speicherung)")
    elif storage_type == "smb":
        if not smb_host or not smb_share:
            errors.append("Für SMB müssen Host und Freigabe (Share) angegeben werden")
        # For SMB, folders are relative to share, so we just check they are not empty
        if not source_folder.strip():
            errors.append("Quellordner darf nicht leer sein")
        if not target_folder.strip():
            errors.append("Zielordner darf nicht leer sein")
    
    # Validate send time format (HH:MM)
    if not re.match(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$', send_time):
        errors.append("Sendezeit muss im Format HH:MM sein (z.B. 09:00)")
    
    # Validate email template
    if not email_template.strip():
        errors.append("E-Mail-Vorlage darf nicht leer sein")
    
    # Validate sender email format if provided
    if sender_address and not re.match(r'^[^@]+@[^@]+\.[^@]+$', sender_address):
        errors.append("Absender E-Mail-Adresse ist ungültig")
    
    if errors:
        return RedirectResponse(
            url=f"/settings?error={'; '.join(errors)}",
            status_code=302
        )
    
    # Save folder and schedule settings
    AppSettings.set(db, AppSettings.KEY_SOURCE_FOLDER, source_folder.strip())
    AppSettings.set(db, AppSettings.KEY_TARGET_FOLDER, target_folder.strip())
    AppSettings.set(db, AppSettings.KEY_SEND_TIME, send_time.strip())
    AppSettings.set(db, AppSettings.KEY_EMAIL_TEMPLATE, email_template)
    AppSettings.set(db, AppSettings.KEY_SEND_PAST_DATES, "true" if send_past_dates else "false")
    
    # Save Storage settings
    AppSettings.set(db, AppSettings.KEY_STORAGE_TYPE, storage_type)
    AppSettings.set(db, AppSettings.KEY_SMB_HOST, smb_host.strip())
    AppSettings.set(db, AppSettings.KEY_SMB_SHARE, smb_share.strip())
    AppSettings.set(db, AppSettings.KEY_SMB_USERNAME, smb_username.strip())
    if smb_password.strip(): # Only update password if provided
        AppSettings.set(db, AppSettings.KEY_SMB_PASSWORD, smb_password.strip())
    AppSettings.set(db, AppSettings.KEY_SMB_DOMAIN, smb_domain.strip())
    
    # Save Microsoft Graph settings (only if provided)
    if tenant_id.strip():
        AppSettings.set(db, AppSettings.KEY_TENANT_ID, tenant_id.strip())
    if client_id.strip():
        AppSettings.set(db, AppSettings.KEY_CLIENT_ID, client_id.strip())
    if client_secret.strip():
        AppSettings.set(db, AppSettings.KEY_CLIENT_SECRET, client_secret.strip())
    if sender_address.strip():
        AppSettings.set(db, AppSettings.KEY_SENDER_ADDRESS, sender_address.strip())
    
    db.commit()
    
    # Reschedule the daily job with new time
    reschedule_daily_job(send_time.strip())
    
    # Update .env in project root so systemd / external tools can use the same credentials
    # (This does not replace DB settings; it only ensures the environment file is in sync if created from installer)
    try:
        project_root = Path(__file__).resolve().parents[1]
        env_path = project_root / ".env"
        # Load existing env key-values (keep comments and unknown lines intact)
        existing_lines = []
        if env_path.exists():
            existing_lines = env_path.read_text(encoding="utf-8").splitlines()
        env_map = {}
        for line in existing_lines:
            if '=' in line and not line.strip().startswith('#'):
                k, v = line.split('=', 1)
                env_map[k.strip()] = v.strip()
        # Update values only if provided (non-empty)
        if tenant_id.strip():
            env_map['TENANT_ID'] = tenant_id.strip()
        if client_id.strip():
            env_map['CLIENT_ID'] = client_id.strip()
        if client_secret.strip():
            env_map['CLIENT_SECRET'] = client_secret.strip()
        if sender_address.strip():
            env_map['SENDER_ADDRESS'] = sender_address.strip()
        # Ensure common keys exist (do not overwrite other keys)
        # Rebuild file: keep original comments/order where possible, otherwise append missing keys
        out_lines = []
        seen = set()
        for line in existing_lines:
            if '=' in line and not line.strip().startswith('#'):
                k, _ = line.split('=', 1)
                key = k.strip()
                if key in env_map:
                    out_lines.append(f"{key}={env_map[key]}")
                    seen.add(key)
                else:
                    out_lines.append(line)
            else:
                out_lines.append(line)
        # Append any keys not present originally
        for k, v in env_map.items():
            if k not in seen:
                out_lines.append(f"{k}={v}")
        # Write back safely
        env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        logger.info(f"Updated .env file at {env_path}")
    except Exception as e:
        logger.warning(f"Failed to update .env file: {e}")
    
    return RedirectResponse(
        url="/settings?message=Einstellungen gespeichert",
        status_code=302
    )


@app.post("/run-now", response_class=HTMLResponse)
async def trigger_run_now(request: Request):
    """Manually trigger invoice processing."""
    try:
        results = run_now()
        message = (
            f"Verarbeitung abgeschlossen: {results['sent']} gesendet, "
            f"{results['skipped']} übersprungen, {results['failed']} fehlgeschlagen"
        )
        return RedirectResponse(
            url=f"/settings?message={message}",
            status_code=302
        )
    except Exception as e:
        logger.error(f"Manual run failed: {e}")
        return RedirectResponse(
            url=f"/settings?error=Fehler bei der Verarbeitung: {e}",
            status_code=302
        )


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, db: Session = Depends(get_db)):
    """Display email logs page."""
    logs = EmailLog.get_recent(db, limit=100)
    
    return templates.TemplateResponse(
        "logs.html",
        {
            "request": request,
            "logs": logs,
        }
    )


# ============================================================================
# API Routes
# ============================================================================

@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


@app.get("/api/settings")
async def get_api_settings(db: Session = Depends(get_db)):
    """Get current settings via API."""
    return AppSettings.get_all_settings(db)


@app.get("/api/logs")
async def get_api_logs(limit: int = 100, db: Session = Depends(get_db)):
    """Get email logs via API."""
    logs = EmailLog.get_recent(db, limit=min(limit, 100))
    return [
        {
            "id": log.id,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None,
            "filename": log.filename,
            "invoice_date": log.invoice_date,
            "recipient_email": log.recipient_email,
            "subject": log.subject,
            "status": log.status,
            "error_message": log.error_message,
        }
        for log in logs
    ]


@app.post("/api/run")
async def api_run_now():
    """Trigger invoice processing via API."""
    try:
        results = run_now()
        return {"status": "success", "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/next-run")
async def api_next_run():
    """Get next scheduled run time via API."""
    next_run = get_next_run_time()
    return {
        "next_run": next_run.isoformat() if next_run else None,
        "timezone": "Europe/Berlin"
    }


@app.get("/api/invoice-preview")
async def api_invoice_preview(db: Session = Depends(get_db)):
    """
    Preview invoices in the configured source folder with their invoice dates.
    Used by the UI to show what will be sent.
    """
    settings = AppSettings.get_all_settings(db)
    fs = get_filesystem(settings)
    source_folder = settings.get(AppSettings.KEY_SOURCE_FOLDER, "")

    if not source_folder:
        raise HTTPException(status_code=400, detail="Quellordner ist nicht konfiguriert.")

    try:
        invoice_files = fs.list_files(source_folder, pattern="RE-*.pdf")
    except Exception as e:
        logger.error(f"Preview: failed to list files in {source_folder}: {e}")
        raise HTTPException(status_code=500, detail=f"Fehler beim Auflisten des Quellordners: {e}")

    today = datetime.now(TIMEZONE).date()
    items = []

    for file_path in sorted(invoice_files):
        filename = os.path.basename(file_path)
        item = {
            "filename": filename,
            "path": file_path,
            "status": "unknown",
            "invoice_date": "",
            "recipient": ""
        }

        try:
            pdf_bytes = fs.read_file(file_path)
            invoice_data = parse_invoice(BytesIO(pdf_bytes), filename=filename)

            item["invoice_date"] = invoice_data.invoice_date_str
            item["recipient"] = invoice_data.recipient_email or ""

            if invoice_data.invoice_date:
                days_until = (invoice_data.invoice_date - today).days
                item["days_until"] = days_until

                if days_until == 0:
                    item["status"] = "today"
                elif days_until > 0:
                    item["status"] = "future"
                else:
                    item["status"] = "past"
            else:
                item["status"] = "no_date"

        except ZUGFeRDParseError as e:
            item["status"] = "parse_error"
            item["error"] = str(e)
        except Exception as e:
            item["status"] = "error"
            item["error"] = str(e)

        items.append(item)

    return {
        "status": "success",
        "source_folder": source_folder,
        "count": len(items),
        "items": items,
    }


@app.get("/api/connection-test")
async def api_connection_test(db: Session = Depends(get_db)):
    """Test Microsoft Graph API connection."""
    try:
        ms_settings = AppSettings.get_microsoft_settings(db)
        mail_service = GraphMailService(
            tenant_id=ms_settings['tenant_id'],
            client_id=ms_settings['client_id'],
            client_secret=ms_settings['client_secret'],
            sender_address=ms_settings['sender_address']
        )
        result = mail_service.test_connection()
        return {"status": "success", "details": result}
    except GraphMailError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Configuration error: {e}")


@app.post("/api/smb/list-shares")
async def list_smb_shares(
    host: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    domain: str = Form("")
):
    """List available SMB shares on the host."""
    try:
        import smbclient
        
        # Register temporary session
        try:
            smbclient.register_session(
                host,
                username=username,
                password=password,
                domain=domain if domain else None
            )
        except Exception as e:
            # Maybe already registered, try to use it or fail
            # Re-registering with same creds usually works or raises "already exists" which is fine if creds match
            # But if different creds, we might have issues. 
            # smbclient global state is tricky. 
            # Usually calling register_session updates it or adds to the pool.
            pass

        shares = []
        # smbclient doesn't have a direct "list_shares" in high level API easily accessible in all versions?
        # Actually it does not expose NetShareEnum easily.
        # But we can try to list root? No, SMB root requires share.
        
        # Alternative: We can try to connect. 
        # But without knowing a share, we can't really "browse" the server root in strict SMB file terms 
        # unless we use IPC$ or similar which is complex.
        
        # Wait, if I cannot list shares easily with smbclient high level, 
        # I might need to rely on the user knowing the share name.
        # OR I can try to use smbprotocol directly.
        
        # Let's check if we can verify connection at least.
        # We can try to connect to IPC$ share which usually exists?
        
        # For now, let's implement a "Test Connection" that requires a Share to be entered.
        # Listing shares is hard without specific RPC calls (SRVSVC).
        
        # Let's change this endpoint to "test-connection" which verifies access to the SPECIFIED share.
        raise HTTPException(status_code=501, detail="Share listing not supported yet")
        
    except Exception as e:
        logger.error(f"SMB Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/smb/test")
async def test_smb_connection(
    host: str = Form(...),
    share: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    domain: str = Form("")
):
    """Test SMB connection to a specific share."""
    try:
        import smbclient

        host = host.strip()
        share = share.strip()
        username = username.strip()
        password = password.strip()
        domain = domain.strip()

        if not host:
            return {"status": "error", "message": "Bitte Host angeben."}
        if not share:
            return {"status": "error", "message": "Bitte Freigabe (Share) angeben."}
        if not username or not password:
            return {"status": "error", "message": "Bitte Benutzername und Passwort angeben."}

        # Clean up share name (remove leading slashes/backslashes)
        clean_share = share.replace("/", "").replace("\\", "")
        
        # Register session
        try:
            # Reset cached sessions to avoid reusing anonymous/old auth
            smbclient.reset_connection_cache()
        except Exception:
            pass

        # Build username with optional domain prefix (DOMAIN\user)
        reg_username = f"{domain}\\{username}" if domain else username

        try:
            smbclient.register_session(
                host,
                username=reg_username,
                password=password,
            )
        except Exception as e:
            return {"status": "error", "message": f"Anmeldung fehlgeschlagen: {e}"}
            
        # Try to list root of share
        path = f"\\\\{host}\\{clean_share}"
        try:
            smbclient.listdir(path)
            return {"status": "success", "message": f"Verbindung zu \\\\{host}\\{clean_share} erfolgreich!"}
        except Exception as e:
            return {"status": "error", "message": f"Zugriff verweigert oder Fehler: {str(e)}"}
            
    except ImportError:
         return {"status": "error", "message": "SMB Bibliothek nicht installiert"}
    except Exception as e:
        return {"status": "error", "message": f"Fehler: {str(e)}"}


# ============================================================================
# Folder Browser API
# ============================================================================

@app.get("/api/browse")
async def browse_folders(path: str = "/", db: Session = Depends(get_db)):
    """
    Browse folders using the configured filesystem (Local or SMB).
    Returns list of directories at the given path.
    """
    try:
        settings = AppSettings.get_all_settings(db)
        fs = get_filesystem(settings)
        
        # Security checks for local filesystem
        if settings.get("storage_type") == "local":
            if any(path.startswith(fp) for fp in ['/proc', '/sys', '/dev', '/run']):
                raise HTTPException(status_code=403, detail="Zugriff auf diesen Pfad nicht erlaubt")
        
        # List directories
        try:
            directories = fs.list_directories(path)
        except PermissionError as e:
            logger.error(f"FS List Permission Error: {e}")
            raise HTTPException(status_code=403, detail=str(e))
        except Exception as e:
            logger.error(f"FS List Error: {e}")
            raise HTTPException(status_code=500, detail=f"Fehler beim Auflisten: {str(e)}")
            
        # Determine parent path
        parent_path = None
        if path != "/" and path != "" and path != ".":
            # Simple string manipulation for parent
            parent_path = str(Path(path).parent)
            if parent_path == ".":
                parent_path = "/"
        
        return {
            "current_path": path,
            "parent_path": parent_path,
            "directories": directories
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error browsing path {path}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/create-folder")
async def create_folder(path: str = Form(...), db: Session = Depends(get_db)):
    """Create a new folder at the specified path using configured FS."""
    try:
        settings = AppSettings.get_all_settings(db)
        fs = get_filesystem(settings)
        
        # Security check for local
        if settings.get("storage_type") == "local":
            forbidden_paths = ['/proc', '/sys', '/dev', '/run', '/etc', '/bin', '/sbin', '/usr']
            if any(path.startswith(fp) for fp in forbidden_paths):
                raise HTTPException(status_code=403, detail="Ordner kann hier nicht erstellt werden")
        
        fs.create_directory(path)
        
        return {"status": "success", "path": path}
        
    except PermissionError:
        raise HTTPException(status_code=403, detail="Keine Berechtigung zum Erstellen des Ordners")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
