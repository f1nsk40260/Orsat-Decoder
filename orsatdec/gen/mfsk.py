"""Générateurs de mires MFSK, DominoEX et THOR, conformes à la partie émission de fldigi
(mfsk.cxx, dominoex.cxx, thor.cxx) : préambule, STX/EOT, codage convolutif, entrelaceur, Varicode,
codage de Gray (MFSK) ou IFK+ (DominoEX/THOR). Phase continue, comme fldigi.

Chaque fonction renvoie un tableau float64 (±amp) à fs Hz, centré sur af Hz.
Les fonctions *_tones() donnent la suite des numéros de tonalité (pour la comparaison avec fldigi).
"""
import numpy as np

from ..decoders.ifk_common import (MFSK_MODES, DOMINO_MODES, THOR_MODES, IFK_TONES, K7, K15,
                                   ConvEncoder, Interleaver, gray_tx)
from ..decoders.ifk_tables import MFSK_VARICODE, DOMINO_VARICODE, THOR_SEC_VARICODE

TAU = 2 * np.pi


def synth(tones, offsets, symdur, fs, af, amp=0.5, drift=0.0):
    """Tonalités successives à phase continue. offsets[t] : écart (Hz) de la tonalité t au centre af.
    drift : dérive linéaire en Hz/s."""
    tones = np.asarray(tones, int)
    n = int(round(len(tones) * symdur * fs))
    t = np.arange(n) / fs
    k = np.minimum((t / symdur).astype(int), len(tones) - 1)
    f = af + np.asarray(offsets)[tones[k]] + drift * t
    ph = TAU * np.cumsum(f) / fs
    return amp * np.cos(ph)


def _chars(text):
    return [b for b in text.encode("latin-1", "replace")]


