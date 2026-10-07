"""Générateurs de mires pour les modes image : fac-similé météo (WEFAX), SSTV et Hellschreiber.

Ils suivent les normes de chaque mode (tables de temps SSTV usuelles). Chaque générateur renvoie un tableau float64 (±amp) à fs Hz.
"""
import numpy as np

TAU = 2 * np.pi


def fm(freq, fs, amp=0.5, phase=0.0):
    """Oscillateur à phase continue suivant une fréquence instantanée par échantillon."""
    ph = phase + np.cumsum(TAU * np.asarray(freq, np.float64) / fs)
    return amp * np.sin(ph)


# ------------------------------------------------------------------ images de test
def test_image(w=320, h=256, seed=0, rgb=True):
    """Image « photo » synthétique : dégradés, disques, carrés de couleur, barres façon texte."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w].astype(np.float64)
    r = 255 * (x / w)
    g = 255 * (y / h)
    b = 255 * (0.5 + 0.5 * np.sin(x / w * 6 + y / h * 3))
    img = np.stack([r, g, b], -1)
    # ciel / sol
    img[: h // 3] = 0.6 * img[: h // 3] + 0.4 * np.array([120, 170, 230])
    # disques
    for (cx, cy, rad, col) in [(0.25, 0.55, 0.16, (230, 40, 40)), (0.7, 0.4, 0.12, (40, 200, 60)),
                               (0.55, 0.75, 0.1, (250, 230, 40))]:
        m = (x - cx * w) ** 2 + (y - cy * h) ** 2 < (rad * min(w, h)) ** 2
        img[m] = col
    # carrés de couleurs pures
    cols = [(255, 255, 255), (0, 0, 0), (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 0, 255), (0, 255, 255), (128, 128, 128)]
    s = max(4, w // 16)
    for i, c in enumerate(cols):
        img[h - s - 4:h - 4, 4 + i * s:4 + (i + 1) * s] = c
    # barres façon texte (lignes de « mots » noirs sur fond blanc)
    tx0, ty0 = int(0.55 * w), int(0.06 * h)
    tw, th = int(0.4 * w), int(0.22 * h)
    img[ty0:ty0 + th, tx0:tx0 + tw] = 245
    lh = max(3, th // 6)
    for li in range(5):
        yy = ty0 + 2 + li * lh
        xx = tx0 + 3
        while xx < tx0 + tw - 6:
            ww = int(rng.integers(3, max(4, tw // 6)))
            ww = min(ww, tx0 + tw - 3 - xx)
            img[yy:yy + max(1, lh // 2), xx:xx + ww] = 20
            xx += ww + max(2, w // 100)
    img = np.clip(img, 0, 255).astype(np.uint8)
    if not rgb:
        img = np.round(img @ np.array([0.299, 0.587, 0.114])).astype(np.uint8)
    return img


def chart_image(w=1809, h=300, seed=0):
    """Carte « météo » en niveaux de gris : fond blanc, cadre noir, isobares, texte, dégradé."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w].astype(np.float64)
    img = np.full((h, w), 255.0)
    mx = int(0.03 * w)
    # isobares (courbes sinueuses épaisses de 3 pixels)
    for k in range(8):
        yc = h * (k + 0.5) / 8 + 0.08 * h * np.sin(x / w * (3 + k) + k)
        img[np.abs(y - yc) < 1.6] = 0
    # disque « dépression » plein gris et dégradé
    m = (x - 0.3 * w) ** 2 + ((y - 0.5 * h) * 3) ** 2 < (0.08 * w) ** 2
    img[m] = 90
    gx = (x > 0.6 * w) & (x < 0.8 * w) & (y > 0.6 * h) & (y < 0.9 * h)
    img[gx] = 255 * (x[gx] - 0.6 * w) / (0.2 * w)
    # « texte »
    for li in range(3):
        yy = int(0.05 * h) + li * 12
        xx = mx + 20
        while xx < 0.45 * w:
            ww = int(rng.integers(6, 40))
            img[yy:yy + 7, xx:xx + ww] = 0
            xx += ww + 8
    # cadre
    img[:, mx:mx + 4] = 0
    img[:, w - mx - 4:w - mx] = 0
    img[:3, mx:w - mx] = 0
    img[-3:, mx:w - mx] = 0
    img[:, :mx] = 255
    img[:, w - mx:] = 255
    return np.clip(img, 0, 255).astype(np.uint8)


