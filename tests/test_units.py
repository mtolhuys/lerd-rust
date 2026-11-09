import io
import json
import os
import socket
import tempfile
import time
import unittest
from unittest import mock

import helpers  # noqa: F401  (puts lib/ on the path)
from lerd_rust import cargo, diagnostics, lerdyaml, setup, state, status, watcher


def write(path, text):
    with open(path, "w") as handle:
        handle.write(text)


def message(level, text, file=None, line=1, rendered=None, target="app"):
    spans = [{"file_name": file, "line_start": line, "column_start": 5, "is_primary": True}] if file else []
    return json.dumps({"reason": "compiler-message", "target": {"name": target}, "message": {
        "level": level, "message": text, "code": {"code": "E0425"} if level == "error" else None,
        "rendered": rendered or f"{level}: {text}\n", "spans": spans}})


class DiagnosticsTest(unittest.TestCase):
    def test_collects_errors_first_and_drops_summaries_and_duplicates(self):
        c = diagnostics.Collector()
        lines = [
            message("warning", "unused variable: `x`", "src/main.rs", 3),
            message("error", "cannot find value `y`", "src/main.rs", 7, rendered="\x1b[31merror\x1b[0m: y\n"),
            message("error", "cannot find value `y`", "src/main.rs", 7),  # lib and test build
            message("error", "aborting due to 1 previous error"),
            message("warning", "1 warning emitted"),
            message("note", "a note"),
            message("failure-note", "For more information about this error, try `rustc --explain E0425`."),
            message("error", "linking with `cc` failed: exit status: 1"),
            json.dumps({"reason": "compiler-artifact"}),
            json.dumps({"reason": "build-finished", "success": False}),
        ]
        logged = [c.feed(l)[1] for l in lines]
        summary = c.summary()
        self.assertFalse(c.success)
        self.assertEqual((summary["errors"], summary["warnings"]), (2, 1))
        self.assertEqual([d["level"] for d in summary["diagnostics"]], ["error", "error", "warning"])
        first = summary["diagnostics"][0]
        self.assertEqual((first["file"], first["line"], first["column"], first["code"]), ("src/main.rs", 7, 5, "E0425"))
        self.assertEqual(first["rendered"], "error: y")  # ANSI stripped for storage
        self.assertIn("\x1b[31merror\x1b[0m: y\n", logged)  # but kept for the log
        self.assertIsNone(summary["diagnostics"][1]["file"])  # a linker error has no span

    def test_survives_lines_that_only_look_like_cargo_messages(self):
        c = diagnostics.Collector()
        for line in ['{"reason":"compiler-message","message":"x"}', '{"reason":"compiler-message","message":{"level":3,"spans":"no"}}',
                     '{"reason":"compiler-artifact","target":"x"}', '[1, 2]']:
            c.feed(line)
        self.assertEqual(c.summary()["diagnostics"], [])

    def test_records_built_programs(self):
        c = diagnostics.Collector()
        c.feed(json.dumps({"reason": "compiler-artifact", "package_id": "p", "manifest_path": "/m/Cargo.toml",
                           "target": {"name": "web", "kind": ["bin"]}, "executable": "/t/debug/web"}))
        c.feed(json.dumps({"reason": "compiler-artifact", "target": {"name": "build-script-build", "kind": ["custom-build"]},
                           "executable": "/t/build/x"}))
        c.feed(json.dumps({"reason": "compiler-artifact", "target": {"name": "lib", "kind": ["lib"]}, "executable": None}))
        self.assertEqual([e["executable"] for e in c.executables], ["/t/debug/web"])

    def test_non_json_lines_are_passed_through(self):
        c = diagnostics.Collector()
        self.assertEqual(c.feed("   Compiling app v0.1.0\n"), (False, "   Compiling app v0.1.0\n"))
        self.assertEqual(c.feed("{not json\n"), (False, "{not json\n"))

    def test_limit(self):
        c = diagnostics.Collector()
        for i in range(60):
            c.feed(message("warning", f"w{i}", "src/lib.rs", i))
        summary = c.summary()
        self.assertEqual(len(summary["diagnostics"]), diagnostics.LIMIT)
        self.assertTrue(summary["truncated"])
        self.assertEqual(summary["warnings"], 60)


