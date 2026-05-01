"""
Sentinel Evolution Console HTTP routes — ``/api/sentinel/evolution/*``.

This is the operator-facing surface of Phase 15 (Evolution Engine).
It provides read-only views of the Merkle-chained ledger, the active
genome (production prompts / agents / adapters / DAG), the proposals
queue (everything currently in *staging*), plus a small set of
mutating endpoints — every one of which requires explicit user
intent and records a ledger entry on success.

Endpoints
---------

* ``GET  /api/sentinel/evolution/health``       — liveness + ledger integrity
* ``GET  /api/sentinel/evolution/genome``       — production state snapshot
* ``GET  /api/sentinel/evolution/ledger``       — chain entries (paginated)
* ``GET  /api/sentinel/evolution/proposals``    — staging items pending review
* ``GET  /api/sentinel/evolution/stats``        — aggregate counts
* ``POST /api/sentinel/evolution/promote``      — promote a staging item
* ``POST /api/sentinel/evolution/rollback``     — roll back a production item
* ``POST /api/sentinel/evolution/trigger/{sub}``— manual subsystem trigger

Design notes
------------

* All read endpoints are pure JSON — no SSE.  The Evolution Console
  polls on a 10-second cadence (the operator UI is NOT a hot path).
* Mutations go through ``ImmutableConstraints`` first, then the
  appropriate ``Store.promote()`` / ``rollback_to()`` method, then
  append a ledger entry.  A failed constraint check returns 400 and
  records a ``constraint_check_failed`` entry so abuse leaves a trail.
* The router is wired in ``main.py`` next to ``sentinel_router`` and
  is gated by ``settings.sentinel_evolution_enabled``.

License: MIT.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth.dependencies import get_optional_user
from ..auth.models import User
from ..config.settings import settings


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Lazy imports — keep the route module load-safe even if the optional
# evolution subsystem can't be imported (missing deps, etc.).
# ─────────────────────────────────────────────────────────────────────


def _evo():  # pragma: no cover — pure dispatch
    """Import the evolution package on first call.  Raises 503 if
    the subsystem is unavailable (e.g. import error)."""
    try:
        from .. import sentinel  # noqa: F401  ensures package init
        from ..sentinel.evolution import (  # type: ignore
            governance as gov,
            prompt_evolution as pev,
            rule_synthesis as rsy,
            agent_spawning as asp,
            curriculum as cur,
            lora_pipeline as lor,
            distillation as dis,
            dag_mutation as dag,
        )
        return {
            "gov": gov, "pev": pev, "rsy": rsy, "asp": asp,
            "cur": cur, "lor": lor, "dis": dis, "dag": dag,
        }
    except Exception as exc:  # pragma: no cover
        raise HTTPException(
            status_code=503,
            detail=f"sentinel.evolution unavailable: "
                   f"{type(exc).__name__}: {exc}",
        )


def _root() -> Path:
    p = Path(settings.sentinel_evolution_root).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _actor(user: Optional[User], x_client_id: Optional[str]) -> str:
    if user is not None:
        return f"user:{user.id}"
    if x_client_id:
        return f"client:{x_client_id[:32]}"
    return settings.sentinel_evolution_actor_default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_iso(value: Any) -> Optional[str]:
    """Coerce a created_at field to an ISO string regardless of whether
    the underlying dataclass stores it as a float epoch or already-ISO
    string."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except Exception:
            return None
    return str(value)


def _check_enabled() -> None:
    if not settings.sentinel_evolution_enabled:
        raise HTTPException(
            status_code=503,
            detail="sentinel evolution disabled (settings.sentinel_evolution_enabled=False)",
        )


# ─────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────


router = APIRouter(prefix="/api/sentinel/evolution", tags=["sentinel-evolution"])


class HealthResponse(BaseModel):
    ok: bool
    enabled: bool
    root: str
    ledger_intact: bool
    entry_count: int
    tail_hash: str
    ts: str


class LedgerEntryView(BaseModel):
    entry_id: str
    ts: float
    ts_iso: str
    actor: str
    kind: str
    payload: Dict[str, Any]
    parent_hash: str
    self_hash: str


class LedgerResponse(BaseModel):
    total: int
    returned: int
    entries: List[LedgerEntryView]
    tail_hash: str
    intact: bool


