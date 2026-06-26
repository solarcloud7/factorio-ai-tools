"""Release smoke test — one script, both layers, one PASS/FAIL.

Verifies the thing a real user hits, which no other check covers (offline pytest
runs local code; `make eval` needs pre-built data). Manually triggered (pulls the
~100 MB release zip + a CPU torch wheel; reuses the cached embedding model).

  * Layer B (packaging): a clean ``uv pip install factorio-ai-tools==VER`` into an
    isolated venv proves the wheel installs, deps resolve, and the console script
    is registered.
  * Layer A (runtime): inside that venv, with a fresh data home, importing the
    server triggers the REAL release-zip download (asserted via FACTORIO_SMOKE_HOME);
    then every DB-backed tool is called and checked against a SUCCESS-ONLY anchor —
    one that appears only in a real result, never in the echoed query or an error
    string — so a broken search can actually fail the gate.

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

# Windows-safe print (CLAUDE.md / .agents/AGENTS.md): the report renders scraped
# tool output that can contain en-dashes/emoji, which raw print() turns into a
# fatal UnicodeEncodeError on PowerShell. Prefer the shared helper; fall back to a
# stdlib ascii-replace so the script still runs in a bare venv.
try:
    from factorio_ai_tools.ingest.common import safe_print
except Exception:  # pragma: no cover - bare-venv fallback
    def safe_print(message):
        print(str(message).encode("ascii", "replace").decode("ascii"))


# --- The checks themselves (run INSIDE the target environment) ----------------

def run_checks():
    """Import the installed package and assert each tool. Prints one SMOKE_JSON
    line. Stdlib-only at module scope (plus the safe_print import, which has a
    fallback) so this runs in a bare venv that has only factorio-ai-tools."""
    results = []

    def check(name, fn):
        try:
            ok, detail = fn()
            results.append({"name": name, "ok": bool(ok), "detail": str(detail)[:200]})
        except Exception as e:
            results.append({"name": name, "ok": False, "detail": f"EXC {type(e).__name__}: {e}"[:200]})

    # Importing server runs ensure_databases (download), loads the model, and opens
    # every table — i.e. full server startup. If it fails, nothing else can run.
    try:
        import factorio_ai_tools.server as srv
        results.append({"name": "import server (download + model + open tables)",
                        "ok": True, "detail": "bootstrap ran"})
    except Exception as e:
        results.append({"name": "import server (download + model + open tables)",
                        "ok": False, "detail": f"EXC {type(e).__name__}: {e}"[:200]})
        print(CHECKS_MARKER + json.dumps(results))
        return

    def layer_a_downloaded():
        # Prove the REAL download branch ran: in published mode the parent sets
        # FACTORIO_SMOKE_HOME to the fresh isolated home, and DATA_DIR must resolve
        # under it (not a pre-existing/local dir). Skipped in --local mode.
        expect = os.environ.get("FACTORIO_SMOKE_HOME")
        d = os.path.abspath(srv.DATA_DIR)
        if not expect:
            return True, f"local mode (DATA_DIR={d})"
        return d.startswith(os.path.abspath(expect)), f"DATA_DIR={d}"

    def stores_present():
        # Check only the release-zip stores; prototypes_lancedb is built separately.
        present = [x for x in srv.RELEASE_STORES if os.path.isdir(os.path.join(srv.DATA_DIR, x))]
        n = len(srv.RELEASE_STORES)
        return len(present) == n, f"{len(present)}/{n} @ {srv.DATA_DIR}"

    def tables_open():
        # Only the release-zip stores are required: prototypes_lancedb is built
        # separately (make ingest-prototypes) and is NOT in the published zip, so
        # its table is legitimately None in published mode — gating on it here
        # would red the smoke on every correctly-deployed release.
        handles = {"factorio": srv.table_factorio, "clusterio": srv.table_clusterio,
                   "wiki": srv.table_wiki, "forum": srv.table_forum, "repo": srv.table_repo}
        missing = [k for k, v in handles.items() if v is None]
        n = len(handles)
        return not missing, (f"all {n} open" if not missing else f"missing: {missing}")

    def has(out, needle):
        # A real hit only: the anchor must be present AND the output must not be the
        # "no results" or an "Error ..." string. The anchors below are SUCCESS-ONLY
        # (absent from the echoed query and from any error message), so this can't
        # false-pass on a broken search.
        low = out.lower()
        ok = (needle.lower() in low
              and "no results found" not in low
              and not low.lstrip().startswith("error"))
        return ok, out[:130].replace("\n", " ")

    def version_real():
        info = json.loads(srv.get_mcp_version_info())
        fac = info.get("factorio_docs_version", "")
        return bool(fac) and fac != "unknown", f"factorio_docs_version={fac!r}"

    check("Layer A: data downloaded to the isolated home", layer_a_downloaded)
    check("all 5 release stores present", stores_present)
    check("tables open", tables_open)
    # anchors are query-independent + success-only (not in the query echo / errors):
    check("docs -> teleport method", lambda: has(srv.search_factorio_docs(["how do I teleport an entity"], limit=5), "method_teleport"))
    check("clusterio plugin=player_auth (escaped LIKE)", lambda: has(srv.search_clusterio_code(["authentication tokens"], plugin="player_auth", limit=3), "player_auth"))
    check("repo 'iron plate recipe' -> recipe.lua", lambda: has(srv.search_github_code(["iron plate recipe"], repo_name="factorio-data", limit=3), "recipe.lua"))
    check("wiki 'transport belt' -> wiki result", lambda: has(srv.search_factorio_wiki(["transport belt speed"], limit=3), "wiki.factorio.com"))
    check("forum -> forum result (vector fallback)", lambda: has(srv.search_factorio_forums(["modding help"], limit=3), "forums.factorio.com"))
    check("blueprint encode<->decode roundtrip", lambda: (
        "entities" in srv.decode_factorio_blueprint(
            srv.encode_factorio_blueprint('{"blueprint":{"item":"blueprint","entities":[]}}')),
        "roundtrip ok"))
    check("get_mcp_version_info: real factorio version", version_real)
    # prototypes_lancedb ships outside the release zip, so it's only present in
    # --local mode (or after `make ingest-prototypes`). Exercise the tool when the
    # store is there; record a pass-with-note when it's legitimately absent.
    if srv.table_prototypes is not None:
        check("prototypes -> electronic-circuit recipe", lambda: has(
            srv.search_factorio_prototypes(["electronic circuit recipe ingredients"], prototype_type="recipe", limit=3),
            "electronic-circuit"))
    else:
        results.append({"name": "prototypes -> electronic-circuit recipe", "ok": True,
                        "detail": "skipped: prototypes_lancedb not in release zip (built via make ingest-prototypes)"})

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
    for line in (stdout or "").splitlines():
        if line.startswith(CHECKS_MARKER):
            try:
                return json.loads(line[len(CHECKS_MARKER):])
            except json.JSONDecodeError:
                return None  # truncated/partial output (child killed mid-print)
    return None


def _uv(args):
    """Run `uv <args>`; None if uv isn't on PATH. utf-8/replace so a non-ASCII log
    line can't raise UnicodeDecodeError while decoding the child's output."""
    try:
        return subprocess.run(["uv", *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None


def _run_check_subprocess(cmd, timeout, env=None):
    """Run `<python> smoke_release.py --run-checks` and return the parsed checks
    list — or a single synthetic FAIL row on timeout / missing exe / unparseable
    output, so the harness degrades to a FAIL verdict instead of a traceback."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return [{"name": "in-env checks", "ok": False, "detail": f"timed out after {timeout}s"}]
    except FileNotFoundError as e:
        return [{"name": "in-env checks", "ok": False, "detail": f"executable not found: {e}"}]
    checks = _parse_checks(proc.stdout)
    if checks is None:
        return [{"name": "in-env checks", "ok": False,
                 "detail": (proc.stderr or proc.stdout or "no output")[-300:].replace("\n", " ")}]
    return checks


