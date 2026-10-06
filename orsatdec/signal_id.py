"""Identification automatique des signaux (d'après la base Artemis / sigidwiki).

Trois étages :
  1. measure()  : mesures sur quelques secondes d'audio BLU — largeur occupée, nombre de tonalités et
                  écart, vitesse de manipulation, enveloppe constante ou non, ordre PSK, période d'ACF,
                  taux de « trous » (OOK/CW) ;
  2. identify() : compare ces mesures aux signaux de la base (orsatdec/data/sigid.json) : paramètres
                  déclarés (largeur, modulation, fréquences) et empreinte mesurée sur l'enregistrement
                  de référence de chaque signal ; renvoie les candidats classés ;
  3. confirm()  : fait tourner les décodeurs d'Orsat-Decoder des candidats décodables et garde celui
                  qui sort un texte lisible (la preuve la plus sûre).

La base est générée par tools/make_sigid.py à partir d'Artemis-DB (GPL-3, données sigidwiki).
"""
import json
import re
from pathlib import Path

import numpy as np

DATA = Path(__file__).resolve().parent / "data" / "sigid.json"

# signaux Artemis (pageid) qu'Orsat-Decoder sait décoder -> mode et paramètres à essayer
DECODABLE = {
    140: [("psk31", {}), ("psk63", {}), ("psk125", {})],
    197: [("rtty", {"baud": b, "shift": s, "reverse": r}) for b in (45.45, 50.0, 75.0)
          for s in (170.0, 425.0, 450.0, 850.0) for r in (False, True)],
    567: [("cw", {})],
    620: [("navtex", {"reverse": r}) for r in (False, True)],
    4702: [("ft8", {})],
    5624: [("ft4", {})],
    200: [("mfsk-" + m, {}) for m in ("16", "8", "22", "32", "4", "11", "31", "64", "128")],
    832: [("dominoex-" + m, {}) for m in ("11", "16", "22", "8", "5", "4", "44", "88", "micro")],
    1421: [("thor-" + m, {}) for m in ("11", "16", "22", "8", "5", "4", "25", "50", "100", "micro")],
    504: [("olivia", {"tones": t, "bw": b}) for t, b in ((8, 250), (16, 500), (32, 1000), (8, 500), (4, 125),
                                                      (16, 1000), (64, 2000), (4, 250))],
    1236: [("contestia", {"tones": t, "bw": b}) for t, b in ((8, 250), (4, 250), (8, 500), (16, 500),
                                                          (32, 1000), (4, 125))],
    1370: [("mt63_1000", {}), ("mt63_500", {}), ("mt63_2000", {})],
    99: [("wefax", {})],
    960: [("sstv", {})],
    1309: [("hell", {})],
    # jalon 3
    296: [("sitora", {"reverse": r}) for r in (False, True)],
    955: [("packet300", {})],
    429: [("dsc", {})],
    2213: [("dsc", {})],
    2276: [("selcal", {})],
    1254: [("dcf77", {})], 7622: [("msf", {})], 1836: [("tdf", {})], 3716: [("jjy", {})], 5207: [("wwvb", {})],
    697: [("wwv", {})], 2060: [("chu", {})],
    201: [("ale", {})],
    3721: [("fsq", {}), ("ifkp", {})],
    1443: [(m, {}) for m in ("throb1", "throb2", "throb4", "throbx1", "throbx2", "throbx4")],
    140: [("psk31", {}), ("psk63", {}), ("psk125", {}), ("psk250", {}), ("psk500", {}), ("qpsk31", {}),
          ("qpsk63", {}), ("qpsk125", {}), ("psk125r", {}), ("psk250r", {}), ("psk500r", {})],
    1981: [("ascii", {"baud": b, "shift": s, "bits": n, "reverse": r}) for b in (110.0, 75.0, 150.0, 300.0, 50.0)
           for s in (170.0, 425.0, 850.0) for n in (7, 8) for r in (False, True)],
}

MOD_CLASS = {   # modulation déclarée dans la base -> grande famille
    "FSK": "fsk", "AFSK": "fsk", "GFSK": "fsk", "MSK": "fsk", "GMSK": "fsk", "IFK": "fsk", "CWFSK": "fsk",
    "PSK": "psk", "BPSK": "psk", "QPSK": "psk", "OQPSK": "psk", "D8PSK": "psk", "SDPSK": "psk", "DQPSK": "psk",
    "8PSK": "psk", "APSK": "psk",
    "QAM": "ofdm", "OFDM": "ofdm",
    "OOK": "ook", "CW": "ook", "ASK": "ook", "Pulse": "ook", "PPM": "ook",
    "AM": "analog", "FM": "analog", "USB": "analog", "LSB": "analog", "VSB": "analog", "SSB": "analog",
}

