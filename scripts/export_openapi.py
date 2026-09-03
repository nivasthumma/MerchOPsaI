"""Write the OpenAPI document to docs/openapi.json — ADR-0032.

The committed file is the contract consumers read, and
`tests/integration/test_contracts.py` fails when it drifts from the running
application. So a response shape changing is a reviewable diff in a pull
request rather than something a client discovers at runtime.

    python scripts/export_openapi.py          write it
    python scripts/export_openapi.py --check  exit 1 if it would change
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.main import app

TARGET = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


def document() -> str:
    # Sorted and indented so a diff shows what changed rather than that the
    # dictionary iterated differently.
    return json.dumps(app.openapi(), indent=2, sort_keys=True, default=str) + "\n"


def main() -> int:
    body = document()
    if "--check" in sys.argv[1:]:
        if not TARGET.exists():
            print(f"{TARGET} does not exist. Run scripts/export_openapi.py.")
            return 1
        if TARGET.read_text() != body:
            print(f"{TARGET} is out of date. Review the change, then regenerate.")
            return 1
        print(f"{TARGET} is current.")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(body)
    spec = json.loads(body)
    print(f"wrote {TARGET.relative_to(Path.cwd())}: "
          f"{len(spec['paths'])} paths, "
          f"{len(spec.get('components', {}).get('schemas', {}))} schemas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