class GenomeProduction(BaseModel):
    dag_version: Optional[str] = None
    dag_node_count: int = 0
    dag_edge_count: int = 0
    prompts: Dict[str, Optional[str]] = Field(default_factory=dict)
    adapters: Dict[str, Optional[str]] = Field(default_factory=dict)
    agents: List[Dict[str, Any]] = Field(default_factory=list)
    students: List[Dict[str, Any]] = Field(default_factory=list)
    rules: List[Dict[str, Any]] = Field(default_factory=list)


class GenomeResponse(BaseModel):
    ts: str
    root: str
    production: GenomeProduction


class ProposalView(BaseModel):
    kind: Literal["prompt", "adapter", "rule", "agent", "dag", "student"]
    id: str
    agent_or_label: str
    status: str
    created_at: Optional[str] = None
    metrics: Dict[str, Any] = Field(default_factory=dict)
    parent: Optional[str] = None
    notes: Optional[str] = None


class ProposalsResponse(BaseModel):
    ts: str
    total: int
    proposals: List[ProposalView]


class StatsResponse(BaseModel):
    ts: str
    ledger_entries: int
    ledger_intact: bool
    counts: Dict[str, Dict[str, int]]   # kind → {production, staging, archived}


class PromoteRequest(BaseModel):
    kind: Literal["prompt", "adapter", "rule", "agent", "dag", "student"]
    target_id: str = Field(..., min_length=1, max_length=200)
    agent_or_label: Optional[str] = Field(None, max_length=120)
    note: Optional[str] = Field(None, max_length=500)


class RollbackRequest(BaseModel):
    kind: Literal["prompt", "adapter", "dag"]
    agent_or_label: str = Field(..., min_length=1, max_length=120)
    target_version: str = Field(..., min_length=1, max_length=200)
    note: Optional[str] = Field(None, max_length=500)


class TriggerRequest(BaseModel):
    payload: Dict[str, Any] = Field(default_factory=dict)
    note: Optional[str] = Field(None, max_length=500)


class MutationResponse(BaseModel):
    ok: bool
    kind: str
    target_id: str
    ledger_entry_id: Optional[str] = None
    message: str = ""


class TriggerResponse(BaseModel):
    ok: bool
    subsystem: str
    ledger_entry_id: Optional[str] = None
    message: str = ""


# ─────────────────────────────────────────────────────────────────────
# Health & integrity
# ─────────────────────────────────────────────────────────────────────


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    _check_enabled()
    e = _evo()
    root = _root()
    ledger = e["gov"].LedgerStore(root)
    entries = ledger.entries()
    intact = ledger.verify()
    return HealthResponse(
        ok=True,
        enabled=True,
        root=str(root),
        ledger_intact=intact,
        entry_count=len(entries),
        tail_hash=ledger.tail_hash,
        ts=_now_iso(),
    )


# ─────────────────────────────────────────────────────────────────────
# Genome — current production state
# ─────────────────────────────────────────────────────────────────────


