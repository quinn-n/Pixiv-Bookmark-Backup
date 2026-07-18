---
name: python-quality
description: Standards and the automated quality gate for this Python project — black formatting, flake8 (and plugins) linting, pytest testing, type hints/mypy, and writing clean, testable code. Use when writing, reviewing, refactoring, or testing Python.
---

# Python quality standards

The bar every change must clear, plus how to write code that clears it easily.

## The quality gate (run via pixi)
- `pixi run fmt` — **black** formats the code. Black owns formatting; never hand-format or argue with it.
- `pixi run fmt-check` — `black --check --diff`; CI/review use this.
- `pixi run lint` — **flake8** (+ plugins like flake8-bugbear, flake8-comprehensions, isort if configured). Fix issues; reserve `# noqa: <code>` for rare, justified cases with the specific code and a reason.
- `pixi run typecheck` — **Pyright** type checking; keep it green. (Pylance is Microsoft's closed-source VS Code extension built on Pyright and has no standalone CLI — Pyright *is* the engine behind Pylance, so the editor's Pylance diagnostics and this CLI gate agree. We install it as `basedpyright`, a pip/conda-forge-packaged build of Pyright that needs no Node.js; the task runs `basedpyright`. If the project also uses mypy, run it too and keep both green.)
- `pixi run test` — **pytest** (with coverage where set up).
- `pixi run check` — the aggregate (fmt-check + lint + typecheck + test). **Green `check` is the definition of "ready".** `typecheck` runs Pyright (via `basedpyright`).

Locally, run these as a good neighbor — prefix the runner with the OS low-priority wrapper so the gate doesn't lag the machine (e.g. `nice -n 10 pixi run check`), full-speed in CI. See the `low-priority-execution` skill.

### black + flake8 harmony
flake8 must not fight black. Set flake8 `max-line-length` to black's (88) and ignore the rules black handles (e.g. `E203`, `W503`). Keep this in `.flake8`/`setup.cfg`/`[tool.flake8]` so they never conflict.

## Writing clean Python
- **Type hints** on public functions; precise types over `Any`; `Optional[T]` where `None` is valid. Use `Enum`/`Literal` for fixed choices. **For data/config/value classes use `attrs` (`@define`/`@frozen` + `field()`) as the project default** rather than hand-written classes or `@dataclass` — see the `attrs-classes` skill. Pyright understands attrs-generated `__init__`s, so keep field annotations precise.
- **Docstrings** on public modules/functions/classes — what it does, args, returns, raises.
- **Pythonic idioms:** `pathlib` over `os.path`; context managers (`with`) for files/resources; comprehensions/generators when clearer; `enumerate`/`zip`; f-strings; early returns over deep nesting.
- **Avoid the traps:** mutable default arguments (`def f(x=[])`), late-binding closures in loops, bare `except:`, swallowing exceptions, broad `except Exception` without re-raise/log, global mutable state.
- **Separation of concerns:** keep the click layer thin (parse/validate/format) and put logic in plain functions/classes that are testable without invoking the CLI.

## Testing standards (pytest)
- Test **behavior and contracts**, not internals. Name tests for scenario + expected outcome.
- Use fixtures, `tmp_path` (never write into the repo/home), `monkeypatch` (env, cwd, time), and `@pytest.mark.parametrize` for input matrices.
- Test commands with `click.testing.CliRunner`: assert output, **exit code**, and error text; cover `--help`, bad args, and pipe/no-TTY paths. Use `subprocess` + `tmp_path` for true end-to-end when needed.
- Assert raised errors with `pytest.raises`. Keep tests deterministic and isolated: no real network, no real clock, no order dependence.
- Don't test third-party libraries; test your usage. Prefer targeted asserts over giant snapshot blobs.

## Dependencies
Add through pixi, not by editing lockfiles: `pixi add <pkg>` (conda-forge, preferred) or `pixi add --pypi <pkg>`; dev/test tools go on the `dev`/`test` feature. Prefer conda-forge builds where they exist; reach for PyPI only when necessary. Keep `pixi.lock` committed for reproducibility.

## Definition of done
Acceptance criteria pass in pytest *and* when running the real command; `pixi run check` is green (including Pyright type checking); public APIs are typed and documented; data/config classes use attrs; security review is clean; no Must-fix review items; new deps are added via pixi to the right feature.
