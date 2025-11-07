"""
API client for REST and GraphQL endpoints.
Supports pagination and authentication.
"""

import asyncio
import aiohttp
from typing import AsyncIterator, Dict, Any
from .base import BaseSourceProcessor
from ..core.models import SourceDocument, SourceType
from ..core.exceptions import APIClientError
from ..config.logging_config import logger


class APIClient(BaseSourceProcessor):
    """API client for REST and GraphQL sources."""

    def __init__(self, settings):
        """Initialize API client."""
        super().__init__(settings)
        self.session: aiohttp.ClientSession = None

    async def can_process(self, source: SourceDocument) -> bool:
        """Check if this is an API source."""
        return source.source_type == SourceType.API

    async def extract_content(self, source: SourceDocument) -> AsyncIterator[str]:
        """Extract data from API."""
        if not source.source_url:
            raise APIClientError("No URL provided for API source")

        if not self.session:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.settings.api_timeout)
            )

        api_type = source.metadata.get("api_type", "rest")

        if api_type == "graphql":
            async for item in self._query_graphql(source):
                yield item
        else:
            async for item in self._query_rest(source):
                yield item

    async def _query_rest(self, source: SourceDocument) -> AsyncIterator[str]:
        """Query REST API with pagination."""
        url = source.source_url
        headers = source.metadata.get("headers", {})
        params = source.metadata.get("params", {})

        page = 1
        while True:
            params["page"] = page

            async with self.session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    raise APIClientError(f"API error: HTTP {resp.status}")

                data = await resp.json()

                # Handle different response formats
                items = data if isinstance(data, list) else data.get("items", data.get("data", []))

                if not items:
                    break

                for item in items:
                    yield str(item)

                # Check if there are more pages
                if not data.get("has_more", False):
                    break

                page += 1

    async def _query_graphql(self, source: SourceDocument) -> AsyncIterator[str]:
        """Query GraphQL API."""
        url = source.source_url
        headers = source.metadata.get("headers", {"Content-Type": "application/json"})
        query = source.metadata.get("query")

        if not query:
            raise APIClientError("No GraphQL query provided")

        async with self.session.post(url, json={"query": query}, headers=headers) as resp:
            if resp.status != 200:
                raise APIClientError(f"GraphQL error: HTTP {resp.status}")

            data = await resp.json()

            if "errors" in data:
                raise APIClientError(f"GraphQL errors: {data['errors']}")

            # Extract data
            result = data.get("data", {})
            for key, value in result.items():
                if isinstance(value, list):
                    for item in value:
                        yield str(item)
                else:
                    yield str(value)

    async def get_metadata(self, source: SourceDocument) -> Dict[str, Any]:
        """Get API metadata."""
        return {
            "api_type": source.metadata.get("api_type", "rest"),
            "url": source.source_url,
        }

    async def cleanup(self):
        """Cleanup HTTP session."""
        if self.session:
            await self.session.close()
