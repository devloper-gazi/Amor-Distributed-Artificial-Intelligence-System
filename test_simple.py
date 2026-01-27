#!/usr/bin/env python3
"""
Simple tests and smoke checks for the Amor API.

- Can be run directly as a script for a human-readable walkthrough.
- Can be run with pytest to execute automated smoke tests.
"""

import json
import os
import time

import pytest
import requests

BASE_URL = os.getenv("AMOR_BASE_URL", "http://localhost:8000")
try:
    DEFAULT_TIMEOUT = float(os.getenv("AMOR_HTTP_TIMEOUT", "10"))
except ValueError:
    DEFAULT_TIMEOUT = 10.0


def _service_available() -> bool:
    try:
        response = requests.get(f"{BASE_URL}/health", timeout=DEFAULT_TIMEOUT)
        return response.status_code == 200
    except requests.RequestException:
        return False


def _require_service() -> None:
    if _service_available():
        return

    message = (
        f"Service not available at {BASE_URL}. "
        "Start the application before running these tests."
    )
    if os.getenv("PYTEST_CURRENT_TEST"):
        pytest.skip(message)
    raise requests.exceptions.ConnectionError(message)


def test_simple_text_processing():
    """
    Test processing with a simple text example.
    Note: The current system expects web/pdf sources.
    This demonstrates the API structure.
    """
    _require_service()

    print("=" * 60)
    print("Simple Document Processing Test")
    print("=" * 60)

    # Check health
    print("\n1. Checking system health...")
    health_response = requests.get(f"{BASE_URL}/health", timeout=DEFAULT_TIMEOUT)
    health_response.raise_for_status()
    health = health_response.json()
    print(f"   Status: {health['status']}")
    print(f"   Components: {json.dumps(health['components'], indent=2)}")

    # Get current stats
    print("\n2. Getting current statistics...")
    stats_response = requests.get(f"{BASE_URL}/stats", timeout=DEFAULT_TIMEOUT)
    stats_response.raise_for_status()
    stats = stats_response.json()
    print(f"   Documents processed: {stats['pipeline']['processed']}")
    print(f"   Cache hit rate: {stats['cache']['hit_rate']}%")

    # Example: Process a document (requires real URL)
    print("\n3. Document Processing Example:")
    print("   To process a document, use:")
    print("""
   curl -X POST http://localhost:8000/process/single \\
     -H "Content-Type: application/json" \\
     -d '{
       "source_type": "web",
       "source_url": "https://example.com",
       "priority": "quality"
     }'
    """)

    print("\n4. Supported Source Types:")
    source_types = ["web", "pdf", "api", "file", "sql", "nosql"]
    for st in source_types:
        print(f"   - {st}")

    print("\n5. Translation Priorities:")
    priorities = {
        "quality": "Use Claude 3.5 Sonnet (best for research)",
        "balanced": "Mix of providers (good speed/quality)",
        "volume": "Fast translation (large batches)"
    }
    for priority, desc in priorities.items():
        print(f"   - {priority}: {desc}")

    print("\n" + "=" * 60)
    print("System is ready for document processing!")
    print("=" * 60)

    # Show example with Python requests
    print("\n6. Python Example:")
    print("""
import requests

# Process a web page
response = requests.post(
    "http://localhost:8000/process/single",
    json={
        "source_type": "web",
        "source_url": "https://www.example.com/article",
        "priority": "quality",
        "metadata": {
            "research_topic": "Your Topic",
            "collected_by": "Your Name"
        }
    }
)

result = response.json()
print(f"Document ID: {result['id']}")
print(f"Language: {result['original_language']['name']}")
print(f"Translated by: {result['translation_provider']}")
print(f"Translation: {result['translated_text']}")
    """)

    print("\n7. Important Notes:")
    print("   - The system automatically detects language")
    print("   - Translations are cached to avoid re-processing")
    print("   - Use 'quality' priority for research work")
    print("   - Add metadata to organize your documents")
    print("   - Monitor progress at http://localhost:8000/stats")

    return health


