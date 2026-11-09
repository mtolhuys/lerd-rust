"""`lerd rust:setup`: make `lerd rust:dev` the site's dev command.

It changes one thing, the `command` under `proxy:` in .lerd.yaml, or writes a
new .lerd.yaml when there is none, and only after showing the change and
getting a yes. Lerd's own consent gate stays in charge: the next `lerd link`
shows the new command and asks before running it.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import socket
import sys
import tempfile
from typing import List, Mapping, Optional, Sequence

from . import lerd, lerdyaml

ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Lerd runs the dev command through sh. Rewriting one that relies on the shell
# (a variable, a pipe, a redirect) would silently change what it does.
SHELL_SYNTAX = re.compile(r"[$`|&;<>(){}*?\[\]~\\]|\n")
FIRST_PORT = 3100
LAST_PORT = 3999


class Refused(Exception):
    pass


def plan(current: Optional[str], cargo_args: Optional[List[str]]) -> str:
    """The dev command to use instead of current. cargo_args, when given,
    replace the arguments carried over from `cargo run ...`."""
    prefix = []  # type: List[str]
    carried = []  # type: List[str]
    if current and current.strip():
        if SHELL_SYNTAX.search(current):
            raise Refused(
                f"The dev command `{current}` uses shell syntax ($, |, >, ; ...), so rewriting it could change "
                "what it does. Set proxy.command in .lerd.yaml by hand to `lerd rust:dev -- <cargo run arguments>`."
            )
        try:
            words = shlex.split(current)
        except ValueError:
            raise Refused(f"The current dev command could not be read: {current}") from None
        if words and words[0] == "env":
            words = words[1:]
        while words and ASSIGNMENT.match(words[0]):
            prefix.append(words.pop(0))
        if len(words) >= 2 and os.path.basename(words[0]) == "lerd" and words[1] == "rust:dev":
            if cargo_args is None:
                return current
            carried = []
        elif len(words) >= 2 and os.path.basename(words[0]) == "cargo" and words[1] == "run":
            carried = words[2:]
        elif len(words) >= 2 and os.path.basename(words[0]) == "cargo" and words[1].startswith("+"):
            raise Refused(
                f"The dev command picks a toolchain with {words[1]}. Pin it in rust-toolchain.toml "
                "instead, then run lerd rust:setup again."
            )
        else:
            raise Refused(
                f"The dev command is `{current}`, not `cargo run ...`, so it is left alone. "
                "To switch by hand, set proxy.command in .lerd.yaml to `lerd rust:dev -- <cargo run arguments>`."
            )
    args = carried if cargo_args is None else cargo_args
    words = prefix + ["lerd", "rust:dev"] + (["--"] + args if args else [])
    return " ".join(shlex.quote(w) for w in words)


def port_free(port: int) -> bool:
    for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET, "127.0.0.1")):
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((address, port))
            except OSError:
                return False
    return True


def free_port(env: Mapping[str, str]) -> int:
    taken = set()
    for site in lerd.sites(env) or []:
        port = site.get("host_port")
        if isinstance(port, int):
            taken.add(port)
    for port in range(FIRST_PORT, LAST_PORT + 1):
        if port not in taken and port_free(port):
            return port
    raise Refused(f"No free port between {FIRST_PORT} and {LAST_PORT}; pass --port.")


def reads_port(root: str) -> bool:
    """A hint only: does any source file mention PORT at all?"""
    for folder, dirs, files in os.walk(os.path.join(root, "src")):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if not name.endswith(".rs"):
                continue
            try:
                with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as handle:
                    if "PORT" in handle.read():
                        return True
            except OSError:
                continue
    return False


def write_atomic(path: str, text: str) -> None:
    folder = os.path.dirname(path)
    mode = None
    try:
        mode = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        pass
    handle, temporary = tempfile.mkstemp(dir=folder, prefix=".lerd.yaml.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as out:
            out.write(text)
        os.chmod(temporary, mode if mode is not None else 0o644)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    argv = list(argv)
    cargo_args = None
    if "--" in argv:
        split = argv.index("--")
        argv, cargo_args = argv[:split], argv[split + 1 :]
    parser = argparse.ArgumentParser(
        prog="lerd rust:setup",
        description="Make `lerd rust:dev` this site's dev command in .lerd.yaml. "
        "Arguments after -- become the cargo run arguments, e.g. lerd rust:setup -- --bin server.",
    )
    parser.add_argument("--yes", action="store_true", help="write without asking")
    parser.add_argument("--dry-run", action="store_true", help="show the change and write nothing")
    parser.add_argument("--port", type=int, help="the port for a new .lerd.yaml (default: the first free one from 3100)")
    options = parser.parse_args(argv)
    if options.port is not None and not 1 <= options.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    options.cargo_args = cargo_args
    return options


def main(argv: Sequence[str], env: Optional[Mapping[str, str]] = None, stdin=None) -> int:
    env = os.environ if env is None else env
    stdin = sys.stdin if stdin is None else stdin
    options = parse_args(argv)
    root = os.getcwd()
    path = os.path.join(root, ".lerd.yaml")
    try:
        if not os.path.isfile(os.path.join(root, "Cargo.toml")):
            raise Refused(f"No Cargo.toml in {root}. Run this in the Rust project Lerd serves.")
        if os.path.exists(path):
            with open(path, encoding="utf-8", newline="") as handle:
                text = handle.read()
            try:
                doc = lerdyaml.parse(text)
            except lerdyaml.Unsupported as error:
                raise Refused(f".lerd.yaml is in a shape this command does not edit ({error}). Nothing was changed.") from None
            if doc.rustyard:
                raise Refused(
                    "This .lerd.yaml is Rustyard's generated adapter. Keep using `rustyard up`, or move the "
                    "adapter aside and run lerd rust:setup again to switch this project to the plugin."
                )
            if doc.proxy is None:
                kind = "a PHP or container site" if {"php_version", "container", "framework"} & set(doc.top_keys) else "not a host-proxy site"
                raise Refused(f".lerd.yaml has no proxy block: this is {kind}. Nothing was changed.")
            command = plan(doc.proxy.command, options.cargo_args)
            if command == doc.proxy.command:
                print(f"Already set: proxy.command is `{command}`.")
                return 0
            new_text = lerdyaml.with_command(doc, command)
            print("In .lerd.yaml, proxy.command changes:")
            print(f"  from: {doc.proxy.command if doc.proxy.command else '(none)'}")
            print(f"  to:   {command}")
        else:
            command = plan(None, options.cargo_args)
            port = options.port or free_port(env)
            new_text = lerdyaml.new_file(command, port)
            print("A new .lerd.yaml:")
            print("".join("  " + line for line in new_text.splitlines(keepends=True)), end="")
        if options.dry_run:
            print("Dry run: nothing was written.")
            return 0
        if not options.yes:
            if not stdin.isatty():
                raise Refused("Not a terminal: pass --yes to write the change.")
            sys.stdout.write("Write it? [y/N] ")
            sys.stdout.flush()
            if stdin.readline().strip().lower() not in ("y", "yes"):
                print("Nothing was changed.")
                return 1
        write_atomic(path, new_text)
    except Refused as error:
        sys.stderr.write(f"lerd rust:setup: {error}\n")
        return 1
    print("Written. Next, run `lerd link` here: Lerd shows the new command, asks you to approve it, and starts it.")
    if not reads_port(root):
        print("Note: nothing in src/ mentions PORT. Under Lerd the app must listen on the HOST and PORT "
              "environment variables; lerd rust:dev warns when nothing answers there.")
    return 0
