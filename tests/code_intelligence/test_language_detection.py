"""
Cycle D — language detection tests.

Drives the user request: "kullanıcının belirttiği dili tanımlama en
gelişmiş ve hatasız şekilde olduğundan emin ol".  Three layers of
detection must work in concert:

  1. ``_heuristic_language_override(prompt, current)`` — explicit
     phrases ("in rust", "with kotlin") MUST win over triage's guess.
  2. ``_sniff_language_from_content(code)`` — when the LLM emits a
     mis-fenced or unfenced block, the body's syntax MUST steer the
     runner choice.
  3. The sandbox's ``LANGUAGE_RUNNERS`` / ``TEST_RUNNERS`` keys MUST
     match the enum values — a triage that says "ruby" with no
     matching runner is a runtime crash.
"""

from __future__ import annotations

import pytest

from document_processor.code_intelligence.agents import (
    _heuristic_language_override,
    _sniff_language_from_content,
)
from document_processor.code_intelligence.sandbox import (
    LANGUAGE_RUNNERS,
    TEST_RUNNERS,
)


# ─── Explicit user-language override ─────────────────────────────


class TestExplicitUserLanguage:
    @pytest.mark.parametrize("prompt,expected", [
        ("write a fizzbuzz in python", "python"),
        ("I need a fizzbuzz in rust", "rust"),
        ("write me kotlin code for sorting", "kotlin"),
        ("a kotlin program for fibonacci", "kotlin"),
        ("write a calculator in c++", "cpp"),
        ("a c# script for parsing csv", "csharp"),
        ("with c-sharp do this", "csharp"),
        ("using typescript build a server", "typescript"),
        ("a typescript snippet", "typescript"),
        ("a ruby script for ETL", "ruby"),
        ("with ruby parse the json", "ruby"),
        ("a php script", "php"),
        ("in php output hello", "php"),
        ("write me bash to copy files", "bash"),
        ("with shell loop over files", "bash"),
        ("an SQL query that joins users and orders", "sql"),
        ("a go program for a tcp server", "go"),
        ("a golang server", "go"),
        ("a java program with hashmap", "java"),
        ("plain c implementation of fizzbuzz", "c"),
        ("in c (not c++) write a parser", "c"),
        ("html and css landing page", "html"),
    ])
    def test_explicit_phrase_wins(self, prompt, expected):
        # Triage starts at "python" (the legacy default) but the
        # explicit user phrase MUST override it.
        assert _heuristic_language_override(prompt, "python") == expected

    def test_no_override_when_no_explicit_phrase(self):
        # Generic prompt without language → keep current.
        assert _heuristic_language_override("compute primes", "python") == "python"

    def test_python_framework_blocks_html_override(self):
        # User says "flask" → keep python even with "website" keyword.
        assert _heuristic_language_override(
            "build a website with flask",
            "python",
        ) == "python"

    def test_legacy_frontend_override_still_works(self):
        # Pass-2 fallback for python-default web/HTML.
        assert _heuristic_language_override(
            "snake game website",
            "python",
        ) == "html"


# ─── Content-based sniffer ───────────────────────────────────────


class TestContentSniffer:
    @pytest.mark.parametrize("code,expected", [
        ("<!DOCTYPE html>\n<html><body><h1>x</h1></body></html>", "html"),
        ("<?php\necho 'hi';", "php"),
        ("@import url(...);\n.body { color: red; }", "css"),
        ("body { color: red; }", "css"),
        ("#!/usr/bin/env python\nprint('hi')", "python"),
        ("#!/usr/bin/env node\nconsole.log('hi')", "javascript"),
        ("#!/usr/bin/env ruby\nputs 'hi'", "ruby"),
        ("#!/bin/bash\nset -e\necho hi", "bash"),
        ("fn main() {\n    println!(\"hi\");\n}", "rust"),
        ("package main\nimport \"fmt\"\nfunc main() { fmt.Println(\"hi\") }", "go"),
        ("fun main() {\n    println(\"hi\")\n}", "kotlin"),
        ('public class Main {\n    public static void main(String[] args) { System.out.println("hi"); }\n}\nimport java.util.*;\n', "java"),
        ("#include <iostream>\nint main() { std::cout << \"hi\"; return 0; }", "cpp"),
        ("#include <stdio.h>\nint main(void) { printf(\"hi\"); return 0; }", "c"),
        ("CREATE TABLE users (id INT);\nSELECT * FROM users;", "sql"),
    ])
    def test_sniffer_recognises_strong_signals(self, code, expected):
        assert _sniff_language_from_content(code, fallback="other") == expected

    def test_fallback_on_unrecognisable_input(self):
        assert _sniff_language_from_content("?????", fallback="python") == "python"

    def test_empty_code_returns_fallback(self):
        assert _sniff_language_from_content("", fallback="cpp") == "cpp"


# ─── Sandbox keys ↔ enum coverage ────────────────────────────────


class TestSandboxKeysCoverage:
    """Every language an agent enum can emit MUST be runnable by the
    sandbox.  Otherwise a triage that says e.g. "kotlin" hits an
    "Unsupported language" error inside ``execute()``."""

    EXPECTED_RUNNER_KEYS = {
        "python", "javascript", "typescript", "go", "rust",
        "cpp", "c", "java", "kotlin", "csharp",
        "ruby", "php", "bash", "html", "css", "sql",
    }

    def test_every_enum_lang_has_a_runner(self):
        missing = self.EXPECTED_RUNNER_KEYS - set(LANGUAGE_RUNNERS.keys())
        assert not missing, f"Missing LANGUAGE_RUNNERS for: {missing}"

    def test_test_runners_cover_compiled_dynamic_langs(self):
        # Test runners only exist for languages where running a test
        # file makes sense.  HTML/CSS/SQL/bash are skipped on purpose.
        expected_test_keys = {
            "python", "javascript", "typescript", "go", "rust",
            "cpp", "c", "ruby", "php",
        }
        missing = expected_test_keys - set(TEST_RUNNERS.keys())
        assert not missing, f"Missing TEST_RUNNERS for: {missing}"

    def test_each_test_runner_has_required_fields(self):
        required = {"image", "test_filename", "impl_filename",
                    "test_install_prefix", "test_cmd", "default_timeout_s"}
        for lang, cfg in TEST_RUNNERS.items():
            missing = required - set(cfg.keys())
            assert not missing, f"{lang}: missing fields {missing}"

    def test_each_runner_has_required_fields(self):
        required = {"image", "cmd", "filename", "default_timeout_s"}
        for lang, cfg in LANGUAGE_RUNNERS.items():
            missing = required - set(cfg.keys())
            assert not missing, f"{lang}: missing fields {missing}"
