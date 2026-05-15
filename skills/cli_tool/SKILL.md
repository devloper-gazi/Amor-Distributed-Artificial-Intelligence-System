---
name: cli_tool
description: Build a Python CLI with argparse subcommands, --help, and exit codes
when_to_use:
  - User asks for a "CLI tool" or "command-line script"
  - User wants a Python program with subcommands and flags
  - User mentions argparse, click, or terminal app
languages:
  - python
must_have_features:
  - argparse with at least 2 subcommands (e.g. add / list / done style)
  - --help on the root + each subcommand
  - Documented exit codes (0=ok, 1=user error, 2=fatal)
  - Stderr for errors / progress; stdout for results
  - --version flag
  - Pytest tests via subprocess.run (NOT importing main())
---

# cli_tool — ground rules

Single-file Python script with `if __name__ == "__main__"` gate.
Stdlib-only unless the user specifically requested click / typer.

## Argument parsing

* Root parser with subcommands.  Each subcommand owns its flags.
* `--version` printed via `argparse.action="version"`.
* `--help` is auto-generated; verify it reads naturally.
* `--verbose / -v` (counted) controls logging level.
* Mutually exclusive flags use `add_mutually_exclusive_group()`.

## Exit codes (must document in --help epilog)

| code | meaning |
|---|---|
| 0 | success |
| 1 | user error (bad arg, missing file, etc.) |
| 2 | fatal (unexpected exception, network down) |
| 130 | interrupted (Ctrl-C) |

* Wrap `main()` in a top-level try / except so KeyboardInterrupt
  prints a clean message and exits 130 (not 1).

## Output discipline

* **stdout**: machine-parseable results (one record per line for
  list-style commands; JSON if explicitly `--json`).
* **stderr**: human-readable info / progress / errors.  Always
  prefixed with the program name.
* No `print()` mixed-mode — pick one and stick with it per
  function.

## Logging

* `logging.basicConfig(level=...)` based on `-v` count:
  default WARNING → `-v` INFO → `-vv` DEBUG.
* Format: `"%(levelname)s %(message)s"` for human readability.

## Tests

* Use `subprocess.run([sys.executable, "main.py", ...])` so the
  test exercises the actual argparse path including exit codes.
* DON'T `from main import main` then call it — that bypasses
  `if __name__ == "__main__"` plus the wrapped error handler.

## Anti-patterns

* DON'T use `sys.argv[1:]` manually — always argparse.
* DON'T mix stdout and stderr for one logical message.
* DON'T return non-zero on success path (use bare `return` or
  `return 0`).
* DON'T print Python tracebacks to the user — log them, exit 2.