# ------------------------------------------------------------------ WEFAX
def wefax_encode(img, fs=12000, af=1900.0, lpm=120, ioc=576, shift=800.0, apt=True, phasing=20,
                 start_s=5.0, stop_s=5.0, black_s=2.0, ppm=0.0, amp=0.5):
    """Émission fac-similé (séquence : APT départ, 20 lignes de phasage, 1 ligne blanche,
    image, APT arrêt, noir). Le départ et l'arrêt sont ici conformes à la norme OMM (alternance
    noir/blanc à 300 ou 675 Hz, puis 450 Hz).
    ppm : erreur d'horloge de l'émetteur (pente à corriger)."""
    img = np.asarray(img)
    if img.ndim == 3:
        img = np.round(img @ np.array([0.299, 0.587, 0.114])).astype(np.uint8)
    rate = fs * (1 + ppm * 1e-6)               # horloge de l'émetteur
    Tl = 60.0 / lpm
    W = img.shape[1]
    parts = []

    def seg(dur, fn):
        n = int(round(dur * rate))
        t = np.arange(n) / rate
        parts.append(fn(t))

    start_f = 300.0 if ioc == 576 else 675.0
    if apt:
        seg(start_s, lambda t: (np.sin(TAU * start_f * t) >= 0).astype(np.float64))
        seg(phasing * Tl, lambda t: (((t % Tl) / Tl < 0.025) | ((t % Tl) / Tl >= 0.975)).astype(np.float64))
        seg(Tl, lambda t: np.ones_like(t))
    H = img.shape[0]

    def image(t):
        row = np.minimum((t / Tl).astype(np.int64), H - 1)
        col = np.minimum(((t % Tl) / Tl * W).astype(np.int64), W - 1)
        return img[row, col] / 255.0
    seg(H * Tl, image)
    if apt:
        seg(stop_s, lambda t: (np.sin(TAU * 450.0 * t) >= 0).astype(np.float64))
        seg(black_s, lambda t: np.zeros_like(t))
    v = np.concatenate(parts)
    return fm(af + (v - 0.5) * shift, fs, amp)


# ------------------------------------------------------------------ SSTV
def sstv_tones(img, mode):
    """Liste (fréquence, durée ms) de l'image complète, VIS compris (ordre et temps de la norme)."""
    from ..decoders.sstv import SSTV_MODES, rgb_to_ycc
    m = SSTV_MODES[mode]
    W, H = m["w"], m["h"]
    img = np.asarray(img)
    if img.shape[0] != H or img.shape[1] != W:
        yy = (np.arange(H) * img.shape[0] / H).astype(int)
        xx = (np.arange(W) * img.shape[1] / W).astype(int)
        img = img[yy][:, xx]
    img = img.astype(np.float64)
    out = [(1900, 300), (1200, 10), (1900, 300), (1200, 30)]
    vis = m["vis"]
    par = 0
    for i in range(7):
        b = (vis >> i) & 1
        par ^= b
        out.append((1100 if b else 1300, 30))
    out.append((1100 if par else 1300, 30))
    out.append((1200, 30))

    def f(v):
        return 1500.0 + 800.0 * np.clip(v, 0, 255) / 255.0

    def scan(vals, ms):
        px = ms / len(vals)
        return [(f(v), px) for v in vals]

    fam, c = m["fam"], m["scan"]
    ycc = rgb_to_ycc(img)
    if fam == "scottie":
        out.append((1200, 9))                    # impulsion de synchro de départ
    if fam in ("martin", "scottie", "wraase"):
        for y in range(H):
            R, G, B = img[y, :, 0], img[y, :, 1], img[y, :, 2]
            if fam == "martin":
                out += [(1200, m["sync"]), (1500, m["gap"])]
                for ch in (G, B, R):
                    out += scan(ch, c) + [(1500, m["gap"])]
            elif fam == "scottie":
                out += [(1500, 1.5)] + scan(G, c) + [(1500, 1.5)] + scan(B, c)
                out += [(1200, 9.0), (1500, 1.5)] + scan(R, c)
            else:
                out += [(1200, m["sync"]), (1500, m["porch"])]
                for ch in (R, G, B):
                    out += scan(ch, c)
    elif fam == "robot":
        for y in range(H):
            Y, Cb, Cr = ycc[y, :, 0], ycc[y, :, 1], ycc[y, :, 2]
            out += [(1200, 9.0), (1500, 3.0)] + scan(Y, m["yscan"])
            if mode == "robot36":
                if y % 2 == 0:
                    out += [(1500, 4.5), (1900, 1.5)] + scan(0.5 * (Cr + ycc[min(y + 1, H - 1), :, 2]), m["cscan"])
                else:
                    out += [(2300, 4.5), (1900, 1.5)] + scan(0.5 * (Cb + ycc[y - 1, :, 1]), m["cscan"])
            else:
                out += [(1500, 4.5), (1900, 1.5)] + scan(Cr, m["cscan"])
                out += [(2300, 4.5), (1900, 1.5)] + scan(Cb, m["cscan"])
    elif fam == "pd":
        for y in range(0, H, 2):
            a, b = ycc[y], ycc[min(y + 1, H - 1)]
            out += [(1200, 20.0), (1500, 2.08)]
            out += scan(a[:, 0], c) + scan((a[:, 2] + b[:, 2]) / 2, c)
            out += scan((a[:, 1] + b[:, 1]) / 2, c) + scan(b[:, 0], c)
    return out


