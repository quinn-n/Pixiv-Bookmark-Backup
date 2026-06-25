---
description: Bootstrap a new pixi-managed Python CLI project with click, pytest, black, flake8, Pyright type-checking, and attrs wired up.
argument-hint: <project name and one-line purpose>
---

Bootstrap a new Python CLI project: **$ARGUMENTS**

Set it up with the project's standard toolchain. Confirm versions against current docs before pinning (use the `fact-checker` if unsure), then:
1. `pixi init --format pyproject` (or `pixi init`), set channels to `conda-forge`, and add the target platforms.
2. Add runtime deps: `pixi add python click attrs` (attrs is the project default for writing classes). Add a `dev` feature with test/quality tools: `pixi add --feature dev pytest pytest-cov black flake8 basedpyright` (add `mypy`, `bandit`, `pip-audit`, `flake8-bugbear` if I want them), and create a `dev` environment from that feature. Note: there is no `pylance` package — Pylance is the closed-source VS Code extension built on Pyright; `basedpyright` is the CLI build of that same engine (no Node.js required), so editor and CLI diagnostics agree.
3. Define pixi **tasks**: `fmt` (`black .`), `fmt-check` (`black --check --diff .`), `lint` (`flake8`), `typecheck` (`basedpyright`), `test` (`pytest`), `audit` (`pip-audit`)/`bandit` if added, and `check` (depends-on fmt-check, lint, typecheck, test). Add `typecheck-mypy` (`mypy src`) if mypy is included.
4. Create the package layout: a `src/<pkg>/` package, a `cli.py` with a `click` group exposing `--version` and a sample command, at least one `attrs` `@define`/`@frozen` config/parameter class used by that command (to set the pattern), a `[project.scripts]` console entry point, and a `tests/` dir with a `conftest.py` and a passing `CliRunner` smoke test.
5. Add config: `[tool.black]`, `[tool.flake8]` (or `.flake8`) aligned with black's line length (88, ignore E203/W503), `[tool.pyright]` (`include = ["src","tests"]`, `typeCheckingMode = "standard"`, `pythonVersion` set), `[tool.pytest.ini_options]`, and a `.gitignore` including `.pixi/`.
6. Run `pixi install` then `pixi run check` and confirm it's green (black, flake8, Pyright, pytest all pass). Report the layout and the commands I'll use.
