#!/usr/bin/env bash
# PreToolUse hook: block writes/edits to sensitive files. Reads tool input JSON on stdin.
input=$(cat)
path=$(printf '%s' "$input" | grep -oE '"(file_path|path)"[^,}]*' | head -1 | sed -E 's/.*: *"?([^"]*)"?.*/\1/')
case "$path" in
  *.env|*.env.*|*id_rsa*|*.pem|*.key|*secrets*|*credentials*|*pixi.lock)
    if [[ "$path" == *pixi.lock ]]; then
      echo "Do not hand-edit pixi.lock. Change dependencies with 'pixi add' / 'pixi update' instead." >&2
    else
      echo "Refusing to write to a sensitive file: $path. Edit it manually if intended." >&2
    fi
    exit 2 ;;
esac
exit 0