def run_local(timeout):
    """Run the checks against the CURRENT repo code + local data/ (fast; no fresh
    download/install). NOTE: this does NOT exercise the ensure_databases download
    path — use the default published mode for that. Good for iterating on checks."""
    return _run_check_subprocess(
        ["uv", "run", "--no-sync", "python", os.path.abspath(__file__), "--run-checks"], timeout)


def run_published(version, keep, timeout):
    """Install the PUBLISHED wheel into an isolated venv, then run the checks inside
    it with a fresh data home (forces the real release-zip download)."""
    # Capture the real model cache BEFORE redirecting HOME for the child, so the
    # isolated run reuses the cached embedder instead of re-downloading ~200 MB.
    real_hf = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")

    pre = []
    tmp = tempfile.mkdtemp(prefix="fac-smoke-")
    try:
        venv = os.path.join(tmp, "venv")
        home = os.path.join(tmp, "home")
        os.makedirs(home)

        mk = _uv(["venv", venv])
        if mk is None:
            return [{"name": "uv available", "ok": False, "detail": "`uv` not found on PATH"}]
        pre.append({"name": "create isolated venv", "ok": mk.returncode == 0,
                    "detail": (mk.stderr or "ok").strip()[-160:]})
        if mk.returncode != 0:
            return pre

        spec = f"factorio-ai-tools=={version}"
        inst = _uv(["pip", "install", "--python", _venv_python(venv), spec])
        if inst is None:
            return pre + [{"name": "uv available", "ok": False, "detail": "`uv` not found on PATH"}]
        pre.append({"name": f"uv pip install {spec}", "ok": inst.returncode == 0,
                    "detail": (inst.stderr or inst.stdout)[-180:].replace("\n", " ")})
        if inst.returncode != 0:
            return pre

        console = _console_script(venv)
        pre.append({"name": "console script registered", "ok": console is not None,
                    "detail": console or "MISSING"})

        env = dict(os.environ)
        env.pop("FACTORIO_MCP_LOCAL_MODE", None)   # don't force a local data/ dir
        env.pop("PYTHONPATH", None)                # don't leak the repo's src onto sys.path
        env["USERPROFILE"] = home                  # Windows expanduser("~") -> fresh data home
        env["HOME"] = home                         # POSIX expanduser("~")
        env["HF_HOME"] = real_hf                   # reuse the cached embedding model
        env["FACTORIO_SMOKE_HOME"] = home          # let the Layer-A check assert the download landed here

        return pre + _run_check_subprocess(
            [_venv_python(venv), os.path.abspath(__file__), "--run-checks"], timeout, env)
    finally:
        if keep:
            safe_print(f"(kept temp env at {tmp})")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def report(results):
    width = max((len(r["name"]) for r in results), default=0)
    safe_print("\n=== Release smoke test ===")
    npass = sum(1 for r in results if r["ok"])
    for r in results:
        safe_print(f"  [{'PASS' if r['ok'] else 'FAIL'}] {r['name']:<{width}}  {r['detail']}")
    ok = bool(results) and npass == len(results)
    safe_print(f"\nRESULT: {npass}/{len(results)} checks passed -> {'PASS' if ok else 'FAIL'}")
    return ok


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

    # Backstop: a harness failure (network, missing uv, timeout) becomes a FAIL row,
    # never a bare traceback.
    try:
        if a.local:
            safe_print("Smoke-testing LOCAL code + data/ ...")
            results = run_local(a.timeout)
        else:
            ver = a.version or _latest_pypi_version()
            safe_print(f"Smoke-testing PUBLISHED factorio-ai-tools=={ver} in an isolated venv (fresh download)...")
            results = run_published(ver, a.keep, a.timeout)
    except Exception as e:
        results = [{"name": "smoke harness", "ok": False, "detail": f"{type(e).__name__}: {e}"[:200]}]

    return 0 if report(results) else 1


if __name__ == "__main__":
    sys.exit(main())
