from __future__ import annotations

ESC = 0x1B
BEL = 0x07

_STRING_INTRODUCERS = frozenset(b"]P X^_".replace(b" ", b""))  # ] P X ^ _

def strip(data: bytes) -> bytes:
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
            break
        c = data[j]

        if c == 0x5B:  # '[' — CSI
            j += 1
            while j < n and 0x20 <= data[j] <= 0x3F:
                j += 1
            if j < n and 0x40 <= data[j] <= 0x7E:
                j += 1
            i = j
            continue

        if c in _STRING_INTRODUCERS: 
            j += 1
            while j < n:
                if data[j] == BEL:
                    j += 1
                    break
                if data[j] == ESC and j + 1 < n and data[j + 1] == 0x5C: 
                    j += 2
                    break
                j += 1
            i = j
            continue
          
        k = i + 1
        while k < n and 0x20 <= data[k] <= 0x2F:
            k += 1
        if k < n and 0x30 <= data[k] <= 0x7E:
            i = k + 1
        else:
            i += 1 
    return bytes(out)

def strip_text(s: str) -> str:
    return strip(s.encode("utf-8", "surrogateescape")).decode("utf-8", "surrogateescape")
