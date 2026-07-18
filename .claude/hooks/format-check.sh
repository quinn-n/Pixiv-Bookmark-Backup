#!/usr/bin/env bash
# PostToolUse hook: after editing a .py file, check black formatting, flake8, and Pyright for that file.

# Run a compute-heavy command at low OS priority locally; full speed in CI.
# Preserves the command's exit code. Degrades to a plain run if no tool is available.
low_prio() {
  if [ -n "${CI:-}${GITHUB_ACTIONS:-}${GITLAB_CI:-}" ]; then "$@"; return; fi
  if [ "$(uname -s)" = "Darwin" ] && command -v taskpolicy >/dev/null 2>&1; then
    taskpolicy -b "$@"; return
  fi
  if command -v nice >/dev/null 2>&1; then
    if command -v ionice >/dev/null 2>&1; then ionice -c3 nice -n 10 "$@"; else nice -n 10 "$@"; fi
    return
  fi
  "$@"
}

input=$(cat)
path=$(printf '%s' "$input" | grep -oE '"(file_path|path)"[^,}]*' | head -1 | sed -E 's/.*: *"?([^"]*)"?.*/\1/')
case "$path" in
  *.py) : ;;
  *) exit 0 ;;
esac
[ -f "$path" ] || exit 0
command -v pixi >/dev/null 2>&1 || exit 0

issues=""
if ! low_prio pixi run -q black --check "$path" >/dev/null 2>&1; then
  issues="${issues}  black: needs formatting — run 'pixi run fmt'.\n"
fi
lt=$(low_prio pixi run -q flake8 "$path" 2>&1); [ $? -ne 0 ] && issues="${issues}  flake8:\n$(printf '%s' "$lt" | sed 's/^/    /')\n"
# Pyright (basedpyright) on the single file; ignore if the tool isn't installed.
if pixi run -q basedpyright --version >/dev/null 2>&1; then
  pt=$(low_prio pixi run -q basedpyright "$path" 2>&1)
  if [ $? -ne 0 ]; then
    issues="${issues}  pyright:\n$(printf '%s' "$pt" | grep -E 'error|warning' | head -20 | sed 's/^/    /')\n"
  fi
fi

if [ -n "$issues" ]; then
  printf "Quality check on %s:\n%b" "$path" "$issues" >&2
  exit 2  # feed feedback back to Claude so it can fix before continuing
fi
exit 0
