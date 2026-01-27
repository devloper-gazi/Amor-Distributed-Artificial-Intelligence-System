"""
Multimodal Content Processor.

Handles extraction of content from various media types:
- Image OCR using Tesseract
- Table extraction from PDFs using Camelot/Tabula
- Audio transcription using Whisper (optional)
- Unified MultimodalSample format for all content types
"""

import asyncio
import base64
import hashlib
import io
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Any, Union, BinaryIO

logger = logging.getLogger(__name__)

# Optional imports with availability flags
try:
    import pytesseract
    from PIL import Image
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False
    logger.warning("pytesseract/PIL not installed. OCR will not be available.")

try:
    import fitz  # PyMuPDF
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False
    logger.warning("PyMuPDF not installed. PDF processing limited.")

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False
    logger.warning("pdfplumber not installed. Table extraction limited.")

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False
    logger.warning("opencv-python not installed. Image preprocessing disabled.")


class ContentType(Enum):
    """Types of content that can be processed."""
    TEXT = "text"
    IMAGE = "image"
    PDF = "pdf"
    TABLE = "table"
    AUDIO = "audio"
    VIDEO = "video"
    UNKNOWN = "unknown"


class ExtractionMethod(Enum):
    """Methods used for content extraction."""
    DIRECT = "direct"
    OCR = "ocr"
    TABLE_EXTRACTION = "table_extraction"
    TRANSCRIPTION = "transcription"
    COMBINED = "combined"


@dataclass
class MultimodalSample:
    """
    Unified format for multimodal content.
    
    This class provides a standardized representation for content
    extracted from various sources (images, PDFs, audio, etc.).
    """
    id: str
    content_type: ContentType
    extraction_method: ExtractionMethod
    
    # Extracted content
    text: str = ""
    tables: List[Dict[str, Any]] = field(default_factory=list)
    images: List[Dict[str, Any]] = field(default_factory=list)
    
    # Source information
    source_url: Optional[str] = None
    source_file: Optional[str] = None
    source_page: Optional[int] = None
    
    # Metadata
    language: Optional[str] = None
    word_count: int = 0
    char_count: int = 0
    confidence: float = 0.0
    
    # Processing info
    processing_time: float = 0.0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    
    # Timestamps
    created_at: datetime = field(default_factory=datetime.utcnow)
    
    # Additional metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "content_type": self.content_type.value,
            "extraction_method": self.extraction_method.value,
            "text": self.text,
            "tables": self.tables,
            "images": self.images,
            "source_url": self.source_url,
            "source_file": self.source_file,
            "source_page": self.source_page,
            "language": self.language,
            "word_count": self.word_count,
            "char_count": self.char_count,
            "confidence": self.confidence,
            "processing_time": self.processing_time,
            "errors": self.errors,
            "warnings": self.warnings,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }


@dataclass
class ProcessorConfig:
    """Configuration for multimodal processor."""
    # OCR settings
    ocr_language: str = "eng"
    ocr_dpi: int = 300
    ocr_psm: int = 3  # Page segmentation mode (3 = auto)
    
    # Image preprocessing
    preprocess_images: bool = True
    denoise: bool = True
    deskew: bool = True
    
    # Table extraction
    table_extraction_method: str = "pdfplumber"  # pdfplumber or camelot
    
    # PDF settings
    extract_images_from_pdf: bool = True
    max_image_size_mb: float = 10.0
    
    # Audio settings (if Whisper available)
    whisper_model: str = "base"
    
    # Performance
    max_concurrent: int = 4
    timeout: float = 300.0


