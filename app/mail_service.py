"""
Microsoft Graph API Email Service.
Sends emails with PDF attachments using Microsoft Graph API and MSAL.
"""

import base64
import logging
import os
import ssl
from pathlib import Path
from typing import Optional, Union

import msal
import requests # Import requests library

from app.config import get_settings

logger = logging.getLogger(__name__)


class GraphMailError(Exception):
    """Exception raised when Graph API email sending fails."""
    pass


class GraphMailService:
    """
    Microsoft Graph API mail service using MSAL client credentials flow.
    
    Requires Azure AD App Registration with:
    - Application (client) ID
    - Directory (tenant) ID
    - Client secret
    - API Permission: Mail.Send (Application permission)
    """
    
    GRAPH_API_ENDPOINT = "https://graph.microsoft.com/v1.0"
    SCOPE = ["https://graph.microsoft.com/.default"]
    
    def __init__(self, tenant_id: str = None, client_id: str = None, 
                 client_secret: str = None, sender_address: str = None):
        """
        Initialize the Graph Mail Service.
        
        Args:
            tenant_id: Azure AD Tenant ID (optional, falls back to env)
            client_id: Azure AD Client ID (optional, falls back to env)
            client_secret: Azure AD Client Secret (optional, falls back to env)
            sender_address: Email address to send from (optional, falls back to env)
        """
        self.env_settings = get_settings()
        
        # Use provided values if explicitly passed (even empty string means "explicitly set").
        # If parameter is None, fall back to environment settings.
        if tenant_id is not None:
            self.tenant_id = tenant_id
        else:
            self.tenant_id = self.env_settings.tenant_id

        if client_id is not None:
            self.client_id = client_id
        else:
            self.client_id = self.env_settings.client_id

        if client_secret is not None:
            self.client_secret = client_secret
        else:
            self.client_secret = self.env_settings.client_secret

        if sender_address is not None:
            self.sender_address = sender_address
        else:
            self.sender_address = self.env_settings.sender_address

        self._app: Optional[msal.ConfidentialClientApplication] = None
        self._token_cache: Optional[dict] = None
        self._ca_bundle: Optional[str] = None

    def _ensure_ca_bundle(self) -> str:
        """
        Ensure we have a valid CA bundle path for requests/msal.
        Falls back to system defaults if certifi path is missing.
        """
        # If an env override is present, validate it
        for env_key in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
            env_path = os.environ.get(env_key)
            if env_path:
                if Path(env_path).exists():
                    return env_path
                logger.warning(f"{env_key} points to missing CA bundle '{env_path}', falling back to defaults.")
                os.environ.pop(env_key, None)

        # Prefer certifi if available and the bundle exists
        try:
            import certifi
            certifi_path = certifi.where()
            if Path(certifi_path).exists():
                os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi_path)
                return certifi_path
            logger.warning(f"Certifi CA bundle not found at expected path: {certifi_path}")
        except Exception as e:
            logger.warning(f"Unable to load certifi CA bundle: {e}")

        # Fallback to common system paths
        fallback_paths = [
            ssl.get_default_verify_paths().cafile,
            "/etc/ssl/certs/ca-certificates.crt",
            "/etc/ssl/cert.pem"
        ]
        for path in fallback_paths:
            if path and Path(path).exists():
                os.environ["REQUESTS_CA_BUNDLE"] = path
                os.environ["SSL_CERT_FILE"] = path
                return path

        raise GraphMailError(
            "Kein gültiges TLS Zertifikatsbundle gefunden. "
            "Bitte certifi neu installieren oder einen gültigen Pfad in SSL_CERT_FILE/REQUESTS_CA_BUNDLE setzen."
        )

    def _create_app(self) -> msal.ConfidentialClientApplication:
        """Create a new MSAL application instance."""
        # Ensure TLS CA bundle is valid before making any requests
        self._ca_bundle = self._ensure_ca_bundle()

        # Validate credentials before creating MSAL app
        if not self.tenant_id or not self.client_id or not self.client_secret:
            raise GraphMailError(
                "Missing Microsoft Graph credentials (tenant_id, client_id, client_secret). "
                "Please configure them in the web UI or .env."
            )
            
        # Check for placeholders
        if "your-tenant-id" in self.tenant_id.lower() or "your-client-id" in self.client_id.lower():
            raise GraphMailError(
                "Invalid configuration: You are using placeholder credentials ('your-tenant-id-here', etc.). "
                "Please configure your actual Azure AD credentials in the Web UI."
            )
            
        authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        
        # Create a requests session and explicitly set trust_env to False
        # This prevents requests from picking up system-wide proxy/auth settings (e.g., SPNEGO)
        # that might interfere with MSAL's intended authentication flow.
        session = requests.Session()
        session.trust_env = False

        return msal.ConfidentialClientApplication(
            client_id=self.client_id,
            client_credential=self.client_secret,
            authority=authority,
            http_client=session # Pass the custom session to MSAL
        )
    
    @property
    def app(self) -> msal.ConfidentialClientApplication:
        """Get or create the MSAL application instance."""
        if self._app is None:
            self._app = self._create_app()
        return self._app
    
    def reconfigure(self, tenant_id: str, client_id: str, 
                    client_secret: str, sender_address: str):
        """Reconfigure the service with new credentials."""
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.sender_address = sender_address
        self._app = None  # Force recreation on next use
    
    def get_access_token(self) -> str:
        """
        Acquire an access token using client credentials flow.
        
        Returns:
            Access token string
            
        Raises:
            GraphMailError: If token acquisition fails
        """
        # Try to get token from cache first
        result = self.app.acquire_token_silent(self.SCOPE, account=None)
        
        if not result:
            logger.debug("No cached token, acquiring new token")
            result = self.app.acquire_token_for_client(scopes=self.SCOPE)
        
        if "access_token" in result:
            logger.debug("Successfully acquired access token")
            return result["access_token"]
        
        error_description = result.get("error_description", "Unknown error")
        error = result.get("error", "unknown")
        raise GraphMailError(f"Failed to acquire token: {error} - {error_description}")
    
    def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        attachment_path: Optional[Union[Path, str]] = None,
        attachment_name: Optional[str] = None,
        attachment_content: Optional[bytes] = None,
    ) -> dict:
        """
        Send an email using Microsoft Graph API.
        
        Args:
            to_email: Recipient email address
            subject: Email subject
            body: Email body (plain text)
            attachment_path: Optional path to PDF attachment (local file)
            attachment_name: Optional custom name for attachment (defaults to filename)
            attachment_content: Optional bytes content (overrides reading from path)
            
        Returns:
            API response as dict
            
        Raises:
            GraphMailError: If sending fails
        """
        logger.info(f"Sending email to {to_email} with subject: {subject}")
        
        # Get access token
        access_token = self.get_access_token()
        
        # Build the email message
        message = {
            "message": {
                "subject": subject,
                "body": {
                    "contentType": "Text",
                    "content": body
                },
                "toRecipients": [
                    {
                        "emailAddress": {
                            "address": to_email
                        }
                    }
                ],
            },
            "saveToSentItems": True
        }
        
        # Add attachment if provided
        final_content = None
        final_name = attachment_name
        
        if attachment_content is not None:
            final_content = attachment_content
            if final_name is None:
                final_name = "invoice.pdf" # Default fallback
        elif attachment_path:
            path_obj = Path(attachment_path)
            if path_obj.exists():
                final_content = path_obj.read_bytes()
                if final_name is None:
                    final_name = path_obj.name

        if final_content:
            attachment_base64 = base64.b64encode(final_content).decode('utf-8')
            
            message["message"]["attachments"] = [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": final_name,
                    "contentType": "application/pdf",
                    "contentBytes": attachment_base64
                }
            ]
            logger.debug(f"Added attachment: {final_name} ({len(final_content)} bytes)")
        
        # Send the email
        endpoint = f"{self.GRAPH_API_ENDPOINT}/users/{self.sender_address}/sendMail"
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        try:
            response = requests.post(
                endpoint,
                headers=headers,
                json=message,
                timeout=30,
                verify=self._ca_bundle or True
            )
            
            if response.status_code == 202:
                logger.info(f"Email sent successfully to {to_email}")
                return {"status": "sent", "recipient": to_email, "subject": subject}
            
            # Handle error response
            try:
                error_data = response.json()
                error_message = error_data.get("error", {}).get("message", response.text)
            except Exception:
                error_message = response.text
            
            raise GraphMailError(
                f"Failed to send email: HTTP {response.status_code} - {error_message}"
            )
            
        except requests.RequestException as e:
            raise GraphMailError(f"Network error sending email: {e}")
    
    def test_connection(self) -> dict:
        """
        Test the Graph API connection by acquiring a token.
        
        Returns:
            Dict with connection status
            
        Raises:
            GraphMailError: If connection test fails
        """
        try:
            token = self.get_access_token()
            return {
                "status": "connected",
                "tenant_id": self.tenant_id,
                "client_id": self.client_id,
                "sender": self.sender_address,
                "token_acquired": True
            }
        except GraphMailError:
            raise
        except Exception as e:
            raise GraphMailError(f"Connection test failed: {e}")


