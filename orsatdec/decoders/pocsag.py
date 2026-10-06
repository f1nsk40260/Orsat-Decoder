"""POCSAG (récepteurs d'appel, pagers) : 512, 1200 et 2400 bauds, audio FM.

- Le discriminateur FM donne directement les bits (NRZ). Les serveurs PhantomSDR retirent le continu de
  l'audio (deux moyennes glissantes de 32 échantillons à 12 kHz), ce qui efface presque le 512 bauds :
  quand la source est PhantomSDR, ce filtre connu est défait (décisions sur un inverse régularisé, puis
  reconstruction exacte de la part retirée). Le 1200 et le 2400 bauds passent tels quels.
- Les trois vitesses sont démodulées en parallèle ; mot de synchro 0x7CD215D8 (les deux polarités),
  lots de 8 trames de 2 mots de 32 bits, BCH(31,21) corrigeant jusqu'à 2 bits, parité.
- Adresse (RIC) = 18 bits d'adresse et numéro de trame ; message numérique (BCD) ou alphanumérique
  (ASCII 7 bits) selon la fonction, avec repli sur le plus lisible.
"""
import time

import numpy as np
from scipy.signal import firwin, lfilter

from ..dsp import Decoder

SYNC = 0x7CD215D8
IDLE = 0x7A89C197
BCH_POLY = 0x769            # x^10 + x^9 + x^8 + x^6 + x^5 + x^3 + 1
NUMERIC = "0123456789*U -)("


def bch_syndrome(cw):
    """Reste de la division du mot de 31 bits (sans la parité) par le polynôme générateur."""
    r = cw >> 1
    for i in range(30, 9, -1):
        if r & (1 << i):
            r ^= BCH_POLY << (i - 10)
    return r & 0x3FF


_SINGLE = {bch_syndrome(1 << (i + 1)): i + 1 for i in range(31)}


def bch_correct(cw):
    """Mot de 32 bits -> (mot corrigé, nb d'erreurs) ou (None, -1). Corrige 1 ou 2 bits."""
    s = bch_syndrome(cw)
    if s == 0:
        return cw, 0                              # (la parité seule peut être fausse)
    if s in _SINGLE:
        return cw ^ (1 << _SINGLE[s]), 1
    for i in range(1, 32):
        s2 = s ^ bch_syndrome(1 << i)
        if s2 in _SINGLE and _SINGLE[s2] != i:
            return cw ^ (1 << i) ^ (1 << _SINGLE[s2]), 2
    return None, -1


def phantom_dc_filter(fs, n=32):
    """Réponse impulsionnelle du retrait de continu des serveurs PhantomSDR : x[n-31] - MA32(MA32(x))."""
    n = max(2, int(round(n * fs / 12000)))
    tri = np.convolve(np.ones(n) / n, np.ones(n) / n)
    h = -tri
    h[n - 1] += 1.0
    return h