# ------------------------------------------------------------------ MFSK
def mfsk_tones(text, mode="MFSK16"):
    sr, symlen, symbits, depth, ntones, preamble = MFSK_MODES[mode]
    enc = ConvEncoder(*K7)
    inlv = Interleaver(symbits, depth)
    out = []
    st = {"reg": 0, "n": 0}

    def sendbit(bit, send=True, data=None):
        if data is None:
            data = enc.encode(bit)
        for i in range(2):
            st["reg"] = (st["reg"] << 1) | ((data >> i) & 1)
            st["n"] += 1
            if st["n"] == symbits:
                v = inlv.bits(st["reg"])
                if send:
                    out.append(gray_tx(v & (ntones - 1)))
                st["n"], st["reg"] = 0, 0

    def sendchar(c):
        for b in MFSK_VARICODE[c]:
            sendbit(b == "1")

    d0 = enc.encode(0)                                # clearbits()
    for _ in range(preamble):
        sendbit(0, send=False, data=d0)
    if mode not in ("MFSK64L", "MFSK128L"):
        for _ in range(preamble // 3):
            sendbit(0)
    for c in (13, 2, 13):
        sendchar(c)
    for c in _chars(text):
        sendchar(c)
    for c in (13, 4, 13):
        sendchar(c)
    sendbit(1)                                        # flushtx(preamble)
    for _ in range(preamble):
        sendbit(0)
    return out


def mfsk_encode(text, fs=12000, af=1500.0, mode="MFSK16", amp=0.5, drift=0.0, reverse=False):
    sr, symlen, symbits, depth, ntones, preamble = MFSK_MODES[mode]
    sp = sr / symlen
    tones = mfsk_tones(text, mode)
    if reverse:
        tones = [ntones - 1 - t for t in tones]
    offs = (np.arange(ntones) - (ntones - 1) / 2) * sp
    return synth(tones, offs, symlen / sr, fs, af, amp, drift)


# ------------------------------------------------------------------ IFK+ (DominoEX, THOR)
class _IFK:
    def __init__(self):
        self.prev = 0
        self.out = []

    def sendsymbol(self, sym):
        tone = (self.prev + 2 + sym) % IFK_TONES
        self.prev = tone
        self.out.append(tone)


_SEC2PRI = {}


def _sec2pri_init():
    groups = [("A", [0xc0, 0xc1, 0xc2, 0xc3, 0xc4, 0xc5, 0xe0, 0xe1, 0xe2, 0xe3, 0xe4, 0xe5]), ("B", [0xdf]),
              ("C", [0xc7, 0xe7, 0xa9]), ("D", [0xd0, 0xb0]),
              ("E", [0xc6, 0xe6, 0xc8, 0xc9, 0xca, 0xcb, 0xe8, 0xe9, 0xea, 0xeb]), ("F", [0x192]),
              ("I", [0xcc, 0xcd, 0xce, 0xcf, 0xec, 0xed, 0xee, 0xef, 0xa1]), ("L", [0xa3]), ("N", [0xd1, 0xf1]),
              ("O", [0xf4, 0xf6, 0xf2, 0xd6, 0xf3, 0xd3, 0xd4, 0xd2, 0xf5, 0xd5]), ("R", [0xae]),
              ("U", [0xd9, 0xda, 0xdb, 0xdc, 0xf9, 0xfa, 0xfb, 0xfc]), ("X", [0xd7]), ("Y", [0xff, 0xfd, 0xdd]),
              ("0", [0xd8]), ("1", [0xb9]), ("2", [0xb2]), ("3", [0xb3]), ("?", [0xbf]), ("!", [0xa1]),
              ("<", [0xab]), (">", [0xbb]), ("{", [ord("(")]), ("}", [ord(")")]), ("|", [ord("\\")])]
    for k, vals in groups:
        for v in vals:
            _SEC2PRI[v] = ord(k)


def mupsk_sec2pri(c):
    """MuPskSec2Pri de fldigi : caractère secondaire -> code primaire réservé (DominoEX FEC)."""
    if not _SEC2PRI:
        _sec2pri_init()
    if ord("a") <= c <= ord("z"):
        c -= 32
    c = _SEC2PRI.get(c, c)
    o = ord
    if o("A") <= c <= o("Z"):
        return c - o("A") + 127
    if o("0") <= c <= o("9"):
        return c - o("0") + 14
    if o(" ") <= c <= o('"'):
        return c - o(" ") + 1
    if c == o("_"):
        return 4
    if o("$") <= c <= o("&"):
        return c - o("$") + 5
    if o("'") <= c <= o("*"):
        return c - o("'") + 9
    if o("+") <= c <= o("/"):
        return c - o("+") + 24
    if o(":") <= c <= o("<"):
        return c - o(":") + 29
    if o("=") <= c <= o("@"):
        return c - o("=") + 153
    if o("[") <= c <= o("]"):
        return c - o("[") + 157
    return o("_")


def dominoex_tones(text, mode="DominoEX 11", fec=False, secondary=""):
    micro = mode == "DominoEX Micro"
    ifk = _IFK()
    enc = ConvEncoder(*K7)
    inlv = Interleaver(4, 4)
    st = {"reg": 0, "n": 0}

    def push(data, send=True):
        for i in range(2):
            st["reg"] = (st["reg"] << 1) | ((data >> i) & 1)
            st["n"] += 1
            if st["n"] == 4:
                v = inlv.bits(st["reg"])
                if send:
                    ifk.sendsymbol(v)
                st["n"], st["reg"] = 0, 0

    def sendchar(c, sec):
        if fec:
            if sec:
                c = mupsk_sec2pri(c)
            else:
                if c == 10:
                    return
                if 1 <= c <= 7 or 9 <= c <= 12 or 14 <= c <= 31 or 127 <= c <= 159:
                    c = ord("_")
            for b in MFSK_VARICODE[c]:
                push(enc.encode(b == "1"))
        else:
            code = DOMINO_VARICODE[c + (256 if sec else 0)]
            ifk.sendsymbol(code[0])
            for s in code[1:]:
                if s & 8:
                    ifk.sendsymbol(s)
                else:
                    break

    if fec:                                           # MuPskClearbits()
        d0 = enc.encode(0)
        for _ in range(100):
            push(d0, send=False)
    sendchar(0, 1)                                    # sendidle()
    for c in ((13,) if micro else (13, 2, 13)):
        sendchar(c, 0)
    for c in _chars(text):
        sendchar(c, 0)
    for c in _chars(secondary):                       # texte secondaire (émis à l'arrêt du clavier)
        sendchar(c, 1)
    for c in ((13,) if micro else (13, 4, 13)):
        sendchar(c, 0)
    for _ in range(4):                                # flushtx()
        sendchar(0, 1)
    return ifk.out


def _ifk_signal(tones, sr, symlen, ds, fs, af, amp, drift, reverse):
    sp = sr * ds / symlen
    if reverse:
        tones = [IFK_TONES - 1 - t for t in tones]
    offs = (np.arange(IFK_TONES) + 0.5 - IFK_TONES / 2) * sp
    return synth(tones, offs, symlen / sr, fs, af, amp, drift)


def dominoex_encode(text, fs=12000, af=1500.0, mode="DominoEX 11", fec=False, secondary="", amp=0.5,
                    drift=0.0, reverse=False):
    sr, symlen, ds = DOMINO_MODES[mode]
    return _ifk_signal(dominoex_tones(text, mode, fec, secondary), sr, symlen, ds, fs, af, amp, drift, reverse)


# ------------------------------------------------------------------ THOR
def thor_tones(text, mode="THOR 16", secondary=""):
    sr, symlen, ds, depth, flushlength, k15 = THOR_MODES[mode]
    micro = mode == "THOR Micro"
    ifk = _IFK()
    enc = ConvEncoder(*(K15 if k15 else K7))
    inlv = Interleaver(4, depth)
    st = {"reg": 0, "n": 0}

    def push(data, send=True):
        for i in range(2):
            st["reg"] = (st["reg"] << 1) | ((data >> i) & 1)
            st["n"] += 1
            if st["n"] == 4:
                v = inlv.bits(st["reg"])
                if send:
                    ifk.sendsymbol(v)
                st["n"], st["reg"] = 0, 0

    def sendchar(c, sec):
        if sec and 32 <= c <= 122:
            code = THOR_SEC_VARICODE[c - 32]
        elif sec:
            code = MFSK_VARICODE[0]
        else:
            code = MFSK_VARICODE[c]
        for b in code:
            push(enc.encode(b == "1"))

    d0 = enc.encode(0)                                # Clearbits()
    for _ in range(1400):
        push(d0, send=False)
    for _ in range(16):
        ifk.sendsymbol(0)
    sendchar(0, 0)                                    # sendidle()
    for c in ((13,) if micro else (13, 2, 13)):
        sendchar(c, 0)
    for c in _chars(text):
        sendchar(c, 0)
    for c in _chars(secondary):
        sendchar(c, 1)
    for c in ((13,) if micro else (13, 4, 13)):
        sendchar(c, 0)
    for _ in range(flushlength):                      # flushtx()
        sendchar(0, 0)
    return ifk.out


def thor_encode(text, fs=12000, af=1500.0, mode="THOR 16", secondary="", amp=0.5, drift=0.0, reverse=False):
    sr, symlen, ds, depth, flushlength, k15 = THOR_MODES[mode]
    return _ifk_signal(thor_tones(text, mode, secondary), sr, symlen, ds, fs, af, amp, drift, reverse)