class LerdYamlTest(unittest.TestCase):
    def test_reads_lerds_own_shape(self):
        doc = lerdyaml.parse("domains:\n  - api\nsecured: true\nproxy:\n  command: cargo run --bin server  # dev\n  port: 3100\nservices:\n  - postgres\n")
        self.assertEqual(doc.proxy.command, "cargo run --bin server")
        self.assertEqual(doc.proxy.port, 3100)
        self.assertEqual(doc.top_keys, ["domains", "secured", "proxy", "services"])

    def test_quoted_values(self):
        self.assertEqual(lerdyaml.parse('proxy:\n  command: "a \\"b\\" c"\n  port: "8000"\n').proxy.command, 'a "b" c')
        self.assertEqual(lerdyaml.parse("proxy:\n  command: 'it''s # here' # c\n").proxy.command, "it's # here")
        self.assertEqual(lerdyaml.scalar_value("sh -c 'exec x'"), "sh -c 'exec x'")

    def test_rewrites_only_the_command_line(self):
        text = "# keep me\r\nsecured: true\r\nproxy:\r\n    port: 3100\r\n    command: cargo run\r\n    ssl: false\r\n"
        out = lerdyaml.with_command(lerdyaml.parse(text), "lerd rust:dev")
        self.assertEqual(out, text.replace("command: cargo run", 'command: "lerd rust:dev"'))
        self.assertEqual(lerdyaml.parse(out).proxy.command, "lerd rust:dev")

    def test_adds_a_missing_command(self):
        out = lerdyaml.with_command(lerdyaml.parse("proxy:\n  port: 3100\n"), "lerd rust:dev")
        self.assertEqual(out, 'proxy:\n  command: "lerd rust:dev"\n  port: 3100\n')

    def test_refuses_what_it_cannot_edit_safely(self):
        for text in [
            "proxy: {command: cargo run, port: 1}\n",
            "proxy:\n  command: |\n    cargo run\n",
            "proxy:\n  command: cargo\n    run\n",
            "proxy:\n\tcommand: cargo run\n",
            "proxy:\n  command: cargo run\nproxy:\n  port: 1\n",
            "proxy:\n  command: \"unterminated\n",
            "proxy:\n  port: many\n",
            "proxy:\n  port: 1\n---\nproxy:\n  port: 2\n",
        ]:
            with self.subTest(text=text), self.assertRaises(lerdyaml.Unsupported):
                lerdyaml.parse(text)

    def test_one_leading_document_marker(self):
        self.assertEqual(lerdyaml.parse("---\nproxy:\n  port: 1\n").proxy.port, 1)

    def test_adds_a_command_under_the_top_level_proxy_only(self):
        text = "services:\n  proxy:\n    x: 1\nproxy:\n  port: 3000\n"
        out = lerdyaml.with_command(lerdyaml.parse(text), "lerd rust:dev")
        self.assertEqual(out, 'services:\n  proxy:\n    x: 1\nproxy:\n  command: "lerd rust:dev"\n  port: 3000\n')

    def test_reads_plain_values_like_yaml(self):
        # In a plain scalar, quotes are ordinary and ` #` always starts a comment.
        doc = lerdyaml.parse("proxy:\n  command: cargo run -- --name 'a #b'\n")
        self.assertEqual(doc.proxy.command, "cargo run -- --name 'a")

    def test_refuses_quoted_values_spanning_lines(self):
        for text in ['proxy:\n  note: "a\n  command: cargo run\n  x: y"\n  port: 1\n',
                     'name: "a\nproxy:\n  command: cargo run\n  port: 1"\n',
                     "name: 'a\nproxy:\n  port: 1'\n"]:
            with self.subTest(text=text), self.assertRaises(lerdyaml.Unsupported):
                lerdyaml.parse(text)

    def test_a_commented_second_document_is_refused(self):
        with self.assertRaises(lerdyaml.Unsupported):
            lerdyaml.parse("proxy:\n  port: 1\n--- # dev\nproxy:\n  port: 2\n")
        self.assertEqual(lerdyaml.parse("--- # top\nproxy:\n  port: 1\n").proxy.port, 1)

    def test_new_file_round_trips(self):
        doc = lerdyaml.parse(lerdyaml.new_file("lerd rust:dev -- --bin 'a b'", 3105))
        self.assertEqual((doc.proxy.command, doc.proxy.port), ("lerd rust:dev -- --bin 'a b'", 3105))

    def test_rustyard_marker(self):
        self.assertTrue(lerdyaml.parse("# Generated by Rustyard. Local adapter; do not commit this file.\nsecured: true\nproxy:\n  command: \"x\"\n  port: 1\n").rustyard)


