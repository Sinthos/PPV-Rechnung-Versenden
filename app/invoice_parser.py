"""
ZUGFeRD/Factur-X PDF Invoice Parser.
Extracts embedded XML from PDF invoices and parses invoice data.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Tuple, List, Union, BinaryIO
from io import BytesIO

import pikepdf
from lxml import etree

logger = logging.getLogger(__name__)


@dataclass
class InvoiceData:
    """Parsed invoice data from ZUGFeRD XML."""
    invoice_date: Optional[date]
    invoice_date_str: str
    recipient_email: Optional[str]
    invoice_number: Optional[str]
    buyer_name: Optional[str]
    raw_xml: Optional[str] = None


class ZUGFeRDParseError(Exception):
    """Exception raised when ZUGFeRD parsing fails."""
    pass


# XML Namespaces used in ZUGFeRD/Factur-X/XRechnung
# NOTE: Adjust these namespaces if your invoices use different versions
NAMESPACES = {
    # ZUGFeRD 2.0 / Factur-X
    'rsm': 'urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100',
    'ram': 'urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100',
    'udt': 'urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100',
    'qdt': 'urn:un:unece:uncefact:data:standard:QualifiedDataType:100',
    
    # ZUGFeRD 1.0 (legacy)
    'rsm1': 'urn:ferd:CrossIndustryDocument:invoice:1p0',
    'ram1': 'urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:12',
    'udt1': 'urn:un:unece:uncefact:data:standard:UnqualifiedDataType:15',
}

# Common embedded XML filenames in ZUGFeRD PDFs
ZUGFERD_XML_FILENAMES = [
    'factur-x.xml',
    'zugferd-invoice.xml', 
    'ZUGFeRD-invoice.xml',
    'xrechnung.xml',
    'invoice.xml',
]


def extract_xml_from_pdf(pdf_source: Union[Path, str, BinaryIO]) -> Optional[str]:
    """
    Extract embedded ZUGFeRD/Factur-X XML from a PDF file.
    
    Args:
        pdf_source: Path to the PDF file or file-like object
        
    Returns:
        XML content as string, or None if not found
        
    Raises:
        ZUGFeRDParseError: If PDF cannot be read or processed
    """
    try:
        # pikepdf.open handles paths and file-like objects
        with pikepdf.open(pdf_source) as pdf:
            # Check for embedded files in the PDF
            if '/Names' not in pdf.Root:
                logger.debug(f"No /Names in PDF root: {pdf_source}")
                return None
            
            names = pdf.Root['/Names']
            if '/EmbeddedFiles' not in names:
                logger.debug(f"No /EmbeddedFiles in PDF: {pdf_source}")
                return None
            
            embedded_files = names['/EmbeddedFiles']
            
            # Get the file names array
            if '/Names' in embedded_files:
                files_array = embedded_files['/Names']
            elif '/Kids' in embedded_files:
                # Handle nested structure
                files_array = []
                for kid in embedded_files['/Kids']:
                    if '/Names' in kid:
                        files_array.extend(kid['/Names'])
            else:
                logger.debug(f"No embedded files array found: {pdf_source}")
                return None
            
            # Iterate through embedded files (name, filespec pairs)
            for i in range(0, len(files_array), 2):
                filename = str(files_array[i])
                filespec = files_array[i + 1]
                
                # Check if this is a ZUGFeRD XML file
                filename_lower = filename.lower()
                if any(zf.lower() in filename_lower for zf in ZUGFERD_XML_FILENAMES) or filename_lower.endswith('.xml'):
                    logger.info(f"Found embedded XML: {filename}")
                    
                    # Extract the file content
                    if '/EF' in filespec and '/F' in filespec['/EF']:
                        stream = filespec['/EF']['/F']
                        xml_bytes = bytes(stream.read_bytes())
                        xml_content = xml_bytes.decode('utf-8')
                        return xml_content
            
            logger.debug(f"No ZUGFeRD XML found in embedded files: {pdf_source}")
            return None
            
    except pikepdf.PdfError as e:
        raise ZUGFeRDParseError(f"Failed to read PDF: {e}")
    except Exception as e:
        raise ZUGFeRDParseError(f"Error extracting XML from PDF: {e}")


def parse_invoice_date(xml_content: str) -> Tuple[Optional[date], str]:
    """
    Parse the invoice date from ZUGFeRD XML.
    
    Args:
        xml_content: XML content as string
        
    Returns:
        Tuple of (date object or None, date string)
        
    NOTE: Adjust XPath expressions below if your XML structure differs.
    Common locations for invoice date:
    - ZUGFeRD 2.0: //rsm:ExchangedDocument/ram:IssueDateTime/udt:DateTimeString
    - ZUGFeRD 1.0: //rsm1:HeaderExchangedDocument/ram1:IssueDateTime/udt1:DateTimeString
    """
    try:
        root = etree.fromstring(xml_content.encode('utf-8'))
        
        # XPath expressions for invoice date (try multiple formats)
        # NOTE: Modify these XPaths to match your specific ZUGFeRD/XRechnung format
        date_xpaths = [
            # ZUGFeRD 2.0 / Factur-X
            '//rsm:ExchangedDocument/ram:IssueDateTime/udt:DateTimeString/text()',
            '//ram:IssueDateTime/udt:DateTimeString/text()',
            
            # Alternative paths
            '//rsm:ExchangedDocument/ram:IssueDateTime/ram:DateTimeString/text()',
            '//ram:ExchangedDocument/ram:IssueDateTime/udt:DateTimeString/text()',
            
            # ZUGFeRD 1.0 (legacy)
            '//rsm1:HeaderExchangedDocument/ram1:IssueDateTime/udt1:DateTimeString/text()',
            
            # Generic fallbacks
            '//*[local-name()="IssueDateTime"]/*[local-name()="DateTimeString"]/text()',
            '//*[local-name()="IssueDate"]/text()',
        ]
        
        for xpath in date_xpaths:
            try:
                results = root.xpath(xpath, namespaces=NAMESPACES)
                if results:
                    date_str = str(results[0]).strip()
                    logger.debug(f"Found date string: {date_str}")
                    
                    # Parse date - common formats: YYYYMMDD, YYYY-MM-DD
                    parsed_date = parse_date_string(date_str)
                    if parsed_date:
                        return parsed_date, date_str
            except etree.XPathEvalError:
                continue
        
        logger.warning("Could not find invoice date in XML")
        return None, ""
        
    except etree.XMLSyntaxError as e:
        logger.error(f"XML syntax error: {e}")
        return None, ""


def parse_date_string(date_str: str) -> Optional[date]:
    """
    Parse a date string in various formats.
    
    Common ZUGFeRD date formats:
    - YYYYMMDD (format code 102)
    - YYYY-MM-DD
    - DD.MM.YYYY
    """
    date_str = date_str.strip()
    
    # Try different date formats
    formats = [
        '%Y%m%d',      # 20231215
        '%Y-%m-%d',    # 2023-12-15
        '%d.%m.%Y',    # 15.12.2023
        '%d/%m/%Y',    # 15/12/2023
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    
    # Try to extract date from longer strings (e.g., with format attribute)
    match = re.search(r'(\d{8})', date_str)
    if match:
        try:
            return datetime.strptime(match.group(1), '%Y%m%d').date()
        except ValueError:
            pass
    
    logger.warning(f"Could not parse date string: {date_str}")
    return None


def parse_recipient_email(xml_content: str) -> Optional[str]:
    """
    Parse the recipient (buyer) email from ZUGFeRD XML.
    
    Args:
        xml_content: XML content as string
        
    Returns:
        Email address or None if not found
        
    NOTE: Adjust XPath expressions below if your XML structure differs.
    Common locations for buyer email:
    - //ram:BuyerTradeParty/ram:DefinedTradeContact/ram:EmailURIUniversalCommunication/ram:URIID
    - //ram:BuyerTradeParty/ram:URIUniversalCommunication/ram:URIID
    """
    try:
        root = etree.fromstring(xml_content.encode('utf-8'))
        
        # XPath expressions for buyer email
        # NOTE: Modify these XPaths to match your specific ZUGFeRD/XRechnung format
        email_xpaths = [
            # ZUGFeRD 2.0 / Factur-X - Buyer contact email
            '//ram:BuyerTradeParty/ram:DefinedTradeContact/ram:EmailURIUniversalCommunication/ram:URIID/text()',
            '//ram:BuyerTradeParty/ram:URIUniversalCommunication/ram:URIID/text()',
            
            # Alternative buyer email locations
            '//ram:ApplicableHeaderTradeAgreement/ram:BuyerTradeParty/ram:DefinedTradeContact/ram:EmailURIUniversalCommunication/ram:URIID/text()',
            '//ram:ApplicableHeaderTradeAgreement/ram:BuyerTradeParty/ram:URIUniversalCommunication/ram:URIID/text()',
            
            # Generic email search
            '//*[local-name()="BuyerTradeParty"]//*[local-name()="EmailURIUniversalCommunication"]/*[local-name()="URIID"]/text()',
            '//*[local-name()="BuyerTradeParty"]//*[local-name()="URIUniversalCommunication"]/*[local-name()="URIID"]/text()',
            
            # Fallback: any URIID that looks like an email under BuyerTradeParty
            '//*[local-name()="BuyerTradeParty"]//*[local-name()="URIID"]/text()',
        ]
        
        for xpath in email_xpaths:
            try:
                results = root.xpath(xpath, namespaces=NAMESPACES)
                for result in results:
                    email = str(result).strip()
                    # Validate it looks like an email
                    if '@' in email and '.' in email:
                        logger.debug(f"Found recipient email: {email}")
                        return email
            except etree.XPathEvalError:
                continue
        
        logger.warning("Could not find recipient email in XML")
        return None
        
    except etree.XMLSyntaxError as e:
        logger.error(f"XML syntax error: {e}")
        return None


def parse_invoice_number(xml_content: str) -> Optional[str]:
    """
    Parse the invoice number from ZUGFeRD XML.
    
    Args:
        xml_content: XML content as string
        
    Returns:
        Invoice number or None if not found
    """
    try:
        root = etree.fromstring(xml_content.encode('utf-8'))
        
        # XPath expressions for invoice number
        number_xpaths = [
            '//rsm:ExchangedDocument/ram:ID/text()',
            '//ram:ExchangedDocument/ram:ID/text()',
            '//*[local-name()="ExchangedDocument"]/*[local-name()="ID"]/text()',
            '//*[local-name()="InvoiceNumber"]/text()',
        ]
        
        for xpath in number_xpaths:
            try:
                results = root.xpath(xpath, namespaces=NAMESPACES)
                if results:
                    return str(results[0]).strip()
            except etree.XPathEvalError:
                continue
        
        return None
        
    except etree.XMLSyntaxError:
        return None


def parse_buyer_name(xml_content: str) -> Optional[str]:
    """
    Parse the buyer/recipient name from ZUGFeRD XML.
    
    Args:
        xml_content: XML content as string
        
    Returns:
        Buyer name or None if not found
    """
    try:
        root = etree.fromstring(xml_content.encode('utf-8'))
        
        # XPath expressions for buyer name
        name_xpaths = [
            '//ram:BuyerTradeParty/ram:Name/text()',
            '//ram:ApplicableHeaderTradeAgreement/ram:BuyerTradeParty/ram:Name/text()',
            '//*[local-name()="BuyerTradeParty"]/*[local-name()="Name"]/text()',
        ]
        
        for xpath in name_xpaths:
            try:
                results = root.xpath(xpath, namespaces=NAMESPACES)
                if results:
                    return str(results[0]).strip()
            except etree.XPathEvalError:
                continue
        
        return None
        
    except etree.XMLSyntaxError:
        return None


def parse_invoice(pdf_source: Union[Path, str, BinaryIO], filename: str = "") -> InvoiceData:
    """
    Parse a ZUGFeRD/Factur-X invoice PDF and extract relevant data.
    
    Args:
        pdf_source: Path to the PDF invoice file or file-like object
        filename: Optional filename for logging/identification
        
    Returns:
        InvoiceData object with parsed information
        
    Raises:
        ZUGFeRDParseError: If parsing fails
    """
    log_name = filename or str(pdf_source)
    logger.info(f"Parsing invoice: {log_name}")
    
    # Extract XML from PDF
    xml_content = extract_xml_from_pdf(pdf_source)
    if not xml_content:
        raise ZUGFeRDParseError(f"No ZUGFeRD XML found in PDF: {log_name}")
    
    # Parse invoice date
    invoice_date, invoice_date_str = parse_invoice_date(xml_content)
    
    # Parse recipient email
    recipient_email = parse_recipient_email(xml_content)
    
    # Parse additional info
    invoice_number = parse_invoice_number(xml_content)
    buyer_name = parse_buyer_name(xml_content)
    
    invoice_data = InvoiceData(
        invoice_date=invoice_date,
        invoice_date_str=invoice_date_str,
        recipient_email=recipient_email,
        invoice_number=invoice_number,
        buyer_name=buyer_name,
        raw_xml=xml_content
    )
    
    logger.info(f"Parsed invoice: date={invoice_date}, email={recipient_email}, number={invoice_number}")
    
    return invoice_data


def find_invoice_files(source_folder: Path, pattern: str = "RE-*.pdf") -> List[Path]:
    """
    Find invoice PDF files matching the pattern in the source folder.
    
    Args:
        source_folder: Directory to search
        pattern: Glob pattern for invoice files (default: RE-*.pdf)
        
    Returns:
        List of matching file paths
    """
    if not source_folder.exists():
        logger.warning(f"Source folder does not exist: {source_folder}")
        return []
    
    if not source_folder.is_dir():
        logger.warning(f"Source path is not a directory: {source_folder}")
        return []
    
    files = list(source_folder.glob(pattern))
    logger.info(f"Found {len(files)} invoice files in {source_folder}")
    
    return sorted(files)
