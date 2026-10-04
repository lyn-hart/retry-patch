"""Offline regression check: python3 test_pi_linux.py (requires Node.js)."""

import contextlib
import io
import json
import re
import runpy
import subprocess
import tempfile
from pathlib import Path


patcher = runpy.run_path(str(Path(__file__).with_name("pi-linux.py")))
patch = patcher["patch_bundle"]
process = patcher["process"]
SOURCE = (
    b'const NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN=/quota|billing/;'
    b'const RETRYABLE_PROVIDER_ERROR_PATTERN=/503/;'
    b'function isContextOverflow(m){return m.errorMessage==="context overflow"}'
    b'function isRetryableAssistantError(message){'
    b'if(message.stopReason!=="error"||!message.errorMessage)return!1;'
    b'let errorMessage4=message.errorMessage;'
    b'return NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN.test(errorMessage4)?!1:'
    b'RETRYABLE_PROVIDER_ERROR_PATTERN.test(errorMessage4)}'
    b'class AgentSession{_modelForMessage(){return null}'
    b'_isRetryableError(message){return isContextOverflow(message,'
    b'(this._modelForMessage(message)??this.model)?.contextWindow??0)?!1:'
    b'isRetryableAssistantError(message)}}'
)


def rejects(call):
    try:
        call()
    except ValueError:
        return
    raise AssertionError("Expected a safe rejection")


def main():
    patched = patch(SOURCE)
    assert patched != SOURCE
    assert patch(patched) == patched
    # Only the two function bodies may change.
    mask = lambda data: patcher["SESSION"].sub(
        "<session>", patcher["CLASSIFIER"].sub("<classifier>", data.decode())
    )
    assert mask(SOURCE) == mask(patched)
    renamed = re.sub(rb"\bmessage\b", b"$m", SOURCE).replace(b"errorMessage4", b"$e")
    assert b'return $m.stopReason==="error"&&' in patch(renamed)
    legacy = patcher["CLASSIFIER"].sub(
        'function isRetryableAssistantError(message){'
        + patcher["MARKER"] + 'return message.stopReason==="error";}',
        patched.decode(),
    ).encode()
    assert legacy != patched and patch(legacy) == patched
    for bad in (
        SOURCE + SOURCE, b"unrelated",
        SOURCE.replace(b"stopReason", b"differentField"),
        SOURCE.replace(b"this._modelForMessage(message)", b"this._modelForMessage(other)"),
        SOURCE.replace(b"PATTERN.test(errorMessage4)", b"PATTERN.test(other)", 1),
    ):
        rejects(lambda: patch(bad))

    # Evaluate the patched functions with Node, not just their text. No provider calls.
    check = rb"""
const assert = require("node:assert/strict");
const session = new AgentSession();
for (const errorMessage of [
  "This model is currently unavailable. Please try again later.",
  "401 Unauthorized", "403 Forbidden", "402 Payment Required", "quota exceeded", "billing",
  "context overflow", "503 Service Unavailable", "", undefined
]) {
  const m = {stopReason: "error", errorMessage};
  assert.equal(isRetryableAssistantError(m), true);
  assert.equal(session._isRetryableError(m), true);
}
for (const errorMessage of [
  'Error: ps.air-outer.com API error (402): {"message":"Budget pool quota has been exhausted. Please ask an administrator to increase the limit or\n select another budget pool.","type":"bad_response_status_code","param":"","code":"bad_response_status_code"}',
  "Budget\npool quota has been exhausted.",
  "BUDGET POOL QUOTA HAS BEEN EXHAUSTED.",
]) {
  const m = {stopReason: "error", errorMessage};
  assert.equal(isRetryableAssistantError(m), false);
  assert.equal(session._isRetryableError(m), false);
}
for (const stopReason of ["aborted", "stop", "toolUse", "length", "pending", undefined]) {
  const m = {stopReason, errorMessage: "503"};
  assert.equal(isRetryableAssistantError(m), false);
  assert.equal(session._isRetryableError(m), false);
}
"""
    subprocess.run(["node"], input=patched + check, check=True, capture_output=True)

    with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
        root = Path(directory)
        (root / "package.json").write_text(json.dumps({
            "name": "@earendil-works/pi-coding-agent", "version": "test",
        }))
        chunks = root / "dist" / "bundle" / "chunks"
        chunks.mkdir(parents=True)
        bundle = chunks / "chunk-arbitrary-hash.js"
        backup = bundle.with_name(bundle.name + ".orig")
        bundle.write_bytes(SOURCE)
        bundle.chmod(0o640)
        (chunks / "unrelated.js").write_text("const untouched = true;")
        assert patcher["find_bundle"](str(root)) == bundle
        before = bundle.stat()
        process(bundle, dry_run=True)
        assert bundle.read_bytes() == SOURCE and not backup.exists()
        assert bundle.stat().st_mtime_ns == before.st_mtime_ns
        rejects(lambda: process(bundle, restore=True))
        process(bundle)
        assert bundle.read_bytes() == patched and backup.read_bytes() == SOURCE
        assert bundle.stat().st_mode == before.st_mode
        backup_before = backup.stat()
        process(bundle)  # Idempotent; never back up patched bytes.
        assert backup.read_bytes() == SOURCE
        assert backup.stat().st_mtime_ns == backup_before.st_mtime_ns
        process(bundle, restore=True, dry_run=True)
        assert bundle.read_bytes() == patched
        process(bundle, restore=True)
        assert bundle.read_bytes() == SOURCE
        process(bundle, restore=True)
        process(bundle)  # Reapply using the same pristine backup.
        bundle.write_bytes(legacy)
        process(bundle, dry_run=True)
        assert bundle.read_bytes() == legacy
        process(bundle)
        assert bundle.read_bytes() == patched and backup.read_bytes() == SOURCE
        assert backup.stat().st_mtime_ns == backup_before.st_mtime_ns
        bundle.write_bytes(legacy)
        process(bundle, restore=True)
        assert bundle.read_bytes() == SOURCE
        for data in (SOURCE, legacy, patched):
            bundle.write_bytes(data + b"\n// newer installation")
            for restore in (False, True):
                rejects(lambda: process(bundle, restore=restore))
            assert bundle.read_bytes() == data + b"\n// newer installation"
            assert backup.read_bytes() == SOURCE
        backup.unlink()
        bundle.write_bytes(patched)
        rejects(lambda: process(bundle))
        assert not backup.exists()
        # Partial/malformed code must not create a backup or change the file.
        partial = patcher["CLASSIFIER"].sub(
            lambda _: patcher["CLASSIFIER"].search(patched.decode())[0], SOURCE.decode()
        ).encode()
        bundle.write_bytes(partial)
        rejects(lambda: process(bundle))
        assert bundle.read_bytes() == partial and not backup.exists()
        bundle.write_bytes(SOURCE + b"\nconst = ;")
        rejects(lambda: process(bundle))
        assert bundle.read_bytes() == SOURCE + b"\nconst = ;" and not backup.exists()
        # A copied/duplicate bundle is ambiguous; never guess which one Pi loads.
        (chunks / "duplicate.js").write_bytes(SOURCE)
        rejects(lambda: patcher["find_bundle"](str(root)))
    print("OK: both retry gates, budget-pool exclusion, other errors, cancellation, syntax, "
          "discovery, dry-run, backup/restore, legacy migration, idempotence, and upgrade/ambiguity guards")


if __name__ == "__main__":
    main()
