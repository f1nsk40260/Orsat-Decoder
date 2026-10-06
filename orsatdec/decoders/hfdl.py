"""HFDL (ARINC 635 / 753) : liaison de données HF des avions et de 16 stations au sol, en USB sur une
sous-porteuse de 1440 Hz, 1800 symboles/s (BPSK, QPSK, 8PSK ; 300 à 1800 bits/s).

Couche physique d'après dumphfdl (Tomasz Lemiesz, GPL-3), réécrite en numpy :
- trame : pré-clé, deux séquences A (127 symboles BPSK), séquence M1 (127, son décalage donne le débit
  et la longueur 1 ou 2 créneaux), M2, 9 séquences d'apprentissage T, puis 72 ou 168 segments de
  30 symboles de données suivis chacun d'une séquence T ;
- recherche de A par corrélation sur une grille d'écarts de fréquence, A1/A2 donnent la fréquence fine ;
- égaliseur LMS fractionné (15 coefficients à 2 échantillons par symbole) appris sur A, M2 et T,
  boucle de phase du second ordre ;
- données : désembrouillage (séquence de 120 symboles), décisions souples, désentrelacement
  (40 lignes), code convolutif K=7 1/2 (répété en 1/4 à 300 bits/s), Viterbi.
Trames : SPDU (squitter des stations), MPDU (montant ou descendant) contenant des LPDU (connexions,
données) et des HFNPDU (données de performance avec la position de l'avion, ACARS…). Chaque PDU est
vérifié par son CRC.
"""
import math
import time

import numpy as np
from scipy.signal import fftconvolve, resample_poly

from ..dsp import Decoder, FirDecim, Mixer, lowpass
from . import acars as acars_mod

SR = 5400.0                      # 3 échantillons par symbole
MF = np.array([-0.0170974647427123, 0.01148231492068473, 0.03138375667422348, 0.009454398851680437,
               -0.04161644170893816, -0.06451564801420356, -0.005495792933327306, 0.1316404671361545,
               0.2759693160697777, 0.3375901874933208, 0.2759693160697777, 0.1316404671361545,
               -0.005495792933327306, -0.06451564801420356, -0.04161644170893816, 0.009454398851680437,
               0.03138375667422348, 0.01148231492068473, -0.0170974647427123])
_A_OCT = [0x5B, 0xBC, 0x74, 0x57, 0x03, 0xD9, 0x89, 0x39, 0xF2, 0x08, 0xD5, 0x36, 0x94, 0x2C, 0x32, 0xFE]
A_BITS = [(o >> (7 - i)) & 1 for o in _A_OCT for i in range(8)][:127]
_M_BITS = [int(c) for c in
           "0111011011110100010110010111110001000000110011011000111001110101110000100110000010101011010010010100"
           "111100100011010100001111111"]
_SHIFTS = [72, 82, 113, 123, 61, 103, 93, 9]
M1 = [[_M_BITS[(s + j) % 127] for j in range(127)] for s in _SHIFTS]
M2 = [[_M_BITS[(s + j) % 127] for j in range(15)] for s in _SHIFTS]
T_SEQ = np.array([1, 1, 1, -1, 1, 1, -1, -1, 1, -1, 1, -1, -1, -1, -1], float)
# M1 -> (bits par symbole, segments de données, rendement 1/n, décalage de colonne du désentrelaceur)
PARAMS = [(1, 72, 4, 17), (1, 72, 2, 17), (2, 72, 2, 17), (3, 72, 2, 17),
          (1, 168, 4, 23), (1, 168, 2, 23), (2, 168, 2, 23), (3, 168, 2, 23)]
PREKEY = 448
FRAME_MAX = 2 * 127 + 127 + 15 + 9 * 15 + 168 * 45