def inverse_fir(h, ntaps=1024, eps=2e-3):
    """FIR inverse régularisé (moindres carrés dans le domaine fréquentiel), avec retard ntaps/2."""
    nf = 8192
    H = np.fft.rfft(h, nf)
    G = np.conj(H) / (np.abs(H) ** 2 + eps)
    g = np.fft.irfft(G, nf)
    d = len(h) // 2
    g = np.roll(g, ntaps // 2 - d)[:ntaps]
    return g * np.hanning(ntaps)


class DCRestore:
    """Annule le retrait de continu des serveurs PhantomSDR sur un signal NRZ, sans amplifier le bruit.

    y = x(n-L) - T*x (T : double moyenne glissante, connue). Un filtre inverse donne une estimation
    bruitée de x dont on ne garde que le signe (le niveau NRZ) ; on reconstruit exactement la partie
    retirée, T*x^, à partir de ces décisions, et on la rajoute à y. Une seconde passe refait les décisions
    sur ce premier résultat, bien plus propre. Traitement par fenêtre glissante (latence ~0,2 s)."""

    def __init__(self, fs, ntaps=1024, hist=4096, cutoff=2000.0):
        h = phantom_dc_filter(fs)
        self.L = int(np.argmax(h))                      # retard du terme direct
        self.tri = -h
        self.tri[self.L] += 1.0
        self.g = inverse_fir(h, ntaps, 1e-3)
        self.D = ntaps // 2 - self.L                     # retard de l'inverse régularisé
        self.lp = firwin(63, min(cutoff, 0.4 * fs), fs=fs)
        self.hist = hist
        self.buf = np.zeros(0)
        self.base = 0                                    # indice absolu de buf[0]
        self.out_n = 0                                   # prochain indice (de y) à restituer
        self.margin = self.D + 2 * len(self.tri) + 2 * 63

    def _restore(self, y, d):
        """y + T*d, d aligné sur le temps de x (d[t] ~ x[t]) : renvoie r[k] ~ x[k-L]."""
        return y + np.convolve(d, self.tri)[:len(y)]

    def process(self, y):
        from scipy.signal import oaconvolve
        self.buf = np.concatenate([self.buf, np.asarray(y, np.float64)])
        n = len(self.buf)
        end = n - self.margin                            # au-delà, les décisions ne sont pas encore sûres
        start = self.out_n - self.base
        if end <= start:
            return np.zeros(0)
        yb = self.buf
        # 1re passe : décisions sur l'inverse régularisé (retard D)
        z = oaconvolve(yb, self.g)[:n]
        z = np.convolve(z, self.lp)[31:31 + n]
        amp = np.percentile(np.abs(z), 75) + 1e-9
        d1 = np.zeros(n)
        d1[:n - self.D] = amp * np.sign(z[self.D:])
        r1 = self._restore(yb, d1)                       # r1[k] ~ x[k-L]
        # 2e passe : décisions sur r1
        q = np.convolve(r1, self.lp)[31:31 + n]
        amp = np.percentile(np.abs(q), 75) + 1e-9
        d2 = np.zeros(n)
        d2[:n - self.L] = amp * np.sign(q[self.L:])
        r2 = self._restore(yb, d2)
        out = r2[start:end]
        self.out_n = self.base + end
        cut = max(0, end - self.hist)
        if cut:
            self.buf = self.buf[cut:]
            self.base += cut
        return out


class _Rx:
    """Une vitesse : filtre, horloge, tranche, recherche de synchro, lots."""

    def __init__(self, fs, baud):
        self.baud = baud
        self.spb = fs / baud
        self.lp = firwin(int(4 * self.spb) | 1, min(0.45 * fs, 0.8 * baud), fs=fs)
        self.zi = np.zeros(len(self.lp) - 1)
        self.phase = 0.0
        self.prev = 0.0
        self.acc = 0.0
        self.n = 0
        self.lo, self.hi = -1e-3, 1e-3
        self.reg = 0
        self.state = "search"
        self.pol = 0
        self.words = []
        self.nbits = 0
        self.word = 0
        self.misses = 0

    def process(self, x, on_word):
        y, self.zi = lfilter(self.lp, 1.0, x, zi=self.zi)
        spb = self.spb
        for v in y:
            # seuil adaptatif : milieu entre les niveaux haut et bas récents (dérive du continu)
            th = 0.5 * (self.lo + self.hi)
            s = v - th
            if (self.prev > 0) != (s > 0):
                err = self.phase if self.phase < spb / 2 else self.phase - spb
                self.phase -= 0.1 * err
            self.prev = s
            if 0.3 * spb <= self.phase <= 0.7 * spb:
                self.acc += v
                self.n += 1
            self.phase += 1
            if self.phase >= spb:
                self.phase -= spb
                m = self.acc / max(1, self.n)
                self.acc, self.n = 0.0, 0
                a = 4.0 / 32                      # niveaux suivis sur ~8 bits
                if m > th:
                    self.hi += a * (m - self.hi)
                else:
                    self.lo += a * (m - self.lo)
                self._bit(1 if m > th else 0, on_word)

    def _bit(self, b, on_word):
        self.reg = ((self.reg << 1) | b) & 0xFFFFFFFF
        if self.state == "search":
            for pol in (0, 1):
                w = self.reg ^ (0xFFFFFFFF if pol else 0)
                if bin(w ^ SYNC).count("1") <= 2:
                    self.state, self.pol, self.nbits, self.word, self.words = "batch", pol, 0, 0, []
                    self.misses = 0
                    on_word(None)                 # début de lot
                    return
            return
        self.word = ((self.word << 1) | (b ^ self.pol)) & 0xFFFFFFFF
        self.nbits += 1
        if self.nbits % 32:
            return
        k = self.nbits // 32
        if k <= 16:
            on_word((k - 1, self.word))
            return
        # 17e mot : la synchro du lot suivant
        if bin(self.word ^ SYNC).count("1") <= 3:
            self.nbits, self.misses = 0, 0
            on_word(None)
        else:
            self.misses += 1
            self.state = "search"
            on_word("end")


class POCSAG(Decoder):
    name = "POCSAG"
    kind = "msg"

    def __init__(self, fs, af=0.0, rates=(512, 1200, 2400), compensate=True, ntaps=1024):
        super().__init__(fs, af)
        self.rx = [_Rx(self.fs, r) for r in rates]
        # restitution du continu (source PhantomSDR) : indispensable à 512 bauds ; à 1200 et 2400 bauds le
        # filtre du serveur gêne peu et la restitution coûterait plus qu'elle ne rapporte dans le bruit
        self.restore = [DCRestore(self.fs, ntaps, cutoff=0.7 * r) if compensate and r < 1000 else None
                        for r in rates]
        self.cur = {r: None for r in rates}       # message en cours par vitesse : [ric, fonction, bits]
        self.out = []
        self.count = 0

    def status(self):
        return {"last": self.count}

    def process(self, x):
        x = np.asarray(x, np.float64)
        self.out = []
        for i, r in enumerate(self.rx):
            xi = self.restore[i].process(x) if self.restore[i] is not None else x
            if len(xi):
                r.process(xi, lambda w, r=r: self._word(r.baud, w))
        return self.out

    # ------------------------------------------------------------------ mots et messages
    def _word(self, baud, w):
        if w is None:
            return
        if w == "end":
            self._flush(baud)
            return
        idx, cw = w
        frame = idx // 2
        raw = cw
        cw, nerr = bch_correct(cw)
        m = self.cur[baud]
        if cw is None:
            if m is not None:
                m[4] = True                       # mot perdu : grave seulement si le message continue
            return
        if cw >> 1 == IDLE >> 1 or bin((raw ^ IDLE) >> 1).count("1") <= 4:
            self._flush(baud)
            return
        if cw & 0x80000000 == 0:                  # adresse
            self._flush(baud)
            ric = (((cw >> 13) & 0x3FFFF) << 3) | frame
            func = (cw >> 11) & 3
            self.cur[baud] = [ric, func, [], False, False, nerr]
        elif m is not None:
            if m[4]:
                m[3] = True                       # un mot manque au milieu du message
                m[4] = False
            data = (cw >> 11) & 0xFFFFF
            m[2] += [(data >> (19 - i)) & 1 for i in range(20)]

    def _flush(self, baud):
        m = self.cur[baud]
        self.cur[baud] = None
        if m is None:
            return
        ric, func, bits, broken, _, nerr = m
        if not bits and nerr >= 2:
            return                                # adresse seule et douteuse : probablement du bruit
        num = self._numeric(bits)
        alpha = self._alpha(bits)
        if not bits:
            txt = "(tonalité seule)"
        elif func == 0 and num.strip():
            txt = num
        elif func == 3 or self._readable(alpha) > self._readable(num):
            txt = alpha
        else:
            txt = num
        self.count += 1
        self.out.append({"t": "msg", "utc": time.strftime("%H%M%S", time.gmtime()),
                         "text": f"POCSAG{baud} {ric:07d} f{func} : {txt}" + (" [incomplet]" if broken else "")})

    @staticmethod
    def _numeric(bits):
        s = []
        for i in range(0, len(bits) - 3, 4):
            v = bits[i] | bits[i + 1] << 1 | bits[i + 2] << 2 | bits[i + 3] << 3
            s.append(NUMERIC[v])
        return "".join(s).rstrip()

    @staticmethod
    def _alpha(bits):
        s = []
        for i in range(0, len(bits) - 6, 7):
            v = sum(bits[i + j] << j for j in range(7))
            if v == 0 or v == 4:                 # bourrage / EOT
                continue
            s.append(chr(v) if 32 <= v < 127 else ("\n" if v in (10, 13) else f"<{v:02x}>"))
        return "".join(s).rstrip()

    @staticmethod
    def _readable(t):
        if not t:
            return 0.0
        return sum(c.isalnum() or c in " .,:-/" for c in t) / len(t) * min(1.0, len(t) / 4)
