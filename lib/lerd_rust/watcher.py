"""`lerd rust:dev`: a Lerd dev-server command that rebuilds the app on save.

Each change is built with `cargo build` while the running app keeps serving.
Only a successful build replaces it: the old process group is stopped as a
whole and the executable that build produced is started, with the environment
`cargo run` would give it. Starting the built file, rather than `cargo run`,
means a save between the build and the start can never compile into the
running site. A failed build leaves the previous build serving and says so in
the log, the site tab and, when allowed, a notification. Everything is written
to stdout, the Dev Server log.
"""

from __future__ import annotations

import argparse
import collections
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from . import cargo as cargo_mod
from . import diagnostics, lerd, state

IGNORED_DIRS = {"target", ".git", "node_modules"}
KEPT_HIDDEN_DIRS = {".cargo"}
IGNORED_FILES = {".lerd.yaml", "4913", ".DS_Store"}
IGNORED_SUFFIXES = (
    "~", ".swp", ".swo", ".swx", ".tmp",
    # What a running app writes into its own project: SQLite databases and
    # their journals, and logs. Watching them restarts the app in a loop.
    ".db", ".sqlite", ".sqlite3", "-wal", "-shm", "-journal", ".log",
)
LARGE_TREE = 20000
STOP_GRACE = 8.0
READY_WARNING_AFTER = 30.0
DEBOUNCE = 0.2
# A file that keeps changing must not hold the loop forever.
MAX_QUIET_WAIT = 2.0

Snapshot = Dict[str, Tuple[int, int]]


def log(message: str) -> None:
    sys.stdout.write(f"[rust:dev] {message}\n")
    sys.stdout.flush()


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    argv = list(argv)
    if "--" in argv:
        split = argv.index("--")
        own, run_args = argv[:split], argv[split + 1 :]
    else:
        own, run_args = argv, []
    parser = argparse.ArgumentParser(
        prog="lerd rust:dev",
        description="Run the app with `cargo run` and rebuild it on every save. "
        "Arguments after -- go to cargo run unchanged, e.g. lerd rust:dev -- --bin server.",
    )
    parser.add_argument("--watch", action="append", default=[], metavar="PATH",
                        help="watch only these paths (repeatable); default: the whole cargo workspace")
    parser.add_argument("--ignore", action="append", default=[], metavar="PATH",
                        help="never watch this path (repeatable)")
    parser.add_argument("--poll", type=int, default=500, metavar="MS",
                        help="how often to look for changes, in milliseconds (default 500)")
    parser.add_argument("--no-notify", action="store_true",
                        help="never send a notification when a build fails")
    options = parser.parse_args(own)
    if options.poll < 100:
        parser.error("--poll must be at least 100 milliseconds")
    options.run_args = run_args
    return options


# Watching ---------------------------------------------------------------


def ignored_file(name: str) -> bool:
    return (
        name in IGNORED_FILES
        or name.endswith(IGNORED_SUFFIXES)
        or name.startswith(".#")
        or (name.startswith("#") and name.endswith("#"))
    )


class Tree:
    """Polls file sizes and modification times. Polling needs no extra
    package, sees every editor's way of saving, and costs a few milliseconds
    for a normal project. Symlinks are never followed."""

    def __init__(self, roots: Sequence[str], ignore: Sequence[str]) -> None:
        # Real paths throughout, so an ignored folder cargo names (its target
        # directory) matches however the project's own path was spelled.
        self.roots = [os.path.realpath(r) for r in roots]
        self.ignore = {os.path.realpath(p) for p in ignore}

    def scan(self) -> Snapshot:
        out = {}  # type: Snapshot
        for root in self.roots:
            try:
                info = os.lstat(root)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                out[root] = (info.st_mtime_ns, info.st_size)
            elif stat.S_ISDIR(info.st_mode):
                self._walk(root, out)
        return out

    def _walk(self, top: str, out: Snapshot) -> None:
        stack = [top]
        while stack:
            folder = stack.pop()
            try:
                entries = os.scandir(folder)
            except OSError:
                continue
            with entries:
                for entry in entries:
                    try:
                        if entry.is_symlink() or entry.path in self.ignore:
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            name = entry.name
                            if name in IGNORED_DIRS or (name.startswith(".") and name not in KEPT_HIDDEN_DIRS):
                                continue
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False) and not ignored_file(entry.name):
                            info = entry.stat(follow_symlinks=False)
                            out[entry.path] = (info.st_mtime_ns, info.st_size)
                    except OSError:
                        continue  # removed while we looked


