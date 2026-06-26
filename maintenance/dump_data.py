"""Produce a VANILLA Factorio `--dump-data` export (base + Space Age DLC, no
community mods) and stage it for `make ingest-prototypes`.

  python maintenance/dump_data.py --factorio <path-to-factorio.exe> --version 2.0.76

Writes factorio-export/vanilla_<version>/{data-raw-dump.json, version.txt}. Forces a
clean base+DLC-only mod set via --mod-directory so enabled community mods don't
pollute the vanilla baseline. The standalone build runs inline; the Steam build
relaunches via Steam and DETACHES, so for Steam either run it from the GUI/wait, or
prefer a standalone install (see factorio-export/README.md).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(__file__)
EXPORT = os.path.join(HERE, "..", "factorio-export")
DLC_MODS = ("base", "elevated-rails", "quality", "space-age")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--factorio", required=True, help="path to the factorio executable")
    ap.add_argument("--version", required=True, help="version label, e.g. 2.0.76")
    a = ap.parse_args()

    if not os.path.exists(a.factorio):
        print(f"ERROR: factorio binary not found: {a.factorio}")
        sys.exit(1)
    # .../<install>/bin/x64/factorio.exe -> <install>
    install = os.path.dirname(os.path.dirname(os.path.dirname(a.factorio)))

    # Clean mod set: only base + Space Age DLC enabled.
    moddir = os.path.abspath(os.path.join(EXPORT, ".vanilla-mods"))
    os.makedirs(moddir, exist_ok=True)
    with open(os.path.join(moddir, "mod-list.json"), "w", encoding="utf-8") as f:
        json.dump({"mods": [{"name": n, "enabled": True} for n in DLC_MODS]}, f)

    # script-output must exist before the run (a known Factorio quirk); it lands under
    # the Factorio WRITE dir, which is %APPDATA%\Factorio for a Steam/installed build
    # or the install dir for a portable one — create both candidates.
    candidates = []
    if os.environ.get("APPDATA"):
        candidates.append(os.path.join(os.environ["APPDATA"], "Factorio", "script-output"))
    candidates.append(os.path.join(install, "script-output"))
    for c in candidates:
        os.makedirs(c, exist_ok=True)

    print(f"Running `--dump-data` (base+DLC only) from {a.factorio} ...")
    subprocess.run([a.factorio, "--dump-data", "--mod-directory", moddir], check=False)

    produced = [os.path.join(c, "data-raw-dump.json") for c in candidates
                if os.path.exists(os.path.join(c, "data-raw-dump.json"))]
    if not produced:
        print("ERROR: data-raw-dump.json was not produced. (Steam build? it detaches — "
              "run it from the GUI or use a standalone install.) Checked:", candidates)
        sys.exit(1)
    src = max(produced, key=os.path.getmtime)  # newest write wins

    out = os.path.join(EXPORT, f"vanilla_{a.version}")
    os.makedirs(out, exist_ok=True)
    shutil.copy(src, os.path.join(out, "data-raw-dump.json"))
    with open(os.path.join(out, "version.txt"), "w", encoding="utf-8") as f:
        f.write(a.version)
    print(f"OK: {os.path.getsize(src) / 1e6:.1f} MB from {src}")
    print(f"  -> factorio-export/vanilla_{a.version}/  (version {a.version})")
    print(f"Next: FACTORIO_DATA_DUMP=factorio-export/vanilla_{a.version}/data-raw-dump.json make ingest-prototypes")


if __name__ == "__main__":
    main()