class ImageProcessor:
    """
    Processes images for OCR and content extraction.
    
    Features:
    - Tesseract OCR with multiple language support
    - Image preprocessing (deskew, denoise, threshold)
    - Confidence scoring
    """
    
    def __init__(self, config: Optional[ProcessorConfig] = None):
        self.config = config or ProcessorConfig()
    
    def _preprocess_image(self, image: "Image.Image") -> "Image.Image":
        """
        Preprocess image for better OCR results.
        
        Applies:
        - Grayscale conversion
        - Denoising
        - Thresholding
        - Deskewing
        """
        if not HAS_CV2 or not self.config.preprocess_images:
            return image
        
        try:
            # Convert PIL to OpenCV format
            img_array = np.array(image)
            
            # Convert to grayscale if needed
            if len(img_array.shape) == 3:
                gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
            else:
                gray = img_array
            
            # Denoise
            if self.config.denoise:
                gray = cv2.fastNlMeansDenoising(gray, None, 10, 7, 21)
            
            # Adaptive thresholding
            thresh = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 11, 2
            )
            
            # Convert back to PIL
            return Image.fromarray(thresh)
            
        except Exception as e:
            logger.warning(f"Image preprocessing failed: {e}")
            return image
    
    async def extract_text(
        self,
        image: Union[bytes, "Image.Image", str, Path],
        language: Optional[str] = None,
    ) -> MultimodalSample:
        """
        Extract text from image using OCR.
        
        Args:
            image: Image as bytes, PIL Image, file path, or base64 string
            language: OCR language code (e.g., 'eng', 'fra', 'deu')
            
        Returns:
            MultimodalSample with extracted text
        """
        import time
        start_time = time.time()
        
        sample_id = hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]
        sample = MultimodalSample(
            id=sample_id,
            content_type=ContentType.IMAGE,
            extraction_method=ExtractionMethod.OCR,
        )
        
        if not HAS_TESSERACT:
            sample.errors.append("Tesseract not available")
            return sample
        
        try:
            # Load image
            if isinstance(image, bytes):
                pil_image = Image.open(io.BytesIO(image))
            elif isinstance(image, str):
                if image.startswith("data:image"):
                    # Base64 encoded
                    image_data = base64.b64decode(image.split(",")[1])
                    pil_image = Image.open(io.BytesIO(image_data))
                else:
                    # File path
                    pil_image = Image.open(image)
                    sample.source_file = str(image)
            elif isinstance(image, Path):
                pil_image = Image.open(image)
                sample.source_file = str(image)
            else:
                pil_image = image
            
            # Preprocess
            processed_image = self._preprocess_image(pil_image)
            
            # OCR with Tesseract
            lang = language or self.config.ocr_language
            
            # Run OCR in executor (blocking call)
            loop = asyncio.get_event_loop()
            ocr_result = await loop.run_in_executor(
                None,
                lambda: pytesseract.image_to_data(
                    processed_image,
                    lang=lang,
                    output_type=pytesseract.Output.DICT,
                    config=f"--psm {self.config.ocr_psm}"
                )
            )
            
            # Extract text and calculate confidence
            texts = []
            confidences = []
            
            for i, conf in enumerate(ocr_result.get("conf", [])):
                if conf > 0:  # Valid confidence
                    text = ocr_result["text"][i].strip()
                    if text:
                        texts.append(text)
                        confidences.append(conf)
            
            sample.text = " ".join(texts)
            sample.word_count = len(texts)
            sample.char_count = len(sample.text)
            sample.language = lang
            
            if confidences:
                sample.confidence = sum(confidences) / len(confidences) / 100
            
            # Store image metadata
            sample.metadata["image_size"] = pil_image.size
            sample.metadata["image_mode"] = pil_image.mode
            
        except Exception as e:
            logger.error(f"OCR extraction failed: {e}")
            sample.errors.append(str(e))
        
        sample.processing_time = time.time() - start_time
        return sample


