#!/usr/bin/env python3
"""A stand-in for cargo, for the watcher's tests. It answers the handful of
commands lerd rust:dev runs. A build "succeeds" unless src/main.rs holds
BROKEN, and writes target/debug/app: a script that runs fake_app.py with the
source as it was when it was built, the way a real binary keeps its code."""

import json
import os
import stat
import sys
import time

args = sys.argv[1:]
root = os.getcwd()
here = os.path.dirname(os.path.abspath(__file__))
log_dir = os.environ.get("FAKE_CARGO_LOG", root)
bins = int(os.environ.get("FAKE_BINS", "1"))
names = ["app", "tool"][:bins]


def record(name, value):
    with open(os.path.join(log_dir, name), "a") as out:
        out.write(f"{value}\n")


def built(name, version):
    path = os.path.join(root, "target", "debug", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as out:
        out.write(f'#!/bin/sh\nexec {sys.executable} {os.path.join(here, "fake_app.py")} {version} "$@"\n')
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


if args[:1] == ["locate-project"]:
    print(os.path.join(root, "Cargo.toml"))
elif args[:1] == ["metadata"]:
    print(json.dumps({
        "packages": [{
            "id": "app 0.1.0", "name": "app", "version": "0.1.0-beta.2", "edition": "2021",
            "manifest_path": os.path.join(root, "Cargo.toml"), "default_run": None, "authors": ["A", "B"],
            "targets": [{"name": n, "kind": ["bin"]} for n in names],
        }],
        "workspace_members": ["app 0.1.0"],
        "target_directory": os.environ.get("FAKE_TARGET_DIR", os.path.join(root, "target")),
    }))
elif args[:1] == ["build"]:
    record("builds.log", " ".join(args))
    with open(os.path.join(root, "src", "main.rs")) as handle:
        source = handle.read().strip()
    record("read.log", source)  # tests wait for this before changing the source
    time.sleep(float(os.environ.get("FAKE_BUILD_SECONDS", "0.2")))
    print("   Compiling app v0.1.0", file=sys.stderr, flush=True)
    # Something a build script or proc macro printed that looks like JSON.
    print(json.dumps({"reason": "compiler-message", "message": "not a diagnostic"}), flush=True)
    if "BROKEN" in source:
        print(json.dumps({"reason": "compiler-message", "target": {"name": "app", "kind": ["bin"]}, "message": {
            "level": "error", "message": "cannot find value `BROKEN` in this scope",
            "code": {"code": "E0425"}, "rendered": "error[E0425]: cannot find value `BROKEN`\n",
            "spans": [{"file_name": "src/main.rs", "line_start": 1, "column_start": 1, "is_primary": True}],
        }}), flush=True)
        print(json.dumps({"reason": "build-finished", "success": False}), flush=True)
        sys.exit(101)
    for name in names:
        print(json.dumps({"reason": "compiler-artifact", "package_id": "app 0.1.0",
                          "manifest_path": os.path.join(root, "Cargo.toml"),
                          "target": {"name": name, "kind": ["bin"]}, "executable": built(name, source)}), flush=True)
    print(json.dumps({"reason": "build-finished", "success": True}), flush=True)
elif args[:1] == ["run"]:
    # Only reached when the build made several programs.
    record("runs.log", " ".join(args))
    with open(os.path.join(root, "src", "main.rs")) as handle:
        source = handle.read().strip()
    os.execv(sys.executable, [sys.executable, os.path.join(here, "fake_app.py"), "run-" + source])
else:
    print(f"fake cargo: unexpected {args}", file=sys.stderr)
    sys.exit(3)
