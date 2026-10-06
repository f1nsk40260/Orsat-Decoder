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


# ------------------------------------------------------------------ signaux horaires
def _bcd_bits(v, weights):
    out = []
    for w in weights:
        if w and v >= w:
            out.append(1)
            v -= w
        else:
            out.append(0)
    return out


def _bcd_bits_le(v, weights):
    """Poids croissants (1, 2, 4, 8, 10, 20…), émis dans cet ordre."""
    out = [0] * len(weights)
    for i in sorted(range(len(weights)), key=lambda k: -weights[k]):
        if weights[i] and v >= weights[i]:
            out[i] = 1
            v -= weights[i]
    return out


def dcf77_bits(y, mo, d, wd, h, mi, summer=True):
    b = [0] * 59
    b[17], b[18], b[20] = (1, 0, 1) if summer else (0, 1, 1)
    b[21:28] = _bcd_bits_le(mi, (1, 2, 4, 8, 10, 20, 40))
    b[28] = sum(b[21:28]) % 2
    b[29:35] = _bcd_bits_le(h, (1, 2, 4, 8, 10, 20))
    b[35] = sum(b[29:35]) % 2
    b[36:42] = _bcd_bits_le(d, (1, 2, 4, 8, 10, 20))
    b[42:45] = _bcd_bits_le(wd, (1, 2, 4))
    b[45:50] = _bcd_bits_le(mo, (1, 2, 4, 8, 10))
    b[50:58] = _bcd_bits_le(y % 100, (1, 2, 4, 8, 10, 20, 40, 80))
    b[58] = sum(b[36:58]) % 2
    return b


def msf_bits(y, mo, d, wd, h, mi, bst=False):
    a, b = [0] * 60, [0] * 60
    a[17:25] = _bcd_bits(y % 100, (80, 40, 20, 10, 8, 4, 2, 1))
    a[25:30] = _bcd_bits(mo, (10, 8, 4, 2, 1))
    a[30:36] = _bcd_bits(d, (20, 10, 8, 4, 2, 1))
    a[36:39] = _bcd_bits(wd, (4, 2, 1))
    a[39:45] = _bcd_bits(h, (20, 10, 8, 4, 2, 1))
    a[45:52] = _bcd_bits(mi, (40, 20, 10, 8, 4, 2, 1))
    a[52:60] = [0, 1, 1, 1, 1, 1, 1, 0]
    for k, (i, j) in zip((54, 55, 56, 57), ((17, 25), (25, 36), (36, 39), (39, 52))):
        b[k] = 1 - sum(a[i:j]) % 2
    b[58] = 1 if bst else 0
    return a, b


def wwvb_syms(y, doy, h, mi, jjy=False):
    s = [0] * 60
    for i in (0, 9, 19, 29, 39, 49, 59):
        s[i] = "M"
    s[1:9] = _bcd_bits(mi, (40, 20, 10, 0, 8, 4, 2, 1))
    s[12:19] = _bcd_bits(h, (20, 10, 0, 8, 4, 2, 1))
    s[22:34] = _bcd_bits(doy, (200, 100, 0, 80, 40, 20, 10, 0, 8, 4, 2, 1))
    if jjy:
        s[41:49] = _bcd_bits(y % 100, (80, 40, 20, 10, 8, 4, 2, 1))
    else:
        s[45:54] = _bcd_bits(y % 100, (80, 40, 20, 10, 0, 8, 4, 2, 1))
    for i in (0, 9, 19, 29, 39, 49, 59):
        s[i] = "M"
    return s


