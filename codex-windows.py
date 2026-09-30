#!/usr/bin/env python3
"""
Patch Codex CLI's retry backoff interval -- Windows build (PE / x86-64).

Verified against codex 0.159.2 (x86_64-pc-windows-msvc). This is the
Windows (PE) build of the codex patcher; for the Linux/macOS ELF/Mach-O build
use codex-linux.py. The jittered backoffs it targets are:

  1. codex-client/src/retry.rs::backoff(base, attempt)  -- generic retry path
       `sleep(backoff(policy.base_delay, attempt+1))`.
  2. core/src/util.rs::backoff(attempt)                 -- stream-reconnect path
       ("Reconnecting N/M" delay when the server sends no explicit retry-after).

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

Windows/PE specifics this build carries that the Linux build does not:
  * RIP-relative operands resolve in VA space, so `next_instr_VA + disp` must be
    compared against the *VA* of the 0.9 constant. PE gives each section an
    independent VirtualAddress vs PointerToRawData (FileAlignment 0x200 !=
    SectionAlignment 0x1000), so `.text`/`.rdata` `VA - file_offset` deltas
    differ. build_pe_maps() translates between the two spaces.
  * MSVC loads the 0.9 lower bound with either `addsd [0.9]` or (one backoff
    path in v0.144.1) `movsd [0.9]`. Since movsd also appears in unrelated math,
    a movsd anchor is accepted only when the full from_millis/mulsd/span chain
    downstream also matches.
  * The millis value may live in an extended register r8..r15 (REX prefixes
    0x4c on the mov, 0x41 on the rewritten `mov r8d,imm32`).

Site 3 (stream_max_retries().min(100) hard cap) is unchanged from prior builds:
the inlined `unwrap_or(5).min(100)` codegen is byte-identical, so the same tail
signature locates every inlined copy and we rewrite the cap immediate to
STREAM_MAX_RETRIES so a large stream_max_retries in config.toml is honored.

On 0.159.2 the image has three such sites, not the two the ELF build has:
retry.rs::backoff (the one that scales a Duration with `as_millis`, an `imul`
by 1000) and two util.rs-shaped copies (fixed 200ms base, no such multiply).
File order is not the label. The `imul` sits ~430 bytes before the from_millis
tail, outside the jitter window, so the label scan is separate.

Sites 1-2 alone do NOT give a fixed interval, because two other delay sources in
core/src/responses_retry.rs outrank or bypass `backoff()` entirely:

  4. `let delay = err.retry_delay().unwrap_or_else(|| backoff(retry_count));`
     A server-supplied Retry-After wins and `backoff()` is never called, so the
     wait becomes whatever the endpoint asked for. `Option<Duration>` is niche-
     encoded (nanos == 1_000_000_000 means None), so the branch reads:
         cmp  <r32>, 0x3b9aca00   ; None?
         jne  <skip the backoff call>
         ...  arg setup ...
         call <core::util::backoff>
     NOP-ing the `jne` makes the fixed backoff unconditional. Accepted only when
     the call provably lands inside a patched backoff -- following an indirect
     `call [rip+GOT]` needs a VA->file-offset map, so build_pe_maps() is used
     for every RIP resolution in this script. On 0.159.2 that is 9 sites
     (3 into util.rs::backoff, 6 into retry.rs::backoff), not the ELF build's 4:
     MSVC left more of these calls outlined.

  5. The `unbounded_connection_retries` ladder (Stable, default ON): on a
     ConnectionFailed the delay is a separate field that starts at
     `Duration::from_secs(5)` and doubles up to `from_secs(60)`. Those are plain
     whole-second constants, not `from_millis(f64 * jitter)`, so the sites 1-2
     anchors cannot see them. The value that is slept is loaded and passed to
     the sleep helper in the Win64 registers the helper actually reads:
         mov  rdx, [<base>+disp32]      ; Duration.secs
         mov  r8d, [<base>+disp32+8]    ; Duration.nanos
         lea  r9,  [rip+<warn static>]
         lea  rcx, [<sleep out>]
         call <sleep>
     disp32 is 0x10b0 in two copies and 0x228 in the third; none of them is the
     ELF disp8 +0x10/+0x18 pair, which is why a literal port of that pattern
     reports NOT FOUND. Rewriting the load (14 bytes: `mov edx,secs` + `xor
     r8d,r8d` + NOPs) pins the sleep. The doubling store later in the function
     is left alone; the next iteration loads the constant again. The same
     function's `.min(60s)` clamp is what distinguishes this from every other
     Duration passed to that helper.

DESIGN RULE -- this patcher only ever changes "how long to wait after a failure
has already been declared". It must never change what counts as a failure, nor
how long detection takes (stream_idle_timeout_ms and friends are config, not
patch targets). Any site added later has to satisfy that; see README.md.

This changes the retry *interval*, not the retry *count*; use config.toml's
stream_max_retries/request_max_retries for the count (site 3 just unclamps the
stream cap so values >100 take effect).

Site 4 makes codex ignore Retry-After, so a genuine quota wall is retried every
second instead of when the server said to come back. That is the point of the
patch, but do not point it at an endpoint that will penalize the hammering.

Usage:
  py codex-windows.py --dry-run      (inspect only)
  py codex-windows.py                (apply the patch)
  py codex-windows.py --restore
  py codex-windows.py --self-test

The retry interval and stream cap are fixed (RETRY_MS / STREAM_MAX_RETRIES).
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import struct
import subprocess
import sys
from pathlib import Path


PLATFORM_TARGETS = {
    ("Windows", "AMD64"): ("@openai/codex-win32-x64", "x86_64-pc-windows-msvc", "codex.exe"),
    ("Windows", "ARM64"): ("@openai/codex-win32-arm64", "aarch64-pc-windows-msvc", "codex.exe"),
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
# (`imul` by 1000). That multiply is not part of the jitter span: on 0.159.2
# it sits ~430 bytes before the tail, outside JITTER_SCAN_BACK, so the label
# window is separate and must not loosen site detection.
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

# Site 5 circuit breaker: 0.159.2 has three MSVC copies of the connection-retry
# sleep (two at field disp 0x10b0, one at 0x228). A much larger count means the
# signature went loose, and site 5 rewrites live instructions, so refuse rather
# than guess.
MAX_CONN_SITES = 4

# Site 5 MSVC shape. The sleep helper takes Duration as (rdx=secs, r8d=nanos)
# plus a rip-relative warn static in r9. The load is 14 bytes because the field
# offset does not fit in disp8. Caps below 30s are other `.min()` pairs that
# happen to sit near an unrelated Duration sleep (traced at 2s/7s/19s).
SLEEP_LEA_R9 = bytes.fromhex("4c8d0d")  # lea r9, [rip+disp32]
SLEEP_LOAD_BACK = 0x48
SLEEP_CALL_AHEAD = 0x18
SLEEP_LOAD_LEN = 14
CONN_SLEEP_CAP_MIN = 30

# Site 5: the delay is confirmed by the `.min(MAX_CONNECTION_RETRY_DELAY)` clamp
# in the same function -- a `cmp r64, imm8` against a whole-second cap. Bounded
# by imm8 encoding; the range covers any plausible retry ceiling (currently 60s).
LADDER_CAP_MIN, LADDER_CAP_MAX = 2, 127
LADDER_WINDOW = 0x1000
# Bytes between the `cmp r64,cap+1` and `cmp r64,cap` halves of the saturating
# min() (on this build a single `setae` sits between them).
LADDER_PAIR_GAP = 12

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


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def require_platform() -> None:
    """Refuse to run outside Windows.

    This build only understands PE images (VA translation, MSVC movsd anchor)
    and Windows binary discovery; a Linux/macOS ELF/Mach-O needs the file-offset
    path in codex-linux.py. Fail fast with that pointer."""
    if os.name != "nt":
        print(f"ERROR: this is the Windows build, but the current OS is "
              f"'{sys.platform}' (os.name={os.name!r}).", file=sys.stderr)
        print("Use codex-linux.py on Linux/macOS.", file=sys.stderr)
        sys.exit(1)


# ── Format / architecture detection (dispatch + safety gate) ──────────────────
def detect_format(data: bytes) -> str:
    """Return 'pe', or die. Refuses ELF/Mach-O (Linux build) and non-x86-64
    PE, since every byte pattern below is x86-64 PE specific."""
    if data[:2] == b"MZ":
        e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
        if data[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
            die("bad PE signature")
        if struct.unpack_from("<H", data, e_lfanew + 4)[0] != 0x8664:
            die("not an x86-64 PE (this patch is x86-64 only)")
        return "pe"
    if data[:4] == b"\x7fELF" or data[:4] in (
            b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
            b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
        die("this is an ELF/Mach-O binary; use codex-linux.py")
    die("unrecognized binary format (expected a Windows PE)")


def build_pe_maps(data: bytes):
    """(off2va, va2off), each returning None for an address outside every section.

    Both directions are needed: resolving a `[rip+disp32]` operand goes offset ->
    VA -> offset, and site 4 additionally dereferences a GOT slot. Because the
    scans run over arbitrary offsets, an unmapped address is a soft None rather
    than a hard failure."""
    e = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, e + 6)[0]
    opt = struct.unpack_from("<H", data, e + 20)[0]
    imgbase = struct.unpack_from("<Q", data, e + 24 + 24)[0]
    sh = e + 24 + opt
    secs = []
    for k in range(nsec):
        o = sh + k * 40
        va = struct.unpack_from("<I", data, o + 12)[0]
        rs = struct.unpack_from("<I", data, o + 16)[0]
        ptr = struct.unpack_from("<I", data, o + 20)[0]
        secs.append((ptr, rs, imgbase + va))

    def off2va(off: int):
        for ptr, rs, va in secs:
            if ptr <= off < ptr + rs:
                return va + (off - ptr)
        return None

    def va2off(v: int):
        for ptr, rs, va in secs:
            if va <= v < va + rs:
                return ptr + (v - va)
        return None

    return off2va, va2off


def validate_ms(ms: int) -> None:
    if not (MS_MIN <= ms <= MS_MAX):
        die(f"--ms must be between {MS_MIN} and {MS_MAX}")


def _rip_target_off(maps, next_instr_off: int, disp: int) -> int:
    """File offset a `[rip+disp32]` operand at `next_instr_off` refers to.

    On PE the operand resolves in VA space, so translate the instruction end to a
    VA, add the displacement, then invert. Section deltas differ, so inverting is
    a scan rather than a subtraction. Returns -1 when either hop leaves the
    image, and callers then simply read no constant there."""
    off2va, va2off = maps
    base = off2va(next_instr_off)
    if base is None:
        return -1
    off = va2off(base + disp)
    return -1 if off is None else off


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


def _jitter_bound_loads(data: bytes, lo: int, hi: int, maps) -> list[tuple[int, int, float]]:
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
                    # PE resolves RIP in VA space; map back to a file offset.
                    const = _rip_target_off(maps, i + plen + 5, disp)
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


def find_jitter_sites(data: bytes, maps=None) -> list[dict]:
    """Locate every `Duration::from_millis((delay * jitter) as u64)` site.

    Driven by *structure*, not by a known constant. For each inlined from_millis
    tail in the image, require all of:

      * a reg-form `mulsd` before it (the `delay_ms * jitter` product) --
        `from_millis` of a plain computed value has no such multiply;
      * a jitter-range f64 bound loaded upstream of that multiply (any constant
        near 1.0; see _jitter_bound_loads) -- this is the `random_range(a..b)`
        fingerprint without hardcoding 0.9;
      * a span between the two that is plausibly the jitter/base arithmetic.

    On the 0.159.2 image 117 tails exist and exactly 3 satisfy all three (the ELF
    build of 0.159.1 has 2), so the conjunction -- not any single byte pattern --
    is what identifies the sites.
    Each dict: {anchor, region_start, region_end, reg, current}.

    `maps` is the (off2va, va2off) pair used to resolve RIP operands; when None
    both default to identity, i.e. resolution in plain file-offset space (used
    only by the self-test's raw buffer)."""
    if maps is None:
        ident = lambda x: x
        maps = (ident, ident)
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
        bounds = _jitter_bound_loads(data, max(0, mv - JITTER_SCAN_BACK), mv, maps)
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
    va = off2va(off)
    if va is None:
        return None
    if data[off] == 0xE8:
        target = va + 5 + struct.unpack_from("<i", data, off + 1)[0]
        return va2off(target)
    if data[off] == 0xFF and data[off + 1] == 0x15:
        slot = va2off(va + 6 + struct.unpack_from("<i", data, off + 2)[0])
        if slot is None or slot + 8 > len(data):
            return None
        return va2off(struct.unpack_from("<Q", data, slot)[0])
    return None


def _call_len(data: bytes, off: int):
    if data[off] == 0xE8:
        return 5
    if data[off] == 0xFF and data[off + 1] == 0x15:
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
                           off2va, va2off) -> list[dict]:
    """Offsets of the `err.retry_delay().unwrap_or_else(|| backoff(n))` branch.

        cmp  <r32>, 0x3b9aca00   ; Option<Duration> niche: 1e9 == None
        jne  <past the backoff call>
        ...  arg setup ...
        call <core::util::backoff>

    A site is accepted only when the call target encloses one of the jitter sites
    patched by sites 1-2, i.e. it provably is `backoff`. That check -- not the
    byte pattern -- is what makes NOP-ing the `jne` safe."""
    entries = [s["region_start"] for s in jitter_sites]
    in_backoff = (lambda p: any(p <= r < p + BACKOFF_FN_WINDOW for r in entries),
                  off2va, va2off)
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
    return out