MEASURE_MAX = 15.0       # secondes d'audio au plus pour les mesures
FEATS = ("bw", "ntones", "spacing", "baud", "envvar", "psk", "acf", "gaps")


# --------------------------------------------------------------------------------------------- mesures

def _spectrum(x, fs, n):
    n = min(n, 1 << int(np.log2(max(256, len(x) // 2))))
    seg = np.lib.stride_tricks.sliding_window_view(x, n)[::n // 2]
    if len(seg) > 300:
        seg = seg[np.linspace(0, len(seg) - 1, 300).astype(int)]
    p = np.mean(np.abs(np.fft.rfft(seg * np.hanning(n), axis=1)) ** 2, axis=0)
    return np.fft.rfftfreq(n, 1 / fs), p


def _smooth(v, w):
    w = max(1, int(w))
    return np.convolve(v, np.ones(w) / w, mode="same") if w > 1 else v


def _occupied(f, p, lo, hi, near=None):
    """Zone occupée par le signal principal. Le bruit est le 20e centile du spectre de la bande audio (la
    médiane monte quand plusieurs signaux occupent la bande) ; on part du segment au-dessus du bruit
    (+6 dB) le plus fort, ou de celui qui est sous le clic, on y joint l'autre tonalité d'un FSK large,
    puis on garde 99 % de la puissance utile."""
    k = (f >= lo) & (f <= hi)
    fk, pk = f[k], p[k]
    df = fk[1] - fk[0]
    sm = _smooth(pk, round(12 / df))
    db = 10 * np.log10(sm + 1e-20)
    floor_db = np.percentile(db, 20)
    peak = db.max()
    if peak - floor_db < 6:
        return None
    floor = 10 ** (floor_db / 10)
    above = db > max(floor_db + 6, peak - 40)
    segs, i = [], 0
    while i < len(above):
        if above[i]:
            j = i
            while j + 1 < len(above) and above[j + 1]:
                j += 1
            segs.append([i, j, db[i:j + 1].max(), db[i:j + 1].max() - np.median(db[i:j + 1])])
            i = j + 1
        else:
            i += 1
    joined = [segs[0]]                 # creux étroits à l'intérieur d'un signal large : on recolle
    for sg in segs[1:]:
        if (sg[0] - joined[-1][1]) * df < 25:
            a0 = joined[-1][0]
            seg = db[a0:sg[1] + 1]
            joined[-1] = [a0, sg[1], seg.max(), seg.max() - np.median(seg)]
        else:
            joined.append(sg)
    segs = joined
    best = max(segs, key=lambda s: s[2])
    if near is not None:              # clic sur un signal : celui qui est sous le clic (±150 Hz) l'emporte
        for reach in (150, 500):      # 500 Hz : clic au milieu des deux tonalités d'un FSK large
            close = [s for s in segs if fk[s[0]] - reach <= near <= fk[s[1]] + reach
                     and (s[1] - s[0] + 1) * df >= 15 and s[2] - floor_db >= 9]
            if close:
                best = max(close, key=lambda s: s[2])
                break
    a, b, top, peaky = best

    def alike(s):
        # l'autre tonalité d'un FSK : même niveau, et comme elle une raie qui domine son segment
        # (un Olivia ou un MFSK voisin a un spectre plat : il n'est pas joint)
        w, w0 = (s[1] - s[0] + 1) * df, (best[1] - best[0] + 1) * df
        if w < 15 or abs(s[2] - top) >= 6:
            return False
        narrow = max(w, w0) < 130 and max(w, w0) / min(w, w0) < 1.6      # deux raies étroites jumelles
        return (s[3] >= 8 and peaky >= 8) or narrow
    changed = True
    while changed:
        changed = False
        for s in segs:
            if s[1] < a and (a - s[1]) * df < 1000 and alike(s) and (b - s[0]) * df < 3600:
                a, changed = s[0], True
            elif s[0] > b and (s[0] - b) * df < 1000 and alike(s) and (s[1] - a) * df < 3600:
                b, changed = s[1], True
    # 99 % de la puissance au-dessus du bruit, dans la zone retenue élargie de 20 %
    m = max(2, min(int((b - a) * 0.2), int(round(40 / df))))     # flancs, sans déborder sur un voisin
    a2, b2 = max(0, a - m), min(len(pk) - 1, b + m)
    u = np.clip(pk[a2:b2 + 1] - floor, 0, None)
    c = np.cumsum(u)
    if c[-1] <= 0:
        return None
    i1 = a2 + int(np.searchsorted(c, 0.005 * c[-1]))
    i2 = a2 + int(np.searchsorted(c, 0.995 * c[-1]))
    return fk[i1], fk[i2], floor_db, peak - floor_db


def _tones(f, p, f1, f2):
    """Tonalités nettes (FSK, MFSK) : pics du spectre fin, dominant leur voisinage d'au moins 6 dB et à
    moins de 15 dB du plus fort. Renvoie (nombre, écart médian, irrégularité)."""
    k = (f >= f1 - 5) & (f <= f2 + 5)
    if k.sum() < 5:
        return 0, 0.0, 1.0
    fk = f[k]
    db = 10 * np.log10(p[k] + 1e-20)
    df = fk[1] - fk[0]
    W = max(2, int(round(4 / df)))          # voisinage ±4 Hz
    cand = []
    for i in range(len(db)):
        lo, hi = max(0, i - W), min(len(db), i + W + 1)
        if db[i] < db[lo:hi].max() or db[i] < db.max() - 15:
            continue
        L, H = max(0, i - 6 * W), min(len(db), i + 6 * W + 1)
        ring = np.concatenate([db[L:lo], db[hi:H]])
        if len(ring) and db[i] - np.median(ring) >= 6:
            cand.append(i)
    pf = np.sort(fk[cand]) if cand else np.array([])
    if len(pf) >= 2:
        d = np.diff(pf)
        return int(len(pf)), float(np.median(d)), float(np.std(d) / (np.mean(d) + 1e-9))
    return int(len(pf)), 0.0, 1.0


def _analytic_band(x, fs, f1, f2):
    """Signal complexe en bande de base limité à [f1, f2] (masque FFT)."""
    n = len(x)
    X = np.fft.fft(x)
    f = np.fft.fftfreq(n, 1 / fs)
    z = np.fft.ifft(X * ((f >= f1) & (f <= f2))) * 2
    return z * np.exp(-2j * np.pi * (f1 + f2) / 2 * np.arange(n) / fs)


def _line(sig, fs, fmin, fmax):
    """Raie la plus saillante de sig entre fmin et fmax, saillance mesurée par rapport à la médiane
    locale (±25 %) pour ne pas confondre la pente du spectre avec une raie : (fréquence, dB)."""
    n = len(sig)
    if n < 256:
        return 0.0, 0.0
    S = np.abs(np.fft.rfft((sig - np.mean(sig)) * np.hanning(n))) ** 2
    f = np.fft.rfftfreq(n, 1 / fs)
    df = f[1]
    S = _smooth(S, max(1, round(0.3 / df)))
    k = np.nonzero((f >= fmin) & (f <= fmax))[0]
    if len(k) < 16:
        return 0.0, 0.0
    best = (0.0, 0.0)
    order = k[np.argsort(S[k])[::-1][:40]]
    for i in order:
        lo = np.searchsorted(f, f[i] * 0.75)
        hi = np.searchsorted(f, f[i] * 1.25)
        ref = np.median(S[lo:hi]) + 1e-20
        sal = 10 * np.log10(S[i] / ref)
        if sal > best[1]:
            best = (float(f[i]), float(sal))
    return best


def _psk_order(z):
    """Ordre M d'un PSK : z^M fait apparaître une raie franche que z seul n'a pas."""
    zn = z / (np.abs(z) + 1e-12)
    w = np.hanning(len(zn))

    def sal(v):
        S = np.abs(np.fft.fft(v * w)) ** 2
        return 10 * np.log10(S.max() / (np.median(S) + 1e-20))
    s1 = sal(zn)
    for M in (2, 4, 8):
        sM = sal(zn ** M)
        if sM > 28 and sM > s1 + 12:
            return M, sM
    return 0, s1


def _acf(z, fs2):
    """Période (ms) du premier vrai pic d'autocorrélation entre 5 ms et 1,2 s (trames, préambules)."""
    z = z[: int(fs2 * 20)]
    z = z - np.mean(z)
    N = len(z)
    nf = 1 << int(np.ceil(np.log2(2 * N)))
    Z = np.fft.fft(z, nf)
    ac = np.abs(np.fft.ifft(Z * np.conj(Z))[:N])
    ac = ac / (ac[0] + 1e-12)
    l0, l1 = int(fs2 * 0.005), min(N - 2, int(fs2 * 1.2))
    if l1 <= l0 + 8:
        return 0.0
    seg = ac[l0:l1]
    i = int(np.argmax(seg))
    if i == 0 or seg[i] < 0.3:
        return 0.0
    if seg[:i].min() > 0.7 * seg[i]:          # pas de creux avant : simple décroissance, pas une période
        return 0.0
    return float((l0 + i) / fs2 * 1000)



def _comb(pf):
    """Écart d'un peigne de tonalités dont on ne voit qu'une partie : le plus grand écart dont tous
    les intervalles observés sont des multiples entiers. Renvoie (tonalités couvertes, écart, irrégularité)."""
    d = np.diff(np.sort(pf))
    d = d[d > 0.5]
    if len(d) == 0:
        return 0, 0.0, 1.0
    best = None
    for c in sorted(set(np.round(d, 1)), reverse=True):
        r = d / c
        if np.all(np.round(r) >= 1) and np.mean(np.abs(r - np.round(r))) < 0.12:
            best = c
            break
    if best is None:
        return int(len(pf)), float(np.median(d)), float(np.std(d) / (np.mean(d) + 1e-9))
    k = np.round(d / best)
    sp = float(np.sum(d) / np.sum(k))                  # écart affiné sur tout le peigne
    n = int(round((np.max(pf) - np.min(pf)) / sp)) + 1
    return n, sp, float(np.mean(np.abs(d / sp - k)))


def _hist_tones(inst, bw):
    """Tonalités d'un FSK d'après l'histogramme de la fréquence instantanée (lissée sur deux périodes
    de la largeur de bande) : les symboles s'empilent sur chaque tonalité."""
    if len(inst) < 100:
        return 0, 0.0, 1.0
    step = max(0.5, bw / 600)
    edges = np.arange(-bw / 2 - 5, bw / 2 + 5 + step, step)
    h, _ = np.histogram(inst, edges)
    h = _smooth(h.astype(float), 3)
    if h.max() <= 0:
        return 0, 0.0, 1.0
    pk = [j for j in range(1, len(h) - 1) if h[j] >= h[j - 1] and h[j] > h[j + 1] and h[j] > 0.2 * h.max()]
    if len(pk) < 2:
        return len(pk), 0.0, 1.0
    return _comb(edges[pk] + step / 2)


def _fundamental(sig, fs, best):
    """Une raie de manipulation a souvent ses harmoniques plus fortes que le fondamental (inversions de
    phase, transitions FSK) : on descend au plus petit sous-multiple encore nettement présent."""
    f0, s0 = best
    n = len(sig)
    S = np.abs(np.fft.rfft((sig - np.mean(sig)) * np.hanning(n))) ** 2
    f = np.fft.rfftfreq(n, 1 / fs)
    S = _smooth(S, max(1, round(0.3 / f[1])))
    res = best
    for k in range(2, 9):
        fk = f0 / k
        if fk < 1.0:
            break
        i = int(np.argmin(np.abs(f - fk)))
        j = slice(max(0, i - 2), i + 3)
        ii = j.start + int(np.argmax(S[j]))
        lo, hi = np.searchsorted(f, f[ii] * 0.75), np.searchsorted(f, f[ii] * 1.25)
        sal = 10 * np.log10(S[ii] / (np.median(S[lo:hi]) + 1e-20))
        if sal >= max(10.0, s0 - 12):
            res = (float(f[ii]), float(sal))
    return res

def measure(x, fs, lo=100.0, hi=3600.0, near=None):
    """Mesures sur l'audio (réel, quelques secondes). Renvoie un dict, ou None s'il n'y a pas de signal."""
    x = np.asarray(x, np.float64)
    if len(x) < fs * 2:
        return None
    x = x[: int(fs * MEASURE_MAX)]          # mesures toujours sur la même durée (empreintes comparables)
    x = x / (np.std(x) + 1e-12)
    f, p = _spectrum(x, fs, 16384)
    occ = _occupied(f, p, lo, hi, near)
    if occ is None:
        return None
    f1, f2, floor, snr = occ
    bw = max(f2 - f1, 2 * (f[1] - f[0]))
    out = {"f1": float(f1), "f2": float(f2), "fc": float((f1 + f2) / 2), "bw": float(bw), "snr": float(snr)}
    out["ntones"], out["spacing"], out["regular"] = _tones(f, p, f1, f2)

    pad = max(8.0, 0.15 * bw)
    D = max(1, int(fs // max(6 * (bw + 2 * pad), 200)))
    z = _analytic_band(x, fs, f1 - pad, f2 + pad)[::D]
    fs2 = fs / D
    env = np.abs(z)
    on = env > 0.3 * np.percentile(env, 95)
    out["gaps"] = float(1 - on.mean())                       # part du temps sans signal (OOK, CW, ARQ)
    e = env[on]
    out["envvar"] = float(np.std(e) / (np.mean(e) + 1e-12)) if len(e) > 10 else 1.0

    # fréquence instantanée lissée (discriminateur FM) : sert aux tonalités FSK et à la vitesse
    inst = np.diff(np.unwrap(np.angle(z + 1e-12))) * fs2 / (2 * np.pi)
    inst = np.clip(inst, -bw, bw)
    smooth_n = max(1, int(fs2 / max(bw, 20) * 2))      # deux périodes de la largeur de bande
    inst_s = _smooth(inst, smooth_n)
    if out["envvar"] < 0.4:                     # enveloppe à peu près constante (bruit compris) : FSK, MFSK, MSK
        n2, sp2, rg2 = _hist_tones(inst_s[on[1:]], bw)
        if n2 >= 2 and sp2 >= max(3.0, bw / 80) and rg2 < 0.35:
            out["ntones"], out["spacing"], out["regular"] = n2, sp2, rg2

    # vitesse de manipulation : raie de |z|² (PSK, OFDM) ou des sauts de fréquence (FSK)
    zg = np.where(on, z, 0)
    jumps = np.abs(np.diff(inst_s)) * on[2:]
    fmax = min(fs2 / 2.2, 1.6 * bw + 50)
    b1 = _line(np.abs(zg) ** 2, fs2, 1.0, fmax)
    b2 = _line(jumps, fs2, 1.0, fmax)
    b = max(b1, b2, key=lambda t: t[1])
    if b[1] > 8:
        src = np.abs(zg) ** 2 if b is b1 else jumps
        b = _fundamental(src, fs2, b)
    out["baud"], out["baud_sal"] = (b[0], b[1]) if b[1] > 8 else (0.0, b[1])
    out["psk"], out["psk_sal"] = _psk_order(z[on] if on.sum() > fs2 else z)
    out["acf"] = _acf(z, fs2)
    return out


# ------------------------------------------------------------------------------------- comparaison

_DB = None


def load_db(path=DATA):
    global _DB
    if _DB is None:
        _DB = json.loads(Path(path).read_text(encoding="utf-8"))["signals"]
    return _DB


def measured_class(m):
    """Grande famille d'après les mesures, avec un poids par famille (0..1)."""
    w = {"fsk": 0.0, "psk": 0.0, "ofdm": 0.0, "ook": 0.0, "analog": 0.0}
    if m["gaps"] > 0.35 and m["bw"] < 400:
        w["ook"] += 1.0
    if m["envvar"] < 0.25:
        w["fsk"] += 1.0 if m["ntones"] >= 2 else 0.5
    if m["psk"]:
        w["psk"] += 1.0
    if m["envvar"] > 0.45 and m["bw"] > 1200:
        w["ofdm"] += 0.8
        w["analog"] += 0.4
    if m["envvar"] > 0.25 and not m["psk"] and m["ntones"] >= 6 and m["regular"] < 0.25:
        w["ofdm"] += 0.5
    if m["baud_sal"] < 10 and m["envvar"] > 0.45:
        w["analog"] += 0.6
    return w


def _ratio(a, b):
    """Écart logarithmique entre deux grandeurs positives (None si l'une manque)."""
    if not a or not b:
        return None
    return abs(np.log2(a / b))


def _baud_ratio(a, b):
    """Comme _ratio, mais une erreur d'harmonique (×2, ×3) ne coûte qu'à moitié : la raie de
    manipulation mesurée tombe parfois sur un multiple de la vraie vitesse."""
    r = _ratio(a, b)
    if r is None:
        return None
    for h in range(1, 13):
        rh = abs(abs(np.log2(a / b)) - np.log2(h))
        if h > 1 and rh < 0.05:
            return min(r, 0.1)            # la mesure a pu tomber sur une harmonique : écart modéré
    return r


def _fsk_like(m):
    return m["envvar"] < 0.4 and m["ntones"] >= 2 and m["spacing"] >= 3 and not m.get("psk")


def fingerprint_distance(m, fp):
    """Distance (≈ 0 identique, ≥ 2 sans rapport) entre nos mesures et l'empreinte d'un signal."""
    terms = []                                       # (écart normalisé, poids)
    r = _ratio(m["bw"], fp["bw"])
    terms.append((min(r / 0.35, 4), 2.0))
    r = _baud_ratio(m.get("baud"), fp.get("baud"))
    if r is not None:
        terms.append((min(r / 0.08, 4), 2.0))
    elif bool(m.get("baud")) != bool(fp.get("baud")):
        terms.append((1.0, 0.7))
    if _fsk_like(m) and _fsk_like(fp):
        terms.append((min(_ratio(m["spacing"], fp["spacing"]) / 0.1, 4), 1.5))
        terms.append((min(abs(np.log2(m["ntones"] / fp["ntones"])) / 0.5, 4), 1.0))
    elif _fsk_like(m) != _fsk_like(fp):
        terms.append((1.5, 1.0))
    if m.get("acf") or fp.get("acf"):
        r = _ratio(m.get("acf"), fp.get("acf"))
        terms.append(((min(r / 0.05, 4) if r is not None else 2.0), 1.5))
    terms.append((min(abs(m["envvar"] - fp["envvar"]) / 0.15, 4), 1.0))   # le bruit fait monter cette mesure
    terms.append((2.0 * (m["psk"] != fp["psk"]), 1.0))
    terms.append((min(abs(m["gaps"] - fp["gaps"]) / 0.12, 4), 1.0))
    return sum(d * w for d, w in terms) / sum(w for _, w in terms)


def identify(m, freq=None, top=8, db=None):
    """Candidats classés pour les mesures m. freq (Hz, facultatif) : fréquence radio du signal, qui
    écarte les signaux connus ailleurs. Renvoie [{id, title, score, why, decodable}] (score : plus
    haut = plus probable ; au-dessus de 0, ressemblance nette)."""
    db = db or load_db()
    cls = measured_class(m)
    res = []
    for s in db:
        sc, why, variant = 0.0, [], None
        fp = s.get("fp")
        refs = ([fp] if fp else []) + s.get("fps", [])
        if refs:
            dists = [(fingerprint_distance(m, r), r) for r in refs]
            dfp, ref = min(dists, key=lambda t: t[0])
            synth = [t for t in dists if t[1].get("mode")]
            if synth:                                # sous-mode le plus proche, même si l'enregistrement réel l'emporte
                ref = min(synth, key=lambda t: t[0])[1]
                variant = {"label": ref["label"], "mode": ref["mode"], "params": ref.get("params", {})}
            sc += 3.0 * (1 - min(dfp, 2.5))
            if dfp < 0.5:
                why.append("empreinte")
        if not refs:
            sc -= 1.5                                # sans enregistrement de référence : jamais devant une empreinte proche
        wmeta = 0.35 if refs else 1.0                 # les paramètres déclarés sont souvent larges ou approximatifs
        if s.get("bw"):
            r = abs(np.log2(max(m["bw"], 5) / s["bw"]))
            sc -= wmeta * min(r, 3) * 1.2
            if r < 0.4:
                why.append("largeur")
        mods = {MOD_CLASS.get(x) for x in s.get("mod", [])} - {None}
        if mods:
            c = max(cls[x] for x in mods)
            sc += wmeta * (1.2 * c - 0.6)
            if c >= 0.8:
                why.append("modulation")
        if freq is not None and s.get("fmin") is not None:
            lo, hi = s["fmin"], s["fmax"]
            if lo == hi:
                lo, hi = lo - 3000, hi + 3000
            if lo * 0.995 <= freq <= hi * 1.005:
                sc += 1.0 if hi - lo < 2e6 else 0.3     # une plage étroite en dit plus long
                why.append("fréquence")
            else:
                sc -= 2.0
        if s.get("acf") and m.get("acf"):
            r = min(abs(np.log2(m["acf"] / a)) for a in s["acf"] if a)
            if r < 0.05:
                sc += 1.5
                why.append("ACF")
        res.append({"id": s["id"], "title": s["title"], "score": round(float(sc), 2), "why": why,
                    "decodable": s["id"] in DECODABLE, "variant": variant})
    res.sort(key=lambda r: -r["score"])
    return res[:top]


# ------------------------------------------------------------------------------------ confirmation

# Mots qu'on rencontre dans le trafic radio (amateur, maritime, météo) et dans les textes d'essai
_KNOWN = set("""
CQ DE VVV K KN SK AR BK TU TNX TKS 73 88 QSL QSO QTH QRZ QRM QRN QSB QSY QRT QRV QRP QRO QTC RST UR ES OM YL XYL
GM GA GE GN HI HW CPY FB NAME OP RIG ANT WX PSE AGN TEST CONTEST NR DX BAND MHZ KHZ WPM BEST REGARDS
ZCZC NNNN RYRY RYRYRY RY SG TTTT MSG MESSAGE WARNING NAVTEX NAVAREA SECURITE GALE STORM FORECAST WIND WAVE
SEA LOW HIGH PRESSURE SYNOP METAR TAF NORTH SOUTH EAST WEST AREA POSITION
THE AND OF TO IS IN FOR ON WITH AT FROM BY THIS THAT ARE BE IT AS ALL HERE THERE YOU YOUR MY ME WE
QUICK BROWN FOX JUMPS JUMPED OVER LAZY DOG DOGS BACK 0123456789 1234567890 ABCDEFGHIJKLMNOPQRSTUVWXYZ
LE LA LES DE DU DES ET EN UN UNE EST POUR SUR AVEC BONJOUR MERCI SALUT
""".split())
_CALL = re.compile(r"^(?:[A-Z]{1,2}|[0-9][A-Z]|[A-Z][0-9])[0-9][A-Z]{1,4}(?:/[A-Z0-9]{1,4})?$")


def plausible(text):
    """Lisibilité d'un texte décodé (0 = charabia). Des lettres au hasard (un décodeur RTTY sur du
    bruit en sort facilement) ne suffisent pas : il faut des mots connus du trafic radio, des
    indicatifs, ou des mots qui se répètent comme dans toute vraie émission."""
    t = text.strip()
    if len(t) < 8:
        return 0.0
    ok = sum(c.isascii() and (c.isalnum() or c in " .,:;-/?'()=+\n\r") for c in t) / len(t)
    if ok < 0.95:
        return 0.0
    toks = re.findall(r"[A-Z0-9/]+", t.upper())
    if not toks:
        return 0.0
    known = sum(w in _KNOWN for w in toks)
    calls = sum(bool(_CALL.match(w)) and any(ch.isdigit() for ch in w) for w in toks)
    # mots répétés (CQ CQ, indicatif répété…) : seulement s'ils ont l'air de mots, et seulement en
    # renfort d'un mot connu ou d'un indicatif (un décodeur sur du bruit répète aussi ses « RRR »)
    long = [w for w in toks if len(w) >= 3 and len(set(w)) >= 2 and any(v in w for v in "AEIOUY")]
    rep = len(long) - len(set(long)) if known + calls else 0
    sc = 3 * known + 3 * calls + 2 * rep
    return float(sc * ok ** 2)


def _variant_tries(c, modes):
    """Essais de décodage pour un candidat : sous-mode reconnu (et son sens inversé) d'abord,
    puis, s'il ne donne rien, toutes les variantes connues du mode."""
    v = c.get("variant")
    first = []
    if v:
        first.append((v["mode"], dict(v["params"])))
        mode = modes.get(v["mode"]) or {}
        if any(p["key"] == "reverse" for p in mode.get("params", [])):
            first.append((v["mode"], {**v["params"], "reverse": True}))
    rest = [t for t in DECODABLE.get(c["id"], []) if t not in first]
    return first, rest


def _decode_text(x, fs, af, mode, params):
    from .modes import default_params
    dec = mode["make"](fs, af, dict(default_params(mode), **params))
    txt = []
    for i in range(0, len(x), 480):
        for ev in dec.process(x[i:i + 480]):
            if ev.get("t") == "text":
                txt.append(ev["text"])
            elif ev.get("t") == "msg" and not ev.get("error"):
                txt.append(ev.get("text", "") + "\n")
    return "".join(txt)


def confirm(x, fs, m, candidates, modes, tail=12.0, stop=None):
    """Fait décoder x par les candidats décodables ; renvoie (mode, params, texte, note) du meilleur,
    ou None. modes : dict id -> entrée de orsatdec.modes (fabrique « make »). stop() : interruption."""
    xs = np.concatenate([x, np.random.default_rng(0).normal(0, 1e-3 * np.std(x), int(fs * tail))])
    best = None
    for stage in (0, 1):
        # 1er passage : le sous-mode reconnu de chaque candidat ; 2e : toutes les variantes du premier seulement
        for c in (candidates if stage == 0 else candidates[:1]):
            for mode_id, params in _variant_tries(c, modes)[stage]:
                if stop and stop():
                    return None
                mode = modes.get(mode_id)
                if mode is None or mode.get("kind") in ("img", "ident") or mode.get("whole"):
                    continue
                t = _decode_text(xs, fs, m["fc"], mode, params)
                # un message protégé par un CRC (packet, DSC, ALE…) est une preuve à lui seul
                sc = 20.0 if mode.get("crc") and t.strip() else plausible(t)
                if best is None or sc > best[3]:
                    best = (mode_id, params, t, sc)
        if best and best[3] >= 9:
            return best
    return None


def analyse(x, fs, freq=None, modes=None, top=5, try_decoders=3, lo=100.0, hi=3600.0, on_measure=None, stop=None, near=None, also=()):
    """Chaîne complète : mesures, candidats, puis confirmation par décodage des candidats décodables.
    Un candidat confirmé passe en tête. Renvoie {"measure", "candidates", "confirmed"} ou None.
    on_measure(résultat sans confirmation) est appelé dès que les candidats sont connus.
    also : candidats d'une analyse précédente du même signal, essayés en premier."""
    m = measure(x, fs, lo, hi, near)
    if m is None:
        return None
    cand = identify(m, freq=freq, top=top)
    if on_measure:
        on_measure({"measure": m, "candidates": list(cand), "confirmed": None})
    conf = None
    if modes is not None:
        dec = [c for c in also if c.get("decodable")]
        dec += [c for c in cand if c["decodable"] and c["id"] not in {d["id"] for d in dec}]
        dec = dec[:max(try_decoders, len([c for c in also if c.get("decodable")]))]
        r = confirm(x, fs, m, dec, modes, stop=stop) if dec else None
        if r:
            mode_id, params, text, sc = r
            owner = next((c for c in dec if (c.get("variant") or {}).get("mode") == mode_id
                          or any(mode_id == t[0] for t in DECODABLE.get(c["id"], []))), None)
            conf = {"id": owner["id"] if owner else None, "mode": mode_id, "params": params,
                    "text": text, "score": round(float(sc), 1), "af": m["fc"]}
            if owner:
                if owner in cand:
                    cand.remove(owner)
                cand.insert(0, owner)
                # le sous-mode affiché est celui qui a décodé
                from .gen.sigid_synth import SYNTH
                lab = next((lb for lb, md, pr, _ in SYNTH.get(owner["id"], [])
                            if md == mode_id and all(params.get(k) == v for k, v in pr.items())), None)
                owner["variant"] = {"label": lab or modes[mode_id]["label"], "mode": mode_id, "params": params}
    return {"measure": m, "candidates": cand, "confirmed": conf}


def identify_file(path, freq_khz=None):
    """orsat-decoder --identify FICHIER.wav [FREQ_kHz] : identifie le signal d'un enregistrement audio."""
    import wave
    from fractions import Fraction
    from scipy.signal import resample_poly
    from .modes import BY_ID
    if not Path(path).is_file():
        print(f"Fichier introuvable : {path}")
        return 1
    try:
        w = wave.open(str(path))
    except (wave.Error, EOFError) as e:
        print(f"Fichier WAV illisible ({e}). Convertir par exemple avec : ffmpeg -i entree -ac 1 sortie.wav")
        return 1
    ch, sw, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
    raw = w.readframes(w.getnframes())
    w.close()
    dt = {1: np.uint8, 2: "<i2", 4: "<i4"}[sw]
    x = np.frombuffer(raw, dt).astype(np.float64)
    if sw == 1:
        x -= 128
    x = x.reshape(-1, ch).mean(axis=1)
    fs = 12000
    if rate != fs:
        fr = Fraction(fs, rate).limit_denominator(1000)
        x = resample_poly(x, fr.numerator, fr.denominator)
    x = x[: fs * 60]                                   # une minute suffit
    r = analyse(x, fs, freq=freq_khz * 1000 if freq_khz else None, modes=BY_ID)
    if r is None:
        print("Pas de signal net dans cet enregistrement.")
        return 1
    m = r["measure"]
    print(f"Signal : {m['f1']:.0f}-{m['f2']:.0f} Hz (largeur {m['bw']:.0f} Hz, {m['snr']:.0f} dB au-dessus du bruit)")
    det = [f"{m['ntones']} tonalités espacées de {m['spacing']:.2f} Hz" if m["ntones"] >= 2 and m["spacing"] else "",
           f"{m['baud']:.2f} bauds" if m["baud"] else "", f"PSK d'ordre {m['psk']}" if m["psk"] else "",
           f"ACF {m['acf']:.1f} ms" if m["acf"] else "", "enveloppe constante" if m["envvar"] < 0.15 else ""]
    print("Mesures : " + ", ".join(d for d in det if d))
    c = r["confirmed"]
    if c:
        print(f"Confirmé par décodage : {BY_ID[c['mode']]['label']} {c['params'] or ''}")
        print("  " + " ".join(c["text"].split())[:200])
    print("Candidats :")
    for i, k in enumerate(r["candidates"], 1):
        v = f" — {k['variant']['label']}" if k.get("variant") else ""
        print(f"  {i}. {k['title']}{v}  (note {k['score']:+.1f} ; {', '.join(k['why']) or 'ressemblance faible'})"
              f"{'  [décodable]' if k['decodable'] else ''}")
    return 0
