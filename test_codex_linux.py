"""End-to-end check: python3 test_codex_linux.py

Byte-level site 6/7 finders live in `codex-linux.py --self-test`. This file
runs a *copy* of the native binary against a local mock that always answers
429 with no Retry-After.

  * site 6 off -> 1 request; on -> 2 (request_max_retries = 1)
  * site 7 raised, 1ms interval, request_max_retries = 150 -> 151 requests
    (the stock cap of 100 would stop at 101)

Skipped when no binary is installed, when the copy has no unique site-6
literal / site-7 clamp, or when CODEX_RETRY_SKIP_E2E=1. Never touches the
installed binary.
"""

import http.server
import json
import os
import runpy
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path


patcher = runpy.run_path(str(Path(__file__).with_name("codex-linux.py")))
find_429 = patcher["find_retry_429_sites"]
find_caps = patcher["find_request_cap_sites"]
make_cap_patch = patcher["make_request_cap_patch"]
REQUEST_MAX_RETRIES = patcher["REQUEST_MAX_RETRIES"]


class Mock429(http.server.BaseHTTPRequestHandler):
    """Counts `/responses` POSTs and always answers 429 without Retry-After."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        if self.path.endswith("/responses"):
            self.server.responses += 1
        body = json.dumps({"error": {
            "message": "mock rate limit", "type": "rate_limit_error",
            "code": "rate_limit_exceeded",
        }}).encode()
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("x-request-id", f"mockreq{self.server.responses}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_codex(binary, codex_home, request_max_retries):
    """Return (returncode, stderr, responses) after one `codex exec`."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Mock429)
    server.responses = 0
    port = server.server_address[1]
    (codex_home / "config.toml").write_text(
        "model = \"gpt-5.6-luna\"\n"
        "model_provider = \"mock\"\n"
        "approval_policy = \"never\"\n"
        "sandbox_mode = \"danger-full-access\"\n"
        "check_for_update_on_startup = false\n\n"
        "[model_providers.mock]\n"
        "name = \"mock\"\n"
        f"base_url = \"http://127.0.0.1:{port}/v1\"\n"
        "wire_api = \"responses\"\n"
        "experimental_bearer_token = \"mock\"\n"
        f"request_max_retries = {request_max_retries}\n"
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        proc = subprocess.run(
            [str(binary), "exec", "--skip-git-repo-check", "say hi"],
            env=dict(os.environ, CODEX_HOME=str(codex_home)),
            cwd=str(codex_home), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=300,
        )
    finally:
        server.shutdown()
    return proc.returncode, proc.stderr, server.responses


def check_e2e():
    if os.environ.get("CODEX_RETRY_SKIP_E2E") == "1":
        print("skip e2e: CODEX_RETRY_SKIP_E2E=1")
        return False
    try:
        source = patcher["find_binary"](None)
    except SystemExit:
        print("skip e2e: no native codex binary found")
        return False

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        binary = tmp / "codex"
        shutil.copy2(source, binary)
        codex_home = tmp / "home"
        codex_home.mkdir()

        data = bytearray(binary.read_bytes())
        sites = find_429(bytes(data))
        if len(sites) != 1:
            print(f"skip e2e: expected 1 site-6 literal, found {len(sites)}")
            return False
        lit = sites[0]

        # Force `retry_429` off, whatever state the source binary is in.
        data[lit["imm_off"]] = 0x00
        binary.write_bytes(data)
        rc, err, count = run_codex(binary, codex_home, 1)
        assert count == 1, (rc, err, count)
        assert "exceeded retry limit, last status: 429" in err, err

        # Site 6 on: the same binary retries once (request_max_retries = 1).
        data[lit["imm_off"]] = 0x01
        binary.write_bytes(data)
        rc, err, count = run_codex(binary, codex_home, 1)
        assert count == 2, (rc, err, count)
        assert "exceeded retry limit, last status: 429" in err, err

        # Site 7 on, with a 1ms interval so a large budget finishes fast: all
        # 150 retries must run. Clamped at the stock 100 this would stop at 101.
        # The source binary may already carry site 7, so only raise it if needed.
        data = bytearray(binary.read_bytes())
        edits, _ = patcher["plan"](data, 1)          # jitter sites -> 1ms
        for off, chunk in edits:
            data[off : off + len(chunk)] = chunk
        caps = find_caps(bytes(data), find_429(bytes(data)))
        if len(caps) != 1:
            print(f"skip e2e: expected 1 site-7 clamp, found {len(caps)}")
            return False
        s = caps[0]
        if not s["patched"]:
            data[s["off"] : s["off"] + s["len"]] = make_cap_patch(
                s["acc"], s["val"], s["len"], REQUEST_MAX_RETRIES)
        binary.write_bytes(data)
        caps = find_caps(bytes(data), find_429(bytes(data)))
        assert caps[0]["patched"] and caps[0]["cap"] == REQUEST_MAX_RETRIES, caps
        rc, err, count = run_codex(binary, codex_home, 150)
        assert count == 151, (rc, err, count)
        return True


def main():
    if check_e2e():
        print("OK: e2e 429 retry + raised request cap")


if __name__ == "__main__":
    main()
