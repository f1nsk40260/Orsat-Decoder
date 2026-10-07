"""Décodeurs Olivia et Contestia.

Principe d'émission (voir gen/olivia.py) : MFSK à « tones » tonalités espacées de bw/tones Hz, symboles en
cosinus surélevé qui se chevauchent de moitié, codage de Gray. Un bloc FEC porte bps = log2(tones)
caractères sur N symboles (N = 64 pour Olivia, caractères de 7 bits ; N = 32 pour Contestia, 6 bits) :
chaque caractère devient un mot de Walsh-Hadamard de N bits, embrouillé par une séquence fixe puis
entrelacé en diagonale dans les bits des symboles.

Réception :
  * mélange autour de la fréquence choisie, filtrage, décimation ;
  * TFD à fenêtre de Hann (la forme du symbole) tous les demi-symboles, sur une grille de fréquences au
    quart d'espacement de tonalité, ce qui couvre plusieurs hypothèses de décalage de fréquence ;
  * énergies normalisées par le bruit -> vraisemblances logarithmiques (LLR) des bits de chaque symbole ;
  * pour CHAQUE position temporelle (demi-symbole) et CHAQUE décalage de fréquence, décodage du bloc
    complet par transformée de Hadamard rapide ;
  * la synchro de bloc et de fréquence est celle qui maximise la confiance du décodage de Walsh, cumulée sur
    les blocs voisins (avant et après : la séquence d'embrouillage rend les mauvaises phases indécodables) ;
  * silencieux : la même confiance (pic de Walsh / bruit des autres sorties) ouvre ou ferme l'affichage ;
  * suivi de dérive : la grille d'analyse suit le décalage de fréquence trouvé quand le signal est accroché.
"""
import numpy as np
from scipy.special import i0e

from ..dsp import Decoder, Mixer, FirDecim, lowpass
from ..gen.olivia import mode_params, binary, gray, encode_block

SR = 8000.0


def fht_last(v):
    """FHT de pj_fht.h (papillon (b+a, b-a)) appliquée sur le dernier axe, vectorisée."""
    n = v.shape[-1]
    d = np.array(v, dtype=np.float32).reshape(-1, n)      # copie, calcul en place
    m = d.shape[0]
    step = 1
    while step < n:
        x = d.reshape(m, -1, 2, step)
        a, b = x[:, :, 0, :], x[:, :, 1, :]
        a += b                                            # a' = a + b
        b *= 2
        b -= a                                            # b' = 2b - (a + b) = b - a
        step *= 2
    return d.reshape(v.shape)


