"""
Database connectors for SQL and NoSQL databases.
Supports PostgreSQL, MySQL, and MongoDB.
"""

import json
import asyncio
from typing import AsyncIterator, Dict, Any
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from motor.motor_asyncio import AsyncIOMotorClient
from .base import BaseSourceProcessor
from ..core.models import SourceDocument, SourceType
from ..core.exceptions import DatabaseConnectionError
from ..config.logging_config import logger


class DatabaseConnector(BaseSourceProcessor):
    """Database connector for SQL and NoSQL sources."""

    def __init__(self, settings):
        """Initialize database connector."""
        super().__init__(settings)
        self.pg_engine = None
        self.mongo_client = None

    async def can_process(self, source: SourceDocument) -> bool:
        """Check if this is a database source."""
        return source.source_type in [SourceType.SQL, SourceType.NOSQL]

    async def extract_content(self, source: SourceDocument) -> AsyncIterator[str]:
        """Extract data from database."""
        if source.source_type == SourceType.SQL:
            async for record in self._stream_sql(source):
                yield record
        else:
            async for record in self._stream_nosql(source):
                yield record

    async def _stream_sql(self, source: SourceDocument) -> AsyncIterator[str]:
        """Stream SQL results."""
        query = source.metadata.get("query")
        if not query:
            raise DatabaseConnectionError("No query provided for SQL source")

        if not self.pg_engine:
            self.pg_engine = create_async_engine(self.settings.postgres_url)

        async with self.pg_engine.connect() as conn:
            result = await conn.stream(text(query))

            async for partition in result.partitions(1000):
                for row in partition:
                    yield json.dumps(dict(row._mapping))

    async def _stream_nosql(self, source: SourceDocument) -> AsyncIterator[str]:
        """Stream MongoDB documents."""
        db_name = source.metadata.get("database", self.settings.mongo_database)
        collection_name = source.metadata.get("collection")
        query = source.metadata.get("query", {})

        if not collection_name:
            raise DatabaseConnectionError("No collection specified for MongoDB source")

        if not self.mongo_client:
            self.mongo_client = AsyncIOMotorClient(self.settings.mongo_url)

        db = self.mongo_client[db_name]
        collection = db[collection_name]

        cursor = collection.find(query).batch_size(1000)

        async for document in cursor:
            document["_id"] = str(document["_id"])
            yield json.dumps(document)

    async def get_metadata(self, source: SourceDocument) -> Dict[str, Any]:
        """Get database metadata."""
        return {
            "database_type": source.source_type,
            "query": source.metadata.get("query", ""),
        }

    async def cleanup(self):
        """Cleanup database connections."""
        if self.pg_engine:
            await self.pg_engine.dispose()
        if self.mongo_client:
            self.mongo_client.close()