def changes(before: Snapshot, after: Snapshot) -> List[str]:
    changed = [p for p, v in after.items() if before.get(p) != v]
    changed += [p for p in before if p not in after]
    return sorted(changed)


def describe(paths: Sequence[str], base: str) -> str:
    names = []
    for path in paths[:3]:
        try:
            names.append(os.path.relpath(path, base))
        except ValueError:
            names.append(path)
    more = len(paths) - len(names)
    return ", ".join(names) + (f" and {more} more" if more > 0 else "")


# Processes --------------------------------------------------------------


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_group(process: subprocess.Popen, grace: float = STOP_GRACE) -> None:
    """Stop a process started with start_new_session and everything it
    started, waiting until the group is gone so its port is free again."""
    pgid = process.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        process.poll()
        if not group_alive(pgid):
            break
        time.sleep(0.05)
    else:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        while group_alive(pgid) and time.monotonic() < deadline + 2:
            process.poll()
            time.sleep(0.05)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


class Build:
    def __init__(self, cargo: str, root: str, args: List[str], env: Mapping[str, str], color: bool) -> None:
        fmt = "json-diagnostic-rendered-ansi" if color else "json"
        self.command = [cargo, "build", f"--message-format={fmt}", *args]
        self.started = time.time()
        self.collector = diagnostics.Collector()
        self.other = collections.deque(maxlen=40)  # type: collections.deque
        self.error = None  # type: Optional[str]
        self.process = None  # type: Optional[subprocess.Popen]
        self.reader = None  # type: Optional[threading.Thread]
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=root,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as error:
            self.error = f"could not start cargo: {error}"
            return
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for raw in self.process.stdout:
            # Whatever a line holds, keep draining the pipe so cargo never blocks.
            try:
                line = raw.decode("utf-8", "replace")
                was_json, text = self.collector.feed(line)
                if not was_json and line.strip():
                    self.other.append(diagnostics.strip_ansi(line.rstrip()))
                if text:
                    sys.stdout.write(text if text.endswith("\n") else text + "\n")
                    sys.stdout.flush()
            except Exception as error:  # noqa: BLE001
                self.other.append(f"(could not read a line of cargo's output: {error})")

    def done(self) -> bool:
        if self.process is None:
            return True
        return self.process.poll() is not None and not (self.reader and self.reader.is_alive())

    def cancel(self) -> None:
        if self.process is None:
            return
        stop_group(self.process, grace=3.0)
        if self.reader:
            self.reader.join(timeout=2)

    def result(self) -> dict:
        ok = self.process is not None and self.process.returncode == 0
        summary = self.collector.summary()
        finished = time.time()
        result = dict(summary, ok=ok, started_at=self.started, finished_at=finished,
                      duration=round(finished - self.started, 2))
        if not ok and summary["errors"] == 0:
            code = self.process.returncode if self.process else None
            result["cargo_error"] = self.error or "\n".join(self.other) or f"cargo build exited with {code}"
        return result

    def executable(self) -> Optional[dict]:
        """The one program this build produced to run; None when it made none
        or several, and only cargo run can tell which one is meant."""
        found = {e["executable"]: e for e in self.collector.executables}
        return next(iter(found.values())) if len(found) == 1 else None


class Probe:
    """Checks that the app answers on the HOST and PORT Lerd gave it: a Rust
    app that hardcodes 127.0.0.1:3000 runs fine and is never reached."""

    def __init__(self, env: Mapping[str, str]) -> None:
        host = env.get("HOST", "")
        port = env.get("PORT", "")
        self.port = int(port) if port.isdigit() else None
        self.host = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        self.address = f"{host or '127.0.0.1'}:{port}" if self.port else None
        self.reset()

    def reset(self) -> None:
        self.since = time.monotonic()
        self.ready_at = None  # type: Optional[float]
        self.warned = False

    @property
    def enabled(self) -> bool:
        return self.port is not None

    @property
    def listening(self) -> Optional[bool]:
        """True once it answered, False once it is overdue, None until then."""
        if not self.enabled:
            return None
        if self.ready_at is not None:
            return True
        return False if self.warned else None

    def check(self) -> bool:
        if not self.enabled or self.ready_at is not None:
            return False
        try:
            with socket.create_connection((self.host, self.port), timeout=0.2):
                pass
        except OSError:
            return False
        self.ready_at = time.time()
        return True

    def overdue(self) -> bool:
        return (
            self.enabled
            and self.ready_at is None
            and not self.warned
            and time.monotonic() - self.since > READY_WARNING_AFTER
        )


