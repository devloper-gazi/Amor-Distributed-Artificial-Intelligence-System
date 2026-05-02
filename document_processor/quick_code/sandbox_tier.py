"""
QuickCode V2 — TieredSandbox: tier-aware ExecutionSandbox wrapper.

Maps the V2 ``SandboxTier`` enum (``QUICK`` / ``PRO``) onto two
pre-configured ``ExecutionSandbox`` instances with different memory
and timeout budgets:

    QUICK : 256 MB memory, 15 s timeout — fast iteration loops
    PRO   : 512 MB memory, 45 s timeout — heavier workloads

Why two sandboxes instead of mutating one?

``ExecutionSandbox`` reads ``_memory_limit`` from a constructor arg;
the value flows into the Docker ``--memory`` flag at run time.  So
swapping memory mid-flight would require either patching the private
attribute (fragile) or reconstructing the sandbox per call
(throws away the cached ``docker_available`` probe).  Holding two
instances side-by-side is the simplest, most predictable design.

The wrapper exposes a single ``execute()`` that returns a
``contracts.SandboxResult``, so V2 callers never touch the legacy
``ExecutionResult`` dataclass.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from typing import Any

from .contracts import SandboxResult, SandboxTier as SandboxTierEnum

logger = logging.getLogger(__name__)


# Sentinel used in tests; pulling the real ExecutionSandbox at module
# load time would require the docker_intelligence package to import
# clean even when Docker is missing — this keeps the import light.
def _build_default_sandbox(
    *, default_timeout: int, memory_mb: int
) -> Any:  # pragma: no cover - lazy import
    from ..code_intelligence.sandbox import ExecutionSandbox

    return ExecutionSandbox(
        default_timeout=default_timeout,
        memory_limit=f"{memory_mb}m",
    )


class TieredSandbox:
    """Sandbox dispatcher that routes ``QUICK`` vs ``PRO`` to a
    pre-configured ``ExecutionSandbox`` instance.

    Args:
        quick_mem_mb / quick_timeout_s — Quick-tier limits.
        pro_mem_mb / pro_timeout_s — Pro-tier limits.
        quick_sandbox / pro_sandbox — Optional injected sandbox
            instances (used by tests).  When omitted we lazily build
            real ``ExecutionSandbox`` instances on first use.
    """

    def __init__(
        self,
        *,
        quick_mem_mb: int = 256,
        quick_timeout_s: int = 15,
        pro_mem_mb: int = 512,
        pro_timeout_s: int = 45,
        quick_sandbox: Any | None = None,
        pro_sandbox: Any | None = None,
    ) -> None:
        self._quick_mem_mb = max(64, int(quick_mem_mb))
        self._quick_timeout_s = max(1, int(quick_timeout_s))
        self._pro_mem_mb = max(self._quick_mem_mb, int(pro_mem_mb))
        self._pro_timeout_s = max(self._quick_timeout_s, int(pro_timeout_s))
        self._quick = quick_sandbox
        self._pro = pro_sandbox

    # ─── Public API ─────────────────────────────────────────────────

    async def execute(
        self,
        code: str,
        *,
        language: str = "python",
        tier: SandboxTierEnum = SandboxTierEnum.QUICK,
        extra_files: dict[str, str] | None = None,
        install_packages: list[str] | None = None,
        stdin_data: str | None = None,
    ) -> SandboxResult:
        """Execute ``code`` in the tier-appropriate sandbox.

        Returns a typed ``SandboxResult`` regardless of which sandbox
        impl ran underneath.  Failures degrade to ``ok=False`` rather
        than raising — the engine wraps verification in a fail-soft
        gate, so a sandbox crash should never abort the run."""
        sandbox = self._sandbox_for(tier)
        timeout_s = self._timeout_for(tier)
        try:
            result = await sandbox.execute(
                code,
                language=language,
                extra_files=extra_files,
                install_packages=install_packages,
                timeout=timeout_s,
                stdin_data=stdin_data,
            )
        except Exception as exc:  # pragma: no cover - infra path
            logger.warning("sandbox tier=%s execute failed: %s", tier.value, exc)
            return SandboxResult(
                ok=False,
                stderr=f"{type(exc).__name__}: {exc}"[:8000],
                tier=tier,
            )

        return self._normalise(result, tier=tier)

    def limits_for(self, tier: SandboxTierEnum) -> dict[str, int]:
        if tier is SandboxTierEnum.PRO:
            return {"memory_mb": self._pro_mem_mb, "timeout_s": self._pro_timeout_s}
        return {"memory_mb": self._quick_mem_mb, "timeout_s": self._quick_timeout_s}

    # ─── Internals ──────────────────────────────────────────────────

    def _timeout_for(self, tier: SandboxTierEnum) -> int:
        return self._pro_timeout_s if tier is SandboxTierEnum.PRO else self._quick_timeout_s

    def _sandbox_for(self, tier: SandboxTierEnum) -> Any:
        if tier is SandboxTierEnum.PRO:
            if self._pro is None:
                self._pro = _build_default_sandbox(
                    default_timeout=self._pro_timeout_s,
                    memory_mb=self._pro_mem_mb,
                )
            return self._pro
        if self._quick is None:
            self._quick = _build_default_sandbox(
                default_timeout=self._quick_timeout_s,
                memory_mb=self._quick_mem_mb,
            )
        return self._quick

    def _normalise(self, raw: Any, *, tier: SandboxTierEnum) -> SandboxResult:
        """Coerce whatever the underlying sandbox returns into a
        ``contracts.SandboxResult``.  Accepts:

        * an ``ExecutionResult`` dataclass (legacy path)
        * a ``dict`` (test fakes)
        * an existing ``SandboxResult`` (passthrough)
        """
        if isinstance(raw, SandboxResult):
            return raw if raw.tier is tier else raw.model_copy(update={"tier": tier})
        if is_dataclass(raw):
            data = asdict(raw)
        elif isinstance(raw, dict):
            data = dict(raw)
        else:
            return SandboxResult(
                ok=False,
                stderr=f"unsupported sandbox return type: {type(raw).__name__}",
                tier=tier,
            )
        # ExecutionResult uses ``exit_code`` + ``error`` + ``skipped``.
        # Translate to SandboxResult's vocabulary; "skipped" runs are
        # not failures — they're a deferred pass.
        skipped = bool(data.get("skipped"))
        ok = (not data.get("error")) and (int(data.get("exit_code") or 0) == 0)
        if skipped:
            ok = True
        return SandboxResult(
            ok=ok,
            stdout=str(data.get("stdout") or "")[:8000],
            stderr=str(data.get("stderr") or data.get("error") or "")[:8000],
            exit_code=int(data.get("exit_code") or 0),
            duration_ms=float(data.get("duration_ms") or 0.0),
            memory_mb=float(data.get("memory_mb") or 0.0),
            timed_out=bool(data.get("timed_out") or False),
            tier=tier,
        )


__all__ = ["TieredSandbox"]
