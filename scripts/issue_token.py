"""Mint an API bearer token for local use.

    python scripts/issue_token.py USR_A_OWNER

Set API_TOKEN_SECRET to sign with a real secret; without it a fixed
development value is used and the token is not secure.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.security import DEV_SECRET_IN_USE, issue_token


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    user_id = sys.argv[1]
    token = issue_token(user_id)
    if DEV_SECRET_IN_USE:
        print("WARNING: API_TOKEN_SECRET is unset — signing with the insecure "
              "development secret.\n", file=sys.stderr)
    print(token)
    print(f"\n  curl -H 'Authorization: Bearer {token}' localhost:8000/tasks/…",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
