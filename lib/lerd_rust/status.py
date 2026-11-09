"""What the site tab, `lerd rust:status` and the MCP tools report: how the
site's dev server is set up, its last build, the project, and `cargo check`."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
from typing import Dict, List, Mapping, Optional, Tuple

from . import cargo as cargo_mod
from . import diagnostics, lerd, lerdyaml, state

PACKAGE = re.compile(r"^[A-Za-z0-9_-]+$")
CHECK_TIMEOUT = 570  # just under the 10 minute ceiling Lerd enforces


class Problem(Exception):
    """A request this plugin cannot answer, reported as a plain message."""


# Which checkout ---------------------------------------------------------


def query(env: Mapping[str, str]) -> Dict[str, str]:
    return {k: v[-1] for k, v in urllib.parse.parse_qs(env.get("LERD_QUERY", "")).items()}


def checkout(env: Mapping[str, str], branch: str = "") -> Tuple[str, str]:
    """(site, path) of the checkout a request is about. A site tab or an
    assistant names a worktree by its branch; Lerd hands over the main site."""
    site = env.get("LERD_SITE", "")
    path = env.get("LERD_SITE_PATH", "")
    if env.get("LERD_SURFACE", "cli") in ("cli", "site_command"):
        # Both run in the checkout itself, a worktree's when one is picked.
        return site, os.getcwd()
    if not path:
        raise Problem("Name a lerd site: this plugin did not receive one.")
    if branch:
        # Never fall back to the main checkout: the answer would be about
        # different code than the caller asked for.
        sites = lerd.sites(env)
        if sites is None:
            raise Problem(f"Could not ask lerd for the worktrees of {site or 'the site'}.")
        for entry in sites:
            if entry.get("name") != site:
                continue
            for worktree in entry.get("worktrees") or []:
                if branch in (worktree.get("branch"), worktree.get("git_branch")) and worktree.get("path"):
                    return site, worktree["path"]
            raise Problem(f"Site {site} has no worktree for branch {branch}.")
        raise Problem(f"lerd does not list a site named {site}.")
    return site, path


# Dev server -------------------------------------------------------------


def uses_watcher(command: Optional[str]) -> bool:
    if not command:
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    for index, word in enumerate(words[:-1]):
        if os.path.basename(word) == "lerd" and words[index + 1] == "rust:dev":
            return True
    return False


def dev_config(path: str) -> Dict:
    file = os.path.join(path, ".lerd.yaml")
    try:
        with open(file, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return {"file": False, "command": None, "port": None, "watcher": False}
    except OSError as error:
        return {"file": True, "error": str(error), "command": None, "port": None, "watcher": False}
    try:
        doc = lerdyaml.parse(text)
    except lerdyaml.Unsupported as error:
        return {"file": True, "error": str(error), "command": None, "port": None, "watcher": False}
    proxy = doc.proxy
    return {
        "file": True,
        "proxy": proxy is not None,
        "command": proxy.command if proxy else None,
        "port": proxy.port if proxy else None,
        "watcher": uses_watcher(proxy.command if proxy else None),
        "rustyard": doc.rustyard,
    }


def build_status(site: str, path: str, env: Mapping[str, str]) -> Dict:
    return {
        "site": site,
        "path": path,
        "rust": os.path.isfile(os.path.join(path, "Cargo.toml")),
        "dev": dev_config(path),
        "dev_server": state.read(path, env),
        "now": time.time(),
    }


# Project ----------------------------------------------------------------


def project_info(path: str, env: Mapping[str, str]) -> Dict:
    if not os.path.isfile(os.path.join(path, "Cargo.toml")):
        return {"rust": False}
    try:
        cargo = cargo_mod.find_cargo(env)
    except cargo_mod.CargoNotFound as error:
        return {"rust": True, "error": str(error)}
    quiet = cargo_mod.quiet_env(cargo, env)
    rustc = os.path.join(os.path.dirname(cargo), "rustc")
    info = {
        "rust": True,
        "cargo": cargo_mod.version(cargo, quiet),
        "rustc": cargo_mod.version(rustc if os.path.exists(rustc) else "rustc", quiet),
    }  # type: Dict
    # Run from the checkout so rust-toolchain.toml picks the same toolchain.
    try:
        meta = cargo_mod.metadata(cargo, path, quiet)
    except Exception as error:  # a broken manifest is worth showing as is
        info["error"] = str(error)
        return info
    members = set(meta.get("workspace_members") or [])
    packages = []
    for package in meta.get("packages", []):
        if members and package.get("id") not in members:
            continue
        packages.append({
            "name": package.get("name"),
            "version": package.get("version"),
            "edition": package.get("edition"),
            "rust_version": package.get("rust_version"),
            "bins": cargo_mod.binaries(package),
            "default_run": package.get("default_run"),
            "root": os.path.realpath(package.get("manifest_path", "")) == os.path.realpath(os.path.join(path, "Cargo.toml")),
        })
    info["packages"] = packages
    info["workspace"] = len(packages) > 1
    return info


# cargo check ------------------------------------------------------------


def run_check(path: str, env: Mapping[str, str], all_targets: bool = True, package: str = "") -> Dict:
    if not os.path.isfile(os.path.join(path, "Cargo.toml")):
        raise Problem(f"No Cargo.toml in {path}.")
    if package and not PACKAGE.match(package):
        raise Problem(f"{package!r} is not a cargo package name.")
    cargo = cargo_mod.find_cargo(env)
    command = [cargo, "check", "--message-format=json"]
    if all_targets:
        command.append("--all-targets")
    if package:
        command += ["-p", package]
    started = time.time()
    process = subprocess.Popen(
        command, cwd=path, env=cargo_mod.child_env(cargo, env), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        out, err = process.communicate(timeout=CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        from .watcher import stop_group

        stop_group(process, grace=3.0)
        raise Problem(f"cargo check took longer than {CHECK_TIMEOUT // 60} minutes and was stopped.") from None
    collector = diagnostics.Collector()
    for line in out.decode("utf-8", "replace").splitlines():
        collector.feed(line)
    result = collector.summary()
    result.update({
        "ok": process.returncode == 0,
        "command": " ".join(["cargo"] + command[1:]),
        "path": path,
        "duration": round(time.time() - started, 2),
    })
    if process.returncode != 0 and result["errors"] == 0:
        result["cargo_error"] = cargo_mod.tail(diagnostics.strip_ansi(err.decode("utf-8", "replace")))
    return result


# Human output -----------------------------------------------------------


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def describe(status: Dict) -> List[str]:
    lines = []
    site = status.get("site") or "(not a linked site)"
    lines.append(f"Site     {site}  {status['path']}")
    if not status["rust"]:
        lines.append("Rust     no Cargo.toml here")
        return lines
    dev = status["dev"]
    if dev.get("error"):
        lines.append(f"Dev      .lerd.yaml could not be read: {dev['error']}")
    elif dev.get("watcher"):
        lines.append(f"Dev      rebuild on save: {dev['command']}  (port {dev.get('port')})")
    elif dev.get("command"):
        lines.append(f"Dev      {dev['command']}  (no rebuild on save; run lerd rust:setup)")
    else:
        lines.append("Dev      no dev command in .lerd.yaml; run lerd rust:setup")
    server = status.get("dev_server")
    now = status["now"]
    if not server:
        lines.append("Build    no build recorded by lerd rust:dev")
        return lines
    phase = server.get("phase")
    build = server.get("build")
    if phase == "stopped":
        lines.append("Server   stopped")
    if server.get("building_since"):
        lines.append(f"Build    building for {int(now - server['building_since'])}s")
    if build:
        when = ago(now - build["finished_at"])
        if build["ok"]:
            extra = f", {build['warnings']} warning(s)" if build["warnings"] else ""
            lines.append(f"Last     ok in {build['duration']}s{extra}, {when}")
        else:
            serving = " (still serving the previous build)" if server.get("serving_previous") else ""
            lines.append(f"Last     FAILED with {build['errors']} error(s), {when}{serving}")
            if build.get("cargo_error"):
                lines.append("         " + build["cargo_error"].splitlines()[-1])
    app = server.get("app")
    if app:
        if app.get("listening") is True:
            lines.append(f"App      listening on {app['address']}")
        elif app.get("listening") is False:
            lines.append(f"App      running, not answering on {app['address']} (does it read HOST and PORT?)")
        else:
            lines.append("App      running")
    elif server.get("app_exit") is not None:
        again = ""
        if server.get("restart_at") and phase != "stopped":
            again = f", starting again in {max(0, int(server['restart_at'] - now))}s"
        lines.append(f"App      exited with status {server['app_exit']}{again}")
    if build:
        for item in build["diagnostics"][:10]:
            where = f"{item['file']}:{item['line']}:{item['column']}" if item.get("file") else "-"
            lines.append(f"  {item['level']:<7} {where}  {item['message']}")
        if len(build["diagnostics"]) > 10 or build.get("truncated"):
            lines.append("  ...")
    return lines


# Entry points -----------------------------------------------------------


def _stdin_json() -> Dict:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise Problem("Expected a JSON object.")
    return data


def _answer(data: Dict) -> int:
    sys.stdout.write(json.dumps(data, indent=None if os.environ.get("LERD_SURFACE") == "api" else 2) + "\n")
    return 0


def _fail(message: str) -> int:
    sys.stderr.write(message.rstrip() + "\n")
    return 1


def status_main(argv: List[str], env: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if env is None else env
    surface = env.get("LERD_SURFACE", "cli")
    try:
        if surface == "api":
            site, path = checkout(env, query(env).get("branch", ""))
            return _answer(build_status(site, path, env))
        if surface == "mcp":
            args = _stdin_json()
            site, path = checkout(env, str(args.get("branch") or ""))
            return _answer(build_status(site, path, env))
        if argv and argv[0] in ("-h", "--help"):
            sys.stdout.write("usage: lerd rust:status [--json]\nShow the dev server's last build for the site in this directory.\n")
            return 0
        site, path = checkout(env)
        status = build_status(site, path, env)
        if "--json" in argv:
            return _answer(status)
        sys.stdout.write("\n".join(describe(status)) + "\n")
        return 0
    except (Problem, ValueError) as error:
        return _fail(str(error))


def project_main(argv: List[str], env: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if env is None else env
    try:
        site, path = checkout(env, query(env).get("branch", ""))
        return _answer(project_info(path, env))
    except Problem as error:
        return _fail(str(error))


def check_main(argv: List[str], env: Optional[Mapping[str, str]] = None) -> int:
    env = os.environ if env is None else env
    surface = env.get("LERD_SURFACE", "cli")
    try:
        if surface == "mcp":
            args = _stdin_json()
            branch = str(args.get("branch") or "")
            all_targets = args.get("all_targets", True) is not False
            package = str(args.get("package") or "")
        else:
            params = query(env)
            branch = params.get("branch", "")
            all_targets = params.get("all_targets", "1") not in ("0", "false")
            package = params.get("package", "")
        site, path = checkout(env, branch)
        return _answer(run_check(path, env, all_targets=all_targets, package=package))
    except (Problem, cargo_mod.CargoNotFound, ValueError) as error:
        return _fail(str(error))