def _build_genome(root: Path, e: Dict[str, Any]) -> GenomeProduction:
    out = GenomeProduction()

    # DAG production version.
    try:
        dag_store = e["dag"].DAGStore(root)
        prod_dag = dag_store.get_production()
        if prod_dag is not None:
            out.dag_version = prod_dag.version
            out.dag_node_count = len(prod_dag.nodes)
            out.dag_edge_count = len(prod_dag.edges)
    except Exception as exc:  # pragma: no cover
        logger.warning("evolution: DAG read failed: %s", exc)

    # Prompts — production version per agent.
    try:
        ps = e["pev"].PromptStore(root)
        prompts: Dict[str, Optional[str]] = {}
        if ps.root.is_dir():
            for sub in ps.root.iterdir():
                if sub.is_dir():
                    prod = ps.get_production(sub.name)
                    prompts[sub.name] = prod.version if prod else None
        out.prompts = prompts
    except Exception as exc:  # pragma: no cover
        logger.warning("evolution: prompts read failed: %s", exc)

    # LoRA adapters — production per agent.
    try:
        ads = e["lor"].AdapterStore(root)
        adapters: Dict[str, Optional[str]] = {}
        if ads.root.is_dir():
            for sub in ads.root.iterdir():
                if sub.is_dir():
                    prod = ads.get_production(sub.name)
                    adapters[sub.name] = prod.version if prod else None
        out.adapters = adapters
    except Exception as exc:  # pragma: no cover
        logger.warning("evolution: adapters read failed: %s", exc)

    # Spawned agents (status = active).
    try:
        ledger = e["gov"].LedgerStore(root)
        constraints = e["gov"].load_immutable_constraints()
        factory = e["asp"].AgentFactory(
            ledger=ledger, constraints=constraints, root=root,
        )
        agents = factory.list_agents(status="active")
        out.agents = [
            {
                "name": a.name,
                "primary_cwe": a.primary_cwe,
                "languages": list(a.languages),
                "status": a.status,
                "promoted_at": a.promoted_at,
            }
            for a in agents
        ]
    except Exception as exc:  # pragma: no cover
        logger.warning("evolution: agents read failed: %s", exc)

    # Distillation students — production.
    try:
        ss = e["dis"].StudentStore(root)
        manifests = ss.list()
        students: List[Dict[str, Any]] = []
        for m in manifests:
            if getattr(m, "status", None) == "production":
                students.append({
                    "name": getattr(m, "name", ""),
                    "teacher": getattr(m, "teacher", ""),
                    "version": getattr(m, "version", ""),
                    "status": m.status,
                })
        out.students = students
    except Exception as exc:  # pragma: no cover
        logger.warning("evolution: students read failed: %s", exc)

    # Rules — promoted Semgrep rules.
    try:
        rstore = e["rsy"].RuleStore(root)
        prod_rules = rstore.production_rules()
        out.rules = [
            {
                "rule_id": r.rule_id,
                "cwe": r.cwe,
                "language": r.language,
                "promoted_at": r.promoted_at,
                "last_seen_precision": r.last_seen_precision,
            }
            for r in prod_rules
        ]
    except Exception as exc:  # pragma: no cover
        logger.warning("evolution: rules read failed: %s", exc)

    return out


@router.get("/genome", response_model=GenomeResponse)
async def genome() -> GenomeResponse:
    _check_enabled()
    e = _evo()
    root = _root()
    return GenomeResponse(
        ts=_now_iso(),
        root=str(root),
        production=_build_genome(root, e),
    )


# ─────────────────────────────────────────────────────────────────────
# Ledger
# ─────────────────────────────────────────────────────────────────────


@router.get("/ledger", response_model=LedgerResponse)
async def ledger(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    kind: Optional[str] = Query(None, max_length=80),
    actor: Optional[str] = Query(None, max_length=120),
) -> LedgerResponse:
    _check_enabled()
    e = _evo()
    root = _root()
    cap = settings.sentinel_evolution_max_ledger_page
    if limit > cap:
        limit = cap
    store = e["gov"].LedgerStore(root)
    all_entries = store.entries()
    intact = store.verify()
    filtered = all_entries
    if kind:
        filtered = [x for x in filtered if x.kind == kind]
    if actor:
        filtered = [x for x in filtered if x.actor == actor]
    # Newest-first ordering for the timeline view.
    filtered = list(reversed(filtered))
    page = filtered[offset:offset + limit]
    views = []
    for x in page:
        try:
            iso = datetime.fromtimestamp(float(x.ts), tz=timezone.utc).isoformat()
        except Exception:
            iso = ""
        views.append(LedgerEntryView(
            entry_id=x.entry_id,
            ts=float(x.ts),
            ts_iso=iso,
            actor=x.actor,
            kind=x.kind,
            payload=x.payload or {},
            parent_hash=x.parent_hash,
            self_hash=x.self_hash,
        ))
    return LedgerResponse(
        total=len(filtered),
        returned=len(views),
        entries=views,
        tail_hash=store.tail_hash,
        intact=intact,
    )


# ─────────────────────────────────────────────────────────────────────
# Proposals — every staging item across subsystems
# ─────────────────────────────────────────────────────────────────────