def _classify_retry_after(data: bytes, cmp_off: int, after_cmp: int, reg: int,
                          in_backoff) -> dict | None:
    """Match the `jne`-or-already-NOPed form at `after_cmp`; None if neither."""
    for length, patched in ((2, False), (6, False), (2, True), (6, True)):
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
        return {"cmp": cmp_off, "reg": reg, "jne": after_cmp, "len": length,
                "call": call_off, "patched": patched}
    return None


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


def _ladder_cap_near(data: bytes, off: int, window: int = LADDER_WINDOW,
                     cap_min: int = LADDER_CAP_MIN):
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
        if not (cap_min < imm <= LADDER_CAP_MAX):
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
    out.extend(_find_sleep_load_sites(data))
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


def _mov_r32_imm_at(data: bytes, off: int):
    """(reg, imm, length) if `off` is `mov r32, imm32`, else None."""
    if off < 0 or off >= len(data):
        return None
    if data[off] == 0x41 and off + 6 <= len(data) and 0xB8 <= data[off + 1] <= 0xBF:
        reg = 8 + (data[off + 1] - MOV_R32_IMM)
        return reg, struct.unpack_from("<I", data, off + 2)[0], 6
    if off + 5 <= len(data) and 0xB8 <= data[off] <= 0xBF:
        return data[off] - MOV_R32_IMM, struct.unpack_from("<I", data, off + 1)[0], 5
    return None