# Global service instance
_mail_service: Optional[GraphMailService] = None


def get_mail_service(tenant_id: str = None, client_id: str = None,
                     client_secret: str = None, sender_address: str = None) -> GraphMailService:
    """
    Get or create the mail service instance.
    
    If credentials are provided, creates a new instance with those credentials.
    Otherwise returns the cached instance or creates one with env settings.
    """
    global _mail_service
    
    if tenant_id is not None and client_id is not None and client_secret is not None and sender_address is not None:
        # Create new instance with provided credentials (allow empty strings to indicate explicit override)
        return GraphMailService(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            sender_address=sender_address
        )
    
    if _mail_service is None:
        _mail_service = GraphMailService()
    return _mail_service


def get_mail_service_from_db() -> GraphMailService:
    """Get mail service configured from database settings."""
    from app.database import get_db_session
    from app.models import AppSettings
    
    with get_db_session() as db:
        ms_settings = AppSettings.get_microsoft_settings(db)
    
    return get_mail_service(
        tenant_id=ms_settings['tenant_id'],
        client_id=ms_settings['client_id'],
        client_secret=ms_settings['client_secret'],
        sender_address=ms_settings['sender_address']
    )


def send_invoice_email(
    to_email: str,
    pdf_path: Path,
    email_template: str,
) -> dict:
    """
    Convenience function to send an invoice email.
    
    This helper uses the database-configured credentials when available.
    Falls back to environment settings only if no DB settings exist.
    
    Args:
        to_email: Recipient email address
        pdf_path: Path to the invoice PDF
        email_template: Email body template
        
    Returns:
        API response dict
        
    Raises:
            GraphMailError: If sending fails or credentials are missing
    """
    # Prefer DB-configured mail service
    service = get_mail_service_from_db()
    
    # Validate that tenant_id and client_id are present
    if not service.tenant_id or not service.client_id or not service.client_secret:
        raise GraphMailError(
            "Microsoft Graph credentials are not configured. "
            "Please configure Tenant ID, Client ID and Client Secret in the web UI."
        )
    
    # Use PDF filename without extension as subject
    subject = pdf_path.stem  # e.g., "RE-2025-12345"
    
    return service.send_email(
        to_email=to_email,
        subject=subject,
        body=email_template,
        attachment_path=pdf_path,
    )
