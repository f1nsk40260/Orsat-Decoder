"""Décodeur SITOR-A / AMTOR mode A (CCIR 476, mode ARQ), en écoute.

La station qui émet (ISS) envoie des blocs de 3 caractères de 7 bits (210 ms à 100 bauds), puis se tait
240 ms pour entendre l'accusé de réception de l'autre station (IRS) : un cycle dure 450 ms. Un bloc est
réémis tant que l'IRS ne l'a pas bien reçu.

1. Démodulation FSK (100 bauds, shift 170 Hz) : discriminateur à ~16 échantillons par bit ;
2. acquisition : on cherche, sur trois cycles, la position où les trois mots de 7 bits respectent la
   règle « 4 marks, 3 spaces » ; poursuite ensuite cycle par cycle, à quelques échantillons près ;
3. répétitions ARQ : si l'on entend aussi l'IRS, ses signaux de contrôle (CS1 et CS2 en alternance pour
   accuser réception, le même signal deux fois pour demander une répétition) disent si le bloc suivant est
   nouveau ; sinon un bloc identique au précédent est pris pour une répétition. Un bloc abîmé n'est pas
   écrit : sa répétition le remplacera.
"""
import numpy as np

from ..dsp import Decoder
from ..tables import (CCIR_LTRS, CCIR_FIGS, CCIR_LTRS_SHIFT, CCIR_FIGS_SHIFT, CCIR_ALPHA, CCIR_BETA, CCIR_REP,
                      CCIR_CHAR32)
from .fsk import FSKDemod

VALID = np.array([bin(c).count("1") == 4 for c in range(128)])
W7 = 1 << np.arange(7)
IDLE = (CCIR_ALPHA, CCIR_BETA, CCIR_REP, CCIR_CHAR32)


