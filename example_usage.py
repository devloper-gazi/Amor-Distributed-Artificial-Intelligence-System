#!/usr/bin/env python3
"""
Example usage of the Document Processing API for research.
This script demonstrates how to use the system for multilingual research.
"""

import requests
import json
import time
from typing import Dict, Any

# API Base URL
BASE_URL = "http://localhost:8000"


def check_health() -> Dict[str, Any]:
    """Check if the service is healthy."""
    response = requests.get(f"{BASE_URL}/health")
    return response.json()


def process_single_document(source_type: str, source_url: str = None,
                           source_path: str = None, priority: str = "balanced") -> Dict[str, Any]:
    """
    Process a single document.

    Args:
        source_type: Type of source (web, pdf, api, file, sql, nosql)
        source_url: URL of the source (for web, api)
        source_path: Path to the source file (for pdf, file)
        priority: Processing priority (quality, balanced, volume)

    Returns:
        Processed document
    """
    payload = {
        "source_type": source_type,
        "priority": priority,
        "metadata": {
            "processed_by": "research_script",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }
    }

    if source_url:
        payload["source_url"] = source_url
    if source_path:
        payload["source_path"] = source_path

    response = requests.post(
        f"{BASE_URL}/process/single",
        json=payload
    )

    return response.json()


def process_batch(sources: list, async_processing: bool = True) -> Dict[str, Any]:
    """
    Process multiple documents in batch.

    Args:
        sources: List of source documents
        async_processing: Process in background if True

    Returns:
        Batch processing response
    """
    payload = {
        "sources": sources,
        "async_processing": async_processing
    }

    response = requests.post(
        f"{BASE_URL}/process",
        json=payload
    )

    return response.json()


def get_document(document_id: str) -> Dict[str, Any]:
    """Get a processed document by ID."""
    response = requests.get(f"{BASE_URL}/document/{document_id}")
    return response.json()


def get_stats() -> Dict[str, Any]:
    """Get processing statistics."""
    response = requests.get(f"{BASE_URL}/stats")
    return response.json()


def example_web_scraping():
    """Example: Scrape and translate a web page."""
    print("\n=== Example 1: Web Scraping ===")
    print("Processing a web page for translation...")

    # Note: Replace with actual URL you want to process
    result = process_single_document(
        source_type="web",
        source_url="https://example.com",  # Replace with real URL
        priority="quality"
    )

    print(f"Document processed: {result['id']}")
    print(f"Original language: {result['original_language']['name']} ({result['original_language']['confidence']*100:.1f}% confidence)")
    print(f"Translated by: {result['translation_provider']}")
    print(f"Processing time: {result['processing_time_ms']:.2f}ms")
    print(f"\nOriginal text (first 200 chars):\n{result['original_text'][:200]}...")
    print(f"\nTranslated text (first 200 chars):\n{result['translated_text'][:200]}...")


def example_batch_processing():
    """Example: Process multiple documents."""
    print("\n=== Example 2: Batch Processing ===")
    print("Processing multiple documents...")

    sources = [
        {
            "source_type": "web",
            "source_url": "https://example.com/article1",  # Replace with real URLs
            "priority": "quality",
            "metadata": {"topic": "AI Research"}
        },
        {
            "source_type": "web",
            "source_url": "https://example.com/article2",
            "priority": "balanced",
            "metadata": {"topic": "Machine Learning"}
        }
    ]

    result = process_batch(sources, async_processing=True)

    print(f"Batch ID: {result['batch_id']}")
    print(f"Submitted: {result['submitted']} documents")
    print(f"Estimated completion: {result['estimated_completion_time_seconds']} seconds")


def example_check_stats():
    """Example: Check processing statistics."""
    print("\n=== Example 3: Processing Statistics ===")

    stats = get_stats()

    print("\nPipeline Stats:")
    print(f"  Total processed: {stats['pipeline']['processed']}")
    print(f"  Failed: {stats['pipeline']['failed']}")
    print(f"  Cache hits: {stats['pipeline']['cache_hits']}")
    print(f"  Cache misses: {stats['pipeline']['cache_misses']}")
    print(f"  Languages detected: {stats['pipeline']['languages_detected']}")
    print(f"  Translation providers used: {stats['pipeline']['providers_used']}")

    print("\nCache Stats:")
    print(f"  Hit rate: {stats['cache']['hit_rate']*100:.1f}%")
    print(f"  Total keys: {stats['cache']['keys']}")

    print("\nStorage Stats:")
    print(f"  Total documents: {stats['storage']['total_documents']}")
    print(f"  By provider: {stats['storage']['by_provider']}")


if __name__ == "__main__":
    print("=" * 60)
    print("Document Processing System - Research Examples")
    print("=" * 60)

    # Check health
    print("\nChecking system health...")
    health = check_health()
    print(f"Status: {health['status']}")
    print(f"Components: {health['components']}")

    if health['status'] != 'healthy':
        print("\n[WARNING] System is not healthy. Please check the services.")
        exit(1)

    print("\n[OK] System is healthy!")

    # Run examples
    # Uncomment the examples you want to run:

    # example_web_scraping()
    # example_batch_processing()
    example_check_stats()

    print("\n" + "=" * 60)
    print("Examples completed!")
    print("=" * 60)