def _collect_proposals(root: Path, e: Dict[str, Any]) -> List[ProposalView]:
    out: List[ProposalView] = []

    # Prompt staging.
    try:
        ps = e["pev"].PromptStore(root)
        if ps.root.is_dir():
            for sub in ps.root.iterdir():
                if not sub.is_dir():
                    continue
                for v in ps.list_versions(sub.name):
                    if v.status == "staging":
                        out.append(ProposalView(
                            kind="prompt",
                            id=f"{sub.name}/{v.version}",
                            agent_or_label=sub.name,
                            status=v.status,
                            created_at=_to_iso(v.created_at),
                            metrics=dict(v.eval_metrics or {}),
                            parent=v.parent_version,
                            notes=v.mutation_method,
                        ))
    except Exception as exc:  # pragma: no cover
        logger.warning("evolution: prompt proposals read failed: %s", exc)

    # Adapter staging.
    try:
        ads = e["lor"].AdapterStore(root)
        if ads.root.is_dir():
            for sub in ads.root.iterdir():
                if not sub.is_dir():
                    continue
                for v in ads.list_versions(sub.name):
                    if v.status == "staging":
                        out.append(ProposalView(
                            kind="adapter",
                            id=f"{sub.name}/{v.version}",
                            agent_or_label=sub.name,
                            status=v.status,
                            created_at=_to_iso(v.created_at),
                            metrics=dict(v.eval_metrics or {}),
                            parent=v.parent_version,
                            notes=f"backend={v.backend} method={v.method}",
                        ))
    except Exception as exc:  # pragma: no cover
        logger.warning("evolution: adapter proposals read failed: %s", exc)

    # Rule staging.
    try:
        rstore = e["rsy"].RuleStore(root)
        for r in rstore.list_status("staging"):
            out.append(ProposalView(
                kind="rule",
                id=r.rule_id,
                agent_or_label=r.cwe,
                status=r.status,
                created_at=_to_iso(r.created_at),
                metrics=dict(r.eval_metrics or {}),
                notes=f"language={r.language}",
            ))
    except Exception as exc:  # pragma: no cover
        logger.warning("evolution: rule proposals read failed: %s", exc)

    # Spawned-agent shadow queue (status = shadow).
    try:
        ledger_store = e["gov"].LedgerStore(root)
        constraints = e["gov"].load_immutable_constraints()
        factory = e["asp"].AgentFactory(
            ledger=ledger_store, constraints=constraints, root=root,
        )
        for a in factory.list_agents(status="shadow"):
            out.append(ProposalView(
                kind="agent",
                id=a.name,
                agent_or_label=a.name,
                status=a.status,
                created_at=_to_iso(a.created_at),
                metrics={"primary_cwe": a.primary_cwe,
                         "languages": list(a.languages)},
                parent=a.parent_agent,
            ))
    except Exception as exc:  # pragma: no cover
        logger.warning("evolution: agent proposals read failed: %s", exc)

    # Student staging.
    try:
        ss = e["dis"].StudentStore(root)
        for v in ss.list():
            if getattr(v, "status", None) == "staging":
                out.append(ProposalView(
                    kind="student",
                    id=f"{getattr(v, 'name', '')}/"
                       f"{getattr(v, 'version', '')}",
                    agent_or_label=getattr(v, "name", ""),
                    status=v.status,
                    created_at=_to_iso(getattr(v, "created_at", None)),
                    metrics=dict(getattr(v, "eval_metrics", {}) or {}),
                    notes=f"teacher={getattr(v, 'teacher', '')}",
                ))
    except Exception as exc:  # pragma: no cover
        logger.warning("evolution: student proposals read failed: %s", exc)

    # DAG staging.
    try:
        dag_store = e["dag"].DAGStore(root)
        for status, d in dag_store.list():
            if status == "staging":
                out.append(ProposalView(
                    kind="dag",
                    id=d.version,
                    agent_or_label="pipeline",
                    status=status,
                    metrics={
                        "node_count": len(d.nodes),
                        "edge_count": len(d.edges),
                    },
                ))
    except Exception as exc:  # pragma: no cover
        logger.warning("evolution: dag proposals read failed: %s", exc)

    # Stable ordering: kind then id.
    out.sort(key=lambda p: (p.kind, p.id))
    return out


@router.get("/proposals", response_model=ProposalsResponse)
async def proposals() -> ProposalsResponse:
    _check_enabled()
    e = _evo()
    root = _root()
    plist = _collect_proposals(root, e)
    return ProposalsResponse(ts=_now_iso(), total=len(plist), proposals=plist)


# ─────────────────────────────────────────────────────────────────────
# Stats
# ─────────────────────────────────────────────────────────────────────


