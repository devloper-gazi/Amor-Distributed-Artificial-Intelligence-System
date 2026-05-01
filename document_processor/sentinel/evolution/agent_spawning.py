"""
Sentinel Evolution — Subsystem E: dynamic agent spawning.

The five core agents (Auditor, Reasoner, RedTeam, Patcher, Judge)
stay fixed.  Sentinel grows new domain-specialist agents over time
as the user's findings cluster around a particular CWE family.

Workflow:

1. **MetaMonitor.observe(findings)** — keeps a rolling window of
   the last N findings (default 100).  When one CWE class accounts
   for ≥ ``threshold_percent`` of the window, returns a spawn
   recommendation.
2. **AgentFactory.spawn(recommendation)** — uses the
   parent Auditor's system prompt + RAG-fetched CWE description +
   the spawn group's confirmed examples to synthesise a specialist
   system prompt.  Saves a ``SpawnedAgent`` manifest to
   ``evolution/spawned_agents/<name>/``.
3. **ShadowTracker.record_run(...)** — for the next 30 days, every
   run with a matching trigger flag invokes the new agent IN
   PARALLEL with the parent Auditor; both verdicts are saved to
   ``shadow_metrics.json`` but only the parent verdict is shown to
   the user.
4. **AgentPromoter.maybe_promote(agent)** — at the end of shadow
   mode, computes precision + recall on the matched runs.  If the
   new agent beats the parent by ≥ ``improvement_percent`` (default
   15%) it is promoted to ``status=active``.  Otherwise demoted to
   ``dormant`` and archived after 60 days.

Every step records a ledger entry through ``LedgerStore`` and is
constraint-checked against ``ImmutableConstraints`` before writing
to disk.

License: MIT.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from .governance import (
    HardConstraintViolation,
    ImmutableConstraints,
    LedgerStore,
)


logger = logging.getLogger(__name__)


LLMCall = Callable[[str, str | None, int], Awaitable[str]]


# ─────────────────────────────────────────────────────────────────────
# Data shapes
# ─────────────────────────────────────────────────────────────────────


@dataclass
class SpawnRecommendation:
    """The MetaMonitor's verdict for the rolling window."""
    cwe: str
    occurrences: int
    window_size: int
    percent: float
    languages: list[str]
    sample_findings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SpawnedAgent:
    """One specialist agent born from a recommendation."""
    name: str                       # e.g. "crypto_specialist"
    primary_cwe: str
    languages: list[str]
    trigger_flag: str               # router flag that wakes it up
    system_prompt: str
    parent_agent: str = "auditor"
    base_model: str = "qwen2.5-coder:7b"
    status: str = "shadow"          # shadow | active | dormant | archived
    created_at: float = 0.0
    promoted_at: float | None = None
    demoted_at: float | None = None
    shadow_metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ShadowRunRecord:
    """One side-by-side run during shadow mode."""
    timestamp: float
    finding_fingerprint: str
    cwe: str
    parent_verdict: str
    parent_confidence: float
    spawned_verdict: str
    spawned_confidence: float
    user_truth: str | None = None   # populated when user confirms / rejects


# ─────────────────────────────────────────────────────────────────────
# MetaMonitor
# ─────────────────────────────────────────────────────────────────────


@dataclass
class MetaMonitor:
    window_size: int = 100
    threshold_percent: float = 30.0   # %

    def observe(self, findings: Iterable[dict[str, Any]]) -> list[SpawnRecommendation]:
        """Walk a stream of recent findings; emit a recommendation
        per CWE class that crosses the threshold."""
        items = list(findings)[-self.window_size:]
        n = len(items)
        if n == 0:
            return []
        counter = Counter(str(f.get("cwe") or "").strip() for f in items)
        recommendations: list[SpawnRecommendation] = []
        for cwe, count in counter.most_common():
            if not cwe:
                continue
            pct = (count / n) * 100.0
            if pct < self.threshold_percent:
                continue
            languages = sorted({
                str(f.get("language") or "").strip()
                for f in items
                if str(f.get("cwe") or "").strip() == cwe
                and f.get("language")
            })
            samples = [f for f in items if str(f.get("cwe") or "") == cwe][:6]
            recommendations.append(SpawnRecommendation(
                cwe=cwe,
                occurrences=count,
                window_size=n,
                percent=round(pct, 2),
                languages=list(languages),
                sample_findings=[
                    {
                        "file": str(s.get("file") or ""),
                        "line_start": int(s.get("line_start") or 0),
                        "raw_message": str(s.get("raw_message") or "")[:240],
                    }
                    for s in samples
                ],
            ))
        return recommendations


# ─────────────────────────────────────────────────────────────────────
# AgentFactory — synth a specialist's system prompt
# ─────────────────────────────────────────────────────────────────────


SPAWN_SYSTEM_PROMPT = (
    "You are a senior security engineer designing a specialist "
    "auditor for a single vulnerability family. Given the parent "
    "Auditor system prompt + sample confirmed findings + the CWE "
    "description, write a NEW system prompt for the specialist. "
    "Keep the JSON contract identical to the parent. Be terse, "
    "expert-tone, no refusal language. Output only the new prompt "
    "— no fences, no commentary."
)


def slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return base or "specialist"


def derive_agent_name(cwe: str) -> str:
    table = {
        "CWE-79": "xss_specialist",
        "CWE-89": "sqli_specialist",
        "CWE-22": "path_specialist",
        "CWE-78": "cmd_injection_specialist",
        "CWE-94": "code_injection_specialist",
        "CWE-327": "crypto_specialist",
        "CWE-328": "crypto_specialist",
        "CWE-329": "crypto_specialist",
        "CWE-502": "deserialization_specialist",
        "CWE-798": "secrets_specialist",
        "CWE-918": "ssrf_specialist",
        "CWE-1321": "prototype_pollution_specialist",
    }
    return table.get(cwe.upper().strip(), f"specialist_{slugify(cwe)}")


def derive_trigger_flag(cwe: str, languages: Iterable[str]) -> str:
    langs = "_".join(sorted({(l or "").strip().lower() for l in languages if l})[:3])
    base = derive_agent_name(cwe).replace("_specialist", "")
    return f"{base}_relevant{('_' + langs) if langs else ''}"


class AgentFactory:
    SYSTEM_PROMPT = SPAWN_SYSTEM_PROMPT

    def __init__(
        self,
        *,
        ledger: LedgerStore,
        constraints: ImmutableConstraints,
        root: str | Path,
    ) -> None:
        self.ledger = ledger
        self.constraints = constraints
        self.root = Path(root) / "spawned_agents"
        self.root.mkdir(parents=True, exist_ok=True)

    async def spawn(
        self,
        recommendation: SpawnRecommendation,
        *,
        parent_system_prompt: str,
        cwe_corpus_entry: dict[str, Any] | None,
        llm: LLMCall,
        max_tokens: int = 1500,
    ) -> SpawnedAgent | None:
        """Synthesise a new agent's system prompt + manifest."""
        if not recommendation.cwe:
            return None
        # Build the user prompt for the LLM.
        rows: list[str] = []
        rows.append(f"# CWE: {recommendation.cwe}")
        rows.append(f"# Languages observed: {', '.join(recommendation.languages) or 'any'}")
        rows.append("")
        rows.append("## Parent Auditor system prompt (do NOT just copy)")
        rows.append(parent_system_prompt[:3000])
        if cwe_corpus_entry:
            rows.append("")
            rows.append("## Authoritative CWE entry")
            rows.append(f"- Name: {cwe_corpus_entry.get('name', '')}")
            rows.append(f"- Description: {str(cwe_corpus_entry.get('description', ''))[:600]}")
            rows.append(f"- Mitigation: {str(cwe_corpus_entry.get('mitigation', ''))[:400]}")
        rows.append("")
        rows.append("## Sample confirmed findings (recent)")
        for s in recommendation.sample_findings[:5]:
            rows.append(f"- {s.get('file')}:{s.get('line_start')} — {s.get('raw_message')}")
        rows.append("")
        rows.append(
            "Write the specialist system prompt now. Keep the JSON "
            "schema identical to the parent. Be direct, expert-tone, "
            "no refusal language."
        )

        try:
            raw = await llm("\n".join(rows), self.SYSTEM_PROMPT, max_tokens)
        except Exception as exc:  # pragma: no cover
            logger.debug("spawn llm failed: %s", exc)
            return None
        new_prompt = (raw or "").strip()
        if new_prompt.startswith("```"):
            new_prompt = new_prompt.strip("`").strip()
        if not new_prompt:
            return None

        agent_name = derive_agent_name(recommendation.cwe)
        trigger = derive_trigger_flag(recommendation.cwe, recommendation.languages)

        # Constraint-check the synthesised prompt before writing.
        try:
            self.constraints.check({"prompt": new_prompt, "name": agent_name})
        except HardConstraintViolation as exc:
            self.ledger.append(
                actor="agent_spawning",
                kind="constraint_check_failed",
                payload={"agent": agent_name, "reason": str(exc)},
            )
            return None

        agent = SpawnedAgent(
            name=agent_name,
            primary_cwe=recommendation.cwe,
            languages=list(recommendation.languages),
            trigger_flag=trigger,
            system_prompt=new_prompt,
            parent_agent="auditor",
            status="shadow",
            created_at=time.time(),
        )
        self.write(agent)
        self.ledger.append(
            actor="agent_spawning",
            kind="agent_spawned",
            payload={
                "agent": agent_name,
                "primary_cwe": agent.primary_cwe,
                "languages": agent.languages,
                "trigger_flag": agent.trigger_flag,
                "occurrences": recommendation.occurrences,
                "percent": recommendation.percent,
            },
        )
        return agent

    # ─── persistence ────────────────────────────────────────────

    def write(self, agent: SpawnedAgent) -> Path:
        agent_dir = self.root / agent.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        manifest = agent_dir / "manifest.yaml"
        try:
            import yaml  # type: ignore
            text = yaml.safe_dump(agent.to_dict(), sort_keys=True)
        except Exception:
            text = json.dumps(agent.to_dict(), indent=2, default=str)
        manifest.write_text(text, encoding="utf-8")
        # Plain-text system prompt for easy diffing.
        (agent_dir / "system_prompt.txt").write_text(
            agent.system_prompt, encoding="utf-8",
        )
        return manifest

    def list_agents(self, *, status: str | None = None) -> list[SpawnedAgent]:
        out: list[SpawnedAgent] = []
        for d in sorted(self.root.iterdir() if self.root.is_dir() else []):
            if not d.is_dir():
                continue
            manifest = d / "manifest.yaml"
            if not manifest.is_file():
                continue
            try:
                import yaml  # type: ignore
                data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            except Exception:
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            agent = SpawnedAgent(
                name=str(data.get("name") or d.name),
                primary_cwe=str(data.get("primary_cwe") or ""),
                languages=list(data.get("languages") or []),
                trigger_flag=str(data.get("trigger_flag") or ""),
                system_prompt=str(data.get("system_prompt") or ""),
                parent_agent=str(data.get("parent_agent") or "auditor"),
                base_model=str(data.get("base_model") or "qwen2.5-coder:7b"),
                status=str(data.get("status") or "shadow"),
                created_at=float(data.get("created_at") or 0.0),
                promoted_at=data.get("promoted_at"),
                demoted_at=data.get("demoted_at"),
                shadow_metrics=dict(data.get("shadow_metrics") or {}),
            )
            if status is None or agent.status == status:
                out.append(agent)
        return out


