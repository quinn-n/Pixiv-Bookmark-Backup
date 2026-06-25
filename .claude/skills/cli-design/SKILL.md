---
name: cli-design
description: Principles and checklists for designing intuitive, scriptable command-line tools with click. Use when designing commands, writing help text, choosing options/flags, shaping output, or reviewing CLI UX.
---

# Intuitive CLI design (with click)

Apply when designing or reviewing any command. Two users to satisfy at once: the **newcomer** who learns from `--help`, and the **scripter** who automates the tool.

## Core principles
- **Discoverable from `--help`.** Every command and option has a clear, present-tense help string. Non-trivial commands show an example. A user should rarely need external docs.
- **Sensible defaults.** The common case needs few flags. Default what can be defaulted; fall back env var → config file → built-in default.
- **Consistency.** Option names, ordering, and behavior are uniform across subcommands (`--output`, not `--out` here and `--dest` there). Kebab-case flags (`--dry-run`).
- **Errors that teach.** State what went wrong *and* the fix ("No config found at X; run `tool init` or pass `--config`"). Never dump a raw traceback for an expected failure.
- **Composability.** Tools are pipeline citizens: **data → stdout, logs/errors → stderr**, so `tool ... | other` works. Offer `--json`/machine output. Don't prompt when stdin/stdout isn't a TTY unless interactive is intended; allow `--yes`/`--no-input` to bypass prompts.
- **Forgiving & safe.** Destructive actions confirm, support `--dry-run`, and are scriptable via `--yes`. Idempotent where possible.

## Exit codes are an API
`0` = success. Use distinct non-zero codes for distinct failure classes (e.g. 1 generic, 2 usage error — click's default for bad args). Document them; scripts depend on them. Use `click.ClickException` / `ctx.exit(code)` rather than `sys.exit(str)`.

## Output design
- Human output: scannable, aligned, minimal noise. Color/emphasis aids comprehension but is never the only signal; auto-disable when not a TTY and honor `NO_COLOR`.
- Machine output: `--json` (or similar) emits valid, stable, parseable data on stdout only.
- Respect `--quiet` (errors only) and `--verbose`/`-v` (more detail, to stderr). Show progress for long operations.

## click conventions
- `@click.group()` for multi-command tools; `@click.command()` for leaves; `@click.argument()` for required positionals; `@click.option()` for everything optional.
- Always pass `type=` (`click.Path(exists=..., path_type=Path)`, `click.Choice`, `click.INT`...) so click validates and errors well.
- Use `help=`, `default=`, `show_default=True`, `envvar=`, `is_flag=`, `multiple=`, `count=` deliberately.
- Add `--version` (`@click.version_option`). Reuse repeated option sets via a shared decorator.
- Keep command callbacks thin: parse/validate in the click layer, delegate to plain, testable functions in the core. This makes logic unit-testable without the CLI and keeps commands readable.

## The five things every command should handle
Success, **empty/no-result**, **invalid input/usage**, **expected runtime failure** (missing file, permission, network), and **interruption** (clean Ctrl-C, no half-written state). A command isn't done until all five behave well.

## Friction audit
Count flags/steps to accomplish the primary task. For each required flag/prompt ask: can it be defaulted, inferred, or read from env/config? The best flag is the one the user never has to type.
