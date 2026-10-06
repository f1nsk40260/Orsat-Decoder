"""JT65 et JT9 (K1JT) : modes à signaux faibles en créneaux d'une minute.

- JT65 (A/B/C) : 126 symboles de 0,372 s, 65 tonalités (une de synchronisation suivant une suite
  pseudo-aléatoire de 126 bits, 64 de données), code de Reed-Solomon (63, 12) sur GF(64),
  entrelacement 7x9, code de Gray. Décodage : Berlekamp-Massey avec effacements des symboles les
  moins sûrs (pas l'algorithme de Kötter-Vardy de WSJT-X, qui n'est pas libre).
- JT9A : 85 symboles de 0,576 s, 9 tonalités (16 de synchronisation), code convolutif K=32 1/2
  (le même que WSPR) décodé par l'algorithme de Fano de wsprd (native/), entrelacement par
  inversion de bits, code de Gray.
Messages de 72 bits « JT » (deux indicatifs et un locator ou un report, ou 13 caractères libres).
Constantes et formats d'après WSJT-X (GPL-3).
"""
import logging
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from ..dsp import Decoder

log = logging.getLogger("orsat.jt")
NATIVE = Path(__file__).resolve().parents[2] / "native" / "bin"

# ---------------------------------------------------------------------------------------- messages
NBASE = 37 * 36 * 10 * 27 * 27 * 27
NGBASE = 180 * 180
_C37 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ "
_C42 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ +-./?"
PFX = ("1A 1S 3A 3B6 3B8 3B9 3C 3C0 3D2 3D2C 3D2R 3DA 3V 3W 3X 3Y 3YB 3YP 4J 4L 4S 4U1I 4U1U 4W 4X 5A 5B 5H 5N "
       "5R 5T 5U 5V 5W 5X 5Z 6W 6Y 7O 7P 7Q 7X 8P 8Q 8R 9A 9G 9H 9J 9K 9L 9M2 9M6 9N 9Q 9U 9V 9X 9Y A2 A3 A4 A5 A6 "
       "A7 A9 AP BS7 BV BV9 BY C2 C3 C5 C6 C9 CE CE0X CE0Y CE0Z CE9 CM CN CP CT CT3 CU CX CY0 CY9 D2 D4 D6 DL DU "
       "E3 E4 E5 EA EA6 EA8 EA9 EI EK EL EP ER ES ET EU EX EY EZ F FG FH FJ FK FKC FM FO FOA FOC FOM FP FR FRG FRJ "
       "FRT FT5W FT5X FT5Z FW FY M MD MI MJ MM MU MW H4 H40 HA HB HB0 HC HC8 HH HI HK HK0 HK0M HL HM HP HR HS HV "
       "HZ I IS IS0 J2 J3 J5 J6 J7 J8 JA JDM JDO JT JW JX JY K KG4 KH0 KH1 KH2 KH3 KH4 KH5 KH5K KH6 KH7 KH8 KH9 "
       "KL KP1 KP2 KP4 KP5 LA LU LX LY LZ OA OD OE OH OH0 OJ0 OK OM ON OX OY OZ P2 P4 PA PJ2 PJ7 PY PY0F PT0S "
       "PY0T PZ R1F R1M S0 S2 S5 S7 S9 SM SP ST SU SV SVA SV5 SV9 T2 T30 T31 T32 T33 T5 T7 T8 T9 TA TF TG TI TI9 "
       "TJ TK TL TN TR TT TU TY TZ UA UA2 UA9 UK UN UR V2 V3 V4 V5 V6 V7 V8 VE VK VK0H VK0M VK9C VK9L VK9M VK9N "
       "VK9W VK9X VP2E VP2M VP2V VP5 VP6 VP6D VP8 VP8G VP8H VP8O VP8S VP9 VQ9 VR VU VU4 VU7 XE XF4 XT XU XW XX9 "
       "XZ YA YB YI YJ YK YL YN YO YS YU YV YV0 Z2 Z3 ZA ZB ZC4 ZD7 ZD8 ZD9 ZF ZK1N ZK1S ZK2 ZK3 ZL ZL7 ZL8 ZL9 "
       "ZP ZS ZS8 KC4 E5").split()
SFX = "P 0 1 2 3 4 5 6 7 8 9 A".split()


