"""v18.1.3 hotfix — Pydantic v2 protected_namespaces regression.

Every BaseModel field starting with ``model_`` collides with Pydantic
v2's reserved ``model_`` namespace (used for its own dunder methods
like ``model_dump``, ``model_validate``, etc.) and emits a UserWarning
at class-definition time:

    UserWarning: Field "model_X" has conflict with protected namespace
    "model_".  You may be able to resolve this warning by setting
    `model_config['protected_namespaces'] = ()`.

The warnings are HARMLESS at runtime but spam the boot log + obscure
real warnings in CI captures.  This test pins ``protected_namespaces
= ()`` on every BaseModel that needs to carry a ``model_*`` field so
future commits don't regress.
"""

from __future__ import annotations

import warnings

import pytest


def _classes_with_model_field_must_opt_out(classes):
    """Helper — assert each class has ``protected_namespaces=()`` in its
    Pydantic v2 ``model_config``."""
    for cls in classes:
        cfg = getattr(cls, "model_config", None)
        # Pydantic v2 ConfigDict — either {} or {"protected_namespaces": ...}
        # The opt-out is what we want.
        assert cfg is not None, f"{cls.__name__} has no model_config"
        protected = cfg.get("protected_namespaces", None)
        assert protected == (), (
            f"{cls.__name__} declares a `model_*` field but does NOT opt out "
            f"of Pydantic's protected_namespaces.  Add "
            f"`model_config = {{'protected_namespaces': ()}}` to the class so "
            "import-time UserWarning stops spamming the boot log."
        )


def test_quick_code_start_response_opts_out():
    from document_processor.api.quick_code_routes import QuickCodeStartResponse
    _classes_with_model_field_must_opt_out([QuickCodeStartResponse])


def test_pair_in_opts_out():
    from document_processor.api.admin_training_routes import PairIn
    _classes_with_model_field_must_opt_out([PairIn])


def test_pair_out_opts_out():
    from document_processor.api.admin_training_routes import PairOut
    _classes_with_model_field_must_opt_out([PairOut])


def test_run_in_opts_out():
    from document_processor.api.admin_training_routes import RunIn
    _classes_with_model_field_must_opt_out([RunIn])


def test_model_routes_classes_opt_out():
    from document_processor.api.model_routes import (
        PreferenceWriteRequest, TestGenerateRequest,
    )
    _classes_with_model_field_must_opt_out(
        [PreferenceWriteRequest, TestGenerateRequest],
    )


def test_no_userwarning_on_clean_import():
    """End-to-end smoke — importing every module that carries a
    ``model_*`` Pydantic field must NOT emit UserWarning.  Catches the
    case where a future commit adds a new model_X field without the
    opt-out."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        import document_processor.api.quick_code_routes  # noqa: F401
        import document_processor.api.admin_training_routes  # noqa: F401
        import document_processor.api.model_routes  # noqa: F401
    pydantic_warnings = [
        w for w in caught
        if "protected namespace" in str(w.message)
    ]
    assert not pydantic_warnings, (
        "Got Pydantic protected_namespaces warnings on import: "
        + ", ".join(str(w.message) for w in pydantic_warnings)
    )
