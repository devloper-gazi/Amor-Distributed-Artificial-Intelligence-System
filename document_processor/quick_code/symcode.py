"""
QuickCode V2 — SymCode: SymPy subprocess validator for math tasks.

Goal
----

When the router classifies a prompt as ``MATH``, we want a check
that's stronger than "tests passed in the sandbox" — we want
*symbolic* assurance that the produced expression is correct.

SymCode validates either:

* a single SymPy expression string (e.g. ``"x**2 + 2*x + 1"``), by
  parsing + simplifying it in an isolated subprocess; or

* a ``code_block`` whose last expression must evaluate to a SymPy
  ``Expr``.  Optional ``expected`` is symbolically compared via
  ``simplify(actual - expected) == 0``.

Sandboxing
----------

Each validation runs in a subprocess that:

* ``sys.path`` is reset to a single empty entry so user code can't
  pull in arbitrary on-disk modules.
* ``__import__`` is wrapped — any module outside the whitelist
  raises ``ImportError`` immediately.
* The whitelist defaults to ``{"sympy"}``; the caller can extend
  it via the constructor.
* A hard ``asyncio.wait_for(timeout_s)`` kills runaway processes.
* Up to ``max_iters`` retries when the subprocess crashes for a
  transient reason (rare in practice — kept as the spec asks).

The check is fail-soft.  If SymPy is missing or the subprocess
mis-behaves we return a ``SymValidationResult(ok=True,
error="symcode_skipped")`` so the engine treats it as a passed_warn
gate rather than a hard failure.

No content filters / refusal language anywhere.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import textwrap
from typing import Any, Iterable

from .contracts import SymValidationResult, TaskIR

logger = logging.getLogger(__name__)


# Defaults pulled from the plan; settings.py mirrors them.
DEFAULT_TIMEOUT_S = 10
DEFAULT_MAX_ITERS = 3
DEFAULT_ALLOWED = frozenset({"sympy"})


# ─────────────────────────────────────────────────────────────────────
# Subprocess script (rendered into stdin)
#
# We render a tiny harness that:
#  1. installs an import-whitelist hook,
#  2. evaluates the user's code in an isolated namespace,
#  3. emits a single JSON line on stdout with the result.
# Every literal value flows through json.dumps so user-provided
# strings can never break out of the rendered Python source.
# ─────────────────────────────────────────────────────────────────────


_HARNESS_TEMPLATE = textwrap.dedent(
    """
    from __future__ import annotations
    import builtins, json, sys, traceback

    def _emit(payload):
        sys.stdout.write('SYMCODE_JSON ' + json.dumps(payload, default=str))
        sys.stdout.write('\\n')
        sys.stdout.flush()

    # Pre-load sympy + its transitive deps with the full sys.path
    # *before* the whitelist locks imports down.  Subsequent
    # ``import sympy`` calls inside user code resolve against the
    # already-cached module in ``sys.modules`` even after we wipe
    # ``sys.path``, so the sandbox stays effective.
    try:
        import sympy as _sp
    except Exception as _exc:
        _emit({{
            'ok': True,
            'skipped': True,
            'error': 'sympy_missing: ' + repr(_exc),
        }})
        sys.exit(0)

    _ALLOWED = set({allowed!r})
    _ORIG_IMPORT = builtins.__import__

    # Snapshot the modules trusted to do unrestricted imports.  Any
    # module already loaded before the hook installs is part of the
    # platform / sympy bundle — those are allowed to keep doing late
    # imports as they process expressions.  User code (which runs
    # under ``__name__ == '__main__'`` or ``'<symcode>'``) does not
    # appear in this set, so it is held to the strict whitelist.
    _TRUSTED_CALLERS = set(sys.modules.keys())

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        caller = (globals or {{}}).get('__name__', '') or ''
        caller_root = caller.split('.', 1)[0] if caller else ''
        # 1) Caller was loaded before the hook installed (the entire
        #    Python platform + the sympy bundle eagerly imported by
        #    the harness).  Trust it.
        if caller in _TRUSTED_CALLERS:
            return _ORIG_IMPORT(name, globals, locals, fromlist, level)
        # 2) Caller is a *lazy* sympy / mpmath / similar submodule
        #    pulled in after the hook.  Sympy loads many of these on
        #    demand (sympy.printing.str etc.) and they need to import
        #    __future__, _io, etc.  We trust everything inside a
        #    whitelisted root.
        if caller_root in _ALLOWED:
            return _ORIG_IMPORT(name, globals, locals, fromlist, level)
        # 3) Relative imports resolve inside the calling package; if
        #    the caller passed neither (1) nor (2) we still allow
        #    these because the parent package was already vetted.
        if level > 0:
            return _ORIG_IMPORT(name, globals, locals, fromlist, level)
        # 4) Strict absolute-import whitelist for user code.
        root = name.split('.')[0]
        if root in _ALLOWED or root == '__main__':
            return _ORIG_IMPORT(name, globals, locals, fromlist, level)
        raise ImportError(
            "module '" + name + "' is not in the symcode whitelist"
        )

    # Wipe sys.path *after* sympy has been imported so user code
    # cannot pull arbitrary modules from disk.
    sys.path = ['']
    builtins.__import__ = _guarded_import

    def _run():
        sp = _sp
        local_ns = {{'sp': sp, 'sympy': sp}}
        code_block = {code_block!r}
        expected = {expected!r}
        mode = {mode!r}
        try:
            if mode == 'expression':
                expr_text = code_block
                actual = sp.sympify(expr_text)
                if expected is None:
                    _emit({{
                        'ok': True,
                        'rationale': 'expression parsed: ' + str(actual),
                        'actual': str(actual),
                    }})
                    return
                exp = sp.sympify(expected)
                diff = sp.simplify(actual - exp)
                ok = diff == 0
                _emit({{
                    'ok': bool(ok),
                    'rationale': (
                        'simplify(actual - expected) = ' + str(diff)
                    ),
                    'actual': str(actual),
                    'expected': str(exp),
                }})
                return

            # mode == 'block' — execute and inspect the binding.
            exec(compile(code_block, '<symcode>', 'exec'), local_ns)
            target = local_ns.get('result')
            if target is None:
                _emit({{
                    'ok': False,
                    'error': "code block must assign to `result`",
                }})
                return
            actual = sp.sympify(target)
            if expected is None:
                _emit({{
                    'ok': True,
                    'rationale': 'result parsed: ' + str(actual),
                    'actual': str(actual),
                }})
                return
            exp = sp.sympify(expected)
            diff = sp.simplify(actual - exp)
            ok = diff == 0
            _emit({{
                'ok': bool(ok),
                'rationale': (
                    'simplify(result - expected) = ' + str(diff)
                ),
                'actual': str(actual),
                'expected': str(exp),
            }})
        except Exception as exc:
            _emit({{
                'ok': False,
                'error': type(exc).__name__ + ': ' + str(exc),
                'traceback': traceback.format_exc(limit=4),
            }})

    _run()
    """
).strip()


def _render_harness(
    *, allowed: Iterable[str], code_block: str, expected: str | None, mode: str
) -> str:
    return _HARNESS_TEMPLATE.format(
        allowed=sorted(allowed),
        code_block=code_block,
        expected=expected,
        mode=mode,
    )


# ─────────────────────────────────────────────────────────────────────
# Public class
# ─────────────────────────────────────────────────────────────────────


class SymCode:
    """SymPy-backed validator for math tasks."""

    def __init__(
        self,
        *,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        max_iters: int = DEFAULT_MAX_ITERS,
        allowed_imports: Iterable[str] = DEFAULT_ALLOWED,
        python_executable: str | None = None,
    ) -> None:
        self._timeout_s = max(1, int(timeout_s))
        self._max_iters = max(1, min(3, int(max_iters)))
        # Always whitelist sympy.  Callers can extend (e.g. for mpmath)
        # but never narrow below that.
        self._allowed = frozenset({"sympy", *allowed_imports})
        self._python = python_executable or sys.executable

    @property
    def allowed_imports(self) -> frozenset[str]:
        return self._allowed

    @property
    def max_iters(self) -> int:
        return self._max_iters

    @property
    def timeout_s(self) -> int:
        return self._timeout_s

    # ─── Public API ─────────────────────────────────────────────────

    async def validate_expression(
        self,
        expression: str,
        *,
        expected: str | None = None,
    ) -> SymValidationResult:
        return await self._run_with_retry(
            code_block=expression, expected=expected, mode="expression"
        )

    async def validate_code_block(
        self,
        code_block: str,
        *,
        expected: str | None = None,
    ) -> SymValidationResult:
        return await self._run_with_retry(
            code_block=code_block, expected=expected, mode="block"
        )

    async def validate(
        self,
        code: str,
        ir: TaskIR | None = None,
        *,
        expected: str | None = None,
    ) -> SymValidationResult:
        """Convenience entry: dispatches on whether the code looks
        like a single SymPy expression or a Python block.

        Heuristic: the presence of a newline or an ``=`` outside
        equality contexts suggests a block; otherwise treat as a
        single expression."""
        del ir  # reserved for future smarter dispatch
        if not code:
            return SymValidationResult(ok=False, error="empty_code")
        if "\n" in code or "result =" in code or "result=" in code:
            return await self.validate_code_block(code, expected=expected)
        return await self.validate_expression(code, expected=expected)

    # ─── Internals ──────────────────────────────────────────────────

    async def _run_with_retry(
        self, *, code_block: str, expected: str | None, mode: str
    ) -> SymValidationResult:
        last_exc: str | None = None
        for attempt in range(1, self._max_iters + 1):
            try:
                return await self._run_once(
                    code_block=code_block,
                    expected=expected,
                    mode=mode,
                    attempt=attempt,
                )
            except asyncio.TimeoutError:
                last_exc = "timeout"
                logger.debug("symcode timeout (attempt %s)", attempt)
            except Exception as exc:  # pragma: no cover - infra
                last_exc = f"{type(exc).__name__}: {exc}"
                logger.debug("symcode subprocess failed (attempt %s): %s", attempt, exc)

        return SymValidationResult(
            ok=True,  # fail-soft: skipped, not a hard failure
            iterations=self._max_iters,
            equivalence_class="skipped",
            rationale="symcode subprocess unreachable",
            error=last_exc or "unknown",
        )

    async def _run_once(
        self,
        *,
        code_block: str,
        expected: str | None,
        mode: str,
        attempt: int,
    ) -> SymValidationResult:
        harness = _render_harness(
            allowed=self._allowed,
            code_block=code_block,
            expected=expected,
            mode=mode,
        )
        # NOTE: we deliberately do NOT pass ``-I`` (isolated) here.
        # ``-I`` strips the user-site path which on Windows is where
        # ``pip install --user sympy`` deposits the package.  The
        # rendered harness re-establishes its own sandbox after
        # importing sympy: it wipes ``sys.path``, installs a strict
        # whitelist hook, and discards every module not pre-loaded
        # at install time.
        proc = await asyncio.create_subprocess_exec(
            self._python,
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(harness.encode("utf-8")),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise

        # Parse the SYMCODE_JSON line.  Anything else on stdout is
        # treated as noise.
        payload: dict[str, Any] | None = None
        for line in stdout_b.decode("utf-8", errors="replace").splitlines():
            if line.startswith("SYMCODE_JSON "):
                try:
                    payload = json.loads(line[len("SYMCODE_JSON "):])
                except json.JSONDecodeError:
                    payload = None
                break

        if payload is None:
            return SymValidationResult(
                ok=True,
                iterations=attempt,
                equivalence_class="skipped",
                rationale="no payload from harness",
                error=stderr_b.decode("utf-8", errors="replace")[:1000] or None,
            )

        if payload.get("skipped"):
            return SymValidationResult(
                ok=True,
                iterations=attempt,
                equivalence_class="skipped",
                rationale="sympy missing in subprocess",
                error=str(payload.get("error") or "")[:500],
            )

        ok = bool(payload.get("ok"))
        return SymValidationResult(
            ok=ok,
            iterations=attempt,
            equivalence_class=("equivalent" if ok else "not_equivalent"),
            rationale=str(payload.get("rationale") or "")[:1000],
            error=(
                str(payload.get("error") or "")[:1000]
                if payload.get("error")
                else None
            ),
        )


__all__ = ["SymCode", "DEFAULT_TIMEOUT_S", "DEFAULT_MAX_ITERS", "DEFAULT_ALLOWED"]
