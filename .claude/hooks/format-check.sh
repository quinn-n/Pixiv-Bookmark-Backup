#!/usr/bin/env bash
# PostToolUse hook: after editing a .py file, check black formatting, flake8, and Pyright for that file.
input=$(cat)
path=$(printf '%s' "$input" | grep -oE '"(file_path|path)"[^,}]*' | head -1 | sed -E 's/.*: *"?([^"]*)"?.*/\1/')
case "$path" in
  *.py) : ;;
  *) exit 0 ;;
esac
[ -f "$path" ] || exit 0
command -v pixi >/dev/null 2>&1 || exit 0

issues=""
if ! pixi run -q black --check "$path" >/dev/null 2>&1; then
  issues="${issues}  black: needs formatting — run 'pixi run fmt'.\n"
fi
lt=$(pixi run -q flake8 "$path" 2>&1); [ $? -ne 0 ] && issues="${issues}  flake8:\n$(printf '%s' "$lt" | sed 's/^/    /')\n"
# Pyright (basedpyright) on the single file; ignore if the tool isn't installed.
if pixi run -q basedpyright --version >/dev/null 2>&1; then
  pt=$(pixi run -q basedpyright "$path" 2>&1)
  if [ $? -ne 0 ]; then
    issues="${issues}  pyright:\n$(printf '%s' "$pt" | grep -E 'error|warning' | head -20 | sed 's/^/    /')\n"
  fi
fi

if [ -n "$issues" ]; then
  printf "Quality check on %s:\n%b" "$path" "$issues" >&2
  exit 2  # feed feedback back to Claude so it can fix before continuing
fi
exit 0