class SetupPlanTest(unittest.TestCase):
    def test_translations(self):
        cases = {
            None: "lerd rust:dev",
            "cargo run": "lerd rust:dev",
            "cargo run --bin server -- --verbose": "lerd rust:dev -- --bin server -- --verbose",
            "RUST_LOG=debug cargo run -p api": "RUST_LOG=debug lerd rust:dev -- -p api",
            "env RUST_LOG=info /usr/bin/cargo run": "RUST_LOG=info lerd rust:dev",
            "cargo run --features 'a b'": "lerd rust:dev -- --features 'a b'",
        }
        for current, expected in cases.items():
            with self.subTest(current=current):
                self.assertEqual(setup.plan(current, None), expected)

    def test_explicit_arguments_win(self):
        self.assertEqual(setup.plan("cargo run --bin a", ["--bin", "b"]), "lerd rust:dev -- --bin b")
        self.assertEqual(setup.plan("lerd rust:dev -- --bin a", ["--bin", "b"]), "lerd rust:dev -- --bin b")

    def test_already_set_is_unchanged(self):
        self.assertEqual(setup.plan("lerd rust:dev -- --bin a", None), "lerd rust:dev -- --bin a")

    def test_refusals(self):
        for current in ["cargo watch -x run", "./target/debug/app", "cargo +nightly run", "cargo run 'unclosed",
                        "cargo run -- --port $PORT", "cargo run 2>&1 | tee dev.log", "cargo run; echo done",
                        "cargo run --bin `whoami`", "cargo run > out.log"]:
            with self.subTest(current=current), self.assertRaises(setup.Refused):
                setup.plan(current, None)


class SetupCommandTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = self.dir.name
        write(os.path.join(self.root, "Cargo.toml"), '[package]\nname = "x"\n')
        os.makedirs(os.path.join(self.root, "src"))
        write(os.path.join(self.root, "src", "main.rs"), 'fn main() { std::env::var("PORT"); }\n')
        self.cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.cwd)
        self.addCleanup(self.dir.cleanup)

    def run_setup(self, *argv, tty=False, answer=""):
        stdin = io.StringIO(answer)
        stdin.isatty = lambda: tty
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = setup.main(list(argv), env={}, stdin=stdin)
        return code, out.getvalue(), err.getvalue()

    def lerd_yaml(self):
        with open(os.path.join(self.root, ".lerd.yaml")) as handle:
            return handle.read()

    def test_edits_existing_file_after_yes(self):
        original = "secured: true\nproxy:\n  command: cargo run --bin web\n  port: 3200\nservices:\n  - redis\n"
        write(".lerd.yaml", original)
        os.chmod(".lerd.yaml", 0o600)
        code, out, _ = self.run_setup("--dry-run")
        self.assertEqual((code, self.lerd_yaml()), (0, original))
        self.assertIn("to:   lerd rust:dev -- --bin web", out)
        code, _, err = self.run_setup()
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        code, out, _ = self.run_setup(tty=True, answer="n\n")
        self.assertEqual((code, self.lerd_yaml()), (1, original))
        code, out, _ = self.run_setup(tty=True, answer="y\n")
        self.assertEqual(code, 0)
        self.assertEqual(self.lerd_yaml(), original.replace("command: cargo run --bin web", 'command: "lerd rust:dev -- --bin web"'))
        self.assertEqual(os.stat(".lerd.yaml").st_mode & 0o777, 0o600)
        self.assertIn("lerd link", out)
        code, out, _ = self.run_setup("--yes")
        self.assertEqual(code, 0)
        self.assertIn("Already set", out)

    def test_writes_a_new_file_with_the_given_port(self):
        code, out, _ = self.run_setup("--yes", "--port", "3333", "--", "--bin", "api")
        self.assertEqual(code, 0)
        doc = lerdyaml.parse(self.lerd_yaml())
        self.assertEqual((doc.proxy.command, doc.proxy.port), ("lerd rust:dev -- --bin api", 3333))
        self.assertNotIn("mentions PORT", out)

    def test_refuses_rustyard_php_and_non_cargo_commands(self):
        for text, expected in [
            ("# Generated by Rustyard. Local adapter; do not commit this file.\nsecured: true\nproxy:\n  command: \"'/x/rustyard' dev\"\n  port: 3100\n", "Rustyard"),
            ("php_version: \"8.4\"\nsecured: true\n", "no proxy block"),
            ("proxy:\n  command: cargo watch -x run\n  port: 1\n", "left alone"),
            ("proxy: {command: x}\n", "Nothing was changed"),
        ]:
            with self.subTest(expected=expected):
                write(".lerd.yaml", text)
                code, _, err = self.run_setup("--yes")
                self.assertEqual(code, 1)
                self.assertIn(expected, err)
                self.assertEqual(self.lerd_yaml(), text)

    def test_needs_a_rust_project(self):
        os.unlink("Cargo.toml")
        code, _, err = self.run_setup("--yes")
        self.assertEqual(code, 1)
        self.assertIn("No Cargo.toml", err)

    def test_free_port_skips_lerd_sites_and_bound_ports(self):
        with socket.socket() as busy:
            busy.bind(("0.0.0.0", 0))
            taken = busy.getsockname()[1]
            with mock.patch.object(setup, "FIRST_PORT", taken), mock.patch.object(setup, "LAST_PORT", taken + 5), \
                 mock.patch.object(setup.lerd, "sites", return_value=[{"host_port": taken + 1}]):
                self.assertEqual(setup.free_port({}), taken + 2)


