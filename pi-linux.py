#!/usr/bin/env python3
"""Patch the npm Pi CLI bundle to retry assistant errors except exhausted budget pools.

Linux/macOS/WSL; Python stdlib + the installed Node.js. Settings stay unchanged.
This includes auth, other quota, and context-overflow errors, but NOT exhausted
budget pools, user cancellation, successful responses, tool-result errors,
or failures outside the assistant loop.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


MARKER = "/* retry-patch: all-assistant-errors */"
IDENT = r"[A-Za-z_$][A-Za-z0-9_$]*"
CLASSIFIER = re.compile(
    rf"\bfunction\s+isRetryableAssistantError\s*\(\s*(?P<arg>{IDENT})\s*\)"
    r"\s*\{(?P<body>[^{}]*)\}"
)
SESSION = re.compile(
    rf"(?<![\w$])_isRetryableError\s*\(\s*(?P<arg>{IDENT})\s*\)"
    r"\s*\{(?P<body>[^{}]*)\}"
)


def patch_bundle(data: bytes) -> bytes:
    """Validate both known leaf functions before changing either one."""
    # ponytail: match verified leaf functions, not a JS parser; re-adapt if upstream rewrites them.
    text = data.decode("utf-8")
    edits = []
    for pattern, name in (
        (CLASSIFIER, "isRetryableAssistantError"),
        (SESSION, "AgentSession._isRetryableError"),
    ):
        matches = list(pattern.finditer(text))
        if len(matches) != 1:
            raise ValueError(f"{name}: expected one definition, found {len(matches)}")
        match = matches[0]
        arg = match["arg"]
        escaped = re.escape(arg)
        body = re.sub(r"\s+", "", match["body"])
        if pattern is CLASSIFIER:
            legacy = f'{MARKER}return {arg}.stopReason==="error";'
            replacement = (
                f'{MARKER}return {arg}.stopReason==="error"'
                r'&&!/budget\s+pool\s+quota\s+has\s+been\s+exhausted/i.test('
                f'{arg}.errorMessage??"");'
            )
            expected = (
                rf'if\({escaped}\.stopReason!=="error"\|\|!{escaped}\.errorMessage\)return!1;'
                rf'(?:let|const|var)(?P<error>{IDENT})={escaped}\.errorMessage;'
                r'returnNON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN\.test\((?P=error)\)'
                r'\?!1:RETRYABLE_PROVIDER_ERROR_PATTERN\.test\((?P=error)\);?'
            )
        else:
            # Without this change context overflow bypasses the shared classifier.
            legacy = replacement = f"{MARKER}return isRetryableAssistantError({arg});"
            expected = (
                rf'returnisContextOverflow\({escaped},'
                rf'\(this\._modelForMessage\({escaped}\)\?\?this\.model\)'
                rf'\?\.contextWindow\?\?0\)\?!1:isRetryableAssistantError\({escaped}\);?'
            )
        if match["body"] not in (legacy, replacement) and not re.fullmatch(expected, body):
            raise ValueError(f"{name}: unsupported code shape; no files changed")
        edits.append((match.start("body"), match.end("body"), replacement))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text.encode("utf-8")


def find_bundle(package: str | None) -> Path:
    if package:
        root = Path(package).expanduser().resolve()
    else:
        executable = shutil.which("pi")
        if not executable:
            raise ValueError("pi not found in PATH; use --package /path/to/pi-coding-agent")
        root = next(
            (p for p in Path(executable).resolve().parents if (p / "package.json").is_file()),
            None,
        )
        if root is None:
            raise ValueError("Cannot locate the npm package; use --package")
    manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
    if manifest.get("name") != "@earendil-works/pi-coding-agent":
        raise ValueError("Expected the @earendil-works/pi-coding-agent npm package")
    print(f"Pi {manifest.get('version', '?')}: {root}")
    # Hashes change on upgrade. Only the bundle containing BOTH definitions counts.
    candidates = []
    for path in sorted((root / "dist" / "bundle").rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        if CLASSIFIER.search(text) and SESSION.search(text):
            candidates.append(path)
    if len(candidates) != 1:
        raise ValueError(f"Expected one CLI retry bundle, found {len(candidates)}; unsupported build")
    return candidates[0]


def atomic_write(path: Path, data: bytes) -> None:
    """Replace in the same directory, preserving the installed file's metadata."""
    stat = path.stat()
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if (stat.st_uid, stat.st_gid) != (os.getuid(), os.getgid()):
            os.chown(temporary, stat.st_uid, stat.st_gid)
        shutil.copystat(path, temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def process(path: Path, *, dry_run: bool = False, restore: bool = False) -> None:
    current = path.read_bytes()
    patched = patch_bundle(current)
    backup = path.with_name(path.name + ".orig")
    original = current
    if backup.exists():
        original = backup.read_bytes()
        if MARKER.encode() in original:
            raise ValueError(f"Backup is not pristine: {backup}")
        expected = patch_bundle(original)
        # Normalize known old/new retry bodies; all other bytes must still match.
        if patched != expected:
            raise ValueError(
                f"Backup does not match this installation: {backup}\n"
                "Pi may have been upgraded or edited. Refusing to overwrite either file; "
                "move the old backup aside after checking it."
            )
    elif restore:
        raise ValueError(f"No backup found: {backup}")
    elif MARKER.encode() in current:
        raise ValueError("Patched/partially patched bundle has no backup; refusing to back it up")

    target = original if restore else patched
    print(f"Bundle: {path}")
    print(f"Backup: {backup}")
    if target == current:
        print("Already restored." if restore else "Already patched; no changes.")
        return
    # Parse only: never run Pi, load extensions, resume a session, or call an API.
    node = shutil.which("node")
    if not node:
        raise ValueError("Node.js is required for the syntax check")
    result = subprocess.run(
        [node, "--input-type=module", "--check"],
        input=target, capture_output=True, timeout=30,
    )
    if result.returncode:
        raise ValueError("Node.js syntax check failed:\n" + result.stderr.decode("utf-8", "replace"))
    print("Verified: both retry gates; Node.js syntax OK.")
    if dry_run:
        print(f"DRY RUN: would {'restore' if restore else 'patch'}; no files or backups written.")
        return
    if not backup.exists():
        # Exclusive creation: never overwrite an existing .orig.
        with backup.open("xb") as stream:
            stream.write(current)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copystat(path, backup)
    atomic_write(path, target)
    print("Restored." if restore else "Patched: assistant errors retry except exhausted budget pools.")
    print("Restart Pi. retry.enabled / maxRetries / delays still apply; settings were not changed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Validate only; do not write or back up")
    parser.add_argument("--restore", action="store_true", help="Restore the matching .orig backup")
    parser.add_argument("--package", help="npm package directory (default: resolve pi from PATH)")
    args = parser.parse_args()
    if os.name != "posix":
        print("ERROR: Run this script inside Linux/macOS/WSL, not Windows Python.", file=sys.stderr)
        return 1
    print("WARNING: auth/other quota/context errors still retry; exhausted budget pools do not.")
    print("Context-overflow recovery waits until retries are exhausted. Close Pi before applying.")
    try:
        process(find_bundle(args.package), dry_run=args.dry_run, restore=args.restore)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
