"""
Invoice Processing Scheduler.
Handles daily scheduled invoice processing and manual triggers.
"""

import logging
import shutil
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Callable, Set
from io import BytesIO

import pytz
from collections import defaultdict
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.base import JobLookupError

from app.config import get_settings
from app.database import get_db_session
from app.models import AppSettings, EmailLog
from app.filesystem import get_filesystem, FileSystemProvider
from app.invoice_parser import (
    parse_invoice,
    ZUGFeRDParseError,
    InvoiceData
)
from app.mail_service import GraphMailService, GraphMailError

logger = logging.getLogger(__name__)

# Timezone for scheduling
TIMEZONE = pytz.timezone('Europe/Berlin')

# Job ID for the daily invoice processing job
DAILY_JOB_ID = "daily_invoice_processing"


class _SafeDict(defaultdict):
    """Default dict that returns empty string for missing keys."""
    def __missing__(self, key):
        return ""


def render_email_template(template: str, invoice_data: InvoiceData, filename: str, today: date) -> str:
    """
    Render the email template with available invoice placeholders.
    
    Supported placeholders:
        {invoice_number}, {buyer_name}, {invoice_date}, {invoice_date_iso},
        {recipient_email}, {filename}, {today}
    """
    if not template:
        return ""

    values = _SafeDict(
        {
            "invoice_number": invoice_data.invoice_number or "",
            "buyer_name": invoice_data.buyer_name or "",
            "invoice_date": invoice_data.invoice_date_str or "",
            "invoice_date_iso": invoice_data.invoice_date.isoformat() if invoice_data.invoice_date else "",
            "recipient_email": invoice_data.recipient_email or "",
            "filename": filename,
            "today": today.isoformat(),
        }
    )

    try:
        return template.format_map(values)
    except Exception:
        # If format fails (e.g., stray braces), fall back to simple replace for known keys
        rendered = template
        for key, val in values.items():
            rendered = rendered.replace(f"{{{key}}}", str(val))
        return rendered


