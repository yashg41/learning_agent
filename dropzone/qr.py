"""Minimal QR encoder producing SVG.

Scope is deliberately narrow: byte mode, error-correction level L, automatic
version selection up to 10 (~270 chars) — comfortably more than a tunnel URL
plus a token. Written from scratch to avoid adding a dependency for one glyph.
"""

# --- Galois field tables for Reed-Solomon (GF(256), primitive poly 0x11d) ---

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(n: int) -> list[int]:
    g = [1]
    for i in range(n):
        g2 = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            g2[j] ^= c
            g2[j + 1] ^= _gf_mul(c, _EXP[i])
        g = g2
    return g


def _rs_encode(data: list[int], n_ec: int) -> list[int]:
    gen = _rs_generator(n_ec)
    rem = [0] * n_ec
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for i, g in enumerate(gen[1:]):
            rem[i] ^= _gf_mul(g, factor)
    return rem


# Per-version, level-L: (total codewords, ec codewords per block, num blocks)
_VERSIONS = {
    1: (26, 7, 1), 2: (44, 10, 1), 3: (70, 15, 1), 4: (100, 20, 1),
    5: (134, 26, 1), 6: (172, 18, 2), 7: (196, 20, 2), 8: (242, 24, 2),
    9: (292, 30, 2), 10: (346, 18, 4),
}

_ALIGNMENT = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}


def _capacity(version: int) -> int:
    total, ec_per_block, blocks = _VERSIONS[version]
    return total - ec_per_block * blocks


def _pick_version(length: int) -> int:
    for v in sorted(_VERSIONS):
        # 4 bits mode + 8/16 bits length + payload, rounded up to bytes
        header_bits = 4 + (8 if v < 10 else 16)
        if (header_bits + length * 8 + 7) // 8 <= _capacity(v):
            return v
    raise ValueError("payload too long for this encoder (max ~270 chars)")


def _build_codewords(data: bytes, version: int) -> list[int]:
    count_bits = 8 if version < 10 else 16
    bits: list[int] = []

    def push(value: int, n: int):
        for i in range(n - 1, -1, -1):
            bits.append((value >> i) & 1)

    push(0b0100, 4)                 # byte mode
    push(len(data), count_bits)
    for b in data:
        push(b, 8)

    cap_bits = _capacity(version) * 8
    push(0, min(4, cap_bits - len(bits)))       # terminator
    while len(bits) % 8:
        bits.append(0)

    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    # Pad bytes alternate 0xEC, 0x11 and must always START at 0xEC. Indexing
    # by len(codewords) would key the alternation off the data length instead,
    # flipping the order whenever the data is an odd number of codewords.
    pad = [0xEC, 0x11]
    n_pad = 0
    while len(codewords) < _capacity(version):
        codewords.append(pad[n_pad % 2])
        n_pad += 1
    return codewords


def _interleave(codewords: list[int], version: int) -> list[int]:
    total, ec_per_block, num_blocks = _VERSIONS[version]
    per_block = len(codewords) // num_blocks
    extra = len(codewords) % num_blocks

    blocks, ec_blocks, pos = [], [], 0
    for i in range(num_blocks):
        size = per_block + (1 if i >= num_blocks - extra else 0)
        block = codewords[pos:pos + size]
        pos += size
        blocks.append(block)
        ec_blocks.append(_rs_encode(block, ec_per_block))

    out = []
    for i in range(max(len(b) for b in blocks)):
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(ec_per_block):
        for b in ec_blocks:
            out.append(b[i])
    return out


def _version_bits(version: int) -> int:
    """6 version bits + 12 BCH(18,6) error-correction bits."""
    value = version << 12
    for _ in range(12):
        if value.bit_length() - 1 < 12:
            break
        value ^= 0x1F25 << (value.bit_length() - 13)
    return (version << 12) | value


