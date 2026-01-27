"""
Ollama LLM Client
Optimized for local inference with VRAM management
"""

import asyncio
import logging
from typing import Optional, Dict, Any, AsyncGenerator
import httpx
from ollama import AsyncClient

logger = logging.getLogger(__name__)


class OllamaClient:
    """
    Async Ollama client with VRAM management and error handling.
    Optimized for single-model operation on 8GB VRAM.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2.5:7b",
        keep_alive: str = "5m",
        timeout: int = 300,
    ):
        """
        Initialize Ollama client.

        Args:
            base_url: Ollama API endpoint
            model: Model name (e.g., 'qwen2.5:7b')
            keep_alive: How long to keep model loaded ('5m', '0' for immediate unload)
            timeout: Request timeout in seconds
        """
        self.base_url = base_url
        self.model = model
        self.keep_alive = keep_alive
        self.timeout = timeout
        self.client = AsyncClient(host=base_url, timeout=timeout)

    async def check_health(self) -> bool:
        """Check if Ollama service is healthy."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{self.base_url}/api/tags", timeout=5.0)
                return response.status_code == 200
        except Exception as e:
            logger.error(f"Ollama health check failed: {e}")
            return False

    async def list_models(self) -> list[str]:
        """List available models."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{self.base_url}/api/tags")
                data = response.json()
                return [model["name"] for model in data.get("models", [])]
        except Exception as e:
            logger.error(f"Failed to list models: {e}")
            return []

    async def pull_model(self, model_name: str) -> bool:
        """
        Pull a model from Ollama library.

        Args:
            model_name: Model to pull (e.g., 'qwen2.5:7b')

        Returns:
            True if successful
        """
        try:
            logger.info(f"Pulling model {model_name}...")
            response = await self.client.pull(model_name)
            logger.info(f"Model {model_name} pulled successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to pull model {model_name}: {e}")
            return False

    async def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stream: bool = False,
    ) -> str | AsyncGenerator[str, None]:
        """
        Generate text completion.

        Args:
            prompt: User prompt
            system: System message
            temperature: Sampling temperature (0-2)
            max_tokens: Maximum tokens to generate
            stream: Return streaming generator

        Returns:
            Generated text or async generator
        """
        try:
            options = {
                "temperature": temperature,
                "num_predict": max_tokens or -1,
            }

            if stream:
                return self._stream_generate(prompt, system, options)

            response = await self.client.generate(
                model=self.model,
                prompt=prompt,
                system=system,
                options=options,
                keep_alive=self.keep_alive,
            )

            return response.get("response", "")

        except Exception as e:
            logger.error(f"Generation failed: {e}")
            raise

    async def _stream_generate(
        self, prompt: str, system: Optional[str], options: Dict[str, Any]
    ) -> AsyncGenerator[str, None]:
        """Internal streaming generator."""
        try:
            stream = await self.client.generate(
                model=self.model,
                prompt=prompt,
                system=system,
                options=options,
                stream=True,
                keep_alive=self.keep_alive,
            )

            async for chunk in stream:
                if chunk.get("response"):
                    yield chunk["response"]

        except Exception as e:
            logger.error(f"Streaming failed: {e}")
            raise

    async def chat(
        self,
        messages: list[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> str:
        """
        Chat completion (OpenAI-compatible format).

        Args:
            messages: List of {"role": "user/assistant", "content": "..."}
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate

        Returns:
            Assistant response text
        """
        try:
            options = {
                "temperature": temperature,
                "num_predict": max_tokens or -1,
            }

            response = await self.client.chat(
                model=self.model,
                messages=messages,
                options=options,
                keep_alive=self.keep_alive,
            )

            return response["message"]["content"]

        except Exception as e:
            logger.error(f"Chat failed: {e}")
            raise

    async def unload_model(self) -> bool:
        """
        Immediately unload model from VRAM.
        Critical for memory management when switching models.
        """
        try:
            await self.client.generate(
                model=self.model, prompt="", keep_alive="0"
            )
            logger.info(f"Model {self.model} unloaded from VRAM")
            return True
        except Exception as e:
            logger.error(f"Failed to unload model: {e}")
            return False

    async def get_model_info(self) -> Dict[str, Any]:
        """Get information about the loaded model."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/api/show",
                    json={"name": self.model},
                )
                return response.json()
        except Exception as e:
            logger.error(f"Failed to get model info: {e}")
            return {}

    async def embeddings(self, text: str) -> list[float]:
        """
        Generate embeddings for text.
        Note: Use dedicated embedding models (nomic-embed-text) for best results.
        """
        try:
            response = await self.client.embeddings(model=self.model, prompt=text)
            return response.get("embedding", [])
        except Exception as e:
            logger.error(f"Embedding generation failed: {e}")
            return []

    def __repr__(self) -> str:
        return f"OllamaClient(model='{self.model}', base_url='{self.base_url}')"