def _xor_r32_at(data: bytes, off: int):
    """(reg, length) if `off` is `xor r32, r32`, else None."""
    if off + 2 <= len(data) and data[off] == 0x31:
        modrm = data[off + 1]
        reg = (modrm >> 3) & 7
        if modrm == (0xC0 | (reg << 3) | reg):
            return reg, 2
    if off + 3 <= len(data) and data[off] == 0x45 and data[off + 1] == 0x31:
        modrm = data[off + 2]
        reg = (modrm >> 3) & 7
        if modrm == (0xC0 | (reg << 3) | reg):
            return 8 + reg, 3
    return None


def _disp32_load_at(data: bytes, off: int):
    """A `mov r, [base+disp32]` (no SIB), or None.

    Returns (wide, dest, base, disp). `wide` is REX.W: the secs half of a
    Duration is a u64, the nanos half a u32."""
    if off < 0 or off + 7 > len(data):
        return None
    rex = data[off]
    if not (0x40 <= rex <= 0x4F) or data[off + 1] != 0x8B:
        return None
    modrm = data[off + 2]
    if (modrm >> 6) != 0b10 or (modrm & 7) == 0b100:
        return None
    disp = struct.unpack_from("<i", data, off + 3)[0]
    dest = ((modrm >> 3) & 7) | (8 if rex & 0x4 else 0)
    base = (modrm & 7) | (8 if rex & 0x1 else 0)
    return bool(rex & 0x8), dest, base, disp