def _place(version: int, bitstream: list[int]) -> list[list[int]]:
    size = version * 4 + 17
    mod: list[list[int | None]] = [[None] * size for _ in range(size)]

    def finder(r: int, c: int):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = r + dr, c + dc
                if 0 <= rr < size and 0 <= cc < size:
                    inside = 0 <= dr <= 6 and 0 <= dc <= 6
                    ring = dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)
                    mod[rr][cc] = 1 if (inside and ring) else 0

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    for i in range(8, size - 8):
        bit = 1 if i % 2 == 0 else 0
        mod[6][i] = bit
        mod[i][6] = bit

    # Alignment patterns sit at every combination of the version's coordinates,
    # except the three that would collide with a finder pattern. Centres on the
    # timing row/column (e.g. (6, 22)) are legitimate and must still be drawn —
    # testing whether the centre module is already occupied would wrongly skip
    # them, since the timing pattern has already written there.
    coords = _ALIGNMENT[version]
    if coords:
        last = coords[-1]
        skip = {(coords[0], coords[0]), (coords[0], last), (last, coords[0])}
        for r in coords:
            for c in coords:
                if (r, c) in skip:
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        edge = abs(dr) == 2 or abs(dc) == 2 or (dr == 0 and dc == 0)
                        mod[r + dr][c + dc] = 1 if edge else 0

    mod[size - 8][8] = 1  # dark module

    # Reserve format areas so data placement skips them.
    for i in range(9):
        if mod[8][i] is None:
            mod[8][i] = 0
        if mod[i][8] is None:
            mod[i][8] = 0
    for i in range(8):
        if mod[8][size - 1 - i] is None:
            mod[8][size - 1 - i] = 0
        if mod[size - 1 - i][8] is None:
            mod[size - 1 - i][8] = 0

    # Version 7 and up carry two 6x3 version-information blocks. They must be
    # written AND reserved; leaving them out shifts all subsequent data
    # placement and corrupts the whole symbol.
    if version >= 7:
        bits = _version_bits(version)
        for i in range(18):
            bit = (bits >> i) & 1
            r, c = i // 3, i % 3
            mod[r][size - 11 + c] = bit
            mod[size - 11 + c][r] = bit

    reserved = [[mod[r][c] is not None for c in range(size)] for r in range(size)]

    # Zig-zag data placement: two-module-wide columns walked right to left,
    # alternating upward/downward. Column 6 is the vertical timing pattern and
    # is removed from the pairing entirely — not merely skipped mid-pair, or
    # every subsequent column pair would be off by one.
    columns = [c for c in range(size - 1, -1, -1) if c != 6]
    pairs = [(columns[i], columns[i + 1]) for i in range(0, len(columns) - 1, 2)]

    idx = 0
    for pair_index, (right, left) in enumerate(pairs):
        rows = range(size - 1, -1, -1) if pair_index % 2 == 0 else range(size)
        for row in rows:
            for c in (right, left):
                if reserved[row][c]:
                    continue
                if idx >= len(bitstream):
                    # Remainder modules stay 0 before masking.
                    bit = 0
                else:
                    bit = bitstream[idx]
                    idx += 1
                # Mask 0: invert where (row + column) is even.
                mod[row][c] = bit ^ (1 if (row + c) % 2 == 0 else 0)

    _place_format(mod, size)
    return [[int(v or 0) for v in row] for row in mod]


def _place_format(mod, size: int):
    """Format info for level L + mask 0, with its fixed BCH/XOR encoding."""
    bits = [int(b) for b in format(0b111011111000100, "015b")]

    coords_a = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
                (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    for bit, (r, c) in zip(bits, coords_a):
        mod[r][c] = bit

    for i in range(7):
        mod[size - 1 - i][8] = bits[i]
    for i in range(8):
        mod[8][size - 8 + i] = bits[7 + i]


def svg(payload: str, scale: int = 6, quiet: int = 4) -> str:
    """Render `payload` as a self-contained SVG string."""
    data = payload.encode("utf-8")
    version = _pick_version(len(data))
    codewords = _build_codewords(data, version)
    interleaved = _interleave(codewords, version)

    bits: list[int] = []
    for cw in interleaved:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)

    matrix = _place(version, bits)
    size = len(matrix)
    dim = (size + quiet * 2) * scale

    rects = []
    for r, row in enumerate(matrix):
        run_start = None
        for c in range(size + 1):
            dark = c < size and row[c] == 1
            if dark and run_start is None:
                run_start = c
            elif not dark and run_start is not None:
                x = (run_start + quiet) * scale
                y = (r + quiet) * scale
                w = (c - run_start) * scale
                rects.append(f'<rect x="{x}" y="{y}" width="{w}" height="{scale}"/>')
                run_start = None

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{dim}" height="{dim}" '
        f'viewBox="0 0 {dim} {dim}" shape-rendering="crispEdges">'
        f'<rect width="{dim}" height="{dim}" fill="#fff"/>'
        f'<g fill="#000">{"".join(rects)}</g></svg>'
    )
