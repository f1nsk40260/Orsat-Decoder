"""Mires PSK de fldigi au-delà du BPSK : QPSK (K=5) et PSK-R (K=7 + entrelaceur), pour les tests."""
import numpy as np

from ..tables import VARICODE
from ..decoders.ifk_common import ConvEncoder, Interleaver
from ..decoders.ifk_tables import MFSK_VARICODE

TAU = 2 * np.pi


def modulate(deltas, fs, af, baud, amp=0.5):
    """Suite de sauts de phase (rad) -> signal, transitions en cosinus surélevé comme fldigi."""
    sps = fs / baud
    n = int(len(deltas) * sps)
    base = np.zeros(n, np.complex128)
    ph = 0.0
    prev = 1.0 + 0j
    for k, dlt in enumerate(deltas):
        a, e = int(round(k * sps)), int(round((k + 1) * sps))
        ph += dlt
        new = np.exp(1j * ph)
        w = 0.5 - 0.5 * np.cos(np.pi * np.arange(e - a) / (e - a))
        base[a:e] = prev * (1 - w) + new * w
        prev = new
    t = np.arange(n) / fs
    return amp * np.real(base * np.exp(1j * TAU * af * t))


def qpsk_encode(text, fs=12000, af=1000.0, baud=31.25, amp=0.5, pre=64, post=64):
    enc = ConvEncoder(5, 0x17, 0x19)
    bits = [0] * pre
    for ch in text:
        c = ord(ch)
        if c < 128:
            bits += [int(b) for b in VARICODE[c]] + [0, 0]
    bits += [0] * post
    deltas = []
    for b in bits:
        s = enc.encode(b) & 3
        sp = (4 - s) & 3
        deltas.append(np.pi + sp * np.pi / 2)
    return modulate(deltas, fs, af, baud, amp)


def pskr_encode(text, fs=12000, af=1000.0, baud=125.0, depth=40, amp=0.5, pre=64, post=None):
    enc = ConvEncoder(7, 0x6d, 0x4f)
    inlv = Interleaver(2, depth)
    bits = [1, 0] * (pre // 2)
    for ch in text:
        bits += [int(b) for b in MFSK_VARICODE[ord(ch)]]
    bits += [0] * (post if post is not None else 4 * depth + 40)
    deltas = []
    for b in bits:
        v = enc.encode(b)                      # bit 0 : polynôme 1, bit 1 : polynôme 2
        v = inlv.bits(v)
        for tb in (v & 1, (v >> 1) & 1):       # bit de poids faible émis le premier
            deltas.append(0.0 if tb else np.pi)
    return modulate(deltas, fs, af, baud, amp)
