#!/usr/bin/env python3
"""
Patch Codex CLI's retry backoff interval -- Linux/macOS build (ELF / Mach-O).

Verified against codex 0.164.0 and 0.159.1 (x86_64-unknown-linux-musl). This is the
Unix (ELF/Mach-O) build of the codex patcher; for the Windows PE build use
codex-windows.py. The two jittered backoffs it targets are:

  1. codex-client/src/retry.rs::backoff(base, attempt)  -- generic retry path
       `sleep(backoff(policy.base_delay, attempt+1))`.
  2. core/src/util.rs::backoff(attempt)                 -- stream-reconnect path
       Re-export of codex_async_utils::backoff since 0.156 ("Reconnecting N/M"
       delay when the server sends no explicit retry-after).

Both compute `Duration::from_millis((f64_delay * jitter) as u64)` with
`jitter = rand::rng().random_range(0.9..1.1)`, and in this build both are
INLINED into their async poll functions -- so, unlike the 0.142.x layout, there
is no standalone `backoff` entry to overwrite with a return-stub.

Instead this patcher rewrites each site *in place*. The value that flows into
the inlined `Duration::from_millis(...)` lives in a GP register right before the
tail:

    <mulsd  xmm, xmm>            ; delay_ms = f64_delay * jitter   (last reg-form mulsd)
    ... saturating f64->u64 ...  ; clamp to a u64 millisecond count
    mov     rax, <millis_reg>    ; from_millis reads the count from <millis_reg>
    shr     rax, 3
    movabs  rcx/rdx, 0x20c49ba5e353f7cf   ; /1000 magic  -> secs + nanos

We overwrite the span between the final `mulsd` and that `mov rax,<millis_reg>`
with `mov <millis_reg>, <ms>` (+ NOP padding), so the native from_millis
codegen that follows splits our constant into {secs, nanos} unchanged. The
result is a fixed retry interval independent of base delay, attempt, and jitter.

Location is anchored on the same x86-64 Rust codegen used by ELF and Mach-O:
  * the single 0.9 rodata constant from `0.9..1.1` (deduped across both sites);
  * the `addsd xmm,[rip->0.9]` that consumes it (this is what distinguishes the
    jitter sites from unrelated 0.9 uses -- a plain `movsd [0.9]` is ignored;
    the MSVC-only movsd variant lives in the Windows script);
  * the `mov rax,reg / shr rax,3 / movabs <from_millis magic>` tail.
RIP-relative operands resolve in file-offset space on the single-mapping ELF /
Mach-O images, so no VA translation is needed (that is the PE-only complication
handled by the Windows script).

Site 3 (stream_max_retries().min(100) hard cap) is unchanged from prior builds:
the inlined `unwrap_or(5).min(100)` codegen is byte-identical, so the same tail
signature locates every inlined copy and we rewrite the cap immediate to
STREAM_MAX_RETRIES so a large stream_max_retries in config.toml is honored.

Sites 1-2 alone do NOT give a fixed interval, because two other delay sources in
core/src/responses_retry.rs outrank or bypass `backoff()` entirely:

  4. CodexErr::retry_delay (protocol/src/error.rs). Callers do
         server_retry_delay.unwrap_or_else(|| backoff(retry_count))
     A server-supplied Retry-After wins and `backoff()` is never called, so the
     wait becomes whatever the endpoint asked for. `Option<Duration>` is niche-
     encoded (nanos == 1_000_000_000 means None), so the branch reads:
         cmp  <r32>, 0x3b9aca00   ; None?
         jne  <skip the backoff call>
         call <codex_async_utils::backoff>
     NOP-ing the `jne` makes the fixed backoff unconditional. The is_retryable
     match above that branch is not a wait, so it is left alone. Accepted only
     when the call provably lands inside a patched backoff, which is what stops
     a look-alike Option<Duration> unwrap from ever being rewritten. On 0.159.1
     that accepts 4 sites: two call util.rs::backoff (the functions that also
     hold the site-5 ladder) and two call retry.rs::backoff.
     In 0.164.0 the server delay is a deadline (Option<Instant>, None == -1),
     with the opposite layout: `cmp r32,-1 ; je <backoff args/call>`. The Some
     arm computes the remaining wait and skips the call (or returns). Rewrite
     only that `je` to an unconditional jump, leaving error eligibility alone.
     This form requires an ELF unwind-table-confirmed backoff entry, decoded
     argument setup and a verified Some-arm exit; 16 sites match on 0.164.0.

  5. The `unbounded_connection_retries` ladder (Stable, default ON): on a
     ConnectionFailed the delay is a separate field that starts at
     `Duration::from_secs(5)` and doubles up to `from_secs(60)`. Those are plain
     whole-second constants, not `from_millis(f64 * jitter)`, so the sites 1-2
     anchors cannot see them. Rewriting the *load* pins the delay no matter what
     the ladder stored -- killing the 5s start and the doubling in one edit:
         mov <secs64>, [<base>+0x10]   ->   mov <secs32>, <whole seconds>
         mov <nanos32>,[<base>+0x18]   ->   xor <nanos32>, <nanos32>
     Both forms are exactly 7 bytes, so the stores of that pair into the async
     state machine (which is what identifies the sequence) stay in place. Site 5
     can only express whole seconds; RETRY_MS is rounded up to >=1s for it.

  6. `ApiRetryConfig.retry_429` in `ModelProviderInfo::to_api_provider` is
     hardwired to `false`, so the HTTP layer's `RetryOn::should_retry` -- which
     lists 429 explicitly -- never gets to retry one. A 429 then falls through
     to `CodexErr::RetryLimit`, whose `retry_delay()` arm is
     `self.server_retry_delay()` with no `backoff()` fallback: with no
     Retry-After the turn dies on the spot, and sites 1-4 never run. Flipping
     the packed bool to `true` (`movw $0x0100` -> `$0x0101`) makes a bare 429
     retryable at the HTTP layer, where the fixed 1s interval now applies for
     `request_max_retries` attempts.

  7. `request_max_retries()` clamps the configured HTTP retry budget with
     `.min(MAX_REQUEST_MAX_RETRIES)`, which is 100 in this build. Site 3 raises
     the stream cap; this one does the same for the request cap, so site 6's
     enlarged budget is actually reachable. Unlike site 3, the Option
     discriminant is already in a register here, so the clamp compiles as
         cmp <val>,<cap> ; mov <acc>,<cap> ; cmovb <acc>,<val>
         test <opt>,<opt> ; mov <res>,<default> ; cmovne <res>,<acc>
     and the `cmp` is imm8 (no room for 9999). The whole 13/14-byte clamp is
     replaced with `mov <acc>,9999 ; cmp <val>,<acc> ; cmovb <acc>,<val>`
     (NOP-padded), i.e. a true `.min(9999)`. It is accepted only when `res` is
     the register the site-6 literal loads `max_attempts` from, which is what
     separates it from the many unrelated `.min(100)` shapes in the image.

DESIGN RULE -- this patcher only ever changes "how long to wait after a failure
has already been declared". It must never change what counts as a failure, nor
how long detection takes (stream_idle_timeout_ms and friends are config, not
patch targets). Any site added later has to satisfy that; see README.md.

Site 6 is the single, deliberate exception, and it is narrow: codex already
classifies 429 as a rate-limit signal and `RetryOn::should_retry` already has
the 429 arm; the provider literal just disables it. Turning it back on restores
the retry codex is built to perform, and it can only re-try a request the server
already refused -- it cannot turn a *thinking* model into a failure.

This changes the retry *interval*, not the retry *count*; use config.toml's
stream_max_retries/request_max_retries for the count (site 3 just unclamps the
stream cap so values >100 take effect). Site 6 makes 429 retryable, but the
budget is still `request_max_retries` (default 4, hard cap 100 in this build),
so set it explicitly when a long rate-limit wall is expected. Site 7 raises
that hard cap to REQUEST_MAX_RETRIES, so `request_max_retries = 9999` now means
9999 retries at the fixed interval.

Site 4 makes codex ignore Retry-After, so a genuine quota wall is retried every
second instead of when the server said to come back. That is the point of the
patch, but do not point it at an endpoint that will penalize the hammering.

Usage:
  python3 codex-linux.py --dry-run   (inspect only)
  sudo python3 codex-linux.py        (apply the patch)
  python3 codex-linux.py --restore
  python3 codex-linux.py --self-test

The retry interval and stream cap are fixed (RETRY_MS / STREAM_MAX_RETRIES).
"""

from __future__ import annotations

import argparse
import bisect
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path


PLATFORM_TARGETS = {
    ("Linux", "x86_64"): ("@openai/codex-linux-x64", "x86_64-unknown-linux-musl", "codex"),
    ("Linux", "aarch64"): ("@openai/codex-linux-arm64", "aarch64-unknown-linux-musl", "codex"),
    ("Darwin", "x86_64"): ("@openai/codex-darwin-x64", "x86_64-apple-darwin", "codex"),
    ("Darwin", "arm64"): ("@openai/codex-darwin-arm64", "aarch64-apple-darwin", "codex"),
}

# Site 3 (identical across System V and MSVC builds). Only the fixed middle is
# matched; the cap and the unwrap_or default are read as immediates, so retuning
# either upstream value does not break detection:
#   mov edx,<cap> | cmovb rdx,rcx ; cmp byte[rax+0x10],0 | mov eax,<default> |
#   cmovne rax,rdx
STREAM_CAP_MID = bytes.fromhex("480f42d180781000")     # cmovb + Option check
STREAM_CAP_CMOVNE = bytes.fromhex("480f45c2")          # cmovne rax,rdx

# core::time::Duration::from_millis divide-by-1000 reciprocal (u64 magic).
# Semantic, not incidental: the value is forced by `millis / 1000` on u64, so it
# survives any codex change short of std switching division strategy.
FROM_MILLIS_MAGIC = bytes.fromhex("cff753e3a59bc420")  # 0x20c49ba5e353f7cf, LE
FROM_MILLIS_TAIL_SHR = bytes.fromhex("48c1e803")       # shr rax,3 (the /8 of /1000)

# Sites 1-2: bounds of `random_range(a..b)` are jitter *multipliers*, so they sit
# near 1.0. Matching the range rather than the literal 0.9 means a retuned jitter
# window still resolves. Exactly 1.0 is excluded (that is no jitter at all).
JITTER_BOUND_MIN, JITTER_BOUND_MAX = 0.5, 2.0

# How far back from a from_millis tail the jitter multiply and its range bound
# may sit (the inlined base/jitter arithmetic on this build spans ~70 bytes).
JITTER_SCAN_BACK = 320

# How far back from the from_millis tail to look for retry.rs's `as_millis`
# (`imul` by 1000). That multiply is not part of the jitter span: on 0.159.1 it
# sits 445 bytes before the tail, outside JITTER_SCAN_BACK, so the label window
# is separate and must not loosen site detection.
AS_MILLIS_SCAN_BACK = 1024

# `mov <reg32>, imm32` opcode base; the low 3 bits select the register.
MOV_R32_IMM = 0xB8

# Site 4: `Option<Duration>` is niche-encoded -- nanos == 1_000_000_000 is None.
# (Duration::nanos is validity-restricted to 0..=999_999_999, so std reuses 1e9.)
NICHE_NONE = struct.pack("<I", 1_000_000_000)

# Same-length NOP runs, used to retire a `jne` without moving anything.
NOP_RUNS = {2: b"\x66\x90", 6: b"\x66\x0f\x1f\x44\x00\x00"}

# How far past a backoff entry a patched jitter site may sit for site 4 to accept
# the call as "this really is core::util::backoff" (its body is ~0x160 bytes).
BACKOFF_FN_WINDOW = 0x600

# Site 4: bytes of arg setup tolerated between the `jne` and the backoff `call`,
# and how far past the call the `jne` may land (the Some-path convergence).
SITE4_CALL_WINDOW = 24
SITE4_JOIN_WINDOW = 32
# 0.164.0: `cmp r32,-1 ; [stack movs] ; je <backoff>` for Option<Instant>.
# Keep this separate from the legacy Duration niche: changing only its constant
# or accepting arbitrary branches would also match error-eligibility checks.
SITE4_MOV_WINDOW = 24
SITE4_SOME_WINDOW = 256
SITE4_DEADLINE_CMP = re.compile(rb"[\x40\x41]?\x83[\xf8-\xff]\xff")
MAX_RETRY_AFTER_SITES = 32

# Site 5 circuit breaker: 0.159.1 still has exactly two connection ladders
# (turn.rs sampling and compact_remote_v2.rs), each next to a site-4 caller of
# util.rs::backoff. A much larger count means the signature went loose, and
# site 5 rewrites live instructions, so refuse rather than guess.
MAX_CONN_SITES = 4

# Site 5: the delay is confirmed by the `.min(MAX_CONNECTION_RETRY_DELAY)` clamp
# in the same function -- a `cmp r64, imm8` against a whole-second cap. Bounded
# by imm8 encoding; the range covers any plausible retry ceiling (currently 60s).
LADDER_CAP_MIN, LADDER_CAP_MAX = 2, 127
LADDER_WINDOW = 0x1000
# Bytes between the `cmp r64,cap+1` and `cmp r64,cap` halves of the saturating
# min() (on this build a single `setae` sits between them).
LADDER_PAIR_GAP = 12

# Site 6: `ApiRetryConfig.retry_429` in ModelProviderInfo::to_api_provider.
# rustc reorders the fields to put the 16-byte Duration first, so the literal
# compiles to a nanosecond store, then a packed 16-bit retry_429/retry_5xx
# store at nanos+0x10, then the retry_transport byte at nanos+0x12:
#   +0x00 base_delay.secs   +0x08 base_delay.nanos   +0x10 max_attempts
#   +0x18 retry_429  +0x19 retry_5xx  +0x1a retry_transport
# Duration::nanos is validity-restricted to 0..=999_999_999, so a store of a
# value in that range is a plausible base_delay and nothing else nearby is.
RETRY_429_NANOS_MAX = 999_999_999
# How far the nanos store may sit before the packed bool pair (the max_attempts
# `mov` between them is a few bytes; this is generous on purpose).
RETRY_429_SCAN_BACK = 64
# imm16 of the `movw`: (retry_5xx << 8) | retry_429. Upstream has retry_5xx true
# and retry_429 false; this patch flips the low byte to 1.
RETRY_429_FALSE, RETRY_429_TRUE = 0x0100, 0x0101
# `movw $imm16, disp(%base)` with an optional REX prefix (needed when the base
# register is r8..r15). Used only to pick candidate offsets; _mov_imm_store()
# is what actually decodes and validates each one.
RETRY_429_MOVW = re.compile(rb"\x66[\x40-\x4f]?\xc7")
# Same refuse-to-guess cap as site 7: the verified image has exactly one literal.
MAX_RETRY_429_SITES = 1

