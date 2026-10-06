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
