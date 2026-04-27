"""AMOR command-line interface — currently exposes the Consortium pipeline.

Usage::

    python -m document_processor.cli consortium "Build me a CSV diff tool" \\
        --depth deep --language python --output ./out

Or via HTTP against an already-running server::

    python -m document_processor.cli consortium "Build me X" --remote http://localhost:8000
"""
