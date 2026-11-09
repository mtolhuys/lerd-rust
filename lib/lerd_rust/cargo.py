"""Find and run cargo.

Lerd starts dev servers and the dashboard with a fixed PATH that does not
include ~/.cargo/bin, where rustup's installer puts cargo. So cargo is looked
up on PATH first and then in the places rustup installs it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Mapping, Optional

OVERRIDE = "LERD_RUST_CARGO"


class CargoNotFound(RuntimeError):
    pass


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_cargo(env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    explicit = env.get(OVERRIDE, "")
    if explicit:
        if os.path.isabs(explicit) and _is_executable(Path(explicit)):
            return explicit
        raise CargoNotFound(
            f"{OVERRIDE}={explicit!r} must be the absolute path of an executable cargo."
        )
    found = shutil.which("cargo", path=env.get("PATH", os.defpath))
    if found:
        return found
    homes = []
    if env.get("CARGO_HOME"):
        homes.append(Path(env["CARGO_HOME"]))
    home = env.get("HOME") or os.path.expanduser("~")
    homes.append(Path(home) / ".cargo")
    for cargo_home in homes:
        candidate = cargo_home / "bin" / "cargo"
        if _is_executable(candidate):
            return str(candidate)
    raise CargoNotFound(
        "cargo was not found on PATH, in $CARGO_HOME/bin or in ~/.cargo/bin. "
        f"Install Rust from https://rustup.rs, or set {OVERRIDE} to cargo's absolute path."
    )


def child_env(cargo: str, env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The environment for cargo and the app: cargo's own folder on PATH, so
    rustc and the other rustup proxies beside it are found too."""
    out = dict(os.environ if env is None else env)
    folder = os.path.dirname(cargo)
    parts = [p for p in out.get("PATH", os.defpath).split(os.pathsep) if p and p != folder]
    # First, so the rustc beside the chosen cargo wins over another one on PATH.
    out["PATH"] = os.pathsep.join([folder] + parts)
    return out


VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?")


def run_env(cargo: str, package: Optional[dict], executable: str, env: Mapping[str, str]) -> Dict[str, str]:
    """The environment `cargo run` gives the program it starts, for running a
    built executable directly: what an app may read at run time through
    std::env::var, and the folders its dynamic libraries live in."""
    out = dict(env)
    out["CARGO"] = cargo
    if package:
        manifest = package.get("manifest_path") or ""
        out["CARGO_MANIFEST_DIR"] = os.path.dirname(manifest)
        out["CARGO_MANIFEST_PATH"] = manifest
        out["CARGO_PKG_NAME"] = package.get("name") or ""
        version = package.get("version") or ""
        out["CARGO_PKG_VERSION"] = version
        match = VERSION.match(version)
        if match:
            out["CARGO_PKG_VERSION_MAJOR"], out["CARGO_PKG_VERSION_MINOR"], out["CARGO_PKG_VERSION_PATCH"] = match.group(1, 2, 3)
            out["CARGO_PKG_VERSION_PRE"] = match.group(4) or ""
        out["CARGO_PKG_AUTHORS"] = ":".join(package.get("authors") or [])
        for key, field in (("DESCRIPTION", "description"), ("HOMEPAGE", "homepage"), ("REPOSITORY", "repository"),
                           ("LICENSE", "license"), ("LICENSE_FILE", "license_file"), ("README", "readme"),
                           ("RUST_VERSION", "rust_version")):
            value = package.get(field)
            out["CARGO_PKG_" + key] = value if isinstance(value, str) else ""
    folder = os.path.dirname(executable)
    libraries = [folder, os.path.join(folder, "deps")]
    existing = [p for p in out.get("LD_LIBRARY_PATH", "").split(os.pathsep) if p]
    out["LD_LIBRARY_PATH"] = os.pathsep.join(libraries + [p for p in existing if p not in libraries])
    return out


def quiet_env(cargo: str, env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """For read-only lookups: never let rustup download a toolchain."""
    out = child_env(cargo, env)
    out["RUSTUP_AUTO_INSTALL"] = "0"
    return out


def metadata(cargo: str, root: str, env: Mapping[str, str], timeout: float = 30) -> dict:
    """`cargo metadata` for the workspace members only. --no-deps reads the
    manifests without resolving dependencies, so it never touches the network."""
    result = subprocess.run(
        [cargo, "metadata", "--no-deps", "--format-version", "1", "--offline"],
        cwd=root,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(_tail(result.stderr.decode("utf-8", "replace")) or "cargo metadata failed")
    return json.loads(result.stdout)


def version(program: str, env: Mapping[str, str], timeout: float = 15) -> Optional[str]:
    try:
        result = subprocess.run(
            [program, "--version"],
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip() or None


def root_package(meta: dict, root: str) -> Optional[dict]:
    """The package whose manifest sits in root, as `cargo run` picks it."""
    manifest = os.path.realpath(os.path.join(root, "Cargo.toml"))
    for package in meta.get("packages", []):
        if os.path.realpath(package.get("manifest_path", "")) == manifest:
            return package
    return None


def package_by_id(meta: Optional[dict], package_id: Optional[str]) -> Optional[dict]:
    if not meta or not package_id:
        return None
    return next((p for p in meta.get("packages", []) if p.get("id") == package_id), None)


def binaries(package: dict) -> List[str]:
    return [t["name"] for t in package.get("targets", []) if "bin" in t.get("kind", [])]


def _tail(text: str, limit: int = 4000) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]


tail = _tail
