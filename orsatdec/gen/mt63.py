"""Générateur MT63 (mire de test), copie fidèle de l'émetteur de fldigi (MT63tx, mt63base.cxx).

64 porteuses BPSK différentielles, symboles en forme de fenêtre (IFFT 512 points + recouvrement),
porteuses impaires décalées d'un demi-symbole, FEC de Walsh sur 7 bits et entrelacement court/long.
Le signal est d'abord produit exactement comme fldigi à 8000 Hz (bande de base complexe à
8000/D Hz, puis filtre d'interpolation « QuadrComb » Blackman3), puis rééchantillonné à fs.
"""
from fractions import Fraction

import numpy as np
from scipy.signal import resample_poly

from ..decoders.mt63 import (SYMBOL_LEN, SYMBOL_SEPAR, CARR_SEPAR, NCARR, MODES_BW, symbol_shape,
                             first_carrier, interleave_offsets, walsh_codeword, quadr_shapes)


class MT63Tx:
    """Émetteur MT63 symbole par symbole (équivalent de MT63tx de fldigi).

    prefill : remplissage initial de l'entrelaceur (fldigi : bits aléatoires) ; None = zéros.
    """

    def __init__(self, bw=1000, long_intlv=True, af=None, prefill="random", seed=1):
        bw = int(bw)
        if bw not in MODES_BW:
            raise ValueError("bande MT63 : 500, 1000 ou 2000 Hz")
        self.bw = bw
        self.D, self.alias_len = MODES_BW[bw]
        self.af = float(af if af is not None else 500 + bw / 2)
        self.first = first_carrier(self.af, bw)
        N = SYMBOL_LEN
        self.mask = N - 1
        self.amp = 4.0 / NCARR
        self.intlv = 64 if long_intlv else 32
        self.pipe_patt = interleave_offsets(long_intlv)          # p_i (en symboles)
        rng = np.random.default_rng(seed)
        if prefill == "random":
            self.pipe = rng.integers(0, 2, (self.intlv, NCARR)).astype(np.int8)
        else:
            self.pipe = np.zeros((self.intlv, NCARR), np.int8)
        self.ptr = 0
        # Phase initiale des porteuses (indices dans la table des facteurs de rotation)
        tx = np.zeros(NCARR, np.int64)
        step, p = 0, 0
        for i in range(NCARR):
            tx[i] = p
            step += 1
            p = (p + step) & self.mask
        self.txvect = tx
        incr = (SYMBOL_SEPAR * CARR_SEPAR) & self.mask
        p0 = (SYMBOL_SEPAR * self.first) & self.mask
        self.phcorr = (p0 + incr * np.arange(NCARR)) & self.mask
        self.bins = (self.first + CARR_SEPAR * np.arange(NCARR)) & self.mask
        self.window = symbol_shape()
        self.ovl = np.zeros(N, np.complex128)                       # tampon de recouvrement
        # Filtre d'interpolation complexe -> réel (fldigi : dspQuadrComb, fenêtre Blackman3)
        self.shape_i, self.shape_q = quadr_shapes(self.af, bw, self.alias_len)
        self.comb_tail = np.zeros(self.alias_len)

    # -- codeur + entrelaceur (MT63encoder) --
    def encode(self, code):
        bits = walsh_codeword(code & 127)                 # 1 = bit « < 0 » de la transformée
        L = self.intlv
        self.pipe[self.ptr] = bits
        rows = (self.ptr + self.pipe_patt) % L
        out = self.pipe[rows, np.arange(NCARR)]
        self.ptr = (self.ptr + 1) % L
        return out

    def _symbol(self):
        """ProcessTxVect : porteuses paires dans un premier bloc, impaires dans un second (½ symbole après)."""
        N = SYMBOL_LEN
        out = []
        for par in (0, 1):
            X = np.zeros(N, np.complex128)
            idx = np.arange(par, NCARR, 2)
            X[self.bins[idx]] = self.amp * np.exp(2j * np.pi * self.txvect[idx] / N)
            x = np.fft.ifft(X) * N                     # IDFT non normalisée
            out.append(self._ovl(x))
        return np.concatenate(out)

    def _ovl(self, x):
        d = SYMBOL_SEPAR // 2
        w = x * self.window
        b = self.ovl
        out = b[:d] + w[:d]
        nb = np.zeros_like(b)
        nb[:SYMBOL_LEN - d] = b[d:] + w[d:]
        self.ovl = nb
        return out

    def _comb(self, z):
        """dspQuadrComb::Process : interpolation ×D et passage complexe -> réel."""
        D, L = self.D, self.alias_len
        up_i = np.zeros(len(z) * D)
        up_q = np.zeros(len(z) * D)
        up_i[::D] = z.real
        up_q[::D] = z.imag
        y = np.convolve(up_i, self.shape_i) + np.convolve(up_q, self.shape_q)
        y[:L - 1] += self.comb_tail[:L - 1]
        n = len(z) * D
        tail = np.zeros(L)
        tail[:len(y) - n] = y[n:]
        self.comb_tail = tail
        return y[:n]

    def send_char(self, code):
        bits = self.encode(code)
        # bit 1 : correction de phase seule ; bit 0 : retournement (+N/2)
        self.txvect = (self.txvect + self.phcorr + np.where(bits == 1, 0, SYMBOL_LEN // 2)) & self.mask
        return self._comb(self._symbol())

    def send_jam(self, rng):
        turn = np.where(rng.integers(0, 2, NCARR) == 1, SYMBOL_LEN // 4, 3 * SYMBOL_LEN // 4)
        self.txvect = (self.txvect + self.phcorr + turn) & self.mask
        return self._comb(self._symbol())


def mt63_symbols(text, eight_bit=True):
    """Suite des codes émis par fldigi pour un texte (hors préambule/vidage)."""
    codes = []
    for ch in text:
        c = ord(ch)
        if c > 255 or (not eight_bit and c > 127):
            c = ord(".")
        if c > 127:
            codes.append(127)
            c &= 127
        codes.append(c)
    return codes


def mt63_encode_8k(text, bw=1000, long_intlv=True, af=None, prefill="random", seed=1, jam=True,
                   normalize=True, tones=0.0):
    """Signal MT63 à 8000 Hz, séquence identique à mt63::tx_process de fldigi :
    [tonalités d'accord], IntlvLen caractères nuls, le texte, IntlvLen-1 nuls de vidage, puis un
    symbole de brouillage. tones > 0 : durée (s) des deux tonalités de début de fldigi (porteuse
    basse et haute, réglage par défaut « MT63USETONES », 4 s)."""
    tx = MT63Tx(bw, long_intlv, af, prefill, seed)
    rng = np.random.default_rng(seed + 1)
    parts = [tx.send_char(0) for _ in range(tx.intlv)]
    parts += [tx.send_char(c) for c in mt63_symbols(text)]
    parts += [tx.send_char(0) for _ in range(tx.intlv - 1)]
    if jam:
        parts.append(tx.send_jam(rng))
    y = np.concatenate(parts)
    if normalize:
        y = y / (np.max(np.abs(y)) + 1e-12)
    if tones > 0:
        n = int(8000 * tones)
        t = np.arange(n) / 8000.0
        w1 = 2 * np.pi * (tx.af - bw / 2.0)
        w2 = 2 * np.pi * (tx.af + 31.0 * bw / 64.0)
        tn = 0.4 * np.cos(w1 * t) + 0.4 * np.cos(w2 * t)          # TONE_AMP 0,8 partagé en deux
        env = np.minimum(1 - np.exp(-np.arange(n) / 40.0), 1 - np.exp(-(n - np.arange(n)) / 40.0))
        y = np.concatenate([tn * env, y])
    return y


def mt63_encode(text, fs=12000, af=None, bw=1000, long_intlv=True, amp=0.5, seed=1, tones=0.0):
    """Mire MT63 à fs Hz. af = centre du signal (défaut fldigi : bord bas à 500 Hz → 500 + bw/2)."""
    y = mt63_encode_8k(text, bw, long_intlv, af, seed=seed, tones=tones)
    if int(fs) != 8000:
        fr = Fraction(float(fs) / 8000).limit_denominator(1000)
        y = resample_poly(y, fr.numerator, fr.denominator)
    return amp * y / (np.max(np.abs(y)) + 1e-12)
