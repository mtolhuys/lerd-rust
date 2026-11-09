import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.dont_write_bytecode = True


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port, timeout=1.0):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/", timeout=timeout) as response:
            return response.read().decode()
    except OSError:
        return None


def wait_for(predicate, timeout=20.0, interval=0.05, message="condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}; last value {last!r}")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); read its state.
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().split(") ", 1)[1][0] != "Z"
    except OSError:
        return False


def read_state(data_dir):
    folder = os.path.join(data_dir, "builds")
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".json") and not n.startswith(".")]
    except FileNotFoundError:
        return None
    if not names:
        return None
    with open(os.path.join(folder, names[0])) as handle:
        return json.load(handle)


def stop(process, sig=signal.SIGTERM, timeout=20):
    if process.poll() is None:
        process.send_signal(sig)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        raise