# ─────────────────────────────────────────────────────────────────────
# ShadowTracker + AgentPromoter
# ─────────────────────────────────────────────────────────────────────


class ShadowTracker:
    """Append-only shadow run log per agent."""

    def __init__(self, *, factory: AgentFactory) -> None:
        self._factory = factory

    def record(self, agent_name: str, record: ShadowRunRecord) -> None:
        path = self._factory.root / agent_name / "shadow_metrics.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), default=str))
            f.write("\n")

    def metrics(self, agent_name: str) -> dict[str, Any]:
        path = self._factory.root / agent_name / "shadow_metrics.jsonl"
        if not path.is_file():
            return {"runs": 0}
        runs = 0
        parent_correct = 0
        spawned_correct = 0
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not d.get("user_truth"):
                    continue
                runs += 1
                truth = str(d.get("user_truth") or "").lower()
                if str(d.get("parent_verdict") or "").lower() == truth:
                    parent_correct += 1
                if str(d.get("spawned_verdict") or "").lower() == truth:
                    spawned_correct += 1
        if runs == 0:
            return {"runs": 0}
        return {
            "runs": runs,
            "parent_accuracy": round(parent_correct / runs, 4),
            "spawned_accuracy": round(spawned_correct / runs, 4),
            "delta": round((spawned_correct - parent_correct) / runs, 4),
        }


class AgentPromoter:
    """Promote / dormant decisions at the end of shadow mode."""

    def __init__(
        self,
        *,
        factory: AgentFactory,
        tracker: ShadowTracker,
        ledger: LedgerStore,
        shadow_days: float = 30.0,
        improvement_percent: float = 0.15,
    ) -> None:
        self.factory = factory
        self.tracker = tracker
        self.ledger = ledger
        self.shadow_days = shadow_days
        self.improvement_percent = improvement_percent

    def maybe_promote(self, agent: SpawnedAgent) -> SpawnedAgent:
        if agent.status != "shadow":
            return agent
        elapsed_days = (time.time() - agent.created_at) / 86400
        if elapsed_days < self.shadow_days:
            return agent  # too early
        metrics = self.tracker.metrics(agent.name)
        agent.shadow_metrics = metrics
        runs = int(metrics.get("runs") or 0)
        delta = float(metrics.get("delta") or 0.0)
        if runs == 0:
            # No labelled data → keep in shadow longer.
            self.factory.write(agent)
            return agent
        if delta >= self.improvement_percent:
            agent.status = "active"
            agent.promoted_at = time.time()
            self.factory.write(agent)
            self.ledger.append(
                actor="agent_spawning",
                kind="agent_promoted",
                payload={
                    "agent": agent.name,
                    "primary_cwe": agent.primary_cwe,
                    "metrics": metrics,
                },
            )
        else:
            agent.status = "dormant"
            agent.demoted_at = time.time()
            self.factory.write(agent)
            self.ledger.append(
                actor="agent_spawning",
                kind="agent_archived",
                payload={
                    "agent": agent.name,
                    "primary_cwe": agent.primary_cwe,
                    "metrics": metrics,
                    "outcome": "demoted_to_dormant",
                },
            )
        return agent


__all__ = [
    "AgentFactory",
    "AgentPromoter",
    "MetaMonitor",
    "SPAWN_SYSTEM_PROMPT",
    "ShadowRunRecord",
    "ShadowTracker",
    "SpawnRecommendation",
    "SpawnedAgent",
    "derive_agent_name",
    "derive_trigger_flag",
    "slugify",
]
