"""Générateurs de signaux de test (mires) : PSK, RTTY, CW, SITOR-B / Navtex.

Ils servent à vérifier les décodeurs. Chaque générateur renvoie un tableau float64 (±1) à fs Hz.
"""
import numpy as np

from ..tables import (VARICODE, BAUDOT_LTRS, BAUDOT_FIGS, BAUDOT_LTRS_SHIFT, BAUDOT_FIGS_SHIFT,
                      CCIR_LTRS, CCIR_FIGS, CCIR_LTRS_SHIFT, CCIR_FIGS_SHIFT, CCIR_ALPHA, CCIR_REP,
                      CCIR_CHAR32, MORSE)

TAU = 2 * np.pi


def add_noise(x, snr_db, fs, bw=2500.0, seed=1):
    """Ajoute un bruit blanc pour un rapport S/B donné, mesuré dans une bande de bw Hz (convention 2500 Hz)."""
    rng = np.random.default_rng(seed)
    p_sig = np.mean(x[np.abs(x) > 1e-9] ** 2) if np.any(np.abs(x) > 1e-9) else 1.0
    p_noise_bw = p_sig / (10 ** (snr_db / 10))
    p_noise = p_noise_bw * (fs / 2) / bw
    return x + rng.normal(0, np.sqrt(p_noise), len(x))


# ------------------------------------------------------------------ PSK
def psk_bits(text, preamble=32, postamble=32):
    bits = "0" * preamble
    for ch in text:
        c = ord(ch)
        if c < 128:
            bits += VARICODE[c] + "00"
    return bits + "1" * postamble


def psk_encode(text, fs=12000, af=1000.0, baud=31.25, amp=0.5):
    bits = psk_bits(text)
    sps = fs / baud
    n = int(len(bits) * sps)
    t = np.arange(n) / fs
    base = np.zeros(n)
    s = 1.0
    for k, b in enumerate(bits):
        a, e = int(round(k * sps)), int(round((k + 1) * sps))
        if b == "0":                       # inversion de phase, enveloppe en cosinus
            tt = np.arange(e - a) / (e - a)
            base[a:e] = s * np.cos(np.pi * tt)
            s = -s
        else:
            base[a:e] = s
    return amp * base * np.cos(TAU * af * t)


# ------------------------------------------------------------------ FSK générique (phase continue)
def fsk(bits_and_durations, fs, f_mark, f_space, amp=0.5):
    """bits_and_durations : liste de (bit, durée en s). 1 = mark."""
    out = []
    ph = 0.0
    carry = 0.0
    for bit, dur in bits_and_durations:
        exact = dur * fs + carry
        n = int(round(exact))
        carry = exact - n
        f = f_mark if bit else f_space
        w = TAU * f / fs
        out.append(amp * np.cos(ph + w * np.arange(n)))
        ph = (ph + w * n) % TAU
    return np.concatenate(out) if out else np.zeros(0)


# ------------------------------------------------------------------ RTTY
def baudot_codes(text):
    codes, figs = [BAUDOT_LTRS_SHIFT], False
    for ch in text.upper():
        if ch in " \r\n":
            codes.append(BAUDOT_LTRS.index(ch))
        elif ch in BAUDOT_LTRS[1:]:
            if figs:
                codes.append(BAUDOT_LTRS_SHIFT); figs = False
            codes.append(BAUDOT_LTRS.index(ch))
        elif ch in BAUDOT_FIGS[1:]:
            if not figs:
                codes.append(BAUDOT_FIGS_SHIFT); figs = True
            codes.append(BAUDOT_FIGS.index(ch))
    return codes


def rtty_encode(text, fs=12000, af=1000.0, baud=45.45, shift=170.0, reverse=False, amp=0.5, idle=0.5):
    T = 1.0 / baud
    seq = [(1, idle)]
    for c in baudot_codes(text):
        seq.append((0, T))
        for i in range(5):
            seq.append(((c >> i) & 1, T))
        seq.append((1, 1.5 * T))
    seq.append((1, idle))
    fm, fsp = af + shift / 2, af - shift / 2
    if reverse:
        fm, fsp = fsp, fm
    return fsk(seq, fs, fm, fsp, amp)


# ------------------------------------------------------------------ CW
def cw_encode(text, fs=12000, af=700.0, wpm=20, amp=0.5, rise=0.005):
    dot = 1.2 / wpm
    inv = {v: k for k, v in MORSE.items()}
    marks = []                               # (on, durée)
    for word in text.upper().split():
        for ch in word:
            code = inv.get(ch)
            if not code:
                continue
            for i, sym in enumerate(code):
                marks.append((1, dot if sym == "." else 3 * dot))
                marks.append((0, dot))
            marks[-1] = (0, 3 * dot)
        marks[-1] = (0, 7 * dot)
    n_total = int(sum(d for _, d in marks) * fs) + fs // 2
    env = np.zeros(n_total)
    pos = fs // 4
    r = int(rise * fs)
    ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(r) / r)
    for on, d in marks:
        n = int(round(d * fs))
        if on:
            seg = np.ones(n)
            seg[:r] = ramp
            seg[-r:] = ramp[::-1]
            env[pos:pos + n] = seg
        pos += n
    t = np.arange(len(env)) / fs
    return amp * env * np.cos(TAU * af * t)


# ------------------------------------------------------------------ SITOR-B / Navtex
def ccir_codes(text):
    ltr = {c: i for i, c in enumerate(CCIR_LTRS) if c != "_"}
    fig = {c: i for i, c in enumerate(CCIR_FIGS) if c != "_"}
    out, figs = [], False
    for ch in text.upper():
        if ch in " \r\n":
            out.append(ltr[ch])
        elif ch in ltr:
            if figs:
                out.append(CCIR_LTRS_SHIFT); figs = False
            out.append(ltr[ch])
        elif ch in fig:
            if not figs:
                out.append(CCIR_FIGS_SHIFT); figs = True
            out.append(fig[ch])
    return out


def sitorb_encode(text, fs=12000, af=1000.0, shift=170.0, phasing=3.0, amp=0.5):
    """Mode B (FEC) : chaque caractère est émis deux fois, la seconde 5 caractères plus tard."""
    codes = [CCIR_LTRS_SHIFT] + ccir_codes(text)
    n_phase = int(phasing / 0.14)
    stream = []
    for _ in range(n_phase):
        stream += [CCIR_REP, CCIR_ALPHA]
    off = 2
    for i, c in enumerate(codes):
        stream += [c, codes[i - off] if i >= off else CCIR_ALPHA]
    for i in range(off):
        stream += [CCIR_CHAR32, codes[len(codes) - off + i]]
    for _ in range(10):
        stream += [CCIR_ALPHA, CCIR_ALPHA]
    seq = []
    for c in stream:
        for j in range(7):
            seq.append(((c >> j) & 1, 0.01))
    return fsk(seq, fs, af + shift / 2, af - shift / 2, amp)


def navtex_message(body, station="F", subject="A", number=1):
    return f"ZCZC {station}{subject}{number:02d}\r\n{body}\r\nNNNN\r\n"
