"""Démodulateur FSK commun (RTTY, SITOR-B) et décodeur RTTY Baudot.

Deux filtres (mark et space) en bande de base, détection d'enveloppe et ATC (correction automatique
de seuil) : chaque tonalité est normalisée par sa propre crête, ce qui encaisse les évanouissements
sélectifs fréquents en ondes courtes.
"""
import numpy as np

from ..dsp import Decoder, Mixer, FirDecim, lowpass, ToneFinder
from ..tables import BAUDOT_LTRS, BAUDOT_FIGS, BAUDOT_LTRS_SHIFT, BAUDOT_FIGS_SHIFT


class FSKDemod:
    """Produit le discriminateur d(t) ∈ [-1, 1] (positif = mark) à fs2 ≈ 16 échantillons par bit."""

    def __init__(self, fs, af, baud, shift, reverse=False, bw_factor=0.6):
        self.fs, self.baud, self.shift, self.reverse = float(fs), float(baud), float(shift), reverse
        self.D = max(1, int(self.fs // (self.baud * 16)))
        self.fs2 = self.fs / self.D
        cut = self.baud * bw_factor + 5
        taps = lowpass(self.fs, cut, 3 * self.fs / self.baud)
        self.mark_mix = Mixer(af + shift / 2, self.fs)
        self.space_mix = Mixer(af - shift / 2, self.fs)
        self.fm = FirDecim(taps, self.D)
        self.fsp = FirDecim(taps, self.D)
        self.mpk = self.spk = 1e-6
        self.level = 0.0
        self.noise = 1e-9
        self.af0 = self.af = float(af)
        self.finder = ToneFinder(self.fs, seconds=1.5, period=1.0)

    def set_af(self, af):
        self.af0 = float(af)
        self._tune(af)

    def _tune(self, af):
        self.af = float(af)
        self.mark_mix.freq = af + self.shift / 2
        self.space_mix.freq = af - self.shift / 2

    def process(self, x):
        if self.finder.feed(x):
            fc = self.finder.find(self.af0, 120.0, shift=self.shift, min_ratio=6.0)
            if fc is not None and abs(fc - self.af) > 6:
                self._tune(fc)
        m = np.abs(self.fm.process(self.mark_mix.process(x)))
        s = np.abs(self.fsp.process(self.space_mix.process(x)))
        out = np.empty(len(m))
        a = 1.0 / (self.fs2 / self.baud * 16)        # constante d'ATC ≈ 16 bits
        for i in range(len(m)):
            mi, si = m[i], s[i]
            self.mpk = max(mi, self.mpk * (1 - a))
            self.spk = max(si, self.spk * (1 - a))
            mn, sn = mi / (self.mpk + 1e-12), si / (self.spk + 1e-12)
            out[i] = np.clip(mn - sn, -1, 1)
            e = max(mi, si) ** 2
            self.level += 0.002 * (e - self.level)
            self.noise += 0.002 * (min(mi, si) ** 2 - self.noise)
        if self.reverse:
            out = -out
        return out

    def snr(self):
        return 10 * np.log10(max(self.level, 1e-12) / max(self.noise, 1e-12))


class RTTY(Decoder):
    name = "RTTY"

    def __init__(self, fs, af=1000.0, baud=45.45, shift=170.0, reverse=False, stop_bits=1.5, squelch=0.0, bw_factor=0.6):
        super().__init__(fs, af)
        self.baud, self.shift = float(baud), float(shift)
        self.demod = FSKDemod(fs, af, baud, shift, reverse, bw_factor)
        self.spb = self.demod.fs2 / self.baud          # échantillons par bit (fractionnaire)
        self.state = "idle"
        self.t = 0.0          # temps (en échantillons) depuis le front du bit de départ
        self.bits = []
        self.prev = 1.0
        self.figs = False
        self.squelch = squelch
        self.good = 0.5       # proportion de caractères bien encadrés (stop correct)
        self.idle_mark = 0

    def set_af(self, af):
        super().set_af(af)
        self.demod.set_af(af)

    def status(self):
        return {"af": round(self.demod.af, 1), "snr": round(self.demod.snr(), 1), "quality": round(self.good, 2)}

    def process(self, x):
        d = self.demod.process(np.asarray(x, np.float64))
        out = []
        spb = self.spb
        w = 0.35 * spb                                     # demi-largeur de la fenêtre d'intégration
        for v in d:
            if self.state == "idle":
                if self.prev > 0 >= v:                    # front mark -> space : début possible
                    self.state, self.t, self.bits, self.acc, self.n = "start", 0.0, [], 0.0, 0
            else:
                self.t += 1
                k = len(self.bits)                        # 0 = départ, 1..5 = données, 6 = stop
                center = (k + 0.5) * spb
                if self.t >= center - w:
                    self.acc += v
                    self.n += 1
                if self.t >= center + w:
                    m = self.acc / max(1, self.n)
                    self.acc, self.n = 0.0, 0
                    if k == 0:
                        if m > 0:
                            self.state = "idle"
                        else:
                            self.bits.append(0)
                    elif k <= 5:
                        self.bits.append(1 if m > 0 else 0)
                    else:
                        ok = m > 0
                        self.good += 0.05 * ((1.0 if ok else 0.0) - self.good)
                        code = sum(b << i for i, b in enumerate(self.bits[1:6]))
                        ch = self._char(code)
                        if ch and self.good >= self.squelch:
                            out.append(ch)
                        self.state = "idle"
            self.prev = v
        return [{"t": "text", "text": "".join(out)}] if out else []

    def _char(self, code):
        if code == BAUDOT_LTRS_SHIFT:
            self.figs = False
            return ""
        if code == BAUDOT_FIGS_SHIFT:
            self.figs = True
            return ""
        ch = (BAUDOT_FIGS if self.figs else BAUDOT_LTRS)[code]
        if ch == " ":
            self.figs = False                             # « unshift on space », usage courant
        if ch in "\x00\x07":
            return ""
        return ch