def _sleep_load_pair_at(data: bytes, off: int):
    """The unpatched 14-byte Duration load, or None.

    mov <secs64>, [base+disp32] ; mov <nanos32>, [base+disp32+8]
    """
    secs = _disp32_load_at(data, off)
    nanos = _disp32_load_at(data, off + 7)
    if secs is None or nanos is None:
        return None
    sec_wide, sec_reg, sec_base, sec_disp = secs
    nano_wide, nano_reg, nano_base, nano_disp = nanos
    if not (sec_wide and not nano_wide and sec_base == nano_base
            and nano_disp == sec_disp + 8 and sec_reg != nano_reg):
        return None
    return sec_reg, nano_reg, sec_disp


def _patched_sleep_load_at(data: bytes, off: int):
    """The patched 14-byte form: `mov <secs32>,imm ; xor <nanos32>,<nanos32>`,
    NOP-padded. Returns (secs_reg, nanos_reg, imm) or None."""
    if off < 0 or off + SLEEP_LOAD_LEN > len(data):
        return None
    mov = _mov_r32_imm_at(data, off)
    if mov is None:
        return None
    secs_reg, imm, n = mov
    p = off + n
    end = off + SLEEP_LOAD_LEN
    while p < end and data[p] == 0x90:
        p += 1
    xor = _xor_r32_at(data, p) if p < end else None
    if xor is None:
        return None
    nanos_reg, xn = xor
    p += xn
    if nanos_reg == secs_reg or any(b != 0x90 for b in data[p:end]):
        return None
    return secs_reg, nanos_reg, imm


