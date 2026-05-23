"""Live verification script for Cycle D Build pipeline fixes.

Reproduces the exact bugs from the user's "a c++ system for user guide"
output and confirms each fix catches them.  Run inside the running
container so it sees the deployed source.
"""
import asyncio

from document_processor.code_intelligence.engine import CodeIntelligenceEngine
from document_processor.code_intelligence.agents import (
    CriticAgent,
    AgentContext,
    _validate_cpp_includes,
    _detect_cpp_forward_ref,
    _inject_cpp_forward_decls,
)


# ─── Fix #1 — Coder C++ awareness ─────────────────────────────────

# Reproduce the user's iteration-1 buggy code (missing #include <functional>)
buggy_v1 = """#include <iostream>
#include <string>
#include <unordered_map>
#include <stdexcept>

void generateGuide(const std::string& format, const std::string& content) {
    std::unordered_map<std::string, std::function<std::string(const std::string&)>> formatters = {
        {"md", formatMarkdown},
        {"latex", formatLatex}
    };
}

std::string formatMarkdown(const std::string& content) { return content; }
std::string formatLatex(const std::string& content) { return content; }
int main() { return 0; }
"""

print("=== FIX 1A — Missing #include detection ===")
patched, added = _validate_cpp_includes(buggy_v1)
print(f"Headers added: {added}")
assert "<functional>" in added
assert "#include <functional>" in patched
print("PASS — auto-injected <functional> [iteration-1 bug fixed at coder time]\n")


print("=== FIX 1B — Forward declaration detection ===")
forward_refs = _detect_cpp_forward_ref(buggy_v1)
print(f"Forward refs detected: {forward_refs}")
assert "formatMarkdown" in forward_refs and "formatLatex" in forward_refs
patched2 = _inject_cpp_forward_decls(buggy_v1, forward_refs)
assert "std::string formatMarkdown(const std::string& content);" in patched2
print("PASS — auto-injected forward decls [iteration-2 bug fixed at coder time]\n")


# ─── Fix #3 — install_packages cross-check ────────────────────────

print("=== FIX 3 — install_packages cross-check ===")
self_contained_cpp = """#include <iostream>
#include <string>
int main() { std::cout << "User Guide" << std::endl; return 0; }
"""
kept, dropped = CodeIntelligenceEngine._filter_unused_packages(
    ["doxygen", "latex"], self_contained_cpp, "cpp",
)
print(f"User's exact case (self-contained C++) — kept: {kept}, dropped: {dropped}")
assert kept == [] and "doxygen" in dropped and "latex" in dropped
print("PASS — doxygen/latex correctly dropped — no wasted install\n")


# ─── Fix #5 — Plan-to-spec extraction ─────────────────────────────

print("=== FIX 5 — Plan-to-spec extraction ===")
plan = {
    "language": "cpp",
    "steps": ["use Doxygen / Sphinx / custom solution"],
    "spec": {
        "signatures": [
            "std::unordered_map<std::string, std::function<void()>> getHandlers()",
            "std::vector<int> compute(std::shared_ptr<Config> cfg)",
        ],
    },
}
out = CodeIntelligenceEngine._extract_focused_spec(plan, "cpp")
suggested = out["focused_spec"]["suggested_includes"]
print(f"Suggested includes from spec signatures: {sorted(suggested)}")
expected = {"<unordered_map>", "<functional>", "<string>", "<vector>", "<memory>"}
assert expected <= set(suggested), f"missing {expected - set(suggested)}"
print("PASS — abstract Doxygen/Sphinx plan grounded with concrete STL headers\n")


# ─── Fix #4 — Verdict-severity coherence guard ────────────────────

print("=== FIX 4 — Verdict-severity coherence guard ===")

critic_json = (
    '{"verdict": "approved_with_minor", "score": 92, '
    '"strengths": [], '
    '"issues": [{"severity": "major", '
    '"description": "Missing dependency checks for required tools", '
    '"suggestion": "add tool checks"}], '
    '"security_concerns": [], "performance_concerns": [], '
    '"final_comment": "minor issues only"}'
)


class _FakeLLM:
    async def __call__(self, prompt: str, system: str, max_tokens: int) -> str:
        return critic_json


async def _run_critic():
    agent = CriticAgent(llm_call=_FakeLLM())
    return await agent.run(AgentContext(
        user_prompt="x", code="int main(){}", language="cpp",
    ))


out = asyncio.run(_run_critic())
print(f"Input verdict: approved_with_minor + major issue")
print(f"Output verdict: {out.data['verdict']}")
print(f"Auto-corrected flag: {out.data.get('verdict_auto_corrected')}")
assert out.data["verdict"] == "needs_revision"
assert out.data.get("verdict_auto_corrected") is True
print("PASS — verdict downgraded to needs_revision (major present)\n")


print("=" * 60)
print("ALL 4 CYCLE-D FIXES VERIFIED LIVE ON DEPLOYED BACKEND.")
print("Each one targets the exact bug class from the user's")
print('"a c++ system for user guide" Build run.')
print("=" * 60)
