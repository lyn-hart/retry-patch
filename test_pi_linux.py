"""Offline regression check: python3 test_pi_linux.py (requires Node.js)."""

import contextlib
import io
import json
import re
import runpy
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch as mock_patch


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


def check_managed_discovery():
    with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
        root = Path(directory)
        agent = root / "agent"
        package = agent / "install/releases/1.2.0/node_modules/@earendil-works/pi-coding-agent"
        chunks = package / "dist/bundle/chunks"
        chunks.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({
            "name": "@earendil-works/pi-coding-agent", "version": "1.2.0",
        }))
        bundle = chunks / "chunk-test.js"
        bundle.write_bytes(SOURCE)
        launcher = agent / "bin/pi"
        launcher.parent.mkdir()
        launcher.write_text("#!/bin/sh\nexit 99\n")  # Discovery must not execute Pi.
        launcher.chmod(0o755)
        entry = root / "local/bin/pi"
        entry.parent.mkdir(parents=True)
        entry.symlink_to("../../agent/bin/pi")
        version_file = agent / "install/current-version"
        version_file.write_text("1.2.0\n")
        find = patcher["find_bundles"]
        with mock_patch.object(patcher["shutil"], "which", return_value=str(entry)):
            assert find(None) == [bundle]
            for invalid in ("", ".", "..", "../../outside", "1.2.0\nother"):
                version_file.write_text(invalid)
                rejects(lambda: find(None))
            assert find(str(package)) == [bundle]  # Explicit paths bypass the launcher.
            version_file.unlink()
            rejects(lambda: find(None))
            # Traditional npm symlinks still resolve without a managed version file.
            entry.unlink()
            entry.symlink_to(bundle)
            assert find(None) == [bundle]
        with mock_patch.object(patcher["shutil"], "which", return_value=None):
            rejects(lambda: find(None))
        (package / "package.json").write_text('{"name":"unrelated"}')
        rejects(lambda: find(str(package)))


def check_split_bundles():
    classifier, session = SOURCE.split(b"class AgentSession", 1)
    with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
        root = Path(directory)
        (root / "package.json").write_text(json.dumps({
            "name": "@earendil-works/pi-coding-agent", "version": "1.2.0", "type": "module",
        }))
        chunks = root / "dist/bundle/chunks"
        chunks.mkdir(parents=True)
        originals = {
            chunks / "chunk-classifier.js": classifier
            + b"export {isRetryableAssistantError,isContextOverflow};",
            chunks / "chunk-session.js": b'import {isRetryableAssistantError,isContextOverflow}'
            b'from "./chunk-classifier.js";export class AgentSession' + session,
        }
        for path, data in originals.items():
            path.write_bytes(data)
        paths = patcher["find_bundles"](str(root))
        assert paths == list(originals)
        backups = {path: path.with_name(path.name + ".orig") for path in paths}
        before = {path: path.stat() for path in paths}
        process(paths, dry_run=True)
        for path in paths:
            assert path.read_bytes() == originals[path] and not backups[path].exists()
            assert path.stat().st_mtime_ns == before[path].st_mtime_ns

        # A failure in the later chunk must not patch or back up the earlier chunk.
        second = paths[1]
        for bad in (
            originals[second].replace(b"this._modelForMessage(message)", b"this._modelForMessage(other)"),
            originals[second] + b"\nconst = ;",
            originals[second].replace(b"_isRetryableError", b"_unsupportedRetryError"),
            originals[second] + classifier,
        ):
            second.write_bytes(bad)
            rejects(lambda: process(patcher["find_bundles"](str(root))))
            assert paths[0].read_bytes() == originals[paths[0]]
            assert second.read_bytes() == bad
            assert all(not backup.exists() for backup in backups.values())
        second.write_bytes(originals[second])
        backups[second].write_bytes(originals[second] + b"\n// different installation")
        rejects(lambda: process(paths))
        assert all(path.read_bytes() == originals[path] for path in paths)
        assert not backups[paths[0]].exists()
        backups[second].unlink()

        process(paths)
        patched = {path: path.read_bytes() for path in paths}
        for path in paths:
            gates = tuple((pattern, name) for pattern, name in patcher["GATES"]
                          if pattern.search(originals[path].decode()))
            assert patched[path] == patch(originals[path], gates=gates) != originals[path]
            assert backups[path].read_bytes() == originals[path]
            assert path.stat().st_mode == before[path].st_mode
        # Import only the tiny patched fixtures, never the installed Pi runtime.
        check = (
            f'import {{isRetryableAssistantError}} from {json.dumps(paths[0].as_uri())};\n'
            f'import {{AgentSession}} from {json.dumps(paths[1].as_uri())};\n'
            'import assert from "node:assert/strict";\n'
            'const session = new AgentSession();\n'
            'for (const [stopReason,errorMessage,expected] of [\n'
            '  ["error","401 Unauthorized",true], ["error","context overflow",true],\n'
            '  ["error","Budget pool quota has been exhausted",false],\n'
            '  ["aborted","503",false], ["stop","",false]\n'
            ']) {\n'
            '  const message = {stopReason,errorMessage};\n'
            '  assert.equal(isRetryableAssistantError(message), expected);\n'
            '  assert.equal(session._isRetryableError(message), expected);\n'
            '}\n'
        )
        subprocess.run(["node", "--input-type=module"], input=check.encode(),
                       check=True, capture_output=True)
        backup_times = {path: backup.stat().st_mtime_ns for path, backup in backups.items()}
        process(paths)
        process(paths, restore=True, dry_run=True)
        assert all(path.read_bytes() == patched[path] for path in paths)
        assert all(backup.stat().st_mtime_ns == backup_times[path]
                   for path, backup in backups.items())
        backups[second].unlink()
        for restore in (False, True):
            rejects(lambda: process(paths, restore=restore))
            assert all(path.read_bytes() == patched[path] for path in paths)
        backups[second].write_bytes(originals[second])
        process(paths, restore=True)
        assert all(path.read_bytes() == originals[path] for path in paths)
        process(paths)
        assert all(path.read_bytes() == patched[path] for path in paths)