STATIONS = {
    1: ("San Francisco, Californie", 38.38, -121.76, (21934, 17919, 13276, 11327, 10081, 8927, 6559, 5508)),
    2: ("Molokai, Hawaï", 21.18, -157.19, (21937, 17919, 13324, 13312, 13276, 11348, 11312, 10027, 8936, 8912, 6565, 5514)),
    3: ("Reykjavik, Islande", 63.85, -22.46, (17985, 15025, 11184, 8977, 6712, 5720, 3900)),
    4: ("Riverhead, New York", 40.88, -72.64, (21931, 17919, 13276, 11387, 8912, 6661, 5652)),
    5: ("Auckland, Nouvelle-Zélande", -37.02, 174.81, (17916, 13351, 10084, 8921, 6535, 5583)),
    6: ("Hat Yai, Thaïlande", 6.94, 100.39, (21949, 17928, 13270, 10066, 8825, 6535, 5655)),
    7: ("Shannon, Irlande", 52.74, -8.93, (11384, 10081, 8942, 8843, 6532, 5547, 3455, 2998)),
    8: ("Johannesburg, Afrique du Sud", -26.13, 28.21, (21949, 17922, 13321, 11321, 8834, 5529, 4681, 3016)),
    9: ("Barrow, Alaska", 71.26, -156.58, (21937, 21928, 17934, 17919, 11354, 10093, 10027, 8936, 8927, 6646, 5544,
                                          5538, 5529, 4687, 4654, 3497, 3007, 2992, 2944)),
    10: ("Muan, Corée du Sud", 35.03, 126.24, (21931, 17958, 13342, 10060, 8939, 6619, 5502, 2941)),
    11: ("Albrook, Panama", 9.08, -79.37, (17901, 13264, 10063, 8894, 6589, 5589)),
    13: ("Santa Cruz, Bolivie", -17.67, -63.16, (21997, 17916, 13315, 11318, 8957, 6628, 4660)),
    14: ("Krasnoïarsk, Russie", 56.15, 92.58, (21990, 17912, 13321, 10087, 8886, 6596, 5622)),
    15: ("Al Muharraq, Bahreïn", 26.31, 50.47, (21982, 17967, 13312, 10030, 8885, 6646, 5544, 2986)),
    16: ("Agana, Guam", 13.49, 144.83, (21928, 17919, 13312, 11306, 8927, 6652, 5451)),
    17: ("Canaries, Espagne", 27.96, -15.41, (21955, 17928, 13303, 11348, 8948, 6529)),
}
LPDU_TYPES = {0x0D: "données", 0x1D: "données acquittées", 0x2F: "connexion refusée", 0x3F: "déconnexion",
              0x5F: "reprise de connexion confirmée", 0x4F: "reprise de connexion", 0x8F: "demande de connexion",
              0x9F: "connexion confirmée", 0xBF: "demande de connexion (DLS)"}
FREQ_CHANGE = {0: "première recherche du vol", 1: "trop de NACK", 2: "squitters perdus", 3: "HFDL désactivé",
               4: "changement de fréquence de la station", 5: "station ou canal arrêté",
               6: "mauvaise qualité montante", 7: "pas de changement"}


def bpsk(bits):
    return 1.0 - 2.0 * np.asarray(bits, float)


def gray_decode(g):
    b = 0
    while g:
        b ^= g
        g >>= 1
    return b


CONST = {m: np.array([np.exp(2j * np.pi * gray_decode(v) / (1 << m)) for v in range(1 << m)]) for m in (1, 2, 3)}


def scrambler(n=120):
    v, out = 0x4D4B, []
    for _ in range(n):
        b = bin(v & 0x4001).count("1") & 1
        v = ((v << 1) | b) & 0x7FFF
        out.append(b)
    return np.array(out)


SCRAMBLE = scrambler()


