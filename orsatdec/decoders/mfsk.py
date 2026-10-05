"""Décodeur MFSK (MFSK4 à MFSK128, compatible fldigi) : tonalités codées en Gray, entrelaceur diagonal,
code convolutif K=7 r=1/2 décodé par Viterbi à décisions souples, Varicode IZ8BLY.

Les images MFSK (en-tête « Pic:LxH... ») ne sont pas affichées : le décodeur signale l'image et se tait
pendant sa durée au lieu d'imprimer des caractères parasites.
"""
import re

import numpy as np

from ..dsp import Decoder
from .ifk_common import (MFSK_MODES, ToneDemod, Viterbi, Interleaver, VariShreg, TextOut, printable,
                         gray_rx, soft_bits)
from .ifk_tables import MFSK_VARIDEC

PIC_RE = re.compile(r"Pic:(\d+)x(\d+)(C?)(?:;|p([248]);)")


class _Branch:
    """Un décodeur de Viterbi + son registre Varicode (deux branches si le nombre de bits par symbole est impair)."""

    def __init__(self, table):
        self.vit = Viterbi(7, 0x6d, 0x4f, traceback=45, chunk=4)
        self.var = VariShreg(table)


class MFSK(Decoder):
    name = "MFSK"

    def __init__(self, fs, af=1500.0, mode="MFSK16", reverse=False, squelch=0.915):
        super().__init__(fs, af)
        sr, symlen, symbits, depth, ntones, _ = MFSK_MODES[mode]
        self.mode, self.reverse, self.squelch = mode, bool(reverse), float(squelch)
        self.sr = sr
        self.nb, self.M = symbits, ntones
        self.dem = ToneDemod(fs, af, sr / symlen, ntones, 1, low_tone=True)
        data = np.array([gray_rx(t) for t in range(ntones)])
        self.bits = np.array([(data >> (symbits - 1 - k)) & 1 for k in range(symbits)], bool)
        self.depth = depth
        self._reset_fec()
        self.out = TextOut()
        self.header = ""
        self.mute = 0          # échantillons audio à ignorer (image en cours)

    def _reset_fec(self):
        self.inlv = Interleaver(self.nb, self.depth, rx=True)
        self.br = [_Branch(MFSK_VARIDEC) for _ in range(1 if self.nb % 2 == 0 else 2)]
        v = self.br[0].vit
        self.inlv.warm = 8 + (v.tb + v.chunk + v.W) * 2 // self.nb
        self.prev_llr = 0.0
        self.nbits = 0
        self.best = 0

    def set_af(self, af):
        super().set_af(af)
        self.dem.set_af(af)

    def quality(self):
        return max(b.vit.quality for b in self.br) if self.inlv.filled else 0.0

    def status(self):
        return {"af": round(self.dem.centre(), 1), "snr": round(self.dem.snr_db(), 1),
                "quality": round(max(0.0, self.quality()), 2), "sync": int(self.quality() > self.squelch),
                "mode": self.mode}

    def _symbol(self, T):
        if self.reverse:
            T = T[::-1]
        lt = self.dem.tone_llr(T)
        llr = soft_bits(lt, self.bits)
        llr = self.inlv.symbols(llr)
        text = []
        for v in llr:
            if self.nbits % 2 == 1:
                text += self._pair(0, self.prev_llr, v)
            elif self.nbits > 0 and len(self.br) == 2:
                text += self._pair(1, self.prev_llr, v)
            self.prev_llr = v
            self.nbits += 1
        return text

    def _pair(self, k, l0, l1):
        b = self.br[k]
        out = []
        if len(self.br) == 2:     # garder la branche la mieux alignée (hystérésis)
            o = self.br[1 - self.best].vit.quality
            if o > self.br[self.best].vit.quality + 0.1:
                self.best = 1 - self.best
        is_open = b.vit.quality > self.squelch and self.inlv.filled
        for bit in b.vit.step(l0, l1):
            c = b.var.bit(bit)
            if c is None or k != self.best:
                continue
            ch = printable(c)
            self._header(c)
            s = self.out.put(ch, is_open)
            if s:
                out.append(s)
        return out

    def _header(self, c):
        if c < 32 or c > 122:
            return
        self.header = (self.header + chr(c))[-64:]
        m = PIC_RE.search(self.header)
        if m:
            w, h, col, spp = int(m.group(1)), int(m.group(2)), m.group(3) == "C", int(m.group(4) or 8)
            if 0 < w <= 4095 and 0 < h <= 4095:
                n = spp * w * h * (3 if col else 1)
                self.mute = int((n + 352) / 8000 * self.fs + 0.5 * self.fs)
                self.pic = f"\n[image MFSK {w}x{h}{' couleur' if col else ''} non affichée]\n"
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