def wwv_syms(y, doy, h, mi):
    s = [0] * 60
    s[0] = None
    for i in (9, 19, 29, 39, 49, 59):
        s[i] = "M"
    le = lambda v, idx: [(v >> k) & 1 for k in range(len(idx))]
    for idx, v in (((4, 5, 6, 7), y % 10), ((51, 52, 53, 54), (y % 100) // 10), ((10, 11, 12, 13), mi % 10),
                   ((15, 16, 17), mi // 10), ((20, 21, 22, 23), h % 10), ((25, 26), h // 10),
                   ((30, 31, 32, 33), doy % 10), ((35, 36, 37, 38), (doy // 10) % 10), ((40, 41), doy // 100)):
        for i, bit in zip(idx, le(v, idx)):
            s[i] = bit
    return s


def timecode_encode(station, minutes, fs=12000, af=1000.0, start_sec=50, amp=0.5):
    """minutes : liste de minutes à annoncer, (année, mois, jour, jour semaine, heure, minute).
    Le signal commence à la seconde start_sec de la minute qui précède la première."""
    import calendar
    secs = []                                         # par seconde : fonction gabarit
    for (y, mo, d, wd, h, mi) in minutes:
        doy = sum(calendar.monthrange(y, m)[1] for m in range(1, mo)) + d
        if station in ("dcf77", "tdf"):
            b = dcf77_bits(y, mo, d, wd, h, mi) + [None]
            secs += [("p", v) for v in b]
        elif station == "msf":
            a, b = msf_bits(y, mo, d, wd, h, mi)
            secs += [("m", (a[i], b[i], i == 0)) for i in range(60)]
        elif station in ("wwvb", "jjy"):
            secs += [("p", v) for v in wwvb_syms(y, doy, h, mi, jjy=station == "jjy")]
        else:
            secs += [("p", v) for v in wwv_syms(y, doy, h, mi)]
    # la première minute est précédée de la fin de la minute d'avant (repères compris)
    lead = secs[start_sec:60] if station not in ("dcf77", "tdf") else secs[start_sec:59] + [("p", None)]
    secs = lead + secs + secs[:3]
    n1 = int(fs)
    t = np.arange(n1) / fs
    out = []
    ph = 0.0
    for kind, v in secs:
        env = np.ones(n1)
        pm = np.zeros(n1)
        if station == "dcf77" and v is not None:
            env[:int((0.1 if v == 0 else 0.2) * fs)] = 0.15
        elif station == "tdf" and v is not None:
            k = int((0.1 if v == 0 else 0.2) * fs)
            ph_t = (t[:k] % 0.1) / 0.1                     # rampes +1, -2, +1 rad (triangle)
            pm[:k] = np.where(ph_t < 0.25, 4 * ph_t, np.where(ph_t < 0.75, 2 - 4 * ph_t, 4 * ph_t - 4))
        elif station == "msf":
            a, b, mark = v
            env[:int(0.1 * fs)] = 0.0
            if mark:
                env[:int(0.5 * fs)] = 0.0
            else:
                if a:
                    env[int(0.1 * fs):int(0.2 * fs)] = 0.0
                if b:
                    env[int(0.2 * fs):int(0.3 * fs)] = 0.0
        elif station in ("wwvb", "jjy") and v is not None:
            w = {0: 0.2, 1: 0.5, "M": 0.8}[v] if station == "wwvb" else {0: 0.8, 1: 0.5, "M": 0.2}[v]
            if station == "wwvb":
                env[:int(w * fs)] = 0.1
            else:
                env[:] = 0.1
                env[:int(w * fs)] = 1.0
        elif station == "wwv" and v is not None:
            w = {0: 0.2, 1: 0.5, "M": 0.8}[v]
            g = np.zeros(n1)
            g[int(0.03 * fs):int((0.03 + w) * fs)] = 1.0
            env = 1.0 + 0.5 * g * np.sin(2 * np.pi * 100.0 * t)
        sig = env * np.cos(2 * np.pi * af * t + ph + pm)
        ph = (ph + 2 * np.pi * af * n1 / fs) % (2 * np.pi)
        out.append(amp * sig)
    return np.concatenate(out)


def chu_encode(year, doy, h, mi, s0=30, nsec=12, fs=12000, af=2125.0, amp=0.5):
    """CHU : secondes s0..s0+nsec, trames FSK 300 bauds aux secondes 31 à 39."""
    from .encoders import fsk
    allseq = []
    for s in range(s0, s0 + nsec):
        sec = s % 60
        seq = [(1, 0.01)]
        frame = None
        if sec == 31:
            dig = [0, 0] + [int(c) for c in f"{year:04d}"] + [3, 7, 0, 0]
            a = [dig[i] | dig[i + 1] << 4 for i in range(0, 10, 2)]
            frame = a + [v ^ 0xFF for v in a]
        elif 32 <= sec <= 39:
            dig = [6] + [int(c) for c in f"{doy:03d}{h:02d}{mi:02d}{sec:02d}"]
            a = [dig[i] | dig[i + 1] << 4 for i in range(0, 10, 2)]
            frame = a + a
        if frame:
            for v in frame:
                seq += [(0, 1 / 300)] + [((v >> j) & 1, 1 / 300) for j in range(8)] + [(1, 2 / 300)]
        dur = sum(d for _, d in seq)
        seq.append((1, 1.0 - dur))
        allseq += seq
    return fsk(allseq, fs, af + 100, af - 100, amp)            # phase continue


# ------------------------------------------------------------------ ALE 2G
def ale_encode(words, fs=12000, af=1625.0, amp=0.5, gap=2.0):
    """words : liste de (préambule, 'ABC') ; préambules : DATA 0, THRU 1, TO 2, TWAS 3, FROM 4, TIS 5, CMD 6, REP 7."""
    from ..decoders.ale import word_bits, GRAY
    names = {"DATA": 0, "THRU": 1, "TO": 2, "TWAS": 3, "FROM": 4, "TIS": 5, "CMD": 6, "REP": 7}
    tone_of = {v: k for k, v in enumerate(GRAY)}
    bits = []
    for pre, txt in words:
        bits += word_bits(names[pre], txt)
    syms = [tone_of[bits[i] << 2 | bits[i + 1] << 1 | bits[i + 2]] for i in range(0, len(bits), 3)]
    seq = [((af - 875.0 + 250.0 * k,), 0.008) for k in syms]
    return np.concatenate([np.zeros(int(gap * fs)), tones_seq(seq, fs, amp, rise=0.0005), np.zeros(int(gap * fs))])


def ale_call(to, tis, amd=None):
    """Appel ALE simple : TO <adresse>, [CMD AMD + texte], TIS <adresse>."""
    def addr(pre, a):
        a = a.ljust((len(a) + 2) // 3 * 3, "@")
        out = [(pre, a[:3])]
        for i, k in enumerate(range(3, len(a), 3)):
            out.append(("DATA" if i % 2 == 0 else "REP", a[k:k + 3]))
        return out
    w = addr("TO", to) * 2
    if amd:
        t = amd.ljust((len(amd) + 2) // 3 * 3)
        w.append(("CMD", t[:3]))
        for i, k in enumerate(range(3, len(t), 3)):
            w.append(("DATA" if i % 2 == 0 else "REP", t[k:k + 3]))
    w += addr("TIS", tis)
    return w


# ------------------------------------------------------------------ THROB
def throb_encode(text, mode="THROB1", fs=12000, af=1000.0, amp=0.5, pre=4):
    from ..decoders.throb import THROB_MODES, PAIRS, CHARS, XPAIRS, XCHARS, pulse
    n8, freqs, pk, x = THROB_MODES[mode]
    n = int(round(n8 * fs / 8000))
    p = pulse(n, pk)
    t = np.arange(n) / fs
    syms = []
    idle, space = 0, 1
    for _ in range(pre):
        syms.append(idle)
        if x:
            idle, space = space, idle
    for c in text.upper():
        if not x and c in "?@-\n":
            syms += [5, {"?": 20, "@": 13, "-": 9, "\n": 0}[c]]
            continue
        if x:
            if c == " " or c not in XCHARS:
                syms.append(space)
                idle, space = space, idle
            else:
                syms.append(XCHARS.index(c))
        else:
            syms.append(CHARS.index(c) if c in CHARS and c != "\0" else 44)
    syms += [idle] * 3
    out = []
    for s in syms:
        a, b = (XPAIRS if x else PAIRS)[s]
        out.append(amp * p * (np.sin(2 * np.pi * (af + freqs[a - 1]) * t) + np.sin(2 * np.pi * (af + freqs[b - 1]) * t)) / 2)
    return np.concatenate([np.zeros(fs // 2)] + out + [np.zeros(fs)])


# ------------------------------------------------------------------ FSQ
FSQ_ENC = None


def fsq_encode(text, baud=3.0, fs=12000, af=1500.0, amp=0.5, idle=6, variant="fsq"):
    """FSQ / IFKP : chaque quartet n fait monter la tonalité de n+1 (modulo 33)."""
    from ..decoders.fsq import SYMLEN, VARIANTS
    SR, _, single, double = VARIANTS[variant]
    enc = {}
    for i, c in enumerate(single):
        enc.setdefault(c, (i,))
    for p, row in enumerate(double):
        for j, c in enumerate(row):
            if c > 0:
                enc.setdefault(c, (p, 29 + j))
    symlen = (SYMLEN[baud] if variant == "fsq" else 4096 * {1: 2.0, 2: 1.0, 4: 0.5}[int(baud)]) * fs / SR
    bw = 33 * 3 * SR / 4096
    base = np.ceil((af - bw / 2) * 4096 / SR)
    base -= base % 3
    sp = enc[32][0]
    nibs = [sp] * idle                          # espaces en préambule
    for c in text:
        nibs += list(enc.get(ord(c), (sp,)))
    nibs += [sp, sp]
    tone, ph, out = 0, 0.0, []
    for nb in nibs:
        tone = (tone + nb + 1) % 33
        f = (base + 3 * tone) * SR / 4096
        n = int(round(symlen))
        out.append(amp * np.cos(ph + 2 * np.pi * f * np.arange(n) / fs))
        ph = (ph + 2 * np.pi * f * n / fs) % (2 * np.pi)
    return np.concatenate([np.zeros(fs // 2)] + out + [np.zeros(fs)])


# ------------------------------------------------------------------ WSPR
def wspr_encode(stations, fs=12000, amp=0.5):
    """stations : liste de ('INDICATIF LOC PWR', décalage audio Hz par rapport à 1500, amplitude relative).
    Renvoie 120 s d'audio : émission de 162 symboles de 8192/12000 s commençant à +1 s."""
    import subprocess
    from pathlib import Path
    binary = Path(__file__).resolve().parents[2] / "native" / "bin" / "wspr_decode_iq"
    n = int(120 * fs)
    out = np.zeros(n)
    spt = 8192 / 12000
    for msg, off, a in stations:
        syms = subprocess.run([str(binary), "-e", msg], capture_output=True, text=True).stdout.strip()
        ph = 0.0
        pos = int(1.0 * fs)
        for k, c in enumerate(syms):
            f = 1500.0 + off + (int(c) - 1.5) * 12000 / 8192
            m = int(round((k + 1) * spt * fs)) - int(round(k * spt * fs))
            out[pos:pos + m] += amp * a * np.cos(ph + 2 * np.pi * f * np.arange(m) / fs)
            ph = (ph + 2 * np.pi * f * m / fs) % (2 * np.pi)
            pos += m
    return out


# ---------------------------------------------------------------------------------------------- DGPS
def rtcm_frame(mtype, station, z, seq, words, health=0):
    """Trame RTCM SC-104 : liste de mots de données de 24 bits (en-tête compris, sans parité)."""
    h1 = (0x66 << 16) | (mtype << 10) | station
    h2 = (z << 11) | (seq << 8) | (len(words) << 3) | health
    return [h1, h2] + list(words)


def rtcm_pack(fields):
    """[(valeur, nombre de bits), …] -> mots de 24 bits (bourrage par des 1, comme le prévoit la norme)."""
    bits = []
    for v, n in fields:
        bits += [(v >> (n - 1 - i)) & 1 for i in range(n)]
    while len(bits) % 24:
        bits.append(1)
    return [int("".join(map(str, bits[i:i + 24])), 2) for i in range(0, len(bits), 24)]


def rtcm_type9(sats):
    """sats : [(PRN, PRC m, RRC m/s, IOD)] -> mots (facteur d'échelle 0,02 m)."""
    f = []
    for prn, prc, rrc, iod in sats:
        f += [(0, 1), (0, 2), (prn & 31, 5), (int(round(prc / 0.02)) & 0xFFFF, 16),
              (int(round(rrc / 0.002)) & 0xFF, 8), (iod, 8)]
    return rtcm_pack(f)


def rtcm_type3(lat, lon, h):
    import math
    a, fl = 6378137.0, 1 / 298.257223563
    e2 = fl * (2 - fl)
    la, lo = math.radians(lat), math.radians(lon)
    n = a / math.sqrt(1 - e2 * math.sin(la) ** 2)
    xyz = ((n + h) * math.cos(la) * math.cos(lo), (n + h) * math.cos(la) * math.sin(lo), (n * (1 - e2) + h) * math.sin(la))
    return rtcm_pack([(int(round(v / 0.01)) & 0xFFFFFFFF, 32) for v in xyz])


def rtcm_type16(text):
    b = list(text.encode("ascii"))
    while len(b) % 3:
        b.append(0)
    return [(b[i] << 16) | (b[i + 1] << 8) | b[i + 2] for i in range(0, len(b), 3)]


def dgps_bits(frames, idle=60):
    """Trames -> bits émis (parité GPS, D29*/D30* enchaînés ; trames nulles de type 6 en bourrage)."""
    from ..decoders.dgps import encode_word
    out = [0, 1] * idle
    d29, d30 = 0, 0
    for fr in frames:
        for data in fr:
            w = encode_word(data, d29, d30)
            out += [(w >> (29 - i)) & 1 for i in range(30)]
            d29, d30 = (w >> 1) & 1, w & 1
    return out + [0, 1] * idle


def dgps_encode(frames, baud=200.0, fs=12000, af=1000.0, amp=0.5):
    """MSK : fréquence af ± débit/4 (bit 1 = fréquence haute), phase continue."""
    bits = dgps_bits(frames)
    spb = fs / baud
    n = int(len(bits) * spb)
    idx = np.minimum((np.arange(n) / spb).astype(int), len(bits) - 1)
    f = af + (np.array(bits)[idx] * 2 - 1) * baud / 4
    return amp * np.cos(2 * np.pi * np.cumsum(f) / fs)


# ---------------------------------------------------------------------------------------------- PACTOR I
def pactor_packet(payload, counter, baud=200.0, fmt=0, flags=0):
    """Paquet PACTOR I (octets) : en-tête 0x55, données complétées, état, CRC X.25."""
    from ..decoders.pactor import crc16
    n = 20 if baud > 150 else 8
    if fmt == 0:
        data = bytes(payload)[:n].ljust(n, b"\x1e")
    else:
        bits = payload[:n * 8].ljust(n * 8, "0")
        data = bytes(int(bits[i:i + 8][::-1], 2) for i in range(0, n * 8, 8))
    st = (counter & 3) | (fmt << 2) | flags
    body = data + bytes([st])
    c = crc16(body)
    return bytes([0x55]) + body + bytes([c & 0xFF, c >> 8])


def pactor_split(text, baud=200.0, huffman=False):
    """Texte -> charges utiles successives (octets ASCII, ou chaînes de bits Huffman sans coupure de code)."""
    n = 20 if baud > 150 else 8
    if not huffman:
        b = text.encode("latin-1")
        return [b[i:i + n] for i in range(0, len(b), n)]
    from ..decoders.pactor import HUFFMAN
    out, cur = [], ""
    for c in text:
        code = HUFFMAN[c]
        if len(cur) + len(code) > n * 8:
            out.append(cur)
            cur = ""
        cur += code
    return out + ([cur] if cur else [])


def pactor_encode(text, baud=200.0, fs=12000, af=1500.0, shift=200.0, amp=0.5, repeats=2, huffman=False, qrt=True):
    """Liaison ARQ vue par un écouteur : cycle de 1,25 s, paquet de 0,96 s ; chaque paquet est émis
    `repeats` fois (répétitions ARQ, la deuxième copie en polarité inverse), puis un paquet QRT."""
    pays = pactor_split(text, baud, huffman)
    pk = [pactor_packet(p, i, baud, 1 if huffman else 0) for i, p in enumerate(pays)]
    pk.append(pactor_packet(b"", len(pays), baud, 0, 0x80))
    spb = fs / baud
    cyc = int(1.25 * fs)
    sig = []
    ph = 0.0
    for p in pk:
        for r in range(repeats):
            bits = [(b >> i) & 1 for b in p for i in range(8)]
            if r % 2:
                bits = [1 - b for b in bits]
            n = int(len(bits) * spb)
            idx = np.minimum((np.arange(n) / spb).astype(int), len(bits) - 1)
            f = af + (np.array(bits)[idx] - 0.5) * shift
            phs = ph + 2 * np.pi * np.cumsum(f) / fs
            ph = phs[-1]
            seg = np.zeros(cyc)
            seg[:n] = amp * np.cos(phs)
            sig.append(seg)
    return np.concatenate(sig)


# ---------------------------------------------------------------------------------------------- ACARS
def acars_block(reg, label, text, mode="2", bid="1", ack="\x15", no="M01A", flight="AF1234"):
    """Bloc ACARS (octets avec parité impaire, de SOH exclu au CRC inclus)."""
    from ..decoders.acars import crc16

    def par(c):
        return c | (0x80 if bin(c).count("1") % 2 == 0 else 0)
    body = mode + reg.rjust(7, ".") + ack + label + bid + "\x02"
    if bid.isdigit():
        body += no + flight.ljust(6)
    body += text
    b = bytes(par(ord(c)) for c in body) + bytes([0x83])
    c = crc16(b)
    return b + bytes([c & 0xFF, c >> 8])


def acars_encode(blocks, fs=12000, amp=0.5, gap=0.5):
    """Émission AM vue après démodulation : MSK 2400 bits/s, 1200 Hz (changement) / 2400 Hz."""
    out = []
    for blk in blocks:
        data = bytes([0xFF] * 16) + bytes([0x2B, 0x2A, 0x16, 0x16, 0x01]) + blk + bytes([0x7F])
        bits = [(b >> i) & 1 for b in data for i in range(8)]
        spb = fs / 2400.0
        n = int(len(bits) * spb)
        # codage différentiel : un bit égal au précédent -> 2400 Hz, sinon 1200 Hz
        prev, f = 1, []
        for b in bits:
            f.append(2400.0 if b == prev else 1200.0)
            prev = b
        idx = np.minimum((np.arange(n) / spb).astype(int), len(bits) - 1)
        ph = 2 * np.pi * np.cumsum(np.array(f)[idx]) / fs
        out += [np.zeros(int(gap * fs)), amp * np.cos(ph)]
    out.append(np.zeros(int(gap * fs)))
    return np.concatenate(out)