def unpackcall(n):
    """-> (indicatif, iv2, préfixe/suffixe)."""
    psfx = ""
    iv2 = 0
    if n < 262177560:
        w = [""] * 6
        for pos, mod, off in ((5, 27, 10), (4, 27, 10), (3, 27, 10), (2, 10, 0), (1, 36, 0)):
            w[pos] = _C37[n % mod + off]
            n //= mod
        w[0] = _C37[n] if n < 37 else "?"
        word = "".join(w).strip()
    else:
        word = ""
        if n >= 267796946:
            return "", 0, ""
        ranges = ((262178563, 264002071, 1, 4), (264002072, 265825580, 2, 4), (265825581, 267649089, 3, 4),
                  (267649090, 267698374, 4, 3), (267698375, 267747659, 5, 3), (267747660, 267796944, 6, 3))
        for lo, hi, v, nc in ranges:
            if lo <= n <= hi:
                iv2 = v
                m = n - lo
                cs = []
                for _ in range(nc - 1):
                    cs.insert(0, _C37[m % 37])
                    m //= 37
                cs.insert(0, _C37[m] if m < 37 else "?")
                psfx = "".join(cs).strip()
                break
        if n == 267796945:
            iv2 = 7
    if word.startswith("3D0"):
        word = "3DA0" + word[3:]
    if word[:1] == "Q" and "A" <= word[1:2] <= "Z":
        word = "3X" + word[1:]
    return word, iv2, psfx


def grid2deg(g):
    g = (g[:4].upper() + "mm") if len(g) < 6 else g[:4].upper() + g[4:6].lower()
    nlong = 180 - 20 * (ord(g[0]) - 65)
    dlong = nlong - 2 * (ord(g[2]) - 48) - 5 * (ord(g[4]) - 97 + 0.5) / 60.0
    dlat = -90 + 10 * (ord(g[1]) - 65) + ord(g[3]) - 48 + 2.5 * (ord(g[5]) - 97 + 0.5) / 60.0
    return dlong, dlat


def deg2grid(dlong, dlat):
    if dlong < -180:
        dlong += 360
    if dlong > 180:
        dlong -= 360
    nlong = int(60.0 * (180.0 - dlong) / 5)
    n1, n2 = nlong // 240, (nlong - 240 * (nlong // 240)) // 24
    nlat = int(60.0 * (dlat + 90) / 2.5)
    m1, m2 = nlat // 240, (nlat - 240 * (nlat // 240)) // 24
    return chr(65 + n1) + chr(65 + m1) + chr(48 + n2) + chr(48 + m2)