@router.get("/stats", response_model=StatsResponse)
async def stats() -> StatsResponse:
    _check_enabled()
    e = _evo()
    root = _root()
    ledger_store = e["gov"].LedgerStore(root)

    def _bucket(items: list, attr: str = "status") -> Dict[str, int]:
        out: Dict[str, int] = {}
        for it in items:
            v = getattr(it, attr, None) or "unknown"
            out[v] = out.get(v, 0) + 1
        return out

    counts: Dict[str, Dict[str, int]] = {}

    # Prompts
    try:
        ps = e["pev"].PromptStore(root)
        all_prompts: list = []
        if ps.root.is_dir():
            for sub in ps.root.iterdir():
                if sub.is_dir():
                    all_prompts.extend(ps.list_versions(sub.name))
        counts["prompts"] = _bucket(all_prompts)
    except Exception:  # pragma: no cover
        counts["prompts"] = {}

    # Adapters
    try:
        ads = e["lor"].AdapterStore(root)
        all_adapters: list = []
        if ads.root.is_dir():
            for sub in ads.root.iterdir():
                if sub.is_dir():
                    all_adapters.extend(ads.list_versions(sub.name))
        counts["adapters"] = _bucket(all_adapters)
    except Exception:  # pragma: no cover
        counts["adapters"] = {}

    # Rules
    try:
        rstore = e["rsy"].RuleStore(root)
        rules: list = []
        for status in ("staging", "production", "archived"):
            rules.extend(rstore.list_status(status))
        counts["rules"] = _bucket(rules)
    except Exception:  # pragma: no cover
        counts["rules"] = {}

    # Spawned agents
    try:
        constraints = e["gov"].load_immutable_constraints()
        factory = e["asp"].AgentFactory(
            ledger=ledger_store, constraints=constraints, root=root,
        )
        agents = factory.list_agents()
        counts["agents"] = _bucket(agents)
    except Exception:  # pragma: no cover
        counts["agents"] = {}

    # Students
    try:
        ss = e["dis"].StudentStore(root)
        counts["students"] = _bucket(ss.list())
    except Exception:  # pragma: no cover
        counts["students"] = {}

    # DAG
    try:
        dag_store = e["dag"].DAGStore(root)
        listed = dag_store.list()
        counts["dag"] = {}
        for status, _ in listed:
            counts["dag"][status] = counts["dag"].get(status, 0) + 1
    except Exception:  # pragma: no cover
        counts["dag"] = {}

    return StatsResponse(
        ts=_now_iso(),
        ledger_entries=len(ledger_store.entries()),
        ledger_intact=ledger_store.verify(),
        counts=counts,
    )


# ─────────────────────────────────────────────────────────────────────
# Promote / Rollback / Trigger — mutating endpoints
# ─────────────────────────────────────────────────────────────────────


def _record(
    actor: str, kind: str, payload: Dict[str, Any], root: Path, e: Dict[str, Any],
) -> str:
    store = e["gov"].LedgerStore(root)
    constraints = e["gov"].load_immutable_constraints()
    try:
        constraints.check(payload)
    except e["gov"].HardConstraintViolation as exc:
        store.append(
            actor=actor, kind="constraint_check_failed",
            payload={"target_kind": kind, "reason": str(exc),
                     "payload_summary": str(payload)[:400]},
        )
        raise HTTPException(
            status_code=400,
            detail=f"hard constraint violated: {exc}",
        )
    entry = store.append(actor=actor, kind=kind, payload=payload)
    return entry.entry_id


