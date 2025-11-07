"""
Storage layer for persisting processed documents.
Supports PostgreSQL for metadata and MongoDB for document content.
"""

import json
from typing import Optional, List, Dict, Any
from datetime import datetime
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import Column, String, Integer, Float, DateTime, JSON, Text, Boolean
from sqlalchemy.orm import declarative_base
from motor.motor_asyncio import AsyncIOMotorClient
from ..config.settings import settings
from ..config.logging_config import logger
from ..core.models import TranslatedDocument, ProcessingStatus
from ..core.exceptions import StorageError


# SQLAlchemy Base
Base = declarative_base()


class TranslatedDocumentModel(Base):
    """SQLAlchemy model for translated documents metadata."""

    __tablename__ = "translated_documents"

    id = Column(String(36), primary_key=True)
    source_id = Column(String(36), index=True, nullable=False)
    original_language_code = Column(String(10), index=True)
    original_language_confidence = Column(Float)
    original_text_length = Column(Integer)
    translated_text_length = Column(Integer)
    translation_provider = Column(String(20), index=True)
    translation_quality_score = Column(Float)
    processing_time_ms = Column(Float)
    cached = Column(Boolean, default=False)
    status = Column(String(20), index=True)
    error = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0)
    metadata = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at = Column(DateTime, nullable=True)