def _step_preserving(data: bytes, off: int, protected: set[int]):
    """Byte length of a mov/lea at `off` that does not write `protected`, or
    None. The gap between the Duration load and `call sleep` on this build is
    only spills and the two setup leas, so anything else is not this site."""
    if off >= len(data) or data[off] == 0x90:
        return 1 if off < len(data) and data[off] == 0x90 else None
    rex = data[off]
    if not (0x40 <= rex <= 0x4F) or off + 3 > len(data):
        return None
    op = data[off + 1]
    if op not in (0x89, 0x8B, 0x8D):
        return None
    modrm = data[off + 2]
    mod, reg_f, rm = modrm >> 6, (modrm >> 3) & 7, modrm & 7
    reg = reg_f | (8 if rex & 0x4 else 0)
    rm_reg = rm | (8 if rex & 0x1 else 0)
    if mod == 0b11:
        length = 3
    elif rm == 4:
        if off + 4 > len(data):
            return None
        sib = data[off + 3]
        if mod == 0:
            length = 8 if (sib & 7) == 5 else 4
        elif mod == 1:
            length = 5
        else:
            length = 8
    elif mod == 0 and rm == 5:
        length = 7
    elif mod == 0:
        length = 3
    elif mod == 1:
        length = 4
    else:
        length = 7
    if off + length > len(data):
        return None
    writes = reg if op in (0x8B, 0x8D) or mod == 0b11 else None
    if writes in protected:
        return None
    return length


def _call_after_load(data: bytes, load_end: int, lea: int, protected: set[int]):
    """File offset of the `call` that consumes this load, or None.

    The walk has to pass through `lea` (the `lea r9, [rip+warn]`) and stop at
    an `e8` without either Duration register being overwritten."""
    p = load_end
    limit = min(len(data), lea + SLEEP_CALL_AHEAD)
    saw_lea = False
    while p < limit:
        if p == lea:
            saw_lea = True
        if data[p] == 0xE8 and p + 5 <= len(data):
            return p if saw_lea else None
        step = _step_preserving(data, p, protected)
        if step is None:
            return None
        p += step
    return None


def _find_sleep_load_sites(data: bytes) -> list[dict]:
    """MSVC `tokio::time::sleep(connection_retry_delay)` argument loads.

    The ELF finder wants `mov r64,[base+0x10] / mov r32,[base+0x18]`, which this
    image does not emit. Here the field offset needs disp32 and the sleep helper
    reads secs from rdx and nanos from r8d, with the warn static in r9:

        mov  rdx, [base+disp32]
        mov  r8d, [base+disp32+8]
        lea  r9,  [rip+<warn static>]
        lea  rcx, [<out>]
        call <sleep>

    A hit also needs the `.min(MAX_CONNECTION_RETRY_DELAY)` pair in the same
    function. The floor is 30s so a nearby `.min()` of a few seconds cannot
    promote some other slept Duration.
    """
    out: list[dict] = []
    seen: set[int] = set()
    i = data.find(SLEEP_LEA_R9)
    while i != -1:
        for back in range(SLEEP_LOAD_LEN, SLEEP_LOAD_BACK + 1):
            load = i - back
            pair = _sleep_load_pair_at(data, load)
            patched = None if pair is not None else _patched_sleep_load_at(data, load)
            if pair is None and patched is None:
                continue
            if pair is not None:
                secs_reg, nanos_reg, disp = pair
                current = None
            else:
                secs_reg, nanos_reg, current = patched
                disp = None
            call = _call_after_load(data, load + SLEEP_LOAD_LEN, i, {secs_reg, nanos_reg})
            if call is None:
                continue
            ladder = _ladder_cap_near(data, load, cap_min=CONN_SLEEP_CAP_MIN)
            if ladder is None or load in seen:
                continue
            seen.add(load)
            out.append({"off": load, "secs_reg": secs_reg, "nanos_reg": nanos_reg,
                        "base_reg": None, "disp": disp, "ladder": ladder,
                        "span": SLEEP_LOAD_LEN, "current": current})
            break
        i = data.find(SLEEP_LEA_R9, i + 1)
    return out


def make_sleep_load_patch(secs_reg: int, nanos_reg: int, secs: int,
                          span: int = SLEEP_LOAD_LEN) -> bytes:
    """`mov <secs32>, secs ; xor <nanos32>, <nanos32>` padded out to `span`.

    The disp32 load it replaces is 14 bytes, so r8d (the nanos register this
    build actually uses) fits; the 7-byte ELF form cannot.
    """
    if not (0 <= secs_reg < 16 and 0 <= nanos_reg < 16) or secs_reg == nanos_reg:
        die("site 5 sleep load has no two distinct registers")
    mov = (_mov_r32_imm_bytes(secs_reg, secs))
    xor = _xor_r32_bytes(nanos_reg)
    if len(mov) + len(xor) > span:
        die(f"site 5 patch ({len(mov) + len(xor)}B) does not fit in {span}B")
    return mov + xor + b"\x90" * (span - len(mov) - len(xor))


def _mov_r32_imm_bytes(reg: int, imm: int) -> bytes:
    if reg >= 8:
        return bytes([0x41, MOV_R32_IMM + (reg & 7)]) + struct.pack("<I", imm)
    return bytes([MOV_R32_IMM + reg]) + struct.pack("<I", imm)


