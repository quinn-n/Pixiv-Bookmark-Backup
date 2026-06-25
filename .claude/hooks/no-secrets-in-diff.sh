#!/usr/bin/env bash
if ! command -v git >/dev/null 2>&1; then exit 0; fi
diff=$(git diff HEAD 2>/dev/null)
if printf '%s' "$diff" | grep -nEi '(api[_-]?key|secret|password|token|bearer)[" '\'' ]*[:=][" '\'' ]*[A-Za-z0-9_\-]{16,}|-----BEGIN (RSA|EC|OPENSSH|PRIVATE) KEY-----' >/dev/null; then
  echo "Possible secret detected in the diff. Review before committing." >&2
  exit 2
fi
exit 0
