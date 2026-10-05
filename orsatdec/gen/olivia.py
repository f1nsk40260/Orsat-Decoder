"""Générateur de mires Olivia et Contestia, calqué bit pour bit sur l'émetteur de fldigi
(src/include/jalocha/pj_mfsk.h : MFSK_Encoder, MFSK_Modulator, RateConverter, MFSK_Transmitter,
et olivia.cxx / contestia.cxx : tonalités de début et de fin, choix de la première porteuse).

Tout est calculé à 8000 Hz comme dans fldigi, puis rééchantillonné à fs.
"""
import numpy as np
from scipy.signal import resample_poly

SR = 8000                                  # fréquence d'échantillonnage interne de fldigi
SCRAMBLE_OLIVIA = 0xE257E6D0291574EC
SCRAMBLE_CONTESTIA = 0xEDB88320


# ------------------------------------------------------------------ transformées de Hadamard (pj_fht.h)
def fht(v):
    """FHT « directe » de pj_fht.h (attention : papillon (a+b, b-a), pas la forme usuelle)."""
    d = np.array(v, dtype=np.float64)
    n = len(d)
    step = 1
    while step < n:
        d = d.reshape(-1, 2, step)
        a, b = d[:, 0, :].copy(), d[:, 1, :].copy()
        d[:, 0, :], d[:, 1, :] = b + a, b - a
        d = d.reshape(n)
        step *= 2
    return d


def ifht(v):
    """FHT inverse de pj_fht.h (papillon (a-b, a+b), pas décroissants)."""
    d = np.array(v, dtype=np.float64)
    n = len(d)
    step = n // 2
    while step:
        d = d.reshape(-1, 2, step)
        a, b = d[:, 0, :].copy(), d[:, 1, :].copy()
        d[:, 0, :], d[:, 1, :] = a - b, a + b
        d = d.reshape(n)
        step //= 2
    return d


def gray(x):
    return x ^ (x >> 1)


def binary(g):
    g ^= g >> 4
    g ^= g >> 2
    g ^= g >> 1
    return g


