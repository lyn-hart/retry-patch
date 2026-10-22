#!/usr/bin/env python3
"""
claude-linux.py - Patch Claude Code binary (Linux/macOS) to:
  1. Remove the retry cap so CLAUDE_CODE_MAX_RETRIES=9999 works
  2. Replace exponential backoff with fixed 1s interval
  3. Patch the Anthropic SDK's built-in retry backoff
  4. Lower rate-limit fallback delays
  5. Ignore Retry-After / ratelimit-reset so 429s also wait ~1s
  6. Pin the API retry loop's delay to 1000ms at the assignment

Targets the code shapes in Claude Code 2.1.306; older versions are not
supported. Patches the minified JS embedded in Linux/macOS binaries.
For Windows use claude-windows.py (maintained separately).

Usage:
  sudo python3 claude-linux.py [--dry-run] [--restore] [--binary PATH]

Options:
  --dry-run   Show what would be changed without modifying the binary.
              Also works on an already-patched binary, to report its state.
  --restore   Restore the original binary from backup
  --binary    Patch this file instead of the auto-discovered one

Environment variables (after patching):
  CLAUDE_CODE_MAX_RETRIES=9999      - Max retry attempts (internal cap disabled)
  CLAUDE_CODE_RETRY_WATCHDOG=1      - Persistent 429/overloaded retry (lifts the 15 cap)
  BUN_JSC_forceDebuggerBytecodeGeneration=1 - Recompile patched source in Bun

Every patch site is located by CODE SHAPE, never by a minified identifier or a
magic number. Bundler-generated names change on every release, so the API
backoff detector binds the wrapper's parameters to its shared-helper call and
adjacent constants. It reads names and numbers from that complete shape.
Upstream structural changes require updating the detector.

Every detector is also required to be UNIQUE: if a shape matches zero or more
than one place, the patch is skipped with a diagnostic instead of guessing.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys

PROC_NAME = "claude"
STOP_HINT = "Stop it first."

# Every retry delay this script writes, in milliseconds.
TARGET_DELAY_MS = 1000
# Retry-After values below this are slept verbatim; raising it means a header
# we failed to neutralize still costs seconds rather than the 10min fallback.
RATE_LIMIT_THRESHOLD_MS = 99999

_IDENT = rb'[a-zA-Z_$][a-zA-Z0-9_$]*'
# A JS numeric literal, including the exponential forms this script writes back
# (`1e3`), so a re-read of an already-patched site parses instead of missing.
_NUMBER = rb'[0-9][0-9.eE+]*'
# "not preceded by an identifier character" -- keeps `VAR=` from matching the
# tail of a longer name such as `myVAR=`.
_NOT_IDENT = rb'(?<![A-Za-z0-9_$])'


def require_platform() -> None:
    """Refuse to run outside Linux/macOS.

    This build hardcodes Unix binary discovery (`which`, POSIX npm paths) and
    sets the exec bit on write; running it on Windows would mis-locate the
    binary and produce a non-executable file. Direct Windows users to the
    dedicated script instead of failing later with a confusing error.
    """
    if os.name != "posix":
        print(f"ERROR: this is the Linux/macOS build, but the current OS is "
              f"'{sys.platform}' (os.name={os.name!r}).", file=sys.stderr)
        print("Use claude-windows.py on Windows.", file=sys.stderr)
        sys.exit(1)


def require_unix_image(data: bytes, binary_path: str) -> None:
    """Reject a Windows PE image, identified by magic bytes rather than name.

    Upstream names the launcher `claude.exe` on every platform, so the suffix
    says nothing about the format -- only the header does. The byte patches
    themselves are platform-independent minified JS, but this build sets the
    Unix exec bit on write and prints Unix hints, so send PE users next door.
    """
    if data[:2] == b"MZ":
        print(f"ERROR: {binary_path} is a Windows PE image.", file=sys.stderr)
        print("Use claude-windows.py for it.", file=sys.stderr)
        sys.exit(1)
    if data[:4] != b"\x7fELF" and data[:4] not in (
        b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",   # Mach-O 64/32 LE
        b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",   # Mach-O universal
    ):
        print(f"  WARN: {binary_path} is neither ELF nor Mach-O "
              f"(magic {data[:4].hex()}); continuing anyway.", file=sys.stderr)


def find_binary() -> str:
    """Find the Claude Code binary path (Linux/macOS)."""

    def resolve_launcher(path: str) -> str:
        """Follow a small shell launcher such as ~/.local/bin/claude."""
        path = os.path.realpath(path)
        try:
            with open(path, "rb") as f:
                head = f.read(4096)
        except OSError:
            return path
        if not head.startswith(b"#!"):
            return path
        match = re.search(rb'exec\s+["\']([^"\']+)["\']', head)
        if not match:
            return path
        target = os.path.expandvars(os.path.expanduser(match.group(1).decode("utf-8", "replace")))
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(path), target)
        target = os.path.realpath(target)
        return target if os.path.isfile(target) else path

    candidates = []

    # Try `which claude` first
    try:
        result = subprocess.run(
            ["which", "claude"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            path = resolve_launcher(result.stdout.strip())
            if os.path.isfile(path):
                return path
    except Exception:
        pass

    # Fallback: look in npm global packages
    try:
        result = subprocess.run(
            ["npm", "root", "-g"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            npm_root = result.stdout.strip()
            candidates += [
                os.path.join(npm_root, "@anthropic-ai/claude-code-linux-x64/claude"),
                os.path.join(npm_root, "@anthropic-ai/claude-code-linux-arm64/claude"),
                os.path.join(npm_root, "@anthropic-ai/claude-code-darwin-arm64/claude"),
                os.path.join(npm_root, "@anthropic-ai/claude-code-darwin-x64/claude"),
            ]
    except Exception:
        pass

    for c in candidates:
        if os.path.isfile(c):
            return os.path.realpath(c)

    print("ERROR: Could not find Claude Code binary. Is it installed?", file=sys.stderr)
    print("Try: npm install -g @anthropic-ai/claude-code", file=sys.stderr)
    sys.exit(1)


# --- Same-length rewriting helpers -------------------------------------------
# Every patch must keep the file size identical, so a replacement is always the
# same byte length as what it overwrites. Numbers get an exponential form or
# trailing spaces; JS accepts whitespace anywhere an operator or comma follows.

def fit_literal(candidates: list[str], width: int) -> bytes | None:
    """First candidate that fits `width`, right-padded with spaces.

    Do NOT pad numbers with leading zeros: `0010000` is a legacy octal literal
    in sloppy-mode JS (= 4096, not 10000) and a SyntaxError under strict/ESM.
    """
    for c in candidates:
        if len(c) <= width:
            return (c + " " * (width - len(c))).encode()
    return None


def js_number(value: int, width: int) -> bytes | None:
    """Render `value` as a JS numeric literal occupying exactly `width` bytes.

    Prefers plain decimal; falls back to the shortest exponential form when the
    slot is too narrow (500 -> 1e3 keeps a 3-byte slot).
    """
    s = str(value)
    trailing_zeros = len(s) - len(s.rstrip("0"))
    exponential = sorted(
        (f"{s[:len(s) - k]}e{k}" for k in range(1, trailing_zeros + 1)),
        key=len,
    )
    return fit_literal([s] + exponential, width)


def parse_number(raw: bytes) -> float | None:
    try:
        return float(raw)
    except ValueError:
        return None


def find_unique(data: bytes, pattern: bytes, what: str, warn_on_zero: bool = True):
    """Return the single match of `pattern`, or None (with a diagnostic).

    Uniqueness is a load-bearing part of every detector here: a shape that
    suddenly matches twice means the assumption behind it broke, and picking
    the first hit would silently patch the wrong place -- which is exactly how
    an earlier version of this script ended up rewriting an unrelated OAuth
    helper. Refusing is the correct outcome.

    Pass ``warn_on_zero=False`` when a zero-hit outcome is expected -- i.e.
    detectors that first try the unpatched shape and then fall through to a
    "(patched)" shape: on an already-patched binary the unpatched lookups
    match zero times by design. Two or more hits still warn either way.
    """
    hits = list(re.finditer(pattern, data))
    if len(hits) == 1:
        return hits[0]
    if len(hits) == 0 and not warn_on_zero:
        return None
    print(f"  WARN: {what}: expected 1 structural match, found {len(hits)}",
          file=sys.stderr)
    return None


# --- Site model ---------------------------------------------------------------
# A "site" is one same-length byte rewrite at a known absolute offset, produced
# by a structural detector that has already verified what is there. Discovery
# and application are separate so --dry-run can report exactly what would be
# written without a second, differently-shaped code path.

def site(key: str, desc: str, offset: int, old: bytes, new: bytes) -> dict:
    return {"key": key, "desc": desc, "offset": offset, "old": old, "new": new,
            "state": "todo"}


def site_done(key: str, desc: str) -> dict:
    return {"key": key, "desc": desc, "offset": None, "old": None, "new": None,
            "state": "done"}


def apply_site(data: bytearray, s: dict, stats: dict) -> None:
    """Write one site into `data`, or account for it as already applied."""
    if s["state"] == "done":
        print(f"  OK {s['desc']} (already patched)")
        stats["applied"] += 1
        return
    off, old, new = s["offset"], s["old"], s["new"]
    if len(old) != len(new):
        print(f"  SKIP: {s['desc']} - byte length mismatch "
              f"({len(old)} vs {len(new)})", file=sys.stderr)
        stats["failed"] += 1
        s["state"] = "failed"
        return
    actual = bytes(data[off:off + len(old)])
    if actual != old:
        print(f"  SKIP: {s['desc']} - byte mismatch at offset {off}", file=sys.stderr)
        print(f"    Expected: {old!r}", file=sys.stderr)
        print(f"    Actual:   {actual!r}", file=sys.stderr)
        stats["failed"] += 1
        s["state"] = "failed"
        return
    print(f"  OK {s['desc']} @ offset {off}")
    print(f"       {old.decode('utf-8', 'replace')}")
    print(f"    -> {new.decode('utf-8', 'replace')}")
    data[off:off + len(new)] = new
    stats["applied"] += 1
    s["state"] = "applied"


# --- Detector 1: retry cap ----------------------------------------------------

def discover_retry_cap(data: bytes) -> tuple[str, dict | None]:
    """Disable the retry-cap guard used by Claude Code 2.1.306."""
    m = find_unique(
        data,
        rb'if\((?P<guard>' + _IDENT + rb'>' + _IDENT + rb'|!1 *)&&!'
        + _IDENT + rb'\)\{if\(' + _IDENT
        + rb'\(\)\.once\("max_retries_clamp_warning"\)\)' + _IDENT
        + rb'\(`CLAUDE_CODE_MAX_RETRIES=\$\{(?P<value>' + _IDENT
        + rb')\} clamped to \$\{(?P<cap>' + _IDENT + rb')\}`,\{level:"warn"\}\);'
        rb'return (?P=cap)\}return (?P=value)\}',
        "CLAUDE_CODE_MAX_RETRIES clamp",
    )
    if m is None:
        return "unknown", None
    old = m.group("guard")
    if old.strip() == b"!1":
        return "clamped", site_done("max_retries", "Disable retry cap clamp")
    if old != m.group("value") + b">" + m.group("cap"):
        print("  WARN: retry cap guard and warning refer to different variables; "
              "refusing to guess", file=sys.stderr)
        return "unknown", None
    return "clamped", site(
        "max_retries",
        f"Disable retry cap clamp against {m.group('cap').decode()}",
        m.start("guard"), old, fit_literal(["!1"], len(old)))


# --- Detector 2: the API retry backoff helper --------------------------------

def discover_backoff_fn(data: bytes) -> dict | None:
    """Match the 2.1.306 API wrapper and its adjacent base/jitter constants.

    Bind each parameter to its use in the shared helper and Retry-After parser.
    Only this wrapper's attempt is pinned; unrelated shared-helper calls stay
    untouched. The same shape also recognizes our equal-length patched bytes.
    """
    m = find_unique(
        data,
        rb'(?:var|let|const) (?P<base_var>' + _IDENT + rb')=(?P<base>' + _NUMBER + rb')'
        rb',(?P<ratio_var>' + _IDENT + rb')=' + _NUMBER + rb';'
        rb'(?P<body>function (?P<name>' + _IDENT + rb')\((?P<attempt>' + _IDENT + rb'),'
        rb'(?P<retry>' + _IDENT + rb'),(?P<cap>' + _IDENT + rb')=' + _NUMBER + rb','
        rb'(?P<random>' + _IDENT + rb')=Math\.random,'
        rb'(?P<base_arg>' + _IDENT + rb')=(?P=base_var)\)\{let (?P<jitter>' + _IDENT + rb')'
        rb'=Math\.round\(' + _IDENT + rb'\(\{attempt:(?P<step>(?P=attempt)|1 *)'
        rb',baseMs:(?P=base_arg),capMs:(?P=cap),'
        rb'jitter:\{kind:"proportional",ratio:(?P=ratio_var)'
        rb'\},random:(?P=random)\}\)\);'
        rb'if\((?P=retry)\)\{let (?P<seconds>' + _IDENT + rb')=parseInt\((?P=retry),10\);'
        rb'if\(!isNaN\((?P=seconds)\)\)(?P<header>return Math\.max\((?P=seconds)\*1000,'
        rb'(?P=jitter)\)|return (?P=jitter) {2,})\}return (?P=jitter)\})',
        "API retry backoff helper",
    )
    if m is None:
        return None
    return {
        "name": m.group("name"), "body_start": m.start("body"),
        "base": {"var": m.group("base_var"), "offset": m.start("base"),
                 "raw": m.group("base")},
        "attempt": {"offset": m.start("step"), "raw": m.group("step")},
        "header": {"offset": m.start("header"), "raw": m.group("header"),
                   "jitter": m.group("jitter"),
                   "patched": not m.group("header").startswith(b"return Math.max")},
    }


def backoff_sites(fn: dict) -> list[dict]:
    """Turn a discovered backoff helper into concrete rewrites."""
    sites = []
    base = fn["base"]
    where = f"const {base['var'].decode()}"
    if parse_number(base["raw"]) == TARGET_DELAY_MS:
        sites.append(site_done("backoff_base", f"Backoff base already {TARGET_DELAY_MS}ms ({where})"))
    else:
        new = js_number(TARGET_DELAY_MS, len(base["raw"]))
        if new is None:
            print(f"  WARN: cannot fit {TARGET_DELAY_MS} into the "
                  f"{len(base['raw'])}-byte backoff base slot", file=sys.stderr)
        else:
            sites.append(site(
                "backoff_base",
                f"Backoff base {base['raw'].decode()} -> {TARGET_DELAY_MS}ms ({where})",
                base["offset"], base["raw"], new))

    attempt = fn["attempt"]
    # Pin this wrapper's input, never the shared helper's exponential factor.
    if attempt["raw"].strip() == b"1":
        sites.append(site_done("backoff_pow", "Backoff attempt already fixed to 1"))
    else:
        sites.append(site(
            "backoff_pow", "Disable exponential growth (delegated attempt -> 1)",
            attempt["offset"], attempt["raw"], fit_literal(["1"], len(attempt["raw"]))))

    hdr = fn["header"]
    if hdr["patched"]:
        sites.append(site_done("retry_after_backoff",
                               "Retry-After already ignored in the backoff helper"))
    else:
        old = hdr["raw"]
        new = fit_literal(["return " + hdr["jitter"].decode()], len(old))
        sites.append(site(
            "retry_after_backoff",
            "Ignore Retry-After in the backoff helper (return the ~1s jitter, "
            "not the header's seconds)",
            hdr["offset"], old, new))
    return sites


# --- Detector 3: pin the retry loop's delay assignments ----------------------

def discover_delay_pins(data: bytes, fn_name: bytes) -> list[dict]:
    """Pin every "how long until the next attempt" assignment to 1000ms.

    Anchored on the retry telemetry event, which names the delay variable in
    its own payload and so survives any renaming:

        G("tengu_api_retry", { attempt: I, delayMs: x, ... })

    Everything assigned to that variable just above -- in the build this was
    written against:

        x = Math.min(resetMs ?? backoff(n, retryAfter, cap, rng), ceil)
        x = backoff(attempt + n, retryAfter, cap, rng, customBase)

    -- becomes a literal 1000. Rewriting the ASSIGNMENT rather than only the
    backoff helper matters because of the `??`: a non-null reset header
    short-circuits the helper entirely, so patching the helper alone leaves
    that path waiting minutes.
    """
    anchor = find_unique(
        data,
        rb'"tengu_api_retry",\{attempt:' + _IDENT + rb',delayMs:(' + _IDENT + rb')[,}]',
        "API retry telemetry (delay variable)",
    )
    if anchor is None:
        return []
    var = anchor.group(1)
    lo = max(0, anchor.start() - 900)
    region = data[lo:anchor.start()]

    # Calls include attempt sums, an injected RNG and a conditional base.
    value = rb'(?:void 0|' + _IDENT + rb'(?:[.+]' + _IDENT + rb')*)'
    arg = value + rb'(?:\?' + value + rb':' + value + rb')?'
    call = re.escape(fn_name) + rb'\(' + arg + rb'(?:,' + arg + rb')*\)'
    assign = (_NOT_IDENT + re.escape(var) + rb'=(?:Math\.min\('
              + _IDENT + rb'\?\?' + call + rb',' + _IDENT + rb'\)|' + call
              + rb')(?=[,;)}])')
    pinned = _NOT_IDENT + re.escape(var) + re.escape(b"=%d" % TARGET_DELAY_MS) + rb' {2,}'

    sites = []
    for i, m in enumerate(re.finditer(assign, region), 1):
        old = m.group(0)
        new = fit_literal([f"{var.decode()}={TARGET_DELAY_MS}"], len(old))
        if new is None:
            print(f"  WARN: delay assignment {i} is too short to pin", file=sys.stderr)
            continue
        sites.append(site(
            f"delay_pin_{i}",
            f"Pin retry delay assignment {i} to {TARGET_DELAY_MS}ms",
            lo + m.start(), old, new))
    if sites:
        return sites
    for i, _ in enumerate(re.finditer(pinned, region), 1):
        sites.append(site_done(f"delay_pin_{i}",
                               f"Retry delay assignment {i} already pinned"))
    if not sites:
        print("  WARN: no retry delay assignment found near the telemetry anchor",
              file=sys.stderr)
    return sites


# --- Detector 4: rate-limit fallback delays ----------------------------------

def discover_rate_limit(data: bytes, ms_parser_name: bytes | None) -> list[dict]:
    """Shorten the 2.1.306 retry loop's rate-limit fallback and minimum waits.

    Bind the inline threshold and fallback to the same Retry-After parser
    result, then resolve all three constants in one unique declaration group.
    """
    if ms_parser_name is None:
        return []
    path = find_unique(
        data,
        rb'let (' + _IDENT + rb')=' + re.escape(ms_parser_name)
        + rb'\((' + _IDENT + rb')\),(' + _IDENT + rb')=\1!==null&&\1<('
        + _IDENT + rb');[\s\S]{0,600}?if\(!\3\)\{let (' + _IDENT
        + rb')=Math\.max\(\1\?\?(' + _IDENT + rb'),(' + _IDENT + rb')\)',
        "rate-limit retry path",
    )
    if path is None:
        return []
    variables = (path.group(4), path.group(6), path.group(7))

    # Short names recur in unrelated modules; resolve the constants together.
    names = rb'(?:' + b'|'.join(re.escape(v) for v in variables) + rb')'
    pair = names + rb'=' + _NUMBER + rb' *'
    groups = []
    for declaration in re.finditer(
        _NOT_IDENT + rb'(?:var|let|const) +'
        + pair + rb'(?:,' + pair + rb'){2}(?=;)', data
    ):
        entries = list(re.compile(rb'(' + names + rb')=(' + _NUMBER + rb')').finditer(
            data, declaration.start(), declaration.end()))
        if {m.group(1) for m in entries} == set(variables) and len(set(variables)) == 3:
            groups.append({m.group(1): m for m in entries})
    if len(groups) != 1:
        print(f"  WARN: rate-limit constant declaration group: expected 1 "
              f"structural match, found {len(groups)}; refusing to guess",
              file=sys.stderr)
        return []
    declarations = groups[0]

    wanted = [
        ("rl_threshold", variables[0], RATE_LIMIT_THRESHOLD_MS,
         "Rate-limit inline-sleep threshold"),
        ("rl_fallback", variables[1], TARGET_DELAY_MS,
         "Rate-limit fallback wait"),
        ("rl_min", variables[2], TARGET_DELAY_MS,
         "Rate-limit minimum wait"),
    ]
    sites = []
    for key, var, target, label in wanted:
        decl = declarations[var]
        raw = decl.group(2)
        current = parse_number(raw)
        if current == target:
            sites.append(site_done(key, f"{label} already {target}ms ({var.decode()})"))
            continue
        new = js_number(target, len(raw))
        if new is None:
            print(f"  WARN: cannot fit {target} into the {len(raw)}-byte "
                  f"{var.decode()} slot", file=sys.stderr)
            continue
        sites.append(site(
            key,
            f"{label} {raw.decode()} -> {target}ms ({var.decode()})",
            decl.start(2), raw, new))
    return sites


# --- Detector 5: Retry-After / ratelimit-reset parsers -----------------------

def discover_retry_after_ms_parser(data: bytes) -> tuple[bytes | None, dict | None]:
    """Stop the Retry-After header from being consumed as a sleep duration.

        function NAME(err) {
            let h = header(err);
            if (h) { let s = parseInt(h, 10); if (!isNaN(s)) return s * 1000 }
            return null
        }

    Forcing the `null` return sends the caller to the (now 1s) backoff path.
    Returns (function name, site) -- the name is fed back into the rate-limit
    detector so the two are proven to be the same code path.
    """
    m = find_unique(
        data,
        rb'function (' + _IDENT + rb')\((' + _IDENT + rb')\)\{let (' + _IDENT + rb')='
        + _IDENT + rb'\(\2\);if\(\3\)\{let (' + _IDENT + rb')=parseInt\(\3,10\);'
        + rb'if\(!isNaN\(\4\)\)(return \4\*1000)\}return null\}',
        "Retry-After milliseconds parser", warn_on_zero=False,
    )
    if m is not None:
        old = m.group(5)
        new = fit_literal(["return null"], len(old))
        if new is None:
            return m.group(1), None
        return m.group(1), site(
            "retry_after_ms",
            f"Ignore Retry-After in {m.group(1).decode()}() "
            f"(return null instead of the header's seconds)",
            m.start(5), old, new)

    done = find_unique(
        data,
        rb'function (' + _IDENT + rb')\((' + _IDENT + rb')\)\{let (' + _IDENT + rb')='
        + _IDENT + rb'\(\2\);if\(\3\)\{let (' + _IDENT + rb')=parseInt\(\3,10\);'
        + rb'if\(!isNaN\(\4\)\)return null +\}return null\}',
        "Retry-After milliseconds parser (patched)",
    )
    if done is not None:
        return done.group(1), site_done(
            "retry_after_ms", "Retry-After already ignored in the ms parser")
    return None, None


def discover_ratelimit_reset(data: bytes) -> dict | None:
    """Stop `anthropic-ratelimit-unified-reset` from setting the retry delay.

        let t = headers.get?.("anthropic-ratelimit-unified-reset");
        if (!t) return null

    The header NAME is a wire protocol string, not a minified identifier, so
    matching on it is stable across builds. Forcing `if(!0)` takes the null
    branch and falls through to the ~1s backoff.
    """
    m = find_unique(
        data,
        rb'get(?:\?\.)?\("anthropic-ratelimit-unified-reset"\);(if\(!(' + _IDENT
        + rb')\))return null',
        "ratelimit-unified-reset guard", warn_on_zero=False,
    )
    if m is not None:
        old = m.group(1)
        new = fit_literal(["if(!0)"], len(old))
        if new is None:
            return None
        return site(
            "ratelimit_reset",
            "Ignore anthropic-ratelimit-unified-reset (take the null branch)",
            m.start(1), old, new)
    if re.search(rb'get(?:\?\.)?\("anthropic-ratelimit-unified-reset"\);if\(!0\)', data):
        return site_done("ratelimit_reset",
                         "anthropic-ratelimit-unified-reset already ignored")
    return None


# --- Detector 6: the bundled Anthropic SDK -----------------------------------
# The SDK is bundled from TypeScript source with its class methods intact, so
# `retryRequest` / `calculateDefaultRetryTimeoutMillis` are real API names, not
# minifier output -- stable anchors. Only the locals inside are minified.

def discover_sdk_backoff(data: bytes) -> dict | None:
    """SDK: `Math.min(0.5*Math.pow(2,n), 8)` seconds -> a flat ~1s.

        calculateDefaultRetryTimeoutMillis(e, t) {
            let n = t - e, s = Math.min(0.5 * Math.pow(2, n), 8),
                jitter = 1 - Math.random() * 0.25;
            return s * jitter * 1000
        }

    Setting the coefficient to 1 and the exponent base to 1 leaves
    1 * (0.75..1) * 1000 ms.
    """
    m = find_unique(
        data,
        rb'calculateDefaultRetryTimeoutMillis\(' + _IDENT + rb',' + _IDENT + rb'\)\{'
        rb'let (' + _IDENT + rb')=' + _IDENT + rb'-' + _IDENT + rb',' + _IDENT
        + rb'=Math\.min\((' + _NUMBER + rb')\*Math\.pow\(([12]),\1\),' + _NUMBER + rb'\)',
        "SDK calculateDefaultRetryTimeoutMillis",
    )
    if m is None:
        return None
    coeff_raw, pow_raw = m.group(2), m.group(3)
    if parse_number(coeff_raw) == 1 and pow_raw == b"1":
        return site_done("sdk", "SDK backoff already flat (~1s)")
    old = data[m.start(2):m.end(3)]
    coeff_new = fit_literal(["1.0", "1", "1e0"], len(coeff_raw))
    if coeff_new is None:
        return None
    new = coeff_new + old[len(coeff_raw):-1] + b"1"
    return site(
        "sdk",
        f"SDK backoff {coeff_raw.decode()}*pow({pow_raw.decode()},n) -> "
        f"{coeff_new.decode().strip()}*pow(1,n) (fixed ~0.75-1s)",
        m.start(2), old, new)


def discover_sdk_retry_after(data: bytes) -> list[dict]:
    """SDK retryRequest: skip both Retry-After headers.

        async retryRequest(opts, left, logId, headers) {
            let ms, a = headers?.get("retry-after-ms");
            if (a) { ... ms = parseFloat(a) }
            let b = headers?.get("retry-after");
            if (b && !ms) { ... }
            ...
        }

    Both guards are forced false so the SDK falls through to
    calculateDefaultRetryTimeoutMillis, which the patch above pins to ~1s.
    The header names are wire protocol strings, so they anchor reliably.
    """
    sites = []
    ms = find_unique(
        data,
        rb'retryRequest\((?:' + _IDENT + rb',){3}' + _IDENT + rb'\)\{let ' + _IDENT
        + rb',(' + _IDENT + rb')=' + _IDENT + rb'(?:\?\.)?get\("retry-after-ms"\);'
        + rb'(if\(\1\))\{',
        "SDK retry-after-ms guard", warn_on_zero=False,
    )
    if ms is not None:
        old = ms.group(2)
        new = fit_literal(["if(0)"], len(old))
        if new is not None:
            sites.append(site("sdk_ra_ms", "Ignore SDK retry-after-ms header",
                              ms.start(2), old, new))
    elif re.search(rb'get(?:\?\.)?\("retry-after-ms"\);if\(0\)', data):
        sites.append(site_done("sdk_ra_ms", "SDK retry-after-ms already ignored"))

    ra = find_unique(
        data,
        rb'let (' + _IDENT + rb')=' + _IDENT + rb'(?:\?\.)?get\("retry-after"\);'
        rb'(if\(\1&&!(' + _IDENT + rb')\))\{let ' + _IDENT + rb'=parseFloat\(\1\)',
        "SDK retry-after guard", warn_on_zero=False,
    )
    if ra is not None:
        old = ra.group(2)
        new = fit_literal([f"if(0&&!{ra.group(3).decode()})"], len(old))
        if new is not None:
            sites.append(site("sdk_ra", "Ignore SDK retry-after / HTTP-date header",
                              ra.start(2), old, new))
    elif re.search(rb'get(?:\?\.)?\("retry-after"\);if\(0&&', data):
        sites.append(site_done("sdk_ra", "SDK retry-after already ignored"))
    return sites


# --- Orchestration ------------------------------------------------------------

def discover_all(data: bytes) -> list[dict]:
    """Run every detector and return the full list of sites, in patch order."""
    sites: list[dict] = []

    print("=== Patch 1: Retry cap (CLAUDE_CODE_MAX_RETRIES) ===")
    _, cap_site = discover_retry_cap(data)
    if cap_site is not None:
        sites.append(cap_site)
    else:
        print("  SKIP: could not determine whether the retry cap is enforced",
              file=sys.stderr)

    print("=== Patch 2: Exponential backoff -> fixed 1s ===")
    fn = discover_backoff_fn(data)
    if fn is not None:
        print(f"  backoff helper: {fn['name'].decode()}() @ {fn['body_start']}")
        sites += backoff_sites(fn)
    else:
        print("  SKIP: API retry backoff helper not located", file=sys.stderr)

    print("=== Patch 3: Bundled Anthropic SDK backoff ===")
    sdk = discover_sdk_backoff(data)
    if sdk is not None:
        sites.append(sdk)
    else:
        print("  SKIP: SDK backoff not located", file=sys.stderr)

    print("=== Patch 4: Retry-After / ratelimit-reset ===")
    ms_name, ms_site = discover_retry_after_ms_parser(data)
    if ms_site is not None:
        sites.append(ms_site)
    else:
        print("  SKIP: Retry-After milliseconds parser not located", file=sys.stderr)
    reset_site = discover_ratelimit_reset(data)
    if reset_site is not None:
        sites.append(reset_site)
    else:
        print("  SKIP: ratelimit-unified-reset guard not located", file=sys.stderr)
    sdk_ra = discover_sdk_retry_after(data)
    if len(sdk_ra) == 2:
        sites += sdk_ra
    else:
        print(f"  SKIP: expected 2 SDK Retry-After guards, located {len(sdk_ra)}",
              file=sys.stderr)

    print("=== Patch 5: Rate-limit fallback delays ===")
    rl = discover_rate_limit(data, ms_name)
    if len(rl) == 3:
        sites += rl
    else:
        print(f"  SKIP: expected 3 rate-limit constants, located {len(rl)}",
              file=sys.stderr)

    print("=== Patch 6: Pin the retry loop's delay to 1s ===")
    if fn is not None:
        pins = discover_delay_pins(data, fn["name"])
        sites += pins
        if pins:
            print(f"  delay assignments: {len(pins)}")
    else:
        print("  SKIP: needs the backoff helper from Patch 2", file=sys.stderr)

    return sites


def looks_already_patched(sites: list[dict]) -> bool:
    """True when every located site is already in its patched state.

    Patching again would be a no-op at best, so the write path refuses and asks
    for a --restore first. --dry-run is allowed through: it writes nothing, and
    reporting the current per-site state is exactly what it is for.
    """
    return bool(sites) and all(s["state"] == "done" for s in sites)


def write_patched(binary_path: str, data: bytearray) -> None:
    """Atomically replace the binary with the patched bytes.

    Uses a temp file + os.replace(), which is atomic and overwrites the
    destination. On Linux this swaps the inode, so a running process keeps its
    old mapping.

    Because the inode is swapped, the new file does NOT inherit the original's
    owner/mode: mkstemp creates it 0600 owned by the caller (root under sudo),
    while npm installs are often user-owned. Copy both across so a later
    `npm update` isn't left with a root-owned binary.
    """
    import tempfile
    st = os.stat(binary_path)
    binary_dir = os.path.dirname(binary_path)
    fd, tmp_path = tempfile.mkstemp(dir=binary_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        # Keep the original mode, but guarantee the exec bit.
        os.chmod(tmp_path, (st.st_mode & 0o7777) | 0o111)
        if hasattr(os, "chown") and (st.st_uid != os.getuid() or st.st_gid != os.getgid()):
            try:
                os.chown(tmp_path, st.st_uid, st.st_gid)
            except (OSError, AttributeError) as e:
                print(f"  WARN: could not restore owner {st.st_uid}:{st.st_gid} "
                      f"on the patched binary: {e}", file=sys.stderr)
        os.replace(tmp_path, binary_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def print_summary(sites: list[dict]) -> None:
    """Report what each behavior actually ended up as."""
    state = {}
    for s in sites:
        state.setdefault(s["key"], s["state"] in ("applied", "done"))

    def row(setting, keys, patched_text):
        flags = [state[k] for k in keys if k in state]
        missing = len([k for k in keys if k not in state])
        total = len(flags) + missing
        done = sum(1 for f in flags if f)
        if total == 0:
            behavior = "not located"
        elif done == total:
            behavior = patched_text
        elif done == 0:
            behavior = "unchanged (patch skipped)"
        else:
            behavior = f"partially patched ({done}/{total})"
        return f"  | {setting:<24}| {behavior:<33}|"

    pin_keys = [k for k in state if k.startswith("delay_pin_")]
    sep = "  +-------------------------+----------------------------------+"
    print(sep)
    print(f"  | {'Setting':<24}| {'Behavior':<33}|")
    print(sep)
    print(row("Max retries", ["max_retries"], "CLAUDE_CODE_MAX_RETRIES (=9999)"))
    print(row("General retry delay", ["backoff_base", "backoff_pow"], "Fixed ~1 second"))
    print(row("API retry loop delay", pin_keys or ["delay_pin_1"], "Pinned to 1000ms"))
    print(row("Rate-limit retry delay",
              ["rl_fallback", "rl_min", "rl_threshold"], "Fixed ~1 second"))
    print(row("SDK-level retry delay", ["sdk"], "Fixed ~0.75-1 second"))
    print(row("Retry-After / reset",
              ["retry_after_backoff", "retry_after_ms", "ratelimit_reset",
               "sdk_ra_ms", "sdk_ra"], "ignored (always ~1s)"))
    print(sep)


def main():
    parser = argparse.ArgumentParser(
        description="Patch Claude Code binary (Linux/macOS): allow 9999 retries, fix backoff to 1s interval"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show changes without modifying the binary")
    parser.add_argument("--restore", action="store_true", help="Restore the original binary from backup")
    parser.add_argument("--binary", help="Patch this file instead of the auto-discovered one")
    args = parser.parse_args()

    require_platform()

    if args.binary:
        binary_path = os.path.realpath(os.path.expanduser(args.binary))
        if not os.path.isfile(binary_path):
            print(f"ERROR: no such file: {binary_path}", file=sys.stderr)
            sys.exit(1)
    else:
        binary_path = find_binary()
    print(f"Found binary: {binary_path}")
    print(f"Binary size: {os.path.getsize(binary_path)} bytes")

    backup_path = binary_path + ".orig"

    # -- Restore mode ----------------------------------------------------------
    if args.restore:
        if not os.path.isfile(backup_path):
            print(f"ERROR: No backup found at {backup_path}", file=sys.stderr)
            sys.exit(1)
        print(f"Restoring original binary from {backup_path} ...")
        try:
            shutil.copy2(backup_path, binary_path)
            os.chmod(binary_path, 0o755)
            print("Restored successfully.")
        except OSError as e:
            print(f"ERROR: Failed to restore: {e}", file=sys.stderr)
            print(f"Is {PROC_NAME} running? {STOP_HINT}", file=sys.stderr)
            sys.exit(1)
        return

    # -- Read binary -----------------------------------------------------------
    with open(binary_path, "rb") as f:
        data = bytearray(f.read())

    require_unix_image(data, binary_path)

    # -- Locate every site by structure ----------------------------------------
    print()
    print("=== Locating patch sites by code structure ===")
    print()
    sites = discover_all(bytes(data))

    # -- Already patched: refuse to write, but let --dry-run report the state ---
    # Checked before the backup so a patched binary is never copied over a good
    # .orig (--dry-run never reaches the backup step at all).
    already_patched = looks_already_patched(sites)
    if already_patched:
        if not args.dry_run:
            print()
            print("This binary already appears to be patched by this script.")
            print("Restore the original first, then re-run:")
            print(f"  sudo python3 {sys.argv[0]} --restore")
            sys.exit(1)
        print()
        print("NOTE: already patched - reporting current state (nothing will be written).")

    # -- Create backup (skip in --dry-run: it must not touch disk) --------------
    if not args.dry_run and not os.path.isfile(backup_path):
        print(f"Creating backup at {backup_path} ...")
        try:
            shutil.copy2(binary_path, backup_path)
        except OSError as e:
            print(f"ERROR: Failed to create backup: {e}", file=sys.stderr)
            print(f"Is {PROC_NAME} running? {STOP_HINT}", file=sys.stderr)
            sys.exit(1)

    # -- Apply -----------------------------------------------------------------
    print()
    print("=== Applying patches ===")
    stats = {"applied": 0, "failed": 0}
    for s in sites:
        apply_site(data, s, stats)

    print()
    print("===========================================================")
    print(f"  Patches {'in place' if already_patched else 'applied'}: {stats['applied']}")
    print(f"  Patches skipped: {stats['failed']}")
    print("===========================================================")

    if args.dry_run:
        print()
        print_summary(sites)
        print()
        if already_patched:
            print("DRY RUN - binary is already patched; nothing to do.")
            print(f"To re-apply from scratch: sudo python3 {sys.argv[0]} --restore")
        else:
            print("DRY RUN - no changes were made.")
            print("Run without --dry-run to apply patches.")
        return

    # -- Nothing patched: bail out instead of printing misleading success text -
    if stats["applied"] == 0:
        print()
        print("ERROR: No patches were applied; the binary is unchanged.", file=sys.stderr)
        print("Claude Code was likely updated and the code shapes no longer match.", file=sys.stderr)
        sys.exit(1)

    # -- Write patched binary --------------------------------------------------
    try:
        write_patched(binary_path, data)
        print()
        print("Patches applied successfully!")
        print("(Running claude sessions still use the old binary; new sessions will use the patched one.)")
    except OSError as e:
        print(f"\nERROR: Failed to write patched binary: {e}", file=sys.stderr)
        print(f"Is {PROC_NAME} running? {STOP_HINT} Then re-run this script.", file=sys.stderr)
        sys.exit(1)

    # -- Final hints -----------------------------------------------------------
    print()
    print("To restore the original binary:")
    print(f"  sudo python3 {sys.argv[0]} --restore")
    print()
    print("Set these environment variables before running claude:")
    print("  export CLAUDE_CODE_MAX_RETRIES=9999       # Max retry attempts (internal cap disabled)")
    print("  export CLAUDE_CODE_RETRY_WATCHDOG=1       # Persistent 429/overloaded retry")
    print("  export BUN_JSC_forceDebuggerBytecodeGeneration=1  # Recompile patched source in Bun")
    print()
    print("Retry behavior after patching:")
    print_summary(sites)
    print()
    print("NOTE: After updating Claude Code (npm update), re-run this script.")


if __name__ == "__main__":
    main()