class CargoTest(unittest.TestCase):
    def test_finds_cargo_outside_path(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".cargo", "bin"))
            fake = os.path.join(home, ".cargo", "bin", "cargo")
            write(fake, "#!/bin/sh\n")
            os.chmod(fake, 0o755)
            self.assertEqual(cargo.find_cargo({"PATH": "/nonexistent", "HOME": home}), fake)
            env = cargo.child_env(fake, {"PATH": "/usr/bin" + os.pathsep + os.path.dirname(fake)})
            self.assertEqual(env["PATH"], os.path.dirname(fake) + os.pathsep + "/usr/bin")
            self.assertEqual(cargo.find_cargo({"PATH": "/nonexistent", "HOME": "/x", "LERD_RUST_CARGO": fake}), fake)
            with self.assertRaises(cargo.CargoNotFound):
                cargo.find_cargo({"PATH": "/nonexistent", "HOME": "/x", "LERD_RUST_CARGO": "cargo"})
            with self.assertRaises(cargo.CargoNotFound):
                cargo.find_cargo({"PATH": "/nonexistent", "HOME": "/nonexistent"})


class RunEnvTest(unittest.TestCase):
    def test_what_cargo_run_would_set(self):
        package = {"name": "web", "version": "1.2.3-rc.1", "manifest_path": "/p/web/Cargo.toml",
                   "authors": ["A <a@x>", "B"], "description": "d", "license": None}
        env = cargo.run_env("/c/cargo", package, "/p/target/debug/web", {"LD_LIBRARY_PATH": "/usr/lib", "KEEP": "1"})
        self.assertEqual(env["CARGO"], "/c/cargo")
        self.assertEqual(env["CARGO_MANIFEST_DIR"], "/p/web")
        self.assertEqual((env["CARGO_PKG_VERSION_MAJOR"], env["CARGO_PKG_VERSION_MINOR"],
                          env["CARGO_PKG_VERSION_PATCH"], env["CARGO_PKG_VERSION_PRE"]), ("1", "2", "3", "rc.1"))
        self.assertEqual(env["CARGO_PKG_AUTHORS"], "A <a@x>:B")
        self.assertEqual(env["CARGO_PKG_LICENSE"], "")
        self.assertEqual(env["LD_LIBRARY_PATH"], os.pathsep.join(["/p/target/debug", "/p/target/debug/deps", "/usr/lib"]))
        self.assertEqual(env["KEEP"], "1")


