"""Signalisations par tonalités : DTMF, appels sélectifs à 5 tons (CCIR, ZVEI, EEA…), Selcal OACI.

- DTMF : deux tonalités simultanées (groupe bas 697-941 Hz, groupe haut 1209-1633 Hz), détectées par
  Goertzel sur des tranches de 20 ms ; un chiffre doit tenir 40 ms et être suivi d'un silence.
- Tons séquentiels : une seule tonalité à la fois (20 à 150 ms chacune). On repère la fréquence de chaque
  tranche, on découpe en tonalités stables, puis on choisit le standard (CCIR, ZVEI…) dont la table
  colle le mieux à la suite reçue ; le ton de répétition (E) redouble le chiffre précédent.
- Selcal OACI (aviation HF) : deux paires de tonalités simultanées d'une seconde, parmi 16 lettres.
"""
import time

import numpy as np

from ..dsp import Decoder


def _utc():
    return time.strftime("%H%M%S", time.gmtime())


def goertzel(frame, freqs, fs):
    """Puissance de frame aux fréquences données (vectorisé)."""
    n = len(frame)
    t = np.arange(n)
    w = np.hanning(n)
    ph = np.exp(-2j * np.pi * np.outer(freqs, t) / fs)
    return np.abs(ph @ (frame * w)) ** 2 / (w.sum() ** 2)


class _Frames:
    """Découpe le flux audio en tranches de `n` échantillons avec un pas `hop`."""

    def __init__(self, n, hop):
        self.n, self.hop = int(n), int(hop)
        self.buf = np.zeros(0)

    def feed(self, x):
        self.buf = np.concatenate([self.buf, np.asarray(x, np.float64)])
        out = []
        while len(self.buf) >= self.n:
            out.append(self.buf[:self.n])
            self.buf = self.buf[self.hop:]
        return out


# =====================================================================================================
DTMF_LOW = [697.0, 770.0, 852.0, 941.0]
DTMF_HIGH = [1209.0, 1336.0, 1477.0, 1633.0]
DTMF_KEYS = ["123A", "456B", "789C", "*0#D"]


class DTMF(Decoder):
    name = "DTMF"
    kind = "msg"

    def __init__(self, fs, af=0.0):
        super().__init__(fs, af)
        self.step = 0.010
        self.frames = _Frames(int(0.025 * self.fs), int(self.step * self.fs))
        self.freqs = np.array(DTMF_LOW + DTMF_HIGH + [x * 2 for x in DTMF_LOW])
        self.cur, self.run, self.silence = None, 0, 0
        self.digits = ""
        self.emitted = False
        self.count = 0

    def status(self):
        return {"last": self.count}

    def _detect(self, fr):
        e = np.mean(fr * fr) + 1e-15
        p = goertzel(fr, self.freqs, self.fs)
        lo, hi = p[:4], p[4:8]
        i, j = int(np.argmax(lo)), int(np.argmax(hi))
        # les deux tonalités dominent leur groupe, portent l'essentiel de l'énergie, torsion limitée
        if lo[i] < 6 * np.partition(lo, -2)[-2] or hi[j] < 6 * np.partition(hi, -2)[-2]:
            return None
        if (lo[i] + hi[j]) / e < 0.12 or not (0.1 < hi[j] / lo[i] < 10):
            return None
        if p[8 + i] > 0.2 * lo[i]:                                # harmonique : parole ou musique
            return None
        return DTMF_KEYS[i][j]

    def process(self, x):
        out = []
        for fr in self.frames.feed(x):
            k = self._detect(fr)
            if k is not None and k == self.cur:
                self.run += 1
                if self.run == 4 and not self.emitted:            # 40 ms tenus
                    self.digits += k
                    self.emitted = True
                self.silence = 0
            elif k is not None:
                self.cur, self.run, self.emitted = k, 1, False
                self.silence = 0
            else:
                self.silence += 1
                if self.silence >= 2:
                    self.cur, self.run, self.emitted = None, 0, False
                if self.digits and self.silence * self.step > 1.5:
                    self.count += 1
                    out.append({"t": "msg", "utc": _utc(), "text": f"DTMF {self.digits}"})
                    self.digits = ""
        return out


