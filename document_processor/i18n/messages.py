"""
Cycle C Sprint 10 Day 4 — backend message catalogues.

Narrow scope on purpose: only error + system-level messages that
hit the wire as ``HTTPException.detail`` or as SSE error envelopes.
UI chrome (button labels, headings) lives in the frontend table.

Keys group by router so a future grep stays trivial:
* ``training.*``  — Sprint 6 admin Training routes
* ``memory.*``   — Sprint 7 admin Memory routes
* ``agent.*``    — Sprint 8 agent routes
* ``stream.*``   — Sprint 9 ResumableStream / SSE
* ``repo.*``     — Sprint 4 repo symbols
* ``common.*``   — shared 401 / 403 / 404 / 5xx detail strings
"""

from __future__ import annotations

from typing import Dict


SUPPORTED: tuple[str, ...] = ("en", "tr")


_EN: Dict[str, str] = {
    # ── common (Sprint 4–9) ────────────────────────────────────
    "common.auth_required":        "authentication required",
    "common.db_unavailable":       "database unavailable",
    "common.not_found":            "not found",
    "common.forbidden":            "forbidden",
    "common.internal_error":       "internal error",
    "common.invalid_mode":         "invalid mode: {{mode}}",

    # ── training (Sprint 6) ────────────────────────────────────
    "training.threshold_not_met":
        "only {{n}} untrained pairs — need {{required}}.  "
        "Pass enforce_threshold=false to bypass for smoke tests.",
    "training.run_not_found":      "run not found",
    "training.gate_blocked":
        "eval has not cleared the promote_ok gate — refusing.",
    "training.toggle_failed":      "lora-adapters toggle failed: {{err}}",
    "training.execute_status":
        "run is in status='{{status}}', execute requires 'pending'",
    "training.export_kickoff":     "pair export failed to start: {{err}}",
    "training.export_rc":
        "pair export rc={{rc}}: {{tail}}",
    "training.trainer_kickoff":    "trainer failed to start: {{err}}",

    # ── memory (Sprint 7) ──────────────────────────────────────
    "memory.unavailable":
        "memory backend not available — set AMOR_MEMORY_BACKEND=mem0",
    "memory.delete_failed":        "memory not found or delete failed",

    # ── repo (Sprint 4 Day 2) ──────────────────────────────────
    "repo.index_unavailable":      "repo index unavailable",

    # ── agent (Sprint 8) ───────────────────────────────────────
    "agent.session_not_found":     "agent session not found",
    "agent.llm_unavailable":       "llm backend unavailable: {{err}}",

    # ── stream (Sprint 9) ──────────────────────────────────────
    "stream.cross_replica_no_redis":
        "session not found on this replica and Redis is unreachable; "
        "cross-replica resume requires Redis Streams.",
}


_TR: Dict[str, str] = {
    # ── ortak (Sprint 4–9) ─────────────────────────────────────
    "common.auth_required":        "kimlik doğrulama gerekli",
    "common.db_unavailable":       "veritabanı erişilemez",
    "common.not_found":            "bulunamadı",
    "common.forbidden":            "yasak",
    "common.internal_error":       "iç hata",
    "common.invalid_mode":         "geçersiz mod: {{mode}}",

    # ── eğitim (Sprint 6) ──────────────────────────────────────
    "training.threshold_not_met":
        "yalnız {{n}} eğitilmemiş çift var — {{required}} gerekiyor.  "
        "Deneme çalışması için enforce_threshold=false geçin.",
    "training.run_not_found":      "çalışma bulunamadı",
    "training.gate_blocked":
        "değerlendirme promote_ok eşiğini geçmedi — reddediliyor.",
    "training.toggle_failed":      "lora-adapters değiştirme başarısız: {{err}}",
    "training.execute_status":
        "çalışma '{{status}}' durumunda, execute 'pending' gerektirir",
    "training.export_kickoff":     "çift dışa aktarma başlatılamadı: {{err}}",
    "training.export_rc":
        "çift dışa aktarma rc={{rc}}: {{tail}}",
    "training.trainer_kickoff":    "eğitici başlatılamadı: {{err}}",

    # ── bellek (Sprint 7) ──────────────────────────────────────
    "memory.unavailable":
        "bellek backend'i kullanılamıyor — AMOR_MEMORY_BACKEND=mem0 ayarlayın",
    "memory.delete_failed":        "anı bulunamadı veya silme başarısız",

    # ── repo (Sprint 4 Day 2) ──────────────────────────────────
    "repo.index_unavailable":      "repo dizini kullanılamıyor",

    # ── ajan (Sprint 8) ────────────────────────────────────────
    "agent.session_not_found":     "ajan oturumu bulunamadı",
    "agent.llm_unavailable":       "llm backend'i kullanılamıyor: {{err}}",

    # ── akış (Sprint 9) ────────────────────────────────────────
    "stream.cross_replica_no_redis":
        "oturum bu replikada bulunamadı ve Redis erişilemez; "
        "replika-arası devam için Redis Streams gerekiyor.",
}


LOCALES: Dict[str, Dict[str, str]] = {
    "en": _EN,
    "tr": _TR,
}