class InvoiceProcessor:
    """
    Processes invoice PDFs: parses, sends emails, and moves files.
    """
    
    def __init__(self):
        self.settings = get_settings()
    
    def process_invoices(
        self,
        force_send: bool = False,
        dry_run: bool = False,
        allow_resend: bool = False,
        selected_files: Optional[Set[str]] = None
    ) -> dict:
        """
        Process all invoice PDFs in the source folder.
        
        Args:
            force_send: If True, send all invoices regardless of date.
                       If False, only send invoices dated today.
            dry_run: If True, parse and validate but do not send or move files.
            allow_resend: If True, skip duplicate checks and resend emails.
        
        Returns:
            Dict with processing results
        """
        logger.info(f"Starting invoice processing (force_send={force_send})")
        
        results = {
            "processed": 0,
            "sent": 0,
            "skipped": 0,
            "failed": 0,
            "would_send": 0,
            "errors": []
        }
        
        with get_db_session() as db:
            # Get current settings
            app_settings = AppSettings.get_all_settings(db)
            
            # Use filesystem abstraction
            fs = get_filesystem(app_settings)
            
            source_folder = app_settings[AppSettings.KEY_SOURCE_FOLDER]
            target_folder = app_settings[AppSettings.KEY_TARGET_FOLDER]
            email_template = app_settings[AppSettings.KEY_EMAIL_TEMPLATE]
            send_past_dates = str(app_settings.get(AppSettings.KEY_SEND_PAST_DATES, "false")).lower() == "true"
            
            # Ensure target folder exists
            try:
                if not fs.exists(target_folder):
                    fs.create_directory(target_folder)
            except Exception as e:
                logger.error(f"Failed to create target folder {target_folder}: {e}")
                return results
            
            # Find invoice files
            try:
                invoice_files = fs.list_files(source_folder, pattern="RE-*.pdf")
            except Exception as e:
                logger.error(f"Failed to list files in {source_folder}: {e}")
                return results
            
            if not invoice_files:
                logger.info(f"No invoice files found in {source_folder}")
                return results
            
            logger.info(f"Found {len(invoice_files)} invoice files to process")
            
            # Get today's date in Berlin timezone
            today = datetime.now(TIMEZONE).date()
            logger.info(f"Today's date (Europe/Berlin): {today}")
            
            for file_path in invoice_files:
                filename = os.path.basename(file_path)

                if selected_files is not None and filename not in selected_files:
                    continue

                results["processed"] += 1
                
                try:
                    result = self._process_single_invoice(
                        db=db,
                        fs=fs,
                        file_path=file_path,
                        filename=filename,
                        target_folder=target_folder,
                        email_template=email_template,
                        today=today,
                        force_send=force_send,
                        send_past_dates=send_past_dates,
                        dry_run=dry_run,
                        allow_resend=allow_resend
                    )
                    
                    if result == "sent":
                        results["sent"] += 1
                        results["would_send"] += 1
                    elif result == "dry_send":
                        results["would_send"] += 1
                    elif result == "skipped":
                        results["skipped"] += 1
                    elif result == "failed":
                        results["failed"] += 1
                        
                except Exception as e:
                    logger.error(f"Unexpected error processing {filename}: {e}")
                    results["failed"] += 1
                    results["errors"].append(f"{filename}: {str(e)}")
                    if not dry_run:
                        try:
                            EmailLog.create(
                                db=db,
                                filename=filename,
                                invoice_date="",
                                recipient_email="",
                                subject=filename.replace(".pdf", ""),
                                status="failed",
                                error_message=f"Unerwarteter Fehler: {e}"
                            )
                        except Exception as log_err:
                            logger.error(f"Could not record failure for {filename}: {log_err}")
            
            if dry_run:
                db.rollback()
            else:
                db.commit()
        
        logger.info(
            f"Invoice processing complete: "
            f"{results['sent']} sent, {results['skipped']} skipped, {results['failed']} failed, "
            f"{results['would_send']} would_send"
        )
        
        return results
    
    def _process_single_invoice(
        self,
        db,
        fs: FileSystemProvider,
        file_path: str,
        filename: str,
        target_folder: str,
        email_template: str,
        today: date,
        force_send: bool,
        send_past_dates: bool,
        dry_run: bool,
        allow_resend: bool
    ) -> str:
        """
        Process a single invoice PDF.
        
        Returns:
            "sent", "skipped", "dry_send", or "failed"
        """
        logger.info(f"Processing invoice: {filename}")
        subject_text = filename.replace(".pdf", "")
        
        # Read file content
        try:
            pdf_content = fs.read_file(file_path)
        except Exception as e:
            logger.error(f"Failed to read file {filename}: {e}")
            if not dry_run:
                EmailLog.create(
                    db=db,
                    filename=filename,
                    invoice_date="",
                    recipient_email="",
                    subject=subject_text,
                    status="failed",
                    error_message=f"Datei konnte nicht gelesen werden: {e}"
                )
            return "failed"
            
        # Parse the invoice
        try:
            invoice_data = parse_invoice(BytesIO(pdf_content), filename=filename)
        except ZUGFeRDParseError as e:
            logger.error(f"Failed to parse invoice {filename}: {e}")
            if not dry_run:
                EmailLog.create(
                    db=db,
                    filename=filename,
                    invoice_date="",
                    recipient_email="",
                    subject=filename.replace(".pdf", ""),
                    status="failed",
                    error_message=f"Parse error: {e}"
                )
            return "failed"
        
        # Check if we have required data
        if not invoice_data.recipient_email:
            logger.warning(f"No recipient email found in {filename}")
            if not dry_run:
                EmailLog.create(
                    db=db,
                    filename=filename,
                    invoice_date=invoice_data.invoice_date_str,
                    recipient_email="",
                    subject=subject_text,
                    status="failed",
                    error_message="No recipient email found in invoice"
                )
            return "failed"

        # Duplicate protection unless explicitly allowed
        if not allow_resend:
            already_sent = (
                db.query(EmailLog)
                .filter(
                    EmailLog.filename == filename,
                    EmailLog.recipient_email == invoice_data.recipient_email,
                    EmailLog.status == "sent"
                )
                .first()
            )
            if already_sent:
                logger.info(f"Invoice {filename} already sent to {invoice_data.recipient_email}, skipping")
                return "skipped"
        
        # Check invoice date (skip if not today, unless force_send)
        if not force_send:
            if invoice_data.invoice_date is None:
                logger.warning(f"No invoice date found in {filename}, skipping")
                return "skipped"
            
            # Only send if date is today or (optionally) in the past; always skip future unless force_send
            if invoice_data.invoice_date > today:
                logger.info(
                    f"Invoice date {invoice_data.invoice_date} is in the future, skipping {filename}"
                )
                return "skipped"
            if not send_past_dates and invoice_data.invoice_date != today:
                logger.info(
                    f"Invoice date {invoice_data.invoice_date} != today {today} and past sending disabled, skipping {filename}"
                )
                return "skipped"

        # Render subject/body placeholders
        subject_text = render_email_template(subject_text, invoice_data, filename, today)
        email_body = render_email_template(email_template, invoice_data, filename, today)

        # Dry run: report what would happen without side effects
        if dry_run:
            logger.info(f"Dry run: would send {filename} to {invoice_data.recipient_email}")
            return "dry_send"
        
        # Get mail service with current DB settings
        ms_settings = AppSettings.get_microsoft_settings(db)
        mail_service = GraphMailService(
            tenant_id=ms_settings['tenant_id'],
            client_id=ms_settings['client_id'],
            client_secret=ms_settings['client_secret'],
            sender_address=ms_settings['sender_address']
        )
        
        # Send the email
        try:
            mail_service.send_email(
                to_email=invoice_data.recipient_email,
                subject=subject_text,
                body=email_body,
                attachment_content=pdf_content,
                attachment_name=filename
            )
        except GraphMailError as e:
            logger.error(f"Failed to send email for {filename}: {e}")
            EmailLog.create(
                db=db,
                filename=filename,
                invoice_date=invoice_data.invoice_date_str,
                recipient_email=invoice_data.recipient_email,
                subject=subject_text,
                status="failed",
                error_message=f"Send error: {e}"
            )
            return "failed"
        
        # Log successful send
        log_entry = EmailLog.create(
            db=db,
            filename=filename,
            invoice_date=invoice_data.invoice_date_str,
            recipient_email=invoice_data.recipient_email,
            subject=subject_text,
            status="sent"
        )
        
        # Move file to target folder
        try:
            # Construct target path
            # We use join_path from fs to handle correct separators
            target_path = fs.join_path(target_folder, filename)
            
            # Check for duplicates? fs.exists might work if path is correct
            # Handling duplicates on SMB without full path manipulation libraries is simpler by just overwriting 
            # or we can check existence.
            if fs.exists(target_path):
                # Simple rename strategy: append timestamp
                base, ext = os.path.splitext(filename)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                new_filename = f"{base}_{ts}{ext}"
                target_path = fs.join_path(target_folder, new_filename)
            
            fs.move_file(file_path, target_path)
            logger.info(f"Moved {filename} to {target_path}")
            
        except Exception as e:
            logger.error(f"Failed to move {filename} to target folder: {e}")
            # Don't fail the whole operation, email was sent successfully
            # Note: We cannot access 'results' here as it's not passed to this method.
            # Instead we return a special status or just log it and rely on the return value.
            
            # Mark log entry as failed so UI and summary reflect the move problem
            if log_entry:
                log_entry.status = "sent"
                log_entry.error_message = f"Datei nicht verschoben: {e}"
            
            # Since email was sent, treat it as sent but keep the warning in the log
            return "sent"
        
        return "sent"