class PDFProcessor:
    """
    Processes PDF documents for text and table extraction.
    
    Features:
    - Text extraction with PyMuPDF
    - Table extraction with pdfplumber
    - Embedded image extraction
    - OCR for scanned PDFs
    """
    
    def __init__(
        self,
        config: Optional[ProcessorConfig] = None,
        image_processor: Optional[ImageProcessor] = None,
    ):
        self.config = config or ProcessorConfig()
        self.image_processor = image_processor or ImageProcessor(self.config)
    
    async def extract_text(
        self,
        pdf_source: Union[bytes, str, Path, BinaryIO],
        pages: Optional[List[int]] = None,
    ) -> List[MultimodalSample]:
        """
        Extract text from PDF.
        
        Args:
            pdf_source: PDF as bytes, file path, or file object
            pages: Optional list of page numbers to process (0-indexed)
            
        Returns:
            List of MultimodalSample, one per page
        """
        import time
        start_time = time.time()
        
        samples = []
        
        if not HAS_PYMUPDF:
            sample = MultimodalSample(
                id="error",
                content_type=ContentType.PDF,
                extraction_method=ExtractionMethod.DIRECT,
            )
            sample.errors.append("PyMuPDF not available")
            return [sample]
        
        try:
            # Open PDF
            if isinstance(pdf_source, bytes):
                doc = fitz.open(stream=pdf_source, filetype="pdf")
            elif isinstance(pdf_source, (str, Path)):
                doc = fitz.open(pdf_source)
            else:
                content = pdf_source.read()
                doc = fitz.open(stream=content, filetype="pdf")
            
            # Process pages
            page_nums = pages or range(len(doc))
            
            for page_num in page_nums:
                if page_num >= len(doc):
                    continue
                
                page = doc[page_num]
                
                sample = MultimodalSample(
                    id=f"pdf_page_{page_num}_{hashlib.sha256(str(time.time()).encode()).hexdigest()[:8]}",
                    content_type=ContentType.PDF,
                    extraction_method=ExtractionMethod.DIRECT,
                    source_page=page_num,
                )
                
                # Extract text
                text = page.get_text()
                
                # If no text, try OCR
                if not text.strip():
                    sample.extraction_method = ExtractionMethod.OCR
                    sample.warnings.append("No direct text found, using OCR")
                    
                    # Render page as image for OCR
                    pix = page.get_pixmap(dpi=self.config.ocr_dpi)
                    img_data = pix.tobytes("png")
                    
                    ocr_sample = await self.image_processor.extract_text(img_data)
                    text = ocr_sample.text
                    sample.confidence = ocr_sample.confidence
                
                sample.text = text
                sample.word_count = len(text.split())
                sample.char_count = len(text)
                
                # Extract images if configured
                if self.config.extract_images_from_pdf:
                    images = await self._extract_images_from_page(page, page_num)
                    sample.images = images
                
                sample.metadata["page_number"] = page_num + 1
                sample.metadata["page_count"] = len(doc)
                
                samples.append(sample)
            
            doc.close()
            
        except Exception as e:
            logger.error(f"PDF extraction failed: {e}")
            sample = MultimodalSample(
                id="error",
                content_type=ContentType.PDF,
                extraction_method=ExtractionMethod.DIRECT,
            )
            sample.errors.append(str(e))
            samples.append(sample)
        
        # Set processing time on first sample
        if samples:
            samples[0].processing_time = time.time() - start_time
        
        return samples
    
    async def _extract_images_from_page(
        self,
        page: "fitz.Page",
        page_num: int,
    ) -> List[Dict[str, Any]]:
        """Extract images from a PDF page."""
        images = []
        
        try:
            image_list = page.get_images(full=True)
            
            for img_index, img_info in enumerate(image_list):
                xref = img_info[0]
                
                try:
                    base_image = page.parent.extract_image(xref)
                    
                    if base_image:
                        # Check size limit
                        size_mb = len(base_image["image"]) / (1024 * 1024)
                        if size_mb > self.config.max_image_size_mb:
                            continue
                        
                        images.append({
                            "index": img_index,
                            "page": page_num,
                            "width": base_image.get("width", 0),
                            "height": base_image.get("height", 0),
                            "colorspace": base_image.get("colorspace", ""),
                            "size_bytes": len(base_image["image"]),
                            "format": base_image.get("ext", "unknown"),
                        })
                        
                except Exception as e:
                    logger.debug(f"Could not extract image {img_index}: {e}")
                    
        except Exception as e:
            logger.warning(f"Image extraction from page failed: {e}")
        
        return images
    
    async def extract_tables(
        self,
        pdf_source: Union[bytes, str, Path],
        pages: Optional[List[int]] = None,
    ) -> List[MultimodalSample]:
        """
        Extract tables from PDF.
        
        Args:
            pdf_source: PDF as bytes or file path
            pages: Optional list of pages to process (1-indexed for pdfplumber)
            
        Returns:
            List of MultimodalSample with extracted tables
        """
        import time
        start_time = time.time()
        
        samples = []
        
        if not HAS_PDFPLUMBER:
            sample = MultimodalSample(
                id="error",
                content_type=ContentType.TABLE,
                extraction_method=ExtractionMethod.TABLE_EXTRACTION,
            )
            sample.errors.append("pdfplumber not available")
            return [sample]
        
        try:
            # Open with pdfplumber
            if isinstance(pdf_source, bytes):
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                    f.write(pdf_source)
                    temp_path = f.name
                pdf = pdfplumber.open(temp_path)
            else:
                pdf = pdfplumber.open(pdf_source)
            
            # Process pages
            page_nums = pages or range(len(pdf.pages))
            
            for page_num in page_nums:
                if page_num >= len(pdf.pages):
                    continue
                
                page = pdf.pages[page_num]
                tables = page.extract_tables()
                
                if tables:
                    sample = MultimodalSample(
                        id=f"table_page_{page_num}_{hashlib.sha256(str(time.time()).encode()).hexdigest()[:8]}",
                        content_type=ContentType.TABLE,
                        extraction_method=ExtractionMethod.TABLE_EXTRACTION,
                        source_page=page_num,
                    )
                    
                    # Convert tables to structured format
                    structured_tables = []
                    for table_idx, table in enumerate(tables):
                        if table and len(table) > 0:
                            # First row as headers
                            headers = table[0] if table else []
                            rows = table[1:] if len(table) > 1 else []
                            
                            structured_tables.append({
                                "index": table_idx,
                                "headers": headers,
                                "rows": rows,
                                "row_count": len(rows),
                                "col_count": len(headers) if headers else 0,
                            })
                            
                            # Also add as text
                            table_text = self._table_to_text(headers, rows)
                            sample.text += table_text + "\n\n"
                    
                    sample.tables = structured_tables
                    sample.word_count = len(sample.text.split())
                    sample.char_count = len(sample.text)
                    sample.metadata["table_count"] = len(structured_tables)
                    sample.metadata["page_number"] = page_num + 1
                    
                    samples.append(sample)
            
            pdf.close()
            
        except Exception as e:
            logger.error(f"Table extraction failed: {e}")
            sample = MultimodalSample(
                id="error",
                content_type=ContentType.TABLE,
                extraction_method=ExtractionMethod.TABLE_EXTRACTION,
            )
            sample.errors.append(str(e))
            samples.append(sample)
        
        if samples:
            samples[0].processing_time = time.time() - start_time
        
        return samples
    
    def _table_to_text(self, headers: List, rows: List) -> str:
        """Convert table to text format."""
        lines = []
        
        if headers:
            lines.append(" | ".join(str(h or "") for h in headers))
            lines.append("-" * 50)
        
        for row in rows:
            lines.append(" | ".join(str(cell or "") for cell in row))
        
        return "\n".join(lines)