def test_api_root_smoke():
    """
    Basic smoke test for the /api root endpoint.
    """
    _require_service()
    response = requests.get(f"{BASE_URL}/api", timeout=DEFAULT_TIMEOUT)
    assert response.status_code == 200
    data = response.json()

    # Core fields that should always be present
    for key in [
        "service",
        "version",
        "environment",
        "status",
        "chat_research_available",
        "local_ai_available",
        "crawling_available",
        "translation_available",
    ]:
        assert key in data


def test_health_smoke():
    """
    Basic smoke test for the /health endpoint.
    """
    _require_service()
    response = requests.get(f"{BASE_URL}/health", timeout=DEFAULT_TIMEOUT)
    assert response.status_code == 200
    data = response.json()

    assert data["status"] in {"healthy", "degraded"}
    assert "components" in data
    assert isinstance(data["components"], dict)


def test_local_ai_research_smoke():
    """
    Smoke test for /api/local-ai/research.

    This uses quick depth and a simple topic. If the Local AI stack
    is not available (e.g. Ollama/model not installed), the test is
    skipped instead of failing hard.
    """
    _require_service()
    payload = {
        "topic": "Smoke test topic",
        "depth": "quick",
        "use_translation": True,
        "target_language": "en",
        "save_to_knowledge": False,
    }

    response = requests.post(
        f"{BASE_URL}/api/local-ai/research",
        json=payload,
        timeout=30,
    )

    if response.status_code == 503:
        pytest.skip("Local AI research not available (received 503).")

    # For other 5xx codes, surface the error
    response.raise_for_status()
    data = response.json()

    assert data.get("success") is True
    session_id = data.get("session_id")
    assert isinstance(session_id, str) and session_id

    # Poll the status endpoint until the session completes or fails,
    # with a bounded timeout so the smoke test stays quick.
    status_url = f"{BASE_URL}/api/local-ai/research/{session_id}/status"
    max_attempts = 20
    for _ in range(max_attempts):
        status_response = requests.get(status_url, timeout=15)
        status_response.raise_for_status()
        status_data = status_response.json()

        state = status_data.get("status")
        assert state in {"started", "in_progress", "completed", "failed"}

        if state in {"completed", "failed"}:
            # We don't assert success vs failure here – the goal is to
            # verify the workflow is reachable and progresses.
            return

        time.sleep(3)

    pytest.fail("Local AI research session did not reach a terminal state in time.")


def test_claude_chat_research_smoke():
    """
    Optional smoke test for /api/chat/research.

    If Claude is not configured (no ANTHROPIC_API_KEY), this test is skipped.
    """
    _require_service()
    # First, check Claude health to see if it's configured.
    health_response = requests.get(f"{BASE_URL}/api/chat/health", timeout=DEFAULT_TIMEOUT)
    if health_response.status_code != 200:
        pytest.skip("Claude chat health endpoint not available.")

    health = health_response.json()
    if not health.get("claude_api_configured"):
        pytest.skip("Claude API not configured; skipping /api/chat/research smoke test.")

    payload = {
        "prompt": "Short smoke-test research prompt about the Amor system.",
        "mode": "research",
        "use_research": False,  # keep this lightweight; no explicit web search
        "max_tokens": 256,
        "temperature": 0.2,
        "history": [],
    }

    response = requests.post(
        f"{BASE_URL}/api/chat/research",
        json=payload,
        timeout=60,
    )
    response.raise_for_status()

    data = response.json()
    assert isinstance(data.get("response"), str)
    assert data["response"].strip() != ""


if __name__ == "__main__":
    try:
        result = test_simple_text_processing()
        print("\n[SUCCESS] Test completed successfully!")
    except requests.exceptions.ConnectionError:
        print("\n[ERROR] Cannot connect to the service.")
        print("Make sure the Docker containers are running:")
        print("  docker compose -f docker-compose.yml -f docker-compose.windows.yml ps")
    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}")
