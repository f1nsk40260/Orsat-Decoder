"""Signaux horaires : DCF77, MSF, TDF (Allouis), WWVB, JJY, WWV/WWVH, CHU.

Réception en BLU avec la porteuse dans l'audio (cliquez sur la porteuse) :
- DCF77, MSF, WWVB : la porteuse baisse au début de chaque seconde ; la durée de la baisse code le bit ;
- JJY : c'est la durée de la pleine puissance qui code le bit ;
- TDF : modulation de phase (±1 rad) au début de chaque seconde, un élément de 100 ms (0) ou deux (1) ;
  le reste de la seconde porte une modulation de phase pseudo-aléatoire (synchronisation fine) : les
  éléments de données sont donc reconnus par leur forme (filtre adapté), pas par la seule présence de modulation ;
- WWV / WWVH : sous-porteuse à 100 Hz (impulsions de 0,2, 0,5 et 0,8 s) ;
- CHU : trames FSK à 300 bauds (Bell 103) aux secondes 31 à 39.
Une minute complète est nécessaire pour la date et l'heure ; d'ici là la carte montre la seconde en cours.

DCF77, TDF, MSF, WWVB et JJY sont lus « à l'horloge » : la période exacte de la seconde (l'audio d'un
WebSDR peut dériver de plus de 1000 ppm) et son début sont estimés en repliant les 40 dernières
secondes ; chaque seconde est ensuite lue dans des fenêtres fixes (0-100 ms, 100-200 ms…), comparées
aux niveaux habituels. Le bruit qui crée de fausses impulsions entre deux secondes est ainsi ignoré.
"""
import time

import numpy as np
from scipy.signal import firwin, lfilter

from ..dsp import Decoder, Mixer, ToneFinder

