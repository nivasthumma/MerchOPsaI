"""Does the repository import from what is actually committed?

The working directory is a liar. It holds every file you have written,
including the ones you never `git add`-ed, so `python -c "import app"` succeeds
there long after it would fail for anyone else. That is not hypothetical: this
repository once carried twenty-four untracked files that committed code
imported, and every test run passed while a fresh clone could not start.

So this exports the tracked tree into a temporary directory and imports the
entrypoints there.

The tree it exports is the INDEX, not HEAD. That distinction is the whole
point of running this before a commit rather than after one: HEAD is what you
already shipped, and it imported fine yesterday. The index is what the next
commit will contain, which is where a forgotten `git add` actually shows up.
`git write-tree` turns the index into a tree object that `git archive` can
read. In CI the two coincide, because `actions/checkout` produces a clean tree
with an index that matches it.

`ui/streamlit_app.py` is not in the list on purpose: importing it runs the
Streamlit script rather than testing anything.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ENTRYPOINTS = (
    "app.api.main",
    "app.agent.runtime",
    "app.policy.engine",
    "app.tools.registry",
    "app.eval.runner",
    "api.index",
)

PROBE = """
import importlib, sys
failed = []
for name in {modules!r}:
    try:
        importlib.import_module(name)
    except Exception as exc:
        failed.append((name, type(exc).__name__, str(exc)))
for name, kind, detail in failed:
    print(f"  {{name}}: {{kind}}: {{detail}}")
sys.exit(1 if failed else 0)
"""


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="merchantops-cleanroom-") as tmp:
        tree = subprocess.run(["git", "write-tree"], cwd=root,
                              capture_output=True, check=False)
        if tree.returncode != 0:
            print("could not read the index:", tree.stderr.decode().strip())
            return 1

        archive = subprocess.run(
            ["git", "archive", tree.stdout.decode().strip()],
            cwd=root, capture_output=True, check=False)
        if archive.returncode != 0:
            print("could not export the tracked tree:",
                  archive.stderr.decode().strip())
            return 1
        subprocess.run(["tar", "-x", "-C", tmp], input=archive.stdout, check=True)

        env = {**os.environ,
               "PYTHONPATH": tmp,
               "MERCHANTOPS_NO_CLI_AUTH_PROBE": "1"}
        env.setdefault("API_TOKEN_SECRET", "cleanroom-probe")
        probe = subprocess.run(
            [sys.executable, "-c", PROBE.format(modules=list(ENTRYPOINTS))],
            cwd=tmp, env=env, capture_output=True, check=False)

    if probe.returncode != 0:
        print("The committed tree does not import.\n")
        print(probe.stdout.decode().rstrip() or probe.stderr.decode().rstrip())
        print("\nSomething imported here is not staged. `git status "
              "--untracked-files=all` will show it.")
        return 1

    print(f"Clean-room import OK — {len(ENTRYPOINTS)} entrypoints, staged tree only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