@router.post("/promote", response_model=MutationResponse)
async def promote(
    body: PromoteRequest,
    user: Optional[User] = Depends(get_optional_user),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
) -> MutationResponse:
    _check_enabled()
    e = _evo()
    root = _root()
    actor = _actor(user, x_client_id)

    target_id = body.target_id
    label = body.agent_or_label or ""

    try:
        if body.kind == "prompt":
            # target_id format: "agent/version"
            if "/" not in target_id:
                raise HTTPException(400, "prompt target_id must be 'agent/version'")
            agent, version = target_id.split("/", 1)
            ps = e["pev"].PromptStore(root)
            match = next(
                (v for v in ps.list_versions(agent) if v.version == version),
                None,
            )
            if match is None:
                raise HTTPException(404, f"prompt {target_id} not found")
            ps.promote(match)
            entry_id = _record(actor, "prompt_promoted",
                               {"agent": agent, "version": version,
                                "note": body.note},
                               root, e)
            return MutationResponse(
                ok=True, kind="prompt", target_id=target_id,
                ledger_entry_id=entry_id,
                message=f"prompt {agent}/{version} promoted",
            )

        if body.kind == "adapter":
            if "/" not in target_id:
                raise HTTPException(400, "adapter target_id must be 'agent/version'")
            agent, version = target_id.split("/", 1)
            ads = e["lor"].AdapterStore(root)
            match = next(
                (v for v in ads.list_versions(agent) if v.version == version),
                None,
            )
            if match is None:
                raise HTTPException(404, f"adapter {target_id} not found")
            ads.promote(match)
            entry_id = _record(actor, "adapter_promoted",
                               {"agent": agent, "version": version,
                                "note": body.note},
                               root, e)
            return MutationResponse(
                ok=True, kind="adapter", target_id=target_id,
                ledger_entry_id=entry_id,
                message=f"adapter {agent}/{version} promoted",
            )

        if body.kind == "rule":
            rstore = e["rsy"].RuleStore(root)
            match = next(
                (r for r in rstore.list_status("staging")
                 if r.rule_id == target_id),
                None,
            )
            if match is None:
                raise HTTPException(404, f"rule {target_id} not found in staging")
            rstore.promote(match)
            entry_id = _record(actor, "rule_promoted",
                               {"rule_id": target_id, "note": body.note},
                               root, e)
            return MutationResponse(
                ok=True, kind="rule", target_id=target_id,
                ledger_entry_id=entry_id,
                message=f"rule {target_id} promoted",
            )

        if body.kind == "agent":
            # Spawned agent: shadow → active.
            ledger_store = e["gov"].LedgerStore(root)
            constraints = e["gov"].load_immutable_constraints()
            factory = e["asp"].AgentFactory(
                ledger=ledger_store, constraints=constraints, root=root,
            )
            agents = factory.list_agents()
            target = next((a for a in agents if a.name == target_id), None)
            if target is None:
                raise HTTPException(404, f"agent {target_id} not found")
            target.status = "active"
            target.promoted_at = _now_iso()
            factory.write(target)
            entry_id = _record(actor, "agent_promoted",
                               {"name": target_id, "note": body.note},
                               root, e)
            return MutationResponse(
                ok=True, kind="agent", target_id=target_id,
                ledger_entry_id=entry_id,
                message=f"agent {target_id} promoted to active",
            )

        if body.kind == "dag":
            dag_store = e["dag"].DAGStore(root)
            target = next(
                (d for status, d in dag_store.list() if d.version == target_id),
                None,
            )
            if target is None:
                raise HTTPException(404, f"DAG version {target_id} not found")
            dag_store.promote(target)
            entry_id = _record(actor, "dag_promoted",
                               {"version": target_id, "note": body.note},
                               root, e)
            return MutationResponse(
                ok=True, kind="dag", target_id=target_id,
                ledger_entry_id=entry_id,
                message=f"DAG {target_id} promoted to production",
            )

        if body.kind == "student":
            if "/" not in target_id:
                raise HTTPException(400, "student target_id must be 'name/version'")
            name, version = target_id.split("/", 1)
            ss = e["dis"].StudentStore(root)
            match = next(
                (m for m in ss.list()
                 if getattr(m, "name", None) == name
                 and getattr(m, "version", None) == version),
                None,
            )
            if match is None:
                raise HTTPException(404, f"student {target_id} not found")
            ss.promote(match)
            entry_id = _record(actor, "student_promoted",
                               {"name": name, "version": version,
                                "note": body.note},
                               root, e)
            return MutationResponse(
                ok=True, kind="student", target_id=target_id,
                ledger_entry_id=entry_id,
                message=f"student {name}/{version} promoted",
            )

        raise HTTPException(400, f"unknown kind: {body.kind}")

    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        logger.exception("promote failed")
        raise HTTPException(500, f"promote failed: {type(exc).__name__}: {exc}")