class WatcherPartsTest(unittest.TestCase):
    def test_parse_args(self):
        options = watcher.parse_args(["--watch", "src", "--poll", "250", "--", "--bin", "a", "--", "--flag"])
        self.assertEqual((options.watch, options.poll, options.run_args), (["src"], 250, ["--bin", "a", "--", "--flag"]))
        with self.assertRaises(SystemExit):
            watcher.parse_args(["--poll", "5"])

    def test_tree_ignores_build_output_vcs_editors_and_symlinks(self):
        with tempfile.TemporaryDirectory() as root:
            for path in ["src/main.rs", "src/.main.rs.swp", "src/main.rs~", "src/#main.rs#", "src/.#main.rs",
                         "target/debug/app", "sub/target/x", ".git/HEAD", "node_modules/a.js", ".idea/x",
                         ".cargo/config.toml", ".env", ".lerd.yaml", "Cargo.toml", "4913", "skip/me.rs"]:
                os.makedirs(os.path.join(root, os.path.dirname(path)), exist_ok=True)
                write(os.path.join(root, path), "x")
            os.symlink(os.path.join(root, "src"), os.path.join(root, "linked"))
            os.symlink(os.path.join(root, "Cargo.toml"), os.path.join(root, "linked.toml"))
            found = watcher.Tree([root], [os.path.join(root, "skip")]).scan()
            names = sorted(os.path.relpath(p, root) for p in found)
            self.assertEqual(names, [".cargo/config.toml", ".env", "Cargo.toml", "src/main.rs"])
            only = watcher.Tree([os.path.join(root, "Cargo.toml")], []).scan()
            self.assertEqual(list(only), [os.path.join(root, "Cargo.toml")])

    def test_changes_and_describe(self):
        before = {"/p/a": (1, 1), "/p/b": (1, 1)}
        after = {"/p/a": (2, 1), "/p/c": (1, 1)}
        self.assertEqual(watcher.changes(before, after), ["/p/a", "/p/b", "/p/c"])
        self.assertEqual(watcher.describe(["/p/a", "/p/b", "/p/c", "/p/d"], "/p"), "a, b, c and 1 more")

    def test_build_args_name_the_binary_cargo_run_would_run(self):
        meta = {"packages": [
            {"name": "web", "manifest_path": os.path.join(os.getcwd(), "Cargo.toml"), "default_run": None,
             "targets": [{"name": "web", "kind": ["bin"]}, {"name": "web", "kind": ["lib"]}]},
            {"name": "multi", "manifest_path": "/elsewhere/Cargo.toml", "default_run": "server",
             "targets": [{"name": "server", "kind": ["bin"]}, {"name": "cli", "kind": ["bin"]}]},
            {"name": "two", "manifest_path": "/two/Cargo.toml", "default_run": None,
             "targets": [{"name": "a", "kind": ["bin"]}, {"name": "b", "kind": ["bin"]}]},
        ]}
        root = os.getcwd()
        args = lambda selection: watcher.build_args(selection, "cargo", root, {}, meta)  # noqa: E731
        self.assertEqual(args([]), ["--bin", "web"])
        self.assertEqual(args(["--release"]), ["--release", "--bin", "web"])
        self.assertEqual(args(["-p", "multi"]), ["-p", "multi", "--bin", "server"])
        self.assertEqual(args(["--package=two"]), ["--package=two"])
        self.assertEqual(args(["--bin", "x"]), ["--bin", "x"])
        self.assertEqual(args(["--manifest-path", "m"]), ["--manifest-path", "m"])
        self.assertEqual(watcher.build_args([], "cargo", root, {}, None), [])  # no metadata: leave it to cargo
        with mock.patch.object(watcher.cargo_mod, "metadata", side_effect=RuntimeError("bad manifest")):
            self.assertIsNone(watcher.read_metadata("cargo", root, {}))

    def test_probe(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            probe = watcher.Probe({"HOST": "0.0.0.0", "PORT": str(port)})
            self.assertEqual(probe.address, f"0.0.0.0:{port}")
            self.assertTrue(probe.check())
            self.assertFalse(probe.check())  # reported once
        probe = watcher.Probe({"HOST": "127.0.0.1", "PORT": str(port)})
        self.assertFalse(probe.check())
        self.assertFalse(probe.overdue())
        self.assertIsNone(probe.listening)  # unknown while it starts, never a false alarm
        probe.since -= watcher.READY_WARNING_AFTER + 1
        self.assertTrue(probe.overdue())
        probe.warned = True
        self.assertIs(probe.listening, False)
        self.assertFalse(watcher.Probe({}).enabled)

    def test_color(self):
        tty = mock.Mock(isatty=lambda: True)
        pipe = mock.Mock(isatty=lambda: False)
        self.assertTrue(watcher.wants_color({"CLICOLOR_FORCE": "1"}, pipe))
        self.assertFalse(watcher.wants_color({"CLICOLOR_FORCE": "1", "NO_COLOR": "1"}, tty))
        self.assertTrue(watcher.wants_color({}, tty))
        self.assertFalse(watcher.wants_color({}, pipe))


class RetriesTest(unittest.TestCase):
    def test_backoff_then_wait_for_a_save_and_reset_after_a_stable_run(self):
        retries = watcher.Retries()
        delays = [retries.after_exit(0.1) for _ in range(len(watcher.RETRY_DELAYS) + 1)]
        self.assertEqual(delays, [float(d) for d in watcher.RETRY_DELAYS] + [None])
        self.assertEqual(retries.after_exit(watcher.STABLE_RUN), float(watcher.RETRY_DELAYS[0]))
        retries.after_exit(0.1)
        retries.reset()
        self.assertEqual(retries.after_exit(0.1), float(watcher.RETRY_DELAYS[0]))


class StateTest(unittest.TestCase):
    def test_round_trip_and_dead_watcher(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as checkout:
            env = {"LERD_PLUGIN_DATA": data}
            state.write(checkout, {"pid": os.getpid(), "phase": "running"}, env)
            self.assertEqual(state.read(checkout, env)["phase"], "running")
            state.write(checkout, {"pid": 2 ** 22 + 12345, "phase": "building", "app": {"pid": 1},
                                   "building_since": 1.0, "serving_previous": True}, env)
            read = state.read(checkout, env)
            self.assertEqual((read["phase"], read["app"], read["building_since"], read["serving_previous"]),
                             ("stopped", None, None, False))
            self.assertIsNone(state.read(checkout, {}))
            self.assertEqual([n for n in os.listdir(os.path.join(data, "builds")) if n.startswith(".tmp")], [])


class StatusTest(unittest.TestCase):
    def test_uses_watcher(self):
        self.assertTrue(status.uses_watcher("lerd rust:dev"))
        self.assertTrue(status.uses_watcher("RUST_LOG=1 /home/u/.local/bin/lerd rust:dev -- --bin a"))
        self.assertFalse(status.uses_watcher("cargo run"))
        self.assertFalse(status.uses_watcher("echo lerd"))
        self.assertFalse(status.uses_watcher(None))

    def test_checkout_by_surface(self):
        cwd = os.getcwd()
        self.assertEqual(status.checkout({"LERD_SURFACE": "cli", "LERD_SITE": "a", "LERD_SITE_PATH": "/x"}), ("a", cwd))
        self.assertEqual(status.checkout({"LERD_SURFACE": "api", "LERD_SITE": "a", "LERD_SITE_PATH": "/x"}), ("a", "/x"))
        with self.assertRaises(status.Problem):
            status.checkout({"LERD_SURFACE": "mcp"})
        sites = [{"name": "a", "worktrees": [{"branch": "feat-x", "git_branch": "feat/x", "path": "/wt"}]}]
        with mock.patch.object(status.lerd, "sites", return_value=sites):
            env = {"LERD_SURFACE": "mcp", "LERD_SITE": "a", "LERD_SITE_PATH": "/x"}
            self.assertEqual(status.checkout(env, "feat/x"), ("a", "/wt"))
            self.assertEqual(status.checkout(env, "feat-x"), ("a", "/wt"))
            with self.assertRaises(status.Problem):
                status.checkout(env, "nope")
        # Never answer about the main checkout when the worktree was asked for.
        for sites in (None, [{"name": "other"}]):
            with self.subTest(sites=sites), mock.patch.object(status.lerd, "sites", return_value=sites), \
                    self.assertRaises(status.Problem):
                status.checkout({"LERD_SURFACE": "api", "LERD_SITE": "a", "LERD_SITE_PATH": "/x"}, "feat-x")

    def test_describe_failed_build(self):
        now = time.time()
        text = "\n".join(status.describe({
            "site": "hello", "path": "/p", "rust": True, "now": now,
            "dev": {"file": True, "watcher": True, "command": "lerd rust:dev", "port": 3100},
            "dev_server": {"phase": "failed", "serving_previous": True, "app": {"address": "h:1", "listening": True},
                           "build": {"ok": False, "errors": 1, "warnings": 0, "finished_at": now - 5, "duration": 1,
                                     "diagnostics": [{"level": "error", "file": "src/main.rs", "line": 2, "column": 3, "message": "boom"}]}},
        }))
        self.assertIn("FAILED with 1 error(s), 5s ago (still serving the previous build)", text)
        self.assertIn("error   src/main.rs:2:3  boom", text)
        self.assertIn("listening on h:1", text)

    def test_check_refuses_odd_package_names(self):
        with tempfile.TemporaryDirectory() as root:
            write(os.path.join(root, "Cargo.toml"), "")
            with self.assertRaises(status.Problem):
                status.run_check(root, {}, package="--config=x")


if __name__ == "__main__":
    unittest.main()