def main():
    check_managed_discovery()
    check_split_bundles()
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
        assert patcher["find_bundles"](str(root)) == [bundle]
        before = bundle.stat()
        process([bundle], dry_run=True)
        assert bundle.read_bytes() == SOURCE and not backup.exists()
        assert bundle.stat().st_mtime_ns == before.st_mtime_ns
        rejects(lambda: process([bundle], restore=True))
        process([bundle])
        assert bundle.read_bytes() == patched and backup.read_bytes() == SOURCE
        assert bundle.stat().st_mode == before.st_mode
        backup_before = backup.stat()
        process([bundle])  # Idempotent; never back up patched bytes.
        assert backup.read_bytes() == SOURCE
        assert backup.stat().st_mtime_ns == backup_before.st_mtime_ns
        process([bundle], restore=True, dry_run=True)
        assert bundle.read_bytes() == patched
        process([bundle], restore=True)
        assert bundle.read_bytes() == SOURCE
        process([bundle], restore=True)
        process([bundle])  # Reapply using the same pristine backup.
        bundle.write_bytes(legacy)
        process([bundle], dry_run=True)
        assert bundle.read_bytes() == legacy
        process([bundle])
        assert bundle.read_bytes() == patched and backup.read_bytes() == SOURCE
        assert backup.stat().st_mtime_ns == backup_before.st_mtime_ns
        bundle.write_bytes(legacy)
        process([bundle], restore=True)
        assert bundle.read_bytes() == SOURCE
        for data in (SOURCE, legacy, patched):
            bundle.write_bytes(data + b"\n// newer installation")
            for restore in (False, True):
                rejects(lambda: process([bundle], restore=restore))
            assert bundle.read_bytes() == data + b"\n// newer installation"
            assert backup.read_bytes() == SOURCE
        backup.unlink()
        bundle.write_bytes(patched)
        rejects(lambda: process([bundle]))
        assert not backup.exists()
        # Partial/malformed code must not create a backup or change the file.
        partial = patcher["CLASSIFIER"].sub(
            lambda _: patcher["CLASSIFIER"].search(patched.decode())[0], SOURCE.decode()
        ).encode()
        bundle.write_bytes(partial)
        rejects(lambda: process([bundle]))
        assert bundle.read_bytes() == partial and not backup.exists()
        bundle.write_bytes(SOURCE + b"\nconst = ;")
        rejects(lambda: process([bundle]))
        assert bundle.read_bytes() == SOURCE + b"\nconst = ;" and not backup.exists()
        # A copied/duplicate bundle is ambiguous; never guess which one Pi loads.
        (chunks / "duplicate.js").write_bytes(SOURCE)
        rejects(lambda: process(patcher["find_bundles"](str(root))))
    print("OK: both retry gates, budget-pool exclusion, other errors, cancellation, syntax, "
          "managed/split-chunk discovery, dry-run, backup/restore, legacy migration, idempotence, "
          "and upgrade/ambiguity/preflight guards")


if __name__ == "__main__":
    main()