class MultimodalProcessor:
    """
    Unified multimodal content processor.
    
    Handles extraction from:
    - Images (OCR)
    - PDFs (text, tables, embedded images)
    - Audio (transcription with Whisper - optional)
    - Web pages (integration with scraper)
    
    All content is normalized to MultimodalSample format.
    """
    
    def __init__(self, config: Optional[ProcessorConfig] = None):
        self.config = config or ProcessorConfig()
        
        self.image_processor = ImageProcessor(self.config)
        self.pdf_processor = PDFProcessor(self.config, self.image_processor)
        
        self._initialized = False
    
    async def initialize(self):
        """Initialize processor."""
        if self._initialized:
            return
        
        # Log available capabilities
        capabilities = []
        if HAS_TESSERACT:
            capabilities.append("OCR")
        if HAS_PYMUPDF:
            capabilities.append("PDF text extraction")
        if HAS_PDFPLUMBER:
            capabilities.append("Table extraction")
        if HAS_CV2:
            capabilities.append("Image preprocessing")
        
        logger.info(f"MultimodalProcessor initialized with: {', '.join(capabilities)}")
        self._initialized = True
    
    def detect_content_type(
        self,
        source: Union[bytes, str, Path],
    ) -> ContentType:
        """
        Detect content type from source.
        
        Args:
            source: Content source
            
        Returns:
            Detected ContentType
        """
        if isinstance(source, (str, Path)):
            source_str = str(source).lower()
            
            # Check file extension
            if source_str.endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff', '.webp')):
                return ContentType.IMAGE
            elif source_str.endswith('.pdf'):
                return ContentType.PDF
            elif source_str.endswith(('.mp3', '.wav', '.m4a', '.flac', '.ogg')):
                return ContentType.AUDIO
            elif source_str.endswith(('.mp4', '.avi', '.mov', '.mkv', '.webm')):
                return ContentType.VIDEO
            elif source_str.endswith(('.txt', '.md', '.html', '.htm', '.xml', '.json')):
                return ContentType.TEXT
        
        elif isinstance(source, bytes):
            # Check magic bytes
            if source[:4] == b'%PDF':
                return ContentType.PDF
            elif source[:3] == b'ID3' or source[:4] == b'fLaC':
                return ContentType.AUDIO
            elif source[:8] in [b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff\xe0']:
                return ContentType.IMAGE
        
        return ContentType.UNKNOWN
    
    async def process(
        self,
        source: Union[bytes, str, Path],
        content_type: Optional[ContentType] = None,
        **kwargs,
    ) -> List[MultimodalSample]:
        """
        Process content from any supported source.
        
        Args:
            source: Content source (bytes, file path, URL)
            content_type: Optional content type (auto-detected if not provided)
            **kwargs: Additional arguments for specific processors
            
        Returns:
            List of MultimodalSample
        """
        if not self._initialized:
            await self.initialize()
        
        # Detect content type
        detected_type = content_type or self.detect_content_type(source)
        
        # Route to appropriate processor
        if detected_type == ContentType.IMAGE:
            sample = await self.image_processor.extract_text(source, **kwargs)
            return [sample]
        
        elif detected_type == ContentType.PDF:
            # Extract both text and tables
            text_samples = await self.pdf_processor.extract_text(source, **kwargs)
            table_samples = await self.pdf_processor.extract_tables(source, **kwargs)
            
            # Merge samples by page
            merged = {}
            for sample in text_samples + table_samples:
                page = sample.source_page or 0
                if page not in merged:
                    merged[page] = sample
                else:
                    # Merge tables into text sample
                    merged[page].tables.extend(sample.tables)
                    if sample.text and sample.text not in merged[page].text:
                        merged[page].text += "\n\n" + sample.text
            
            return list(merged.values())
        
        elif detected_type == ContentType.TEXT:
            # Direct text content
            if isinstance(source, bytes):
                text = source.decode('utf-8', errors='ignore')
            elif isinstance(source, (str, Path)):
                with open(source, 'r', encoding='utf-8', errors='ignore') as f:
                    text = f.read()
            else:
                text = str(source)
            
            sample = MultimodalSample(
                id=hashlib.sha256(text[:1000].encode()).hexdigest()[:16],
                content_type=ContentType.TEXT,
                extraction_method=ExtractionMethod.DIRECT,
                text=text,
                word_count=len(text.split()),
                char_count=len(text),
            )
            return [sample]
        
        else:
            sample = MultimodalSample(
                id="unknown",
                content_type=detected_type,
                extraction_method=ExtractionMethod.DIRECT,
            )
            sample.errors.append(f"Unsupported content type: {detected_type}")
            return [sample]
    
    async def process_batch(
        self,
        sources: List[Union[bytes, str, Path]],
        max_concurrent: Optional[int] = None,
    ) -> List[List[MultimodalSample]]:
        """
        Process multiple sources concurrently.
        
        Args:
            sources: List of content sources
            max_concurrent: Max concurrent processing (default from config)
            
        Returns:
            List of sample lists, one per source
        """
        max_concurrent = max_concurrent or self.config.max_concurrent
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def process_with_limit(source):
            async with semaphore:
                return await self.process(source)
        
        tasks = [process_with_limit(source) for source in sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Handle exceptions
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Processing failed for source {i}: {result}")
                sample = MultimodalSample(
                    id=f"error_{i}",
                    content_type=ContentType.UNKNOWN,
                    extraction_method=ExtractionMethod.DIRECT,
                )
                sample.errors.append(str(result))
                processed_results.append([sample])
            else:
                processed_results.append(result)
        
        return processed_results
    
    def get_capabilities(self) -> Dict[str, bool]:
        """Get available processing capabilities."""
        return {
            "ocr": HAS_TESSERACT,
            "pdf_text": HAS_PYMUPDF,
            "pdf_tables": HAS_PDFPLUMBER,
            "image_preprocessing": HAS_CV2,
        }
    
    async def close(self):
        """Cleanup resources."""
        self._initialized = False
        logger.info("MultimodalProcessor closed")
    
    async def __aenter__(self):
        await self.initialize()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