def interleave_perm(m1):
    """Ordre de lecture du désentrelaceur : pop[k] = rang d'émission du k-ième bit codé."""
    mod, cnt, _, shift = PARAMS[m1]
    cols = cnt * 30 * mod // 40
    n = cols * 40
    table = np.zeros((40, cols), int)
    r = c = 0
    for j in range(n):
        table[r, c] = j
        r += 1
        if r == 40:
            r = 0
            c += 1
        c -= shift
        if c < 0:
            c += cols
    out = np.empty(n, int)
    r = c = 0
    for k in range(n):
        out[k] = table[r, c]
        r = (r + 9) % 40
        if r == 0:
            c += 1
    return out


_PERM = {}


def perm(m1):
    if m1 not in _PERM:
        _PERM[m1] = interleave_perm(m1)
    return _PERM[m1]


# ---------------------------------------------------------------------------------------- Viterbi K=7
def _par(x):
    return bin(x).count("1") & 1


_POLY = (0x6D, 0x4F)
_NS = np.arange(64)
_P0, _P1 = _NS >> 1, (_NS >> 1) | 32
_EXP0 = np.array([[2 * _par((((p << 1) | (s & 1)) & 0x7F) & q) - 1 for q in _POLY] for p, s in zip(_P0, _NS)], float)
_EXP1 = np.array([[2 * _par((((p << 1) | (s & 1)) & 0x7F) & q) - 1 for q in _POLY] for p, s in zip(_P1, _NS)], float)


def viterbi(llr):
    """llr (> 0 : bit 1), 2 par bit -> bits décodés (état initial et final 0)."""
    n = len(llr) // 2
    L = np.asarray(llr[:2 * n], float).reshape(n, 2)
    met = np.full(64, -1e9)
    met[0] = 0.0
    dec = np.zeros((n, 64), bool)
    for k in range(n):
        l0, l1 = L[k]
        m0 = met[_P0] + _EXP0[:, 0] * l0 + _EXP0[:, 1] * l1
        m1 = met[_P1] + _EXP1[:, 0] * l0 + _EXP1[:, 1] * l1
        d = m1 > m0
        dec[k] = d
        met = np.where(d, m1, m0)
        met -= met.max()
    out = np.zeros(n, np.uint8)
    st = 0
    for k in range(n - 1, -1, -1):
        out[k] = st & 1
        st = (st >> 1) | (32 if dec[k, st] else 0)
    return out


def conv_encode(bits):
    sr, out = 0, []
    for b in bits:
        sr = ((sr << 1) | int(b)) & 0x7F
        out += [_par(sr & _POLY[0]), _par(sr & _POLY[1])]
    return out


def crc16(data):
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0x8408 if c & 1 else c >> 1
    return c ^ 0xFFFF


def fcs_ok(buf, n):
    return len(buf) >= n + 2 and crc16(buf[:n]) == (buf[n] | (buf[n + 1] << 8))


# ---------------------------------------------------------------------------------------- PDU
def _rev(b):
    return int(f"{b:08b}"[::-1], 2)


def icao(buf):
    return (_rev(buf[0]) << 16) | (_rev(buf[1]) << 8) | _rev(buf[2])


def coord(c):
    c &= 0xFFFFF
    if c & 0x80000:
        c -= 0x100000
    return c * 180.0 / 0x7FFFF


def station(gid, ctx=None):
    s = STATIONS.get(gid)
    if ctx is not None and s:
        ctx.gs.add(gid)
    return f"{s[0]} ({gid})" if s else f"station {gid}"


def freqs(gid, mask):
    s = STATIONS.get(gid)
    out = []
    for i in range(20):
        if mask >> i & 1:
            out.append(str(s[3][i]) if s and i < len(s[3]) else f"#{i}")
    return ", ".join(out) + (" kHz" if out and s else "")


def _latlon(lat, lon):
    return f"{abs(lat):.4f}° {'N' if lat >= 0 else 'S'} {abs(lon):.4f}° {'E' if lon >= 0 else 'O'}"


