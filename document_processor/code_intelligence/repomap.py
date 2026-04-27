"""
RepoMap — workspace symbol/import graph + token-budgeted summary.

Inspired by Aider's tree-sitter PageRank approach. The map walks the
workspace, parses every supported source file with tree-sitter (when
available), extracts top-level definitions and their references,
builds a directed graph in NetworkX, runs personalized PageRank
biased toward files mentioned in the current task, and renders a
token-budgeted summary that fits a configurable target (default
1024 tokens).

Graceful degradation
--------------------
If tree-sitter or its language pack is not installed, we fall back to
a regex-based extractor for Python (function/class/import lines). The
summary is still useful, just less accurate.

If NetworkX is not installed, the PageRank step is skipped — files
are ranked by raw definition count plus a focus-file boost.

Output is always a deterministic markdown block of the form::

    # RepoMap (1024 tokens)
    document_processor/code_intelligence/engine.py
    ├── class CodeIntelligenceEngine
    │   ├── async run() -> dict
    │   └── async _run_phase(name, runner)
    ├── _CODE_EFFORT_BUDGETS
    └── (4 imports)

The engine prepends this block to the Coder, Debugger and Critic
prompts so the LLM has up-to-date structural context.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# Default token estimator — character-count / 4 is a good rough ratio
# for English source code with mixed identifiers.
def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# Languages we attempt to parse (tree-sitter mapping).
_LANG_BY_SUFFIX: Dict[str, str] = {
    ".py":   "python",
    ".js":   "javascript",
    ".jsx":  "javascript",
    ".ts":   "typescript",
    ".tsx":  "tsx",
    ".go":   "go",
    ".rs":   "rust",
    ".rb":   "ruby",
    ".java": "java",
    ".c":    "c",
    ".h":    "c",
    ".cpp":  "cpp",
    ".cc":   "cpp",
    ".hpp":  "cpp",
    ".cs":   "c_sharp",
    ".swift": "swift",
    ".kt":   "kotlin",
    ".php":  "php",
}


@dataclass
class SymbolEntry:
    name: str
    kind: str   # "class" | "function" | "method" | "import" | "constant"
    line: int
    signature: Optional[str] = None


@dataclass
class FileSnapshot:
    path: str
    language: str
    symbols: List[SymbolEntry] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    references: Set[str] = field(default_factory=set)
    sloc: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# Tree-sitter loader — best-effort
# ─────────────────────────────────────────────────────────────────────────────


_TS_AVAILABLE = False
_TS_GET_PARSER: Any = None


def _try_init_tree_sitter() -> bool:
    global _TS_AVAILABLE, _TS_GET_PARSER
    if _TS_AVAILABLE:
        return True
    try:
        from tree_sitter_language_pack import (  # type: ignore[import-not-found]
            get_parser,
        )
        _TS_GET_PARSER = get_parser
        _TS_AVAILABLE = True
        return True
    except ImportError:
        logger.info(
            "repomap_tree_sitter_unavailable falling_back_to_regex"
        )
        return False
    except Exception as exc:  # pragma: no cover
        logger.warning("repomap_tree_sitter_init_failed: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Regex fallback for Python
# ─────────────────────────────────────────────────────────────────────────────


_PY_DEF_RE = re.compile(
    r"^(?P<indent>\s*)(?P<kind>def|async\s+def|class)\s+(?P<name>[A-Za-z_][\w]*)"
)
_PY_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+(?P<from>[\w.]+)\s+import\s+(?P<what>[^#\n]+)|import\s+(?P<mod>[\w., ]+))"
)
_PY_REF_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]+)\b")


def _regex_python_snapshot(path: str, source: str) -> FileSnapshot:
    snap = FileSnapshot(path=path, language="python")
    for i, line in enumerate(source.splitlines(), start=1):
        m = _PY_DEF_RE.match(line)
        if m:
            kind = "class" if m.group("kind").strip() == "class" else "function"
            snap.symbols.append(SymbolEntry(
                name=m.group("name"),
                kind=kind,
                line=i,
                signature=line.strip()[:120],
            ))
            continue
        im = _PY_IMPORT_RE.match(line)
        if im:
            if im.group("from"):
                snap.imports.append(im.group("from"))
            elif im.group("mod"):
                for m_ in im.group("mod").split(","):
                    snap.imports.append(m_.strip())
            continue
    # Reference set: capitalised identifiers used in the body.
    snap.references = set(_PY_REF_RE.findall(source))
    snap.sloc = len([l for l in source.splitlines() if l.strip()])
    return snap


# ─────────────────────────────────────────────────────────────────────────────
# Tree-sitter snapshot
# ─────────────────────────────────────────────────────────────────────────────


def _ts_snapshot(path: str, source: str, language: str) -> FileSnapshot:
    snap = FileSnapshot(path=path, language=language)
    if not _try_init_tree_sitter():
        return _regex_python_snapshot(path, source) if language == "python" \
            else snap
    try:
        parser = _TS_GET_PARSER(language)
        tree = parser.parse(source.encode("utf-8"))
    except Exception:
        return _regex_python_snapshot(path, source) if language == "python" \
            else snap

    src = source.encode("utf-8")

    # Walk the tree with a stack — extract function/class/method
    # definitions plus import-like nodes, by node type.
    def_kinds = {
        "function_definition", "function_declaration",
        "method_definition", "method_declaration",
        "class_definition", "class_declaration",
        "struct_item", "enum_item", "trait_item", "impl_item",
    }
    import_kinds = {
        "import_statement", "import_from_statement",
        "import_declaration", "use_declaration",
    }

    def name_of(node: Any) -> Optional[str]:
        for child in node.children:
            if child.type in {"identifier", "type_identifier",
                              "constant", "name", "field_identifier"}:
                return src[child.start_byte:child.end_byte].decode(
                    "utf-8", errors="replace",
                )
        return None

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in def_kinds:
            n = name_of(node)
            kind = (
                "class" if "class" in node.type else
                "method" if "method" in node.type else
                "function"
            )
            if n:
                snap.symbols.append(SymbolEntry(
                    name=n,
                    kind=kind,
                    line=node.start_point[0] + 1,
                ))
        elif node.type in import_kinds:
            txt = src[node.start_byte:node.end_byte].decode(
                "utf-8", errors="replace",
            )
            snap.imports.append(txt.strip().splitlines()[0][:200])
        # Don't descend into function bodies (signatures only).
        if node.type not in def_kinds:
            stack.extend(node.children)

    snap.sloc = len([l for l in source.splitlines() if l.strip()])
    return snap


# ─────────────────────────────────────────────────────────────────────────────
# RepoMap
# ─────────────────────────────────────────────────────────────────────────────


class RepoMap:
    """Workspace map with optional tree-sitter + PageRank."""

    def __init__(
        self,
        workspace: Path,
        scope: Optional[Iterable[str]] = None,
        gitignore: bool = True,
        max_files: int = 800,
        max_file_size_kb: int = 256,
    ):
        self.workspace = Path(workspace).resolve()
        self.scope = [Path(s) for s in (scope or ["document_processor",
                                                  "web_ui"])]
        self.gitignore = gitignore
        self.max_files = max_files
        self.max_file_size = max_file_size_kb * 1024
        self.snapshots: Dict[str, FileSnapshot] = {}

    # ── Discovery + parsing ───────────────────────────────────────────────

    def _walk_files(self) -> List[Path]:
        roots: List[Path] = []
        for s in self.scope:
            p = (self.workspace / s).resolve()
            if not p.exists():
                continue
            roots.append(p)
        ignored = self._gitignore_set() if self.gitignore else set()
        out: List[Path] = []
        for root in roots:
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in _LANG_BY_SUFFIX:
                    continue
                rel = path.relative_to(self.workspace).as_posix()
                if any(part.startswith(".") and part not in {"."}
                       for part in path.parts):
                    continue
                if any(rel.startswith(ig) for ig in ignored):
                    continue
                try:
                    if path.stat().st_size > self.max_file_size:
                        continue
                except OSError:
                    continue
                out.append(path)
                if len(out) >= self.max_files:
                    return out
        return out

    def _gitignore_set(self) -> Set[str]:
        gi = self.workspace / ".gitignore"
        if not gi.exists():
            return set()
        # Conservative: only honour line-prefix matches, no glob wizardry.
        lines = gi.read_text(encoding="utf-8", errors="replace").splitlines()
        out: Set[str] = set()
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("/"):
                line = line[1:]
            line = line.rstrip("/")
            if line:
                out.add(line)
        return out

    def build(self) -> int:
        """Parse every file in scope. Returns the number of snapshots."""
        files = self._walk_files()
        for f in files:
            try:
                source = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            language = _LANG_BY_SUFFIX.get(f.suffix.lower(), "")
            rel = f.relative_to(self.workspace).as_posix()
            try:
                if language == "python":
                    snap = _regex_python_snapshot(rel, source)
                else:
                    snap = _ts_snapshot(rel, source, language)
            except Exception:
                continue
            self.snapshots[rel] = snap
        logger.info(
            "repomap_built files=%d languages=%d",
            len(self.snapshots),
            len({s.language for s in self.snapshots.values()}),
        )
        return len(self.snapshots)

    # ── Ranking ───────────────────────────────────────────────────────────

    def rank(
        self,
        focus_files: Optional[List[str]] = None,
        boost: float = 50.0,
    ) -> List[Tuple[str, float]]:
        """
        Return (path, score) sorted descending. PageRank when NetworkX
        is available; otherwise a heuristic on symbol count + focus
        boost.
        """
        focus = set(focus_files or [])
        try:
            import networkx as nx  # type: ignore[import-not-found]
        except ImportError:
            return self._heuristic_rank(focus, boost)

        g = nx.DiGraph()
        # Map symbol name → defining file for cheap reference resolution.
        sym_to_file: Dict[str, str] = {}
        for path, snap in self.snapshots.items():
            g.add_node(path)
            for sym in snap.symbols:
                sym_to_file.setdefault(sym.name, path)

        # Edges: A → B when A references a symbol defined in B.
        for path, snap in self.snapshots.items():
            for ref in snap.references:
                target = sym_to_file.get(ref)
                if target and target != path:
                    g.add_edge(path, target)

        if not g.nodes:
            return []

        personalization = {
            n: (boost if n in focus else 1.0) for n in g.nodes
        }
        try:
            scores = nx.pagerank(
                g, personalization=personalization, max_iter=80,
            )
        except Exception:
            return self._heuristic_rank(focus, boost)

        return sorted(scores.items(), key=lambda x: x[1], reverse=True)

    def _heuristic_rank(
        self,
        focus: Set[str],
        boost: float,
    ) -> List[Tuple[str, float]]:
        out: List[Tuple[str, float]] = []
        for path, snap in self.snapshots.items():
            score = float(len(snap.symbols)) + 0.5 * len(snap.imports)
            if path in focus:
                score *= boost
            out.append((path, score))
        out.sort(key=lambda x: x[1], reverse=True)
        return out

    # ── Rendering ─────────────────────────────────────────────────────────

    def repo_map(
        self,
        focus_files: Optional[List[str]] = None,
        budget_tokens: int = 1024,
    ) -> str:
        """Token-budgeted markdown summary of the workspace."""
        if not self.snapshots:
            self.build()
        ranked = self.rank(focus_files=focus_files)

        # Binary-search style fitter — render N top files; if over
        # budget, reduce N until we fit.
        def render_for(n: int) -> str:
            head = ranked[:n]
            blocks: List[str] = [f"# RepoMap ({budget_tokens} tokens)"]
            for path, score in head:
                snap = self.snapshots.get(path)
                if not snap:
                    continue
                blocks.append(self._render_file(snap))
            return "\n".join(blocks).strip() + "\n"

        # Binary search over file count.
        lo, hi = 1, len(ranked)
        best = render_for(lo)
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = render_for(mid)
            if _estimate_tokens(candidate) <= budget_tokens:
                best = candidate
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    @staticmethod
    def _render_file(snap: FileSnapshot) -> str:
        lines = [f"\n{snap.path}"]
        # Group classes first, then top-level functions, then imports.
        classes = [s for s in snap.symbols if s.kind == "class"]
        funcs = [s for s in snap.symbols if s.kind in ("function", "method")]
        for c in classes[:8]:
            lines.append(f"├── class {c.name}")
        for f_ in funcs[:8]:
            sig = f" {f_.signature}" if f_.signature else ""
            prefix = "├── "
            lines.append(f"{prefix}{f_.kind} {f_.name}{sig[:80]}")
        if snap.imports:
            lines.append(f"└── ({len(snap.imports)} imports)")
        return "\n".join(lines)
