"""
Tests for the C++-specific coder pre-validation: missing-include
detection + forward-declaration injection.

Drives the user-reported bug class from "a c++ system for user guide":
  - Iteration 1: missing #include <functional> for std::function
  - Iteration 2: map literal references functions defined later

Both classes must now be caught BEFORE sandbox.execute() runs, so the
debug loop is shorter or unnecessary.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from document_processor.code_intelligence.agents import (
    CoderAgent,
    AgentContext,
    _validate_cpp_includes,
    _detect_cpp_forward_ref,
    _inject_cpp_forward_decls,
)
from document_processor.code_intelligence import prompts as P


# ─── _validate_cpp_includes ───────────────────────────────────────


class TestValidateCppIncludes:
    def test_noop_when_no_std_usage(self):
        code = "int main() { return 0; }"
        patched, added = _validate_cpp_includes(code)
        assert added == []
        assert patched == code

    def test_injects_missing_functional(self):
        code = (
            "#include <iostream>\n\n"
            "int main() {\n"
            "    std::function<int(int)> f = [](int x) { return x; };\n"
            "    std::cout << f(5) << std::endl;\n"
            "    return 0;\n"
            "}\n"
        )
        patched, added = _validate_cpp_includes(code)
        assert "<functional>" in added
        assert "#include <functional>" in patched

    def test_injects_multiple_missing(self):
        code = (
            "int main() {\n"
            "    std::vector<std::string> v;\n"
            "    std::shared_ptr<int> p;\n"
            "    std::cout << v.size() << std::endl;\n"
            "    return 0;\n"
            "}\n"
        )
        patched, added = _validate_cpp_includes(code)
        assert "<vector>" in added
        assert "<string>" in added
        assert "<memory>" in added
        assert "<iostream>" in added
        # All four headers appear in the patched code
        for h in ("<vector>", "<string>", "<memory>", "<iostream>"):
            assert f"#include {h}" in patched

    def test_does_not_duplicate_existing_headers(self):
        code = (
            "#include <vector>\n"
            "#include <iostream>\n\n"
            "int main() {\n"
            "    std::vector<int> v;\n"
            "    std::cout << v.size();\n"
            "    return 0;\n"
            "}\n"
        )
        patched, added = _validate_cpp_includes(code)
        assert added == []
        # No duplication
        assert patched.count("#include <vector>") == 1
        assert patched.count("#include <iostream>") == 1

    def test_handles_empty_code(self):
        patched, added = _validate_cpp_includes("")
        assert added == []
        assert patched == ""


# ─── _detect_cpp_forward_ref + _inject_cpp_forward_decls ─────────


class TestForwardRefDetection:
    def test_detects_forward_ref_in_map_literal(self):
        code = (
            "#include <iostream>\n"
            "#include <unordered_map>\n"
            "#include <functional>\n\n"
            "void useMap() {\n"
            '    std::unordered_map<std::string, std::function<void()>> handlers = {\n'
            '        {"a", handlerA},\n'
            '        {"b", handlerB},\n'
            "    };\n"
            "}\n\n"
            "void handlerA() { std::cout << \"A\"; }\n"
            "void handlerB() { std::cout << \"B\"; }\n"
        )
        names = _detect_cpp_forward_ref(code)
        assert "handlerA" in names
        assert "handlerB" in names

    def test_no_forward_ref_when_definitions_are_above(self):
        code = (
            "void handlerA() {}\n"
            "void handlerB() {}\n"
            'std::unordered_map<std::string, void(*)()> m = {{"a", handlerA}, {"b", handlerB}};\n'
        )
        names = _detect_cpp_forward_ref(code)
        assert names == []

    def test_inject_forward_decls_renders_decls_above_first_fn(self):
        code = (
            "#include <iostream>\n\n"
            'auto m = std::unordered_map<int, int>{{1, helper(2)}};\n\n'
            "int helper(int x) { return x * 2; }\n"
        )
        patched = _inject_cpp_forward_decls(code, ["helper"])
        # Forward declaration block appears BEFORE the function definition
        decl_pos = patched.find("int helper(int x);")
        defn_pos = patched.find("int helper(int x) {")
        assert decl_pos != -1, "forward decl was not injected"
        assert defn_pos != -1
        assert decl_pos < defn_pos


# ─── CoderAgent.run() integration ─────────────────────────────────


class _FakeLLM:
    def __init__(self, response: str):
        self.response = response

    async def __call__(self, prompt: str, system: str, max_tokens: int) -> str:
        return self.response


@pytest.mark.asyncio
async def test_coder_agent_auto_adds_missing_includes_for_cpp():
    """When coder output uses std::function without <functional>, the
    CoderAgent post-validator injects the missing header automatically."""

    raw = """```cpp
#include <iostream>

int main() {
    std::function<int(int)> f = [](int x) { return x * 2; };
    std::cout << f(21) << std::endl;
    return 0;
}
```
```json
{"language": "cpp", "filename": "main.cpp", "dependencies": []}
```"""

    agent = CoderAgent(llm_call=_FakeLLM(raw))
    out = await agent.run(AgentContext(
        user_prompt="lambda demo",
        plan={"language": "cpp"},
        language="cpp",
    ))
    assert out.error is None
    assert "#include <functional>" in (out.code or "")
    assert any(
        "auto-added missing std headers" in w
        for w in out.data.get("coder_auto_fixes", [])
    )


@pytest.mark.asyncio
async def test_coder_agent_passthrough_for_python():
    """Non-C++ output is NOT touched by the C++ validator."""

    raw = """```python
import sys

def main():
    print("hello")

if __name__ == "__main__":
    main()
```
```json
{"language": "python", "filename": "main.py", "dependencies": []}
```"""

    agent = CoderAgent(llm_call=_FakeLLM(raw))
    out = await agent.run(AgentContext(
        user_prompt="hello world",
        plan={"language": "python"},
        language="python",
    ))
    assert out.error is None
    assert out.data.get("coder_auto_fixes") == []


# ─── prompt-level: C++ ground rules injected ─────────────────────


def test_cpp_ground_rules_injected_for_cpp_plan():
    prompt = P.coder_prompt(
        "build a c++ thing",
        plan={"language": "cpp"},
    )
    assert "C++ ground rules" in prompt
    assert "std::function" in prompt
    assert "Define every function BEFORE its first use" in prompt


def test_cpp_ground_rules_omitted_for_python_plan():
    prompt = P.coder_prompt(
        "build a python thing",
        plan={"language": "python"},
    )
    assert "C++ ground rules" not in prompt
