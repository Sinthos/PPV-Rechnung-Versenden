"""
Configuration management using Pydantic Settings.
Loads configuration from environment variables and .env file.
"""

import os
from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # Microsoft Graph API Configuration (optional - can be configured via GUI)
    tenant_id: str = Field(
        default="",
        description="Azure AD Tenant ID"
    )
    client_id: str = Field(
        default="",
        description="Azure AD Application Client ID"
    )
    client_secret: str = Field(
        default="",
        description="Azure AD Application Client Secret"
    )
    sender_address: str = Field(
        default="",
        description="Email address to send invoices from"
    )
    
    # Application Data Directory
    app_data_dir: str = Field(
        default="/opt/ppv-rechnung/data",
        description="Directory for application data (database, logs)"
    )
    
    # Default folder settings (can be overridden in web UI)
    default_source_folder: str = Field(
        default="/Dokumente",
        description="Default source folder for invoice PDFs"
    )
    default_target_folder: str = Field(
        default="/Dokumente/RE - Rechnung",
        description="Default target folder for processed invoices"
    )
    default_send_time: str = Field(
        default="09:00",
        description="Default daily send time (HH:MM format)"
    )
    
    # Web server settings
    host: str = Field(default="0.0.0.0", description="Web server host")
    port: int = Field(default=8000, description="Web server port")
    
    # Logging
    log_level: str = Field(default="INFO", description="Logging level")
    
    # Admin access to web UI / API (HTTP Basic Auth)
    admin_user: str = Field(default="", description="Basic Auth username for UI/API")
    admin_password: str = Field(default="", description="Basic Auth password for UI/API")
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
    
    @property
    def database_url(self) -> str:
        """Get SQLite database URL."""
        db_path = Path(self.app_data_dir) / "ppv_rechnung.db"
        return f"sqlite:///{db_path}"
    
    @property
    def database_path(self) -> Path:
        """Get SQLite database file path."""
        return Path(self.app_data_dir) / "ppv_rechnung.db"
    
    def ensure_directories(self) -> None:
        """Ensure all required directories exist."""
        Path(self.app_data_dir).mkdir(parents=True, exist_ok=True)


# Global settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create the global settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_directories()
    return _settings


def reload_settings() -> Settings:
    """Reload settings from environment."""
    global _settings
    _settings = Settings()
    _settings.ensure_directories()
    return _settings
