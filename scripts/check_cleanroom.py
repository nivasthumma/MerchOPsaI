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

`ui/streamlit_app.py` cannot be imported: importing it RUNS the Streamlit
script rather than testing anything. That left the one file CI could not check
at all -- and it imports seventeen symbols straight out of `app.*`, so a
renamed function breaks the surface the README calls contract-conformant, with
nothing to notice.

So it is checked statically instead: every `from app.X import Y` is parsed out
and resolved against the real module. That is not as strong as an import -- it
cannot catch a changed SIGNATURE -- but it catches the failure that actually
happens, which is a symbol that moved, and it catches it without running a web
server.
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

# Files that cannot be imported because importing them does something. Their
# `from app.X import Y` lines are resolved instead.
STATIC_ONLY = ("ui/streamlit_app.py",)

PROBE = """
import ast, importlib, pathlib, sys
failed = []
for name in {modules!r}:
    try:
        importlib.import_module(name)
    except Exception as exc:
        failed.append((name, type(exc).__name__, str(exc)))

for relpath in {static!r}:
    src = pathlib.Path(relpath)
    if not src.exists():
        failed.append((relpath, "FileNotFoundError", "not in the staged tree"))
        continue
    for node in ast.walk(ast.parse(src.read_text())):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not node.module or not node.module.startswith("app"):
            continue
        try:
            mod = importlib.import_module(node.module)
        except Exception as exc:
            failed.append((relpath, type(exc).__name__,
                           f"line {{node.lineno}}: {{node.module}}: {{exc}}"))
            continue
        for alias in node.names:
            if not hasattr(mod, alias.name):
                failed.append((relpath, "ImportError",
                               f"line {{node.lineno}}: "
                               f"{{node.module}} has no {{alias.name}}"))
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
            [sys.executable, "-c", PROBE.format(modules=list(ENTRYPOINTS), static=list(STATIC_ONLY))],
            cwd=tmp, env=env, capture_output=True, check=False)

    if probe.returncode != 0:
        out = probe.stdout.decode().rstrip() or probe.stderr.decode().rstrip()
        print("The committed tree does not import.\n")
        print(out)
        # Two failures land here and they need different advice. Printing the
        # forgotten-`git add` line for a renamed symbol sends the reader to
        # `git status`, which shows nothing, and they conclude the checker is
        # broken.
        if "has no " in out:
            print("\nA name moved and something still imports it under the old "
                  "name.\nThe line above says where. This is why the file is "
                  "checked at all:\nit cannot be imported, so nothing else "
                  "would have noticed.")
        else:
            print("\nSomething imported here is not staged. `git status "
                  "--untracked-files=all` will show it.")
        return 1

    print(f"Clean-room import OK — {len(ENTRYPOINTS)} entrypoints imported, "
          f"{len(STATIC_ONLY)} resolved statically, staged tree only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