class OliviaBase(Decoder):
    name = "Olivia"
    contestia = False

    def __init__(self, fs, af=1500.0, tones=32, bw=1000, reverse=False, search=None, squelch=None,
                 slices=4, sig_s=3.0, olivia8bit=True):
        super().__init__(fs, af)
        p = mode_params(int(tones), int(bw), self.contestia)
        self.p = p
        self.M, self.B, self.N = p["tones"], p["bps"], p["nsym"]
        self.reverse = bool(reverse)
        self.olivia8bit = olivia8bit
        self.spacing = p["bw"] / self.M                       # écart entre tonalités (Hz)
        self.Tsym = p["symlen"] / SR                          # durée de la fenêtre de symbole (s)
        self.Tsep = p["sep"] / SR                             # période symbole (s)
        self.S = int(slices)                                  # tranches d'analyse par symbole
        self.P = self.S * self.N                              # période d'un bloc, en tranches
        self.Q = 4                                            # pas de la grille : 1/4 d'espacement
        self.delta = self.spacing / self.Q
        if search is None:                                    # plage d'acquisition (Hz) autour du clic
            search = max(self.spacing, 30.0) + self.delta
        self.Om = int(np.ceil(search / self.delta))
        self.O = 2 * self.Om + 1
        self.Dmax = int(np.ceil(max(3 * self.spacing, 150.0) / self.delta))  # dérive suivie au-delà
        self.half = self.Q * (self.M - 1) // 2                # tonalité 0 = centre - half pas
        self.X = 2 * self.Q                                   # points de grille en plus (bruit), de chaque côté
        self.sig_s = float(sig_s)
        # seuil du silencieux sur la confiance de Walsh cumulée (pic / bruit des autres sorties, moyennée sur
        # les bps caractères du bloc et sur les blocs voisins) : placé juste au-dessus du maximum observé sur
        # 300 à 400 s de bruit seul pour chaque configuration. squelch : décalage réglable par l'utilisateur.
        sq = float(squelch) if squelch is not None else 0.0
        if self.contestia:
            tm = {2: 4.0, 3: 3.7, 4: 3.55, 5: 3.35, 6: 3.25}
        else:
            tm = {2: 4.0, 3: 3.8, 4: 3.6, 5: 3.45, 6: 3.5}
        self.Tm = tm.get(self.B, 4.2 if self.B < 2 else 3.25) + sq

        # ---------------- frontal : bande de base complexe
        span = self.p["bw"] / 2 + (self.Om + self.Dmax + self.X + 2) * self.delta
        self.D = max(1, int(self.fs // (2.6 * span)))
        self.r = self.fs / self.D
        self.mix = Mixer(af, self.fs)
        self.fir = FirDecim(lowpass(self.fs, span, max(31, 4 * self.fs / span)), self.D)
        self.W = int(round(self.Tsym * self.r))
        self.hop = self.Tsep / self.S * self.r
        self.win = 0.5 * (1 - np.cos(2 * np.pi * (np.arange(self.W) + 0.5) / self.W))
        self.bb = np.zeros(0, np.complex128)
        self.bb0 = 0                                          # indice global de bb[0]
        self.k = 0                                            # prochaine tranche à calculer
        self.jc = 0                                           # centre de la grille (pas delta) : suivi de dérive
        self._make_dft()
        self.noise = None

        # ---------------- tables de décodage
        n = self.N
        code, shift = self.p["code"], self.p["shift"]
        self.sgn = np.ones((self.B, n), np.float32)
        self.bitsel = np.zeros((self.B, n), dtype=np.int64)
        for c in range(self.B):
            cb = (c * shift) & (n - 1)
            for i in range(n):
                if (code >> cb) & 1:
                    self.sgn[c, i] = -1.0
                cb = (cb + 1) & (n - 1)
                self.bitsel[c, i] = (c + i) % self.B
        # bits de chaque tonalité (après décodage de Gray), pour les LLR
        sym = np.array([binary(s) for s in range(self.M)])
        if self.reverse:
            sym = sym[::-1]
        self.mask1 = np.array([((sym >> b) & 1) for b in range(self.B)], np.float64).T   # [M, B]
        self.mask0 = 1.0 - self.mask1
        self.oB = np.arange(2 * self.Om + 1) * self.B
        tone_idx = self.Q * np.arange(self.M)
        o = np.arange(-self.Om, self.Om + 1)
        self.gidx = (o[:, None] + self.Om + self.X + tone_idx[None, :])  # [O, M] -> colonne de la grille

        # ---------------- historiques (anneaux indexés par numéro absolu de tranche)
        self.Kp, self.Kf = 2, 1                               # blocs voisins pris en compte (passé, futur)
        self.Hn = self.P * (self.Kp + self.Kf + 3)
        self.llr = np.zeros((self.Hn, self.O, self.B), np.float32)
        self.q = np.zeros((self.Hn, self.O), np.float32)      # confiance du bloc finissant à cette tranche
        self.ch = np.zeros((self.Hn, self.O, self.B), np.int16)
        self.eg = np.zeros((self.Hn, self.nj), np.float32)    # énergies normalisées de la grille (S/B)
        self.t_dec = self.P - self.S                          # prochaine tranche à décoder (bloc complet)
        self.next_t = None                                    # position attendue du prochain bloc
        self.locked = False
        self.open = False
        self.held = []
        self.miss = 0
        self.snr_db = None
        self.esc = False
        self.last_cr = False
        self.best_q = 0.0

    # ------------------------------------------------------------------ réglages
    def _make_dft(self):
        j = np.arange(-(self.Om + self.half + self.X), self.Om + self.half + self.X + 1)
        self.nj = len(j)
        f = (self.jc + j) * self.delta
        n = np.arange(self.W)
        self.dft = (self.win[None, :] * np.exp(-2j * np.pi * f[:, None] * n[None, :] / self.r)).T

    def set_af(self, af):
        steps = int(round((float(af) - self.af) / self.delta)) - self.jc   # déplacement de la grille
        super().set_af(af)
        self.mix.freq = float(af)
        self.jc = 0
        self._make_dft()
        if abs(steps) <= self.Om:
            self._roll(-steps)                               # petit retouche : on garde l'historique
        else:
            self.llr[:] = 0; self.q[:] = 0; self.ch[:] = 0
            self.locked = False
            self.next_t = None

    def _roll(self, d):
        """Décale les historiques de d pas de grille (o' = o + d)."""
        if d == 0:
            return
        for a, fill in ((self.llr, 0), (self.q, 0), (self.ch, 0), (self.eg, 0)):
            a[:] = np.roll(a, d, axis=1)
            if d > 0:
                a[:, :d] = fill
            else:
                a[:, d:] = fill

    def _recentre(self, o):
        """Suivi de dérive : la grille d'analyse se recentre sur le décalage o (en pas delta)."""
        new = int(np.clip(self.jc + o, -self.Dmax, self.Dmax))
        d = new - self.jc
        if d == 0:
            return
        self.jc = new
        self._make_dft()
        self._roll(-d)

    def status(self):
        st = {"af": round(float(self.af + (self.jc + (self.cur_o if self.locked else 0)) * self.delta), 1),
              "sync": 1 if self.open else 0, "mode": f"{self.M}/{self.p['bw']}"}
        if self.snr_db is not None and self.open:
            st["snr"] = round(self.snr_db, 1)
        return st

    cur_o = 0

    # ------------------------------------------------------------------ traitement
    def process(self, x):
        x = np.asarray(x, np.float64)
        step = int(self.fs * 0.25)
        if len(x) > step:                                 # gros paquets : découpés (taille des anneaux)
            out = []
            for i in range(0, len(x), step):
                for ev in self._process(x[i:i + step]):
                    out.append(ev["text"])
            return [{"t": "text", "text": "".join(out)}] if out else []
        return self._process(x)

    def _process(self, x):
        z = self.fir.process(self.mix.process(x))
        self.bb = np.concatenate([self.bb, z])
        end = self.bb0 + len(self.bb)
        starts = []
        while True:
            s = int(round(self.k * self.hop))
            if s + self.W > end:
                break
            starts.append(s - self.bb0)
            self.k += 1
        out = []
        if starts:
            idx = np.array(starts)[:, None] + np.arange(self.W)[None, :]
            # einsum plutôt que @ : pas de BLAS multitâche pour ces petites matrices (bien plus rapide ici)
            E = np.abs(np.einsum('kw,wj->kj', self.bb[idx], self.dft)) ** 2   # [K, nj]
            k0 = self.k - len(starts)
            self._frames(E, k0)
            out = self._decide()
        keep = int(round(self.k * self.hop)) - self.bb0
        if keep > 0:
            self.bb = self.bb[keep:]
            self.bb0 += keep
        return [{"t": "text", "text": "".join(out)}] if out else []

    def _frames(self, E, k0):
        K = E.shape[0]
        # niveau de bruit : 25e centile de la grille (loi exponentielle), peu biaisé par le signal
        nz = np.percentile(E, 25, axis=1) / 0.28768
        e = np.empty_like(E)
        for i in range(K):                                # bruit de fond lissé
            if self.noise is None:
                self.noise = nz[i]
            self.noise += 0.04 * (nz[i] - self.noise)
            e[i] = E[i] / (max(self.noise, 1e-9 * E[i].max()) + 1e-30)
        te = e[:, self.gidx]                                               # [K, O, M]
        # vraisemblance (non cohérente) qu'une tonalité porte le signal : log I0(2 sqrt(S e))
        a = 2 * np.sqrt(self.sig_s * te)
        m = np.log(i0e(a)) + a
        # LLR(bit) = log sum_{bit=0} exp(m) - log sum_{bit=1} exp(m) ; le maximum commun se simplifie
        em = np.exp(m - m.max(-1, keepdims=True))
        s0 = np.einsum("kom,mb->kob", em, self.mask0)
        s1 = np.einsum("kom,mb->kob", em, self.mask1)
        llr = np.clip(np.log(np.maximum(s0, 1e-30)) - np.log(np.maximum(s1, 1e-30)), -12, 12).astype(np.float32)
        ts = k0 + np.arange(K)
        self.llr[ts % self.Hn] = llr
        self.eg[ts % self.Hn] = e
        # décodage de tous les blocs complets
        last = self.k - 1
        if last < self.t_dec:
            return
        tt = np.arange(self.t_dec, last + 1)
        self.t_dec = last + 1
        rel = (np.arange(self.N) - (self.N - 1)) * self.S                  # tranches des N symboles
        frames = (tt[:, None] + rel[None, :]) % self.Hn                    # [T, N]
        # V[t, o, c, i] = llr[tranche du symbole i, o, bit (c + i) mod bps] * embrouillage[c, i]
        idx = (frames[:, None, None, :] * (self.O * self.B) + self.oB[None, :, None, None]
               + self.bitsel[None, None, :, :])
        V = self.llr.reshape(-1)[idx] * self.sgn
        Y = fht_last(V)                                                    # FHT : [T, O, B, N]
        aY = np.abs(Y)
        pos = aY.argmax(-1)
        pk = np.take_along_axis(Y, pos[..., None], -1)[..., 0]
        tot = (Y * Y).sum(-1)
        nse = (tot - pk * pk) / (self.N - 1)
        q = np.abs(pk).mean(-1) / np.sqrt(nse.mean(-1) + 1e-12)            # confiance (S/B de Walsh)
        ch = pos + np.where(pk < 0, self.N, 0)
        self.q[tt % self.Hn] = q
        self.ch[tt % self.Hn] = ch

    def _metric(self, t0, t1):
        """Confiance cumulée des blocs aux tranches t0..t1-1 (et des blocs voisins, à ±1 pas de fréquence)."""
        ts = np.arange(t0, t1)
        M = self.q[ts % self.Hn].astype(np.float64).copy()
        wsum = 1.0
        for j in list(range(1, self.Kf + 1)) + [-i for i in range(1, self.Kp + 1)]:
            tj = ts + j * self.P
            if tj[0] < self.P - self.S:
                continue
            nb = self.q[tj % self.Hn]
            nb = np.maximum(np.maximum(nb, np.roll(nb, 1, axis=1)), np.roll(nb, -1, axis=1))
            w = 0.6 if j > 0 else 0.4
            M += w * nb
            wsum += w
        return M / wsum

    def _decide(self):
        out = []
        while True:
            if self.next_t is None:
                self.next_t = self.P - self.S + self.P // 2
            if self.locked:
                w0, w1 = self.next_t - 2 * self.S, self.next_t + 2 * self.S + 1
            else:
                w0, w1 = self.next_t - self.P // 2, self.next_t + self.P // 2
            w0 = max(w0, self.P - self.S)
            if w1 - 1 + self.Kf * self.P >= self.t_dec:
                break
            M = self._metric(w0, w1)
            i, o = np.unravel_index(int(np.argmax(M)), M.shape)
            t = w0 + i
            mq = float(M[i, o])
            q = float(self.q[t % self.Hn, o])
            self.best_q = mq
            chars = self.ch[t % self.Hn, o]
            lk = 1.0 if self.locked else 0.0                  # hystérésis : seuils plus bas une fois accroché
            good = mq >= self.Tm - 0.15 * lk and q >= self.Tm - 0.2 - 0.3 * lk
            if good:
                self.miss = 0
                self.locked = True
                self.cur_o = int(o - self.Om)
                self._snr(t, o, chars)
            else:
                self.miss += 1
                if self.miss >= 2:
                    self.locked = False
            if getattr(self, "debug", False):
                print(f"t={t} o={o - self.Om} q={q:.2f} mq={mq:.2f} jc={self.jc} {self._dbgchars(chars)!r}")
            txt = self._chars(chars)
            # silencieux à mémoire : un bloc douteux juste avant un bon bloc est rendu quand même
            if good:
                if self.held and self.held[0] == t - self.P and abs(self.held[1] - o) <= 1:
                    out.extend(self.held[2])
                self.held = []
                out.extend(txt)
                self.open = True
            else:
                self.open = False
                self.held = (t, o, txt) if q >= self.Tm else []
            self.next_t = t + self.P
            if good and abs(o - self.Om) >= 2:
                self._recentre(o - self.Om)
                self.cur_o = 0
            if not self.locked and self.miss > 8 and self.jc != 0:
                self._recentre(-self.jc)                     # signal perdu : retour au clic
        return out

    def _snr(self, t, o, codes):
        """S/B estimé (dB dans 2500 Hz) : on recode le bloc décodé pour connaître la tonalité émise à chaque
        symbole, et on mesure son énergie normalisée par le bruit."""
        sym = encode_block([int(c) for c in codes], self.p, False)
        tone = np.array([gray(int(v)) for v in sym])
        if self.reverse:
            tone = self.M - 1 - tone
        rel = (np.arange(self.N) - (self.N - 1)) * self.S
        es = float(np.mean(self.eg[(t + rel) % self.Hn, self.gidx[o, tone]])) - 1.0
        # es ~ Es/N0 vu par la fenêtre de Hann ; S/B(2500) = Es/N0 / (Tsep * 2500), corrigé de la fenêtre
        v = 10 * np.log10(max(es, 1e-3) / (self.Tsep * 2500.0) * 1.5)
        self.snr_db = v if self.snr_db is None else 0.7 * self.snr_db + 0.3 * v

    def _dbgchars(self, codes):
        esc, cr = self.esc, self.last_cr
        r = "".join(self._chars(codes))
        self.esc, self.last_cr = esc, cr
        return r

    def _chars(self, codes):
        out = []
        for c in codes:
            c = int(c)
            if self.contestia:
                if c == 0:
                    continue
                c = {59: 32, 60: 13, 61: 8}.get(c, c + 32)
            else:
                if self.olivia8bit:
                    if self.esc:
                        self.esc = False
                        c += 128
                    elif c == 127:
                        self.esc = True
                        continue
            if c == 13 or c == 10:
                if c == 10 and self.last_cr:
                    self.last_cr = False
                    continue
                self.last_cr = c == 13
                out.append("\n")
                continue
            self.last_cr = False
            if c < 32 or c == 127:
                continue
            out.append(chr(c))
        return out


class Olivia(OliviaBase):
    name = "Olivia"
    contestia = False


class Contestia(OliviaBase):
    name = "Contestia"
    contestia = True

    def __init__(self, fs, af=1500.0, tones=8, bw=500, **kw):
        super().__init__(fs, af, tones=tones, bw=bw, **kw)
