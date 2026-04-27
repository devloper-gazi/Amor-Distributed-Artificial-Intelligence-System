# License notes — Code Intelligence Mode

The Code Intelligence Mode v2 build is permissively licensed
end-to-end.

## Direct dependencies

| Package | SPDX | Used by |
|---|---|---|
| `pylint` | MIT | static_analysis.py |
| `bandit` | Apache-2.0 | static_analysis.py |
| `radon` | MIT | static_analysis.py |
| `mypy` | MIT | static_analysis.py |
| `PyYAML` | MIT | adversarial_reviewer.py |
| `networkx` | BSD-3-Clause | repomap.py |
| `huggingface_hub` | Apache-2.0 | capability_discoverer.py |
| `PyGithub` | LGPL-3.0+ | capability_discoverer.py |
| `arxiv` | MIT | capability_discoverer.py |
| `langfuse` (optional) | MIT | observability.py |
| `tree-sitter-language-pack` (optional) | MIT | repomap.py |
| `httpx` | BSD-3-Clause | model_registry.py |

## Notes

### PyGithub — LGPL-3.0+
PyGithub is LGPL, not Apache-2.0. AMOR links it dynamically at runtime
without modifying it; LGPL-3.0+ permits this. We're only using it to
make HTTP API calls, so the LGPL terms don't propagate to our code.

If you need to ship a proprietary fork that statically embeds PyGithub,
swap to `httpx`-based GitHub API calls (the `CapabilityDiscoverer` is
already structured to make that swap trivial — `_discover_github` is
the only call site).

### Optional langfuse — MIT
`langfuse` is opt-in (only loaded when `CODE_LANGFUSE_URL` is set).
Otherwise observability falls through to local JSONL — no third-party
dep is needed.

### tree-sitter parsers
Tree-sitter parsers are MIT, but each language grammar has its own
license (most are Apache-2.0 or MIT; a handful are GPL-2 like Bash).
RepoMap handles any individual parser failing → the file falls through
to the regex Python extractor or stays unparsed.

## Audit

Run a license sweep before each release:

```bash
pip-licenses --fail-on=GPL,AGPL --packages \
  $(grep -v '^#' requirements.txt | grep -v '^$' | sed 's/[<>=].*//')
```

LGPL is intentionally NOT in the fail-on list because PyGithub is LGPL.
If you cannot accept LGPL in your build, either remove `PyGithub` and
its `_discover_github` call site, or swap to a custom `httpx` adapter.