# =====================================================================================================
# Tables des appels sélectifs (chiffres 0-9, puis A B C D E F ; E = ton de répétition), durée nominale
SELCALL = {
    "CCIR": ([1981, 1124, 1197, 1275, 1358, 1446, 1540, 1640, 1747, 1860, 2400, 930, 2247, 991, 2110, 2400], 0.100),
    "EEA": ([1981, 1124, 1197, 1275, 1358, 1446, 1540, 1640, 1747, 1860, 1055, 930, 2247, 991, 2110, 2400], 0.040),
    "ZVEI1": ([2400, 1060, 1160, 1270, 1400, 1530, 1670, 1830, 2000, 2200, 2800, 810, 970, 885, 2600, 680], 0.070),
    "ZVEI2": ([2400, 1060, 1160, 1270, 1400, 1530, 1670, 1830, 2000, 2200, 885, 810, 740, 680, 970, 2600], 0.070),
    "ZVEI3": ([2200, 970, 1060, 1160, 1270, 1400, 1530, 1670, 1830, 2000, 885, 810, 740, 680, 2400, 2600], 0.070),
    "DZVEI": ([2200, 970, 1060, 1160, 1270, 1400, 1530, 1670, 1830, 2000, 825, 885, 740, 680, 2400, 2600], 0.070),
    "PZVEI": ([2400, 1060, 1160, 1270, 1400, 1530, 1670, 1830, 2000, 2200, 970, 810, 2800, 885, 2600, 680], 0.070),
    "EIA": ([600, 741, 882, 1023, 1164, 1305, 1446, 1587, 1728, 1869, 2151, 2433, 2010, 2292, 459, 1091], 0.033),
    "EURO": ([979.8, 903.1, 832.5, 767.4, 707.4, 652.0, 601.0, 554.0, 510.7, 470.8, 433.9, 400.0, 368.7, 1153.1,
              1062.9, 339.9], 0.100),
}
SYMS = "0123456789ABCDEF"


def match_selcall(tones, durs, prefer=None):
    """Suite de fréquences (Hz) -> (standard(s), chiffres, erreur moyenne en %) du standard le plus proche.
    Plusieurs standards partagent les mêmes chiffres (ZVEI…) : c'est alors le ton de répétition et
    l'absence de lettres A-F (rares dans un appel) qui tranchent ; les ex aequo sont tous cités."""
    res = []
    for name, (tab, dur) in SELCALL.items():
        if prefer and name != prefer:
            continue
        tab = np.asarray(tab, float)
        err, digits = 0.0, ""
        for f in tones:
            k = int(np.argmin(np.abs(tab - f)))
            e = abs(tab[k] - f) / tab[k]
            err += e
            s = SYMS[k]
            digits += digits[-1] if (s == "E" and digits) else s
        err = err / max(1, len(tones)) * 100
        derr = abs(np.median(durs) - dur) / dur
        letters = sum(c in "ABCDEF" for c in digits)
        res.append((name, digits, err, err + 3 * derr + 1.0 * letters))
    if not res:
        return None
    res.sort(key=lambda r: r[3])
    b = res[0]
    same = [r[0] for r in res if r[1] == b[1] and abs(r[3] - b[3]) < 0.05]
    return ("/".join(same), b[1], b[2], b[3])


