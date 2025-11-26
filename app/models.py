"""
Database models for the PPV Rechnung application.
Includes models for email logs and application settings.
"""

import logging
from datetime import datetime
from typing import Optional, List

from sqlalchemy import Column, Integer, String, DateTime, Text, desc
from sqlalchemy.orm import Session

from app.database import Base
from app.config import get_settings

logger = logging.getLogger(__name__)

# Maximum number of email logs to keep
MAX_EMAIL_LOGS = 100


class EmailLog(Base):
    """
    Model for storing sent email logs.
    Automatically prunes to keep only the last MAX_EMAIL_LOGS entries.
    """
    __tablename__ = "email_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    filename = Column(String(255), nullable=False)
    invoice_date = Column(String(20), nullable=False)
    recipient_email = Column(String(255), nullable=False)
    subject = Column(String(500), nullable=False)
    status = Column(String(50), default="sent", nullable=False)  # sent, failed
    error_message = Column(Text, nullable=True)
    
    def __repr__(self):
        return f"<EmailLog(id={self.id}, filename='{self.filename}', recipient='{self.recipient_email}')>"
    
    @classmethod
    def create(
        cls,
        db: Session,
        filename: str,
        invoice_date: str,
        recipient_email: str,
        subject: str,
        status: str = "sent",
        error_message: Optional[str] = None
    ) -> "EmailLog":
        """Create a new email log entry and prune old entries."""
        log_entry = cls(
            filename=filename,
            invoice_date=invoice_date,
            recipient_email=recipient_email,
            subject=subject,
            status=status,
            error_message=error_message
        )
        db.add(log_entry)
        db.flush()  # Get the ID
        
        # Prune old entries to keep only MAX_EMAIL_LOGS
        cls.prune_old_entries(db)
        
        return log_entry
    
    @classmethod
    def prune_old_entries(cls, db: Session) -> int:
        """Remove oldest entries to keep only MAX_EMAIL_LOGS."""
        count = db.query(cls).count()
        if count > MAX_EMAIL_LOGS:
            # Get IDs of entries to delete
            entries_to_delete = (
                db.query(cls.id)
                .order_by(cls.timestamp.asc())
                .limit(count - MAX_EMAIL_LOGS)
                .all()
            )
            ids_to_delete = [e.id for e in entries_to_delete]
            
            if ids_to_delete:
                db.query(cls).filter(cls.id.in_(ids_to_delete)).delete(synchronize_session=False)
                logger.info(f"Pruned {len(ids_to_delete)} old email log entries")
                return len(ids_to_delete)
        return 0
    
    @classmethod
    def get_recent(cls, db: Session, limit: int = 100) -> List["EmailLog"]:
        """Get the most recent email logs."""
        return (
            db.query(cls)
            .order_by(desc(cls.timestamp))
            .limit(limit)
            .all()
        )


