"""
FastAPI Application for PPV Rechnung Versenden.
Provides web UI for settings and logs, plus API endpoints.
"""

import logging
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import os
from fastapi import FastAPI, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import init_db, get_db
from app.models import AppSettings, EmailLog
from app.scheduler import (
    start_scheduler,
    stop_scheduler,
    reschedule_daily_job,
    run_now,
    get_next_run_time
)
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
    tenant_id: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    sender_address: str = Form(""),
    db: Session = Depends(get_db)
):
    """Save settings from form."""
    errors = []
    
    # Validate source folder
    source_path = Path(source_folder)
    if not source_path.is_absolute():
        errors.append("Quellordner muss ein absoluter Pfad sein")
    
    # Validate target folder
    target_path = Path(target_folder)
    if not target_path.is_absolute():
        errors.append("Zielordner muss ein absoluter Pfad sein")
    
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


# ============================================================================
# Folder Browser API
# ============================================================================

@app.get("/api/browse")
async def browse_folders(path: str = "/"):
    """
    Browse folders on the server filesystem.
    Returns list of directories at the given path.
    """
    try:
        # Normalize and validate path
        browse_path = Path(path).resolve()
        
        # Security: Don't allow browsing certain system directories
        forbidden_paths = ['/proc', '/sys', '/dev', '/run']
        if any(str(browse_path).startswith(fp) for fp in forbidden_paths):
            raise HTTPException(status_code=403, detail="Zugriff auf diesen Pfad nicht erlaubt")
        
        if not browse_path.exists():
            raise HTTPException(status_code=404, detail="Pfad existiert nicht")
        
        if not browse_path.is_dir():
            raise HTTPException(status_code=400, detail="Pfad ist kein Verzeichnis")
        
        # Get parent path
        parent_path = str(browse_path.parent) if browse_path != browse_path.parent else None
        
        # List directories
        directories = []
        try:
            for item in sorted(browse_path.iterdir()):
                if item.is_dir() and not item.name.startswith('.'):
                    try:
                        # Check if we can access the directory
                        list(item.iterdir())
                        directories.append({
                            "name": item.name,
                            "path": str(item),
                            "has_children": any(p.is_dir() for p in item.iterdir() if not p.name.startswith('.'))
                        })
                    except PermissionError:
                        # Include but mark as inaccessible
                        directories.append({
                            "name": item.name,
                            "path": str(item),
                            "has_children": False,
                            "access_denied": True
                        })
        except PermissionError:
            raise HTTPException(status_code=403, detail="Keine Berechtigung für diesen Ordner")
        
        return {
            "current_path": str(browse_path),
            "parent_path": parent_path,
            "directories": directories
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error browsing path {path}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/create-folder")
async def create_folder(path: str = Form(...)):
    """Create a new folder at the specified path."""
    try:
        folder_path = Path(path).resolve()
        
        # Security check
        forbidden_paths = ['/proc', '/sys', '/dev', '/run', '/etc', '/bin', '/sbin', '/usr']
        if any(str(folder_path).startswith(fp) for fp in forbidden_paths):
            raise HTTPException(status_code=403, detail="Ordner kann hier nicht erstellt werden")
        
        folder_path.mkdir(parents=True, exist_ok=True)
        
        return {"status": "success", "path": str(folder_path)}
        
    except PermissionError:
        raise HTTPException(status_code=403, detail="Keine Berechtigung zum Erstellen des Ordners")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