# Site 7: the `request_max_retries().min(MAX_REQUEST_MAX_RETRIES)` clamp, which
# has its own shape in the same function as the site-6 literal. The Option
# discriminant is already in a register here (site 3's form compares it in
# memory), so a site is confirmed by linking its result register to the
# `max_attempts` slot of that literal, not by a fixed byte pattern.
REQUEST_CAP_WINDOW = 0x800   # how far the clamp may sit from the site-6 literal

# 32-bit GP register names by 4-bit register number (for readable logs).
REG_NAMES = ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi",
             "r8d", "r9d", "r10d", "r11d", "r12d", "r13d", "r14d", "r15d")


def reg_name(reg: int) -> str:
    return REG_NAMES[reg] if 0 <= reg < len(REG_NAMES) else f"reg{reg}"

MS_MIN, MS_MAX = 1, 86_400_000  # 1ms .. 24h; also keeps the imm32 non-negative
REGION_MIN, REGION_MAX = 5, 256  # sanity bounds on the span we overwrite

# Fixed policy (no longer CLI-configurable): retry interval and stream cap.
RETRY_MS = 1000          # fixed retry interval in milliseconds
STREAM_MAX_RETRIES = 9999  # raise the stream_max_retries hard cap (default 100) to this
REQUEST_MAX_RETRIES = 9999  # raise the request_max_retries hard cap (default 100) to this


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def require_platform() -> None:
    """Refuse to run outside Linux/macOS.

    This build only understands ELF/Mach-O images and Unix binary discovery;
    a Windows PE needs the VA translation and MSVC movsd anchor in
    codex-windows.py. Fail fast with that pointer instead of later
    dying on an unrecognized format."""
    if os.name != "posix":
        print(f"ERROR: this is the Linux/macOS build, but the current OS is "
              f"'{sys.platform}' (os.name={os.name!r}).", file=sys.stderr)
        print("Use codex-windows.py on Windows.", file=sys.stderr)
        sys.exit(1)