# Cargo arguments --------------------------------------------------------


def _option_value(args: Sequence[str], short: str, long: str) -> Optional[str]:
    for index, arg in enumerate(args):
        if arg in (short, long) and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(long + "="):
            return arg[len(long) + 1 :]
        if short and arg.startswith(short) and len(arg) > len(short) and not arg.startswith("--"):
            return arg[len(short) :]
    return None


def build_args(selection: List[str], cargo: str, root: str, env: Mapping[str, str],
               meta: Optional[dict] = None) -> List[str]:
    """`cargo build` builds every binary while `cargo run` builds the one it
    runs. Name that one, as cargo run picks it, so a broken second binary
    never blocks the app from restarting."""
    if meta is None or any(
        a in ("--bin", "--example", "--manifest-path") or a.startswith(("--bin=", "--example=", "--manifest-path="))
        for a in selection
    ):
        return selection
    name = _option_value(selection, "-p", "--package")
    if name:
        package = next((p for p in meta.get("packages", []) if p.get("name") == name), None)
    else:
        package = cargo_mod.root_package(meta, root)
    if package is None:
        return selection
    if package.get("default_run"):
        return selection + ["--bin", package["default_run"]]
    bins = cargo_mod.binaries(package)
    if len(bins) == 1:
        return selection + ["--bin", bins[0]]
    return selection


def read_metadata(cargo: str, root: str, env: Mapping[str, str]) -> Optional[dict]:
    try:
        return cargo_mod.metadata(cargo, root, env)
    except Exception:  # cargo build reports a broken manifest itself
        return None


