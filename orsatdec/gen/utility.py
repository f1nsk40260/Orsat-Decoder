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


# ------------------------------------------------------------------ tonalités
def dtmf_encode(digits, fs=12000, on=0.07, off=0.06, amp=0.5, twist=1.0):
    from ..decoders.tones import DTMF_LOW, DTMF_HIGH, DTMF_KEYS
    seq = [((), 0.3)]
    for d in digits:
        r = next(i for i, row in enumerate(DTMF_KEYS) if d in row)
        c = DTMF_KEYS[r].index(d)
        seq += [((DTMF_LOW[r], DTMF_HIGH[c]), on), ((), off)]
    seq.append(((), 2.0))
    return tones_seq(seq, fs, amp)


def selcall_encode(digits, standard="ZVEI1", fs=12000, amp=0.5):
    from ..decoders.tones import SELCALL, SYMS
    tab, dur = SELCALL[standard]
    seq, prev = [((), 0.3)], None
    for d in digits:
        k = SYMS.index(d)
        if d == prev:
            k = SYMS.index("E")                  # ton de répétition
            prev = None
        else:
            prev = d
        seq.append(((tab[k],), dur))
    seq.append(((), 1.0))
    return tones_seq(seq, fs, amp, rise=0.002)


def icao_selcal_encode(code, fs=12000, amp=0.5):
    from ..decoders.tones import ICAO_TONES
    a, b = code.split("-")
    seq = [((), 0.4), ((ICAO_TONES[a[0]], ICAO_TONES[a[1]]), 1.0), ((), 0.2),
           ((ICAO_TONES[b[0]], ICAO_TONES[b[1]]), 1.0), ((), 2.5)]
    return tones_seq(seq, fs, amp)


# ------------------------------------------------------------------ DSC / CCIR 493-4
def dsc_symbol_bits(v):
    info = [(v >> i) & 1 for i in range(7)]
    z = 7 - sum(info)
    return info + [(z >> 2) & 1, (z >> 1) & 1, z & 1]


def dsc_message(fmt, fields, eos=127):
    """Caractères d'un appel (format, champs, EOS) et son ECC."""
    msg = [fmt] + list(fields) + [eos]
    ecc = 0
    for c in msg:
        ecc ^= c
    return msg, ecc


def dsc_encode(fmt, fields, eos=127, fs=12000, af=1700.0, baud=100.0, shift=170.0, dots=None, amp=0.5, invert=False):
    msg, ecc = dsc_message(fmt, fields, eos)
    dx = [125] * 6 + [fmt] + msg + [ecc, eos, eos]
    rx = list(range(111, 103, -1)) + [fmt] + msg + [ecc, eos, eos]
    slots = []
    for p in range(len(dx) + 2):
        slots.append(dx[p] if p < len(dx) else 126)
        slots.append(rx[p] if p < len(rx) else 126)
    bits = [1, 0] * ((dots or (100 if baud < 300 else 10)))
    for v in slots:
        bits += dsc_symbol_bits(v)
    if invert:
        bits = [1 - b for b in bits]
    lo, hi = af - shift / 2, af + shift / 2
    sig = fsk([(b, 1.0 / baud) for b in bits], fs, lo, hi, amp)        # 1 = tonalité basse
    return np.concatenate([np.zeros(int(0.3 * fs)), sig, np.zeros(int(0.5 * fs))])


# ------------------------------------------------------------------ POCSAG
def pocsag_cw(data21):
    from ..decoders.pocsag import BCH_POLY
    r = data21 << 10
    for i in range(30, 9, -1):
        if r & (1 << i):
            r ^= BCH_POLY << (i - 10)
    cw = (data21 << 10) | (r & 0x3FF)
    return (cw << 1) | (bin(cw).count("1") & 1)


def pocsag_bits(messages, preamble=576):
    """messages : liste de (ric, fonction, texte, alphanumérique ?)."""
    from ..decoders.pocsag import SYNC, IDLE, NUMERIC
    cws = []
    for ric, func, text, alpha in messages:
        frame = ric & 7
        while len(cws) % 16 != 2 * frame:
            cws.append(IDLE)
        cws.append(pocsag_cw(((ric >> 3) << 2) | func))
        bits = []
        if alpha:
            for ch in text + "\x04":
                bits += [(ord(ch) >> j) & 1 for j in range(7)]
        else:
            for ch in text:
                v = NUMERIC.index(ch)
                bits += [(v >> j) & 1 for j in range(4)]
        while len(bits) % 20:
            bits.append(0 if alpha else 1)          # bourrage (espaces 0xC en numérique : 0011)
        for i in range(0, len(bits), 20):
            d = 0
            for b in bits[i:i + 20]:
                d = (d << 1) | b
            cws.append(pocsag_cw((1 << 20) | d))
        cws.append(IDLE)
    while len(cws) % 16:
        cws.append(IDLE)
    out = [1, 0] * (preamble // 2)
    for i in range(0, len(cws), 16):
        for w in [SYNC] + cws[i:i + 16]:
            out += [(w >> (31 - j)) & 1 for j in range(32)]
    return out


def pocsag_encode(messages, baud=1200, fs=12000, amp=0.5, phantom=False, invert=False):
    """Bande de base NRZ comme à la sortie d'un discriminateur FM (1 = niveau bas si invert)."""
    bits = pocsag_bits(messages)
    spb = fs / baud
    n = int(len(bits) * spb)
    idx = np.minimum((np.arange(n) / spb).astype(int), len(bits) - 1)
    lv = np.array(bits)[idx] * 2.0 - 1
    if invert:
        lv = -lv
    # passage dans un récepteur FM : bande limitée
    from scipy.signal import firwin, lfilter
    lv = lfilter(firwin(101, 0.9 * baud, fs=fs), 1.0, lv)
    x = np.concatenate([np.zeros(int(0.3 * fs)), amp * lv, np.zeros(int(0.3 * fs))])
    if phantom:
        from ..decoders.pocsag import phantom_dc_filter
        x = np.convolve(x, phantom_dc_filter(fs))[:len(x)]
    return x
