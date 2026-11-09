ESC = 0x1b
BEL = 0x07


def strip(data):
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b != ESC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            break

        c = data[i+1]
        j = i + 2
        if c == ord('['):
            while j < n and 0x20 <= data[j] <= 0x3f:
                j += 1
            if j < n and 0x40 <= data[j] <= 0x7e:
                j += 1
            i = j
        elif c in b']PX^_':
            while j < n:
                if data[j] == BEL:
                    j += 1
                    break
                if data[j] == ESC and j+1 < n and data[j+1] == 0x5c:
                    j += 2
                    break
                j += 1
            i = j
        else:
            k = i + 1
            while k < n and 0x20 <= data[k] <= 0x2f:
                k += 1
            if k < n and 0x30 <= data[k] <= 0x7e:
                i = k + 1
            else:
                i += 1
    return bytes(out)

def strip_text(s):
    return strip(s.encode('utf-8', 'surrogateescape')).decode('utf-8', 'surrogateescape')
