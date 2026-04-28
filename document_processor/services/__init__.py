"""
Services module for document processor.
Contains high-level service implementations.

Eager imports are deliberately avoided here — `translation_service`
needs `redis` at import time, and `model_manager` / `model_resolution`
should be importable in test contexts that don't install redis. The
two real consumers (`orchestration/pipeline.py`, `api/translation_routes.py`)
already use lazy `..services.translation_service` paths.
"""

# Re-exports remain available via the deeper `from .translation_service import …`
# path. The names below preserve `__all__` discoverability without
# triggering the heavyweight import at package-load time.

__all__ = [
    "TranslationService",
    "TranslationConfig",
    "TranslationJob",
    "ModelManager",
]


def __getattr__(name: str):  # PEP 562 lazy attribute access
    if name in {"TranslationService", "TranslationConfig", "TranslationJob"}:
        from .translation_service import (
            TranslationConfig,
            TranslationJob,
            TranslationService,
        )
        return {
            "TranslationService": TranslationService,
            "TranslationConfig": TranslationConfig,
            "TranslationJob": TranslationJob,
        }[name]
    if name == "ModelManager":
        from .model_manager import ModelManager
        return ModelManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