def spdu(buf, ctx=None):
    if not fcs_ok(buf, 64):
        return None
    gid = buf[1] & 0x7F
    note = (buf[0] & 0xC0) >> 6
    gs = [(gid, bool(buf[1] & 0x80), buf[54] >> 4 | buf[55] << 4 | buf[56] << 12),
          (buf[57] & 0x7F, bool(buf[57] & 0x80), buf[58] | buf[59] << 8 | (buf[60] & 0xF) << 16),
          (buf[60] >> 4 | (buf[61] & 0x7) << 4, bool(buf[61] & 0x8), buf[61] >> 4 | buf[62] << 4 | buf[63] << 12)]
    s = f"Squitter {station(gid, ctx)}"
    s += " · " + ("synchro UTC" if gs[0][1] else "sans synchro UTC")
    if note:
        s += " · " + ("", "canal arrêté", "changement de fréquence prochain", "STATION ARRÊTÉE")[note]
    s += f" · fréquences actives : {freqs(gid, gs[0][2]) or 'aucune'}"
    others = [f"{station(g)} : {freqs(g, m)}" for g, _, m in gs[1:] if g and m]
    if others:
        s += "\n    autres stations : " + " ; ".join(others)
    return s


class AircraftCache:
    """Identités des avions connectés (ICAO) et, pour la trame en cours, positions et stations vues."""

    def __init__(self):
        self.ids = {}
        self.gid = 0
        self.cur = ""
        self.pos = []
        self.gs = set()

    def position(self, lat, lon, flight, tt):
        if -90 <= lat <= 90 and -180 <= lon <= 180 and (abs(lat) > 0.01 or abs(lon) > 0.01):
            self.pos.append({"lat": round(lat, 5), "lon": round(lon, 5), "flight": flight, "ac": self.cur,
                             "time": f"{tt // 3600:02d}:{tt % 3600 // 60:02d}:{tt % 60:02d}"})

    def name(self, gid, ac):
        ic = self.ids.get((gid, ac))
        return f"avion {ac}" + (f" (ICAO {ic:06X})" if ic is not None else "")


def hfnpdu(buf, up, ctx=None):
    if len(buf) < 2 or buf[0] != 0xFF:
        return "données " + buf.hex() if buf else ""
    t = buf[1]
    if t == 0xD1 and len(buf) >= 47:
        fid = bytes(buf[2:8]).decode("latin-1").strip()
        lat = coord(buf[8] | buf[9] << 8 | (buf[10] & 0xF) << 16)
        lon = coord((buf[10] & 0xF0) >> 4 | buf[11] << 4 | buf[12] << 12)
        tt = 2 * (buf[13] | buf[14] << 8)
        gid = buf[17] & 0x7F
        if ctx is not None:
            ctx.position(lat, lon, fid, tt)
        return (f"données de performance · vol {fid} · {_latlon(lat, lon)} à {tt // 3600:02d}:{tt % 3600 // 60:02d}:"
                f"{tt % 60:02d} UTC · {station(gid)}, {freqs(gid, 1 << buf[18])}"
                f" · changement de fréquence : {FREQ_CHANGE.get(buf[46] & 0xF, '?')}")
    if t == 0xD5 and len(buf) >= 15:
        fid = bytes(buf[2:8]).decode("latin-1").strip()
        lat = coord(buf[8] | buf[9] << 8 | (buf[10] & 0xF) << 16)
        lon = coord((buf[10] & 0xF0) >> 4 | buf[11] << 4 | buf[12] << 12)
        tt = 2 * (buf[13] | buf[14] << 8)
        if ctx is not None:
            ctx.position(lat, lon, fid, tt)
        props = []
        for p in range(15, len(buf) - 5, 6):
            g = buf[p] & 0x7F
            m = buf[p + 1] | buf[p + 2] << 8 | (buf[p + 3] & 0xF) << 16
            if g and m:
                props.append(f"{station(g)} {freqs(g, m)}")
        return (f"données de fréquence · vol {fid} · {_latlon(lat, lon)} à {tt // 3600:02d}:{tt % 3600 // 60:02d}:"
                f"{tt % 60:02d} UTC" + ("\n    entendues : " + " ; ".join(props) if props else ""))
    if t == 0xD0:
        return "table système (partielle)"
    if t == 0xD2:
        return "demande de table système"
    if t == 0xDE:
        return "écho différé"
    if t == 0xFF:
        a = bytes(buf[2:])
        if a[:1] == b"\x01":
            body = a[1:]
            end = next((i for i, c in enumerate(body) if c in (0x83, 0x97, 0x03, 0x17)), len(body) - 1)
            blk = bytearray(body[:end + 1])
            if blk and blk[-1] in (0x03, 0x17):
                blk[-1] |= 0x80
            m = acars_mod.parse(blk)
            if m:
                return acars_mod.describe(m)
        return "données enveloppées " + a.hex()
    return f"HFNPDU 0x{t:02X}"