# ── Format / architecture detection (dispatch + safety gate) ──────────────────
def detect_format(data: bytes) -> str:
    """Return 'elf' | 'macho', or die. Refuses PE (Windows build) and non-x86-64
    binaries, since every byte pattern below is x86-64 ELF/Mach-O specific."""
    if data[:2] == b"MZ":
        die("this is a Windows PE; use codex-windows.py")
    if data[:4] == b"\x7fELF":
        if struct.unpack_from("<H", data, 18)[0] != 0x3E:
            die("ELF is not x86-64 (e_machine != 0x3E); this patch is x86-64 only")
        return "elf"
    if data[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):  # Mach-O LE
        if struct.unpack_from("<I", data, 4)[0] != 0x01000007:
            die("Mach-O is not x86_64 (arm64 uses a different ISA/ABI; unsupported)")
        return "macho"
    if data[:4] in (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
        die("fat/universal Mach-O not supported; extract the x86_64 slice first")
    die("unrecognized binary format (expected ELF or Mach-O)")


def validate_ms(ms: int) -> None:
    if not (MS_MIN <= ms <= MS_MAX):
        die(f"--ms must be between {MS_MIN} and {MS_MAX}")


# ── Offset <-> VA maps (only site 4 needs them) ────────────────────────────────
def load_segments(data: bytes, fmt: str) -> list[tuple[int, int, int]]:
    """[(file_off, vaddr, file_size)] for every mapped segment.

    Sites 1-3 compare RIP operands that land in the same single mapping, so they
    stay in file-offset space. Site 4 has to *dereference* a GOT slot in the
    writable segment, where `p_offset != p_vaddr` (a 0x1000 skew on this build),
    so it needs a real translation in both directions."""
    segs: list[tuple[int, int, int]] = []
    if fmt == "elf":
        phoff = struct.unpack_from("<Q", data, 0x20)[0]
        phentsize = struct.unpack_from("<H", data, 0x36)[0]
        phnum = struct.unpack_from("<H", data, 0x38)[0]
        for i in range(phnum):
            b = phoff + i * phentsize
            if struct.unpack_from("<I", data, b)[0] == 1:  # PT_LOAD
                off, va = struct.unpack_from("<QQ", data, b + 8)
                filesz = struct.unpack_from("<Q", data, b + 32)[0]
                segs.append((off, va, filesz))
    elif fmt == "macho":
        ncmds = struct.unpack_from("<I", data, 16)[0]
        p = 32
        for _ in range(ncmds):
            cmd, cmdsize = struct.unpack_from("<II", data, p)
            if cmdsize <= 0:
                break
            if cmd == 0x19:  # LC_SEGMENT_64
                va, _vmsize, off, filesz = struct.unpack_from("<QQQQ", data, p + 24)
                segs.append((off, va, filesz))
            p += cmdsize
    return segs


def segment_maps(segs: list[tuple[int, int, int]]):
    """(off2va, va2off); each returns None for an address outside every segment."""
    def off2va(off: int):
        for so, sv, ss in segs:
            if so <= off < so + ss:
                return off - so + sv
        return None

    def va2off(va: int):
        for so, sv, ss in segs:
            if sv <= va < sv + ss:
                return va - sv + so
        return None

    return off2va, va2off


def _elf_eh_frame_function_starts(data: bytes) -> list[int] | None:
    """Validated .eh_frame_hdr function entry file offsets.

    None means no supported index; [] means a recognized but malformed/empty
    index (do not fall back to proximity matching in that case).
    Deliberately supports only version=1, eh_frame_ptr=sdata4|pcrel,
    fde_count=udata4, table=sdata4|datarel (01 1b 03 3b).
    Relocations and ASLR are irrelevant: both indices are relative VAs.
    """
    if data[:4] != b"\x7fELF":
        return None
    if len(data) < 64:
        return []
    if data[4:7] != b"\x02\x01\x01":
        return None
    if struct.unpack_from("<H", data, 18)[0] != 62:
        return None
    if struct.unpack_from("<I", data, 20)[0] != 1:
        return []
    phoff = struct.unpack_from("<Q", data, 32)[0]
    ehsize, phentsize, phnum = struct.unpack_from("<HHH", data, 52)
    if ehsize != 64 or phentsize != 56 or phnum == 0xffff:
        return []
    if phoff > len(data) or phnum > (len(data) - phoff) // 56:
        return []

    loads = []
    headers = []
    for i in range(phnum):
        typ, flags, off, va, _pa, filesz, memsz, _align = struct.unpack_from(
            "<IIQQQQQQ", data, phoff + 56 * i)
        if typ not in (1, 0x6474e550):
            continue
        if (filesz > memsz or off > len(data)
                or filesz > len(data) - off or va + memsz > 1 << 64):
            return []
        if typ == 1:
            loads.append((off, va, filesz, flags))
        else:
            headers.append((off, va, filesz))
    if not headers:
        return None
    if len(headers) != 1:
        return []

    def va_to_off(va: int, size: int = 1, executable: bool = False):
        matches = [off + va - start for off, start, length, flags in loads
                   if start <= va and size <= length
                   and va - start <= length - size
                   and (not executable or flags & 1)]
        # Ambiguous PT_LOAD mappings are refused rather than guessed.
        return matches[0] if len(matches) == 1 else None

    hdr, hdr_va, hdr_size = headers[0]
    if hdr_size < 12 or va_to_off(hdr_va, hdr_size) != hdr:
        return []
    if data[hdr:hdr + 4] != b"\x01\x1b\x03\x3b":
        return None
    eh_va = hdr_va + 4 + struct.unpack_from("<i", data, hdr + 4)[0]
    if va_to_off(eh_va, 4) is None:
        return []
    count = struct.unpack_from("<I", data, hdr + 8)[0]
    if count > (hdr_size - 12) // 8:
        return []
    starts = []
    prior_va = -1
    prior_off = -1
    for i in range(count):
        start_delta, fde_delta = struct.unpack_from("<ii", data, hdr + 12 + 8 * i)
        start_va = hdr_va + start_delta
        fde_va = hdr_va + fde_delta
        start_off = va_to_off(start_va, executable=True)
        if (start_off is None or va_to_off(fde_va, 4) is None
                or start_va <= prior_va or start_off <= prior_off):
            return []
        starts.append(start_off)
        prior_va, prior_off = start_va, start_off
    return starts


def elf_backoff_entries(data: bytes, jitter_sites: list[dict],
                        window: int = BACKOFF_FN_WINDOW) -> set[int] | None:
    """Restrict target to the index entry preceding a found jitter region.

    The index's next start is an upper bound, not the decoded FDE end.
    This does not establish a general CFG proof: keep the window and all
    call-site / branch / dataflow checks. Last unbounded index entry is
    conservatively excluded. region_end, if present, must also fit.
    """
    starts = _elf_eh_frame_function_starts(data)
    if starts is None:
        return None
    entries = set()
    for site in jitter_sites:
        region = site["region_start"]
        end = site.get("region_end", region + 1)
        i = bisect.bisect_right(starts, region) - 1
        if (0 <= i < len(starts) - 1 and window > 0
                and starts[i] <= region < starts[i] + window
                and region < end <= starts[i + 1]):
            entries.add(starts[i])
    return entries


# ── Sites 1 & 2: in-place jitter -> fixed interval ────────────────────────────
def _f64_at(data: bytes, off: int):
    """The f64 stored at `off`, or None if `off` is not a full 8 bytes inside the
    image or the value is not finite."""
    if off < 0 or off + 8 > len(data):
        return None
    value = struct.unpack_from("<d", data, off)[0]
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _jitter_bound_loads(data: bytes, lo: int, hi: int) -> list[tuple[int, int, float]]:
    """(instr_off, const_off, value) for every reg-form SSE2 f64 `[rip+disp32]`
    load in [lo,hi) whose constant is a plausible *jitter multiplier bound*.

    This is what replaces hunting for the literal 0.9: `random_range(a..b)`
    multiplies a delay by a factor near 1.0, so any bound must sit in
    JITTER_BOUND_RANGE and cannot be exactly 1.0 (that would be no jitter). The
    specific numbers 0.9/1.1 are codex's current choice, not a requirement --
    if upstream retunes the range this keeps matching."""
    out = []
    for opcode in (0x10, 0x58, 0x5C):  # movsd / addsd / subsd
        for pref in (b"\xf2\x0f" + bytes([opcode]),
                     b"\xf2\x44\x0f" + bytes([opcode])):
            plen, i = len(pref), data.find(pref, lo, hi)
            while i != -1:
                if (data[i + plen] & 0xC7) == 0x05:  # [rip+disp32]
                    disp = struct.unpack_from("<i", data, i + plen + 1)[0]
                    const = (i + plen + 5) + disp
                    value = _f64_at(data, const)
                    if (value is not None and value != 1.0
                            and JITTER_BOUND_MIN < value < JITTER_BOUND_MAX):
                        out.append((i, const, value))
                i = data.find(pref, i + 1, hi)
    return sorted(out)


def _mulsd_len(data: bytes, off: int):
    """Length of a reg-form mulsd at `off` (f2 0f 59 /r = 4, REX.R f2 44 0f 59 = 5),
    or None if `off` is not a reg-form mulsd."""
    if data[off : off + 3] == b"\xf2\x0f\x59" and data[off + 3] >= 0xC0:
        return 4
    if data[off : off + 4] == b"\xf2\x44\x0f\x59" and data[off + 4] >= 0xC0:
        return 5
    return None


def _find_from_millis_tail(data: bytes, start: int, window: int = 384):
    """From `start`, find the inlined Duration::from_millis tail:
        mov rax,<reg>  (48/4c 89 /r, rm=000)  ;  shr rax,3 (48 c1 e8 03)  ;  movabs magic
    The source reg may be r8..r15 (REX.R -> prefix 0x4c); the millis reg is then
    the full 4-bit number. Return (mov_off, millis_reg) or None."""
    i = data.find(b"\x48\xc1\xe8\x03", start, start + window)
    while i != -1:
        mv = i - 3
        # dest is rax (rm=000, no REX.B); source reg field + REX.R -> 4-bit reg.
        is_mov = (data[mv] in (0x48, 0x4c) and data[mv + 1] == 0x89
                  and (data[mv + 2] & 0xC7) == 0xC0)
        has_magic = FROM_MILLIS_MAGIC in data[i + 4 : i + 20]
        if is_mov and has_magic:
            reg = ((data[mv + 2] >> 3) & 7) | (0x8 if data[mv] == 0x4c else 0)
            return mv, reg
        i = data.find(b"\x48\xc1\xe8\x03", i + 1, start + window)
    return None


def _all_from_millis_tails(data: bytes) -> list[tuple[int, int]]:
    """(mov_off, millis_reg) for every inlined Duration::from_millis tail."""
    out, i = [], data.find(FROM_MILLIS_TAIL_SHR)
    while i != -1:
        mv = i - 3
        if mv >= 0 and (data[mv] in (0x48, 0x4C) and data[mv + 1] == 0x89
                        and (data[mv + 2] & 0xC7) == 0xC0
                        and FROM_MILLIS_MAGIC in data[i + 4 : i + 20]):
            reg = ((data[mv + 2] >> 3) & 7) | (0x8 if data[mv] == 0x4C else 0)
            out.append((mv, reg))
        i = data.find(FROM_MILLIS_TAIL_SHR, i + 1)
    return out


def find_jitter_sites(data: bytes) -> list[dict]:
    """Locate every `Duration::from_millis((delay * jitter) as u64)` site.

    Driven by *structure*, not by a known constant. For each inlined from_millis
    tail in the image, require all of:

      * a reg-form `mulsd` before it (the `delay_ms * jitter` product) --
        `from_millis` of a plain computed value has no such multiply;
      * a jitter-range f64 bound loaded upstream of that multiply (any constant
        near 1.0; see _jitter_bound_loads) -- this is the `random_range(a..b)`
        fingerprint without hardcoding 0.9;
      * a span between the two that is plausibly the jitter/base arithmetic.

    On the 0.159.1 image 116 tails exist and exactly 2 satisfy all three, so the
    conjunction -- not any single byte pattern -- is what identifies the sites.
    Each dict: {anchor, region_start, region_end, reg, current}."""
    out = []
    for mv, reg in _all_from_millis_tails(data):
        # Nearest reg-form mulsd before the tail: the `delay_ms = f64 * jitter` op.
        mul = None
        for j in range(mv - 1, max(0, mv - JITTER_SCAN_BACK) - 1, -1):
            n = _mulsd_len(data, j)
            if n is not None:
                mul = (j, n)
                break
        if mul is None:
            continue
        region_start, region_end = mul[0] + mul[1], mv
        if not (REGION_MIN <= region_end - region_start <= REGION_MAX):
            continue
        bounds = _jitter_bound_loads(data, max(0, mv - JITTER_SCAN_BACK), mv)
        if not bounds:
            continue
        anchor, _const, _value = bounds[0]
        kind = ", ".join(f"{v:g}" for _, _, v in bounds)
        # Idempotency: an already-patched site holds `mov <reg>,imm32` at start
        # (with a REX.B prefix when reg is r8..r15).
        current = None
        base = region_start + (1 if reg >= 8 else 0)
        rex_ok = reg < 8 or data[region_start] == 0x41
        if rex_ok and (data[base] & 0xF8) == MOV_R32_IMM and (data[base] & 7) == (reg & 7):
            current = struct.unpack_from("<I", data, base + 1)[0]
        out.append({"anchor": anchor, "kind": kind, "mulsd": mul[0], "tail": mv,
                    "region_start": region_start, "region_end": region_end,
                    "reg": reg, "current": current})
    return out


def make_jitter_patch(reg: int, region_len: int, ms: int) -> bytes:
    """`mov <reg32>, ms` (zero-extended to 64-bit) padded with NOPs.
    Extended registers r8..r15 (reg>=8) need a REX.B (0x41) prefix."""
    validate_ms(ms)
    prefix = b"\x41" if reg >= 8 else b""
    patch = prefix + bytes([MOV_R32_IMM + (reg & 7)]) + struct.pack("<i", ms)
    return patch + b"\x90" * (region_len - len(patch))


# ── Site 4 (Retry-After overriding the fixed backoff) ─────────────────────────
def _call_target_off(data: bytes, off: int, off2va, va2off):
    """File offset of the function a `call` at `off` reaches, or None.

    Handles `e8 rel32` (direct) and `ff 15 disp32` (indirect through a GOT slot,
    which is what this build emits for cross-codegen-unit calls)."""
    if _call_len(data, off) is None:
        return None
    va = off2va(off)
    if va is None:
        return None
    if data[off] == 0xE8:
        target = va + 5 + struct.unpack_from("<i", data, off + 1)[0]
        return va2off(target)
    if data[off] == 0xFF and data[off + 1] == 0x15:
        slot = va2off(va + 6 + struct.unpack_from("<i", data, off + 2)[0])
        if slot is None or slot < 0 or slot + 8 > len(data):
            return None
        return va2off(struct.unpack_from("<Q", data, slot)[0])
    return None


def _call_len(data: bytes, off: int):
    if off < 0 or off >= len(data):
        return None
    if data[off] == 0xE8 and off + 5 <= len(data):
        return 5
    if data[off:off + 2] == b"\xff\x15" and off + 6 <= len(data):
        return 6
    return None


def _backoff_call_after(data: bytes, start: int, in_backoff, limit: int):
    """(call_off, call_len) of the first call to core::util::backoff within
    `limit` bytes of `start`, or None."""
    for i in range(start, min(start + limit, len(data) - 6)):
        n = _call_len(data, i)
        if n is None:
            continue
        target = _call_target_off(data, i, *in_backoff[1:])
        if target is not None and in_backoff[0](target):
            return i, n
    return None


def find_retry_after_sites(data: bytes, jitter_sites: list[dict],
                           off2va, va2off, *,
                           backoff_entries: set[int] | None = None) -> list[dict]:
    """Verified server-delay/backoff selections, in legacy and deadline layouts.

    Legacy: cmp r32,1e9 ; jne <past backoff call> -> NOP the jne.
    0.164:  cmp r32,-1 ; je <backoff args/call>   -> make the je unconditional.

    On ELF, constrain calls to the unwind-table entry containing a jitter site,
    not merely any function within BACKOFF_FN_WINDOW before it. New deadline
    sites require this stronger check; the legacy matcher retains its previous
    fallback for images without a supported ELF unwind table (e.g. Mach-O).
    Explicit entries are useful for synthetic tests with identity VA maps.
    """
    if backoff_entries is None:
        backoff_entries = elf_backoff_entries(data, jitter_sites)
    regions = [s["region_start"] for s in jitter_sites]
    is_backoff = (lambda p: p in backoff_entries) if backoff_entries is not None else (
        lambda p: any(p <= r < p + BACKOFF_FN_WINDOW for r in regions))
    in_backoff = (is_backoff, off2va, va2off)
    out: list[dict] = []
    i = data.find(NICHE_NONE)
    while i != -1:
        # cmp r32,imm32: `81 /7 imm32` (modrm f8..ff) or the eax short form `3d`.
        cmp_off = None
        if i >= 2 and data[i - 2] == 0x81 and 0xF8 <= data[i - 1] <= 0xFF:
            cmp_off, reg = i - 2, data[i - 1] & 7
        elif i >= 1 and data[i - 1] == 0x3D:
            cmp_off, reg = i - 1, 0
        if cmp_off is not None:
            site = _classify_retry_after(data, cmp_off, i + 4, reg, in_backoff)
            if site is not None:
                out.append(site)
        i = data.find(NICHE_NONE, i + 1)
    if backoff_entries:
        out.extend(_find_deadline_retry_after_sites(
            data, in_backoff, _elf_eh_frame_function_starts(data)))
    return sorted(out, key=lambda s: s["branch"])


def _classify_retry_after(data: bytes, cmp_off: int, after_cmp: int, reg: int,
                          in_backoff) -> dict | None:
    """Match the `jne`-or-already-NOPed form at `after_cmp`; None if neither."""
    for length, patched in ((2, False), (6, False), (2, True), (6, True)):
        if after_cmp + length > len(data):
            continue
        blob = data[after_cmp : after_cmp + length]
        if patched:
            if blob != NOP_RUNS[length]:
                continue
            join = None
        elif length == 2:
            if data[after_cmp] != 0x75:
                continue
            join = after_cmp + 2 + struct.unpack_from("<b", data, after_cmp + 1)[0]
        else:
            if data[after_cmp : after_cmp + 2] != b"\x0f\x85":
                continue
            join = after_cmp + 6 + struct.unpack_from("<i", data, after_cmp + 2)[0]
        found = _backoff_call_after(data, after_cmp + length, in_backoff,
                                    SITE4_CALL_WINDOW)
        if found is None:
            continue
        call_off, call_len = found
        call_end = call_off + call_len
        # The branch must jump *over* the backoff call and rejoin just past it.
        if join is not None and not (call_end <= join <= call_end + SITE4_JOIN_WINDOW):
            continue
        return {"cmp": cmp_off, "reg": reg, "branch": after_cmp, "len": length,
                "call": call_off, "patched": patched, "condition": "jne",
                "none": 1_000_000_000, "patch": NOP_RUNS[length]}
    return None


def _stack_mov_end(data: bytes, off: int):
    """End of a flag-preserving r64 <-> [rsp+disp8/32] MOV, or None.

    0.164 schedules up to three such spills/reloads between cmp and je. Do not
    scan blindly across arbitrary instructions: an intervening flag writer
    would make that je test something other than the server-delay niche.
    """
    if off + 4 > len(data) or data[off] not in (0x48, 0x4C):
        return None
    if data[off + 1] not in (0x89, 0x8B) or data[off + 3] != 0x24:
        return None
    modrm = data[off + 2]
    if (modrm & 0xC7) not in (0x44, 0x84):
        return None
    end = off + (5 if modrm >> 6 == 1 else 8)
    return end if end <= len(data) else None


def _backoff_arg_end(data: bytes, off: int):
    """Decode one MOV/load/immediate or self-XOR setting rdi/rsi/rdx.

    These are the System V backoff arguments. Reject branches, stores, calls
    and unknown instructions instead of searching for call bytes inside them.
    """
    if off >= len(data):
        return None
    rex, j = 0, off
    if 0x40 <= data[j] <= 0x4F:
        rex, j = data[j], j + 1
    if j >= len(data):
        return None
    op = data[j]
    args = (2, 6, 7)
    if 0xB8 <= op <= 0xBF and not rex & 8:
        reg = (op & 7) | ((rex & 1) << 3)
        return j + 5 if reg in args and j + 5 <= len(data) else None
    if j + 1 >= len(data):
        return None
    modrm = data[j + 1]
    reg = ((modrm >> 3) & 7) | (((rex >> 2) & 1) << 3)
    rm = (modrm & 7) | ((rex & 1) << 3)
    if modrm >> 6 == 3:
        if ((op == 0x89 and rm in args) or (op == 0x8B and reg in args)
                or (op == 0x31 and rm == reg and rm in args)):
            return j + 2
    elif op == 0x8B and reg in args:
        mem = _mem_operand(data, j + 1, rex)
        if mem is not None:
            return mem["end"]
    return None


def _deadline_backoff_call(data: bytes, start: int, in_backoff):
    """Walk only argument setup, ending at a verified backoff call."""
    i = start
    while i < min(start + SITE4_CALL_WINDOW, len(data)):
        n = _call_len(data, i)
        if n is not None:
            target = _call_target_off(data, i, *in_backoff[1:])
            return (i, n) if target is not None and in_backoff[0](target) else None
        end = _backoff_arg_end(data, i)
        if end is None:
            return None
        i = end
    return None


def _rsp_operand(data: bytes, off: int, reg: int):
    """Plain [rsp+disp8/32] ModR/M+SIB operand with a specified reg field."""
    if off + 3 > len(data) or data[off + 1] != 0x24:
        return None
    mod, low = data[off] >> 6, data[off] & 0x3F
    if mod not in (1, 2) or low != ((reg << 3) | 4):
        return None
    end = off + (3 if mod == 1 else 6)
    if end > len(data):
        return None
    return end, int.from_bytes(data[off + 2:end], "little", signed=True)


def _remaining_delay_end(data: bytes, off: int):
    """End of the Option<remaining Duration> tail selecting rax/edx.

    xor eax,eax ; cmp byte[rsp+D],0 ; mov edx,[rsp+D+16] ;
    cmovne edx,eax ; cmove rax,[rsp+D+8]. All relative stack slots must agree.
    """
    if data[off:off + 3] != b"\x31\xc0\x80":
        return None
    flag = _rsp_operand(data, off + 3, 7)
    if flag is None:
        return None
    i, disp = flag
    if data[i:i + 2] != b"\x00\x8b":
        return None
    nanos = _rsp_operand(data, i + 2, 2)
    if nanos is None or nanos[1] != disp + 16:
        return None
    i = nanos[0]
    if data[i:i + 6] != b"\x0f\x45\xd0\x48\x0f\x44":
        return None
    secs = _rsp_operand(data, i + 6, 0)
    return secs[0] if secs is not None and secs[1] == disp + 8 else None


def _some_delay_tail_ok(data: bytes, some: int, exit_off: int) -> bool:
    # A precomputed delay can be a single stack reload followed by the jump.
    if data[some:some + 2] == b"\x48\x8b":
        secs = _rsp_operand(data, some + 2, 0)
        if secs is not None and secs[0] == exit_off:
            return True
    # Otherwise require the decoded remaining-delay result, not raw EB/E9
    # bytes that might occur inside an immediate or memory displacement.
    return any(_remaining_delay_end(data, off) == exit_off
               for off in range(max(some, exit_off - 40), exit_off))


def _some_arm_skips_backoff(data: bytes, some: int, backoff: int, call_end: int,
                            caller_end: int) -> bool:
    """Some must jump past the call, or return with the same stack epilogue.

    The second form is the shared CodexErr::retry_delay function. Do NOT touch
    its separate error-kind checks or the no-fallback RetryLimit arm.
    """
    for length, opcode in ((2, 0xEB), (5, 0xE9)):
        off = backoff - length
        if off >= some and data[off] == opcode:
            join = backoff + int.from_bytes(data[off + 1:backoff],
                                            "little", signed=True)
            if (call_end <= join <= call_end + SITE4_JOIN_WINDOW and join < caller_end
                    and _some_delay_tail_ok(data, some, off)):
                return True
    epilogue = data[backoff - 5:backoff]
    return (backoff - 5 >= some and len(epilogue) == 5
            and epilogue[:3] == b"\x48\x83\xc4" and 0 < epilogue[3] < 0x80
            and epilogue[4] == 0xC3 and call_end + 5 <= caller_end
            and data[call_end:call_end + 5] == epilogue
            and _some_delay_tail_ok(data, some, backoff - 5))


def _find_deadline_retry_after_sites(data: bytes, in_backoff,
                                    function_starts: list[int] | None = None) -> list[dict]:
    """0.164 Option<Instant> None (-1): force the existing backoff arm.

    JE rel8 becomes JMP rel8; JE rel32 becomes NOP + JMP rel32. The latter
    keeps the same instruction end, so its relative displacement is unchanged.
    Both patched forms retain enough structure for an idempotent re-read.
    """
    out = []
    for m in SITE4_DEADLINE_CMP.finditer(data):
        cmp_off, after_cmp = m.span()
        # Never accept the middle of a 16/64-bit compare as a 32-bit niche test.
        if cmp_off and (0x40 <= data[cmp_off - 1] <= 0x4F
                        or data[cmp_off - 1] == 0x66):
            continue
        reg = (data[after_cmp - 2] & 7) | (8 if data[cmp_off] == 0x41 else 0)
        branch = after_cmp
        while True:
            end = _stack_mov_end(data, branch)
            if end is None or end > after_cmp + SITE4_MOV_WINDOW:
                break
            branch = end
        head = data[branch:branch + 2]
        if head[:1] in (b"\x74", b"\xeb"):
            length, patched = 2, head[:1] == b"\xeb"
            disp_off = branch + 1
            prefix = b"\xeb"
        elif head in (b"\x0f\x84", b"\x90\xe9"):
            length, patched = 6, head == b"\x90\xe9"
            disp_off = branch + 2
            prefix = b"\x90\xe9"
        else:
            continue
        some = branch + length
        if some > len(data):
            continue
        disp = data[disp_off:some]
        backoff = some + int.from_bytes(disp, "little", signed=True)
        if not (some < backoff <= min(some + SITE4_SOME_WINDOW, len(data) - 1)):
            continue
        found = _deadline_backoff_call(data, backoff, in_backoff)
        if found is None:
            continue
        call, call_len = found
        caller_end = len(data)
        if function_starts is not None:
            caller = bisect.bisect_right(function_starts, cmp_off) - 1
            if not (0 <= caller < len(function_starts) - 1
                    and call + call_len < function_starts[caller + 1]):
                continue
            caller_end = function_starts[caller + 1]
        if not _some_arm_skips_backoff(data, some, backoff, call + call_len, caller_end):
            continue
        out.append({"cmp": cmp_off, "reg": reg, "branch": branch, "len": length,
                    "call": call, "patched": patched, "condition": "je",
                    "none": 0xFFFF_FFFF, "patch": prefix + disp})
    return out


# ── Site 5 (unbounded connection-retry ladder: 5s doubling to 60s) ────────────
def _conn_tail_ok(data: bytes, off: int, secs_reg: int, nanos_reg: int) -> bool:
    """True when `off` holds the tail that identifies the delay load:

        mov [<base>+disp32], <secs64>    ; spill the Duration into the
        mov [<base>+disp32], <nanos32>   ;   async poll state machine
        lea <r64>, [rip+disp32]          ; static-level check of the warn! that
                                         ;   reports the wait

    The rip-relative `lea` is load-bearing: unrelated code also spills a
    {secs,nanos} pair into a state machine (a stray `mov eax,60 ; xor ecx,ecx`
    reads exactly like an already-patched site), and that code continues with
    base-relative `lea`s instead. It is a necessary condition only -- what
    actually confirms the site is _has_ladder_near(), below."""
    if data[off : off + 2] != b"\x48\x89":
        return False
    m1 = data[off + 2]
    if (m1 >> 6) != 0b10 or ((m1 >> 3) & 7) != secs_reg or (m1 & 7) == 0b100:
        return False
    q = off + 7  # 48 89 <modrm> <disp32>
    if data[q] != 0x89:
        return False
    m2 = data[q + 1]
    if not ((m2 >> 6) == 0b10 and ((m2 >> 3) & 7) == nanos_reg
            and (m2 & 7) == (m1 & 7)):
        return False
    lea = q + 6  # 89 <modrm> <disp32>
    return (data[lea] in (0x48, 0x4C) and data[lea + 1] == 0x8D
            and (data[lea + 2] & 0xC7) == 0x05)


def _ladder_cap_near(data: bytes, off: int, window: int = LADDER_WINDOW):
    """The `min(MAX_CONNECTION_RETRY_DELAY)` cap guarding this delay, or None.

    What makes a load *the* connection-retry delay is not its byte encoding but
    that the same function also clamps the doubled value:

        cmp <r64>, <cap+1>   ; 48 83 /7 imm8, from `.min(Duration::from_secs(C))`
        setae/cmovbe ...     ; on u64 seconds

    A bare `cmp r64, imm8` is far too common to mean anything (a tracing-level
    check 30 bytes away encodes as `cmp rax,3`), so the *pair* is required: the
    saturating u64 `.min()` emits adjacent compares against `cap+1` and `cap`,
    separated only by the setae/setb that captures the first result. Requiring
    that shape is what upgrades site 5 from a byte-pattern guess to a semantic
    match: on the 0.153.4 image both load copies have such a pair in range, and
    the six look-alike `mov eax,60 ; xor ecx,ecx` decoys have none."""
    lo, hi = max(0, off - window), min(len(data) - 4, off + window)
    best = None
    for i in range(lo, hi):
        if data[i] != 0x48 or data[i + 1] != 0x83 or (data[i + 2] & 0xF8) != 0xF8:
            continue
        imm = data[i + 3]
        if not (LADDER_CAP_MIN < imm <= LADDER_CAP_MAX):
            continue
        # The companion `cmp r64, imm-1` follows within a few bytes (the setCC
        # that consumes the first compare sits between them).
        cap = imm - 1
        paired = any(data[j] == 0x48 and data[j + 1] == 0x83
                     and (data[j + 2] & 0xF8) == 0xF8 and data[j + 3] == cap
                     for j in range(i + 4, min(i + 4 + LADDER_PAIR_GAP, len(data) - 4)))
        if paired and (best is None or abs(i - off) < abs(best[0] - off)):
            best = (i, cap)
    return best


def find_conn_delay_sites(data: bytes) -> list[dict]:
    """Sites of `let retry_delay = retry_state.connection_retry_delay;`.

        mov <secs64>, [<base>+0x10]     (48 8b /r, mod=01, disp8=0x10)
        mov <nanos32>,[<base>+0x18]     (8b /r,    mod=01, disp8=0x18)

    The absent REX means both destination registers are already < 8, which is
    what lets the 7-byte replacement fit exactly. A candidate is accepted only
    when _conn_tail_ok() *and* _ladder_cap_near() agree -- the latter is the
    semantic check (this delay is the one clamped by the doubling ladder), the
    former only proves the shape. Also recognizes the patched form so --dry-run
    reports the current value instead of "not found"."""
    out: list[dict] = []
    i = data.find(b"\x48\x8b")
    while i != -1:
        m1 = data[i + 2]
        if (m1 >> 6) == 0b01 and (m1 & 7) != 0b100 and data[i + 3] == 0x10:
            secs, base = (m1 >> 3) & 7, m1 & 7
            m2 = data[i + 5]
            if (data[i + 4] == 0x8B and (m2 >> 6) == 0b01 and (m2 & 7) == base
                    and data[i + 6] == 0x18 and base != secs
                    and _conn_tail_ok(data, i + 7, secs, (m2 >> 3) & 7)):
                ladder = _ladder_cap_near(data, i)
                if ladder is not None:
                    out.append({"off": i, "secs_reg": secs,
                                "nanos_reg": (m2 >> 3) & 7, "base_reg": base,
                                "ladder": ladder, "current": None})
        i = data.find(b"\x48\x8b", i + 1)
    out.extend(_find_patched_conn_delay_sites(data))
    return sorted(out, key=lambda s: s["off"])


def _find_patched_conn_delay_sites(data: bytes) -> list[dict]:
    """Already-patched form: `mov <secs32>,imm32 ; xor <nanos32>,<nanos32>`."""
    out: list[dict] = []
    i = data.find(b"\x31", 5)
    while i != -1:
        modrm = data[i + 1]
        reg = (modrm >> 3) & 7
        if modrm == (0xC0 | (reg << 3) | reg) and 0xB8 <= data[i - 5] <= 0xBF:
            secs = data[i - 5] - MOV_R32_IMM
            ladder = _ladder_cap_near(data, i - 5)
            if ladder is not None and _conn_tail_ok(data, i + 2, secs, reg):
                out.append({"off": i - 5, "secs_reg": secs, "nanos_reg": reg,
                            "base_reg": None, "ladder": ladder,
                            "current": struct.unpack_from("<I", data, i - 4)[0]})
        i = data.find(b"\x31", i + 1)
    return out


def make_conn_delay_patch(secs_reg: int, nanos_reg: int, secs: int) -> bytes:
    """`mov <secs32>, secs ; xor <nanos32>, <nanos32>` -- exactly 7 bytes, the
    length of the two loads it replaces, so nothing downstream shifts."""
    if not (0 <= secs_reg < 8 and 0 <= nanos_reg < 8):
        die("site 5 needs both registers below r8 to fit in 7 bytes")
    patch = (bytes([MOV_R32_IMM + secs_reg]) + struct.pack("<I", secs)
             + bytes([0x31, 0xC0 | (nanos_reg << 3) | nanos_reg]))
    assert len(patch) == 7, patch.hex(" ")
    return patch


def conn_delay_secs(ms: int) -> int:
    """Site 5 stores a whole-second Duration, so round RETRY_MS up to >= 1s."""
    return max(1, round(ms / 1000))


# ── Site 3 (stream_max_retries cap) ───────────────────────────────────────────
def find_stream_cap_sites(data: bytes) -> list[dict]:
    """Sites of the inlined `stream_max_retries.unwrap_or(D).min(CAP)`.

        mov    edx, <CAP>        ; the .min() bound -- what we rewrite
        cmovb  rdx, rcx          ; take the configured value when it is smaller
        cmp    byte [rax+0x10],0 ; Option discriminant: is it Some?
        mov    eax, <D>          ; unwrap_or default
        cmovne rax, rdx          ; Some -> the clamped value

    Both immediates are read out rather than baked into the pattern, so a
    retuned default (currently 5) or cap (currently 100) still matches. Each
    dict: {off, cap, default}."""
    sites: list[dict] = []
    i = data.find(STREAM_CAP_MID)
    while i != -1:
        mov_cap = i - 5
        after = i + len(STREAM_CAP_MID)
        if (mov_cap >= 0 and data[mov_cap] == 0xBA          # mov edx, imm32
                and data[after] == 0xB8                      # mov eax, imm32
                and data[after + 5 : after + 9] == STREAM_CAP_CMOVNE):
            sites.append({
                "off": mov_cap,
                "cap": struct.unpack_from("<I", data, mov_cap + 1)[0],
                "default": struct.unpack_from("<I", data, after + 1)[0],
            })
        i = data.find(STREAM_CAP_MID, i + 1)
    return sites


def current_stream_caps(data: bytes) -> list[int]:
    return sorted({s["cap"] for s in find_stream_cap_sites(data)})


# ── Site 6: HTTP-layer 429 retryability (ApiRetryConfig.retry_429) ────────────
def _mem_operand(data: bytes, j: int, rex: int):
    """ModR/M memory operand at `j`. Rejects register-direct, SIB, RIP-relative.

    Returns {reg, base, disp, end} where `reg` is the ModR/M reg field (REX.R
    extended) and `end` is the first byte after the displacement."""
    n = len(data)
    if j >= n:
        return None
    modrm = data[j]
    if (modrm & 0xC0) == 0xC0:
        return None
    mod, rm = modrm >> 6, modrm & 7
    if rm == 0b100:                        # SIB: base is not a plain register
        return None
    if mod == 0 and rm == 0b101:           # RIP-relative
        return None
    disp_len = 0 if mod == 0 else 1 if mod == 1 else 4
    end = j + 1 + disp_len
    if end > n:
        return None
    disp = 0 if disp_len == 0 else int.from_bytes(
        data[j + 1:end], "little", signed=True)
    return {
        "reg": ((modrm >> 3) & 7) | (((rex >> 2) & 1) << 3),
        "base": rm | ((rex & 1) << 3),
        "disp": disp,
        "end": end,
    }


def _mov_imm_store(data: bytes, off: int):
    """Decode `mov{b,w,l} $imm, disp(%base)` at `off`, or None.

    Handles the 0x66 operand-size prefix (movw) and an optional REX prefix that
    extends the base register. Only the immediate-to-memory form with reg=000
    (/0) is accepted; register-direct, SIB and RIP-relative encodings are
    rejected. `imm_off` is where the little-endian immediate starts, so a
    1-byte flag inside it can be rewritten in place."""
    n = len(data)
    j = off
    if j >= n:
        return None
    opsize16 = False
    if data[j] == 0x66:
        opsize16 = True
        j += 1
    rex = 0
    if j < n and 0x40 <= data[j] <= 0x4F:
        rex = data[j]
        j += 1
    if j + 1 >= n:
        return None
    op = data[j]
    if opsize16:
        if op != 0xC7:
            return None
        imm_len = 2
    elif op == 0xC7:
        imm_len = 4
    elif op == 0xC6:
        imm_len = 1
    else:
        return None
    if ((data[j + 1] >> 3) & 7) != 0:      # C6/C7 /0
        return None
    mem = _mem_operand(data, j + 1, rex)
    if mem is None:
        return None
    imm_off = mem["end"]
    if imm_off + imm_len > n:
        return None
    imm = int.from_bytes(data[imm_off:imm_off + imm_len], "little")
    return {"off": off, "opsize16": opsize16, "op": op, "imm_len": imm_len,
            "imm": imm, "imm_off": imm_off, "base": mem["base"],
            "disp": mem["disp"], "end": imm_off + imm_len}


def _nanos_store_before(data: bytes, start: int, base: int, disp: int):
    """The nearest `movl $nanos, disp(%base)` before `start`, or None.

    That store is base_delay: `Duration::from_millis(200)` folds to a 32-bit
    nanosecond field. Requiring a *valid* Duration nanosecond count is what
    keeps an arbitrary `movl` store from being mistaken for it."""
    for off in range(start - 1, max(0, start - RETRY_429_SCAN_BACK) - 1, -1):
        st = _mov_imm_store(data, off)
        if (st is not None and st["op"] == 0xC7 and not st["opsize16"]
                and st["imm_len"] == 4 and st["base"] == base
                and st["disp"] == disp and st["end"] <= start
                and 0 < st["imm"] <= RETRY_429_NANOS_MAX):
            return st
    return None


def find_retry_429_sites(data: bytes) -> list[dict]:
    """Sites of `ApiRetryConfig { retry_429: false, retry_5xx: true, .. }`.

    The literal in ModelProviderInfo::to_api_provider compiles to

        movl $<nanos>, N(%r)        ; base_delay = Duration::from_millis(200)
        mov  <r64>, (N+0x8)(%r)     ; max_attempts
        movw $0x0100, (N+0x10)(%r)  ; retry_429 = 0, retry_5xx = 1
        movb $0x1, (N+0x12)(%r)     ; retry_transport = 1

    (rustc reorders the fields; the +0x10/+0x12 offsets from the nanos field,
    rather than any absolute displacement, are what identify it.) All four
    stores are required -- including max_attempts, whose source register is
    recorded so site 7 can link the `.min(100)` clamp without a second decoder.
    Flipping the low immediate byte to 1 turns `retry_429: false` into `true`,
    so the HTTP layer's `RetryOn::should_retry` -- which already lists 429 --
    stops returning a bare 429 as a terminal `RetryLimit`. The already-true
    form is reported so --dry-run stays idempotent. Each dict: {movw, imm,
    imm_off, base, disp, base_delay_nanos, max_attempts_reg, patched}."""
    out: list[dict] = []
    for m in RETRY_429_MOVW.finditer(data):
        i = m.start()
        st = _mov_imm_store(data, i)
        if (st is not None and st["opsize16"]
                and st["imm"] in (RETRY_429_FALSE, RETRY_429_TRUE)):
            base, disp = st["base"], st["disp"]
            # retry_transport is the byte right after the packed pair.
            tr = _mov_imm_store(data, st["end"])
            if (tr is not None and tr["op"] == 0xC6 and tr["base"] == base
                    and tr["disp"] == disp + 2 and tr["imm"] == 1):
                nano = _nanos_store_before(data, i, base, disp - 0x10)
                maxa = _r64_store_before(data, i, base, disp - 0x08)
                if nano is not None and maxa is not None:
                    out.append({"movw": i, "imm": st["imm"],
                                "imm_off": st["imm_off"], "base": base,
                                "disp": disp, "base_delay_nanos": nano["imm"],
                                "max_attempts_reg": maxa["src"],
                                "patched": st["imm"] == RETRY_429_TRUE})
    return out


def _r64_store(data: bytes, off: int):
    """Decode `mov r64, disp(%base)` (`REX.W 89 /r`) at `off`, or None.

    Same mem-operand rules as `_mov_imm_store` (disp8/disp32, REX.B base,
    REX.R source). REX.W is required: without it `89` is a 32-bit store."""
    n = len(data)
    if off >= n:
        return None
    rex, j = 0, off
    if 0x40 <= data[j] <= 0x4F:
        rex = data[j]
        j += 1
    if (rex & 0x08) == 0 or j >= n or data[j] != 0x89:
        return None
    mem = _mem_operand(data, j + 1, rex)
    if mem is None:
        return None
    return {"off": off, "src": mem["reg"], "base": mem["base"],
            "disp": mem["disp"], "end": mem["end"]}


def _r64_store_before(data: bytes, start: int, base: int, disp: int):
    """The nearest `mov <r64>, disp(%base)` before `start`, or None."""
    for off in range(start - 1, max(0, start - RETRY_429_SCAN_BACK) - 1, -1):
        st = _r64_store(data, off)
        if (st is not None and st["base"] == base and st["disp"] == disp
                and st["end"] <= start):
            return st
    return None


# ── Site 7: request_max_retries hard cap (site 3's idea, register form) ──────
def _dec_mov_r32_imm(data: bytes, off: int):
    """`mov r32, imm32` (`b8+rd`, optional REX.B prefix) -> (reg, imm, end)."""
    j = off
    rex_b = False
    if j < len(data) and data[j] == 0x41:
        rex_b, j = True, j + 1
    if j + 5 > len(data) or not (0xB8 <= data[j] <= 0xBF):
        return None
    return ((data[j] - 0xB8) | (0x8 if rex_b else 0),
            int.from_bytes(data[j + 1 : j + 5], "little"), j + 5)


def _dec_cmp_r64_imm8(data: bytes, off: int):
    """`cmp r/m64, imm8` (`REX.W 83 /7`, mod=11) -> (rm, imm8, end)."""
    if off + 4 > len(data) or not (0x48 <= data[off] <= 0x4F):
        return None
    if data[off + 1] != 0x83:
        return None
    modrm = data[off + 2]
    if (modrm & 0xC0) != 0xC0 or ((modrm >> 3) & 7) != 7:
        return None
    return ((modrm & 7) | ((data[off] & 1) << 3), data[off + 3], off + 4)


def _dec_rr(data: bytes, off: int, opcode: int):
    """`REX.W <opcode> /r`, mod=11 -> (reg, rm, end), or None."""
    if off + 3 > len(data) or not (0x48 <= data[off] <= 0x4F):
        return None
    if data[off + 1] != opcode or (data[off + 2] & 0xC0) != 0xC0:
        return None
    rex, modrm = data[off], data[off + 2]
    return (((modrm >> 3) & 7) | (((rex >> 2) & 1) << 3),
            (modrm & 7) | ((rex & 1) << 3), off + 3)


def _dec_cmov(data: bytes, off: int, opcode: int):
    """`REX.W 0f <opcode> /r`, mod=11 (`cmovcc`) -> (reg, rm, end), or None."""
    if off + 4 > len(data) or not (0x48 <= data[off] <= 0x4F):
        return None
    if data[off + 1] != 0x0F or data[off + 2] != opcode:
        return None
    modrm = data[off + 3]
    if (modrm & 0xC0) != 0xC0:
        return None
    rex = data[off]
    return (((modrm >> 3) & 7) | (((rex >> 2) & 1) << 3),
            (modrm & 7) | ((rex & 1) << 3), off + 4)


def _decode_request_cap(data: bytes, off: int):
    """Decode the unpatched `.min(cap)` half at `off`, or None.

    `unwrap_or(default).min(cap)` with the Option discriminant in a register:

        cmp  <val64>, <cap>       83 /7 imm8   (mod=11, reg=7)
        mov  <acc32>, <cap>       b8+acc imm32
        cmovb <acc64>, <val64>    0f 42 /r
        test <opt64>, <opt64>     85 /r  (reg == rm)
        mov  <res32>, <default>   b8+res imm32
        cmovne <res64>, <acc64>   0f 45 /r

    Both immediates of the clamp must agree (they are the same constant), and
    every register has to line up across the pair, so this cannot drift into a
    look-alike. Returned dict: {off, cap, default, val, acc, res, patched}."""
    c = _dec_cmp_r64_imm8(data, off)
    if c is None:
        return None
    val, cap8, p = c
    m1 = _dec_mov_r32_imm(data, p)
    if m1 is None:
        return None
    acc, cap32, p = m1
    cb = _dec_cmov(data, p, 0x42)          # cmovb acc, val
    if cb is None or cb[0] != acc or cb[1] != val:
        return None
    t = _dec_rr(data, cb[2], 0x85)         # test opt, opt
    if t is None or t[0] != t[1]:
        return None
    m2 = _dec_mov_r32_imm(data, t[2])
    if m2 is None:
        return None
    res, default, p = m2
    cn = _dec_cmov(data, p, 0x45)          # cmovne res, acc
    if cn is None or cn[0] != res or cn[1] != acc:
        return None
    if cap8 != cap32:
        return None
    return {"off": off, "len": cb[2] - off, "cap": cap32, "default": default,
            "val": val, "acc": acc, "res": res, "patched": False}


def _decode_request_cap_patched(data: bytes, off: int):
    """Recognize the site-7 rewrite so --dry-run stays idempotent:

        mov <acc32>, REQUEST_MAX_RETRIES ; cmp <val64>,<acc64> ;
        cmovb <acc64>,<val64>   (then NOP padding to the original length)"""
    m = _dec_mov_r32_imm(data, off)
    if m is None or m[1] != REQUEST_MAX_RETRIES:
        return None
    acc, cap, p = m
    cm = _dec_rr(data, p, 0x39)            # cmp val, acc
    if cm is None or cm[0] != acc:
        return None
    val = cm[1]
    cb = _dec_cmov(data, cm[2], 0x42)      # cmovb acc, val
    if cb is None or cb[0] != acc or cb[1] != val:
        return None
    return {"off": off, "cap": cap, "default": None, "val": val,
            "acc": acc, "res": None, "patched": True}


def make_request_cap_patch(acc: int, val: int, region_len: int,
                           cap: int = REQUEST_MAX_RETRIES) -> bytes:
    """`mov <acc32>,cap ; cmp <val64>,<acc64> ; cmovb <acc64>,<val64>` + NOPs.

    Padded to exactly `region_len` -- the length of the `cmp`/`mov`/`cmovb` it
    replaces (13 bytes when the accumulator is below r8, 14 when the original
    `mov` carried a REX.B prefix) -- so nothing downstream shifts."""
    if not (0 <= acc < 16 and 0 <= val < 16):
        die("site 7 register out of range")
    rex = 0x48 | (0x4 if acc >= 8 else 0) | (1 if val >= 8 else 0)
    modrm = 0xC0 | ((acc & 7) << 3) | (val & 7)
    mov = (b"\x41" if acc >= 8 else b"") + bytes([0xB8 + (acc & 7)]) + struct.pack("<I", cap)
    patch = mov + bytes([rex, 0x39, modrm]) + bytes([rex, 0x0F, 0x42, modrm])
    if len(patch) > region_len:
        die(f"site 7 rewrite needs {len(patch)}B but the clamp is {region_len}B")
    return patch + b"\x90" * (region_len - len(patch))


def find_request_cap_sites(data: bytes, literals: list[dict]) -> list[dict]:
    """Sites of the `request_max_retries().min(MAX_REQUEST_MAX_RETRIES)` clamp.

    Confirmed only when the clamp sits in the same function as an
    `ApiRetryConfig` literal *and* its result register is the one that literal
    loads `max_attempts` from -- that is what distinguishes it from the many
    unrelated `unwrap_or(n).min(100)` shapes in the image. Already-patched
    sites are reported so the patch stays idempotent."""
    out: list[dict] = []
    for lit in literals:
        want = lit["max_attempts_reg"]
        lo = max(0, lit["movw"] - REQUEST_CAP_WINDOW)
        hi = min(len(data) - 13, lit["movw"] + REQUEST_CAP_WINDOW)
        for off in range(lo, hi):
            st = _decode_request_cap(data, off) or _decode_request_cap_patched(data, off)
            if st is None or abs(off - lit["movw"]) >= REQUEST_CAP_WINDOW:
                continue
            if not st["patched"] and st["res"] != want:
                continue
            out.append(st)
    return out


# ── Plan ──────────────────────────────────────────────────────────────────────
SITE_LABELS = ("retry.rs::backoff", "util.rs::backoff")


def _as_millis_before(data: bytes, end: int) -> bool:
    """True when `Duration::as_millis` (`imul` by 1000) sits before `end`.

    retry.rs::backoff scales a caller-supplied Duration (`secs * 1000 + nanos /
    1e6`). util.rs::backoff starts from a fixed 200ms and has no such multiply.
    The multiply can sit well above the jitter span (445 bytes on 0.159.1).
    File order is not stable: on 0.159.1 util.rs is the lower address.
    """
    start = max(0, end - AS_MILLIS_SCAN_BACK)
    limit = min(end, len(data))
    i = start
    while i < limit - 6:
        op = i + 1 if 0x40 <= data[i] <= 0x4F else i
        if op + 6 <= limit and data[op] == 0x69:
            if struct.unpack_from("<i", data, op + 2)[0] == 1000:
                return True
        i += 1
    return False


def jitter_label(data: bytes, site: dict) -> str:
    if _as_millis_before(data, site["region_end"]):
        return "retry.rs::backoff"
    return "util.rs::backoff"


def plan(data: bytes, ms: int):
    """Return (edits, report). edits: [(off, bytes)]. report: [(label, site)]."""
    edits, report = [], []
    sites = find_jitter_sites(data)
    if len(sites) < len(SITE_LABELS):
        die(f"expected at least {len(SITE_LABELS)} jittered backoff sites "
            f"(0.9..1.1), found {len(sites)}")
    for s in sites:
        label = jitter_label(data, s)
        patch = make_jitter_patch(s["reg"], s["region_end"] - s["region_start"], ms)
        edits.append((s["region_start"], patch))
        report.append((label, s))
    missing = [name for name in SITE_LABELS if name not in (lab for lab, _ in report)]
    if missing:
        got = ", ".join(lab for lab, _ in report)
        die(f"expected both {' and '.join(SITE_LABELS)}, got {got}; "
            f"missing {', '.join(missing)}")
    return edits, report


# ── Binary discovery ──────────────────────────────────────────────────────────
def is_native_binary(path: Path) -> bool:
    try:
        head = path.read_bytes()[:4]
    except OSError:
        return False
    return (head == b"\x7fELF"
            or head in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
                        b"\xca\xfe\xba\xbe"))