FR = 200.0                                    # cadence de l'enveloppe (Hz)
FRI = int(FR)
WEEKDAYS = ["", "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def _bcd(bits, weights):
    return sum(w for b, w in zip(bits, weights) if b)


def _even(bits):
    return sum(bits) % 2 == 0


# ----------------------------------------------------------------------------------------------- trames
def frame_dcf77(b, tdf=False):
    """b[0..58] -> texte, ou None si les parités sont fausses."""
    if len(b) < 59 or None in b[17:59] or b[20] != 1:
        return None
    if not (_even(b[21:29]) and _even(b[29:36]) and _even(b[36:59])):
        return None
    mi = _bcd(b[21:28], (1, 2, 4, 8, 10, 20, 40))
    hh = _bcd(b[29:35], (1, 2, 4, 8, 10, 20))
    dd = _bcd(b[36:42], (1, 2, 4, 8, 10, 20))
    wd = _bcd(b[42:45], (1, 2, 4))
    mo = _bcd(b[45:50], (1, 2, 4, 8, 10))
    yy = _bcd(b[50:58], (1, 2, 4, 8, 10, 20, 40, 80))
    if mi > 59 or hh > 23 or not 1 <= dd <= 31 or not 1 <= mo <= 12:
        return None
    zone = "heure d'été" if b[17] else ("heure d'hiver" if b[18] else "")
    extra = []
    if b[16]:
        extra.append("changement d'heure annoncé")
    if b[19]:
        extra.append("seconde intercalaire annoncée")
    if not tdf and b[15]:
        extra.append("bit d'appel")
    txt = f"{WEEKDAYS[wd] if wd < 8 else '?'} {dd:02d}/{mo:02d}/20{yy:02d} {hh:02d}:{mi:02d} {zone}".strip()
    return txt + (" (" + ", ".join(extra) + ")" if extra else "")


def frame_msf(a, b):
    """a, b : bits A et B des secondes 0..59 -> texte ou None."""
    if len(a) < 60 or None in a[17:52] or None in b[54:59]:
        return None
    par = lambda bits, p: (sum(bits) + p) % 2 == 1              # parité impaire
    if not (par(a[17:25], b[54]) and par(a[25:36], b[55]) and par(a[36:39], b[56]) and par(a[39:52], b[57])):
        return None
    yy = _bcd(a[17:25], (80, 40, 20, 10, 8, 4, 2, 1))
    mo = _bcd(a[25:30], (10, 8, 4, 2, 1))
    dd = _bcd(a[30:36], (20, 10, 8, 4, 2, 1))
    wd = _bcd(a[36:39], (4, 2, 1))
    hh = _bcd(a[39:45], (20, 10, 8, 4, 2, 1))
    mi = _bcd(a[45:52], (40, 20, 10, 8, 4, 2, 1))
    if mi > 59 or hh > 23 or not 1 <= dd <= 31 or not 1 <= mo <= 12:
        return None
    wname = ["dimanche", "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi"][wd] if wd < 7 else "?"
    return f"{wname} {dd:02d}/{mo:02d}/20{yy:02d} {hh:02d}:{mi:02d} " + ("BST" if b[58] else "GMT")


def frame_wwvb(s, jjy=False):
    """s[0..59] : 0, 1 ou 'M' (repère) -> texte ou None."""
    if len(s) < 60 or any(s[i] != "M" for i in (0, 9, 19, 29, 39, 49, 59)):
        return None
    if any(v is None for v in s):
        return None
    mi = _bcd(s[1:9], (40, 20, 10, 0, 8, 4, 2, 1))
    hh = _bcd(s[12:19], (20, 10, 0, 8, 4, 2, 1))
    doy = _bcd(s[22:34], (200, 100, 0, 80, 40, 20, 10, 0, 8, 4, 2, 1))
    if jjy:
        yy = _bcd(s[41:49], (80, 40, 20, 10, 8, 4, 2, 1))
    else:
        yy = _bcd(s[45:54], (80, 40, 20, 10, 0, 8, 4, 2, 1))
    if mi > 59 or hh > 23 or not 1 <= doy <= 366:
        return None
    d = time.strptime(f"20{yy:02d} {doy}", "%Y %j")
    txt = f"{d.tm_mday:02d}/{d.tm_mon:02d}/20{yy:02d} {hh:02d}:{mi:02d} UTC"
    return txt + ("" if jjy else ("  heure d'été" if s[57] == 1 and s[58] == 1 else ""))


def frame_wwv(s):
    """WWV / WWVH (sous-porteuse 100 Hz, bits poids faible en premier) -> texte ou None."""
    if len(s) < 60 or any(s[i] != "M" for i in (9, 19, 29, 39, 49, 59)):
        return None
    g = lambda i: 1 if s[i] == 1 else 0
    if any(s[i] is None for i in list(range(4, 9)) + list(range(10, 19)) + list(range(20, 27)) + list(range(30, 42)) + list(range(51, 55))):
        return None
    yy = sum(g(i) << k for k, i in enumerate((4, 5, 6, 7))) + 10 * sum(g(i) << k for k, i in enumerate((51, 52, 53, 54)))
    mi = sum(g(i) << k for k, i in enumerate((10, 11, 12, 13))) + 10 * sum(g(i) << k for k, i in enumerate((15, 16, 17)))
    hh = sum(g(i) << k for k, i in enumerate((20, 21, 22, 23))) + 10 * sum(g(i) << k for k, i in enumerate((25, 26)))
    doy = (sum(g(i) << k for k, i in enumerate((30, 31, 32, 33))) + 10 * sum(g(i) << k for k, i in enumerate((35, 36, 37, 38)))
           + 100 * sum(g(i) << k for k, i in enumerate((40, 41))))
    if mi > 59 or hh > 23 or not 1 <= doy <= 366:
        return None
    d = time.strptime(f"20{yy:02d} {doy}", "%Y %j")
    return f"{d.tm_mday:02d}/{d.tm_mon:02d}/20{yy:02d} {hh:02d}:{mi:02d} UTC"


# ----------------------------------------------------------------------------------------------- décodeur
STATIONS = {
    # nature du signal « actif » en début de seconde, et classement des durées (s)
    "dcf77": {"label": "DCF77", "sig": "low", "classes": [(0.06, 0.15, 0), (0.15, 0.26, 1)]},
    "msf": {"label": "MSF", "sig": "low"},
    # TDF : un élément (rampes +1, -2, +1 rad en 100 ms) = 0, deux éléments = 1, rien à la seconde 59
    "tdf": {"label": "TDF", "sig": "pm", "classes": [(0.06, 0.16, 0), (0.16, 0.3, 1)]},
    "wwvb": {"label": "WWVB", "sig": "low", "classes": [(0.12, 0.35, 0), (0.35, 0.65, 1), (0.65, 0.95, "M")]},
    "jjy": {"label": "JJY", "sig": "high", "classes": [(0.65, 0.95, 0), (0.35, 0.65, 1), (0.12, 0.35, "M")]},
    "wwv": {"label": "WWV/WWVH", "sig": "sub100", "classes": [(0.12, 0.35, 0), (0.35, 0.65, 1), (0.65, 0.95, "M")]},
}


class TimeCode(Decoder):
    name = "Signal horaire"
    kind = "msg"

    def __init__(self, fs, af=1000.0, station="dcf77"):
        super().__init__(fs, af)
        self.st = station
        self.cfg = STATIONS[station]
        self.D = int(self.fs // FR)
        self.mix = Mixer(af, self.fs)
        sig = self.cfg["sig"]
        self.lp = firwin(int(self.fs / 25) | 1, 160.0 if sig == "sub100" else 60.0, fs=self.fs)
        self.zi = np.zeros(len(self.lp) - 1, complex)
        self.finder = ToneFinder(self.fs, seconds=2.0, period=2.0)
        self.af0 = float(af)
        self.n = 0                              # échantillons audio reçus (pour la décimation)
        self.env = []                           # « niveau actif » à 200 Hz, 4 dernières secondes
        self.t = 0                              # indice du prochain point à 200 Hz
        self.lo = self.hi = None
        self.on = False
        self.run_start = None
        self.pend = None                        # impulsion terminée, en attente (un trou court la prolonge)
        self.ph_ref = 0j
        self.dc = 0.0
        self.D1 = max(1, int(self.fs // 1200))          # sous-porteuse : enveloppe AM à ~1200 Hz
        self.fs1 = self.fs / self.D1
        self.am_lp = firwin(int(self.fs / 100) | 1, 400.0, fs=self.fs)
        self.am_zi = np.zeros(len(self.am_lp) - 1)
        self.sub_lp = firwin(int(self.fs1 / 5) | 1, 15.0, fs=self.fs1)
        self.sub_zi = np.zeros(len(self.sub_lp) - 1, complex)
        self.n1 = 0
        self.D2 = int(round(self.fs1 / FR))
        self.prev_z = None
        self.fmean = 0.0
        self.phi = 0.0
        self.pmean = 0.0
        self.pbuf = []
        self.group = None                       # impulsions de la seconde en cours : [début, [(décalage, durée)]]
        self.prev_t0 = None
        self.prev_sym = None
        self.sec = None
        self.syms = {}
        self.count = 0
        self.carrier = None                     # dernière porteuse trouvée (Hz audio), pour l'état affiché
        # lecture « à l'horloge »
        self.clocked = station in ("dcf77", "tdf", "msf", "wwvb", "jjy")
        self.ebuf = np.zeros(0)
        self.ebuf0 = 0                          # indice absolu (à 200 Hz) de ebuf[0]
        self.etot = 0
        self.P = None                           # durée de la seconde (points à 200 Hz)
        self.nxt = None                         # début (absolu, fractionnaire) de la prochaine seconde à lire
        self.est_at = 0
        self.far_d = None
        self.actq, self.idleq = [], []

    def set_af(self, af):
        super().set_af(af)
        self.af0 = float(af)
        self.mix.freq = float(af)

    def status(self):
        st = {"af": round(self.mix.freq, 1), "last": self.count, "sync": self.sec is not None}
        if self.sec is not None and self.sec >= 0:
            st["info"] = f"seconde {self.sec}"
        elif self.carrier is None:
            st["info"] = f"porteuse introuvable près de {self.af0:.0f} Hz audio (cliquez dessus dans le mini-spectre)"
        else:
            st["info"] = "porteuse trouvée, recherche du début de minute"
        return st

    # -------------------------------------------------------------------------------- niveau « actif »
    def _decimate(self, y):
        k0 = (-self.n) % self.D
        self.n += len(y)
        return y[k0::self.D]

    def _active(self, z):
        sig = self.cfg["sig"]
        if sig == "sub100":                      # audio AM, raie à 100 Hz, enveloppe
            a, self.am_zi = lfilter(self.am_lp, 1.0, np.abs(z), zi=self.am_zi)
            k0 = (-self.n) % self.D1
            self.n += len(a)
            a = a[k0::self.D1]                   # ~1200 Hz
            if len(a):
                self.dc += 0.01 * (np.mean(a) - self.dc)
            t = (self.n1 + np.arange(len(a))) / self.fs1
            self.n1 += len(a)
            y, self.sub_zi = lfilter(self.sub_lp, 1.0, (a - self.dc) * np.exp(-2j * np.pi * 100.0 * t), zi=self.sub_zi)
            k1 = (-(self.n1 - len(a))) % self.D2
            return np.abs(y[k1::self.D2])
        y = self._decimate(z)
        if sig == "low":
            return -np.abs(y)
        if sig == "high":
            return np.abs(y)
        # « pm » : phase déroulée, privée de sa tendance lente (1 s), en valeur absolue lissée sur 50 ms
        out = np.empty(len(y))
        for i, v in enumerate(y):
            if self.prev_z is None:
                self.prev_z = v
            dphi = np.angle(v * np.conj(self.prev_z))
            self.prev_z = v
            self.fmean += 0.005 * (dphi - self.fmean)              # écart de fréquence de la porteuse
            self.phi += dphi - self.fmean
            self.pmean += 0.02 * (self.phi - self.pmean)            # reste de dérive lente
            if self.clocked:                                    # TDF : phase signée, pour le filtre adapté
                out[i] = self.phi - self.pmean
                continue
            self.pbuf.append(abs(self.phi - self.pmean))
            if len(self.pbuf) > 10:
                del self.pbuf[0]
            out[i] = sum(self.pbuf) / len(self.pbuf)
        return out

    def process(self, x):
        x = np.asarray(x, np.float64)
        if self.finder.feed(x):
            fc = self.finder.find(self.af0, 400.0, min_ratio=10.0)    # ±400 Hz : tout le mini-spectre
            self.carrier = fc
            if fc is not None and abs(fc - self.mix.freq) > 0.5:
                self.mix.freq = fc
        z = self.mix.process(x)
        z, self.zi = lfilter(self.lp, 1.0, z, zi=self.zi)
        out = []
        if self.clocked:
            self._clocked(self._active(z), out)
            return out
        for v in self._active(z):
            self._point(v, out)
        return out

    # -------------------------------------------------------------------------------- lecture à l'horloge
    def _clocked(self, a, out):
        self.ebuf = np.concatenate([self.ebuf, a])
        self.etot += len(a)
        if self.etot - self.est_at >= 2 * FRI and len(self.ebuf) >= 12 * FRI:
            self.est_at = self.etot
            self._estimate()
        while self.P is not None and self.nxt + self.P + 2 <= self.etot:
            self._slice(self.nxt, out)
            self.nxt += self.P
        keep = 45 * FRI
        if len(self.ebuf) > keep:
            cut = len(self.ebuf) - keep
            if self.nxt is not None:
                cut = min(cut, int(self.nxt) - self.ebuf0 - 2)
            if cut > 0:
                self.ebuf = self.ebuf[cut:]
                self.ebuf0 += cut

    @staticmethod
    def _fold(seg, P, nb=100):
        bins = np.floor((np.arange(len(seg)) % P) / P * nb).astype(int)
        return np.bincount(bins, seg, nb) / np.maximum(np.bincount(bins, None, nb), 1)

    def _estimate(self):
        """Période et début des secondes, par repli des 40 dernières secondes."""
        seg = self.ebuf[-40 * FRI:]
        k0 = self.etot - len(seg)                                   # indice absolu de seg[0]
        seg = seg - np.median(seg)
        best = None
        for P in np.arange(0.985, 1.015, 0.0005) * FR:
            sc = self._fold(seg, P).std()
            if best is None or sc > best[0]:
                best = (sc, P)
        for P in np.arange(best[1] - 0.0005 * FR, best[1] + 0.0005 * FR, 0.00002 * FRI):
            sc = self._fold(seg, P).std()
            if sc > best[0]:
                best = (sc, P)
        P = best[1]
        if self.st == "tdf":
            # élément de données (toujours présent de 0 à 100 ms, sauf à la seconde 59) : meilleure
            # corrélation du profil moyen avec le triangle, à 5 ms près
            prof = self._fold(seg, P, 200)
            ext = np.concatenate([prof, prof[:20]])
            tri = self._tri(20)
            c = [abs(np.dot(ext[q:q + 20], tri)) for q in range(200)]
            onset = k0 + int(np.argmax(c)) / 200 * P
        else:
            prof = self._fold(seg, P)
            ext = np.concatenate([prof, prof, prof])
            # début de seconde : plus forte montée du niveau « actif » (60 ms après contre 250 ms avant)
            c = [ext[100 + p:106 + p].mean() - ext[75 + p:97 + p].mean() for p in range(100)]
            p = int(np.argmax(c))
            onset = k0 + p / 100 * P
        if self.P is None:
            self.P = P
            self.nxt = onset
            while self.nxt - P >= self.ebuf0:
                self.nxt -= P
        else:
            self.P = P
            d = (onset - self.nxt + P / 2) % P - P / 2
            # un premier calage faux (peu de secondes, bruit) : deux estimations de suite très
            # différentes et concordantes font sauter directement au bon début de seconde
            far = abs(d) > 0.06 * P
            if far and self.far_d is not None and abs(d - self.far_d) < 0.03 * P:
                self.nxt += d
                self.far_d = None
            else:
                self.far_d = d if far else None
                self.nxt += float(np.clip(0.5 * d, -3, 3))

    @staticmethod
    def _tri(n):
        """Élément TDF : phase 0 → +1 rad (25 ms) → −1 rad (75 ms) → 0 (100 ms)."""
        u = (np.arange(n) + 0.5) / n
        t = np.where(u < 0.25, 4 * u, np.where(u < 0.75, 2 - 4 * u, 4 * u - 4))
        return t / np.sqrt(np.sum(t * t))

    def _corr(self, s0, a):
        """|corrélation| de la phase avec l'élément TDF commençant à a (fraction de seconde)."""
        n = max(8, int(round(0.1 * self.P)))
        i0 = int(round(s0 + a * self.P)) - self.ebuf0
        seg = self.ebuf[max(0, i0):max(0, i0) + n]
        if len(seg) < n:
            return 0.0
        return abs(float(np.dot(seg - seg.mean(), self._tri(n))))

    def _m(self, s0, a, b):
        i0 = int(round(s0 + a * self.P)) - self.ebuf0
        i1 = int(round(s0 + b * self.P)) - self.ebuf0
        return float(self.ebuf[max(0, i0):max(i0 + 1, i1)].mean())

    def _slice(self, s0, out):
        st = self.st
        if st == "tdf":
            e1, e2 = self._corr(s0, 0.0), self._corr(s0, 0.1)
            self.actq = (self.actq + [e1])[-30:]
            ref = float(np.percentile(self.actq, 75))           # élément présent (59 secondes sur 60)
            if ref <= 1e-9:
                return
            self._seq("-" if e1 < 0.5 * ref else (1 if e2 > 0.5 * ref else 0), out)
            return
        m = lambda a, b: self._m(s0, a, b)
        idle_win = {"msf": (0.55, 0.95), "wwvb": (0.85, 0.97), "jjy": (0.85, 0.97)}.get(st, (0.4, 0.95))
        self.actq = (self.actq + [m(0.02, 0.08)])[-30:]
        self.idleq = (self.idleq + [m(*idle_win)])[-30:]
        act, idle = float(np.median(self.actq)), float(np.median(self.idleq))
        if act <= idle:
            return
        mid = (act + idle) / 2
        on = lambda a, b: m(a, b) > mid
        if st in ("dcf77", "tdf"):
            sym = "-" if not on(0.02, 0.08) else (1 if on(0.12, 0.18) else 0)
        elif st == "msf":
            sym = "MARK" if on(0.35, 0.45) else (1 if on(0.12, 0.18) else 0, 1 if on(0.22, 0.28) else 0)
        elif st == "wwvb":
            sym = "M" if on(0.57, 0.73) else (1 if on(0.27, 0.43) else 0)
        else:                                                         # JJY : durée de la pleine puissance
            sym = 0 if on(0.57, 0.73) else (1 if on(0.27, 0.43) else "M")
        self._seq(sym, out)

    def _seq(self, sym, out):
        st = self.st
        prev, self.prev_sym = self.prev_sym, sym
        if st in ("dcf77", "tdf") and sym == "-":                    # seconde 59 : la minute suivante commence
            self._finish(out)
            self.sec, self.syms = -1, {}
            return
        if (st == "msf" and sym == "MARK") or (st in ("wwvb", "jjy") and sym == "M" and prev == "M"):
            self._finish(out)
            self.sec, self.syms = 0, {0: sym}
            return
        if self.sec is not None:
            self.sec += 1
            if self.sec > 60:
                self.sec, self.syms = None, {}
                return
            self.syms[self.sec] = sym

    def _point(self, v, out):
        self.env.append(v)
        if len(self.env) > int(4 * FR):
            del self.env[0]
        self.t += 1
        if self.t % int(FR / 4) == 0 and len(self.env) >= FR:
            e = np.array(self.env)
            self.lo, self.hi = np.percentile(e, 10), np.percentile(e, 97)
        if self.lo is None or self.hi - self.lo < 1e-12:
            return
        # hystérésis, et les trous de moins de 30 ms ne coupent pas une impulsion (bruit)
        span = self.hi - self.lo
        on = v > self.lo + (0.4 if self.on else 0.6) * span
        if on and not self.on:
            if self.pend is not None and self.t - self.pend[1] < 0.03 * FR:
                self.run_start = self.pend[0]
            else:
                if self.pend is not None:
                    self._run(self.pend[0] / FR, (self.pend[1] - self.pend[0]) / FR, out)
                self.run_start = self.t
            self.pend = None
        elif not on and self.on and self.run_start is not None:
            self.pend = (self.run_start, self.t)
        elif not on and self.pend is not None and self.t - self.pend[1] >= 0.03 * FR:
            self._run(self.pend[0] / FR, (self.pend[1] - self.pend[0]) / FR, out)
            self.pend = None
        self.on = on

    # -------------------------------------------------------------------------------- secondes
    def _run(self, t0, w, out):
        """Une période « active » : regroupée avec celles de la même seconde (MSF, TDF)."""
        if w < 0.012:
            return
        if self.group is not None and t0 - self.group[0] < 0.45:
            self.group[1].append((t0 - self.group[0], w))
            return
        if self.group is not None:
            self._second(self.group, t0, out)
        self.group = [t0, [(0.0, w)]]

    def _symbol(self, runs):
        st = self.st
        if st == "msf":
            if any(w > 0.4 for _, w in runs):
                return "MARK"
            off = lambda a, b: any(s < b and s + w > a for s, w in runs)
            return (1 if off(0.12, 0.18) else 0, 1 if off(0.22, 0.28) else 0)
        end = max(s + w for s, w in runs)                        # TDF : une ou deux périodes
        for a, b, sym in self.cfg["classes"]:
            if a <= end < b:
                return sym
        return None

    def _second(self, group, next_t0, out):
        t0, runs = group
        sym = self._symbol(runs)
        gap = None if self.prev_t0 is None else t0 - self.prev_t0
        self.prev_t0 = t0
        n = int(round(gap)) if gap is not None else None
        if n is not None and (n < 1 or abs(gap - n) > 0.1):
            n = None
        st = self.st
        start = False
        if st in ("dcf77", "tdf"):
            start = n == 2                                           # seconde 59 sans impulsion
        elif st == "msf":
            start = sym == "MARK"
        elif st in ("wwvb", "jjy"):
            start = sym == "M" and self.prev_sym == "M" and n == 1   # repères 59 puis 0
        elif st == "wwv":
            start = n == 2 and self.prev_sym == "M"                  # P0 en 59, seconde 0 sans impulsion
        self.prev_sym = sym
        if start:
            self._finish(out)
            self.sec = 1 if st == "wwv" else 0
            self.syms = {}
        elif self.sec is not None:
            if n is None:
                self.sec, self.syms = None, {}
                return
            self.sec += n
            if self.sec > 60:
                self.sec, self.syms = None, {}
                return
        if self.sec is not None:
            self.syms[self.sec] = sym

    def _finish(self, out):
        if self.sec is None or self.sec < 58:
            return
        st, s = self.st, self.syms
        if st in ("dcf77", "tdf"):
            txt = frame_dcf77([s.get(i) for i in range(59)], tdf=st == "tdf")
        elif st == "msf":
            ab = [s.get(i) if isinstance(s.get(i), tuple) else (None, None) for i in range(60)]
            txt = frame_msf([v[0] for v in ab], [v[1] for v in ab])
        elif st == "wwv":
            v = [s.get(i) for i in range(60)]
            v[59] = "M"
            txt = frame_wwv(v)
        else:
            v = [s.get(i) for i in range(60)]
            v[0] = v[59] = "M"
            txt = frame_wwvb(v, jjy=st == "jjy")
        if txt:
            self.count += 1
            out.append({"t": "msg", "utc": time.strftime("%H%M%S", time.gmtime()), "text": f"{self.cfg['label']} : {txt}"})


# ----------------------------------------------------------------------------------------------- CHU
class CHU(Decoder):
    """CHU (Canada, 3330 / 7850 / 14670 kHz) : aux secondes 31 à 39, 10 octets en FSK 300 bauds
    (Bell 103 : 2025 / 2225 Hz depuis la porteuse), 8 bits, 2 bits d'arrêt. Seconde 31 : format B
    (année, DUT1, TAI-UTC) ; 32 à 39 : format A (jour, heure, minute, seconde)."""
    name = "CHU"
    kind = "msg"

    def __init__(self, fs, af=2125.0):
        super().__init__(fs, af)
        from .fsk import RTTY
        self.rx = RTTY(fs, af, baud=300.0, shift=200.0, bits=8, squelch=0.0)
        self.rx._ascii = self._byte
        self.t = 0.0
        self.bytes = []                       # (instant, octet)
        self.year = None
        self.count = 0
        self.last = None

    def set_af(self, af):
        super().set_af(af)
        self.rx.set_af(af)

    def status(self):
        return {**self.rx.status(), "last": self.count}

    def _byte(self, code):
        self.bytes.append((self.t, code & 0xFF))
        return ""

    def process(self, x):
        self.rx.process(x)
        self.t += len(x) / self.fs
        out = []
        # une trame = octets rapprochés, suivis d'un silence de plus de 0,2 s
        if self.bytes and self.t - self.bytes[-1][0] > 0.25:
            frame = [b for _, b in self.bytes]
            self.bytes = []
            if len(frame) >= 10:
                self._frame(frame[-10:], out)
        return out

    @staticmethod
    def _digits(b):
        d = []
        for v in b:
            d += [v & 15, v >> 4]
        return d

    def _frame(self, f, out):
        a, b = f[:5], f[5:]
        if a == b:                                              # format A
            d = self._digits(a)
            if d[0] != 6 or any(v > 9 for v in d[1:]):
                return
            doy = d[1] * 100 + d[2] * 10 + d[3]
            hh, mm, ss = d[4] * 10 + d[5], d[6] * 10 + d[7], d[8] * 10 + d[9]
            if hh > 23 or mm > 59 or ss > 60:
                return
            key = (doy, hh, mm)
            if key == self.last:
                return                                          # une ligne par minute
            self.last = key
            y = f" {self.year}," if self.year else ""
            self.count += 1
            out.append({"t": "msg", "utc": time.strftime("%H%M%S", time.gmtime()),
                        "text": f"CHU :{y} jour {doy}, {hh:02d}:{mm:02d}:{ss:02d} UTC"})
        elif all((x ^ y) == 0xFF for x, y in zip(a, b)):           # format B : 2e moitié inversée
            d = self._digits(a)
            if any(v > 9 for v in d[2:6]):
                return
            self.year = d[2] * 1000 + d[3] * 100 + d[4] * 10 + d[5]