def lpdu(buf, up, ctx):
    if len(buf) < 3 or not fcs_ok(buf, len(buf) - 2):
        return None
    b = buf[:-2]
    t = b[0]
    name = LPDU_TYPES.get(t, f"LPDU 0x{t:02X}")
    if t in (0x0D, 0x1D):
        return hfnpdu(b[1:], up, ctx)
    if t in (0x9F, 0x5F) and len(b) >= 5:
        ic, ac = icao(b[1:4]), b[4]
        ctx.ids[(ctx.gid, ac)] = ic
        return f"{name} : ICAO {ic:06X} -> avion {ac}"
    if t in (0x8F, 0xBF, 0x4F) and len(b) >= 4:
        return f"{name} : ICAO {icao(b[1:4]):06X}"
    if t in (0x2F, 0x3F) and len(b) >= 5:
        return f"{name} : ICAO {icao(b[1:4]):06X}, motif {b[4]}"
    return name


def mpdu(buf, ctx):
    """-> liste de lignes, ou None si le CRC de l'en-tête est faux."""
    if buf[0] & 2:                                        # descendant (avion -> sol)
        n = (buf[0] >> 2) & 0xF
        h = 6 + n
        if not fcs_ok(buf, h):
            return None
        gid, ac = buf[1] & 0x7F, buf[2]
        ctx.gid = gid
        ctx.cur = ctx.name(gid, ac)
        head = f"{ctx.cur} -> {station(gid, ctx)}"
        sizes = [buf[6 + j] + 1 for j in range(n)]
        groups = [(head, sizes)]
        up = False
    else:                                                 # montant (sol -> avions)
        nac = ((buf[0] & 0x70) >> 4) + 1
        h = 2
        hdr = []
        for _ in range(nac):
            if len(buf) < h + 2:
                return None
            k = buf[h + 1] >> 4
            hdr.append((buf[h], [buf[h + 2 + j] + 1 for j in range(k)] if len(buf) >= h + 2 + k else []))
            h += 2 + k
        if not fcs_ok(buf, h):
            return None
        gid = buf[1] & 0x7F
        ctx.gid = gid
        groups = [(f"{station(gid, ctx)} -> {ctx.name(gid, ac)}", sizes) for ac, sizes in hdr]
        up = True
    out = []
    p = h + 2
    for head, sizes in groups:
        parts = []
        for sz in sizes:
            if p + sz > len(buf):
                break
            r = lpdu(buf[p:p + sz], up, ctx)
            p += sz
            if r:
                parts.append(r)
        out.append(head + (" · " + "\n    ".join(parts) if parts else ""))
    return out


# ---------------------------------------------------------------------------------------- démodulation
def _template():
    t = np.zeros(127 * 3)
    t[::3] = bpsk(A_BITS)
    return np.convolve(t, MF)


TEMPLATE = _template()


