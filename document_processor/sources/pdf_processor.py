"""
PDF processor with OCR support for scanned documents.
Uses PyMuPDF for native text and Tesseract for OCR.
"""

import asyncio
import fitz  # PyMuPDF
import pytesseract
from pdf2image import convert_from_path
from typing import AsyncIterator, Dict, Any
from pathlib import Path
from .base import BaseSourceProcessor
from ..core.models import SourceDocument, SourceType
from ..core.exceptions import PDFProcessingError
from ..config.logging_config import logger
from ..infrastructure.monitoring import monitor


class PDFProcessor(BaseSourceProcessor):
    """PDF processor supporting native text and OCR."""

    async def can_process(self, source: SourceDocument) -> bool:
        """Check if this is a PDF source."""
        return source.source_type == SourceType.PDF

    async def extract_content(self, source: SourceDocument) -> AsyncIterator[str]:
        """Extract text from PDF."""
        if not source.source_path:
            raise PDFProcessingError("No path provided for PDF source")

        path = Path(source.source_path)
        if not path.exists():
            raise PDFProcessingError(f"PDF file not found: {path}")

        # Try native extraction first
        try:
            async for chunk in self._extract_native(str(path)):
                yield chunk
        except Exception as e:
            logger.warning(f"Native extraction failed, trying OCR: {e}")
            # Fallback to OCR
            async for chunk in self._extract_ocr(str(path)):
                yield chunk

    async def _extract_native(self, path: str) -> AsyncIterator[str]:
        """Extract text natively from PDF."""
        loop = asyncio.get_event_loop()

        def extract_page(page_num: int) -> str:
            doc = fitz.open(path)
            page = doc[page_num]
            text = page.get_text()
            doc.close()
            return text

        doc = fitz.open(path)
        num_pages = len(doc)
        doc.close()

        async with monitor.track_extraction_duration("pdf"):
            for page_num in range(num_pages):
                text = await loop.run_in_executor(None, extract_page, page_num)
                if text.strip():
                    yield text

    async def _extract_ocr(self, path: str) -> AsyncIterator[str]:
        """Extract text using OCR."""
        loop = asyncio.get_event_loop()

        def ocr_page(image):
            return pytesseract.image_to_string(
                image,
                lang=self.settings.pdf_ocr_languages
            )

        images = await loop.run_in_executor(None, convert_from_path, path)

        for image in images:
            text = await loop.run_in_executor(None, ocr_page, image)
            if text.strip():
                yield text

    async def get_metadata(self, source: SourceDocument) -> Dict[str, Any]:
        """Extract PDF metadata."""
        try:
            doc = fitz.open(source.source_path)
            metadata = {
                "pages": len(doc),
                "title": doc.metadata.get("title"),
                "author": doc.metadata.get("author"),
                "subject": doc.metadata.get("subject"),
            }
            doc.close()
            return metadata
        except Exception as e:
            logger.error(f"Failed to get PDF metadata: {e}")
            return {}