def workspace_root(cargo: str, root: str, env: Mapping[str, str]) -> str:
    try:
        result = subprocess.run(
            [cargo, "locate-project", "--workspace", "--message-format", "plain"],
            cwd=root, env=dict(env), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return root
    manifest = result.stdout.decode("utf-8", "replace").strip()
    if result.returncode == 0 and manifest.endswith("Cargo.toml"):
        return os.path.dirname(manifest)
    return root


def wants_color(env: Mapping[str, str], stream) -> bool:
    if env.get("NO_COLOR"):
        return False
    if env.get("CARGO_TERM_COLOR") in ("never",):
        return False
    if env.get("CLICOLOR_FORCE") not in (None, "", "0") or env.get("FORCE_COLOR") not in (None, "", "0"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


# The loop ---------------------------------------------------------------


class Watcher:
    def __init__(self, options: argparse.Namespace, env: Mapping[str, str], root: str, cargo: str) -> None:
        self.options = options
        self.env = env
        self.root = root
        self.cargo = cargo
        self.run_env = cargo_mod.child_env(cargo, env)
        self.color = wants_color(env, sys.stdout)
        if self.color:
            self.run_env.setdefault("CARGO_TERM_COLOR", "always")
        args = options.run_args
        split = args.index("--") if "--" in args else len(args)
        self.selection = list(args[:split])
        self.app_args = list(args[split + 1 :])
        self.meta = None  # type: Optional[dict]
        self.state_failed = False
        self.stop = threading.Event()
        self.build = None  # type: Optional[Build]
        self.app = None  # type: Optional[subprocess.Popen]
        self.app_started = None  # type: Optional[float]
        self.app_exit = None  # type: Optional[int]
        self.last = None  # type: Optional[dict]
        self.failure_notified = False
        self.probe = Probe(env)
        self.site = env.get("LERD_SITE", "")
        self.notify = bool(env.get("LERD_SURFACE")) and not options.no_notify

    # State shown to the site tab, rust:status and assistants.
    def phase(self) -> str:
        if self.build is not None:
            return "building"
        if self.last is not None and not self.last["ok"]:
            return "failed"
        if self.app is not None:
            return "running"
        return "exited" if self.app_exit is not None else "starting"

    def save(self, phase: Optional[str] = None) -> None:
        app = None
        if self.app is not None:
            app = {
                "pid": self.app.pid,
                "started_at": self.app_started,
                "address": self.probe.address,
                "listening": self.probe.listening,
                "ready_at": self.probe.ready_at,
            }
        try:
            self._write_state(phase, app)
        except OSError as error:
            # The tab loses its view of the build; the site itself is fine.
            if not self.state_failed:
                self.state_failed = True
                log(f"could not record the build for the Rust tab: {error}")

    def _write_state(self, phase: Optional[str], app: Optional[dict]) -> None:
        state.write(self.root, {
            "pid": os.getpid(),
            "site": self.site,
            "domain": self.env.get("LERD_DOMAIN", ""),
            "phase": phase or self.phase(),
            "cargo_run_args": self.options.run_args,
            "building_since": self.build.started if self.build else None,
            "build": self.last,
            "app": app,
            "app_exit": self.app_exit,
            "serving_previous": bool(self.app is not None and self.last is not None and not self.last["ok"]),
        }, self.env)

    def start_build(self) -> None:
        if self.build is not None:
            self.build.cancel()
            log("stopped the unfinished build")
        self.meta = read_metadata(self.cargo, self.root, self.run_env)
        args = build_args(self.selection, self.cargo, self.root, self.run_env, self.meta)
        self.build = Build(self.cargo, self.root, args, self.run_env, self.color)
        log("building: " + " ".join(["cargo", "build", *args]))
        self.save()

    def finish_build(self) -> None:
        assert self.build is not None
        result = self.build.result()
        executable = self.build.executable()
        self.build = None
        self.last = result
        if result["ok"]:
            self.failure_notified = False
            log(f"build finished in {result['duration']}s"
                + (f" with {result['warnings']} warning(s)" if result["warnings"] else ""))
            self.start_app(executable)
        else:
            serving = "still serving the previous build" if self.app is not None else "nothing is serving the site yet"
            count = f"{result['errors']} error(s)" if result["errors"] else "cargo failed"
            log(f"BUILD FAILED ({count}); {serving}. Fix the error and save to rebuild.")
            if "cargo_error" in result:
                sys.stdout.write(result["cargo_error"] + "\n")
                sys.stdout.flush()
            self.send_failure(result)
        self.save()

    def send_failure(self, result: dict) -> None:
        if not self.notify or self.failure_notified:
            return
        self.failure_notified = True
        first = next((d for d in result["diagnostics"] if d["level"] == "error"), None)
        if first and first.get("file"):
            body = f"{first['file']}:{first['line']}: {first['message']}"
        elif first:
            body = first["message"]
        else:
            body = (result.get("cargo_error") or "cargo build failed").splitlines()[-1]
        body += " Still serving the previous build." if self.app is not None else ""
        sent, reason = lerd.notify(f"Build failed: {self.site or os.path.basename(self.root)}", body, self.site, self.env)
        if not sent:
            log(f"no notification sent: {reason}")

    def start_app(self, executable: Optional[dict] = None) -> None:
        if self.app is not None:
            log("stopping the previous build")
            stop_group(self.app)
            self.app = None
        if executable:
            # Exactly what was just built, never source saved since.
            command = [executable["executable"], *self.app_args]
            package = cargo_mod.package_by_id(self.meta, executable.get("package_id"))
            env = cargo_mod.run_env(self.cargo, package, executable["executable"], self.run_env)
            shown = " ".join([os.path.relpath(executable["executable"], self.root), *self.app_args])
        else:
            # The build made several programs; only cargo run knows which one.
            command = [self.cargo, "run", *self.options.run_args]
            env = self.run_env
            shown = " ".join(["cargo", "run", *self.options.run_args])
        try:
            self.app = subprocess.Popen(
                command, cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                stdout=None, stderr=subprocess.STDOUT, start_new_session=True,
            )
        except OSError as error:
            self.app_exit = 127
            log(f"could not start {shown}: {error}")
            return
        self.app_started = time.time()
        self.app_exit = None
        self.probe.reset()
        log("started: " + shown)

    def watch_app(self) -> None:
        if self.app is None:
            return
        code = self.app.poll()
        if code is None:
            if self.probe.check():
                log(f"listening on {self.probe.address} after {self.probe.ready_at - self.app_started:.1f}s")
                self.save()
            elif self.probe.overdue():
                self.probe.warned = True
                log(f"the app is running but nothing answers on {self.probe.address}. Under Lerd it must "
                    "listen on the HOST and PORT environment variables, not a hardcoded address.")
                self.save()
            return
        # The app is gone; make sure nothing it started keeps the port.
        stop_group(self.app, grace=2.0)
        self.app = None
        self.app_exit = code
        log(f"the app exited with status {code}; save a file to rebuild and start it again")
        self.save()

    def wait_for_quiet(self, tree: Tree, snapshot: Snapshot) -> Tuple[Snapshot, List[str]]:
        changed = []  # type: List[str]
        deadline = time.monotonic() + MAX_QUIET_WAIT
        while not self.stop.is_set() and time.monotonic() < deadline:
            self.stop.wait(DEBOUNCE)
            again = tree.scan()
            if again == snapshot:
                break
            changed += changes(snapshot, again)
            snapshot = again
        return snapshot, changed

    def run(self) -> int:
        parent = os.getppid()
        workspace = workspace_root(self.cargo, self.root, self.run_env)
        roots = [os.path.join(self.root, p) for p in self.options.watch] or [workspace]
        ignore = [os.path.join(self.root, p) for p in self.options.ignore]
        for target in (self.env.get("CARGO_TARGET_DIR"), self.env.get("CARGO_BUILD_TARGET_DIR")):
            if target:
                ignore.append(os.path.join(self.root, target))
        meta = read_metadata(self.cargo, self.root, self.run_env)
        if meta and meta.get("target_directory"):
            # Wherever cargo builds, also when .cargo/config.toml moves it.
            ignore.append(meta["target_directory"])
        tree = Tree(roots, ignore)
        snapshot = tree.scan()
        log(f"watching {len(snapshot)} files in " + ", ".join(os.path.relpath(r, self.root) for r in roots))
        if len(snapshot) > LARGE_TREE:
            log("that is a lot of files to poll; narrow it with --watch src --watch Cargo.toml ...")
        if self.probe.enabled:
            log(f"the app should listen on {self.probe.address}")
        self.start_build()
        poll = self.options.poll / 1000.0
        while not self.stop.is_set():
            if os.getppid() != parent:
                log("the process that started this one has gone; stopping")
                break
            if self.build is not None and self.build.done():
                self.finish_build()
            self.watch_app()
            self.stop.wait(poll)
            if self.stop.is_set():
                break
            current = tree.scan()
            if current != snapshot:
                found = changes(snapshot, current)
                snapshot, more = self.wait_for_quiet(tree, current)
                if self.stop.is_set():
                    break
                log("changed: " + describe(sorted(set(found + more)), os.path.realpath(self.root)))
                self.start_build()
        self.shutdown()
        return 0

    def shutdown(self) -> None:
        if self.build is not None:
            self.build.cancel()
            self.build = None
        if self.app is not None:
            log("stopping the app")
            stop_group(self.app)
            self.app = None
        self.save("stopped")


def main(argv: Sequence[str], env: Optional[Mapping[str, str]] = None) -> int:
    env = dict(os.environ if env is None else env)
    options = parse_args(argv)
    root = os.getcwd()
    if not os.path.isfile(os.path.join(root, "Cargo.toml")):
        sys.stderr.write(f"lerd rust:dev: no Cargo.toml in {root}\n")
        return 2
    try:
        cargo = cargo_mod.find_cargo(env)
    except cargo_mod.CargoNotFound as error:
        sys.stderr.write(f"lerd rust:dev: {error}\n")
        return 2
    watcher = Watcher(options, env, root, cargo)

    def request_stop(signum, _frame):
        watcher.stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, request_stop)
    try:
        return watcher.run()
    finally:
        # Even on an unexpected error, never leave cargo or the app behind.
        if watcher.build is not None or watcher.app is not None:
            watcher.shutdown()