def demod_frame(y, i0, f, ph0):
    """y : sortie du filtre adapté à 5400 Hz ; i0 : début de A1. -> (M1, symboles de données, erreurs T) ou None."""
    n0 = i0 + 9
    tt = np.arange(len(y)) / SR
    nmax = len(y) - n0 - 20
    if nmax < 600:
        return None
    pos = n0 + 1.5 * np.arange(int(nmax / 1.5))
    yr = y * np.exp(-2j * np.pi * f * tt)
    idx = np.arange(len(y))
    s = np.interp(pos, idx, yr.real) + 1j * np.interp(pos, idx, yr.imag)
    s *= np.exp(-1j * ph0)
    s /= np.sqrt(np.mean(np.abs(s[:2 * 254]) ** 2)) + 1e-12
    s = np.concatenate([np.zeros(7, complex), s])
    st = {"w": np.zeros(15, complex), "th": 0.0, "dth": 0.0}
    st["w"][7] = 1.0
    nsym = (len(s) - 15) // 2

    def step(k, d=None, mod=1, upd=True):
        if k >= nsym:
            return None
        x = s[2 * k:2 * k + 15][::-1]
        w = st["w"]
        rot = np.exp(-1j * st["th"])
        yv = np.dot(w, x) * rot
        if d is None:
            C = CONST[mod]
            d = C[np.argmin(np.abs(C - yv))]
        if upd:
            e = (d - yv) / rot
            w += 0.05 * e * np.conj(x) / (np.vdot(x, x).real + 1e-9)
        pe = np.angle(yv * np.conj(d))
        st["th"] += 0.08 * pe + st["dth"]
        st["dth"] += 0.002 * pe
        return yv

    ref = np.concatenate([bpsk(A_BITS), bpsk(A_BITS)])
    for k in range(254):
        step(k, ref[k])
    k = 254
    m1s = [step(k + j) for j in range(127)]
    if any(v is None for v in m1s):
        return None
    k += 127
    hard = (np.real(m1s) < 0).astype(int)
    corr = [abs(2 * np.mean(hard == np.array(M1[i])) - 1) for i in range(8)]
    m1 = int(np.argmax(corr))
    if corr[m1] < 0.3:
        return None
    mod, cnt, _, _ = PARAMS[m1]
    for j, d in enumerate(bpsk(M2[m1])):
        step(k + j, d)
    k += 15
    terr = 0
    for _ in range(9):
        for j in range(15):
            yv = step(k + j, T_SEQ[j])
            if yv is None:
                return None
            terr += np.sign(yv.real) != T_SEQ[j]
        k += 15
    data = np.empty(cnt * 30, complex)
    for sg in range(cnt):
        for j in range(30):
            yv = step(k + j, None, mod, upd=False)
            if yv is None:
                return None
            data[sg * 30 + j] = yv
        k += 30
        for j in range(15):
            yv = step(k + j, T_SEQ[j])
            if yv is None:
                return None
            terr += np.sign(yv.real) != T_SEQ[j]
        k += 15
    return m1, data, int(terr), k


def soft_bits(data, mod):
    """Symboles -> LLR (> 0 : bit 1), bit de poids fort d'abord (comme liquid-dsp)."""
    C = CONST[mod]
    d2 = np.abs(data[:, None] - C[None, :]) ** 2
    out = np.empty((len(data), mod))
    vals = np.arange(1 << mod)
    sig2 = max(np.mean(np.min(d2, axis=1)), 0.02)
    for b in range(mod):
        bit = (vals >> (mod - 1 - b)) & 1
        out[:, b] = (d2[:, bit == 0].min(axis=1) - d2[:, bit == 1].min(axis=1)) / sig2
    return out.ravel()


