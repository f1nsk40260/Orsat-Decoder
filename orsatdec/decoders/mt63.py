"""Décodeur MT63 (500, 1000, 2000 Hz ; entrelacement court ou long), compatible fldigi.

Constantes et briques communes à l'émetteur (gen/mt63.py) et au récepteur.

Le récepteur reprend MT63rx de fldigi (synchro par autocorrélation du signal au carré, démodulation
différentielle, décodeur de Walsh qui cherche l'écart de porteuse ±8) avec ces différences :
  - intégration de synchro de 64 symboles (fldigi : 16 ou 32) et démodulation retardée de 32 symboles :
    ~1 dB de mieux, la synchro de fldigi décroche souvent avant le FEC vers -9 dB ;
  - pas de seconde correction ±1,28 case vers la moyenne (elle peut figer la synchro sur une fausse
    fréquence sous le bruit) ; somme des corrélations calculée correctement (fldigi soustrait la partie
    imaginaire dans DoCorrelSum) ;
  - fréquence par droite de régression robuste (dérive lente suivie sans décrocher) ;
  - porteuse 0 désentrelacée avec le bon retard (fldigi la lit avec un symbole de décalage) ;
  - squelch sur le S/B lissé du FEC.
"""
from fractions import Fraction

import numpy as np
from scipy.signal import firwin

from ..dsp import Decoder, Mixer

SYMBOL_LEN = 512        # longueur de la forme de symbole = taille de la FFT
SYMBOL_SEPAR = 200      # écart entre symboles (échantillons à 8000/D Hz)
CARR_SEPAR = 4          # écart entre porteuses en cases de FFT
NCARR = 64              # porteuses de données
MODES_BW = {500: (8, 128), 1000: (4, 64), 2000: (2, 64)}   # bande -> (décimation depuis 8 kHz, long. filtre)

# Forme de symbole de MT63ASC (symbol.dat de fldigi), exprimée exactement (écart < 1e-8)
# comme une somme de 12 cosinus centrée sur l'échantillon 256.
_SHAPE_COS = (0.248782749531, 0.416434389739, 0.210798980059, 0.0459663121398, 0.0344826593344,
              0.0436773669698, 0.0109545825572, -0.00458115026081, -0.00512450468084,
              -0.00109469534919, 0.00010053367358, -0.000407222754822)

SHORT_INTLV = [4, 5, 6, 7] * 16
LONG_INTLV = list(range(1, 64)) + [0]


def symbol_shape():
    n = np.arange(SYMBOL_LEN) - SYMBOL_LEN // 2
    w = sum(c * np.cos(2 * np.pi * k * n / SYMBOL_LEN) for k, c in enumerate(_SHAPE_COS))
    w[0] = 0.0
    return w


def first_carrier(af, bw):
    """Indice (case FFT) de la première porteuse, comme MT63tx::Preset (af = centre du signal)."""
    return int(np.floor((af - bw / 2.0) * 256 / bw + 0.5))


def interleave_offsets(long_intlv):
    patt = LONG_INTLV if long_intlv else SHORT_INTLV
    L = 64 if long_intlv else 32
    p, out = 0, []
    for i in range(NCARR):
        out.append(p)
        p += patt[i]
        if p >= L:
            p -= L
    return np.array(out)


