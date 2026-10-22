"""Offline regression check: python3 test_claude_linux.py."""

import contextlib
import io
import re
import runpy
from pathlib import Path


patcher = runpy.run_path(str(Path(__file__).with_name("claude-linux.py")))
# Claude Code 2.1.306: optional base delay and reset fallback inside Math.min.
CURRENT = (
    b'var fe=500,YGo=0.25;function xN(e,r,n=32000,o=Math.random,a=fe){'
    b'let s=Math.round(Ty({attempt:e,baseMs:a,capMs:n,'
    b'jitter:{kind:"proportional",ratio:YGo},random:o}));'
    b'if(r){let i=parseInt(r,10);if(!isNaN(i))return Math.max(i*1000,s)}return s}'
    b'function loop(dn,q,Jr,wmn,r,sr,TQe,Ys,a){let ns;'
    b'ns=Math.min(sr??xN(q,Jr,wmn,r.random),TQe),ns=Math.min(ns,Ys);'
    b'ns=xN(dn+q,Jr,sr,r.random,Ys?a.CLAUDE_CODE_OVERLOADED_RETRY_BASE_DELAY_MS:void 0);'
    b'ns=Math.min(ns,TQe);'
    b'log("tengu_api_retry",{attempt:dn,delayMs:ns})}'
)


def check(source, pin_count):
    fn = patcher["discover_backoff_fn"](source)
    assert fn is not None
    sites = patcher["backoff_sites"](fn)
    sites += patcher["discover_delay_pins"](source, fn["name"])
    assert len(sites) == 3 + pin_count, sites
    assert {s["key"] for s in sites} == {
        "backoff_base", "backoff_pow", "retry_after_backoff",
        *(f"delay_pin_{i}" for i in range(1, pin_count + 1)),
    }
    result = bytearray(source)
    stats = {"applied": 0, "failed": 0}
    for site in sites:
        patcher["apply_site"](result, site, stats)
    assert stats == {"applied": len(sites), "failed": 0}
    assert len(result) == len(source)
    fn = patcher["discover_backoff_fn"](bytes(result))
    assert fn is not None
    done = patcher["backoff_sites"](fn)
    done += patcher["discover_delay_pins"](bytes(result), fn["name"])
    assert len(done) == len(sites)
    assert all(s["state"] == "done" for s in done), done
    return bytes(result)


def check_rate_limit():
    """Resolve the current retry path's constants without touching reused names."""
    source = (b'var WLo=1800000,hLe=20000,GLo=600000;'
              b'var hLe=24;'
              b'function f(Yt){let dr=zLo(Yt),or=dr!==null&&dr<hLe;'
              b'if(!or){let Wr=Math.max(dr??WLo,GLo)}}')
    sites = patcher["discover_rate_limit"](source, b"zLo")
    assert len(sites) == 3, sites
    result = bytearray(source)
    stats = {"applied": 0, "failed": 0}
    for s in sites:
        patcher["apply_site"](result, s, stats)
    assert stats == {"applied": 3, "failed": 0}
    assert b"WLo=1000" in bytes(result)
    assert b"hLe=99999" in bytes(result)
    assert b"GLo=1000" in bytes(result)
    assert b"hLe=24;" in bytes(result)  # Unrelated constant untouched.
    assert len(result) == len(source)
    done = patcher["discover_rate_limit"](bytes(result), b"zLo")
    assert len(done) == 3 and all(s["state"] == "done" for s in done)

    # Reordered declaration group still resolves by name, not position.
    reordered = (b'var GLo=600000,WLo=1800000,hLe=20000;'
                 b'function f(Yt){let dr=zLo(Yt),or=dr!==null&&dr<hLe;'
                 b'if(!or){let Wr=Math.max(dr??WLo,GLo)}}')
    assert len(patcher["discover_rate_limit"](reordered, b"zLo")) == 3

    # Two candidate declaration groups with a single retry path: refuse rather
    # than patch an ambiguous group. Assert on the declaration-group diagnostic
    # specifically -- duplicating the whole fixture would instead abort earlier,
    # on the duplicated retry path, and pass without exercising this guard.
    duplicate = b'var WLo=1800000,hLe=20000,GLo=600000;' + source
    diagnostics = io.StringIO()
    with contextlib.redirect_stderr(diagnostics):
        assert patcher["discover_rate_limit"](duplicate, b"zLo") == []
    assert "rate-limit constant declaration group" in diagnostics.getvalue()
    assert "refusing to guess" in diagnostics.getvalue()

    # Duplicated retry path is caught by the earlier uniqueness check.
    assert patcher["discover_rate_limit"](source + source, b"zLo") == []

    # Missing groups or parser anchors must not fall back to a global lookup.
    split = source.replace(b"var WLo=1800000,hLe=20000,GLo=600000;",
                           b"var WLo=1800000;var hLe=20000;var GLo=600000;")
    assert patcher["discover_rate_limit"](split, b"zLo") == []
    assert patcher["discover_rate_limit"](source, b"wrongParser") == []
    assert patcher["discover_rate_limit"](source, None) == []