class SitorA(Decoder):
    name = "SITOR-A"

    def __init__(self, fs, af=1000.0, shift=170.0, reverse=False, baud=100.0, cycle=0.45):
        super().__init__(fs, af)
        self.demod = FSKDemod(fs, af, baud, shift, reverse, bw_factor=0.6)
        self.fs2 = self.demod.fs2
        self.spb = self.fs2 / baud
        self.cyc = cycle * self.fs2                  # durée d'un cycle ARQ en échantillons
        self.offs = np.round(np.arange(21) * self.spb).astype(int)
        self.win = (int(round(0.2 * self.spb)), int(round(0.8 * self.spb)))
        self.buf = np.zeros(0)
        self.base = 0                                # indice absolu du premier échantillon de buf
        self.pos = None                              # début (absolu, fractionnaire) du prochain bloc attendu
        self.misses = 0
        self.figs = False
        self.last = None                             # dernier bloc écrit
        self.dirty = False                           # le bloc précédent était abîmé
        self.quality = 0.0
        self.cs = [None, None]                       # signaux de l'IRS entendus après les deux derniers blocs
        self.prev_start = None                       # début (absolu) du bloc précédent

    def set_af(self, af):
        super().set_af(af)
        self.demod.set_af(af)

    def status(self):
        return {"af": round(self.demod.af, 1), "snr": round(self.demod.snr(), 1),
                "quality": round(self.quality, 2), "sync": self.pos is not None}

    # -------------------------------------------------------------- blocs
    def _blocks(self, starts):
        """Mots (entiers 7 bits) et confiance des blocs commençant aux positions `starts` (relatives à buf)."""
        c = np.concatenate([[0.0], np.cumsum(self.buf)])
        idx = starts[:, None] + self.offs[None, :]
        soft = c[idx + self.win[1]] - c[idx + self.win[0]]
        hard = (soft > 0).astype(int).reshape(len(starts), 3, 7)
        words = hard @ W7
        conf = np.abs(soft).sum(axis=1)
        return words, conf

    def _fits(self, start):
        return start >= 0 and start + self.offs[-1] + self.win[1] + 1 < len(self.buf)

    def process(self, x):
        d = self.demod.process(np.asarray(x, np.float64))
        self.buf = np.concatenate([self.buf, d])
        out = []
        while True:
            if self.pos is None:
                if not self._acquire():
                    break
            rel = int(round(self.pos)) - self.base
            if not self._fits(rel + 4):
                break
            cand = np.arange(rel - 4, rel + 5)
            cand = cand[cand >= 0]
            words, conf = self._blocks(cand)
            nvalid = VALID[words].sum(axis=1)
            score = nvalid * 1e6 + conf
            k = int(np.argmax(score))
            best = cand[k] + self.base
            # réponse de l'IRS dans le silence qui a suivi le bloc précédent
            if self.prev_start is not None:
                self.cs = [self.cs[1], self._irs(self.prev_start - self.base, best - self.base, conf[k] / 21)]
            hint = None if None in self.cs else self.cs[0] == self.cs[1]
            self.pos = self.pos + 0.5 * (best - self.pos)
            self._block(words[k], nvalid[k], out, hint)
            self.prev_start = best
            if nvalid[k] < 2:
                self.misses += 1
                if self.misses >= 4:
                    self.pos = self.prev_start = None
                    self.cs = [None, None]
                    self.misses = 0
                    continue
            else:
                self.misses = 0
            self.pos += self.cyc
        # on garde ~4 cycles d'audio
        keep = int(4 * self.cyc + 30 * self.spb)
        if len(self.buf) > keep:
            cut = len(self.buf) - keep
            if self.pos is not None:
                cut = min(cut, int(self.pos) - self.base - int(self.spb))
            if cut > 0:
                self.buf = self.buf[cut:]
                self.base += cut
        txt = "".join(out)
        return [{"t": "text", "text": txt}] if txt else []

    def _acquire(self):
        n3 = int(np.ceil(3 * self.cyc))
        if len(self.buf) < n3 + 21 * self.spb + self.spb:
            return False
        starts = np.arange(0, int(self.cyc))
        tot = np.zeros(len(starts))
        conf = np.zeros(len(starts))
        for k in range(3):
            w, c = self._blocks(starts + int(round(k * self.cyc)))
            tot += VALID[w].sum(axis=1)
            conf += c
        score = tot * 1e6 + conf
        i = int(np.argmax(score))
        if tot[i] >= 8:
            self.pos = float(self.base + starts[i])
            self.last, self.dirty = None, False
            return True
        # rien : on avance d'un cycle
        drop = int(self.cyc)
        self.buf = self.buf[drop:]
        self.base += drop
        return False

    def _irs(self, a, b, ref):
        """Mot de 7 bits valide et net dans le silence entre deux blocs (a, b : débuts relatifs), ou None."""
        lo, hi = a + int(22 * self.spb), b - int(8 * self.spb)
        if lo < 0 or hi <= lo or b + self.win[1] + 1 >= len(self.buf):
            return None
        c = np.concatenate([[0.0], np.cumsum(self.buf)])
        p = np.arange(lo, hi)
        idx = p[:, None] + self.offs[None, :7]
        soft = c[idx + self.win[1]] - c[idx + self.win[0]]
        words = (soft > 0).astype(int) @ W7
        weak = np.abs(soft).min(axis=1)
        ok = VALID[words] & (weak > 0.35 * ref)
        if not np.any(ok):
            return None
        i = int(np.argmax(np.where(ok, np.abs(soft).sum(axis=1), -1)))
        return int(words[i])

    def _block(self, words, nvalid, out, hint=None):
        self.quality += 0.2 * (nvalid / 3 - self.quality)
        words = tuple(int(w) for w in words)
        if nvalid < 3:
            # abîmé : l'ISS le répétera. S'il ressemble au dernier bloc écrit, c'en est déjà une répétition.
            same = self.last is not None and nvalid >= 1 and all(
                w == l for w, l in zip(words, self.last) if VALID[w])
            if not same:
                self.dirty = True
            return
        if hint is True or (hint is None and words == self.last and not self.dirty):
            if not self.dirty:
                return                               # répétition ARQ d'un bloc déjà écrit
        if all(w in IDLE for w in words):
            self.last, self.dirty = words, False     # repos (α/β) ou demande de répétition (RQ)
            return
        self.last, self.dirty = words, False
        for w in words:
            ch = self._char(w)
            if ch:
                out.append(ch)

    def _char(self, code):
        if code in IDLE:
            return ""
        if code == CCIR_LTRS_SHIFT:
            self.figs = False
            return ""
        if code == CCIR_FIGS_SHIFT:
            self.figs = True
            return ""
        ch = (CCIR_FIGS if self.figs else CCIR_LTRS)[code]
        return "" if ch in "_\x07" else ch
