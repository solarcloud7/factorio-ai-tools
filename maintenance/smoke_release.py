"""Release smoke test — one script, both layers, one PASS/FAIL.

Verifies the thing a real user hits, which no other check covers (offline pytest
runs local code; `make eval` needs pre-built data). Manually triggered (pulls the
~100 MB release zip + a CPU torch wheel; reuses the cached embedding model).

  * Layer B (packaging): a clean ``uv pip install factorio-ai-tools==VER`` into an
    isolated venv proves the wheel installs, deps resolve, and the console script
    is registered.
  * Layer A (runtime): inside that venv, with a fresh data home, importing the
    server triggers the REAL release-zip download; then every search/non-search
    tool is called and checked against its known-good regression anchor.

Usage:
    uv run --no-sync python maintenance/smoke_release.py             # latest on PyPI
    uv run --no-sync python maintenance/smoke_release.py --version 1.2.0
    uv run --no-sync python maintenance/smoke_release.py --local     # local code+data (fast)
    # (--run-checks is internal: it's how the checks run inside the target env)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

CHECKS_MARKER = "SMOKE_JSON:"


# --- The checks themselves (run INSIDE the target environment) ----------------

def run_checks():
    """Import the installed package and assert each tool. Prints one SMOKE_JSON
    line. Stdlib-only at module scope so this runs in a bare venv that has only
    factorio-ai-tools installed; the package is imported lazily below."""
    results = []
    state = {}

    def check(name, fn):
        try:
            ok, detail = fn()
            results.append({"name": name, "ok": bool(ok), "detail": str(detail)[:200]})
        except Exception as e:
            results.append({"name": name, "ok": False, "detail": f"EXC {type(e).__name__}: {e}"[:200]})

    def do_import():
        # Importing server runs ensure_databases (download), loads the model, and
        # opens every table — i.e. full server startup.
        import factorio_ai_tools.server as s
        from factorio_ai_tools import server
        state["s"], state["server"] = s, server
        return True, "server imported (bootstrap ran)"

    check("import server (download + model + open tables)", do_import)
    if "s" not in state:
        print(CHECKS_MARKER + json.dumps(results))
        return
    s, server = state["s"], state["server"]

    def stores_present():
        d = server.DATA_DIR
        present = [x for x in server.ALL_STORES if os.path.isdir(os.path.join(d, x))]
        return len(present) == len(server.ALL_STORES), f"{len(present)}/{len(server.ALL_STORES)} @ {d}"

    def tables_open():
        handles = {
            "factorio": server.table_factorio, "clusterio": server.table_clusterio,
            "wiki": server.table_wiki, "forum": server.table_forum, "repo": server.table_repo,
        }
        missing = [k for k, v in handles.items() if v is None]
        return not missing, ("all 5 open" if not missing else f"missing: {missing}")

    def has(out, needle):
        return needle.lower() in out.lower(), out[:130].replace("\n", " ")

    check("bootstrap: all 5 stores present", stores_present)
    check("tables open", tables_open)
    check("docs -> teleport API", lambda: has(s.search_factorio_docs(["how do I teleport an entity"], limit=5), "teleport"))
    check("clusterio plugin=player_auth (escaped LIKE)", lambda: has(s.search_clusterio_code(["authentication tokens"], plugin="player_auth", limit=3), "player_auth"))
    check("repo 'iron plate recipe' -> recipe.lua", lambda: has(s.search_github_code(["iron plate recipe"], repo_name="factorio-data", limit=3), "recipe.lua"))
    check("wiki 'transport belt'", lambda: has(s.search_factorio_wiki(["transport belt speed"], limit=3), "belt"))
    check("forum returns (vector fallback)", lambda: (bool(s.search_factorio_forums(["modding help"], limit=1)), "non-empty"))
    check("blueprint encode<->decode roundtrip", lambda: (
        "blueprint" in s.decode_factorio_blueprint(
            s.encode_factorio_blueprint('{"blueprint":{"item":"blueprint","entities":[]}}')),
        "roundtrip ok"))
    check("get_mcp_version_info", lambda: ("factorio_docs_version" in s.get_mcp_version_info(), "ok"))

    print(CHECKS_MARKER + json.dumps(results))


# --- Orchestration (parent process) ------------------------------------------

def _latest_pypi_version(pkg="factorio-ai-tools"):
    with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=20) as r:
        return json.load(r)["info"]["version"]


def _venv_python(venv_dir):
    for p in (os.path.join(venv_dir, "Scripts", "python.exe"),
              os.path.join(venv_dir, "bin", "python")):
        if os.path.exists(p):
            return p
    raise RuntimeError(f"venv python not found under {venv_dir}")


def _console_script(venv_dir):
    for p in (os.path.join(venv_dir, "Scripts", "factorio-ai-tools.exe"),
              os.path.join(venv_dir, "bin", "factorio-ai-tools")):
        if os.path.exists(p):
            return p
    return None


def _parse_checks(stdout):
    for line in stdout.splitlines():
        if line.startswith(CHECKS_MARKER):
            return json.loads(line[len(CHECKS_MARKER):])
    return None


def run_local(timeout):
    """Run the checks against the CURRENT repo code + local data/ (fast; no fresh
    download/install). Good for iterating on the checks themselves."""
    proc = subprocess.run(
        ["uv", "run", "--no-sync", "python", os.path.abspath(__file__), "--run-checks"],
        capture_output=True, text=True, timeout=timeout,
    )
    checks = _parse_checks(proc.stdout)
    if checks is None:
        return [{"name": "local checks executed", "ok": False,
                 "detail": (proc.stderr or proc.stdout)[-300:].replace("\n", " ")}]
    return checks


def run_published(version, keep, timeout):
    """Install the PUBLISHED wheel into an isolated venv, then run the checks inside
    it with a fresh data home (forces the real release-zip download)."""
    # Capture the real model cache BEFORE we redirect HOME for the child, so the
    # isolated run reuses the cached embedder instead of re-downloading ~200 MB.
    real_hf = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")

    pre = []
    tmp = tempfile.mkdtemp(prefix="fac-smoke-")
    try:
        venv = os.path.join(tmp, "venv")
        home = os.path.join(tmp, "home")
        os.makedirs(home)

        mk = subprocess.run(["uv", "venv", venv], capture_output=True, text=True)
        pre.append({"name": "create isolated venv", "ok": mk.returncode == 0,
                    "detail": (mk.stderr or "ok").strip()[-160:]})
        if mk.returncode != 0:
            return pre

        spec = f"factorio-ai-tools=={version}"
        inst = subprocess.run(["uv", "pip", "install", "--python", _venv_python(venv), spec],
                              capture_output=True, text=True)
        pre.append({"name": f"uv pip install {spec}", "ok": inst.returncode == 0,
                    "detail": (inst.stderr or inst.stdout)[-180:].replace("\n", " ")})
        if inst.returncode != 0:
            return pre

        pre.append({"name": "console script registered", "ok": _console_script(venv) is not None,
                    "detail": _console_script(venv) or "MISSING"})

        env = dict(os.environ)
        env.pop("FACTORIO_MCP_LOCAL_MODE", None)   # don't force a local data/ dir
        env.pop("PYTHONPATH", None)                # don't leak the repo's src onto sys.path
        env["USERPROFILE"] = home                  # Windows expanduser("~") -> fresh data home
        env["HOME"] = home                         # POSIX expanduser("~")
        env["HF_HOME"] = real_hf                   # reuse the cached embedding model

        proc = subprocess.run([_venv_python(venv), os.path.abspath(__file__), "--run-checks"],
                              env=env, capture_output=True, text=True, timeout=timeout)
        checks = _parse_checks(proc.stdout)
        if checks is None:
            pre.append({"name": "in-venv checks executed", "ok": False,
                        "detail": (proc.stderr or proc.stdout)[-300:].replace("\n", " ")})
            return pre
        return pre + checks
    finally:
        if keep:
            print(f"(kept temp env at {tmp})", file=sys.stderr)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def report(results):
    width = max((len(r["name"]) for r in results), default=0)
    print("\n=== Release smoke test ===")
    npass = sum(1 for r in results if r["ok"])
    for r in results:
        print(f"  [{'PASS' if r['ok'] else 'FAIL'}] {r['name']:<{width}}  {r['detail']}")
    ok = npass == len(results) and results
    print(f"\nRESULT: {npass}/{len(results)} checks passed -> {'PASS' if ok else 'FAIL'}")
    return bool(ok)


def main():
    ap = argparse.ArgumentParser(description="Release smoke test (published wheel + fresh download + tool assertions).")
    ap.add_argument("--run-checks", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--version", help="PyPI version to test (default: latest on PyPI)")
    ap.add_argument("--local", action="store_true", help="run checks against local code + data/ (fast)")
    ap.add_argument("--keep", action="store_true", help="keep the temp venv/home for inspection")
    ap.add_argument("--timeout", type=int, default=900, help="seconds for the in-env run (default 900)")
    a = ap.parse_args()

    if a.run_checks:
        run_checks()
        return 0

    if a.local:
        print("Smoke-testing LOCAL code + data/ ...")
        results = run_local(a.timeout)
    else:
        ver = a.version or _latest_pypi_version()
        print(f"Smoke-testing PUBLISHED factorio-ai-tools=={ver} in an isolated venv (fresh download)...")
        results = run_published(ver, a.keep, a.timeout)

    return 0 if report(results) else 1


if __name__ == "__main__":
    sys.exit(main())
