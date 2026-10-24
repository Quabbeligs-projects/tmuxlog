"""Strip terminal escape sequences for the clean rendering.

This is the ONLY transformation master.clean.log applies to raw.bin: it removes
escape sequences and nothing else, so the clean log differs from the raw bytes
only in escapes, never in content (per docs/DESIGN.md §5). In particular it does
NOT touch carriage returns, backspaces, BEL, NUL, other C0 controls, UTF-8 or
arbitrary binary — those are content. Collapsing CR/backspace for readability is
a separate transform applied only to the analyze input in Phase 4, never here.

Only ESC (0x1B)-introduced sequences are removed. Eight-bit C1 introducers
(0x80-0x9F) are left alone on purpose: in a byte stream they are far more likely
to be UTF-8 continuation bytes than real C1 controls, and treating them as
controls would corrupt multibyte text.

Sequences removed:
  - CSI   ESC [ , parameters/intermediates 0x20-0x3F, one final 0x40-0x7E
  - OSC   ESC ] , string ended by BEL (0x07) or ST (ESC \\)
  - DCS/SOS/PM/APC  ESC P / X / ^ / _ , string ended by ST (or BEL, tolerated)
  - nF/Fp/Fe/Fs  ESC , optional intermediates 0x20-0x2F, one final 0x30-0x7E
A dangling or truncated sequence at end-of-data drops the bytes it has; a bare
ESC that introduces nothing recognizable drops only the ESC.

stdlib only.
"""

from __future__ import annotations

ESC = 0x1B
BEL = 0x07

_STRING_INTRODUCERS = frozenset(b"]P X^_".replace(b" ", b""))  # ] P X ^ _


def strip(data: bytes) -> bytes:
    """Return ``data`` with ESC-introduced escape sequences removed."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b != ESC:
            out.append(b)
            i += 1
            continue

        j = i + 1
        if j >= n:
            break  # dangling ESC at EOF
        c = data[j]

        if c == 0x5B:  # '[' — CSI
            j += 1
            while j < n and 0x20 <= data[j] <= 0x3F:   # parameter + intermediate bytes
                j += 1
            if j < n and 0x40 <= data[j] <= 0x7E:      # final byte
                j += 1
            i = j
            continue

        if c in _STRING_INTRODUCERS:  # OSC / DCS / SOS / PM / APC — string sequence
            j += 1
            while j < n:
                if data[j] == BEL:
                    j += 1
                    break
                if data[j] == ESC and j + 1 < n and data[j + 1] == 0x5C:  # ST = ESC '\'
                    j += 2
                    break
                j += 1
            i = j
            continue

        # Any other ESC: optional intermediates 0x20-0x2F then one final 0x30-0x7E
        k = i + 1
        while k < n and 0x20 <= data[k] <= 0x2F:
            k += 1
        if k < n and 0x30 <= data[k] <= 0x7E:
            i = k + 1
        else:
            i += 1  # unrecognized: drop only the ESC, keep the rest
    return bytes(out)


def strip_text(s: str) -> str:
    """Convenience: strip escapes from a str, preserving exotic bytes round-trip."""
    return strip(s.encode("utf-8", "surrogateescape")).decode("utf-8", "surrogateescape")
