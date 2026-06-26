"""Guard: every repo-clone dir a `make ingest-*` target creates at the repo root
must have a .gitignore entry, so a stray `git add .` can't commit a vendored
checkout. A clone dir was once briefly un-ignored — this turns that class of miss
into a red test instead of a lucky `git status` catch."""

import os

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")

# Dirs cloned into the repo root by a `make ingest-*` target. Add the new dir here
# (and to .gitignore) whenever an ingest target clones a repo. (prototypes no longer
# clones factorio-data — it reads a `--dump-data` JSON export.)
CLONE_DIRS = ["clusterio"]


def _gitignore_patterns():
    with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
        return {ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")}


@pytest.mark.parametrize("clone_dir", CLONE_DIRS)
def test_clone_dir_is_gitignored(clone_dir):
    accepted = {f"/{clone_dir}/", f"{clone_dir}/", f"/{clone_dir}", clone_dir}
    patterns = _gitignore_patterns()
    assert accepted & patterns, (
        f"{clone_dir}/ (a `make ingest-*` clone target) has no .gitignore entry — "
        f"add `/{clone_dir}/` so the vendored checkout can't be committed accidentally"
    )