class StorageManager:
    """
    Storage manager for persisting documents.

    Uses PostgreSQL for metadata and MongoDB for full document content.
    """

    def __init__(self):
        """Initialize storage manager."""
        self.pg_engine = None
        self.pg_session_maker = None
        self.mongo_client = None
        self.mongo_db = None
        self._pg_connected = False
        self._mongo_connected = False

    async def connect_postgres(self):
        """Connect to PostgreSQL."""
        if self._pg_connected:
            return

        try:
            self.pg_engine = create_async_engine(
                settings.postgres_url,
                pool_size=settings.postgres_pool_size,
                max_overflow=settings.postgres_max_overflow,
                pool_pre_ping=True,
                echo=settings.debug,
            )

            # Create session maker
            self.pg_session_maker = async_sessionmaker(
                self.pg_engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )

            # Create tables
            async with self.pg_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            self._pg_connected = True

            logger.info(
                "postgres_connected",
                host=settings.postgres_host,
                database=settings.postgres_database,
            )

        except Exception as e:
            logger.error("postgres_connection_failed", error=str(e))
            raise StorageError(f"Failed to connect to PostgreSQL: {e}")

    async def connect_mongo(self):
        """Connect to MongoDB."""
        if self._mongo_connected:
            return

        try:
            self.mongo_client = AsyncIOMotorClient(
                settings.mongo_url,
                maxPoolSize=settings.mongo_max_pool_size,
            )

            self.mongo_db = self.mongo_client[settings.mongo_database]

            # Test connection
            await self.mongo_db.command("ping")

            self._mongo_connected = True

            logger.info(
                "mongodb_connected",
                host=settings.mongo_host,
                database=settings.mongo_database,
            )

        except Exception as e:
            logger.error("mongodb_connection_failed", error=str(e))
            raise StorageError(f"Failed to connect to MongoDB: {e}")

    async def disconnect(self):
        """Disconnect from databases."""
        if self.pg_engine and self._pg_connected:
            await self.pg_engine.dispose()
            self._pg_connected = False
            logger.info("postgres_disconnected")

        if self.mongo_client and self._mongo_connected:
            self.mongo_client.close()
            self._mongo_connected = False
            logger.info("mongodb_disconnected")

    async def save_document(self, document: TranslatedDocument) -> bool:
        """
        Save translated document to storage.

        Args:
            document: Translated document to save

        Returns:
            True if successful
        """
        try:
            # Save metadata to PostgreSQL
            await self._save_to_postgres(document)

            # Save full content to MongoDB
            await self._save_to_mongo(document)

            logger.debug("document_saved", document_id=document.id)
            return True

        except Exception as e:
            logger.error(
                "document_save_failed",
                document_id=document.id,
                error=str(e),
            )
            return False

    async def _save_to_postgres(self, document: TranslatedDocument):
        """Save document metadata to PostgreSQL."""
        if not self._pg_connected:
            await self.connect_postgres()

        async with self.pg_session_maker() as session:
            model = TranslatedDocumentModel(
                id=document.id,
                source_id=document.source_id,
                original_language_code=document.original_language.code,
                original_language_confidence=document.original_language.confidence,
                original_text_length=document.original_text_length,
                translated_text_length=document.translated_text_length,
                translation_provider=document.translation_provider,
                translation_quality_score=document.translation_quality_score,
                processing_time_ms=document.processing_time_ms,
                cached=document.cached,
                status=document.status,
                error=document.error,
                retry_count=document.retry_count,
                metadata=document.metadata,
                created_at=document.created_at,
                completed_at=document.completed_at,
            )

            session.add(model)
            await session.commit()

    async def _save_to_mongo(self, document: TranslatedDocument):
        """Save full document to MongoDB."""
        if not self._mongo_connected:
            await self.connect_mongo()

        collection = self.mongo_db["documents"]

        doc_dict = document.model_dump()

        # Convert datetime to ISO format
        doc_dict["created_at"] = document.created_at.isoformat()
        if document.completed_at:
            doc_dict["completed_at"] = document.completed_at.isoformat()

        await collection.replace_one(
            {"id": document.id},
            doc_dict,
            upsert=True,
        )

    async def get_document(self, document_id: str) -> Optional[TranslatedDocument]:
        """
        Get document by ID from MongoDB.

        Args:
            document_id: Document ID

        Returns:
            TranslatedDocument or None
        """
        if not self._mongo_connected:
            await self.connect_mongo()

        try:
            collection = self.mongo_db["documents"]
            doc_dict = await collection.find_one({"id": document_id})

            if doc_dict:
                # Remove MongoDB _id field
                doc_dict.pop("_id", None)
                return TranslatedDocument(**doc_dict)

            return None

        except Exception as e:
            logger.error(
                "document_get_failed",
                document_id=document_id,
                error=str(e),
            )
            return None

    async def query_documents(
        self,
        filters: Dict[str, Any],
        limit: int = 100,
        skip: int = 0,
    ) -> List[TranslatedDocument]:
        """
        Query documents from MongoDB.

        Args:
            filters: Query filters
            limit: Maximum documents to return
            skip: Number of documents to skip

        Returns:
            List of documents
        """
        if not self._mongo_connected:
            await self.connect_mongo()

        try:
            collection = self.mongo_db["documents"]
            cursor = collection.find(filters).skip(skip).limit(limit)

            documents = []
            async for doc_dict in cursor:
                doc_dict.pop("_id", None)
                documents.append(TranslatedDocument(**doc_dict))

            return documents

        except Exception as e:
            logger.error("document_query_failed", error=str(e))
            return []

    async def get_statistics(self) -> Dict[str, Any]:
        """
        Get storage statistics.

        Returns:
            Dictionary with statistics
        """
        stats = {}

        # PostgreSQL stats
        if self._pg_connected:
            try:
                async with self.pg_session_maker() as session:
                    from sqlalchemy import select, func

                    # Total documents
                    result = await session.execute(
                        select(func.count(TranslatedDocumentModel.id))
                    )
                    stats["total_documents"] = result.scalar()

                    # By status
                    result = await session.execute(
                        select(
                            TranslatedDocumentModel.status,
                            func.count(TranslatedDocumentModel.id)
                        ).group_by(TranslatedDocumentModel.status)
                    )
                    stats["by_status"] = {row[0]: row[1] for row in result}

                    # By provider
                    result = await session.execute(
                        select(
                            TranslatedDocumentModel.translation_provider,
                            func.count(TranslatedDocumentModel.id)
                        ).group_by(TranslatedDocumentModel.translation_provider)
                    )
                    stats["by_provider"] = {row[0]: row[1] for row in result}

            except Exception as e:
                logger.error("postgres_stats_failed", error=str(e))

        # MongoDB stats
        if self._mongo_connected:
            try:
                collection = self.mongo_db["documents"]
                stats["mongodb_documents"] = await collection.count_documents({})
            except Exception as e:
                logger.error("mongodb_stats_failed", error=str(e))

        return stats

    async def health_check(self) -> Dict[str, bool]:
        """
        Check storage health.

        Returns:
            Dictionary with health status
        """
        health = {}

        # PostgreSQL health
        try:
            if not self._pg_connected:
                await self.connect_postgres()

            async with self.pg_engine.connect() as conn:
                await conn.execute("SELECT 1")
            health["postgres"] = True
        except Exception as e:
            logger.error("postgres_health_check_failed", error=str(e))
            health["postgres"] = False

        # MongoDB health
        try:
            if not self._mongo_connected:
                await self.connect_mongo()

            await self.mongo_db.command("ping")
            health["mongodb"] = True
        except Exception as e:
            logger.error("mongodb_health_check_failed", error=str(e))
            health["mongodb"] = False

        return health


# Global storage manager instance
storage_manager = StorageManager()