def check_current():
    patched = check(CURRENT, 2)
    assert b"var fe=1e3,YGo=0.25;" in patched
    assert b"attempt:1,baseMs:a" in patched
    assert b"ns=Math.min(ns,Ys)" in patched
    assert b"ns=Math.min(ns,TQe)" in patched

    unrelated = b"function other(){let fe=900,YGo=0.5;return fe+YGo}"
    assert check(unrelated + CURRENT, 2).startswith(unrelated)
    renamed = {b"fe": b"$base", b"YGo": b"$ratio", b"xN": b"apiRetry",
               b"e": b"attempt", b"r": b"header", b"n": b"cap", b"o": b"rng",
               b"a": b"baseDelay", b"s": b"jitter", b"i": b"seconds",
               b"Ty": b"sharedBackoff", b"ns": b"$delay"}
    check(re.sub(rb"[A-Za-z_$][\w$]*",
                 lambda m: renamed.get(m[0], m[0]), CURRENT), 2)

    fn = patcher["discover_backoff_fn"](CURRENT)
    for site in patcher["backoff_sites"](fn):
        partial = bytearray(CURRENT)
        offset = site["offset"]
        partial[offset:offset + len(site["old"])] = site["new"]
        check(bytes(partial), 2)

    for bad in (CURRENT + CURRENT,
                CURRENT.replace(b",a=fe", b""),
                CURRENT.replace(b",YGo=0.25", b""),
                CURRENT.replace(b"a=fe", b"a=other"),
                CURRENT.replace(b"baseMs:a", b"baseMs:fe"),
                CURRENT.replace(b"ratio:YGo", b"ratio:other"),
                CURRENT.replace(b"attempt:e", b"attempt:other"),
                CURRENT.replace(b"random:o", b"random:other"),
                CURRENT.replace(b"parseInt(r,10)", b"parseInt(other,10)")):
        assert patcher["discover_backoff_fn"](bad) is None

    for expr in (b"Math.min(sr??xN(q,Jr,wmn,r.random),TQe)",
                 b"xN(dn+q,Jr,sr,r.random,Ys?a.base:void 0)"):
        for suffix in (b"+extra", b".value", b"??fallback"):
            source = b"ns=" + expr + suffix + b';log("tengu_api_retry",{attempt:dn,delayMs:ns})'
            assert patcher["discover_delay_pins"](source, b"xN") == []


def check_retry_cap():
    source = (
        b'function FQe(){let e=h5(),n=a.CLAUDE_CODE_MAX_RETRIES;'
        b'if(n!==void 0&&n>=0){if(n>vQe&&!e){'
        b'if(Js().once("max_retries_clamp_warning"))'
        b't(`CLAUDE_CODE_MAX_RETRIES=${n} clamped to ${vQe}`,{level:"warn"});'
        b'return vQe}return n}return e?kfr:_fr}'
    )
    renamed = re.sub(rb"\b(?:vQe|n)\b",
                     lambda m: {b"vQe": b"$cap", b"n": b"parsed"}[m[0]], source)
    for original in (source, renamed):
        status, site = patcher["discover_retry_cap"](original)
        assert status == "clamped" and site is not None
        result = bytearray(original)
        stats = {"applied": 0, "failed": 0}
        patcher["apply_site"](result, site, stats)
        assert stats == {"applied": 1, "failed": 0}
        assert len(result) == len(original)
        _, done = patcher["discover_retry_cap"](bytes(result))
        assert done is not None and done["state"] == "done"
    for bad in (b"", source + source,
                source.replace(b"n>vQe", b"other>vQe"),
                source.replace(b"n>vQe", b"n>other"),
                source.replace(b"return vQe", b"return other")):
        assert patcher["discover_retry_cap"](bad) == ("unknown", None)


def main():
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        check_current()
        check_rate_limit()
        check_retry_cap()
    print("OK: 2.1.306 shapes, renaming, equal-length rewrites, "
          "idempotence, partial patches, and ambiguity guards")


if __name__ == "__main__":
    main()
