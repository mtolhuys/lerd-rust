"""The dev server end to end: real processes, a stand-in cargo, and a real
cargo with a dependency-free app when one is installed."""

import json
import os
import shutil
import threading
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from helpers import HERE, ROOT, alive, free_port, get, read_state, stop, wait_for

DEV = os.path.join(ROOT, "bin", "dev")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)
    # Make the change visible even on file systems with coarse timestamps.
    later = time.time() + 1
    os.utime(path, (later, later))


class FakeCargoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "app")
        self.data = os.path.join(self.tmp.name, "data")
        self.logs = os.path.join(self.tmp.name, "logs")
        os.makedirs(self.logs)
        write(os.path.join(self.root, "Cargo.toml"), '[package]\nname = "app"\n')
        write(os.path.join(self.root, "src", "main.rs"), "v1")
        self.port = free_port()
        fake = os.path.join(HERE, "fake_cargo.py")
        self.env = dict(os.environ, LERD_RUST_CARGO=fake, HOST="127.0.0.1", PORT=str(self.port),
                        LERD_PLUGIN_DATA=self.data, FAKE_CARGO_LOG=self.logs, PYTHONDONTWRITEBYTECODE="1",
                        LERD_SITE="app")
        self.env.pop("LERD_SURFACE", None)
        self.output = open(os.path.join(self.tmp.name, "dev.log"), "w+")
        self.addCleanup(self.output.close)

    def start(self, *args, **env):
        process = subprocess.Popen([sys.executable, DEV, "--poll", "100", *args], cwd=self.root,
                                   env=dict(self.env, **env), stdout=self.output, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: process.poll() is None and stop(process, signal.SIGKILL))
        return process

    def log(self):
        self.output.flush()
        with open(self.output.name) as handle:
            return handle.read()

    def apps(self):
        try:
            with open(os.path.join(self.logs, "apps.log")) as handle:
                return [json.loads(line) for line in handle.read().splitlines()]
        except FileNotFoundError:
            return []

    def state(self):
        return read_state(self.data) or {}

    def builds(self):
        with open(os.path.join(self.logs, "builds.log")) as handle:
            return len(handle.read().splitlines())

    def test_rebuilds_swaps_keeps_last_good_and_cleans_up(self):
        dev = self.start()
        wait_for(lambda: get(self.port) == "v1", message="first build serving")
        wait_for(lambda: (self.state().get("app") or {}).get("listening"), message="listening recorded")
        self.assertEqual(self.state()["phase"], "running")
        first = self.apps()[0]
        first_app, first_child = first["pid"], first["child"]
        with open(os.path.join(self.logs, "builds.log")) as handle:
            self.assertIn("--bin app", handle.read())  # names the binary cargo run picks
        # The built executable runs directly, with what cargo run would set.
        self.assertEqual((first["pkg"], first["manifest_dir"], first["cwd"]),
                         ("app", os.path.realpath(self.root), os.path.realpath(self.root)))
        self.assertFalse(os.path.exists(os.path.join(self.logs, "runs.log")))

        write(os.path.join(self.root, "src", "main.rs"), "v2")
        wait_for(lambda: get(self.port) == "v2", message="rebuilt app serving")
        # The whole previous process group is gone, including the app's own child.
        wait_for(lambda: not alive(first_app) and not alive(first_child), message="old group stopped")

        write(os.path.join(self.root, "src", "main.rs"), "BROKEN")
        wait_for(lambda: self.state().get("phase") == "failed", message="failed build recorded")
        state = self.state()
        self.assertTrue(state["serving_previous"])
        self.assertEqual(state["build"]["errors"], 1)
        self.assertEqual(state["build"]["diagnostics"][0]["file"], "src/main.rs")
        self.assertEqual(get(self.port), "v2")  # the last good build keeps serving
        self.assertIn("BUILD FAILED (1 error(s)); still serving the previous build", self.log())

        # Ignored files do not trigger a build: build output, Lerd's config, and
        # what a running app writes, such as a SQLite database and its journal.
        builds = self.builds()
        for name in ["target/debug/x", ".lerd.yaml", "dev.db", "dev.db-wal", "dev.db-shm", "storage/app.log"]:
            write(os.path.join(self.root, name), "x")
        time.sleep(0.6)
        self.assertEqual(self.builds(), builds)

        write(os.path.join(self.root, "src", "main.rs"), "v3")
        wait_for(lambda: get(self.port) == "v3", message="recovered build serving")
        self.assertEqual(self.state()["phase"], "running")

        last = self.apps()[-1]
        self.assertEqual(stop(dev), 0)
        self.assertFalse(alive(last["pid"]))
        self.assertFalse(alive(last["child"]))
        self.assertEqual(self.state()["phase"], "stopped")
        self.assertIsNone(get(self.port))

    def test_first_build_failing_then_fixed(self):
        write(os.path.join(self.root, "src", "main.rs"), "BROKEN")
        dev = self.start()
        wait_for(lambda: self.state().get("phase") == "failed", message="failed first build")
        self.assertFalse(self.state()["serving_previous"])
        self.assertIn("nothing is serving the site yet", self.log())
        write(os.path.join(self.root, "src", "main.rs"), "v1")
        wait_for(lambda: get(self.port) == "v1", message="fixed build serving")
        self.assertEqual(stop(dev, signal.SIGINT), 0)

    def test_a_change_during_a_build_restarts_it(self):
        dev = self.start(FAKE_BUILD_SECONDS="1.5")
        wait_for(lambda: self.state().get("phase") == "building", message="building")
        write(os.path.join(self.root, "src", "main.rs"), "v2")
        wait_for(lambda: get(self.port) == "v2", timeout=30, message="latest source serving")
        self.assertIn("stopped the unfinished build", self.log())
        self.assertEqual([a["version"] for a in self.apps()], ["v2"])
        stop(dev)

    def test_app_exit_is_reported_and_its_children_stopped(self):
        dev = self.start()
        wait_for(lambda: get(self.port) == "v1", message="serving")
        app, child = self.apps()[0]["pid"], self.apps()[0]["child"]
        os.kill(app, signal.SIGKILL)
        wait_for(lambda: self.state().get("phase") == "exited", message="exit recorded")
        self.assertEqual(self.state()["app_exit"], -9)
        wait_for(lambda: not alive(child), message="orphaned child stopped")
        stop(dev)

    def test_no_false_alarm_while_the_app_starts(self):
        # Not answering yet is "unknown", not "ignores HOST and PORT"; the
        # warning itself needs 30s and is covered by the Probe unit test.
        dev = self.start(FAKE_NO_LISTEN="1")
        wait_for(lambda: self.state().get("phase") == "running", message="running")
        time.sleep(0.5)
        self.assertIsNone(self.state()["app"]["listening"])
        self.assertNotIn("nothing answers", self.log())
        stop(dev)

    def test_starts_the_built_program_not_newer_source(self):
        # The program that starts is the one the build made, even when the
        # source changed after it, until that change has been built too.
        # Change the source during the build without the watcher seeing it
        # (same size, old timestamp): `cargo run` would compile and serve it.
        main = os.path.join(self.root, "src", "main.rs")
        before = os.stat(main)
        dev = self.start(FAKE_BUILD_SECONDS="0.8")
        wait_for(lambda: self.state().get("phase") == "building", message="building")
        with open(main, "w") as handle:
            handle.write("v9")
        os.utime(main, ns=(before.st_atime_ns, before.st_mtime_ns))
        wait_for(lambda: get(self.port) is not None, message="serving")
        self.assertEqual(get(self.port), "v1")
        self.assertEqual([a["version"] for a in self.apps()], ["v1"])
        stop(dev)

    def test_several_programs_fall_back_to_cargo_run(self):
        dev = self.start(FAKE_BINS="2")
        wait_for(lambda: get(self.port) == "run-v1", message="cargo run serving")
        with open(os.path.join(self.logs, "builds.log")) as handle:
            self.assertNotIn("--bin", handle.read())
        stop(dev)

    def test_ignores_a_target_directory_cargo_names(self):
        dev = self.start(FAKE_TARGET_DIR=os.path.join(self.root, "out"))
        wait_for(lambda: get(self.port) == "v1", message="serving")
        builds = self.builds()
        write(os.path.join(self.root, "out", "debug", "incremental", "x"), "x")
        time.sleep(0.6)
        self.assertEqual(self.builds(), builds)
        stop(dev)

    def test_a_file_that_never_stops_changing_does_not_hold_the_loop(self):
        dev = self.start()
        wait_for(lambda: get(self.port) == "v1", message="serving")
        done = threading.Event()

        def noisy():
            n = 0
            while not done.is_set():
                n += 1
                write(os.path.join(self.root, "src", "generated.rs"), str(n))
                time.sleep(0.05)

        writer = threading.Thread(target=noisy)
        writer.start()
        try:
            builds = self.builds()
            wait_for(lambda: self.builds() > builds + 1, timeout=15, message="builds despite constant writes")
        finally:
            done.set()
            writer.join()
        stop(dev)

    def test_stops_when_its_parent_goes(self):
        launcher = subprocess.Popen(
            [sys.executable, "-c", "import subprocess,sys,time; p=subprocess.Popen(sys.argv[1:]); "
             "print(p.pid, flush=True); time.sleep(60)", sys.executable, DEV, "--poll", "100"],
            cwd=self.root, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.addCleanup(launcher.stdout.close)
        self.addCleanup(lambda: launcher.poll() is None and launcher.kill())
        dev_pid = int(launcher.stdout.readline())
        wait_for(lambda: get(self.port) == "v1", message="serving")
        app, child = self.apps()[0]["pid"], self.apps()[0]["child"]
        launcher.kill()
        launcher.wait()
        wait_for(lambda: not alive(dev_pid) and not alive(app) and not alive(child),
                 message="watcher and app gone after the parent died")

    def test_refuses_without_cargo_toml_or_cargo(self):
        os.unlink(os.path.join(self.root, "Cargo.toml"))
        result = subprocess.run([sys.executable, DEV], cwd=self.root, env=self.env, capture_output=True, text=True,
                                timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("no Cargo.toml", result.stderr)
        write(os.path.join(self.root, "Cargo.toml"), "")
        # Hide every place cargo could be found, CARGO_HOME included (CI sets it).
        env = dict(self.env, LERD_RUST_CARGO="", PATH="/nonexistent", HOME=self.tmp.name)
        env.pop("CARGO_HOME", None)
        result = subprocess.run([sys.executable, DEV], cwd=self.root, env=env, capture_output=True, text=True,
                                timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("cargo was not found", result.stderr)
        self.assertEqual(result.stdout, "")


SERVER = textwrap.dedent("""\
    use std::io::{Read, Write};
    use std::net::TcpListener;

    fn main() {
        let host = std::env::var("HOST").unwrap_or_else(|_| "127.0.0.1".into());
        let port = std::env::var("PORT").expect("PORT");
        let listener = TcpListener::bind(format!("{host}:{port}")).unwrap();
        for stream in listener.incoming() {
            let mut stream = stream.unwrap();
            let mut buffer = [0; 1024];
            let _ = stream.read(&mut buffer);
            let body = format!("VERSION:{}", std::env::var("CARGO_PKG_NAME").unwrap_or_default());
            let _ = write!(stream, "HTTP/1.1 200 OK\\r\\nContent-Length: {}\\r\\nConnection: close\\r\\n\\r\\n{}", body.len(), body);
        }
    }
""")


@unittest.skipUnless(shutil.which("cargo"), "cargo is not installed")
class RealCargoTest(unittest.TestCase):
    def test_real_build_failure_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "realapp")
            data = os.path.join(tmp, "data")
            write(os.path.join(root, "Cargo.toml"),
                  '[package]\nname = "realapp"\nversion = "0.1.0"\nedition = "2021"\ndefault-run = "realapp"\n\n'
                  '[[bin]]\nname = "realapp"\npath = "src/main.rs"\n\n[[bin]]\nname = "other"\npath = "src/other.rs"\n')
            write(os.path.join(root, "src", "main.rs"), SERVER.replace("VERSION", "one"))
            # A second, broken binary: cargo run never builds it, so neither may we.
            write(os.path.join(root, "src", "other.rs"), "fn main() { broken }\n")
            port = free_port()
            env = dict(os.environ, HOST="127.0.0.1", PORT=str(port), LERD_PLUGIN_DATA=data,
                       CARGO_TARGET_DIR=os.path.join(tmp, "target"), PYTHONDONTWRITEBYTECODE="1")
            env.pop("LERD_SURFACE", None)
            with open(os.path.join(tmp, "dev.log"), "w+") as output:
                dev = subprocess.Popen([sys.executable, DEV, "--poll", "100"], cwd=root, env=env,
                                       stdout=output, stderr=subprocess.STDOUT)
                try:
                    wait_for(lambda: get(port) == "one:realapp", timeout=180, message="real build serving")
                    write(os.path.join(root, "src", "main.rs"), SERVER.replace("VERSION", "two").replace("let port", "let port: u32 = missing;\n    let port"))
                    wait_for(lambda: (read_state(data) or {}).get("phase") == "failed", timeout=180, message="real failure")
                    build = read_state(data)["build"]
                    self.assertGreaterEqual(build["errors"], 1)
                    error = build["diagnostics"][0]
                    self.assertEqual((error["file"], error["code"]), ("src/main.rs", "E0425"))
                    self.assertIn("missing", error["rendered"])
                    self.assertEqual(get(port), "one:realapp")
                    write(os.path.join(root, "src", "main.rs"), SERVER.replace("VERSION", "three"))
                    wait_for(lambda: get(port) == "three:realapp", timeout=180, message="real recovery")
                    self.assertEqual(stop(dev), 0)
                    self.assertIsNone(get(port))
                finally:
                    if dev.poll() is None:
                        dev.kill()
                    output.seek(0)
                    log = output.read()
            self.assertIn("listening on 127.0.0.1", log)
            self.assertNotIn("other.rs", log)


if __name__ == "__main__":
    unittest.main()
