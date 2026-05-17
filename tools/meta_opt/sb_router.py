#!/usr/bin/env python3
"""Cycle K.1 — Simulated Bifurcation meta-optimizer for skill scheduling.

Toshiba's Simulated Bifurcation algorithm (Goto et al. 2019) solves
Ising / QUBO problems via a coupled Hamiltonian system that exhibits
bifurcation behaviour analogous to quantum annealing — without the
quantum hardware.  On classical CPU it scales to ~1000 spins in
seconds, and beats greedy + CMA-ES on combinatorial scheduling
benchmarks where the search space has many local minima.

AMOR uses SB to schedule SKILLS (Anthropic Agent Skills, Cycle F
Sprint 4) when many skills compete for a limited prompt-token
budget under multiple soft constraints:

  * each skill has a token-cost ``c_i``
  * each skill has a per-mode utility ``u_i`` (game, web-app, etc.)
  * pairwise affinity ``a_ij`` rewards co-activating related skills
  * pairwise conflict ``-c_ij`` punishes contradictory pairs
  * total budget ``B`` capped by ``settings.code_skills_token_budget``

Plan-agent locked deferral: SB requires populated skill library
(≥24 skills) AND routing telemetry to formulate QUBOs that beat
greedy.  AMOR currently has 8 skills.  This module ships the
SOLVER + SCHEDULING ABSTRACTION ahead of skill-library growth so
the operator can flip ``code_sb_meta_opt_enabled=True`` when the
library passes 24 skills WITHOUT another code drop.

Files this provides:
  * ``SkillScheduleQUBO`` — builds the QUBO matrix from a skill
    catalogue + per-skill metadata
  * ``solve_skill_schedule`` — wraps simulated_bifurcation.minimize
    so callers get a list of activated skill IDs back
  * CLI surface (``python -m tools.meta_opt.sb_router``) — dry-run
    over the existing ``skills/*/SKILL.md`` catalogue

Plan-agent reuse pin: route SB decisions through
``ApprovalPolicy.PROMPT`` category for operator audit.  This module
intentionally only RETURNS the schedule; the route layer that
calls it is responsible for the approval round-trip.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        _r = getattr(_s, "reconfigure", None)
        if _r is not None:
            try:
                _r(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

logger = logging.getLogger(__name__)


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ─── Inputs ─────────────────────────────────────────────────────────


@dataclass
class SkillEntry:
    """A single candidate skill for scheduling."""
    skill_id: str
    token_cost: int                   # estimated tokens skill adds to prompt
    utility: float                    # per-mode utility (0..1)
    affinity_with: Dict[str, float] = field(default_factory=dict)
    conflict_with: Dict[str, float] = field(default_factory=dict)
    description: str = ""


@dataclass
class ScheduleResult:
    activated: List[str]
    total_cost: int
    total_utility: float
    objective_value: float
    wall_clock_s: float
    skills_evaluated: int


# ─── QUBO builder ───────────────────────────────────────────────────


class SkillScheduleQUBO:
    """Builds the symmetric QUBO matrix for skill scheduling.

    Decision variable ``x_i ∈ {0, 1}`` ≙ activate skill i.

    Objective (MINIMISE — flip sign for utility):
        - Σ u_i · x_i              # reward utility
        + Σ p_budget · max(0, Σ c_i x_i − B)
        - Σ_{i<j} a_ij · x_i x_j   # reward affinity
        + Σ_{i<j} c_ij · x_i x_j   # punish conflict

    The budget constraint is encoded as a quadratic penalty:
        p · (Σ c_i x_i − B)^2
    which expands to:
        p · (Σ c_i^2 x_i + 2Σ_{i<j} c_i c_j x_i x_j
             − 2B Σ c_i x_i + B^2)
    The constant B^2 drops out for the optimiser; everything else
    becomes diagonal / off-diagonal entries of Q.
    """

    def __init__(
        self,
        skills: Sequence[SkillEntry],
        *,
        budget: int = 2000,
        budget_penalty: float = 0.01,
        utility_weight: float = 1.0,
        affinity_weight: float = 0.5,
        conflict_weight: float = 1.0,
    ) -> None:
        if not skills:
            raise ValueError("at least one skill required")
        self.skills = list(skills)
        self._id_to_idx = {s.skill_id: i for i, s in enumerate(self.skills)}
        self.budget = int(budget)
        self.budget_penalty = float(budget_penalty)
        self.utility_weight = float(utility_weight)
        self.affinity_weight = float(affinity_weight)
        self.conflict_weight = float(conflict_weight)

    @property
    def n(self) -> int:
        return len(self.skills)

    def build(self) -> List[List[float]]:
        """Construct the symmetric QUBO matrix (Python list of lists).

        Returns Q such that the optimiser minimises ``x^T Q x``.
        Off-diagonals are symmetric: Q[i][j] == Q[j][i] == coef/2
        each (so x^T Q x gives `coef · x_i · x_j` once)."""
        n = self.n
        Q = [[0.0 for _ in range(n)] for _ in range(n)]

        # Diagonal: utility reward (negate to minimise) + budget
        # linear/quadratic contributions.
        for i, s in enumerate(self.skills):
            # Linear utility term — flip sign so we MINIMISE negative utility.
            Q[i][i] += -self.utility_weight * s.utility
            # Budget quadratic expansion: c_i^2 - 2 B c_i, scaled by penalty.
            Q[i][i] += self.budget_penalty * (
                (s.token_cost ** 2) - 2.0 * self.budget * s.token_cost
            )

        # Off-diagonals: budget cross-term + affinity (reward) + conflict (penalty).
        for i in range(n):
            for j in range(i + 1, n):
                ci, cj = self.skills[i].token_cost, self.skills[j].token_cost
                # Budget cross-term: 2 c_i c_j x_i x_j ⇒ coef = 2 p c_i c_j
                cross = 2.0 * self.budget_penalty * ci * cj
                affinity = self.skills[i].affinity_with.get(self.skills[j].skill_id, 0.0)
                if affinity == 0.0:
                    affinity = self.skills[j].affinity_with.get(self.skills[i].skill_id, 0.0)
                conflict = self.skills[i].conflict_with.get(self.skills[j].skill_id, 0.0)
                if conflict == 0.0:
                    conflict = self.skills[j].conflict_with.get(self.skills[i].skill_id, 0.0)
                coef = (
                    cross
                    - self.affinity_weight * affinity
                    + self.conflict_weight * conflict
                )
                # Split symmetrically — x^T Q x then evaluates to coef·x_i·x_j.
                Q[i][j] += coef / 2.0
                Q[j][i] += coef / 2.0

        return Q

    def evaluate(self, activated: Sequence[int]) -> float:
        """Re-compute x^T Q x for a candidate solution.  Useful for
        sanity checks + comparing solvers."""
        Q = self.build()
        n = self.n
        x = [1.0 if i in set(activated) else 0.0 for i in range(n)]
        s = 0.0
        for i in range(n):
            for j in range(n):
                s += Q[i][j] * x[i] * x[j]
        return s


# ─── Solver wrapper ─────────────────────────────────────────────────


def solve_skill_schedule(
    qubo: SkillScheduleQUBO,
    *,
    max_steps: int = 10000,
    use_sb: bool = True,
) -> ScheduleResult:
    """Solve the schedule QUBO via simulated_bifurcation.minimize.

    When ``use_sb=False``, falls back to a deterministic greedy
    heuristic — used by the bench harness to compare SB vs greedy
    (Plan-agent acceptance: SB must beat greedy on a 32-skill
    benchmark).
    """
    started = time.perf_counter()
    n = qubo.n
    Q = qubo.build()

    if use_sb:
        try:
            import simulated_bifurcation as sb       # noqa: PLC0415
            import numpy as np                       # noqa: PLC0415
            best_x, best_obj = sb.minimize(
                np.array(Q),
                input_type="binary",
                max_steps=max_steps,
                agents=4,
                heated=False,
                verbose=False,
            )
            # `best_x` is a torch.Tensor; coerce to a plain list of 0/1.
            try:
                xs = best_x.cpu().numpy().astype(int).tolist()
            except AttributeError:
                xs = list(best_x)
            objective_value = float(best_obj)
        except Exception as exc:
            logger.warning("SB solve failed (%s); falling back to greedy", exc)
            xs, objective_value = _greedy_solve(qubo)
    else:
        xs, objective_value = _greedy_solve(qubo)

    activated_ids: List[str] = []
    total_cost = 0
    total_utility = 0.0
    for i, on in enumerate(xs):
        if on:
            s = qubo.skills[i]
            activated_ids.append(s.skill_id)
            total_cost += s.token_cost
            total_utility += s.utility

    return ScheduleResult(
        activated=activated_ids,
        total_cost=total_cost,
        total_utility=total_utility,
        objective_value=objective_value,
        wall_clock_s=time.perf_counter() - started,
        skills_evaluated=n,
    )


def _greedy_solve(qubo: SkillScheduleQUBO) -> Tuple[List[int], float]:
    """Plan-agent acceptance comparator: greedy = sort skills by
    utility / token_cost ratio + activate in that order until budget
    exhausted.  Beats random; SB must beat THIS to justify its dep."""
    sorted_idx = sorted(
        range(qubo.n),
        key=lambda i: -(qubo.skills[i].utility / max(qubo.skills[i].token_cost, 1)),
    )
    chosen: List[int] = [0] * qubo.n
    used = 0
    for i in sorted_idx:
        c = qubo.skills[i].token_cost
        if used + c <= qubo.budget:
            chosen[i] = 1
            used += c
    obj = qubo.evaluate([i for i, x in enumerate(chosen) if x])
    return chosen, obj


# ─── Skill catalogue loader (real AMOR skills/*/SKILL.md) ──────────


def load_skill_catalogue(skills_root: Path) -> List[SkillEntry]:
    """Walk ``skills/*/SKILL.md`` and build a SkillEntry list.

    Frontmatter keys consulted:
      * ``name``  / dir name → skill_id
      * ``token_cost``  (default 200 — typical skill body size)
      * ``utility``  (default 0.5 — neutral)
      * ``affinity_with``  / ``conflict_with``  (dict-of-floats)
    """
    if not skills_root.is_dir():
        return []
    skills: List[SkillEntry] = []
    for skill_dir in sorted(skills_root.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        try:
            end = text.find("---", 3)
            front = text[3:end].strip() if end > 0 else ""
        except Exception:
            front = ""
        meta: Dict[str, Any] = {}
        for line in front.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        skills.append(SkillEntry(
            skill_id=meta.get("name") or skill_dir.name,
            token_cost=int(meta.get("token_cost", 200) or 200),
            utility=float(meta.get("utility", 0.5) or 0.5),
            description=meta.get("description", ""),
        ))
    return skills


# ─── CLI ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--skills-root", default=str(_REPO_ROOT / "skills"),
        help="root dir holding skills/*/SKILL.md (default ./skills/)",
    )
    p.add_argument("--budget", type=int, default=2000,
                   help="prompt-token budget for combined skills")
    p.add_argument("--budget-penalty", type=float, default=0.001)
    p.add_argument("--affinity-weight", type=float, default=0.5)
    p.add_argument("--conflict-weight", type=float, default=1.0)
    p.add_argument("--max-steps", type=int, default=10000)
    p.add_argument("--greedy", action="store_true",
                   help="skip SB; run the greedy comparator only")
    p.add_argument("--json", action="store_true",
                   help="emit ScheduleResult JSON to stdout")
    p.add_argument("--dry-run", action="store_true",
                   help="print loaded catalogue + QUBO size + exit")
    return p


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args()
    skills = load_skill_catalogue(Path(args.skills_root))
    if not skills:
        logger.error("no skills loaded from %s", args.skills_root)
        return 1
    logger.info("loaded %d skills", len(skills))
    if args.dry_run:
        for s in skills:
            print(f"  {s.skill_id} (cost={s.token_cost}, util={s.utility:.2f})")
        return 0

    qubo = SkillScheduleQUBO(
        skills,
        budget=args.budget,
        budget_penalty=args.budget_penalty,
        affinity_weight=args.affinity_weight,
        conflict_weight=args.conflict_weight,
    )
    result = solve_skill_schedule(
        qubo,
        max_steps=args.max_steps,
        use_sb=not args.greedy,
    )
    payload = {
        "activated": result.activated,
        "total_cost": result.total_cost,
        "total_utility": round(result.total_utility, 4),
        "objective_value": round(result.objective_value, 4),
        "wall_clock_s": round(result.wall_clock_s, 3),
        "skills_evaluated": result.skills_evaluated,
        "solver": "greedy" if args.greedy else "simulated_bifurcation",
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Solver: {payload['solver']}")
        print(f"Activated {len(result.activated)} of {result.skills_evaluated} skills:")
        for s in result.activated:
            print(f"  - {s}")
        print(f"Total cost: {result.total_cost} / {args.budget} budget")
        print(f"Total utility: {result.total_utility:.3f}")
        print(f"Objective: {result.objective_value:.3f}")
        print(f"Wall: {result.wall_clock_s:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