def mode_params(tones, bw, contestia=False):
    """Paramètres dérivés comme MFSK_Transmitter::Preset()."""
    bps = int(np.log2(tones))
    tones = 1 << bps
    lb = int(np.log2(bw // 125))
    bw = (1 << lb) * 125
    symlen = 1 << (bps + 7 - lb)           # longueur de symbole (et de FFT) à 8000 Hz
    bpc = 6 if contestia else 7            # bits par caractère
    return dict(bps=bps, tones=tones, bw=bw, symlen=symlen, sep=symlen // 2,
                nsym=1 << (bpc - 1), bpc=bpc,
                code=SCRAMBLE_CONTESTIA if contestia else SCRAMBLE_OLIVIA,
                shift=5 if contestia else 13)


def first_carrier(af, p, contestia=False):
    """Indice (en cases FFT de 8000/symlen Hz) de la tonalité la plus basse, comme olivia.cxx/contestia.cxx."""
    if contestia:
        fcm = np.float32((af - (p["bw"] // 2)) / 500)
    else:
        fco = p["bw"] * (1.0 - 0.5 / p["tones"]) / 2.0
        fcm = np.float32((af - fco) / 500.0)
    return int(np.float32(p["symlen"] // 16) * fcm) + 1


# ------------------------------------------------------------------ codeur FEC (MFSK_Encoder)
def contestia_char(c):
    if ord("a") <= c <= ord("z"):
        c += ord("A") - ord("a")
    if c == 32:
        return 59
    if c == 13:
        return 60
    if c == 10:
        return 0
    if 33 <= c <= 90:
        return c - 32
    if c == 8:
        return 61
    if c == 0:
        return 0
    return ord("?") - 32


def encode_block(chars, p, contestia=False):
    """Un bloc FEC : bps caractères -> nsym symboles (entiers de bps bits, avant codage de Gray)."""
    n, bps = p["nsym"], p["bps"]
    out = np.zeros(n, dtype=np.int64)
    mask = 2 * n - 1
    for fb in range(bps):
        c = chars[fb]
        c = contestia_char(c) if contestia else (c & mask)
        buf = np.zeros(n)
        if c < n:
            buf[c] = 1
        else:
            buf[c - n] = -1
        buf = ifht(buf)
        cb = (fb * p["shift"]) & (n - 1)
        for tb in range(n):
            if (p["code"] >> cb) & 1:
                buf[tb] = -buf[tb]
            cb = (cb + 1) & (n - 1)
        for tb in range(n):
            if buf[tb] < 0:
                out[tb] |= 1 << ((fb + tb) % bps)
    return out


def text_blocks(text, p, contestia=False, olivia8bit=True):
    """Reproduit la file d'attente de olivia::tx_process + MFSK_Transmitter::Output :
    renvoie la liste des blocs (listes de bps codes) réellement émis."""
    bps = p["bps"]
    calls = []                             # codes ajoutés à la file par chaque appel de tx_process
    for ch in text:
        c = ord(ch)
        if c > 127:
            if contestia:
                c = ord(".") if c > 255 else c
                if c > 127:
                    calls.append([127, c & 127])
                    continue
            elif olivia8bit and c <= 255:
                calls.append([127, c & 127])
                continue
            else:
                c = ord(".")
        calls.append([c])
    queue = [0]                            # olivia.cxx : Tx->PutChar(0) après le préambule
    pos, stop, blocks = 0, False, []
    symptr = 0
    while True:
        if stop or len(queue) < bps:
            if not stop:
                if pos < len(calls):
                    queue += calls[pos]; pos += 1
                else:
                    stop = True
        if symptr == 0:
            if stop and not queue:
                break
            blk = queue[:bps]
            del queue[:bps]
            blocks.append(blk + [0] * (bps - len(blk)))
        symptr = (symptr + 1) % p["nsym"]
    return blocks


def symbols_for(text, tones=32, bw=1000, contestia=False):
    p = mode_params(tones, bw, contestia)
    return np.concatenate([encode_block(b, p, contestia) for b in text_blocks(text, p, contestia)])


# ------------------------------------------------------------------ modulateur (MFSK_Modulator)
def _rateconv_taps():
    """RateConverter de pj_mfsk.h à OutputRate = 1 : un RIF de 16 coefficients (le plus ancien d'abord)."""
    tap, over = 16, 16
    flen = tap * over
    i = np.arange(flen)
    ph = np.pi * (2 * i - flen) / flen
    win = 0.35875 + 0.48829 * np.cos(ph) + 0.14128 * np.cos(2 * ph) + 0.01168 * np.cos(3 * ph)
    with np.errstate(invalid="ignore", divide="ignore"):
        x = ph * (3.0 / 8) * tap
        filt = np.where(ph != 0, np.sin(x) / np.where(x == 0, 1, x), 1.0)
    shape = win * filt
    return shape[(over - 1)::over]          # Convolute(0) : FilterShape[15 + 16k], k = 0..15


def modulate(symbols, p, fc, rand_bits):
    """MFSK_Modulator::Send/Output + RateConverter + normalisation par paquet de MFSK_Transmitter::Output.
    fc : première porteuse (cases FFT). rand_bits : itérable d'entiers (rand() de fldigi)."""
    L, sep = p["symlen"], p["sep"]
    mask = L - 1
    cos_t = np.cos(2 * np.pi * np.arange(L) / L)
    shape = 1.0 - cos_t
    nsym = len(symbols)
    tap = np.zeros((nsym + 2) * sep + L)
    phase = 0
    t = np.arange(L)
    for k, s in enumerate(symbols):
        f = fc + 2 * int(gray(int(s)))
        phase = (phase + f * (sep // 2 - L // 2)) & mask
        tap[k * sep:k * sep + L] += cos_t[(phase + f * t) & mask] * shape
        phase = (phase + f * (sep // 2 + L // 2)) & mask
        d = L // 4
        if next(rand_bits) & 1:
            d = -d
        phase = (phase + d) & mask
    # Output() est appelé après chaque symbole, plus une fois à l'arrêt (queue du dernier symbole)
    raw = tap[:(nsym + 1) * sep]
    g = _rateconv_taps()
    conv = np.convolve(raw, g[::-1])[:len(raw)]     # y[n] = sum_k g[k] x[n-15+k]
    out = conv.reshape(-1, sep)
    mx = np.max(np.abs(out), axis=1, keepdims=True)
    mx[mx == 0] = 1.0
    return (out / mx).ravel()


def start_tones(af, bw):
    """olivia::send_tones : deux tonalités aux bords de la bande, A B A B, 256 ms chacune."""
    sr4 = 512 * 16 // 4
    amp = np.ones(sr4)
    i = np.arange(sr4 // 8)
    amp[i] = amp[sr4 - 1 - i] = 0.5 * (1.0 - np.cos(np.pi * i / (sr4 // 8)))

    def tone(f):
        ph = np.cumsum(np.full(sr4, 2 * np.pi * f / SR))
        return np.cos(ph) * amp
    a, b = tone(af - bw / 2.0), tone(af + bw / 2.0)
    return np.concatenate([a, b, a, b])


def lcg_rand(seed=12345):
    """Même générateur que le banc C++ de vérification (substitut déterministe de rand())."""
    s = seed
    while True:
        s = (s * 1103515245 + 12345) & 0x7FFFFFFF
        yield s >> 16


def _encode(text, fs, af, tones, bw, amp, contestia, tones_on, rand_bits):
    p = mode_params(tones, bw, contestia)
    syms = symbols_for(text, tones, bw, contestia)
    fc = first_carrier(af, p, contestia)
    if rand_bits is None:
        rng = np.random.default_rng(7)
        rand_bits = iter(rng.integers(0, 2, len(syms) + 1).tolist())
    x = modulate(syms, p, fc, rand_bits)
    if tones_on:
        tn = start_tones(af, p["bw"])
        x = np.concatenate([tn, x, tn, np.zeros(512)])
    if fs != SR:
        g = np.gcd(int(fs), SR)
        x = resample_poly(x, int(fs) // g, SR // g)
    return amp * x


def olivia_encode(text, fs=12000, af=1500.0, tones=32, bw=1000, amp=0.5, start_tones=True, rand_bits=None):
    """Signal Olivia tones/bw centré sur af (convention fldigi), float64 à fs Hz."""
    return _encode(text, fs, af, tones, bw, amp, False, start_tones, rand_bits)


def contestia_encode(text, fs=12000, af=1500.0, tones=8, bw=500, amp=0.5, start_tones=True, rand_bits=None):
    """Signal Contestia tones/bw centré sur af (convention fldigi), float64 à fs Hz."""
    return _encode(text, fs, af, tones, bw, amp, True, start_tones, rand_bits)
