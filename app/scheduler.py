"""
Invoice Processing Scheduler.
Handles daily scheduled invoice processing and manual triggers.
"""

import logging
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Callable

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.base import JobLookupError

from app.config import get_settings
from app.database import get_db_session
from app.models import AppSettings, EmailLog
from app.invoice_parser import (
    find_invoice_files,
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


class InvoiceProcessor:
    """
    Processes invoice PDFs: parses, sends emails, and moves files.
    """
    
    def __init__(self):
        self.settings = get_settings()
    
    def process_invoices(self, force_send: bool = False) -> dict:
        """
        Process all invoice PDFs in the source folder.
        
        Args:
            force_send: If True, send all invoices regardless of date.
                       If False, only send invoices dated today.
        
        Returns:
            Dict with processing results
        """
        logger.info(f"Starting invoice processing (force_send={force_send})")
        
        results = {
            "processed": 0,
            "sent": 0,
            "skipped": 0,
            "failed": 0,
            "errors": []
        }
        
        with get_db_session() as db:
            # Get current settings
            app_settings = AppSettings.get_all_settings(db)
            source_folder = Path(app_settings[AppSettings.KEY_SOURCE_FOLDER])
            target_folder = Path(app_settings[AppSettings.KEY_TARGET_FOLDER])
            email_template = app_settings[AppSettings.KEY_EMAIL_TEMPLATE]
            
            # Ensure target folder exists
            target_folder.mkdir(parents=True, exist_ok=True)
            
            # Find invoice files
            invoice_files = find_invoice_files(source_folder)
            
            if not invoice_files:
                logger.info("No invoice files found to process")
                return results
            
            # Get today's date in Berlin timezone
            today = datetime.now(TIMEZONE).date()
            logger.info(f"Today's date (Europe/Berlin): {today}")
            
            for pdf_path in invoice_files:
                results["processed"] += 1
                
                try:
                    result = self._process_single_invoice(
                        db=db,
                        pdf_path=pdf_path,
                        target_folder=target_folder,
                        email_template=email_template,
                        today=today,
                        force_send=force_send
                    )
                    
                    if result == "sent":
                        results["sent"] += 1
                    elif result == "skipped":
                        results["skipped"] += 1
                    elif result == "failed":
                        results["failed"] += 1
                        
                except Exception as e:
                    logger.error(f"Unexpected error processing {pdf_path}: {e}")
                    results["failed"] += 1
                    results["errors"].append(f"{pdf_path.name}: {str(e)}")
            
            db.commit()
        
        logger.info(
            f"Invoice processing complete: "
            f"{results['sent']} sent, {results['skipped']} skipped, {results['failed']} failed"
        )
        
        return results
    
    def _process_single_invoice(
        self,
        db,
        pdf_path: Path,
        target_folder: Path,
        email_template: str,
        today: date,
        force_send: bool
    ) -> str:
        """
        Process a single invoice PDF.
        
        Returns:
            "sent", "skipped", or "failed"
        """
        filename = pdf_path.name
        logger.info(f"Processing invoice: {filename}")
        
        # Parse the invoice
        try:
            invoice_data = parse_invoice(pdf_path)
        except ZUGFeRDParseError as e:
            logger.error(f"Failed to parse invoice {filename}: {e}")
            EmailLog.create(
                db=db,
                filename=filename,
                invoice_date="",
                recipient_email="",
                subject=pdf_path.stem,
                status="failed",
                error_message=f"Parse error: {e}"
            )
            return "failed"
        
        # Check if we have required data
        if not invoice_data.recipient_email:
            logger.warning(f"No recipient email found in {filename}")
            EmailLog.create(
                db=db,
                filename=filename,
                invoice_date=invoice_data.invoice_date_str,
                recipient_email="",
                subject=pdf_path.stem,
                status="failed",
                error_message="No recipient email found in invoice"
            )
            return "failed"
        
        # Check invoice date (skip if not today, unless force_send)
        if not force_send:
            if invoice_data.invoice_date is None:
                logger.warning(f"No invoice date found in {filename}, skipping")
                return "skipped"
            
            if invoice_data.invoice_date != today:
                logger.info(
                    f"Invoice date {invoice_data.invoice_date} != today {today}, skipping {filename}"
                )
                return "skipped"
        
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
                subject=pdf_path.stem,
                body=email_template,
                attachment_path=pdf_path
            )
        except GraphMailError as e:
            logger.error(f"Failed to send email for {filename}: {e}")
            EmailLog.create(
                db=db,
                filename=filename,
                invoice_date=invoice_data.invoice_date_str,
                recipient_email=invoice_data.recipient_email,
                subject=pdf_path.stem,
                status="failed",
                error_message=f"Send error: {e}"
            )
            return "failed"
        
        # Log successful send
        EmailLog.create(
            db=db,
            filename=filename,
            invoice_date=invoice_data.invoice_date_str,
            recipient_email=invoice_data.recipient_email,
            subject=pdf_path.stem,
            status="sent"
        )
        
        # Move file to target folder
        try:
            target_path = target_folder / filename
            
            # Handle duplicate filenames
            if target_path.exists():
                base = pdf_path.stem
                suffix = 1
                while target_path.exists():
                    target_path = target_folder / f"{base}_{suffix}.pdf"
                    suffix += 1
            
            shutil.move(str(pdf_path), str(target_path))
            logger.info(f"Moved {filename} to {target_path}")
            
        except Exception as e:
            logger.error(f"Failed to move {filename} to target folder: {e}")
            # Don't fail the whole operation, email was sent successfully
        
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


def run_now() -> dict:
    """
    Manually trigger invoice processing immediately.
    
    Returns:
        Processing results dict
    """
    logger.info("Manual invoice processing triggered")
    processor = get_processor()
    return processor.process_invoices(force_send=True)


def get_next_run_time() -> Optional[datetime]:
    """Get the next scheduled run time."""
    scheduler = get_scheduler()
    
    if not scheduler.running:
        return None
    
    job = scheduler.get_job(DAILY_JOB_ID)
    if job:
        return job.next_run_time
    
    return None