# Global scheduler instance
_scheduler: Optional[BackgroundScheduler] = None
_processor: Optional[InvoiceProcessor] = None


def get_processor() -> InvoiceProcessor:
    """Get or create the global invoice processor instance."""
    global _processor
    if _processor is None:
        _processor = InvoiceProcessor()
    return _processor


def get_scheduler() -> BackgroundScheduler:
    """Get or create the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=TIMEZONE)
    return _scheduler


def scheduled_job():
    """Job function called by the scheduler."""
    logger.info("Scheduled invoice processing triggered")
    try:
        processor = get_processor()
        results = processor.process_invoices(force_send=False)
        logger.info(f"Scheduled processing results: {results}")
    except Exception as e:
        logger.error(f"Scheduled processing failed: {e}")


def start_scheduler():
    """Start the scheduler with the configured daily time."""
    scheduler = get_scheduler()
    
    if scheduler.running:
        logger.info("Scheduler already running")
        return
    
    # Get the configured send time
    with get_db_session() as db:
        app_settings = AppSettings.get_all_settings(db)
        send_time = app_settings[AppSettings.KEY_SEND_TIME]
    
    # Parse time
    try:
        hour, minute = map(int, send_time.split(':'))
    except ValueError:
        logger.warning(f"Invalid send time '{send_time}', using default 09:00")
        hour, minute = 9, 0
    
    # Add the daily job
    scheduler.add_job(
        scheduled_job,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=TIMEZONE),
        id=DAILY_JOB_ID,
        replace_existing=True,
        name="Daily Invoice Processing"
    )
    
    scheduler.start()
    logger.info(f"Scheduler started, daily job scheduled at {hour:02d}:{minute:02d} Europe/Berlin")


def stop_scheduler():
    """Stop the scheduler."""
    scheduler = get_scheduler()
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def reschedule_daily_job(send_time: str):
    """
    Reschedule the daily job with a new time.
    
    Args:
        send_time: New time in HH:MM format
    """
    scheduler = get_scheduler()
    
    if not scheduler.running:
        logger.warning("Scheduler not running, cannot reschedule")
        return
    
    # Parse time
    try:
        hour, minute = map(int, send_time.split(':'))
    except ValueError:
        logger.error(f"Invalid send time format: {send_time}")
        return
    
    # Remove existing job if present
    try:
        scheduler.remove_job(DAILY_JOB_ID)
    except JobLookupError:
        pass
    
    # Add new job with updated time
    scheduler.add_job(
        scheduled_job,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=TIMEZONE),
        id=DAILY_JOB_ID,
        replace_existing=True,
        name="Daily Invoice Processing"
    )
    
    logger.info(f"Rescheduled daily job to {hour:02d}:{minute:02d} Europe/Berlin")


def run_now(dry_run: bool = False, allow_resend: bool = False, selected_files: Optional[Set[str]] = None) -> dict:
    """
    Manually trigger invoice processing immediately.
    
    Returns:
        Processing results dict
    """
    logger.info("Manual invoice processing triggered")
    processor = get_processor()
    return processor.process_invoices(
        force_send=True,
        dry_run=dry_run,
        allow_resend=allow_resend,
        selected_files=selected_files
    )


def get_next_run_time() -> Optional[datetime]:
    """Get the next scheduled run time."""
    scheduler = get_scheduler()
    
    if not scheduler.running:
        return None
    
    job = scheduler.get_job(DAILY_JOB_ID)
    if job:
        return job.next_run_time
    
    return None
