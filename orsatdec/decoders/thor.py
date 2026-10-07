"""Décodeur THOR (THOR 4 à THOR 100) : modulation IFK+ à 18 tonalités, code convolutif
(K=7, ou K=15 pour 25x4, 50x1, 50x2 et 100) avec entrelaceur, Varicode MFSK.

La détection est non cohérente et souple : chaque quartet est estimé à partir des énergies des deux
symboles consécutifs, puis converti en rapports de vraisemblance pour le décodeur de Viterbi.
Le jeu de caractères secondaire (texte d'attente) est reconnu mais pas affiché. Les images THOR
(« pic%... ») sont signalées et le décodeur se tait pendant leur durée.
"""
import numpy as np

from ..dsp import Decoder
from .ifk_common import (THOR_MODES, IFK_TONES, K7, K15, ToneDemod, Viterbi, Interleaver, VariShreg, TextOut,
                         IFKDiff, printable)
from .ifk_tables import THOR_VARIDEC

PIC_SIZES = {"A": (59, 74, 3), "T": (59, 74, 3), "t": (59, 74, 1), "S": (160, 120, 3), "s": (160, 120, 1),
             "L": (320, 240, 3), "l": (320, 240, 1), "V": (640, 480, 3), "v": (640, 480, 1), "F": (640, 480, 1),
             "P": (240, 300, 3), "p": (240, 300, 1), "M": (120, 150, 3), "m": (120, 150, 1)}


class THOR(Decoder):
    name = "THOR"

    def __init__(self, fs, af=1500.0, mode="THOR 16", reverse=False, squelch=None):
        super().__init__(fs, af)
        sr, symlen, ds, depth, _, k15 = THOR_MODES[mode]
        self.mode, self.reverse = mode, bool(reverse)
        # seuil de qualité Viterbi (bruit pur : ~0,88 en K=7, ~0,90 en K=15)
        self.squelch = float(squelch if squelch is not None else (0.935 if k15 else 0.925))
        self.sr, self.symlen, self.depth, self.k15 = sr, symlen, depth, k15
        self.dem = ToneDemod(fs, af, sr / symlen, IFK_TONES, ds)
        self.out = TextOut()
        self.header = ""
        self.mute = 0
        self._reset_fec()

    def _reset_fec(self):
        k, p1, p2 = K15 if self.k15 else K7
        self.vit = Viterbi(k, p1, p2, traceback=180 if self.k15 else 45, chunk=8 if self.k15 else 4)
        self.inlv = Interleaver(4, self.depth, rx=True)
        self.inlv.warm = 8 + (self.vit.tb + self.vit.chunk + self.vit.W) // 2
        self.ifk = IFKDiff()
        self.var = VariShreg(THOR_VARIDEC)

    def set_af(self, af):
        super().set_af(af)
        self.dem.set_af(af)

    def quality(self):
        return self.vit.quality if self.inlv.filled else 0.0

    def status(self):
        q = self.quality()
        return {"af": round(self.dem.centre(), 1), "snr": round(self.dem.snr_db(), 1),
                "quality": round(max(0.0, q), 2), "sync": int(q > self.squelch), "mode": self.mode}

    def _symbol(self, T):
        if self.reverse:
            T = T[::-1]
        Lc = self.ifk.metric(self.dem.tone_llr(T))
        if Lc is None:
            return []
        llr = self.inlv.symbols(np.clip(self.ifk.bit_llr(Lc), -30, 30))
        out = []
        for i in (0, 2):
            is_open = self.vit.quality > self.squelch and self.inlv.filled
            for bit in self.vit.step(llr[i], llr[i + 1]):
                c = self.var.bit(bit)
                if c is None or c < 0 or c & 0x100:       # rien, inconnu, ou jeu secondaire
                    continue
                self._header(c)
                s = self.out.put(printable(c), is_open)
                if s:
                    out.append(s)
        return out

    def _header(self, c):
        self.header = (self.header + chr(c & 0xFF))[-5:]
        if self.header.startswith("pic%") and self.header[4] in PIC_SIZES:
            w, h, k = PIC_SIZES[self.header[4]]
            n = 40 * self.symlen + w * h * k * 10
            self.mute = int(n / self.sr * self.fs + 0.5 * self.fs)
            self.pic = f"\n[image THOR {w}x{h} non affichée]\n"
            self.header = ""

    def process(self, x):
        x = np.asarray(x, np.float64)
        out = []
        if self.mute > 0:
            n = min(self.mute, len(x))
            self.mute -= n
            x = x[n:]
            if self.mute == 0:
                self.dem.reset()
                self._reset_fec()
                self.out.drop()
            if len(x) == 0:
                return []
        self.dem.feed(x)
        for T in self.dem.symbols():
            self.dem.lock = self.quality() > self.squelch - 0.04
            out += self._symbol(T)
            if self.mute:
                out.append(self.pic)
                self.dem.buf = self.dem.buf[:0]
                break
        return [{"t": "text", "text": "".join(out)}] if out else []
