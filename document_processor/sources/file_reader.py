"""
File reader for various formats (CSV, JSON, XML, Excel, Word).
Supports streaming for large files.
"""

import json
import csv
import asyncio
from typing import AsyncIterator, Dict, Any
from pathlib import Path
import xml.etree.ElementTree as ET
import pandas as pd
from docx import Document as DocxDocument
from .base import BaseSourceProcessor
from ..core.models import SourceDocument, SourceType
from ..core.exceptions import FileReadError
from ..config.logging_config import logger


class FileReader(BaseSourceProcessor):
    """File reader for local files in various formats."""

    async def can_process(self, source: SourceDocument) -> bool:
        """Check if this is a file source."""
        return source.source_type == SourceType.FILE

    async def extract_content(self, source: SourceDocument) -> AsyncIterator[str]:
        """Extract content from file based on format."""
        if not source.source_path:
            raise FileReadError("No path provided for file source")

        path = Path(source.source_path)
        if not path.exists():
            raise FileReadError(f"File not found: {path}")

        # Determine file type
        suffix = path.suffix.lower()

        if suffix == ".csv":
            async for chunk in self._read_csv(path):
                yield chunk
        elif suffix == ".json":
            async for chunk in self._read_json(path):
                yield chunk
        elif suffix == ".xml":
            async for chunk in self._read_xml(path):
                yield chunk
        elif suffix in [".xlsx", ".xls"]:
            async for chunk in self._read_excel(path):
                yield chunk
        elif suffix == ".docx":
            async for chunk in self._read_docx(path):
                yield chunk
        elif suffix == ".txt":
            async for chunk in self._read_text(path):
                yield chunk
        else:
            raise FileReadError(f"Unsupported file format: {suffix}")

    async def _read_csv(self, path: Path) -> AsyncIterator[str]:
        """Read CSV file."""
        loop = asyncio.get_event_loop()

        def read():
            rows = []
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(json.dumps(row))
            return rows

        rows = await loop.run_in_executor(None, read)
        for row in rows:
            yield row

    async def _read_json(self, path: Path) -> AsyncIterator[str]:
        """Read JSON file."""
        loop = asyncio.get_event_loop()

        def read():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data

        data = await loop.run_in_executor(None, read)

        if isinstance(data, list):
            for item in data:
                yield json.dumps(item)
        else:
            yield json.dumps(data)

    async def _read_xml(self, path: Path) -> AsyncIterator[str]:
        """Read XML file."""
        loop = asyncio.get_event_loop()

        def read():
            tree = ET.parse(path)
            root = tree.getroot()
            items = []
            for elem in root.iter():
                if elem.text and elem.text.strip():
                    items.append(elem.text.strip())
            return items

        items = await loop.run_in_executor(None, read)
        for item in items:
            yield item

    async def _read_excel(self, path: Path) -> AsyncIterator[str]:
        """Read Excel file."""
        loop = asyncio.get_event_loop()

        def read():
            df = pd.read_excel(path)
            return df.to_dict("records")

        records = await loop.run_in_executor(None, read)
        for record in records:
            yield json.dumps(record)

    async def _read_docx(self, path: Path) -> AsyncIterator[str]:
        """Read Word document."""
        loop = asyncio.get_event_loop()

        def read():
            doc = DocxDocument(path)
            return [para.text for para in doc.paragraphs if para.text.strip()]

        paragraphs = await loop.run_in_executor(None, read)
        for para in paragraphs:
            yield para

    async def _read_text(self, path: Path) -> AsyncIterator[str]:
        """Read text file."""
        loop = asyncio.get_event_loop()

        def read():
            with open(path, "r", encoding="utf-8") as f:
                return f.read()

        content = await loop.run_in_executor(None, read)
        # Split into chunks
        chunk_size = self.settings.chunk_size_bytes
        for i in range(0, len(content), chunk_size):
            yield content[i:i + chunk_size]

    async def get_metadata(self, source: SourceDocument) -> Dict[str, Any]:
        """Get file metadata."""
        path = Path(source.source_path)
        return {
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "format": path.suffix,
        }
