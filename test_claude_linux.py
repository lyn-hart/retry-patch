"""Offline regression check: python3 test_claude_linux.py."""

import contextlib
import io
import re
import runpy
from pathlib import Path


patcher = runpy.run_path(str(Path(__file__).with_name("claude-linux.py")))
LEGACY = (
    b'var base=500;function retry(attempt,header,cap=32000){'
    b'let d=Math.min(base*Math.pow(2,attempt-1),cap),j=d+Math.random()*0.25*d;'
    b'if(header){let s=parseInt(header,10);if(!isNaN(s))return Math.max(s*1000,j)}return j}'
    b'function loop(n,h,cap,ceil,err){let x;'
    b'x=reset(err)??Math.min(retry(n,h,cap),ceil);'
    b'x=Math.min(retry(n,h,cap),ceil);x=retry(n,h);'
    b'log("tengu_api_retry",{attempt:n,delayMs:x})}'
)
MODERN = (
    b'var le=500;function ZD(e,r,n=32000,o=Math.random){'
    b'let a=Math.round(dC({attempt:e,baseMs:le,capMs:n,'
    b'jitter:{kind:"proportional",ratio:0.25},random:o}));'
    b'if(r){let s=parseInt(r,10);if(!isNaN(s))return Math.max(s*1000,a)}return a}'
    b'function loop(Pt,F,Vn,r,nr,MIo,NNe){let mo;'
    b'mo=nr??Math.min(ZD(F,Vn,MIo,r.random),NNe);'
    b'mo=ZD(Pt+F,Vn,void 0,r.random);mo=Math.min(mo,NNe);'
    b'log("tengu_api_retry",{attempt:Pt,delayMs:mo})}'
)


def check(source, pin_count):
    fn = patcher["discover_backoff_fn"](source)
    assert fn is not None
    sites = patcher["backoff_sites"](source, fn)
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
    done = patcher["backoff_sites"](bytes(result), fn)
    done += patcher["discover_delay_pins"](bytes(result), fn["name"])
    assert len(done) == len(sites)
    assert all(s["state"] == "done" for s in done), done
    return bytes(result)


def main():
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        check(LEGACY, 3)
        patched = check(MODERN, 2)
        assert b"attempt:1,baseMs:le" in patched
        assert b"mo=Math.min(mo,NNe)" in patched  # Not a backoff assignment.

        # Short names are reused across modules; only the adjacent base changes.
        unrelated = b"function other(){let le=500;return le}"
        assert check(unrelated + MODERN, 2).startswith(unrelated)
        renamed = {b"le": b"$base", b"e": b"attempt", b"ZD": b"apiRetry",
                   b"r": b"header", b"n": b"cap", b"o": b"rng",
                   b"a": b"delay", b"s": b"seconds", b"dC": b"computeBackoff"}
        check(re.sub(rb"[A-Za-z_$][\w$]*",
                     lambda m: renamed.get(m[0], m[0]), MODERN), 2)

        # Each individual rewrite can also be rediscovered after a partial patch.
        fn = patcher["discover_backoff_fn"](MODERN)
        for site in patcher["backoff_sites"](MODERN, fn):
            partial = bytearray(MODERN)
            offset = site["offset"]
            partial[offset:offset + len(site["old"])] = site["new"]
            check(bytes(partial), 2)
        assert "WARN:" not in output.getvalue(), output.getvalue()

        # Reject ambiguity and shapes whose bound parameters no longer agree.
        for bad in (MODERN + MODERN, LEGACY + MODERN,
                    MODERN.replace(b"attempt:e", b"attempt:other"),
                    MODERN.replace(b"baseMs:le", b"baseMs:other"),
                    MODERN.replace(b"random:o", b"random:other"),
                    MODERN.replace(b"parseInt(r,10)", b"parseInt(other,10)")):
            assert patcher["discover_backoff_fn"](bad) is None

        # Never rewrite just the prefix of a more complicated delay expression.
        for suffix in (b"+extra", b".value"):
            source = (b"mo=ZD(Pt+F,Vn,void 0,r.random)" + suffix
                      + b';log("tengu_api_retry",{attempt:Pt,delayMs:mo})')
            assert patcher["discover_delay_pins"](source, b"ZD") == []
    print("OK: legacy/2.1.285 shapes, renaming, equal-length rewrites, "
          "idempotence, partial patches, and ambiguity guards")


if __name__ == "__main__":
    main()
