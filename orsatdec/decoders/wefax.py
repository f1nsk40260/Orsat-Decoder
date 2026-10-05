"""Fac-similé météo HF (WEFAX) : IOC 576 / 288, 60 à 240 lignes/min, FM 1500 Hz (noir) – 2300 Hz (blanc).

Chaîne : mélange vers la bande de base autour du centre (1900 Hz), filtrage, discriminateur de fréquence
(différence de phase entre échantillons), puis découpage en lignes.

Automatisme (comme fldigi / la norme OMM) :
  - signal de départ APT : alternance noir/blanc à 300 Hz (IOC 576) ou 675 Hz (IOC 288), 5 s ;
  - lignes de phasage : noir avec une impulsion blanche de 5 % centrée sur le début de ligne ;
    on mesure les instants des impulsions et une droite des moindres carrés donne à la fois la durée
    exacte de ligne (correction de pente) et l'origine des lignes ;
  - signal d'arrêt APT : alternance à 450 Hz → fin de l'image.
Si le départ a été manqué (canal ouvert en cours d'émission), l'image démarre « en roue libre » dès
qu'un signal de fac-similé est reconnu (forte corrélation d'une ligne à l'autre) ; la pente est ensuite
estimée sur l'image elle-même et le début de ligne est calé sur la marge ou le cadre de la carte :
l'image déjà reçue est alors recalculée et renvoyée.
En réception, la pente reste suivie en continu par corrélation entre lignes distantes de 16 lignes.
"""
import base64

import numpy as np

from ..dsp import Decoder, Mixer, FirDecim, lowpass

TAU = 2 * np.pi
LPMS = (60, 90, 100, 120, 180, 240)


