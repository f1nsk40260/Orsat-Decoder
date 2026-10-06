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
        self.span = 150.0
        self.finder = ToneFinder(self.fs, seconds=1.5, period=1.0)

    def set_af(self, af):
        self.af0 = float(af)
        self.span = 30.0
        self._tune(af)

    def _tune(self, af):
        self.af = float(af)
        self.mark_mix.freq = af + self.shift / 2
        self.space_mix.freq = af - self.shift / 2

    def process(self, x):
        if self.finder.feed(x):
            fc = self.finder.find(self.af0, self.span, shift=self.shift, min_ratio=6.0)
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

    def __init__(self, fs, af=1000.0, baud=45.45, shift=170.0, reverse=False, stop_bits=1.5, squelch=0.65, bw_factor=0.6,
                 bits=5):
        super().__init__(fs, af)
        self.nbits = int(bits)        # 5 : Baudot ITA2 ; 7 ou 8 : ASCII (bit de poids faible en premier)
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
        self.held = []        # caractères reçus pendant que le silencieux est fermé
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
                k = len(self.bits)                        # 0 = départ, 1..n = données, n+1 = stop
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
                    elif k <= self.nbits:
                        self.bits.append(1 if m > 0 else 0)
                    else:
                        ok = m > 0
                        self.good += 0.05 * ((1.0 if ok else 0.0) - self.good)
                        code = sum(b << i for i, b in enumerate(self.bits[1:1 + self.nbits]))
                        ch = self._char(code) if self.nbits == 5 else self._ascii(code)
                        # silencieux à mémoire : sur du bruit, la moitié seulement des caractères ont un
                        # bit d'arrêt correct ; on retient les derniers et on les rend dès que c'est un signal
                        if self.good >= self.squelch:
                            out.extend(self.held)
                            self.held.clear()
                            if ch:
                                out.append(ch)
                        elif ch:
                            self.held = (self.held + [ch])[-10:]
                        self.state = "idle"
            self.prev = v
        return [{"t": "text", "text": "".join(out)}] if out else []

    @staticmethod
    def _ascii(code):
        c = code & 0x7F
        if c in (10, 13) or 32 <= c < 127:
            return chr(c)
        return ""

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
