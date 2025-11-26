"""
Microsoft Graph API Email Service.
Sends emails with PDF attachments using Microsoft Graph API and MSAL.
"""

import base64
import logging
from pathlib import Path
from typing import Optional

import msal
import requests

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
        
        # Use provided values or fall back to environment
        self.tenant_id = tenant_id or self.env_settings.tenant_id
        self.client_id = client_id or self.env_settings.client_id
        self.client_secret = client_secret or self.env_settings.client_secret
        self.sender_address = sender_address or self.env_settings.sender_address
        
        self._app: Optional[msal.ConfidentialClientApplication] = None
        self._token_cache: Optional[dict] = None
    
    def _create_app(self) -> msal.ConfidentialClientApplication:
        """Create a new MSAL application instance."""
        return msal.ConfidentialClientApplication(
            client_id=self.client_id,
            client_credential=self.client_secret,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
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
        attachment_path: Optional[Path] = None,
        attachment_name: Optional[str] = None,
    ) -> dict:
        """
        Send an email using Microsoft Graph API.
        
        Args:
            to_email: Recipient email address
            subject: Email subject
            body: Email body (plain text)
            attachment_path: Optional path to PDF attachment
            attachment_name: Optional custom name for attachment (defaults to filename)
            
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
        if attachment_path and attachment_path.exists():
            attachment_content = attachment_path.read_bytes()
            attachment_base64 = base64.b64encode(attachment_content).decode('utf-8')
            
            if attachment_name is None:
                attachment_name = attachment_path.name
            
            message["message"]["attachments"] = [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": attachment_name,
                    "contentType": "application/pdf",
                    "contentBytes": attachment_base64
                }
            ]
            logger.debug(f"Added attachment: {attachment_name} ({len(attachment_content)} bytes)")
        
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
                timeout=30
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
    
    if tenant_id and client_id and client_secret and sender_address:
        # Create new instance with provided credentials
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
    
    Args:
        to_email: Recipient email address
        pdf_path: Path to the invoice PDF
        email_template: Email body template
        
    Returns:
        API response dict
        
    Raises:
        GraphMailError: If sending fails
    """
    service = get_mail_service()
    
    # Use PDF filename without extension as subject
    subject = pdf_path.stem  # e.g., "RE-2025-12345"
    
    return service.send_email(
        to_email=to_email,
        subject=subject,
        body=email_template,
        attachment_path=pdf_path,
    )