def tones_to_audio(tones, fs=12000, af_shift=0.0, amp=0.5, ppm=0.0):
    """Synthèse à phase continue d'une suite (Hz, ms), en reportant les fractions d'échantillon."""
    rate = fs * (1 + ppm * 1e-6)
    fr = np.array([t[0] for t in tones], np.float64)
    ms = np.array([t[1] for t in tones], np.float64)
    ends = np.round(np.cumsum(ms) * rate / 1000.0).astype(np.int64)
    counts = np.diff(np.concatenate([[0], ends]))
    freq = np.repeat(fr, counts) + af_shift
    return fm(freq, fs, amp)


def sstv_encode(img, mode="martin1", fs=12000, af=1900.0, amp=0.5, ppm=0.0):
    return tones_to_audio(sstv_tones(img, mode), fs, af - 1900.0, amp, ppm)


# ------------------------------------------------------------------ Hellschreiber
def _feld_font():
    from ..decoders.hell import FELD7X7_14
    return FELD7X7_14


HELL_GEN = {
    # mode : (colonnes/s, type, largeur de bande / déplacement FSK)
    "feld": (17.5, "am", 0), "slow": (2.1875, "am", 0), "x5": (87.5, "am", 0), "x9": (157.5, "am", 0),
    "fsk245": (17.5, "fsk", 122.5), "fsk105": (17.5, "fsk", 55.0), "hell80": (35.0, "fsk", 300.0),
}


def hell_columns(text):
    """Colonnes de 14 bits (bit 0 = rangée du bas, émise en premier), comme feld::tx_char."""
    font = _feld_font()
    cols = []
    null = [0]

    def ch_cols(c):
        out = []
        g = font.get(c)
        if g is None:
            return out
        ordbits = 0
        for v in g:
            ordbits |= v
        for col in range(16):
            mask = 1 << (15 - col)
            bits = 0
            for row in range(14):
                if g[13 - row] & mask:
                    bits |= 1 << row
            testval = (1 << (15 - col)) - 1
            if bits == 0 and (ordbits & testval) == 0:
                break
            out.append(bits)
        return out

    def tx_char(c):
        cols.extend(null)
        if c == " ":
            cols.extend(null * 3)
        else:
            cols.extend(ch_cols(c))
        cols.extend(null)

    for _ in range(3):
        tx_char(".")
    for c in text:
        if c in "\r\n":
            c = " "
        tx_char(c)
    for _ in range(3):
        tx_char(".")
    tx_char(" ")
    return cols


def hell_encode(text, mode="feld", fs=12000, af=1000.0, amp=0.5, pulse=0, reverse=False, ppm=0.0):
    """Émission Hellschreiber suivant feld::send_symbol (mise en forme en cosinus surélevé de 4 ms
    pour pulse=0, 2 ms pour 1, 1 ms pour 2, carré pour 3 ; durées à 8000 Hz)."""
    colrate, kind, bw = HELL_GEN[mode]
    pixrate = colrate * 14
    rate = fs * (1 + ppm * 1e-6)
    cols = hell_columns(text)
    bits = []
    for c in cols:
        for i in range(14):
            bits.append((c >> i) & 1)
    bits = np.array(bits, np.int8)
    nb = len(bits)
    ends = np.round(np.arange(1, nb + 1) * rate / pixrate).astype(np.int64)
    counts = np.diff(np.concatenate([[0], ends]))
    if kind == "fsk":
        sgn = np.where(bits == 1, -1.0, 1.0) * (-1 if reverse else 1)
        freq = af + np.repeat(sgn, counts) * bw / 2
        return fm(freq, fs, amp)
    # AM : enveloppe de feld::send_symbol — premier pixel allumé en montée (OnShape), dernier pixel
    # d'une suite (bit suivant de la même colonne à 0) en descente (OffShape), sinon plein
    ramp_n = {0: 33, 1: 16, 2: 8, 3: 0}[pulse]
    rows = np.tile(np.arange(14), len(cols))
    prev = np.concatenate([[0], bits[:-1]])
    nxt = np.where(rows < 13, np.concatenate([bits[1:], [0]]), 0)
    starts = np.concatenate([[0], ends[:-1]])
    n = int(ends[-1])
    pix = np.repeat(np.arange(nb), counts)
    u = (np.arange(n) - starts[pix]) * (8000.0 / rate)        # rang de l'échantillon à 8000 Hz
    env = bits[pix].astype(np.float64)
    if ramp_n:
        on = 0.5 * (1 - np.cos(np.pi * np.minimum(u, ramp_n) / ramp_n))
        on = np.where(u < 32, on, 1.0)
        off = np.where(u < 32, 0.5 * (1 - np.cos(np.pi * np.clip(31 - u, 0, ramp_n) / ramp_n)), 0.0)
        rise = (bits[pix] == 1) & (prev[pix] == 0)
        fall = (bits[pix] == 1) & (prev[pix] == 1) & (nxt[pix] == 0)
        env = np.where(rise, on, np.where(fall, off, env))
    t = np.arange(n) / fs
    return amp * env * np.sin(TAU * af * t)