class Selcall(Decoder):
    """Appels sélectifs à tons séquentiels (5 tons ou plus)."""
    name = "Selcall"
    kind = "msg"

    def __init__(self, fs, af=0.0, standard="auto", lo=280.0, hi=3000.0):
        super().__init__(fs, af)
        self.standard = None if standard == "auto" else standard
        self.step = 0.005
        self.nfft = 4096
        self.frames = _Frames(int(0.020 * self.fs), int(self.step * self.fs))
        self.lo, self.hi = lo, hi
        self.seq = []                  # (fréquence, durée) des tonalités de la suite en cours
        self.cur = []                  # fréquences des tranches de la tonalité en cours
        self.quiet = 0
        self.miss = 0
        self.count = 0

    def status(self):
        return {"last": self.count}

    def _peak(self, fr):
        w = np.hanning(len(fr))
        sp = np.abs(np.fft.rfft(fr * w, self.nfft)) ** 2
        f = np.fft.rfftfreq(self.nfft, 1 / self.fs)
        band = (f >= self.lo) & (f <= self.hi)
        sb = sp[band]
        k = int(np.argmax(sb))
        tot = sb.sum() + 1e-20
        # raie nette : puissance dans ±40 Hz autour d'elle, comparée au bruit (médiane) et au total
        df = f[1] - f[0]
        r = int(40 / df)
        pk = sb[max(0, k - r):k + r + 1].sum()
        noise = np.median(sb) * (2 * r + 1) + 1e-20
        if pk / noise < 15 or pk / tot < 0.25:
            return None
        if 0 < k < len(sb) - 1:                              # interpolation parabolique (log)
            a, b, c = np.log(sb[k - 1:k + 2] + 1e-30)
            d = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0.0
        else:
            d = 0.0
        return float(f[band][0] + (k + d) * df)

    def _close_tone(self):
        if len(self.cur) * self.step >= 0.018:
            self.seq.append((float(np.median(self.cur)), len(self.cur) * self.step))
        self.cur = []

    def _flush(self, out):
        self._close_tone()
        seq = []
        for f, d in self.seq:                                   # tonalité coupée en deux par le bruit
            if d >= 0.6:
                continue
            if seq and abs(seq[-1][0] - f) < 0.015 * f:
                seq[-1] = (seq[-1][0], seq[-1][1] + d)
            else:
                seq.append((f, d))
        self.seq = []
        if len(seq) < 4:
            return
        tones = [f for f, _ in seq]
        durs = [d for _, d in seq]
        r = match_selcall(tones, durs, self.standard)
        if r is None or r[2] > 2.5:
            txt = "tons inconnus : " + " ".join(f"{f:.0f}" for f in tones)
        else:
            txt = f"{r[0]} {r[1]}  ({np.median(durs) * 1000:.0f} ms, écart {r[2]:.1f} %)"
        self.count += 1
        out.append({"t": "msg", "utc": _utc(), "text": txt})

    def process(self, x):
        out = []
        for fr in self.frames.feed(x):
            f = self._peak(fr)
            if f is None:
                self.quiet += 1
                self.miss += 1
                if self.cur and self.miss > 2:                  # trou de plus de 10 ms : fin de tonalité
                    self._close_tone()
                if self.quiet * self.step > 0.15 and (self.seq or self.cur):
                    self._flush(out)
                continue
            self.quiet = self.miss = 0
            if self.cur and abs(f - np.median(self.cur[-4:])) > 0.015 * f:
                self._close_tone()
            self.cur.append(f)
            if len(self.cur) * self.step > 1.0:                 # porteuse ou sifflement, pas un appel
                self.cur, self.seq = [], []
        return out


# =====================================================================================================
ICAO_TONES = {"A": 312.6, "B": 346.7, "C": 384.6, "D": 426.6, "E": 473.2, "F": 524.8, "G": 582.1, "H": 645.7,
              "J": 716.1, "K": 794.3, "L": 881.0, "M": 977.2, "P": 1083.9, "Q": 1202.3, "R": 1333.5, "S": 1479.1}


class ICAOSelcal(Decoder):
    name = "Selcal"
    kind = "msg"

    def __init__(self, fs, af=0.0):
        super().__init__(fs, af)
        self.letters = list(ICAO_TONES)
        self.freqs = np.array([ICAO_TONES[k] for k in self.letters])
        self.step = 0.05
        self.frames = _Frames(int(0.1 * self.fs), int(self.step * self.fs))
        self.cur, self.run = None, 0
        self.pairs = []                 # (paire, instant de fin)
        self.t = 0.0
        self.count = 0

    def status(self):
        return {"last": self.count}

    def process(self, x):
        out = []
        for fr in self.frames.feed(x):
            self.t += self.step
            p = goertzel(fr, self.freqs, self.fs)
            e = np.mean(fr * fr) + 1e-15
            o = np.argsort(p)[::-1]
            pair = None
            if p[o[1]] > 8 * p[o[2]] and (p[o[0]] + p[o[1]]) / e > 0.1 and p[o[0]] < 6 * p[o[1]]:
                pair = "".join(sorted(self.letters[o[0]] + self.letters[o[1]]))
            if pair and pair == self.cur:
                self.run += 1
            else:
                if self.cur and 0.6 <= self.run * self.step <= 1.6:
                    self.pairs.append((self.cur, self.t))
                self.cur, self.run = pair, 1 if pair else 0
            self.pairs = [(q, t) for q, t in self.pairs if self.t - t < 2.0]
            if len(self.pairs) >= 2:
                (a, ta), (b, tb) = self.pairs[-2], self.pairs[-1]
                if 0.9 <= tb - ta <= 1.9 and a != b:
                    self.count += 1
                    out.append({"t": "msg", "utc": _utc(), "text": f"SELCAL {a}-{b}"})
                    self.pairs = []
        return out