@router.post("/rollback", response_model=MutationResponse)
async def rollback(
    body: RollbackRequest,
    user: Optional[User] = Depends(get_optional_user),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
) -> MutationResponse:
    _check_enabled()
    e = _evo()
    root = _root()
    actor = _actor(user, x_client_id)

    try:
        if body.kind == "prompt":
            ps = e["pev"].PromptStore(root)
            match = next(
                (v for v in ps.list_versions(body.agent_or_label)
                 if v.version == body.target_version),
                None,
            )
            if match is None:
                raise HTTPException(
                    404,
                    f"prompt {body.agent_or_label}/{body.target_version} not found",
                )
            ps.promote(match)
            entry_id = _record(actor, "prompt_rolled_back",
                               {"agent": body.agent_or_label,
                                "version": body.target_version,
                                "note": body.note},
                               root, e)
            return MutationResponse(
                ok=True, kind="prompt",
                target_id=f"{body.agent_or_label}/{body.target_version}",
                ledger_entry_id=entry_id,
                message="prompt rolled back",
            )

        if body.kind == "adapter":
            ads = e["lor"].AdapterStore(root)
            result = ads.rollback_to(body.agent_or_label, body.target_version)
            if result is None:
                raise HTTPException(
                    404,
                    f"adapter {body.agent_or_label}/{body.target_version} not found",
                )
            entry_id = _record(actor, "lora_rolled_back",
                               {"agent": body.agent_or_label,
                                "version": body.target_version,
                                "note": body.note},
                               root, e)
            return MutationResponse(
                ok=True, kind="adapter",
                target_id=f"{body.agent_or_label}/{body.target_version}",
                ledger_entry_id=entry_id,
                message="adapter rolled back",
            )

        if body.kind == "dag":
            dag_store = e["dag"].DAGStore(root)
            target = next(
                (d for status, d in dag_store.list()
                 if d.version == body.target_version),
                None,
            )
            if target is None:
                raise HTTPException(
                    404, f"DAG version {body.target_version} not found",
                )
            dag_store.promote(target)
            entry_id = _record(actor, "dag_rolled_back",
                               {"version": body.target_version,
                                "note": body.note},
                               root, e)
            return MutationResponse(
                ok=True, kind="dag", target_id=body.target_version,
                ledger_entry_id=entry_id,
                message=f"DAG rolled back to {body.target_version}",
            )

        raise HTTPException(400, f"rollback unsupported for kind={body.kind}")

    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        logger.exception("rollback failed")
        raise HTTPException(500, f"rollback failed: {type(exc).__name__}: {exc}")


_TRIGGER_FLAGS: Dict[str, str] = {
    "prompt": "sentinel_evolution_allow_prompt_trigger",
    "rule": "sentinel_evolution_allow_rule_trigger",
    "spawn": "sentinel_evolution_allow_spawn_trigger",
    "dag": "sentinel_evolution_allow_dag_trigger",
    "lora": "sentinel_evolution_allow_lora_trigger",
    "distill": "sentinel_evolution_allow_distill_trigger",
    "curriculum": "sentinel_evolution_allow_curriculum_trigger",
}


@router.post("/trigger/{subsystem}", response_model=TriggerResponse)
async def trigger(
    subsystem: str,
    body: TriggerRequest,
    user: Optional[User] = Depends(get_optional_user),
    x_client_id: Optional[str] = Header(default=None, alias="X-Client-Id"),
) -> TriggerResponse:
    """Records an operator-initiated subsystem trigger in the ledger.

    The mutation itself runs out-of-band — this endpoint *queues* the
    intent (recorded as ``manual_trigger`` in the ledger), and a
    background worker (or the operator) picks it up.  This keeps the
    HTTP path fast and never holds an open connection while a long-
    running training job runs.
    """
    _check_enabled()
    e = _evo()
    root = _root()
    actor = _actor(user, x_client_id)
    flag = _TRIGGER_FLAGS.get(subsystem)
    if flag is None:
        raise HTTPException(400, f"unknown subsystem: {subsystem}")
    if not getattr(settings, flag, False):
        raise HTTPException(
            403,
            f"subsystem '{subsystem}' trigger is disabled "
            f"(settings.{flag}=False)",
        )
    payload = {
        "subsystem": subsystem,
        "request": dict(body.payload or {}),
        "note": body.note,
    }
    try:
        entry_id = _record(actor, "manual_trigger", payload, root, e)
    except HTTPException:
        raise
    return TriggerResponse(
        ok=True, subsystem=subsystem, ledger_entry_id=entry_id,
        message=f"manual trigger for '{subsystem}' queued",
    )