class AppSettings(Base):
    """
    Model for storing application settings as key-value pairs.
    Settings can be modified through the web UI.
    """
    __tablename__ = "app_settings"
    
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Setting keys
    KEY_SOURCE_FOLDER = "source_folder"
    KEY_TARGET_FOLDER = "target_folder"
    KEY_SEND_TIME = "send_time"
    KEY_EMAIL_TEMPLATE = "email_template"
    
    # Network Storage Settings (SMB)
    KEY_STORAGE_TYPE = "storage_type"  # 'local' or 'smb'
    KEY_SMB_HOST = "smb_host"
    KEY_SMB_SHARE = "smb_share"
    KEY_SMB_USERNAME = "smb_username"
    KEY_SMB_PASSWORD = "smb_password"
    KEY_SMB_DOMAIN = "smb_domain"
    
    # Microsoft Graph API settings (stored in DB, override .env)
    KEY_TENANT_ID = "tenant_id"
    KEY_CLIENT_ID = "client_id"
    KEY_CLIENT_SECRET = "client_secret"
    KEY_SENDER_ADDRESS = "sender_address"
    
    # Default email template
    DEFAULT_EMAIL_TEMPLATE = """Sehr geehrte Damen und Herren,

anbei erhalten Sie unsere Rechnung als PDF-Dokument.

Bei Fragen stehen wir Ihnen gerne zur Verfügung.

Mit freundlichen Grüßen
PPV Medien GmbH"""
    
    def __repr__(self):
        return f"<AppSettings(key='{self.key}', value='{self.value[:50] if self.value else None}...')>"
    
    @classmethod
    def get(cls, db: Session, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a setting value by key."""
        setting = db.query(cls).filter(cls.key == key).first()
        if setting:
            return setting.value
        return default
    
    @classmethod
    def set(cls, db: Session, key: str, value: str) -> "AppSettings":
        """Set a setting value, creating or updating as needed."""
        setting = db.query(cls).filter(cls.key == key).first()
        if setting:
            setting.value = value
            setting.updated_at = datetime.utcnow()
        else:
            setting = cls(key=key, value=value)
            db.add(setting)
        db.flush()
        return setting
    
    @classmethod
    def get_all_settings(cls, db: Session) -> dict:
        """Get all settings as a dictionary with defaults applied."""
        settings = get_settings()
        
        # Use helper method to filter placeholders for MS settings
        ms_settings = cls.get_microsoft_settings(db)
        
        return {
            cls.KEY_SOURCE_FOLDER: cls.get(db, cls.KEY_SOURCE_FOLDER, settings.default_source_folder),
            cls.KEY_TARGET_FOLDER: cls.get(db, cls.KEY_TARGET_FOLDER, settings.default_target_folder),
            cls.KEY_SEND_TIME: cls.get(db, cls.KEY_SEND_TIME, settings.default_send_time),
            cls.KEY_EMAIL_TEMPLATE: cls.get(db, cls.KEY_EMAIL_TEMPLATE, cls.DEFAULT_EMAIL_TEMPLATE),
            # Storage settings
            cls.KEY_STORAGE_TYPE: cls.get(db, cls.KEY_STORAGE_TYPE, "local"),
            cls.KEY_SMB_HOST: cls.get(db, cls.KEY_SMB_HOST, ""),
            cls.KEY_SMB_SHARE: cls.get(db, cls.KEY_SMB_SHARE, ""),
            cls.KEY_SMB_USERNAME: cls.get(db, cls.KEY_SMB_USERNAME, ""),
            cls.KEY_SMB_PASSWORD: cls.get(db, cls.KEY_SMB_PASSWORD, ""),
            cls.KEY_SMB_DOMAIN: cls.get(db, cls.KEY_SMB_DOMAIN, ""),
            # Microsoft Graph settings
            cls.KEY_TENANT_ID: ms_settings['tenant_id'],
            cls.KEY_CLIENT_ID: ms_settings['client_id'],
            cls.KEY_CLIENT_SECRET: ms_settings['client_secret'],
            cls.KEY_SENDER_ADDRESS: ms_settings['sender_address'],
        }
    
    @classmethod
    def initialize_defaults(cls, db: Session) -> None:
        """Initialize default settings if they don't exist."""
        settings = get_settings()
        defaults = {
            cls.KEY_SOURCE_FOLDER: settings.default_source_folder,
            cls.KEY_TARGET_FOLDER: settings.default_target_folder,
            cls.KEY_SEND_TIME: settings.default_send_time,
            cls.KEY_EMAIL_TEMPLATE: cls.DEFAULT_EMAIL_TEMPLATE,
            cls.KEY_STORAGE_TYPE: "local",
            # Don't initialize Microsoft settings from env - let user configure via GUI
        }
        
        for key, value in defaults.items():
            existing = db.query(cls).filter(cls.key == key).first()
            if not existing:
                db.add(cls(key=key, value=value))
                logger.info(f"Initialized default setting: {key}")
        
        db.commit()
    
    @classmethod
    def get_microsoft_settings(cls, db: Session) -> dict:
        """
        Get Microsoft Graph API settings from database.
        Filters out placeholder values from environment variables.
        """
        settings = get_settings()
        
        def get_valid_value(db_key, env_value):
            # Check DB value first
            db_val = cls.get(db, db_key)
            if db_val and db_val.strip():
                return db_val
            
            # Check environment value, ignoring placeholders
            if env_value:
                # Filter out standard placeholders
                lower_val = env_value.lower()
                if "your-" in lower_val and "-here" in lower_val:
                    return ""
                # Filter out likely unmodified placeholders
                if "your-tenant-id" in lower_val or "your-client-id" in lower_val:
                    return ""
                if env_value == "rechnung@ppv-web.de":  # Default sender in example
                    return ""
                
                return env_value
            
            return ""

        return {
            'tenant_id': get_valid_value(cls.KEY_TENANT_ID, settings.tenant_id),
            'client_id': get_valid_value(cls.KEY_CLIENT_ID, settings.client_id),
            'client_secret': get_valid_value(cls.KEY_CLIENT_SECRET, settings.client_secret),
            'sender_address': get_valid_value(cls.KEY_SENDER_ADDRESS, settings.sender_address),
        }