def target_info() -> tuple[str, str, str]:
    key = (platform.system(), platform.machine())
    if key not in PLATFORM_TARGETS:
        die(f"unsupported platform {key[0]} {key[1]}")
    return PLATFORM_TARGETS[key]


def package_root_from_wrapper(wrapper: Path):
    # npm wrapper layouts: .../@openai/codex/bin/codex(.js).
    if wrapper.name in {"codex", "codex.js"} and wrapper.parent.name == "bin":
        for root in (wrapper.parent.parent,
                     wrapper.parent / "node_modules" / "@openai" / "codex"):
            if (root / "package.json").is_file():
                return root
    return None


def binary_from_package_root(root: Path):
    pkg_name, triple, exe = target_info()
    for candidate in (
        root / "node_modules" / pkg_name / "vendor" / triple / "bin" / exe,
        root / "vendor" / triple / "bin" / exe,
    ):
        if candidate.is_file():
            return candidate.resolve()
    return None


def find_binary(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_file():
            die(f"binary not found: {p}")
        return p

    candidates: list[Path] = []
    found = shutil.which("codex")
    if found:
        candidates.append(Path(found).resolve())
    for root in ("/usr/lib/node_modules/@openai/codex",
                 "/usr/local/lib/node_modules/@openai/codex"):
        candidates.append(Path(root))
    try:
        npm_root = subprocess.run(["npm", "root", "-g"], capture_output=True,
                                  text=True, timeout=10)
        if npm_root.returncode == 0 and npm_root.stdout.strip():
            candidates.append(Path(npm_root.stdout.strip()) / "@openai" / "codex")
    except Exception:
        pass

    seen: set[Path] = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        if c.is_file() and is_native_binary(c):
            return c
        root = c if c.is_dir() else package_root_from_wrapper(c)
        if root:
            binary = binary_from_package_root(root)
            if binary:
                return binary

    die("could not find native Codex binary; pass --binary /path/to/codex")


# ── Patch driver ──────────────────────────────────────────────────────────────
def patch_binary(binary: Path, ms: int, max_retries: int, dry_run: bool) -> None:
    validate_ms(ms)
    if not (1 <= max_retries <= 0xFFFF_FFFF):
        die("STREAM_MAX_RETRIES must be between 1 and 4294967295")
    if not (1 <= REQUEST_MAX_RETRIES <= 0xFFFF_FFFF):
        die("REQUEST_MAX_RETRIES must be between 1 and 4294967295")
    data = bytearray(binary.read_bytes())
    fmt = detect_format(bytes(data))  # elf/macho only; PE is rejected

    print(f"Found binary: {binary}")
    print(f"Binary size:  {len(data)} bytes  [{fmt.upper()} x86-64]")

    def fmt_ms(v):
        return "unpatched" if v is None else f"{v}ms"

    # -- Sites 1 & 2: jitter -> fixed interval --------------------------------
    print()
    print("=== Jitter backoff sites (from_millis of a jittered product) ===")
    tails = _all_from_millis_tails(bytes(data))
    print(f"  {len(tails)} inlined Duration::from_millis tail(s) in the image; "
          f"keeping those with a mulsd + jitter-range bound upstream")
    edits, report = plan(data, ms)
    for idx, (label, s) in enumerate(report, 1):
        span = s["region_end"] - s["region_start"]
        print(f"  [{idx}] {label}")
        print(f"        anchor  : jitter bound(s) {s['kind']} loaded "
              f"@ 0x{s['anchor']:x}")
        print(f"        mulsd   : final reg-form @ 0x{s['mulsd']:x}")
        print(f"        millis  : from_millis reads {reg_name(s['reg'])} "
              f"(mov tail @ 0x{s['tail']:x})")
        print(f"        rewrite : [0x{s['region_start']:x},0x{s['region_end']:x}) "
              f"span {span}B -> mov {reg_name(s['reg'])},{ms} + NOPs")
        print(f"        current : {fmt_ms(s['current'])} -> {ms}ms")

    # -- Site 3: stream_max_retries cap ---------------------------------------
    print()
    print("=== stream_max_retries hard cap ===")
    cap_sites = find_stream_cap_sites(data)
    cap_now = current_stream_caps(data)
    if cap_sites:
        locs = ", ".join(f"0x{s['off']:x}" for s in cap_sites)
        defaults = sorted({s["default"] for s in cap_sites})
        print(f"  {len(cap_sites)} inlined site(s): {locs}")
        print(f"  current cap: {cap_now} -> {max_retries}")
        print(f"  unwrap_or default (untouched): {defaults} "
              f"- set stream_max_retries in config.toml to exceed it")
    else:
        print("  NOT FOUND - skipping (cap stays 100)")

    # -- Site 4: Retry-After outranking the fixed backoff ---------------------
    print()
    print("=== Retry-After override (server delay vs backoff) ===")
    off2va, va2off = segment_maps(load_segments(bytes(data), fmt))
    ra_sites = find_retry_after_sites(bytes(data), [s for _, s in report],
                                      off2va, va2off)
    if len(ra_sites) > MAX_RETRY_AFTER_SITES:
        print(f"  {len(ra_sites)} site(s) matched but at most {MAX_RETRY_AFTER_SITES} "
              "are plausible - REFUSING to patch site 4 (signature went loose)")
        ra_sites = []
    for idx, s in enumerate(ra_sites, 1):
        deadline = s["condition"] == "je"
        state = ("already unconditional" if deadline else "already NOPed") \
            if s["patched"] else "live"
        niche = "Option<Instant>" if deadline else "Option<Duration>"
        print(f"  [{idx}] cmp {reg_name(s['reg'])},0x{s['none']:x} @ 0x{s['cmp']:x}"
              f"  ({niche} None niche)")
        print(f"        branch  : {s['len']}-byte {s['condition']} "
              f"@ 0x{s['branch']:x} -> {state}")
        print(f"        verified: call @ 0x{s['call']:x} lands in a patched backoff")
        if deadline:
            target = _call_target_off(data, s["call"], off2va, va2off)
            print(f"        entry   : ELF unwind table confirms 0x{target:x}; "
                  "Some arm skips the call")
        rewrite = "je -> unconditional jmp" if deadline else f"jne -> {s['len']}-byte NOP"
        print(f"        rewrite : {rewrite} "
              f"(server Retry-After ignored, backoff always wins)")
    if not ra_sites:
        print("  NOT FOUND - skipping (a server Retry-After may still override "
              f"the {ms}ms interval)")
    for s in ra_sites:
        if not s["patched"]:
            edits.append((s["branch"], s["patch"]))

    # -- Site 5: unbounded connection-retry ladder (5s -> 60s) ----------------
    secs = conn_delay_secs(ms)
    print()
    print("=== connection retry ladder (unbounded_connection_retries) ===")
    conn_sites = find_conn_delay_sites(bytes(data))
    if len(conn_sites) > MAX_CONN_SITES:
        print(f"  {len(conn_sites)} site(s) matched but at most {MAX_CONN_SITES} "
              f"are plausible - REFUSING to patch site 5 (signature went loose)")
        conn_sites = []
    for idx, s in enumerate(conn_sites, 1):
        ladder_off, cap = s["ladder"]
        cur = f"unpatched (doubling ladder, capped at {cap}s)" \
            if s["current"] is None else f"{s['current']}s fixed"
        print(f"  [{idx}] delay load @ 0x{s['off']:x}  "
              f"secs={reg_name(s['secs_reg'])} nanos={reg_name(s['nanos_reg'])}")
        print(f"        verified: min({cap}s) ladder clamp @ 0x{ladder_off:x}")
        print(f"        current : {cur} -> {secs}s")
        print(f"        rewrite : 7B -> mov {reg_name(s['secs_reg'])},{secs} ; "
              f"xor {reg_name(s['nanos_reg'])},{reg_name(s['nanos_reg'])}")
    if not conn_sites:
        print("  NOT FOUND - skipping (set unbounded_connection_retries = false "
              "in config.toml instead)")
    if secs * 1000 != ms:
        print(f"  NOTE: site 5 stores whole seconds, so {ms}ms is applied as {secs}s")
    for s in conn_sites:
        if s["current"] != secs:
            edits.append((s["off"], make_conn_delay_patch(
                s["secs_reg"], s["nanos_reg"], secs)))

    # -- Site 6: HTTP-layer 429 retryability ----------------------------------
    print()
    print("=== HTTP 429 retryability (ApiRetryConfig.retry_429) ===")
    ra429_sites = find_retry_429_sites(bytes(data))
    if len(ra429_sites) > MAX_RETRY_429_SITES:
        print(f"  {len(ra429_sites)} site(s) matched where at most {MAX_RETRY_429_SITES} "
              f"is plausible - REFUSING to patch site 6 (signature went loose)")
        ra429_sites = []
    for idx, s in enumerate(ra429_sites, 1):
        state = "already true" if s["patched"] else "false"
        print(f"  [{idx}] movw $0x{s['imm']:04x} @ 0x{s['movw']:x}"
              f"  (base_delay {s['base_delay_nanos'] / 1e6:g}ms)")
        print(f"        retry_429 = {state} -> true")
        print(f"        rewrite : imm16 low byte @ 0x{s['imm_off']:x} 00 -> 01 "
              f"(a bare 429 becomes retryable at the HTTP layer)")
    if not ra429_sites:
        print("  NOT FOUND - skipping (a 429 with no Retry-After stays terminal; "
              "only a 429 that carries Retry-After is retried)")
    for s in ra429_sites:
        if not s["patched"]:
            edits.append((s["imm_off"], b"\x01"))

    # -- Site 7: request_max_retries hard cap ---------------------------------
    print()
    print("=== request_max_retries hard cap ===")
    req_sites = find_request_cap_sites(bytes(data), ra429_sites)
    if len(req_sites) > 1:
        print(f"  {len(req_sites)} site(s) matched where at most 1 is plausible - "
              f"REFUSING to patch site 7 (signature went loose)")
        req_sites = []
    for idx, s in enumerate(req_sites, 1):
        cur = f"already {s['cap']}" if s["patched"] else s["cap"]
        dflt = "?" if s["default"] is None else s["default"]
        print(f"  [{idx}] clamp @ 0x{s['off']:x}  "
              f"(val={reg_name(s['val'])} acc={reg_name(s['acc'])})")
        print(f"        linked  : result feeds max_attempts of the "
              f"ApiRetryConfig literal (unwrap_or default {dflt})")
        print(f"        current : {cur} -> {REQUEST_MAX_RETRIES}")
        span = f"{s['len']}B -> " if s.get("len") is not None else ""
        print(f"        rewrite : {span}mov {reg_name(s['acc'])},{REQUEST_MAX_RETRIES} ; "
              f"cmp {reg_name(s['val'])},{reg_name(s['acc'])} ; "
              f"cmovb {reg_name(s['acc'])},{reg_name(s['val'])}")
    if not req_sites:
        print(f"  NOT FOUND - skipping (request_max_retries stays capped at 100)")
    for s in req_sites:
        if not s["patched"]:
            edits.append((s["off"], make_request_cap_patch(
                s["acc"], s["val"], s["len"], REQUEST_MAX_RETRIES)))

    # -- Summary --------------------------------------------------------------
    print()
    print("=== Summary ===")
    print(f"  jitter sites : {len(report)} to patch -> {ms}ms fixed interval")
    print(f"  stream cap   : {len(cap_sites)} site(s) -> {max_retries}")
    ra_status = "ignored at matched sites" if ra_sites else "NOT PATCHED (server delay may win)"
    print(f"  retry-after  : {len(ra_sites)} site(s) -> {ra_status}")
    print(f"  conn ladder  : {len(conn_sites)} site(s) -> {secs}s flat")
    print(f"  retry 429    : {len(ra429_sites)} site(s) -> retryable")
    print(f"  request cap  : {len(req_sites)} site(s) -> {REQUEST_MAX_RETRIES}")
    print(f"  retry interval: {ms}ms")

    if dry_run:
        print()
        print("DRY RUN - no changes were made.")
        return

    backup = binary.with_name(binary.name + ".orig")
    if not backup.exists():
        print()
        print(f"Creating backup: {backup}")
        shutil.copy2(binary, backup)

    for off, patch in edits:
        data[off : off + len(patch)] = patch
    cap_bytes = struct.pack("<I", max_retries)
    for s in cap_sites:
        data[s["off"] + 1 : s["off"] + 5] = cap_bytes

    mode = binary.stat().st_mode  # preserve permissions (esp. the exec bit)
    tmp = binary.with_name(binary.name + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    try:
        os.replace(tmp, binary)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        die("could not replace binary (is codex running, or lacking permission?). "
            "Close codex and retry.")
    print()
    print(f"Patched successfully: {len(report)} jitter + {len(ra_sites)} "
          f"retry-after + {len(conn_sites)} conn-ladder + {len(cap_sites)} cap "
          f"+ {len(ra429_sites)} retry-429 + {len(req_sites)} request-cap "
          f"site(s) ({len(edits)} edit(s) written).")
    print(f"Restore with: {Path(sys.executable).name} {Path(sys.argv[0]).name} --restore")


def restore_binary(binary: Path) -> None:
    backup = binary.with_name(binary.name + ".orig")
    if not backup.is_file():
        die(f"no backup found: {backup}")
    shutil.copy2(backup, binary)
    print(f"Restored {binary} from {backup}")


def self_test() -> None:
    # jitter patch: `mov ecx, 1500` (=0x5dc), NOP-padded to region length
    p = make_jitter_patch(1, 20, 1500)  # reg=1 -> ecx
    assert p[0] == 0xB9 and struct.unpack_from("<i", p, 1)[0] == 1500, p.hex(" ")
    assert len(p) == 20 and p[5:] == b"\x90" * 15
    p2 = make_jitter_patch(7, 12, 1000)  # reg=7 -> edi
    assert p2[0] == 0xBF and struct.unpack_from("<i", p2, 1)[0] == 1000
    # extended reg r8 (>=8) -> REX.B prefix `41 b8 imm32`
    p3 = make_jitter_patch(8, 12, 1000)  # reg=8 -> r8d
    assert p3[:2] == b"\x41\xb8" and struct.unpack_from("<i", p3, 2)[0] == 1000
    assert len(p3) == 12 and p3[6:] == b"\x90" * 6
    # synthetic site: addsd xmm0,[rip->0.9] ; mulsd xmm2,xmm1 ; <clamp> ;
    #                 mov rax,rcx ; shr rax,3 ; movabs magic
    addsd = b"\xf2\x0f\x58\x05\x00\x00\x00\x00"      # disp32 filled in below
    mul = b"\xf2\x0f\x59\xd1"                          # mulsd xmm2,xmm1 (reg-form)
    junk = b"\x66\x0f\x57\xc0" * 5                     # 20 bytes of clamp filler
    tail = b"\x48\x89\xc8\x48\xc1\xe8\x03" + b"\x48\xba" + FROM_MILLIS_MAGIC
    code = addsd + mul + junk + tail
    a_off = 64                                        # addsd position in the buffer
    c09 = 64 + len(code) + 32                          # 0.9 constant, clear of the code
    blob = bytearray(b"\x00" * 64 + code + b"\x00" * 64)
    struct.pack_into("<i", blob, a_off + 4, c09 - (a_off + 8))  # rip disp -> c09
    struct.pack_into("<d", blob, c09, 0.9)
    sites = find_jitter_sites(bytes(blob))
    assert len(sites) == 1, sites
    s = sites[0]
    assert s["reg"] == 1  # mov rax,rcx -> millis reg = ecx
    assert s["region_start"] == a_off + len(addsd) + len(mul)
    assert s["region_end"] == a_off + len(addsd) + len(mul) + len(junk)
    assert s["current"] is None
    # apply and confirm idempotent re-read
    patched = bytearray(blob)
    patch = make_jitter_patch(s["reg"], s["region_end"] - s["region_start"], 2500)
    patched[s["region_start"]:s["region_end"]] = patch
    s2 = find_jitter_sites(bytes(patched))[0]
    assert s2["current"] == 2500, s2
    # Newer codex layout loads the bound with movsd before multiplying.
    movsd_blob = bytearray(blob)
    movsd_blob[a_off + 2] = 0x10  # f2 0f 58 (addsd) -> f2 0f 10 (movsd)
    movsd_sites = find_jitter_sites(bytes(movsd_blob))
    assert len(movsd_sites) == 1 and movsd_sites[0]["anchor"] == a_off
    # A bare bound load is not enough: unrelated loads have no from_millis tail.
    unrelated = bytearray(movsd_blob)
    magic = unrelated.find(FROM_MILLIS_MAGIC)
    unrelated[magic:magic + len(FROM_MILLIS_MAGIC)] = b"\x00" * len(FROM_MILLIS_MAGIC)
    assert find_jitter_sites(bytes(unrelated)) == []
    # Detection is on the jitter *range*, not the literal 0.9: a retuned window
    # still resolves, while a constant that is not a jitter multiplier does not.
    for bound, expected in ((0.75, 1), (1.25, 1), (0.9, 1),
                            (1.0, 0),        # exactly 1.0 == no jitter
                            (200.0, 0)):     # a delay, not a multiplier
        probe = bytearray(blob)
        struct.pack_into("<d", probe, c09, bound)
        assert len(find_jitter_sites(bytes(probe))) == expected, bound
    # No as_millis multiply -> the fixed-200ms util.rs backoff. Planting
    # `imul rax, rax, 1000` is what retry.rs does to a Duration base.
    assert jitter_label(bytes(blob), s) == "util.rs::backoff"
    tagged = bytearray(blob)
    at = s["region_end"] - 7
    tagged[at:at + 7] = b"\x48\x69\xc0\xe8\x03\x00\x00"
    assert jitter_label(bytes(tagged), s) == "retry.rs::backoff"
    # 0.159.1 puts that imul 445 bytes before the tail, past JITTER_SCAN_BACK.
    # The label window has to reach it; an imul past the window stays util.rs.
    end = AS_MILLIS_SCAN_BACK + 32
    far = bytearray(b"\x00" * (end + 16))
    at = end - 445
    far[at:at + 7] = b"\x48\x69\xc0\xe8\x03\x00\x00"
    assert jitter_label(bytes(far), {"region_end": end}) == "retry.rs::backoff"
    outside = bytearray(b"\x00" * (end + 16))
    # opcode (the 0x69) sits one byte before the scan window
    out_at = end - AS_MILLIS_SCAN_BACK - 2
    outside[out_at:out_at + 7] = b"\x48\x69\xc0\xe8\x03\x00\x00"
    assert jitter_label(bytes(outside), {"region_end": end}) == "util.rs::backoff"
    _self_test_site3()
    _self_test_elf_entries()
    _self_test_site4()
    _self_test_site4_deadline()
    _self_test_site5()
    _self_test_site6()
    _self_test_site7()
    # format detection: ELF accepted, PE rejected via die()/SystemExit.
    assert detect_format(b"\x7fELF" + b"\x00" * 14 + b"\x3e\x00" + b"\x00" * 8) == "elf"
    try:
        detect_format(b"MZ" + b"\x00" * 64)
    except SystemExit:
        pass
    else:
        raise AssertionError("PE should be rejected by the Linux build")
    print("self-test OK")


def _self_test_site3() -> None:
    """`unwrap_or(D).min(CAP)` with both immediates read out, not hardcoded."""
    def build(cap: int, default: int) -> bytes:
        return (bytes([0xBA]) + struct.pack("<I", cap)      # mov edx, cap
                + STREAM_CAP_MID                            # cmovb + Option check
                + bytes([0xB8]) + struct.pack("<I", default)  # mov eax, default
                + STREAM_CAP_CMOVNE)                        # cmovne rax,rdx
    at = 0x20
    blob = b"\x00" * at + build(100, 5) + b"\x00" * 0x20
    sites = find_stream_cap_sites(blob)
    assert len(sites) == 1, sites
    assert sites[0] == {"off": at, "cap": 100, "default": 5}, sites[0]
    assert current_stream_caps(blob) == [100]
    # a retuned default and cap must still be found (this is the point)
    other = b"\x00" * at + build(250, 3) + b"\x00" * 0x20
    assert find_stream_cap_sites(other)[0]["cap"] == 250
    assert find_stream_cap_sites(other)[0]["default"] == 3
    # already-patched cap reads back as-is
    done = b"\x00" * at + build(9999, 5) + b"\x00" * 0x20
    assert current_stream_caps(done) == [9999]
    # the fixed middle alone, without the two movs around it, is not a site
    assert find_stream_cap_sites(b"\x00" * at + STREAM_CAP_MID + b"\x00" * 0x20) == []


def _elf_unwind_fixture() -> bytearray:
    """Three PT_LOADs with distinct VA/file skews; header in the third."""
    data = bytearray(0x1000)
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    struct.pack_into("<HHIQQQIHHHHHH", data, 16,
                     3, 62, 1, 0x200000, 64, 0, 0, 64, 56, 4, 0, 0, 0)
    phdrs = [(1, 4, 0, 0x100000, 0, 0x400, 0x400, 0x1000),
             (1, 5, 0x400, 0x200000, 0, 0x400, 0x400, 0x1000),
             (1, 4, 0x800, 0x300000, 0, 0x800, 0x800, 0x1000),
             (0x6474e550, 4, 0x800, 0x300000, 0, 36, 36, 4)]
    for i, fields in enumerate(phdrs):
        struct.pack_into("<IIQQQQQQ", data, 64 + i * 56, *fields)
    data[0x800:0x804] = b"\x01\x1b\x03\x3b"
    struct.pack_into("<iI", data, 0x804, 0x300100 - 0x300004, 3)
    for i, va in enumerate((0x200000, 0x200100, 0x200200)):
        struct.pack_into("<ii", data, 0x80c + i * 8,
                         va - 0x300000, 0x300100 + 16 * i - 0x300000)
    return data


def _self_test_elf_entries() -> None:
    d = _elf_unwind_fixture()
    assert _elf_eh_frame_function_starts(d) == [0x400, 0x500, 0x600]
    s = [{"region_start": 0x520, "region_end": 0x550}]
    assert elf_backoff_entries(d, s) == {0x500}
    assert 0x400 not in elf_backoff_entries(d, s)  # old window falsely accepts
    assert elf_backoff_entries(d, s, window=0x20) == set()  # strict boundary
    assert elf_backoff_entries(d, s, window=0x21) == {0x500}
    assert elf_backoff_entries(d, [{"region_start": 0x500}]) == {0x500}
    assert elf_backoff_entries(d, [{"region_start": 0x3ff}]) == set()
    assert elf_backoff_entries(d, [{"region_start": 0x650}]) == set()
    assert elf_backoff_entries(d, [{"region_start": 0x520,
                                   "region_end": 0x601}]) == set()
    assert elf_backoff_entries(d, []) == set()
    assert elf_backoff_entries(b"MachO", s) is None
    assert elf_backoff_entries(b"\x7fELF", s) == set()
    bad = bytearray(d); bad[0x800] = 2
    assert elf_backoff_entries(bad, s) is None  # unsupported != successful empty
    bad = bytearray(d); struct.pack_into("<I", bad, 64 + 3 * 56, 0)
    assert elf_backoff_entries(bad, s) is None  # no PT_GNU_EH_FRAME
    bad = bytearray(d); struct.pack_into("<I", bad, 0x808, 0xffffffff)
    assert elf_backoff_entries(bad, s) == set()  # oversized count
    bad = bytearray(d); struct.pack_into("<I", bad, 0x808, 0)
    assert elf_backoff_entries(bad, s) == set()  # valid zero count
    bad = bytearray(d); bad[0x814:0x81c] = bad[0x80c:0x814]
    assert elf_backoff_entries(bad, s) == set()  # duplicate starts
    bad = bytearray(d); struct.pack_into("<i", bad, 0x814, 0x1fffff-0x300000)
    assert elf_backoff_entries(bad, s) == set()  # unsorted/nonexecutecutable
    bad = bytearray(d); struct.pack_into("<i", bad, 0x810, 0x7fffffff)
    assert elf_backoff_entries(bad, s) == set()  # unmapped FDE pointer
    bad = bytearray(d); struct.pack_into("<Q", bad, 32, len(d) - 1)
    assert elf_backoff_entries(bad, s) == set()  # truncated phdr table
    bad = bytearray(d); struct.pack_into("<I", bad, 64 + 56 + 4, 4)
    assert elf_backoff_entries(bad, s) == set()  # entry not in executable load


def _self_test_site4() -> None:
    """Synthetic `err.retry_delay().unwrap_or_else(|| backoff(n))` branch:
        cmp ecx,1e9 ; jne +0x0b ; mov rdi,rcx ; call F ; <3B join pad>
    with identity off<->VA maps (a real image needs segment_maps)."""
    ident = lambda x: x
    F = 0x100                                   # pretend backoff entry
    C = 0x400                                   # the cmp
    blob = bytearray(b"\x90" * 0x600)
    body = (b"\x81\xf9" + NICHE_NONE            # cmp ecx, 1_000_000_000
            + b"\x75\x0b"                       # jne  -> C+0x13
            + b"\x48\x89\xcf"                   # mov rdi,rcx
            + b"\xe8" + struct.pack("<i", F - (C + 16)))
    blob[C : C + len(body)] = body
    assert bytes(blob).count(NICHE_NONE) == 1
    jitter = [{"region_start": F + 0x10}]       # sits inside the "function"
    sites = find_retry_after_sites(bytes(blob), jitter, ident, ident)
    assert len(sites) == 1, sites
    s = sites[0]
    assert (s["cmp"], s["branch"], s["len"], s["reg"]) == (C, C + 6, 2, 1), s
    assert s["patched"] is False and s["call"] == C + 11
    # a call that does not land in a patched backoff must be rejected outright
    far = bytearray(blob)
    assert find_retry_after_sites(bytes(far), [{"region_start": F + 0x900}],
                                  ident, ident) == []
    # the indirect form this build actually emits: `call [rip+GOT]`, where the
    # slot holds the backoff entry (identity maps still exercise both hops).
    G = 0x300
    ind = bytearray(b"\x90" * 0x600)
    ind[G : G + 8] = struct.pack("<Q", F)
    indirect = (b"\x81\xf9" + NICHE_NONE      # cmp ecx, 1_000_000_000
                + b"\x75\x0c"                 # jne  -> C+0x14
                + b"\x48\x89\xcf"              # mov rdi,rcx
                + b"\xff\x15" + struct.pack("<i", G - (C + 17)))
    ind[C : C + len(indirect)] = indirect
    via_got = find_retry_after_sites(bytes(ind), jitter, ident, ident)
    assert len(via_got) == 1, via_got
    assert via_got[0]["call"] == C + 11 and via_got[0]["patched"] is False
    # apply and confirm the NOPed form is still recognized (idempotency)
    patched = bytearray(blob)
    patched[s["branch"] : s["branch"] + 2] = s["patch"]
    again = find_retry_after_sites(bytes(patched), jitter, ident, ident)
    assert len(again) == 1 and again[0]["patched"] is True, again
    # Keep the older six-byte JNE and accumulator cmp forms working too.
    for cmp in (b"\x81\xf9" + NICHE_NONE, b"\x3d" + NICHE_NONE):
        long = bytearray(b"\x90" * 0x600)
        branch = C + len(cmp)
        call = branch + 6 + 3
        body = (cmp + b"\x0f\x85" + struct.pack("<i", 11)
                + b"\x48\x89\xcf" + b"\xe8" + struct.pack("<i", F - (call + 5)))
        long[C:C + len(body)] = body
        sites = find_retry_after_sites(bytes(long), jitter, ident, ident)
        assert len(sites) == 1 and sites[0]["len"] == 6 and not sites[0]["patched"]
        long[branch:branch + 6] = sites[0]["patch"]
        assert find_retry_after_sites(bytes(long), jitter, ident, ident)[0]["patched"]
        assert find_retry_after_sites(bytes(long), jitter, ident, ident,
                                      backoff_entries={F - 0x10}) == []


def _self_test_site4_deadline() -> None:
    """0.164 deadline branches: both jump sizes, spills, CFG and idempotency."""
    ident = lambda x: x
    C, F, G = 0x80, 0x300, 0x500
    jitter = [{"region_start": F + 0x20, "region_end": F + 0x40}]
    result = bytes.fromhex("31c0 807c242000 8b542430 0f45d0 480f44442428")
    epilogue = bytes.fromhex("4883c438 c3")
    spills = (bytes.fromhex("4889842450010000 48894c2418 4c897c2450"),
              bytes.fromhex("488b6c2408"))

    def build(*, long=False, gap=b"", cmp=b"\x83\xf9\xff",
              returns=False, copy=False, got=False, long_exit=False):
        d = bytearray(b"\x90" * 0x700)
        branch = C + len(cmp) + len(gap)
        length = 6 if long else 2
        some = branch + length
        body = bytes.fromhex("488b442470") if copy else b"\xe8\x00\x00\x00\x00" + result
        exit_len = 5 if returns or long_exit else 2
        target = some + len(body) + exit_len
        args = bytes.fromhex("4889f7") if returns else bytes.fromhex("488b7b40 8b7348 4c89fa")
        call = target + len(args)
        call_len = 6 if got else 5
        join = call + call_len
        if returns:
            exit_code = epilogue
        elif long_exit:
            exit_code = b"\xe9" + struct.pack("<i", join - target)
        else:
            exit_code = b"\xeb" + struct.pack("<b", join - target)
        branch_code = (b"\x0f\x84" + struct.pack("<i", target - some)) if long else (
            b"\x74" + struct.pack("<b", target - some))
        call_code = (b"\xff\x15" + struct.pack("<i", G - join)) if got else (
            b"\xe8" + struct.pack("<i", F - join))
        code = cmp + gap + branch_code + body + exit_code + args + call_code
        if returns:
            code += epilogue
        d[C:C + len(code)] = code
        struct.pack_into("<Q", d, G, F)
        return d, branch, length, some, target, call

    def find(d, entries=(F,)):
        return find_retry_after_sites(bytes(d), jitter, ident, ident,
                                      backoff_entries=set(entries))

    cases = ({}, {"long": True}, {"gap": spills[0]}, {"gap": spills[1]},
             {"returns": True}, {"got": True}, {"copy": True},
             {"long_exit": True}, {"long": True, "got": True, "gap": spills[0]},
             {"cmp": b"\x41\x83\xf9\xff"})
    for case in cases:
        d, branch, length, some, target, call = build(**case)
        sites = find(d)
        assert len(sites) == 1, (case, sites)
        s = sites[0]
        assert (s["cmp"], s["branch"], s["len"], s["call"]) == (C, branch, length, call)
        assert s["reg"] == (9 if case.get("cmp") else 1) and not s["patched"]
        patch = s["patch"]
        assert len(patch) == length
        assert (patch[:1] == b"\xeb" if length == 2 else patch[:2] == b"\x90\xe9")
        disp = patch[1:] if length == 2 else patch[2:]
        assert some + int.from_bytes(disp, "little", signed=True) == target
        done = bytearray(d)
        done[branch:some] = patch
        assert len(done) == len(d) and done[:branch] == d[:branch] and done[some:] == d[some:]
        again = find(done)
        assert len(again) == 1 and again[0]["patched"] and again[0]["patch"] == patch
        assert find(d, {F - 0x10}) == []  # nearby but wrong function
        assert find(d, set()) == []
        assert find_retry_after_sites(bytes(d), jitter, ident, ident) == []  # no unwind proof

    for case in ({"gap": b"\x31\xc0"},             # clobbers comparison flags
                 {"gap": bytes.fromhex("488b6d08")},  # not a stack MOV
                 {"gap": spills[0] * 2},              # over the allowed gap
                 {"cmp": b"\x48\x83\xf9\xff"},   # 64-bit, not a niche
                 {"cmp": b"\x66\x83\xf9\xff"}):  # 16-bit
        assert find(build(**case)[0]) == [], case

    d, branch, length, some, target, call = build()
    bad = bytearray(d); bad[some + 5 + 10] ^= 1  # nanos slot no longer D+16
    assert find(bad) == []
    bad = bytearray(d); bad[target - 1] = 0      # Some jump no longer skips call
    assert find(bad) == []
    bad = bytearray(d); bad[target:target + 2] = b"\xeb\x00"  # branch, not args
    assert find(bad) == []
    bad = bytearray(d); bad[target:target + 3] = b"\x48\x89\x07"  # store, not args
    assert find(bad) == []
    bad = bytearray(d); struct.pack_into("<i", bad, call + 1, F - 0x10 - (call + 5))
    assert find(bad) == []  # the old proximity check would accept this callee
    bad = bytearray(d); bad[branch + 1] = 0xFE  # backward branch
    assert find(bad) == []
    # EB-like bytes inside a movabs immediate are not a Some-arm terminator.
    bad = bytearray(d)
    fake = b"\x48\xb8" + b"\x00" * 6 + d[target - 2:target]
    bad[some:target] = b"\x90" * (target - some - len(fake)) + fake
    assert find(bad) == []
    # Both epilogues must balance the same stack allocation.
    d, branch, length, some, target, call = build(returns=True)
    bad = bytearray(d); bad[call + 5 + 3] = 0x28
    assert find(bad) == []
    # Crossing indexed caller boundaries is refused, even with a valid callee.
    assert _find_deadline_retry_after_sites(bytes(d), (lambda p: p == F, ident, ident),
                                            [C, target, F, G]) == []
    # Truncated instructions do not raise or match.
    for end in range(len(d[C:call + 5])):
        assert find(d[:C + end]) == []
    assert _call_len(b"\xe8", 0) is None
    assert _call_len(b"\xff\x15", 0) is None
    assert _call_len(b"", 0) is None
    assert _call_len(b"\x90", -1) is None
    assert _call_target_off(b"\xff\x15\xf9\xff\xff\xff", 0, ident, ident) is None
    # Integration: real ELF headers, nonidentity VA maps and automatic entry
    # discovery must authorize only the containing backoff, not its neighbor.
    d, branch, length, some, target, call = build()
    elf = _elf_unwind_fixture()
    at, callee = 0x420, 0x500
    elf[at:at + call + 5 - C] = d[C:call + 5]
    rebased_call = at + call - C
    struct.pack_into("<i", elf, rebased_call + 1, callee - (rebased_call + 5))
    maps = segment_maps(load_segments(elf, "elf"))
    real_jitter = [{"region_start": callee + 0x20, "region_end": callee + 0x40}]
    sites = find_retry_after_sites(bytes(elf), real_jitter, *maps)
    assert len(sites) == 1 and sites[0]["branch"] == at + branch - C
    bad = bytearray(elf)
    struct.pack_into("<i", bad, rebased_call + 1, 0x400 - (rebased_call + 5))
    assert find_retry_after_sites(bytes(bad), real_jitter, *maps) == []
    bad = bytearray(elf); bad[0x800] = 2  # unsupported unwind encoding cannot authorize new form
    assert find_retry_after_sites(bytes(bad), real_jitter, *maps) == []


def _self_test_site5() -> None:
    """Synthetic connection-delay load plus the tail and ladder that confirm it."""
    load = b"\x48\x8b\x48\x10" + b"\x8b\x40\x18"          # rcx <- secs, eax <- nanos
    tail = (b"\x48\x89\x8b" + struct.pack("<i", 0x1b8)     # mov [rbx+0x1b8],rcx
            + b"\x89\x83" + struct.pack("<i", 0x1c0)       # mov [rbx+0x1c0],eax
            + b"\x48\x8d\x05" + struct.pack("<i", 0x40))   # lea rax,[rip+0x40]
    # the saturating min(60s): cmp rax,0x3d ; setae sil ; cmp rax,0x3c
    ladder = (b"\x48\x83\xf8\x3d" + b"\x40\x0f\x93\xc6"
              + b"\x48\x83\xf8\x3c")
    at = 0x40
    blob = bytearray(b"\x00" * at + load + tail + ladder + b"\x00" * 0x40)
    # without the ladder clamp nearby the shape alone is rejected
    assert find_conn_delay_sites(b"\x00" * at + load + tail + b"\x00" * 0x40) == []
    sites = find_conn_delay_sites(bytes(blob))
    assert len(sites) == 1, sites
    s = sites[0]
    assert (s["off"], s["secs_reg"], s["nanos_reg"], s["base_reg"]) == (at, 1, 0, 0), s
    assert s["current"] is None
    assert s["ladder"][1] == 60, s["ladder"]  # cmp rax,0x3d -> a 60s ceiling
    # a retuned ceiling is still recognized (the cap value is read, not assumed)
    retuned = bytearray(b"\x00" * at + load + tail
                        + b"\x48\x83\xf8\x1f" + b"\x40\x0f\x93\xc6"
                        + b"\x48\x83\xf8\x1e" + b"\x00" * 0x40)
    assert find_conn_delay_sites(bytes(retuned))[0]["ladder"][1] == 30
    # a lone cmp (e.g. a tracing-level check) is not a ladder: the pair is required
    lone_cmp = b"\x00" * at + load + tail + b"\x48\x83\xf8\x03" + b"\x00" * 0x40
    assert find_conn_delay_sites(lone_cmp) == []
    patch = make_conn_delay_patch(s["secs_reg"], s["nanos_reg"], 1)
    assert patch == b"\xb9\x01\x00\x00\x00\x31\xc0", patch.hex(" ")
    assert len(patch) == len(load)
    patched = bytearray(blob)
    patched[at : at + len(patch)] = patch
    s2 = find_conn_delay_sites(bytes(patched))
    assert len(s2) == 1 and s2[0]["current"] == 1 and s2[0]["off"] == at, s2
    # the load pair alone is not enough: without the paired tail it is skipped
    lone = bytearray(b"\x00" * at + load + b"\x00" * 0x40)
    assert find_conn_delay_sites(bytes(lone)) == []
    # unrelated `mov eax,60 ; xor ecx,ecx` spilling a Duration reads like an
    # already-patched site until the rip-relative lea is required.
    decoy = (b"\xb8\x3c\x00\x00\x00\x31\xc9"
             + b"\x48\x89\x83" + struct.pack("<i", 0x508)
             + b"\x89\x8b" + struct.pack("<i", 0x510)
             + b"\x4c\x8d\xb3" + struct.pack("<i", 0x518))  # lea r14,[rbx+..]
    assert find_conn_delay_sites(b"\x00" * at + decoy + b"\x00" * 0x40) == []
    assert conn_delay_secs(1000) == 1 and conn_delay_secs(1) == 1
    assert conn_delay_secs(2400) == 2


def _self_test_site6() -> None:
    """Synthetic `ApiRetryConfig` literal: a Duration nanos store, the
    max_attempts store, the packed retry_429/retry_5xx `movw`, and the
    retry_transport `movb` -- in disp8/disp32 forms and with a REX.B base."""
    def build(nano: bytes, maxa: bytes, movw: bytes, movb: bytes) -> bytes:
        at = 0x40
        return b"\x00" * at + nano + maxa + movw + movb + b"\x00" * 0x40

    # disp32 form (what 0.162.1 emits), base rax:
    disp32 = build(
        b"\xc7\x80" + struct.pack("<i", 0x98) + struct.pack("<I", 200_000_000),
        b"\x48\x89\xb0" + struct.pack("<i", 0xA0),
        b"\x66\xc7\x80" + struct.pack("<i", 0xA8) + struct.pack("<H", 0x0100),
        b"\xc6\x80" + struct.pack("<i", 0xAA) + b"\x01",
    )
    sites = find_retry_429_sites(disp32)
    assert len(sites) == 1, sites
    s = sites[0]
    assert (s["imm"], s["base"], s["disp"], s["base_delay_nanos"],
            s["max_attempts_reg"], s["patched"]) == (
                0x0100, 0, 0xA8, 200_000_000, 6, False), s
    # Flip the low immediate byte; the already-true form is recognized.
    patched = bytearray(disp32)
    patched[s["imm_off"]] = 0x01
    again = find_retry_429_sites(bytes(patched))
    assert len(again) == 1 and again[0]["patched"] is True, again
    assert again[0]["imm"] == 0x0101

    # disp32 form with a high base register (REX.B on every store).
    rexb = build(
        b"\x41\xc7\x80" + struct.pack("<i", 0x98) + struct.pack("<I", 200_000_000),
        b"\x49\x89\xb0" + struct.pack("<i", 0xA0),
        b"\x66\x41\xc7\x80" + struct.pack("<i", 0xA8) + struct.pack("<H", 0x0100),
        b"\x41\xc6\x80" + struct.pack("<i", 0xAA) + b"\x01",
    )
    r = find_retry_429_sites(rexb)
    assert len(r) == 1 and r[0]["base"] == 8 and r[0]["max_attempts_reg"] == 6, r
    # and the compact disp8 encoding of the same shape
    compact = build(
        b"\xc7\x40\x18" + struct.pack("<I", 200_000_000),
        b"\x48\x89\x70\x20",
        b"\x66\xc7\x40\x28" + struct.pack("<H", 0x0100),
        b"\xc6\x40\x2a\x01",
    )
    c = find_retry_429_sites(compact)
    assert len(c) == 1 and c[0]["disp"] == 0x28 and c[0]["max_attempts_reg"] == 6, c

    # Negatives: every one of the four stores is load-bearing.
    assert find_retry_429_sites(build(
        b"\xc7\x80" + struct.pack("<i", 0x98) + struct.pack("<I", 200_000_000),
        b"\x48\x89\xb0" + struct.pack("<i", 0xA0),
        b"\x66\xc7\x80" + struct.pack("<i", 0xA8) + struct.pack("<H", 0x0100),
        b"\x00" * 7,                                   # no retry_transport byte
    )) == []
    assert find_retry_429_sites(build(
        b"\xc7\x80" + struct.pack("<i", 0x98) + struct.pack("<I", 200_000_000),
        b"\x00" * 7,                                   # no max_attempts store
        b"\x66\xc7\x80" + struct.pack("<i", 0xA8) + struct.pack("<H", 0x0100),
        b"\xc6\x80" + struct.pack("<i", 0xAA) + b"\x01",
    )) == []
    assert find_retry_429_sites(build(
        b"\xc7\x80" + struct.pack("<i", 0x98) + struct.pack("<I", 200_000_000),
        b"\x48\x89\xb0" + struct.pack("<i", 0xA0),
        b"\x66\xc7\x80" + struct.pack("<i", 0xA8) + struct.pack("<H", 0x0102),
        b"\xc6\x80" + struct.pack("<i", 0xAA) + b"\x01",  # retry_5xx != 1
    )) == []
    assert find_retry_429_sites(build(
        b"\x00" * 11,                                 # no base_delay nanos store
        b"\x48\x89\xb0" + struct.pack("<i", 0xA0),
        b"\x66\xc7\x80" + struct.pack("<i", 0xA8) + struct.pack("<H", 0x0100),
        b"\xc6\x80" + struct.pack("<i", 0xAA) + b"\x01",
    )) == []
    assert find_retry_429_sites(build(
        b"\xc7\x80" + struct.pack("<i", 0x98) + struct.pack("<I", 2_000_000_000),
        b"\x48\x89\xb0" + struct.pack("<i", 0xA0),
        b"\x66\xc7\x80" + struct.pack("<i", 0xA8) + struct.pack("<H", 0x0100),
        b"\xc6\x80" + struct.pack("<i", 0xAA) + b"\x01",  # invalid nanos
    )) == []
    assert _mov_imm_store(b"\xb8\x00\xc2\xeb\x0b", 0) is None  # reg-direct


def _self_test_site7() -> None:
    """Synthetic request-cap clamp linked to a site-6 `ApiRetryConfig` literal."""
    def mov_imm(reg, imm):
        return (b"\x41" if reg >= 8 else b"") + bytes([0xB8 + (reg & 7)]) + struct.pack("<I", imm)

    def rr(dst, src, opcode):
        rex = 0x48 | (1 if src >= 8 else 0) | (0x4 if dst >= 8 else 0)
        return bytes([rex, opcode, 0xC0 | ((dst & 7) << 3) | (src & 7)])

    def cmov(dst, src, op2):
        rex = 0x48 | (1 if src >= 8 else 0) | (0x4 if dst >= 8 else 0)
        return bytes([rex, 0x0F, op2, 0xC0 | ((dst & 7) << 3) | (src & 7)])

    def clamp(acc=0, val=13, res=6, opt=12, cap=100, default=4):
        return (bytes([0x48 | (1 if val >= 8 else 0), 0x83, 0xF8 | (val & 7), cap])
                + mov_imm(acc, cap) + cmov(acc, val, 0x42)
                + rr(opt, opt, 0x85) + mov_imm(res, default) + cmov(res, acc, 0x45))

    def wrap(lit, clamp_bytes=None):
        return b"\x00" * 0x40 + lit + b"\x00" * 0x40 + (clamp() if clamp_bytes is None else clamp_bytes) + b"\x00" * 0x40

    def build(acc, val, res, opt=12, cap=100, default=4, lit_reg=None):
        lit_reg = res if lit_reg is None else lit_reg
        nano = 0x98
        lit = (b"\xc7\x80" + struct.pack("<i", nano) + struct.pack("<I", 200_000_000)
               + bytes([0x48 | (0x4 if lit_reg >= 8 else 0), 0x89,
                        0x80 | ((lit_reg & 7) << 3)])
               + struct.pack("<i", nano + 8)
               + b"\x66\xc7\x80" + struct.pack("<i", nano + 0x10) + struct.pack("<H", 0x0100)
               + b"\xc6\x80" + struct.pack("<i", nano + 0x12) + b"\x01")
        return wrap(lit, clamp(acc, val, res, opt, cap, default))

    # The real register set: value r13, accumulator rax, result rsi (max_attempts).
    blob = build(acc=0, val=13, res=6)
    lits = find_retry_429_sites(blob)
    assert len(lits) == 1, lits
    assert lits[0]["max_attempts_reg"] == 6, lits[0]
    sites = find_request_cap_sites(blob, lits)
    assert len(sites) == 1, sites
    s = sites[0]
    assert (s["cap"], s["default"], s["val"], s["acc"], s["res"],
            s["patched"]) == (100, 4, 13, 0, 6, False), s
    # The rewrite is 13 bytes (same as the three instructions it replaces).
    patch = make_request_cap_patch(s["acc"], s["val"], s["len"], 9999)
    assert patch == bytes.fromhex("b8 0f 27 00 00 49 39 c5 49 0f 42 c5 90"), patch.hex(" ")
    done = bytearray(blob)
    done[s["off"] : s["off"] + len(patch)] = patch
    again = find_request_cap_sites(bytes(done), lits)
    assert len(again) == 1 and again[0]["patched"] is True, again
    # REX.B (`mov r8d, imm32`) keeps the rewrite at 13 bytes without the NOP.
    assert make_request_cap_patch(8, 13, 14, 9999)[:2] == b"\x41\xb8"
    assert len(make_request_cap_patch(8, 13, 14, 9999)) == 14
    assert len(make_request_cap_patch(0, 13, 13, 9999)) == 13
    # A retuned default is read out, not assumed.
    retuned = build(0, 13, 6, default=7)
    assert find_request_cap_sites(retuned, find_retry_429_sites(retuned))[0]["default"] == 7
    # A clamp whose result is not the literal's max_attempts register is not
    # this site -- that is what keeps unrelated `.min(100)` shapes out.
    other = build(acc=0, val=13, res=1, lit_reg=6)
    assert find_request_cap_sites(other, find_retry_429_sites(other)) == []
    # Both immediates of the clamp have to agree (cmp imm8 == mov imm32).
    bad = bytearray(blob)
    off = _find_clamp_off(blob)
    bad[off + 3] = 0x63                                  # cmp says 99, mov says 100
    assert _decode_request_cap(bytes(bad), off) is None

    # Site 7 must follow the same encodings site 6 already accepts.
    rexb_lit = (
        b"\x41\xc7\x80" + struct.pack("<i", 0x98) + struct.pack("<I", 200_000_000)
        + b"\x49\x89\xb0" + struct.pack("<i", 0xA0)
        + b"\x66\x41\xc7\x80" + struct.pack("<i", 0xA8) + struct.pack("<H", 0x0100)
        + b"\x41\xc6\x80" + struct.pack("<i", 0xAA) + b"\x01"
    )
    compact_lit = (
        b"\xc7\x40\x18" + struct.pack("<I", 200_000_000)
        + b"\x48\x89\x70\x20"
        + b"\x66\xc7\x40\x28" + struct.pack("<H", 0x0100)
        + b"\xc6\x40\x2a\x01"
    )
    for name, lit in (("rexb", rexb_lit), ("disp8", compact_lit)):
        extra = wrap(lit)
        found = find_retry_429_sites(extra)
        assert len(found) == 1 and found[0]["max_attempts_reg"] == 6, (name, found)
        linked = find_request_cap_sites(extra, found)
        assert len(linked) == 1 and linked[0]["res"] == 6, (name, linked)


def _find_clamp_off(blob: bytes) -> int:
    for off in range(len(blob) - 13):
        if _decode_request_cap(blob, off) is not None:
            return off
    raise AssertionError("no clamp")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Patch Codex native binary retry backoff interval "
                    "(Linux/macOS ELF/Mach-O, x86-64)")
    parser.add_argument("--binary", help="path to native codex binary")
    parser.add_argument("--dry-run", action="store_true",
                        help="inspect only; do not write or back up")
    parser.add_argument("--restore", action="store_true", help="restore binary from .orig backup")
    parser.add_argument("--self-test", action="store_true", help="run small internal checks")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return

    require_platform()
    binary = find_binary(args.binary)
    if args.restore:
        restore_binary(binary)
    else:
        patch_binary(binary, RETRY_MS, STREAM_MAX_RETRIES, args.dry_run)


if __name__ == "__main__":
    main()
