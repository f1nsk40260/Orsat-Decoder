"""Générateurs de mires pour les modes « utilitaires » (tests des décodeurs uniquement, pas d'émission) :
SITOR-A, Packet AX.25, DTMF, appels sélectifs, DSC, signaux horaires…"""
import numpy as np

from ..tables import CCIR_ALPHA, CCIR_BETA, CCIR_LTRS_SHIFT
from .encoders import fsk, ccir_codes

TAU = 2 * np.pi


def tones_seq(seq, fs, amp=0.5, rise=0.003):
    """seq : liste de (fréquences (tuple, vide = silence), durée s). Phase continue par fréquence."""
    out = []
    r = max(1, int(rise * fs))
    t0 = 0
    for freqs, dur in seq:
        n = int(round(dur * fs))
        t = (t0 + np.arange(n)) / fs
        y = np.zeros(n)
        for f in freqs:
            y += np.cos(TAU * f * t)
        if freqs and n > 2 * r:
            env = np.ones(n)
            ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(r) / r)
            env[:r], env[-r:] = ramp, ramp[::-1]
            y *= env / max(1, len(freqs))
        out.append(amp * y)
        t0 += n
    return np.concatenate(out) if out else np.zeros(0)


# ------------------------------------------------------------------ SITOR-A / AMTOR ARQ
def sitora_encode(text, fs=12000, af=1000.0, shift=170.0, amp=0.5, repeat=(), irs=0.0, idle=4, seed=1):
    """Cycles de 450 ms : bloc de 3 caractères (210 ms), puis 240 ms de silence (réponse de l'IRS, de
    niveau `irs` relatif, si irs > 0). `repeat` : numéros de blocs émis deux fois (demande de répétition)."""
    rng = np.random.default_rng(seed)
    codes = [CCIR_LTRS_SHIFT] + ccir_codes(text)
    while len(codes) % 3:
        codes.append(CCIR_BETA)
    blocks = [(CCIR_BETA, CCIR_ALPHA, CCIR_BETA)] * idle + [tuple(codes[i:i + 3]) for i in range(0, len(codes), 3)]
    blocks += [(CCIR_BETA, CCIR_ALPHA, CCIR_BETA)] * idle
    seq = [(1, 0.3)]
    out = [np.zeros(int(0.3 * fs))]
    controls = [0x2B, 0x35]                      # deux mots valides pour CS1 / CS2 (accusés alternés)
    ack = 0
    for i, b in enumerate(blocks):
        for r in range(2 if i in repeat else 1):
            if not (i in repeat and r == 0):
                ack ^= 1                             # bloc bien reçu : l'IRS change de signal
            seq = []
            for c in b:
                seq += [((c >> j) & 1, 0.01) for j in range(7)]
            out.append(fsk(seq, fs, af + shift / 2, af - shift / 2, amp))
            gap = np.zeros(int(round(0.24 * fs)))
            if irs > 0:
                c = controls[ack]
                cs = fsk([((c >> j) & 1, 0.01) for j in range(7)], fs, af + shift / 2, af - shift / 2, amp * irs)
                a = int(0.085 * fs)
                gap[a:a + len(cs)] = cs
            out.append(gap)
    out.append(np.zeros(int(0.3 * fs)))
    return np.concatenate(out)


# ------------------------------------------------------------------ Packet AX.25
def ax25_bytes(src, dst, info, digis=(), pid=0xF0):
    from ..decoders.packet import crc16_x25

    def addr(call, last, h=False):
        c, _, s = call.partition("-")
        b = bytes((ord(ch) << 1) for ch in c.ljust(6)[:6])
        return b + bytes([0x60 | ((int(s or 0) & 15) << 1) | (0x80 if h else 0) | (1 if last else 0)])

    calls = [dst, src] + list(digis)
    a = b"".join(addr(c.rstrip("*"), i == len(calls) - 1, c.endswith("*")) for i, c in enumerate(calls))
    f = a + bytes([0x03, pid]) + info.encode("latin-1")
    crc = crc16_x25(f)
    return f + bytes([crc & 0xFF, crc >> 8])


def hdlc_bits(frames, flags=30, tail=4):
    """Trames -> bits NRZI (niveaux 0/1) avec fanions et bourrage."""
    raw = []
    flag = [0, 1, 1, 1, 1, 1, 1, 0]
    raw += flag * flags
    for f in frames:
        ones = 0
        for byte in f:
            for i in range(8):
                b = (byte >> i) & 1
                raw.append(b)
                ones = ones + 1 if b else 0
                if ones == 5:
                    raw.append(0)
                    ones = 0
        raw += flag * tail
    lvl, out = 0, []
    for b in raw:
        if b == 0:
            lvl ^= 1
        out.append(lvl)
    return out


def afsk_encode(levels, fs=12000, baud=1200.0, f1=1200.0, f0=2200.0, amp=0.5):
    return fsk([(b, 1.0 / baud) for b in levels], fs, f1, f0, amp)


def packet_encode(frames, fs=12000, baud=1200.0, mark=1200.0, space=2200.0, amp=0.5, gap=0.3):
    out = [np.zeros(int(gap * fs))]
    for f in frames:
        out.append(afsk_encode(hdlc_bits([f]), fs, baud, mark, space, amp))
        out.append(np.zeros(int(gap * fs)))
    return np.concatenate(out)
