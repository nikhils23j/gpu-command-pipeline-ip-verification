"""Python golden reference model for gpu_command_pipeline.

Written from the spec table in the README, not by reading the RTL, so the two
can disagree (which is the whole point).
"""

MASK = 0xFFFF

OPS = ["ADD", "SUB", "ADDS", "MUL", "MAC", "SHL", "SHR", "SRA",
       "MIN", "MAX", "CMPLT", "AND", "OR", "XOR", "DIVU", "REMU"]
OP = {name: i for i, name in enumerate(OPS)}
DIV_OPS = {OP["DIVU"], OP["REMU"]}


def s16(x: int) -> int:
    """Interpret a 16-bit value as two's-complement signed."""
    x &= MASK
    return x - 0x10000 if x & 0x8000 else x


def golden(op: int, a: int, b: int, c: int) -> tuple[int, int]:
    """Return (result, flag) for one command."""
    a &= MASK
    b &= MASK
    c &= MASK
    name = OPS[op]
    sh = b & 0xF
    if name == "ADD":
        full = a + b
        return full & MASK, int(full > MASK)
    if name == "SUB":
        return (a - b) & MASK, int(a < b)
    if name == "ADDS":
        full = s16(a) + s16(b)
        if full > 0x7FFF:
            return 0x7FFF, 1
        if full < -0x8000:
            return 0x8000, 1
        return full & MASK, 0
    if name == "MUL":
        full = a * b
        return full & MASK, int(full > MASK)
    if name == "MAC":
        full = a * b + c
        return full & MASK, int(full > MASK)
    if name == "SHL":
        return (a << sh) & MASK, 0
    if name == "SHR":
        return a >> sh, 0
    if name == "SRA":
        return (s16(a) >> sh) & MASK, 0
    if name == "MIN":
        return (a if s16(a) < s16(b) else b), 0
    if name == "MAX":
        return (b if s16(a) < s16(b) else a), 0
    if name == "CMPLT":
        return int(s16(a) < s16(b)), 0
    if name == "AND":
        return a & b, 0
    if name == "OR":
        return a | b, 0
    if name == "XOR":
        return a ^ b, 0
    if name == "DIVU":
        return (MASK, 1) if b == 0 else (a // b, 0)
    if name == "REMU":
        return (a, 1) if b == 0 else (a % b, 0)
    raise ValueError(op)