def unpackgrid(ng):
    if ng < NGBASE:
        grid = deg2grid((ng // 180) * 2 - 180 + 2, ng % 180 - 90)
        if grid[:2] == "KA":
            return f"{int(grid[2:]) - 50:+03d}"
        if grid[:2] == "LA":
            return f"R{int(grid[2:]) - 50:+03d}"
        return grid
    n = ng - NGBASE - 1
    if 1 <= n <= 30:
        return f"-{n:02d}"
    if 31 <= n <= 60:
        return f"R-{n - 30:02d}"
    return {61: "RO", 62: "RRR", 63: "73"}.get(n, "")


def grid2k(grid):
    dlong, dlat = grid2deg(grid + "ma")
    nlong, nlat = round(dlong), round(dlat)
    return 5 * (nlong + 179) // 2 + nlat - 84 if nlat >= 85 else 0


def getpfx2(k, call):
    if k > 450:
        k -= 450
    if 1 <= k <= len(PFX):
        return PFX[k - 1] + "/" + call
    if 401 <= k <= 400 + len(SFX):
        return call + "/" + SFX[k - 401]
    return call


def unpacktext(nc1, nc2, nc3):
    nc3 &= 32767
    if nc1 & 1:
        nc3 += 32768
    nc1 //= 2
    if nc2 & 1:
        nc3 += 65536
    nc2 //= 2
    m = [""] * 13
    for i in range(4, -1, -1):
        m[i] = _C42[nc1 % 42]
        nc1 //= 42
    for i in range(9, 4, -1):
        m[i] = _C42[nc2 % 42]
        nc2 //= 42
    for i in range(12, 9, -1):
        m[i] = _C42[nc3 % 42]
        nc3 //= 42
    return "".join(m).strip()


def unpackmsg(d):
    """12 mots de 6 bits -> texte du message."""
    nc1 = (d[0] << 22) + (d[1] << 16) + (d[2] << 10) + (d[3] << 4) + ((d[4] >> 2) & 15)
    nc2 = ((d[4] & 3) << 26) + (d[5] << 20) + (d[6] << 14) + (d[7] << 8) + (d[8] << 2) + ((d[9] >> 4) & 3)
    ng = ((d[9] & 15) << 12) + (d[10] << 6) + d[11]
    if ng >= 32768:
        return unpacktext(nc1, nc2, ng)
    c1, iv2, psfx = unpackcall(nc1)
    cqnnn = False
    if iv2 == 0:
        if nc1 == NBASE + 1:
            c1 = "CQ"
        if nc1 == NBASE + 2:
            c1 = "QRZ"
        nfreq = nc1 - NBASE - 3
        if 0 <= nfreq <= 999:
            c1 = f"CQ {nfreq:03d}"
            cqnnn = True
    c2, _, _ = unpackcall(nc2)
    grid = unpackgrid(ng)
    if iv2 > 0:
        lead = {1: "CQ", 2: "QRZ", 3: "DE", 4: "CQ", 5: "QRZ", 6: "DE"}.get(iv2, "DE")
        if iv2 in (1, 2, 3):
            msg = f"{lead} {psfx}/{c2} {grid}"
        elif iv2 in (4, 5, 6):
            msg = f"{lead} {c2}/{psfx} {grid}"
        else:
            k = grid2k(grid) if len(grid) == 4 and grid[0].isalpha() else 0
            msg = f"DE {getpfx2(k, c2)}" if 451 <= k <= 900 else f"DE {c2} {grid}"
        return " ".join(msg.split())
    k = grid2k(grid) if len(grid) == 4 and grid[0].isalpha() and grid[2].isdigit() else 0
    if 1 <= k <= 450:
        c1 = getpfx2(k, c1)
    if 451 <= k <= 900:
        c2 = getpfx2(k, c2)
    msg = f"{c1} {c2}" + ("" if k else f" {grid}")
    msg = " ".join(msg.split())
    if msg.startswith("CQ9DX "):
        msg = "CQ " + msg[3:]
    return msg


def _nchar(c):
    if c.isdigit():
        return ord(c) - 48
    if "A" <= c <= "Z":
        return ord(c) - 55
    return 36


def packcall(call):
    if call == "CQ":
        return NBASE + 1
    if call == "QRZ":
        return NBASE + 2
    call = call.upper()
    if len(call) >= 3 and call[2].isdigit():
        tmp = call.ljust(6)
    elif len(call) >= 2 and call[1].isdigit() and len(call) <= 5:
        tmp = (" " + call).ljust(6)
    else:
        raise ValueError(call)
    n = _nchar(tmp[0])
    n = 36 * n + _nchar(tmp[1])
    n = 10 * n + _nchar(tmp[2])
    for c in tmp[3:]:
        n = 27 * n + _nchar(c) - 10
    return n


def packmsg(msg):
    """« CALL1 CALL2 GRID » ou report -> 12 mots de 6 bits (formes standard seulement)."""
    p = msg.upper().split()
    c1, c2 = packcall(p[0]), packcall(p[1])
    g = p[2] if len(p) > 2 else ""
    if not g:
        ng = NGBASE + 1
    elif g.startswith("R-"):
        ng = NGBASE + 31 + int(g[2:])
    elif g.startswith("-"):
        ng = NGBASE + 1 + int(g[1:])
    elif g in ("RO", "RRR", "73"):
        ng = NGBASE + {"RO": 62, "RRR": 63, "73": 64}[g]
    else:
        dlong, dlat = grid2deg(g + "mm")
        ng = ((int(dlong) + 180) // 2) * 180 + int(dlat + 90)
    d = [0] * 12
    d[0], d[1], d[2], d[3] = (c1 >> 22) & 63, (c1 >> 16) & 63, (c1 >> 10) & 63, (c1 >> 4) & 63
    d[4] = ((c1 & 15) << 2) | ((c2 >> 26) & 3)
    d[5], d[6], d[7], d[8] = (c2 >> 20) & 63, (c2 >> 14) & 63, (c2 >> 8) & 63, (c2 >> 2) & 63
    d[9] = ((c2 & 3) << 4) | ((ng >> 12) & 15)
    d[10], d[11] = (ng >> 6) & 63, ng & 63
    return d


# ---------------------------------------------------------------------------------------- Reed-Solomon (63, 12)
_NN, _NROOTS, _FCR, _PRIM = 63, 51, 3, 1
_A0 = _NN
ALPHA = [0] * 64
INDEX = [0] * 64
_sr = 1
for _i in range(_NN):
    INDEX[_sr] = _i
    ALPHA[_i] = _sr
    _sr <<= 1
    if _sr & 64:
        _sr ^= 0x43
    _sr &= 63
INDEX[0] = _A0
ALPHA[_A0] = 0


def _mod(x):
    while x >= _NN:
        x -= _NN
        x = (x >> 6) + (x & _NN)
    return x


_GEN = [1] + [0] * _NROOTS
_root = _FCR * _PRIM
for _i in range(_NROOTS):
    _GEN[_i + 1] = 1
    for _j in range(_i, 0, -1):
        _GEN[_j] = _GEN[_j - 1] ^ ALPHA[_mod(INDEX[_GEN[_j]] + _root)] if _GEN[_j] else _GEN[_j - 1]
    _GEN[0] = ALPHA[_mod(INDEX[_GEN[0]] + _root)]
    _root += _PRIM
_GEN = [INDEX[g] for g in _GEN]


def _encode_rs(data):
    bb = [0] * _NROOTS
    for i in range(_NN - _NROOTS):
        fb = INDEX[data[i] ^ bb[0]]
        if fb != _A0:
            for j in range(1, _NROOTS):
                bb[j] ^= ALPHA[_mod(fb + _GEN[_NROOTS - j])]
        bb = bb[1:] + [ALPHA[_mod(fb + _GEN[0])] if fb != _A0 else 0]
    return bb


def _decode_rs(data, eras):
    """Décodeur de Karn (erreurs et effacements) ; data modifié sur place. -> nombre de corrections ou -1."""
    s = [data[0]] * _NROOTS
    for j in range(1, _NN):
        for i in range(_NROOTS):
            s[i] = data[j] if s[i] == 0 else data[j] ^ ALPHA[_mod(INDEX[s[i]] + (_FCR + i) * _PRIM)]
    if not any(s):
        return 0
    s = [INDEX[v] for v in s]
    lam = [1] + [0] * _NROOTS
    ne = len(eras)
    if ne:
        lam[1] = ALPHA[_mod(_PRIM * (_NN - 1 - eras[0]))]
        for i in range(1, ne):
            u = _mod(_PRIM * (_NN - 1 - eras[i]))
            for j in range(i + 1, 0, -1):
                tmp = INDEX[lam[j - 1]]
                if tmp != _A0:
                    lam[j] ^= ALPHA[_mod(u + tmp)]
    b = [INDEX[v] for v in lam]
    r = el = ne
    while True:
        r += 1
        if r > _NROOTS:
            break
        discr = 0
        for i in range(r):
            if lam[i] != 0 and s[r - i - 1] != _A0:
                discr ^= ALPHA[_mod(INDEX[lam[i]] + s[r - i - 1])]
        discr = INDEX[discr]
        if discr == _A0:
            b = [_A0] + b[:-1]
        else:
            t = [lam[0]] + [lam[i + 1] ^ ALPHA[_mod(discr + b[i])] if b[i] != _A0 else lam[i + 1]
                            for i in range(_NROOTS)]
            if 2 * el <= r + ne - 1:
                el = r + ne - el
                b = [_A0 if lam[i] == 0 else _mod(INDEX[lam[i]] - discr + _NN) for i in range(_NROOTS + 1)]
            else:
                b = [_A0] + b[:-1]
            lam = t
    lam = [INDEX[v] for v in lam]
    deg = max([i for i in range(_NROOTS + 1) if lam[i] != _A0], default=0)
    reg = lam[:]
    roots, locs = [], []
    k = 0                                     # IPRIM - 1 (IPRIM = 1)
    for i in range(1, _NN + 1):
        q = 1
        for j in range(deg, 0, -1):
            if reg[j] != _A0:
                reg[j] = _mod(reg[j] + j)
                q ^= ALPHA[reg[j]]
        if q == 0:
            roots.append(i)
            locs.append(k)
            if len(roots) == deg:
                break
        k = _mod(k + 1)
    if deg != len(roots):
        return -1
    omega = []
    for i in range(deg):
        tmp = 0
        for j in range(i, -1, -1):
            if s[i - j] != _A0 and lam[j] != _A0:
                tmp ^= ALPHA[_mod(s[i - j] + lam[j])]
        omega.append(INDEX[tmp])
    for j in range(len(roots) - 1, -1, -1):
        num1 = 0
        for i in range(deg - 1, -1, -1):
            if omega[i] != _A0:
                num1 ^= ALPHA[_mod(omega[i] + i * roots[j])]
        num2 = ALPHA[_mod(roots[j] * (_FCR - 1) + _NN)]
        den = 0
        for i in range(min(deg, _NROOTS - 1) & ~1, -1, -2):
            if lam[i + 1] != _A0:
                den ^= ALPHA[_mod(lam[i + 1] + i * roots[j])]
        if den == 0:
            return -1
        if num1:
            data[locs[j]] ^= ALPHA[_mod(INDEX[num1] + INDEX[num2] + _NN - INDEX[den])]
    return len(roots)


def rs_encode(dgen):
    """12 mots -> 63 symboles (ordre de WSJT-X)."""
    dat1 = dgen[::-1]
    b = _encode_rs(dat1)
    return b[::-1] + dat1[::-1]


def rs_decode(recd0, eras0):
    """63 symboles (ordre WSJT-X), positions d'effacement -> (12 mots, nombre de corrections)."""
    recd = recd0[62:50:-1] + recd0[50::-1]
    eras = []
    for p in eras0:                      # position dans recd0 -> position dans recd
        eras.append(62 - p if p >= 51 else 12 + 50 - p)
    n = _decode_rs(recd, eras)
    return recd[11::-1], n


def interleave63(d, direction):
    out = [0] * 63
    for i in range(7):
        for j in range(9):
            if direction > 0:
                out[j + 9 * i] = d[i + 7 * j]
            else:
                out[i + 7 * j] = d[j + 9 * i]
    return out


def gray(n):
    return n ^ (n >> 1)


def igray(g):
    b = 0
    while g:
        b ^= g
        g >>= 1
    return b


NPRC = [1, 0, 0, 1, 1, 0, 0, 0, 1, 1, 1, 1, 1, 1, 0, 1, 0, 1, 0, 0, 0, 1, 0, 1, 1, 0, 0, 1, 0, 0, 0, 1, 1, 1, 0, 0,
        1, 1, 1, 1, 0, 1, 1, 0, 1, 1, 1, 1, 0, 0, 0, 1, 1, 0, 1, 0, 1, 0, 1, 1, 0, 0, 1, 1, 0, 1, 0, 1, 0, 1, 0, 0,
        1, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 1, 0, 0, 1, 0, 1, 1, 0, 1, 0, 1, 0, 1, 0, 0, 1, 1,
        0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1]


# ---------------------------------------------------------------------------------------- JT9
JT9_SYNC = [1, 1, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0,
            0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0,
            1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1]
_J0 = [n for n in (int(f"{m:08b}"[::-1], 2) for m in range(256)) if n <= 205]
_P1, _P2 = 0xF2D05351, 0xE4613C47


def _par(x):
    return bin(x).count("1") & 1


def jt9_tones(msg):
    return jt9_tones_from_words(packmsg(msg))


def jt9_tones_from_words(d):
    bits = [(w >> (5 - k)) & 1 for w in d for k in range(6)] + [0] * 31
    st, sym = 0, []
    for b in bits:
        st = ((st << 1) | b) & 0xFFFFFFFF
        sym += [_par(st & _P1), _par(st & _P2)]
    inter = [0] * 207
    for i in range(206):
        inter[_J0[i]] = sym[i]
    vals = [inter[3 * k] * 4 + inter[3 * k + 1] * 2 + inter[3 * k + 2] for k in range(69)]
    out, j = [], 0
    for s in JT9_SYNC:
        if s:
            out.append(0)
        else:
            out.append(gray(vals[j]) + 1)
            j += 1
    return out


def fano_decode(soft):
    """206 valeurs souples (0..255, 255 = bit 1) -> 72 bits ou None (Fano de wsprd, natif)."""
    exe = NATIVE / "wspr_decode_iq"
    if not exe.exists():
        return None
    r = subprocess.run([str(exe), "-f", "103"], input=bytes(soft), capture_output=True, timeout=20)
    out = r.stdout.decode().split()
    if not out or out[0] == "FAIL":
        return None
    data = bytes.fromhex(out[0])
    return [(data[i // 8] >> (7 - i % 8)) & 1 for i in range(72)]


# ---------------------------------------------------------------------------------------- démodulation commune
def _baseband(X, N, f_lo, nb):
    """Bins [f_lo, f_lo + nb) du spectre complet -> signal complexe à nb*df Hz (f_lo -> 0)."""
    seg = X[f_lo:f_lo + nb].copy()
    taper = max(1, nb // 20)
    w = np.ones(nb)
    w[:taper] = np.hanning(2 * taper)[:taper]
    w[-taper:] = np.hanning(2 * taper)[taper:]
    return np.fft.ifft(seg * w) * (nb / N)


def _tone_powers(z, fsd, sps, start, nsym, f_rel, spacing, ntones):
    """Puissance de chaque tonalité pour chaque symbole (fenêtre d'un symbole)."""
    L = int(round(sps))
    tt = np.arange(L) / fsd
    E = np.exp(-2j * np.pi * (f_rel + np.arange(ntones)[:, None] * spacing) * tt[None, :])
    P = np.zeros((nsym, ntones))
    for k in range(nsym):
        a = int(round(start + k * sps))
        if a < 0 or a + L > len(z):
            P[k] = np.nan
            continue
        P[k] = np.abs(E @ z[a:a + L]) ** 2
    return P


class JTMode:
    """Paramètres et recherche commune (synchro, recalage) de JT65 et JT9."""

    def __init__(self, kind, sub="A"):
        self.kind = kind
        if kind == "jt65":
            self.tsym = 4096 / 11025.0
            self.spacing = 11025 / 4096.0 * {"A": 1, "B": 2, "C": 4}[sub]
            self.nsym = 126
            self.sync = NPRC
            self.ntones = 66
        else:
            self.tsym = 6912 / 12000.0
            self.spacing = 12000 / 6912.0
            self.nsym = 85
            self.sync = JT9_SYNC
            self.ntones = 9

    def candidates(self, x, fs, fmin, fmax, t0=1.0, dt=(-2.0, 3.5), maxc=12, thr=3.0):
        sps = self.tsym * fs
        L = int(round(sps))
        hop = L // 2
        df_bin = 1.0 / self.tsym / 2          # demi-espacement de base (JT9 : espacement / 2)
        nfft = int(round(fs / df_bin))
        nfr = (len(x) - L) // hop
        if nfr < 2 * self.nsym:
            return []
        win = np.hanning(L)
        idx = np.arange(L)[None, :] + hop * np.arange(nfr)[:, None]
        S = np.abs(np.fft.rfft(x[idx] * win, nfft)) ** 2
        S /= np.median(S) + 1e-3 * np.mean(S) + 1e-20
        b0, b1 = int(fmin / df_bin), int(fmax / df_bin)
        ts = np.arange(max(0, int((t0 + dt[0]) * fs / hop)), min(nfr - 2 * self.nsym, int((t0 + dt[1]) * fs / hop)))
        if len(ts) == 0:
            return []
        sy = np.array(self.sync)
        pos = np.nonzero(sy)[0] * 2
        neg = np.nonzero(sy == 0)[0] * 2
        sc = np.zeros((len(ts), b1 - b0))
        for i, t in enumerate(ts):
            rows = S[t + pos, b0:b1].mean(0)
            other = S[t + neg, b0:b1].mean(0)
            sc[i] = rows - other
        sc /= np.std(sc) + 1e-12
        out = []
        for f in np.argsort(sc, axis=None)[::-1][:maxc * 30]:
            i, b = divmod(int(f), b1 - b0)
            if sc[i, b] < thr:
                break
            t, fr = ts[i] * hop / fs, (b0 + b) * df_bin
            if any(abs(t - a) < 0.4 * self.tsym * 4 and abs(fr - c) < 3 * self.spacing for a, c, _ in out):
                continue
            out.append((t, fr, float(sc[i, b])))
            if len(out) >= maxc:
                break
        return out

    def symbols(self, X, N, fs, t, f):
        """Recalage fin (temps 1/16 symbole, fréquence 1/8 d'espacement) -> puissances (nsym, ntones)."""
        bw = (self.ntones + 3) * self.spacing
        dfb = fs / N
        lo = int((f - 2 * self.spacing) / dfb)
        nb = int(bw / dfb)
        if lo < 0:
            return None
        z = _baseband(X, N, lo, nb)
        fsd = nb * dfb
        sps = self.tsym * fsd
        f_rel0 = f - lo * dfb
        sy = np.array(self.sync, bool)
        best = None
        for dfr in np.arange(-0.5, 0.51, 0.125) * self.spacing:
            for dts in np.arange(-0.5, 0.51, 1 / 16):
                st = (t + dts * self.tsym) * fsd
                P = _tone_powers(z, fsd, sps, st, self.nsym, f_rel0 + dfr, self.spacing, 1)
                if np.isnan(P).all():
                    continue
                v = np.nan_to_num(P[:, 0])
                m = v[sy].mean() - v[~sy].mean()
                if best is None or m > best[0]:
                    best = (m, dfr, st)
        if best is None:
            return None
        _, dfr, st = best
        P = _tone_powers(z, fsd, sps, st, self.nsym, f_rel0 + dfr, self.spacing, self.ntones)
        return P, f + dfr, st / fsd


def decode_jt65(P):
    """Puissances (126, 66) -> texte ou None ; tonalité 0 = synchro, k + 2 = donnée k."""
    sy = np.array(NPRC, bool)
    D = P[~sy][:, 2:66]                                     # 63 symboles de données x 64
    valid = ~np.isnan(D).any(1)
    D = np.nan_to_num(D)
    hard = D.argmax(1)
    srt = np.sort(D, 1)
    rel = srt[:, -1] / (srt[:, -2] + 1e-12)
    rel[~valid] = 0
    order = np.argsort(rel)
    sent = [igray(int(v)) for v in hard]                    # Gray inverse
    sent = interleave63(sent, -1)
    perm = interleave63(list(range(63)), -1)                # position désentrelacée -> position reçue
    inv = {p: i for i, p in enumerate(perm)}
    best = None
    nmiss = int((~valid).sum())
    for nera in range(max(nmiss, 0), 51, 3):
        eras = sorted({inv[int(i)] for i in order[:nera]})
        dec, n = rs_decode(sent[:], eras)
        if n < 0:
            continue
        cw = rs_encode(dec)
        agree = sum(a == b for a, b in zip(cw, sent))
        if agree >= 63 - nera - max(0, (51 - nera) // 2) and (best is None or agree > best[1]):
            best = (dec, agree, nera)
            break
    if best is None:
        return None
    dec = best[0]
    if not any(dec):
        return None
    tones = [gray(v) for v in interleave63(rs_encode(dec), 1)]
    q = np.mean(D[np.arange(63)[valid], np.array(tones)[valid]]) / (np.mean(D[valid]) + 1e-20)
    if q < 3.0:                                             # mot de code sans rapport avec le signal reçu
        return None
    return unpackmsg(dec), q


def decode_jt9(P):
    sy = np.array(JT9_SYNC, bool)
    D = np.nan_to_num(P[~sy][:, 1:9])                       # 69 x 8 (tonalité k+1 = Gray k)
    vals = np.array([igray(g) for g in range(8)])
    llr = np.zeros((69, 3))
    lp = np.log(D + 1e-20)
    for b in range(3):
        bit = (vals >> (2 - b)) & 1
        llr[:, b] = lp[:, bit == 1].max(1) - lp[:, bit == 0].max(1)
    llr = llr.ravel()
    llr = llr / (np.std(llr) + 1e-9)
    soft = np.clip(128 + 40 * llr, 0, 255).astype(np.uint8)
    inter = np.append(soft, 128)[:207]
    sym = [int(inter[_J0[i]]) for i in range(206)]
    bits = fano_decode(sym)
    if bits is None:
        return None
    d = [int("".join(map(str, bits[6 * i:6 * i + 6])), 2) for i in range(12)]
    if not any(d):
        return None
    tones = [t for t, s in zip(jt9_tones_from_words(d), JT9_SYNC) if not s]
    q = np.mean(D[np.arange(69), np.array(tones) - 1]) / (np.mean(D) + 1e-20)
    if q < 2.0:
        return None
    return unpackmsg(d), q


def decode_window(x, fs, kind, sub="A", fmin=200.0, fmax=3000.0, t0=1.0, dt=(-2.0, 3.5)):
    mode = JTMode(kind, sub)
    x = np.asarray(x, float)
    out = []
    X = np.fft.fft(x)
    N = len(x)
    seen = set()
    taken = []
    width = (mode.ntones + 1) * mode.spacing
    for t, f, sc in mode.candidates(x, fs, fmin, fmax, t0, dt):
        if any(-width < f - b < width for _, b in taken):
            continue                                        # dans la bande d'un signal déjà décodé
        r = mode.symbols(X, N, fs, t, f)
        if r is None:
            continue
        P, ff, tt = r
        res = decode_jt65(P) if kind == "jt65" else decode_jt9(P)
        if res is None:
            continue
        text = res[0]
        if text in seen or not text.strip():
            continue
        seen.add(text)
        taken.append((t, ff))
        sig = np.nanmean(np.nanmax(P, 1))
        noise = np.nanmedian(P)
        snr = 10 * np.log10(max(sig / max(noise, 1e-20) - 1, 1e-3)) - 10 * np.log10(2500 * mode.tsym)
        out.append({"freq": ff, "dt": tt - t0, "snr": round(float(snr)), "text": text})
    return out


def jt_audio(kind, items, fs=12000, sub="A", amp=0.5, t0=1.0, length=60.0):
    """Mire : [(fréquence de synchro, message)] -> une minute d'audio."""
    mode = JTMode(kind, sub)
    x = np.zeros(int(length * fs))
    for f0, msg in items:
        if kind == "jt65":
            sent = gray_all(interleave63(rs_encode(packmsg(msg)), 1))
            tones, k = [], 0
            for s in NPRC:
                if s:
                    tones.append(0)
                else:
                    tones.append(sent[k] + 2)
                    k += 1
        else:
            tones = jt9_tones(msg)
        n = int(round(mode.tsym * fs * len(tones)))
        idx = np.minimum((np.arange(n) / (mode.tsym * fs)).astype(int), len(tones) - 1)
        fr = f0 + np.array(tones)[idx] * mode.spacing
        ph = 2 * np.pi * np.cumsum(fr) / fs
        s0 = int(t0 * fs)
        x[s0:s0 + n] += amp / len(items) * np.cos(ph)[:len(x) - s0]
    return x


def gray_all(v):
    return [gray(a) for a in v]


class JT(Decoder):
    """Créneaux d'une minute (UTC) ; tout le passe-bande est décodé à la fin de chaque minute."""
    kind = "msg"

    def __init__(self, fs, kind="jt65", sub="A", latency=0.6):
        super().__init__(fs, 1500.0)
        self.mode_kind, self.sub = kind, sub
        self.name = ("JT65" + sub) if kind == "jt65" else "JT9"
        self.latency = latency
        self.buf = []
        self.slot = None
        self.lock = threading.Lock()
        self.results = []
        self.last_count = 0

    def status(self):
        return {"af": 1500.0, "slot": 60, "last": self.last_count}

    def process(self, x):
        slot = int((time.time() - self.latency) // 60)
        if self.slot is None:
            self.slot = slot
        if slot != self.slot:
            audio = np.concatenate(self.buf) if self.buf else np.zeros(0)
            start = self.slot * 60
            self.buf, self.slot = [], slot
            if len(audio) > self.fs * 50:
                threading.Thread(target=self._decode, args=(audio, start), daemon=True).start()
        self.buf.append(np.asarray(x, np.float64))
        with self.lock:
            res, self.results = self.results, []
        return res

    def _decode(self, audio, start):
        try:
            found = decode_window(audio, self.fs, self.mode_kind, self.sub)
        except Exception as e:                          # pragma: no cover
            log.warning("%s : %s", self.name, e)
            found = []
        utc = time.strftime("%H%M", time.gmtime(start))
        msgs = [{"t": "msg", "mode": self.name, "utc": utc, "snr": d["snr"], "dt": round(d["dt"], 1),
                 "freq": int(round(d["freq"])), "text": d["text"]} for d in found]
        self.last_count = len(msgs)
        with self.lock:
            self.results.extend(msgs)