def walsh_inv(d):
    """dspWalshInvTrans (sur le dernier axe, longueur 64)."""
    d = np.array(d, np.float64)
    n = d.shape[-1]
    step = n // 2
    while step:
        v = d.reshape(d.shape[:-1] + (n // (2 * step), 2, step))
        a, b = v[..., 0, :].copy(), v[..., 1, :].copy()
        v[..., 0, :] = a - b
        v[..., 1, :] = a + b
        step //= 2
    return d


def walsh_fwd(d):
    """dspWalshTrans (sur le dernier axe)."""
    d = np.array(d, np.float64)
    n = d.shape[-1]
    step = 1
    while step < n:
        v = d.reshape(d.shape[:-1] + (n // (2 * step), 2, step))
        a, b = v[..., 0, :].copy(), v[..., 1, :].copy()
        v[..., 0, :] = a + b
        v[..., 1, :] = b - a
        step *= 2
    return d


def _walsh_table():
    t = np.zeros((128, NCARR), np.int8)
    for code in range(128):
        w = np.zeros(NCARR)
        if code < NCARR:
            w[code] = 1.0
        else:
            w[code - NCARR] = -1.0
        t[code] = walsh_inv(w) < 0
    return t


_WALSH = _walsh_table()


def walsh_codeword(code):
    return _WALSH[code & 127]


def _blackman3(ph):
    return 0.35875 + 0.48829 * np.cos(ph) + 0.14128 * np.cos(2 * ph) + 0.01168 * np.cos(3 * ph)


def quadr_shapes(af, bw, L):
    """Filtres I/Q (dspWinFirI / WinFirQ, fenêtre Blackman3) du séparateur/combineur de fldigi."""
    hbw = 1.5 * bw / 2
    lo, hi = max(af - hbw, 100.0), min(af + hbw, 4000.0)
    lo, hi = lo * np.pi / 4000, hi * np.pi / 4000
    t = np.arange(L) + 1.0 - L / 2
    ph = 2 * np.pi * t / L
    tt = np.where(t == 0, 1.0, t)
    si = np.where(t == 0, hi - lo, (np.sin(hi * tt) - np.sin(lo * tt)) / tt)
    sq = np.where(t == 0, 0.0, (-np.cos(hi * tt) + np.cos(lo * tt)) / tt)
    w = _blackman3(ph)
    return si * w / np.pi, -sq * w / np.pi


# ---------------------------------------------------------------------------------------------
# Récepteur
# ---------------------------------------------------------------------------------------------




class _Resampler:
    """Rééchantillonneur polyphasé à état (rapport rationnel up/down), signal complexe."""

    def __init__(self, fs_in, fs_out, cutoff, trans):
        fr = Fraction(float(fs_out) / float(fs_in)).limit_denominator(2000)
        self.up, self.down = fr.numerator, fr.denominator
        fsu = fs_in * self.up
        L = int(np.ceil(4.0 * fs_in / trans))          # nombre de prises par phase
        h = firwin(L * self.up, cutoff, fs=fsu, window=("kaiser", 7.0)) * self.up
        self.L = L
        self.hp = h.reshape(L, self.up).T[:, :]          # hp[ph, j] = h[ph + j*up]
        self.buf = np.zeros(L - 1, np.complex128)        # historique (L-1 derniers échantillons)
        self.t = 0                                       # instant de la prochaine sortie (× up), relatif à buf

    def process(self, x):
        x = np.concatenate([self.buf, x])
        n = len(x)
        # sortie k : t_k = t + k*down ; base = t_k // up + (L-1) doit être < n
        t0 = self.t
        kmax = ((n - self.L + 1) * self.up - 1 - t0) // self.down + 1 if n >= self.L else 0
        kmax = max(0, kmax)
        if kmax:
            tk = t0 + self.down * np.arange(kmax)
            base = tk // self.up + self.L - 1
            ph = tk % self.up
            idx = base[:, None] - np.arange(self.L)[None, :]
            y = np.einsum("kj,kj->k", x[idx], self.hp[ph])
        else:
            y = np.zeros(0, np.complex128)
        t_next = t0 + self.down * kmax
        drop = t_next // self.up                          # échantillons d'entrée devenus inutiles
        drop = min(drop, n - (self.L - 1))
        self.buf = x[drop:]
        self.t = t_next - drop * self.up
        return y


def _lp2(inp, mid, out, w):
    """dspLowPass2 vectorisé (filtre passe-bas du 2e ordre de MT63), en place."""
    w1, w2, w5 = w
    s = mid + out
    d = mid - out
    mid += w2 * inp - w1 * s
    out += w5 * d


def _sel_fit_aver(data, thres, loops=4, cplx=False):
    """dspSelFitAver : moyenne robuste (élimine les valeurs aberrantes)."""
    n = len(data)
    lev = data.sum() / n
    err = (np.abs(data) ** 2).sum() / n - abs(lev) ** 2
    for _ in range(loops):
        th = (0.5 if cplx else 1.0) * thres * thres * err
        dif = np.abs(data - lev) ** 2
        sel = dif <= th
        incl = int(sel.sum())
        if incl == 0:
            break
        dl = data[sel].sum() / incl - lev
        err = abs(dif[sel].sum() / incl - abs(dl) ** 2)
        lev = lev + dl
    return lev, float(np.sqrt(max(err, 0.0)))


def _sel_fit_line(y, thres, loops=4):
    """Comme _sel_fit_aver mais ajuste une droite y = a + b t (t = 0 pour la dernière valeur).
    Renvoie (a, b, écart-type des résidus retenus)."""
    n = len(y)
    t = np.arange(n) - (n - 1.0)
    sel = np.ones(n, bool)
    a, b, rms = float(np.mean(y)), 0.0, float(np.std(y))
    for _ in range(loops + 1):
        if sel.sum() < 3:
            break
        tt, yy = t[sel], y[sel]
        tm, ym = tt.mean(), yy.mean()
        var = ((tt - tm) ** 2).sum()
        b = float(((tt - tm) * (yy - ym)).sum() / var) if var > 0 else 0.0
        a = float(ym - b * tm)
        res = y - (a + b * t)
        rms = float(np.sqrt(np.mean(res[sel] ** 2)))
        sel = res ** 2 <= thres * thres * rms * rms + 1e-12
    return a, b, rms


class MT63(Decoder):
    """Récepteur MT63 d'après MT63rx de fldigi (P. Jalocha SP9VRC) : synchro temps/fréquence
    par autocorrélation du signal élevé au carré, démodulation différentielle de 64 porteuses,
    désentrelacement et décodage de Walsh avec recherche de l'écart de porteuse (±8)."""
    name = "MT63"

    def __init__(self, fs, af=None, bw=1000, interleave="long", integration=64, squelch=None,
                 data_integ=16):
        bw = int(bw)
        if bw not in MODES_BW:
            raise ValueError("bande MT63 : 500, 1000 ou 2000 Hz")
        if af is None:
            af = 500 + bw / 2
        super().__init__(fs, af)
        self.bw = bw
        self.long = str(interleave).lower() in ("long", "l", "64", "1", "true")
        self.integ = int(integration)
        self.dinteg = int(data_integ or integration)
        self.fit_back_frac = 0.2          # retard propre des intégrateurs de corrélation (mesuré)
        self.delay_frac = 0.5             # retard de la démodulation sur la synchro (en fraction de l intégration)
        self.fs_dec = 2.0 * bw
        self.bin_hz = self.fs_dec / SYMBOL_LEN
        self.carr_hz = CARR_SEPAR * self.bin_hz
        # Squelch : rapport signal/bruit du FEC lissé (comme fldigi) ; le bruit seul reste sous ~3,15
        self.sq = float(squelch) if squelch is not None else 3.3
        self.af0 = float(af)
        self._setup(float(af))

    # -- initialisation complète --
    def _setup(self, af):
        bw = self.bw
        first = first_carrier(af, bw)
        self.f_mix = (first + 128) * bw / 256.0        # porteuses aux cases -128 + 4 i
        self.mix = Mixer(self.f_mix, self.fs)
        self.rs = _Resampler(self.fs, self.fs_dec, 0.80 * bw, 0.45 * bw)
        N = SYMBOL_LEN
        self.mask = N - 1
        self.win = symbol_shape()
        I = self.integ
        self.first = (-128) % N
        self.symb_div = 4
        self.scan_margin = 8
        self.sync_step = SYMBOL_SEPAR // 4
        self.proc_delay = int(round(self.delay_frac * I)) * SYMBOL_SEPAR
        self.scan_first = (self.first - self.scan_margin * CARR_SEPAR) % N
        self.scan_len = (NCARR + 2 * self.scan_margin) * CARR_SEPAR
        self.scan_bins = (self.scan_first + np.arange(self.scan_len)) & self.mask
        c = (self.scan_first * SYMBOL_SEPAR) & self.mask
        cc = (c + SYMBOL_SEPAR * np.arange(self.scan_len)) & self.mask
        self.sync_phcorr = np.exp(2j * np.pi * 2 * cc / N)
        S = self.scan_len
        self.sync_pipe = np.zeros((4, S), np.complex128)
        self.cor_mid = np.zeros((4, S), np.complex128)
        self.cor_out = np.zeros((4, S), np.complex128)
        self.pw_mid = np.zeros(S)
        self.pw_out = np.zeros(S)
        self.w = (1.0 / I, 2.0 / I, 5.0 / I)
        self.wp = (1.0 / (4 * I), 2.0 / (4 * I), 5.0 / (4 * I))
        self.fit_len = 2 * self.scan_margin * CARR_SEPAR
        self.symb_pipe = np.zeros(I, np.complex128)
        self.freq_pipe = np.zeros(I)
        self.track_ptr = 0
        self.symb_fit_pos = self.scan_margin * CARR_SEPAR
        self.locked = False
        self.sync_conf = 0.0
        self.sync_fofs = 0.0
        self.sync_fdev = 0.0
        self.symb_ptr = 0
        self.sync_shift = 0.0
        self.aver_symb = 0j
        self.aver_freq = 0.0
        self.data_freq = 0.0
        self.fit_back = self.fit_back_frac * I
        self.hold_thr = 1.5 * np.sqrt(1.0 / (I * NCARR))
        self.lock_thr = 1.5 * self.hold_thr
        self.sync_ptr = 0
        # données
        self.dmargin = 8
        self.dscan_len = NCARR + 2 * self.dmargin
        self.dscan_first = (self.first - self.dmargin * CARR_SEPAR) % N
        self.dbins = (self.dscan_first + CARR_SEPAR * np.arange(self.dscan_len)) & self.mask
        self.ref = np.zeros(self.dscan_len, np.complex128)
        Id = self.dinteg
        self.wd = (1.0 / Id, 2.0 / Id, 5.0 / Id)
        self.dpipe_len = Id // 2
        self.dpipe = np.zeros((self.dpipe_len, self.dscan_len))
        self.even = np.arange(self.dscan_len) % 2 == 0
        self.dpipe_ptr = 0
        self.dpw_mid = np.zeros(self.dscan_len)
        self.dpw_out = np.zeros(self.dscan_len)
        # décodeur FEC
        L = 64 if self.long else 32
        self.L = L
        self.p = interleave_offsets(self.long)
        # retard de désentrelacement de chaque porteuse : total constant (L+1) symboles.
        # (fldigi lit la porteuse 0 avec un retard 1 au lieu de L+1 : un bit faux sur 64.)
        e = (L - self.p) % L
        self.ddelay = L + 1 - e
        self.nrows = L + 2
        self.ipipe = np.zeros((self.nrows, self.dscan_len))
        self.iptr = 0
        nscan = 2 * self.dmargin + 1
        self.nscan = nscan
        s = np.arange(nscan)[:, None]
        i = np.arange(NCARR)[None, :]
        self.dec_rowoff = self.ddelay[None, :] - ((s & 1) & (i & 1))   # lignes en arrière
        self.dec_col = s + i
        self.snr_mid = np.zeros(nscan)
        self.snr_out = np.zeros(nscan)
        self.dec_len = Id // 2
        self.dec_pipe = np.zeros((self.dec_len, nscan), np.int64)
        self.dec_ptr = 0
        self.snr_pipe = np.zeros((self.dec_len, nscan))
        self.char_snr = 0.0
        self.snr = 0.0
        self.carr_ofs = 0
        self.escape = False
        self.last_cr = False
        # tampon de bande de base (index absolu) : on garde l'historique nécessaire
        hist = self.proc_delay + N + SYMBOL_SEPAR
        self.z = np.zeros(hist, np.complex128)
        self.z0 = -hist                      # index absolu de z[0]
        self.sync_pos = 0                    # début absolu de la prochaine tranche de synchro
        self.data_prev = -self.proc_delay
        self.level = 0.0
        self.noise = 1e-9

    def set_af(self, af):
        af = float(af)
        self.af0 = af
        self.af = af
        cur = self._cur_af()
        # petit déplacement : la synchro (±8 porteuses) suit sans rien perdre
        if abs(af - cur) <= 3 * self.carr_hz and abs(af - self.f_mix) <= 6 * self.carr_hz:
            return
        self._setup(af)

    def _cur_af(self):
        return self.f_mix + (self.sync_fofs + CARR_SEPAR * self.carr_ofs) * self.bin_hz

    def status(self):
        snr_db = 10 * np.log10(max(self.snr, 1e-3))
        return {"af": round(self._cur_af(), 1), "snr": round(float(snr_db), 1),
                "sync": int(self.locked), "mode": f"MT63-{self.bw}{'L' if self.long else 'S'}"}

    # -- synchronisation (MT63rx::SyncProcess) --
    def _sync(self, X):
        self.sync_ptr = (self.sync_ptr + 1) & 3
        sp = self.sync_ptr
        Xs = X[self.scan_bins]
        P = Xs.real ** 2 + Xs.imag ** 2
        A = np.sqrt(P)
        D = np.where(P > 0, Xs * Xs / np.where(A > 0, A, 1.0), 0)
        _lp2(P, self.pw_mid, self.pw_out, self.wp)
        cor = D * np.conj(self.sync_pipe[sp] * self.sync_phcorr)
        _lp2(cor, self.cor_mid[sp], self.cor_out[sp], self.w)
        self.sync_pipe[sp] = D
        if sp != (self.symb_ptr ^ 2):
            return
        pw = self.pw_out
        norm = np.where(pw > 0, self.cor_out / np.where(pw > 0, pw, 1.0), 0)      # (4, S)
        FL = self.fit_len
        # somme sur les 64 porteuses pour chaque position possible (paires au temps s, impaires à s+2)
        aver = np.empty((4, FL), np.complex128)
        idx = np.arange(FL)[:, None] + 8 * np.arange(32)[None, :]
        for s in range(4):
            s2 = (s + 2) & 3
            aver[s] = (norm[s][idx].sum(1) + norm[s2][idx + 4].sum(1)) / NCARR
        am = np.abs(aver)
        fit = (am[0] - am[2]) + 1j * (am[1] - am[3])
        fp = fit.real ** 2 + fit.imag ** 2
        j = int(np.argmax(fp[2:FL - 2])) + 2
        Pm = fp[j]
        k = int((j - self.symb_fit_pos) / CARR_SEPAR)
        if k > 1:
            j -= (k - 1) * CARR_SEPAR
        elif k < -1:
            j -= (k + 1) * CARR_SEPAR
        j = min(max(j, 2), FL - 3)
        self.symb_fit_pos = j
        if Pm > 0:
            st = fit[j] + 0.5 * (fit[j - 1] + fit[j + 1])
            symb_shift = (np.angle(st) / (2 * np.pi)) * 4
            if symb_shift < 0:
                symb_shift += 4

            def sp_(z):
                return st.real * z.real + st.imag * z.imag
            pI = sp_(fit[j]) + 0.7 * sp_(fit[j - 1]) + 0.7 * sp_(fit[j + 1])
            pQ = 0.7 * sp_(fit[j + 1]) - 0.7 * sp_(fit[j - 1]) + 0.5 * sp_(fit[j + 2]) - 0.5 * sp_(fit[j - 2])
            fofs = j + np.arctan2(pQ, pI) / (2 * np.pi / 8)
            i = int(np.floor(fofs + 0.5))
            i = min(max(i, 0), FL - 1)
            s = int(np.floor(symb_shift)) & 3
            s2 = (s + 1) & 3
            w0 = (np.floor(symb_shift) + 1 - symb_shift)
            w1 = symb_shift - np.floor(symb_shift)
            Aa = (0.5 * SYMBOL_LEN) / SYMBOL_SEPAR
            z = w0 * aver[s][i] + w1 * aver[s2][i]
            F0 = i + np.angle(z) / (2 * np.pi) * Aa - fofs
            cand = (F0 - Aa, F0, F0 + Aa)
            fofs += min(cand, key=abs)
            # NB : fldigi corrige encore ici de ±Aa vers la moyenne précédente ; sous le bruit cela
            # peut verrouiller durablement la synchro sur une fausse fréquence (écart de 1,28 case) :
            # on s'en passe, la moyenne robuste (sel_fit_aver) rejette déjà les mesures aberrantes.
        else:
            st = 0j
            fofs = 0.0
        A2 = 2 * CARR_SEPAR
        if self.locked:
            if st.real * self.aver_symb.real + st.imag * self.aver_symb.imag < 0:
                st = -st
                fofs -= CARR_SEPAR
            k = np.floor((fofs - self.aver_freq) / A2 + 0.5)
            fofs -= k * A2
        else:
            prev = self.symb_pipe[self.track_ptr]
            if st.real * prev.real + st.imag * prev.imag < 0:
                st = -st
                fofs -= CARR_SEPAR
            k = np.floor(fofs / A2 + 0.5)
            fofs -= k * A2
        self.track_ptr = (self.track_ptr + 1) % self.integ
        self.symb_pipe[self.track_ptr] = st
        self.freq_pipe[self.track_ptr] = fofs
        self.aver_symb, _ = _sel_fit_aver(self.symb_pipe, 3.0, 4, cplx=True)
        # Fréquence : droite de régression robuste sur l'historique (fldigi : simple moyenne) ; une
        # dérive lente ne fait alors ni décrocher la synchro ni prendre de retard.
        order = (self.track_ptr + 1 + np.arange(self.integ)) % self.integ
        a, b, self.sync_fdev = _sel_fit_line(self.freq_pipe[order], 2.5, 4)
        self.aver_freq = a                                   # valeur prédite pour la dernière mesure
        self.data_freq = a - b * self.fit_back               # au moment des tranches démodulées
        conf = abs(self.aver_symb)
        self.sync_conf = conf
        self.sync_fofs = float(self.data_freq)
        if conf > 0:
            ph = np.angle(self.aver_symb) / (2 * np.pi)
            sh = ph * SYMBOL_SEPAR
            if sh < 0:
                sh += SYMBOL_SEPAR
            self.symb_ptr = int(np.floor(ph * 4)) & 3
            self.sync_shift = sh
        if self.locked:
            if conf < self.hold_thr or self.sync_fdev > 0.25:
                self.locked = False
        elif conf > self.lock_thr and self.sync_fdev < 0.125:
            self.locked = True
        self.sync_conf *= 0.5

    # -- démodulation (MT63rx::DataProcess) --
    def _slice(self, a):
        i = a - self.z0
        return self.z[i:i + SYMBOL_LEN]

    def _data(self, s1, fofs, tdist):
        N = SYMBOL_LEN
        rot = np.exp(-2j * np.pi * fofs * np.arange(N) / N) * self.win
        F = np.fft.fft(np.stack([self._slice(s1), self._slice(s1 + SYMBOL_SEPAR // 2)]) * rot, axis=1)
        X = np.where(self.even, F[0][self.dbins], F[1][self.dbins])
        # rotation attendue de chaque porteuse depuis le symbole précédent (écart de temps + CAF)
        ph = np.exp(2j * np.pi * ((tdist * self.dbins) & self.mask) / N) * np.exp(2j * np.pi * tdist * fofs / N)
        _lp2(X.real ** 2 + X.imag ** 2, self.dpw_mid, self.dpw_out, self.wd)
        pw = self.dpw_out
        pwn = np.where(pw > 0, pw, 1.0)
        dv = X * np.conj(self.ref * ph)                  # détection différentielle
        self.ref = X
        soft_now = np.where(pw > 0, np.clip(dv.real / pwn, -1, 1), 0.0)
        old = self.dpipe[self.dpipe_ptr].copy()
        self.dpipe[self.dpipe_ptr] = soft_now
        self.dpipe_ptr = (self.dpipe_ptr + 1) % self.dpipe_len
        return self._fec(old)

    # -- désentrelacement + Walsh (MT63decoder::Process) --
    def _fec(self, soft):
        self.ipipe[self.iptr] = soft
        rows = (self.iptr - self.dec_rowoff) % self.nrows
        self.iptr = (self.iptr + 1) % self.nrows
        M = self.ipipe[rows, self.dec_col]                       # (17, 64)
        W = walsh_fwd(M)
        mx = W.argmax(1)
        mn = W.argmin(1)
        r = np.arange(self.nscan)
        vmax = W[r, mx]
        vmin = W[r, mn]
        pos = np.abs(vmax) > np.abs(vmin)
        code = np.where(pos, mx + NCARR, mn)
        sig = np.where(pos, np.abs(vmax), np.abs(vmin))
        W[r, np.where(pos, mx, mn)] = 0.0
        noise = np.sqrt((W ** 2).mean(1))
        snr = np.where(noise > 0, sig / np.where(noise > 0, noise, 1.0), 0.0)
        _lp2(snr, self.snr_mid, self.snr_out, self.wd)
        self.dec_pipe[self.dec_ptr] = code
        self.snr_pipe[self.dec_ptr] = snr
        self.dec_ptr = (self.dec_ptr + 1) % self.dec_len
        best = int(np.argmax(self.snr_out))
        self.snr = float(self.snr_out[best])
        self.char_snr = float(self.snr_pipe[self.dec_ptr, best])
        self.carr_ofs = best - self.dmargin
        return int(self.dec_pipe[self.dec_ptr, best])

    def _char(self, c, out):
        if self.snr < self.sq:
            self.escape = False
            return
        if c == 127:
            self.escape = True
            return
        if self.escape:
            c += 128
            self.escape = False
        elif c < 8:
            return
        if c == 13:
            out.append("\n")
            self.last_cr = True
            return
        if c == 10:
            if not self.last_cr:
                out.append("\n")
            self.last_cr = False
            return
        self.last_cr = False
        if c >= 32 and c != 127:
            out.append(chr(c))

    def process(self, x):
        x = np.asarray(x, np.float64)
        z = self.rs.process(self.mix.process(x))
        if len(z):
            self.z = np.concatenate([self.z, z])
        end = self.z0 + len(self.z)
        N = SYMBOL_LEN
        out = []
        # tranches de synchro disponibles, FFT en un seul appel
        npos = (end - N - self.sync_pos) // self.sync_step + 1 if end - N >= self.sync_pos else 0
        if npos > 0:
            pos = self.sync_pos + self.sync_step * np.arange(npos)
            idx = (pos - self.z0)[:, None] + np.arange(N)[None, :]
            FX = np.fft.fft(self.z[idx] * self.win, axis=1)
            for k in range(npos):
                self._sync(FX[k])
                if self.sync_ptr == self.symb_ptr:
                    s1 = int(pos[k]) - self.proc_delay + (int(self.sync_shift) - self.symb_ptr * self.sync_step)
                    c = self._data(s1, self.sync_fofs, s1 - self.data_prev)
                    self.data_prev = s1
                    self._char(c, out)
            self.sync_pos += self.sync_step * npos
        # on ne garde que l'historique utile
        keep_from = self.sync_pos - self.proc_delay - SYMBOL_SEPAR - N
        cut = keep_from - self.z0
        if cut > 4096:
            self.z = self.z[cut:]
            self.z0 += cut
        return [{"t": "text", "text": "".join(out)}] if out else []