# ------------------------------------------------------------------ briques communes (aussi pour la SSTV)
class FMDemod:
    """Discriminateur FM : renvoie la fréquence instantanée (Hz, relative à fc) à fs2 = fs / D."""

    def __init__(self, fs, fc, cutoff, fs2_min=6000.0):
        self.fs = float(fs)
        self.D = max(1, int(self.fs // fs2_min))
        self.fs2 = self.fs / self.D
        self.mix = Mixer(fc, self.fs)
        self.fir = FirDecim(lowpass(self.fs, cutoff, 4 * self.fs / cutoff), self.D)
        self.prev = 0j
        self.k = self.fs2 / TAU

    @property
    def fc(self):
        return self.mix.freq

    @fc.setter
    def fc(self, f):
        self.mix.freq = float(f)

    def process(self, x):
        z = self.fir.process(self.mix.process(np.asarray(x, np.float64)))
        if len(z) == 0:
            return np.zeros(0, np.float32), z
        zz = np.concatenate([[self.prev], z])
        self.prev = z[-1]
        f = np.angle(zz[1:] * np.conj(zz[:-1])) * self.k
        return f.astype(np.float32), z


class Buf:
    """Tampon extensible (ajout amorti, purge par le début) : évite de recopier tout le tampon
    à chaque bloc quand il contient une image entière."""

    def __init__(self, dtype):
        self.a = np.zeros(1024, dtype)
        self.s = 0
        self.e = 0

    def append(self, x):
        n = len(x)
        if self.e + n > len(self.a):
            cur = self.a[self.s:self.e]
            cap = max(len(self.a), 2 * (len(cur) + n))
            na = np.zeros(cap, self.a.dtype)
            na[:len(cur)] = cur
            self.a, self.s, self.e = na, 0, len(cur)
        self.a[self.e:self.e + n] = x
        self.e += n

    def drop(self, n):
        self.s = min(self.e, self.s + n)

    @property
    def v(self):
        return self.a[self.s:self.e]

    def __len__(self):
        return self.e - self.s


def box_sample(buf, start, step, n):
    """Moyenne de buf sur n intervalles consécutifs [start + i*step, start + (i+1)*step) (indices
    fractionnaires) : échantillonnage anti-repliement, qui moyenne aussi le bruit sur chaque pixel."""
    a = int(max(0, np.floor(start) - 1))
    b = int(min(len(buf), np.ceil(start + step * n) + 2))
    if b - a < 2:
        return np.full(n, float(buf[min(max(a, 0), len(buf) - 1)]) if len(buf) else 0.0)
    seg = np.asarray(buf[a:b], np.float64)
    C = np.concatenate([[0.0], np.cumsum(seg)])
    e = start - a + step * np.arange(n + 1) + 0.5
    Ce = np.interp(e, np.arange(len(C)), C)
    return np.diff(Ce) / step


def b64(a):
    return base64.b64encode(np.ascontiguousarray(a, np.uint8).tobytes()).decode("ascii")


def row_shift(a, b, maxs):
    """Décalage (pixels, sous-pixel) de la ligne b par rapport à la ligne a, et qualité du pic."""
    a = a - a.mean()
    b = b - b.mean()
    na, nb = np.sqrt(np.sum(a * a)), np.sqrt(np.sum(b * b))
    if na < 1e-6 or nb < 1e-6:
        return 0.0, 0.0
    n = len(a)
    N = 1 << int(np.ceil(np.log2(2 * n)))
    c = np.fft.irfft(np.conj(np.fft.rfft(a, N)) * np.fft.rfft(b, N), N) / (na * nb)
    lags = np.concatenate([np.arange(0, maxs + 1), np.arange(-maxs, 0)])
    cc = c[lags % N]
    i = int(np.argmax(cc))
    s = float(lags[i])
    if 0 < i < len(cc) - 1 and lags[i - 1] == lags[i] - 1 and lags[(i + 1) % len(cc)] == lags[i] + 1:
        y0, y1, y2 = cc[i - 1], cc[i], cc[(i + 1) % len(cc)]
        den = y0 - 2 * y1 + y2
        if den < 0:
            s += 0.5 * (y0 - y2) / den
    return s, float(cc[i])


# ------------------------------------------------------------------ décodeur
class WeFax(Decoder):
    name = "WEFAX"
    kind = "img"

    def __init__(self, fs, af=1900.0, ioc=576, lpm=120, shift=800.0, auto=True, free_run=True,
                 timeout=10.0, max_lines=4000):
        super().__init__(fs, af)
        self.ioc = int(ioc)
        self.W = int(self.ioc * np.pi)
        self.lpm_user = float(lpm)
        self.lpm = float(lpm)
        self.shift = float(shift)
        self.auto = auto
        self.free_run = free_run
        self.timeout = float(timeout)
        self.max_lines = int(max_lines)
        self.dem = FMDemod(fs, af, shift / 2 + 500)
        self.raw = np.zeros(0)
        self.fs2 = self.dem.fs2
        self.off = 0.0                       # CAF : décalage mesuré du centre (Hz)
        self.af0 = float(af)
        self.span = 150.0
        # flux vidéo (0 = noir, 1 = blanc) et fréquence brute, indexés en absolu
        self.v = np.zeros(0, np.float32)
        self.fr = np.zeros(0, np.float32)
        self.v0 = 0                          # indice absolu de v[0]
        self.n = 0                           # nombre total d'échantillons vidéo reçus
        # fenêtres APT / présence de signal (0,5 s)
        self.win = int(self.fs2 / 2)
        self.win_pos = 0
        self.apt_hist = []                   # derniers résultats de fenêtre : "start" / "stop" / None
        self.sig_hist = []
        self.no_sig = 0.0                    # secondes sans signal
        self.snr = 0.0
        self.since_end = 1e9                 # secondes depuis la dernière fin d'image
        self.since_afc = 0
        # état
        self.state = "idle"                  # idle | apt | phasing | image
        self.pulses = []                     # centres des impulsions de phasage (indices absolus)
        self.scan_pos = 0
        self.T = self._spl(self.lpm)
        self.t0 = 0.0
        self.k = 0                           # prochaine ligne à extraire
        self.rows = []                       # image (lignes uint8)
        self.pending = []                    # indices des lignes à envoyer
        self.free = False                    # roue libre, pas encore calée
        self.aligned = False
        self.slant_at = 0
        self.flush_count = 0
        self.ev = []

    # ---------------------------------------------------------------- utilitaires
    def _spl(self, lpm):
        return self.fs2 * 60.0 / lpm

    def set_af(self, af):
        super().set_af(af)
        self.af0 = float(af)
        self.off = 0.0
        self.span = 50.0
        self.dem.fc = af

    def status(self):
        st = {"af": round(float(self.af0 + self.off), 1), "snr": round(float(self.snr), 1), "lpm": round(self.lpm, 1),
              "ioc": self.ioc, "sync": 1 if self.state == "image" and self.aligned else 0,
              "mode": {"idle": "attente", "apt": "APT", "phasing": "phasage", "image": "image"}[self.state]}
        if self.state == "image":
            st["ligne"] = len(self.rows)
        return st

    # ---------------------------------------------------------------- CAF
    def _afc(self):
        """Le noir (−400 Hz) et le blanc (+400 Hz) forment deux amas de fréquence instantanée :
        la moyenne locale autour de chaque pic (pondérée par leur population) donne le centre exact."""
        n = int(2 * self.fs2)
        f = self.fr[-n:].astype(np.float64)
        if len(f) < n or len(self.sig_hist) < 3 or not all(self.sig_hist[-3:]):
            return
        L = max(1, int(0.004 * self.fs2))           # moyenne sur 4 ms : réduit fortement le bruit FM
        f = f[:len(f) // L * L].reshape(-1, L).mean(axis=1)
        n = len(f)
        half = self.shift / 2
        h, e = np.histogram(f, bins=280, range=(-700, 700))
        h = np.convolve(h, np.ones(5) / 5, mode="same")
        c = (e[:-1] + e[1:]) / 2
        num = den = 0.0
        for sgn in (-1, 1):
            sel = np.abs(c - sgn * half) < self.span
            if not sel.any():
                continue
            pk = c[np.argmax(np.where(sel, h, -1))]
            for _ in range(3):
                m = np.abs(f - pk) < 50
                if m.sum() < 0.03 * n:
                    break
                pk = float(np.mean(f[m]))
            else:
                w = float(m.sum())
                num += w * (pk - sgn * half)
                den += w
        if den == 0:
            return
        est = num / den
        if abs(est) > self.span + 10:
            return
        g = 0.4 if self.state in ("idle", "apt") else 0.1
        self.off += g * (est - self.off)
        self.dem.fc = self.af0 + self.off         # le filtre suit : plus de biais vers l'ancien centre

    # ---------------------------------------------------------------- fenêtres de 0,5 s
    def _window(self, v, f):
        """Détection APT (puissance vidéo à 300/675/450 Hz) et présence d'un signal FM."""
        x = v - v.mean()
        var = float(np.sum(x * x)) + 1e-9
        t = np.arange(len(x)) / self.fs2
        r = {}
        for fq in (300.0, 675.0, 450.0):
            p = np.abs(np.dot(x, np.exp(-1j * TAU * fq * t))) ** 2 * 2 / len(x)
            r[fq] = p / var
        res = None
        th = 0.2
        if r[300.0] > th and var / len(x) > 0.03:
            res = 576
        elif r[675.0] > th and var / len(x) > 0.03:
            res = 288
        elif r[450.0] > th and var / len(x) > 0.03:
            res = "stop"
        if res is not None and r[300.0 if res == 576 else 675.0 if res == 288 else 450.0] > 0.5:
            # alternance noir/blanc 50 % : la fréquence moyenne est le centre
            est = float(np.mean(np.clip(f, -self.shift, self.shift)))
            if abs(est) < self.span + 10:
                self.off += 0.5 * (est - self.off)
                self.dem.fc = self.af0 + self.off
        # présence : densité spectrale dans la bande du signal comparée aux bandes voisines
        raw = self.raw[-int(self.fs / 2):]
        sig = False
        if len(raw) >= int(self.fs / 4):
            sp = np.abs(np.fft.rfft(raw * np.hanning(len(raw)))) ** 2
            fq = np.fft.rfftfreq(len(raw), 1 / self.fs)
            c = self.af0 + self.off
            half = self.shift / 2
            ib = np.abs(fq - c) < half + 50
            ob = (np.abs(fq - c) > half + 400) & (np.abs(fq - c) < half + 800) & (fq > 100) & (fq < self.fs / 2 - 100)
            if ib.any() and ob.any():
                ratio = np.mean(sp[ib]) / (np.mean(sp[ob]) + 1e-20)
                sig = ratio > 1.5
                self.snr = float(10 * np.log10(max(ratio - 1, 1e-3) * (self.shift + 100) / 2500))
        return res, sig

    # ---------------------------------------------------------------- traitement
    def process(self, x):
        self.ev = []
        self.raw = np.concatenate([self.raw, np.asarray(x, np.float64)])[-int(self.fs):]
        f, _ = self.dem.process(x)
        if len(f) == 0:
            return []
        df = self.dem.fc - self.af0
        self.fr = np.concatenate([self.fr, f + df])
        v = np.clip(0.5 + (f - (self.off - df)) / self.shift, 0.0, 1.0).astype(np.float32)
        self.v = np.concatenate([self.v, v])
        self.n += len(v)
        self.since_afc += len(v)
        if self.since_afc >= self.fs2:
            self.since_afc = 0
            self._afc()
        dt = len(v) / self.fs2
        self.since_end += dt
        # fenêtres APT
        while self.n - self.win_pos >= self.win:
            a = self.win_pos - self.v0
            res, sig = self._window(self.v[a:a + self.win], self.fr[len(self.fr) - (self.n - self.win_pos):][:self.win])
            self.win_pos += self.win
            self.apt_hist = (self.apt_hist + [res])[-4:]
            self.sig_hist = (self.sig_hist + [sig])[-40:]
            self.no_sig = 0.0 if sig else self.no_sig + 0.5
            self._on_window()
        if self.state == "phasing":
            self._phasing()
        if self.state == "image":
            self._lines()
            if self.no_sig >= self.timeout:
                self._end("signal perdu")
        if self.state == "idle" and self.free_run:
            self._try_free_run()
        self.flush_count += len(v)
        if self.pending and (self.flush_count >= self.fs2 / 2):
            self._flush()
        self._trim()
        return self.ev

    def _on_window(self):
        h = self.apt_hist
        start = len(h) >= 2 and h[-1] in (576, 288) and h[-1] == h[-2]
        stop = len(h) >= 2 and h[-1] == "stop" and h[-2] == "stop"
        if self.auto and start and self.state != "apt":
            if self.state == "image":
                self._end("nouveau départ APT")
            if h[-1] != self.ioc:
                self.ioc = h[-1]
                self.W = int(self.ioc * np.pi)
            self.state = "apt"
        elif self.state == "apt" and not (h[-1] in (576, 288)):
            # fin du signal de départ : on cherche les lignes de phasage
            self.state = "phasing"
            self.pulses = []
            self.scan_pos = self.win_pos - self.win
            self.phase_t = 0.0
        if stop and self.state in ("image", "phasing"):
            self._end("APT stop")
        if self.state == "phasing":
            self.phase_t += 0.5
            if self.phase_t > 45:            # pas de phasage reconnu : on part en roue libre
                self.state = "idle"
                self.since_end = 1e9
                self._start_image(free=True, t0=float(self.n - 2 * self._spl(self.lpm_user)))

    # ---------------------------------------------------------------- phasage
    def _phasing(self):
        a = max(self.scan_pos, self.v0)
        seg = self.v[a - self.v0:]
        L = int(0.010 * self.fs2) | 1          # filtre adapté à l'impulsion la plus courte (12,5 ms à 240 l/min)
        if len(seg) < int(0.3 * self.fs2):
            return
        sm = np.convolve(seg, np.ones(L) / L, mode="same")
        blk = float(np.median(sm))               # niveau du noir (relevé par le bruit FM à faible S/B)
        thr = min(0.55, blk + 0.22)
        hi = sm > thr
        d = np.diff(hi.astype(np.int8))
        ups = np.nonzero(d == 1)[0] + 1
        downs = np.nonzero(d == -1)[0] + 1
        last_done = a
        guard = int(0.08 * self.fs2)
        for u in ups:
            dd = downs[downs > u]
            if len(dd) == 0:
                break
            e = dd[0]
            if e + guard >= len(sm) or u - guard < 0:
                if e + guard >= len(sm):
                    break
                continue
            dur = (e - u) / self.fs2
            if 0.008 <= dur <= 0.065 and sm[u - guard:u - L].mean() < thr - 0.1 and sm[e + L:e + guard].mean() < thr - 0.1:
                # centre précis : barycentre de la vidéo lissée au-dessus du seuil
                idx = np.arange(u - L, e + L)
                w = np.clip(sm[idx] - thr, 0, None)
                c = float(np.sum(idx * w) / max(np.sum(w), 1e-9))
                self.pulses.append(a + c)
            last_done = a + e
        self.scan_pos = max(self.scan_pos, last_done)
        self._phasing_fit()

    def _phasing_fit(self):
        p = np.array(self.pulses)
        if len(p) < 5:
            return
        dif = np.diff(p)
        best = None
        for lpm in LPMS:
            T = self._spl(lpm)
            k = dif / T
            ok = np.abs(k - np.round(k)) < 0.02
            ok &= np.round(k) >= 1
            if best is None or ok.sum() > best[1]:
                best = (lpm, ok.sum())
        lpm, nok = best
        if nok < 4:
            return
        T0 = self._spl(lpm)
        # numéros de ligne des impulsions (référence : celle qui rallie le plus d'impulsions),
        # puis droite des moindres carrés
        best = None
        for ref in p[-6:]:
            q = (p - ref) / T0
            sel = np.abs(q - np.round(q)) < 0.03
            if best is None or sel.sum() > best[0].sum():
                best = (sel, ref)
        sel, ref = best
        kk = np.round((p - ref) / T0)[sel]
        pp = p[sel]
        kk, uk = kk, np.unique(kk)
        if len(uk) < len(kk):
            return
        if len(kk) < 5:
            return
        A = np.vstack([kk, np.ones_like(kk)]).T
        sol = np.linalg.lstsq(A, pp, rcond=None)[0]
        T, c = float(sol[0]), float(sol[1])
        if abs(T / T0 - 1) > 0.02:
            return
        # le phasage est fini quand la ligne suivante attendue n'a pas d'impulsion
        c = c + T * kk.max()                     # position ajustée de la dernière impulsion
        nxt = c + T
        if self.n - self.v0 > 0 and self.n > nxt + 0.2 * T and self.scan_pos > nxt + 0.1 * T:
            self.lpm = lpm * T0 / T
            self.T = T
            self._start_image(free=False, t0=c + T)

    # ---------------------------------------------------------------- roue libre
    def _try_free_run(self):
        if self.since_end < 20 or len(self.sig_hist) < 16 or not all(self.sig_hist[-16:]):
            return
        if any(h is not None for h in self.apt_hist):
            return
        T = self._spl(self.lpm_user)
        n = int(8 * self.fs2) // 8 * 8
        if self.n - self.v0 < n + T or not hasattr(self, "_fr_next"):
            self._fr_next = getattr(self, "_fr_next", 0)
        if self.n < getattr(self, "_fr_next", 0) or len(self.v) < n + int(T):
            return
        self._fr_next = self.n + int(self.fs2)
        L = 8
        a = self.v[-n:].reshape(-1, L).mean(axis=1) if n % L == 0 else self.v[-n:]
        b = self.v[-n - int(T):-int(T)]
        b = b.reshape(-1, L).mean(axis=1) if n % L == 0 else b
        a = a - a.mean()
        b = b - b.mean()
        den = np.sqrt(np.sum(a * a) * np.sum(b * b))
        if den < 1e-6:
            return
        r = float(np.sum(a * b) / den)
        if r > 0.3 and np.std(self.v[-n:]) > 0.08:
            self.lpm = self.lpm_user
            self._start_image(free=True, t0=float(self.n - n - int(T)))

    # ---------------------------------------------------------------- image
    def _start_image(self, free, t0):
        if not free:
            pass
        else:
            self.T = self._spl(self.lpm_user)
        self.state = "image"
        self.free = free
        self.aligned = not free
        self.t0 = float(t0)
        self.k = 0
        self.rows = []
        self.pending = []
        self.slant_at = 32 if not free else 48
        self.T_ref = self.T
        self.start_v0 = self.v0
        title = f"Fac-similé IOC {self.ioc}, {self.lpm:.0f} l/min" + (" (roue libre)" if free else "")
        self.ev.append({"t": "img", "op": "new", "w": self.W, "h": 0, "fmt": "gray", "title": title})

    def _row(self, k):
        start = self.t0 + k * self.T - self.v0
        r = box_sample(self.v, start, self.T / self.W, self.W)
        return np.clip(np.round(r * 255), 0, 255).astype(np.uint8)

    def _lines(self):
        while self.state == "image":
            end = self.t0 + (self.k + 1) * self.T
            if end + 2 >= self.n:
                break
            if self.t0 + self.k * self.T < self.v0:     # ne devrait pas arriver (tampon purgé)
                self.k += 1
                continue
            self.rows.append(self._row(self.k))
            self.pending.append(len(self.rows) - 1)
            self.k += 1
            if len(self.rows) >= self.slant_at:
                self._slant()
                self.slant_at = len(self.rows) + 16
            if len(self.rows) >= self.max_lines:
                self._end("longueur maximale")

    def _slant_estimate(self, rows, rng=2.0):
        """Pente (pixels / ligne) qui rend le profil moyen des colonnes le plus net : les bords verticaux
        (cadre, marge, méridiens) ne se superposent exactement que pour la bonne pente.
        Cisaillement calculé dans le domaine de Fourier (une FFT par ligne)."""
        R = np.asarray(rows, np.float64)
        n = len(R)
        if n < 16:
            return None
        dec = max(1, self.W // 900)
        R = R[:, :R.shape[1] // dec * dec].reshape(n, -1, dec).mean(axis=2)
        R = R - R.mean(axis=1, keepdims=True)
        Wd = R.shape[1]
        F = np.fft.rfft(R, axis=1)[:, 1:Wd // 4]
        fr = np.arange(1, Wd // 4)
        kk = np.arange(n) - (n - 1) / 2

        def score(ds):
            # M[d, f] = moyenne_k F[k, f] exp(2πi f d k / Wd)
            ph = np.exp(2j * np.pi * np.outer(ds / dec, kk)[:, :, None] * fr[None, None, :] / Wd)
            M = np.einsum("dkf,kf->df", ph, F) / n
            return np.sum(np.abs(M) ** 2 * fr ** 2, axis=1)

        ds = np.linspace(-rng, rng, int(rng / 0.02) * 2 + 1)
        sc = score(ds)
        i = int(np.argmax(sc))
        if sc[i] < 1.05 * np.median(sc):
            return None
        d = ds[i]
        if 0 < i < len(ds) - 1:
            y0, y1, y2 = sc[i - 1], sc[i], sc[i + 1]
            den = y0 - 2 * y1 + y2
            if den < 0:
                d += 0.5 * (y0 - y2) / den * (ds[1] - ds[0])
        if abs(d) > rng * 0.98:
            return None
        return float(d)

    def _slant(self):
        if self.free and not self.aligned:
            self._free_align()
            return
        n = len(self.rows)
        rows = self.rows[max(0, n - 64):]
        d = self._slant_estimate(rows, rng=0.5)
        if d is None or abs(d) < 0.02:
            return
        Tn = self.T * (1 + 0.5 * d / self.W)
        # après un phasage, la durée de ligne est déjà connue à mieux que 1e-4 : retouches limitées
        lim = 3e-4 if not self.free else 0.01
        if abs(Tn / self.T_ref - 1) > lim:
            return
        kn = self.k
        self.t0 = self.t0 + kn * (self.T - Tn)
        self.T = Tn

    def _free_align(self):
        """Roue libre : pente sur les premières lignes (2 passes), puis début de ligne sur la marge."""
        for _ in range(3):
            rows = [self._row(k) for k in range(self.k)]
            d = self._slant_estimate(rows)
            if d is None:
                break
            Tn = self.T * (1 + d / self.W)
            if abs(Tn / self._spl(self.lpm_user) - 1) > 0.01:
                break
            self.T = Tn
            if abs(d) < 0.02:
                break
        self.T_ref = self.T
        rows = np.array([self._row(k) for k in range(self.k)], np.float64) / 255
        x0 = self._find_margin(rows)
        if x0 is not None:
            self.t0 += x0 / self.W * self.T
            if self.t0 + self.T * 0 < self.v0:
                self.t0 += self.T
        self.aligned = True
        nk = self.k
        # on recalcule et renvoie toute l'image déjà reçue
        self.k = 0
        last = self.t0 + nk * self.T
        self.rows = []
        while self.t0 + (self.k + 1) * self.T + 2 < self.n and self.t0 + (self.k + 1) * self.T <= last + self.T:
            self.rows.append(self._row(self.k))
            self.k += 1
        self.pending = list(range(len(self.rows)))

    def _find_margin(self, rows):
        W = self.W
        dark = (rows < 0.35).mean(axis=0)
        dark = np.convolve(np.concatenate([dark[-2:], dark, dark[:2]]), np.ones(3) / 3, mode="same")[2:-2]
        sd = rows.std(axis=0)
        cols = dark > 0.85
        if cols.any() and not cols.all():
            # amas de colonnes noires persistantes (cadre de la carte) ; marge = intervalle étroit le plus vide
            idx = np.nonzero(cols)[0]
            cl = [[idx[0], idx[0]]]
            for i in idx[1:]:
                if i - cl[-1][1] <= 3:
                    cl[-1][1] = i
                else:
                    cl.append([i, i])
            if len(cl) > 1 and cl[0][0] == 0 and cl[-1][1] == W - 1:
                cl[0][0] = cl[-1][0] - W
                cl.pop()
            best = None
            for j in range(len(cl)):
                a = cl[j][1] + 1
                b = cl[(j + 1) % len(cl)][0] + (W if j + 1 >= len(cl) else 0)
                g = b - a
                if 0 < g <= 0.15 * W:
                    gi = np.arange(a, b) % W
                    s = sd[gi].mean()
                    if best is None or s < best[0]:
                        best = (s, (a + b) / 2)
            if best is not None:
                return best[1] % W
            return (cl[0][0] - 0.01 * W) % W
        # sinon : bande uniforme (marge blanche) la plus calme
        w = max(3, int(0.03 * W))
        ext = np.concatenate([sd, sd[:w]])
        m = np.convolve(ext, np.ones(w) / w, mode="valid")[:W]
        i = int(np.argmin(m))
        if m[i] < 0.25 * np.median(sd):
            return (i + w / 2) % W
        return None

    def _flush(self):
        self.flush_count = 0
        if not self.pending:
            return
        p = sorted(set(self.pending))
        self.pending = []
        # lignes contiguës en un seul événement
        runs = [[p[0], p[0]]]
        for i in p[1:]:
            if i == runs[-1][1] + 1:
                runs[-1][1] = i
            else:
                runs.append([i, i])
        for a, b in runs:
            data = np.concatenate(self.rows[a:b + 1])
            self.ev.append({"t": "img", "op": "rows", "y": a, "n": b - a + 1, "data": b64(data)})

    def _end(self, why=""):
        if self.state == "image":
            if self.free and not self.aligned and self.rows:
                self._free_align()
            self._flush()
            self.ev.append({"t": "img", "op": "end"})
        self.state = "idle"
        self.free = False
        self.since_end = 0.0
        self.lpm = self.lpm_user
        self.T = self._spl(self.lpm)

    def _trim(self):
        keep = int(10 * self.fs2)
        if self.state == "image":
            if self.free and not self.aligned:
                keep = max(keep, int(self.n - self.t0 + self.T))
            else:
                keep = max(keep, int(self.n - (self.t0 + self.k * self.T)) + int(self.T))
        elif self.state == "phasing":
            keep = max(keep, int(self.n - min(self.scan_pos, self.n)) + int(self.fs2))
        if len(self.v) > keep + self.fs2:
            cut = len(self.v) - keep
            self.v = self.v[cut:]
            self.v0 += cut
        if len(self.fr) > 3 * self.fs2:
            self.fr = self.fr[-int(3 * self.fs2):]
