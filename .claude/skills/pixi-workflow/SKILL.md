---
name: pixi-workflow
description: How to manage environments, dependencies, and tasks with pixi for this project. Use whenever adding/updating dependencies, running code or tools, defining tasks, or setting up environments — so everything stays reproducible and runs in the project env.
---

# Pixi workflow

Pixi manages dependencies (conda-forge + PyPI), environments, and tasks from one manifest with a committed lockfile. **Always run project code and tools through pixi** so they use the project environment, never a stray global interpreter.

## Manifest & lockfile
- Config lives in `pixi.toml`, or in `pyproject.toml` under `[tool.pixi.*]` (for packaged Python projects). `[workspace]` sets `channels` (use `conda-forge`) and `platforms`.
- `pixi.lock` is generated — **never hand-edit it**, and **commit it** for reproducible installs across machines/CI.
- `pixi install` materializes the environment from the lock.

## Dependencies
- Add conda-forge deps: `pixi add <pkg>` (e.g. `pixi add click`). **Prefer conda-forge** when the package exists there.
- Add PyPI-only deps: `pixi add --pypi <pkg>`.
- Scope dev/test tools to a feature: `pixi add --feature dev pytest black flake8`.
- Inspect: `pixi list` (installed + versions), `pixi tree` (graph), `pixi search <pkg>` (is it on conda-forge?). `pixi update` bumps within constraints.
- To verify a library's real API/version, run inside the env: `pixi run python -c "import click; print(click.__version__)"`.

## Features & environments
Features are reusable groups of deps/tasks; environments compose features:
```toml
[dependencies]
python = ">=3.11"
click = "*"

[feature.dev.dependencies]
pytest = "*"
pytest-cov = "*"
black = "*"
flake8 = "*"

[feature.dev.tasks]
# dev-only tasks can live here

[environments]
default = { features = [] }
dev = { features = ["dev"] }
```
Run a task/command in a chosen env with `--environment` (e.g. `pixi run -e dev test`).

## Tasks (the project's command vocabulary)
Define tasks once; everyone (and every agent) uses the same names. Tasks can depend on each other:
```toml
[tasks]
fmt       = "black ."
fmt-check = "black --check --diff ."
lint      = "flake8"
typecheck = "mypy src"          # if mypy is used
test      = "pytest"
check     = { depends-on = ["fmt-check", "lint", "typecheck", "test"] }
```
Run with `pixi run <task>` (e.g. `pixi run test`, `pixi run check`). A failing dependency stops the chain. Hide internal tasks with a leading `_`.

## Conventions for this project
- Standard tasks: `fmt`, `fmt-check`, `lint`, `typecheck` (if mypy), `test`, `check`. Add `audit`/`bandit` for security if those tools are installed.
- Never `pip install` into the env directly or edit the lockfile by hand — go through `pixi add`.
- Add `.pixi/` to `.gitignore`; commit `pixi.toml`/`pyproject.toml` and `pixi.lock`.
- For supply-chain caution you can set a workspace freshness cutoff to avoid pulling brand-new (potentially compromised) releases.