def _xor_r32_bytes(reg: int) -> bytes:
    if reg >= 8:
        low = reg & 7
        return bytes([0x45, 0x31, 0xC0 | (low << 3) | low])
    return bytes([0x31, 0xC0 | (reg << 3) | reg])


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


# ── Plan ──────────────────────────────────────────────────────────────────────
SITE_LABELS = ("retry.rs::backoff", "util.rs::backoff")


def _as_millis_before(data: bytes, end: int) -> bool:
    """True when `Duration::as_millis` (`imul` by 1000) sits before `end`.

    retry.rs::backoff scales a caller-supplied Duration (`secs * 1000 + nanos /
    1e6`). util.rs::backoff starts from a fixed 200ms and has no such multiply.
    The multiply can sit well above the jitter span (~430 bytes on 0.159.2).
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


def _site4_backoff_name(data: bytes, site: dict, jitter_report, off2va, va2off) -> str:
    """Which patched backoff a site-4 call actually enters, for the dry-run log."""
    target = _call_target_off(data, site["call"], off2va, va2off)
    if target is None:
        return "backoff"
    best = None
    for label, js in jitter_report:
        region = js["region_start"]
        if target <= region < target + BACKOFF_FN_WINDOW:
            dist = region - target
            if best is None or dist < best[0]:
                best = (dist, label)
    return best[1] if best else "backoff"


def plan(data: bytes, ms: int, maps=None):
    """Return (edits, report). edits: [(off, bytes)]. report: [(label, site)]."""
    edits, report = [], []
    sites = find_jitter_sites(data, maps)
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
        head = path.read_bytes()[:2]
    except OSError:
        return False
    return head == b"MZ"


def target_info() -> tuple[str, str, str]:
    key = (platform.system(), platform.machine())
    if key not in PLATFORM_TARGETS:
        die(f"unsupported platform {key[0]} {key[1]}")
    return PLATFORM_TARGETS[key]


def package_root_from_wrapper(wrapper: Path):
    # Windows global shims at .../npm/codex(.cmd|.ps1) next to
    # node_modules/@openai/codex, or a bin/codex(.js) wrapper.
    if wrapper.name in {"codex", "codex.js", "codex.cmd", "codex.ps1"} \
            and wrapper.parent.name in {"bin", "npm"}:
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
    for cmd in ("codex", "codex.cmd", "codex.exe"):
        found = shutil.which(cmd)
        if found:
            candidates.append(Path(found).resolve())
    try:
        npm_root = subprocess.run(["npm", "root", "-g"], capture_output=True,
                                  text=True, timeout=10, shell=True)
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

    die("could not find native Codex binary; pass --binary C:\\path\\to\\codex.exe")


# ── Patch driver ──────────────────────────────────────────────────────────────
def patch_binary(binary: Path, ms: int, max_retries: int, dry_run: bool) -> None:
    validate_ms(ms)
    if not (1 <= max_retries <= 0xFFFF_FFFF):
        die("STREAM_MAX_RETRIES must be between 1 and 4294967295")
    data = bytearray(binary.read_bytes())
    fmt = detect_format(bytes(data))  # pe only; ELF/Mach-O rejected

    print(f"Found binary: {binary}")
    print(f"Binary size:  {len(data)} bytes  [{fmt.upper()} x86-64]")

    # PE resolves RIP-relative operands in VA space; one map pair serves every
    # site (sites 1-2 translate operands, site 4 also dereferences a GOT slot).
    maps = build_pe_maps(bytes(data))

    def fmt_ms(v):
        return "unpatched" if v is None else f"{v}ms"

    # -- Sites 1 & 2: jitter -> fixed interval --------------------------------
    print()
    print("=== Jitter backoff sites (from_millis of a jittered product) ===")
    tails = _all_from_millis_tails(bytes(data))
    print(f"  {len(tails)} inlined Duration::from_millis tail(s) in the image; "
          f"keeping those with a mulsd + jitter-range bound upstream")
    edits, report = plan(data, ms, maps)
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
    print("=== Retry-After override (responses_retry.rs: unwrap_or_else) ===")
    ra_sites = find_retry_after_sites(bytes(data), [s for _, s in report], *maps)
    for idx, s in enumerate(ra_sites, 1):
        state = "already NOPed" if s["patched"] else "live"
        print(f"  [{idx}] cmp {reg_name(s['reg'])},0x3b9aca00 @ 0x{s['cmp']:x}"
              f"  (Option<Duration> None niche)")
        print(f"        branch  : {s['len']}-byte jne @ 0x{s['jne']:x} -> {state}")
        print(f"        verified: call @ 0x{s['call']:x} lands in a patched "
              f"{_site4_backoff_name(bytes(data), s, report, *maps)}")
        print(f"        rewrite : jne -> {s['len']}-byte NOP "
              f"(server Retry-After ignored, backoff always wins)")
    if not ra_sites:
        print("  NOT FOUND - skipping (a server Retry-After will still override "
              f"the {ms}ms interval)")
    for s in ra_sites:
        if not s["patched"]:
            edits.append((s["jne"], NOP_RUNS[s["len"]]))

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
        span = s.get("span", 7)
        print(f"        rewrite : {span}B -> mov {reg_name(s['secs_reg'])},{secs} ; "
              f"xor {reg_name(s['nanos_reg'])},{reg_name(s['nanos_reg'])}")
    if not conn_sites:
        print("  NOT FOUND - skipping (set unbounded_connection_retries = false "
              "in config.toml instead)")
    if secs * 1000 != ms:
        print(f"  NOTE: site 5 stores whole seconds, so {ms}ms is applied as {secs}s")
    for s in conn_sites:
        if s["current"] != secs:
            if s.get("span", 7) == SLEEP_LOAD_LEN:
                patch = make_sleep_load_patch(s["secs_reg"], s["nanos_reg"], secs)
            else:
                patch = make_conn_delay_patch(s["secs_reg"], s["nanos_reg"], secs)
            edits.append((s["off"], patch))

    # -- Summary --------------------------------------------------------------
    print()
    print("=== Summary ===")
    print(f"  jitter sites : {len(report)} to patch -> {ms}ms fixed interval")
    print(f"  stream cap   : {len(cap_sites)} site(s) -> {max_retries}")
    print(f"  retry-after  : {len(ra_sites)} site(s) -> ignored")
    print(f"  conn ladder  : {len(conn_sites)} site(s) -> {secs}s flat")
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

    mode = binary.stat().st_mode  # preserve permissions
    tmp = binary.with_name(binary.name + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    try:
        os.replace(tmp, binary)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        die("could not replace binary (is codex running, or lacking permission?). "
            "Close codex and retry (Windows locks running executables).")
    print()
    print(f"Patched successfully: {len(report)} jitter + {len(ra_sites)} "
          f"retry-after + {len(conn_sites)} conn-ladder + {len(cap_sites)} cap "
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
    # extended reg r8 (>=8) -> REX.B prefix `41 b8 imm32` (as in the MSVC build)
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
    # Newer MSVC layout loads the bound with movsd before multiplying.
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
    at_imul = s["region_end"] - 7
    tagged[at_imul:at_imul + 7] = b"\x48\x69\xc0\xe8\x03\x00\x00"
    assert jitter_label(bytes(tagged), s) == "retry.rs::backoff"
    # 0.159.2 puts that imul ~430 bytes before the tail, past JITTER_SCAN_BACK.
    end = AS_MILLIS_SCAN_BACK + 32
    far = bytearray(b"\x00" * (end + 16))
    at_far = end - 430
    far[at_far:at_far + 7] = b"\x48\x69\xc0\xe8\x03\x00\x00"
    assert jitter_label(bytes(far), {"region_end": end}) == "retry.rs::backoff"
    outside = bytearray(b"\x00" * (end + 16))
    out_at = end - AS_MILLIS_SCAN_BACK - 2
    outside[out_at:out_at + 7] = b"\x48\x69\xc0\xe8\x03\x00\x00"
    assert jitter_label(bytes(outside), {"region_end": end}) == "util.rs::backoff"
    _self_test_pe_maps()
    _self_test_site3()
    _self_test_site4()
    _self_test_site5()
    _self_test_sleep_load()
    # format detection: PE accepted, ELF rejected via die()/SystemExit.
    assert detect_format(b"MZ" + b"\x00" * 0x3a + struct.pack("<I", 0x40)
                         + b"PE\x00\x00" + struct.pack("<H", 0x8664)) == "pe"
    try:
        detect_format(b"\x7fELF" + b"\x00" * 16)
    except SystemExit:
        pass
    else:
        raise AssertionError("ELF should be rejected by the Windows build")
    print("self-test OK")


def _self_test_pe_maps() -> None:
    """build_pe_maps() must invert cleanly on a header whose two sections have
    different `VA - file offset` deltas (the whole reason PE needs translation)."""
    opt = 0xF0
    sh = 0x80 + 24 + opt
    hdr = bytearray(sh + 2 * 40 + 0x40)
    hdr[0:2] = b"MZ"
    struct.pack_into("<I", hdr, 0x3C, 0x80)
    hdr[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", hdr, 0x84, 0x8664)      # Machine
    struct.pack_into("<H", hdr, 0x86, 2)           # NumberOfSections
    struct.pack_into("<H", hdr, 0x94, opt)         # SizeOfOptionalHeader
    struct.pack_into("<Q", hdr, 0x80 + 24 + 24, 0x140000000)  # ImageBase
    # (.text: VA 0x1000 / raw 0x400) and (.rdata: VA 0x20000 / raw 0x1e600)
    for k, (va, raw, size) in enumerate(((0x1000, 0x400, 0x200),
                                         (0x20000, 0x1E600, 0x200))):
        o = sh + k * 40
        struct.pack_into("<I", hdr, o + 12, va)
        struct.pack_into("<I", hdr, o + 16, size)
        struct.pack_into("<I", hdr, o + 20, raw)
    data = bytes(hdr)
    off2va, va2off = build_pe_maps(data)
    for off in (0x400, 0x4FF, 0x1E600, 0x1E7FF):
        assert va2off(off2va(off)) == off, hex(off)
    # a RIP operand must resolve across sections (.text -> .rdata constant)
    assert _rip_target_off((off2va, va2off), 0x410,
                           off2va(0x1E600) - off2va(0x410)) == 0x1E600
    # the two deltas really do differ, so a single mapping would be wrong
    assert off2va(0x400) - 0x400 != off2va(0x1E600) - 0x1E600
    # unmapped addresses are a soft None, not a die()
    assert off2va(0) is None and va2off(0x140000000) is None


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


def _self_test_site4() -> None:
    """Synthetic `err.retry_delay().unwrap_or_else(|| backoff(n))` branch:
        cmp ecx,1e9 ; jne +0x0b ; mov rdi,rcx ; call F ; <3B join pad>
    with identity off<->VA maps (a real image needs build_pe_maps)."""
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
    assert (s["cmp"], s["jne"], s["len"], s["reg"]) == (C, C + 6, 2, 1), s
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
    patched[s["jne"] : s["jne"] + 2] = NOP_RUNS[2]
    again = find_retry_after_sites(bytes(patched), jitter, ident, ident)
    assert len(again) == 1 and again[0]["patched"] is True, again


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


def _self_test_sleep_load() -> None:
    """MSVC sleep-argument load: disp32 Duration into rdx/r8d, then lea r9,[rip]."""
    load = (bytes.fromhex("488b91b0100000")    # mov rdx, [rcx+0x10b0]
            + bytes.fromhex("448b81b8100000"))  # mov r8d, [rcx+0x10b8]
    setup = (bytes.fromhex("4c8d0d00000000")   # lea r9, [rip+0]
             + bytes.fromhex("488d0d00000000")  # lea rcx, [rip+0]
             + b"\xe8\x00\x00\x00\x00")         # call
    # saturating min(60s): cmp rax,0x3d ; setae sil ; cmp rax,0x3c
    ladder = (b"\x48\x83\xf8\x3d" + b"\x40\x0f\x93\xc6" + b"\x48\x83\xf8\x3c")
    at = 0x80
    blob = bytearray(b"\x90" * at + load + setup + b"\x90" * 16 + ladder + b"\x90" * 16)
    sites = [s for s in find_conn_delay_sites(bytes(blob)) if s.get("span") == SLEEP_LOAD_LEN]
    assert len(sites) == 1, sites
    s = sites[0]
    assert (s["off"], s["secs_reg"], s["nanos_reg"], s["current"]) == (at, 2, 8, None), s
    assert s["ladder"][1] == 60, s["ladder"]
    # a short clamp is some other .min(), not the connection-retry ceiling
    short_ladder = b"\x48\x83\xf8\x04" + b"\x40\x0f\x93\xc6" + b"\x48\x83\xf8\x03"
    short = bytearray(b"\x90" * at + load + setup + b"\x90" * 16 + short_ladder + b"\x90" * 16)
    assert [s for s in find_conn_delay_sites(bytes(short)) if s.get("span") == SLEEP_LOAD_LEN] == []
    patch = make_sleep_load_patch(s["secs_reg"], s["nanos_reg"], 1)
    assert len(patch) == SLEEP_LOAD_LEN
    assert patch.startswith(b"\xba\x01\x00\x00\x00") and b"\x45\x31\xc0" in patch
    patched = bytearray(blob)
    patched[at:at + len(patch)] = patch
    again = [s for s in find_conn_delay_sites(bytes(patched)) if s.get("span") == SLEEP_LOAD_LEN]
    assert len(again) == 1 and again[0]["current"] == 1 and again[0]["off"] == at, again


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Patch Codex native binary retry backoff interval "
                    "(Windows PE, x86-64)")
    parser.add_argument("--binary", help="path to native codex.exe binary")
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
