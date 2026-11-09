"""The program the stand-in cargo "builds": a tiny HTTP server that answers
with the version it was built from, and starts a child so the tests can see
that the whole process group is stopped."""

import http.server
import json
import os
import subprocess
import sys
import time

version = sys.argv[1]
log_dir = os.environ["FAKE_CARGO_LOG"]
child = subprocess.Popen(["sleep", "300"])
with open(os.path.join(log_dir, "apps.log"), "a") as out:
    out.write(json.dumps({
        "pid": os.getpid(), "child": child.pid, "version": version, "argv": sys.argv[2:],
        "pkg": os.environ.get("CARGO_PKG_NAME"), "manifest_dir": os.environ.get("CARGO_MANIFEST_DIR"),
        "cwd": os.getcwd(),
    }) + "\n")
if os.environ.get("FAKE_NO_LISTEN"):
    time.sleep(300)
    sys.exit(0)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = version.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


http.server.HTTPServer((os.environ.get("HOST", "127.0.0.1"), int(os.environ["PORT"])), Handler).serve_forever()
