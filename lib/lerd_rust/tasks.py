"""The cargo commands in a site's Commands menu. Lerd runs them in the site's
folder, or a worktree's, and streams what they print to the dashboard."""

from __future__ import annotations

import os
import sys
from typing import Mapping, Optional

from . import cargo as cargo_mod

TASKS = {
    "check": ["check", "--all-targets"],
    "test": ["test"],
    "clippy": ["clippy", "--all-targets"],
    "fmt-check": ["fmt", "--all", "--check"],
    "fmt": ["fmt", "--all"],
}


def main(task: str, env: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if env is None else env
    root = os.getcwd()
    if not os.path.isfile(os.path.join(root, "Cargo.toml")):
        sys.stderr.write(f"No Cargo.toml in {root}: this is not a Rust project.\n")
        return 1
    try:
        cargo = cargo_mod.find_cargo(env)
    except cargo_mod.CargoNotFound as error:
        sys.stderr.write(f"{error}\n")
        return 1
    args = TASKS[task]
    branch = env.get("LERD_BRANCH")
    where = f" ({branch})" if branch else ""
    sys.stdout.write(f"$ cargo {' '.join(args)}{where}\n")
    sys.stdout.flush()
    os.execve(cargo, [cargo, *args], cargo_mod.child_env(cargo, env))
    return 127  # not reached
