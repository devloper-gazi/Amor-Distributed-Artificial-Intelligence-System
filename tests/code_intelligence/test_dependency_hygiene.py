"""
Tests for the install_packages cross-check (Cycle D Fix #3).

Drives the user-reported bug from "a c++ system for user guide":
the sandbox installed `doxygen` + `latex` even though the generated
self-contained C++ code never invoked either.

The new ``CodeIntelligenceEngine._filter_unused_packages`` keeps only
packages whose name appears in actual imports/includes/shell-out
strings of the generated code.
"""

from __future__ import annotations

import pytest

from document_processor.code_intelligence.engine import (
    CodeIntelligenceEngine,
)


# Conveniently rebound to avoid `cls.` boilerplate
filter_unused = CodeIntelligenceEngine._filter_unused_packages


# ─── C++: drop unconditionally if no shell-out ────────────────────


class TestCppDependencyHygiene:
    def test_self_contained_cpp_drops_all_packages(self):
        """The exact user-reported case: doxygen+latex declared but
        the C++ code is a self-contained string formatter."""

        code = (
            "#include <iostream>\n"
            "#include <string>\n"
            "int main() {\n"
            '    std::cout << "User Guide" << std::endl;\n'
            "    return 0;\n"
            "}\n"
        )
        kept, dropped = filter_unused(["doxygen", "latex"], code, "cpp")
        assert kept == []
        assert "doxygen" in dropped and "latex" in dropped

    def test_cpp_with_system_call_keeps_referenced_packages(self):
        code = (
            "#include <cstdlib>\n"
            "int main() {\n"
            '    system("doxygen Doxyfile");\n'
            "    return 0;\n"
            "}\n"
        )
        kept, dropped = filter_unused(["doxygen", "latex"], code, "cpp")
        assert "doxygen" in kept
        assert "latex" in dropped

    def test_cpp_with_no_packages_returns_empty(self):
        kept, dropped = filter_unused([], "int main(){}", "cpp")
        assert kept == [] and dropped == []


# ─── Python: keep packages whose modules are imported ────────────


class TestPythonDependencyHygiene:
    def test_keeps_imported_package(self):
        code = "import requests\nresp = requests.get('https://example.com')\n"
        kept, dropped = filter_unused(["requests"], code, "python")
        assert kept == ["requests"]
        assert dropped == []

    def test_drops_unimported_package(self):
        code = "import requests\nresp = requests.get('x')\n"
        kept, dropped = filter_unused(["requests", "numpy"], code, "python")
        assert "requests" in kept
        assert "numpy" in dropped

    def test_pypi_to_module_mapping(self):
        """beautifulsoup4 → bs4, pillow → PIL, etc."""
        code = "from bs4 import BeautifulSoup\nfrom PIL import Image\n"
        kept, dropped = filter_unused(
            ["beautifulsoup4", "pillow", "numpy"], code, "python",
        )
        assert "beautifulsoup4" in kept
        assert "pillow" in kept
        assert "numpy" in dropped

    def test_pinned_version_kept_intact(self):
        code = "import flask\napp = flask.Flask(__name__)\n"
        kept, dropped = filter_unused(["flask==3.0.0"], code, "python")
        assert kept == ["flask==3.0.0"]
        assert dropped == []


# ─── JS/TS: keep packages whose names appear in require/import ────


class TestJavaScriptDependencyHygiene:
    def test_keeps_required_package(self):
        code = 'const _ = require("lodash");\n_.shuffle([1,2,3]);'
        kept, dropped = filter_unused(["lodash", "react"], code, "javascript")
        assert "lodash" in kept
        assert "react" in dropped

    def test_keeps_es_imported_package(self):
        code = 'import express from "express";\nimport React from "react";'
        kept, dropped = filter_unused(["express", "react", "axios"], code, "javascript")
        assert "express" in kept
        assert "react" in kept
        assert "axios" in dropped

    def test_typescript_same_logic(self):
        code = 'import { foo } from "lodash";'
        kept, dropped = filter_unused(["lodash", "axios"], code, "typescript")
        assert "lodash" in kept
        assert "axios" in dropped


# ─── Other languages: pass-through ───────────────────────────────


class TestUnknownLanguagePassthrough:
    def test_rust_passthrough(self):
        kept, dropped = filter_unused(["serde"], "fn main() {}", "rust")
        assert kept == ["serde"]
        assert dropped == []

    def test_empty_code_passthrough(self):
        kept, dropped = filter_unused(["x"], "", "python")
        assert kept == ["x"]
        assert dropped == []