def decode_bits(m1, data):
    mod, cnt, rate, _ = PARAMS[m1]
    s = np.tile(SCRAMBLE, len(data) // 120 + 1)[:len(data)]
    llr = soft_bits(data * np.where(s, -1, 1), mod)
    p = perm(m1)
    coded = llr[p]
    if rate == 4:
        coded = (coded[0::2] + coded[1::2]) / 2
    bits = viterbi(coded)
    nb = len(bits) // 8
    return bytes(int(np.dot(bits[8 * i:8 * i + 8], 1 << np.arange(8))) for i in range(nb))


def decode_pdu(buf, ctx):
    if not buf:
        return None
    if buf[0] & 1:
        return mpdu(buf, ctx)
    r = spdu(buf, ctx)
    return [r] if r else None


# ---------------------------------------------------------------------------------------- décodeur
class HFDL(Decoder):
    name = "HFDL"
    kind = "msg"

    def __init__(self, fs, af=1440.0, span=150.0):
        super().__init__(fs, af)
        self.mix = Mixer(af, fs)
        self.D = 2 if fs >= 11000 else 1
        self.fs2 = fs / self.D
        self.dec = FirDecim(lowpass(fs, 1300.0, 101), self.D)
        self.buf = np.zeros(0, complex)
        self.base = 0                     # indice absolu (fs2) du premier échantillon de buf
        self.scan = 0                     # indice absolu à partir duquel chercher
        self.next_scan = 0
        self.span = span
        self.ctx = AircraftCache()
        self.ctx.gid = 0
        self.count = 0
        self.frames = 0
        self.last_rate = ""

    def set_af(self, af):
        super().set_af(af)
        self.mix.freq = float(af)

    def status(self):
        st = {"af": round(self.af, 1), "last": self.count}
        if self.frames:
            st["info"] = f"{self.frames} trames, dernière {self.last_rate}"
        return st

    def _to5400(self, z):
        if abs(self.fs2 - 6000.0) < 1:
            y = resample_poly(z, 9, 10)
        else:
            y = resample_poly(z, int(SR), int(self.fs2))
        return np.convolve(y, MF, "same")

    def process(self, x):
        z = self.dec.process(self.mix.process(np.asarray(x, np.float64)))
        self.buf = np.concatenate([self.buf, z])
        end = self.base + len(self.buf)
        out = []
        if end >= self.next_scan:
            self.next_scan = end + int(0.5 * self.fs2)
            self._scan(out)
        keep = max(self.scan - int(1.0 * self.fs2), end - int(14 * self.fs2))
        if keep > self.base:
            self.buf = self.buf[keep - self.base:]
            self.base = keep
        return out

    def _scan(self, out):
        r = self.fs2 / SR
        while True:
            a = max(self.scan, self.base)
            seg = self.buf[a - self.base:]
            need = (127 * 2 + 40) * 3 * r
            if len(seg) < need + 200:
                return
            y = self._to5400(seg)
            hit = self._find(y)
            if hit is None:
                self.scan = self.base + len(self.buf) - int((127 * 2 + 60) * 3 * r)
                return
            i, f = hit
            frame_end = i + (FRAME_MAX + 60) * 3
            if frame_end > len(y):
                self.scan = a + int(max(i - 60, 0) * r)       # attendre la fin de la trame
                return
            res = self._frame(y, i, f, out)
            self.scan = a + int((i + (res if res else 2 * 127 * 3)) * r)

    def _find(self, y):
        tt = np.arange(len(y)) / SR
        nt = len(TEMPLATE)
        nrm = np.sqrt(np.convolve(np.abs(y) ** 2, np.ones(nt), "valid")) * np.linalg.norm(TEMPLATE) + 1e-12
        lim = len(nrm) - 381
        if lim <= 0:
            return None
        smax = np.zeros(lim)
        farg = np.zeros(lim)
        for f in np.arange(-self.span, self.span + 0.1, 4.0):
            c = np.abs(fftconvolve(y * np.exp(-2j * np.pi * f * tt), np.conj(TEMPLATE[::-1]), "valid")) / nrm
            sc = np.minimum(c[:lim], c[381:381 + lim])        # A1 et A2, 127 symboles plus loin
            better = sc > smax
            smax[better] = sc[better]
            farg[better] = f
        over = np.nonzero(smax > 0.3)[0]
        if len(over) == 0:
            return None
        j0 = int(over[0])
        j = j0 + int(np.argmax(smax[j0:j0 + 30]))
        return j, float(farg[j])

    def _frame(self, y, i, f, out):
        tt = np.arange(len(y)) / SR
        lo, hi = max(i - 30, 0), i + 30 + 381 + len(TEMPLATE)
        best = None
        for ff in np.arange(f - 3.0, f + 3.01, 0.5):
            c = fftconvolve(y[lo:hi] * np.exp(-2j * np.pi * ff * tt[lo:hi]), np.conj(TEMPLATE[::-1]), "valid")
            j = int(np.argmax(np.abs(c[:61])))
            if best is None or abs(c[j]) > abs(best[0]):
                best = (c[j], ff, lo + j, c[j + 381] if j + 381 < len(c) else 0)
        c1, ff, i0, c2 = best
        df = np.angle(c2 * np.conj(c1)) / (2 * np.pi * 381 / SR) if abs(c2) > 0 else 0.0
        fine = ff + df
        r = demod_frame(y[:i0 + (FRAME_MAX + 40) * 3], i0, fine, np.angle(c1))
        if r is None:
            return None
        m1, data, terr, nsym = r
        self.ctx.pos, self.ctx.gs, self.ctx.cur = [], set(), ""
        try:
            buf = decode_bits(m1, data)
            lines = decode_pdu(buf, self.ctx)
        except (IndexError, ValueError):
            lines = None
        self.frames += 1
        mod, cnt, rate, _ = PARAMS[m1]
        bps = int(1800 * mod / rate * 30 / 45)
        self.last_rate = f"{bps} bits/s, {'1 créneau' if cnt == 72 else '2 créneaux'}"
        if lines:
            utc = time.strftime("%H%M%S", time.gmtime())
            for ln in lines:
                self.count += 1
                out.append({"t": "msg", "mode": "HFDL", "utc": utc, "freq": int(round(self.af + fine)),
                            "text": f"{ln}  [{bps} bits/s]"})
            # pour la carte : positions d'avions et stations au sol de la trame
            out[-1]["pos"] = self.ctx.pos
            out[-1]["gs"] = [{"id": g, "name": STATIONS[g][0], "lat": STATIONS[g][1], "lon": STATIONS[g][2]}
                             for g in sorted(self.ctx.gs)]
        return (nsym + 10) * 3


def frame_symbols(pdu, m1):
    """Mire de test : PDU (octets) -> symboles complexes de la trame entière (pré-clé comprise)."""
    mod, cnt, rate, _ = PARAMS[m1]
    nsym = cnt * 30
    ncoded = nsym * mod
    nbits = ncoded // rate
    bits = [(b >> j) & 1 for b in pdu for j in range(8)]
    bits = (bits + [0] * nbits)[:nbits - 6] + [0] * 6
    coded = conv_encode(bits)
    if rate == 4:
        coded = [c for c in coded for _ in (0, 1)]
    p = perm(m1)
    tx = np.zeros(ncoded, int)
    tx[p] = coded
    syms = []
    C = CONST[mod]
    for i in range(nsym):
        v = 0
        for b in tx[i * mod:(i + 1) * mod]:
            v = (v << 1) | int(b)
        syms.append(C[v] * (-1 if SCRAMBLE[i % 120] else 1))
    data = np.array(syms)
    pre = [np.ones(PREKEY), bpsk(A_BITS), bpsk(A_BITS), bpsk(M1[m1]), bpsk(M2[m1])] + [T_SEQ] * 9
    seq = list(np.concatenate(pre).astype(complex))
    for sg in range(cnt):
        seq += list(data[sg * 30:(sg + 1) * 30]) + list(T_SEQ)
    return np.array(seq)
