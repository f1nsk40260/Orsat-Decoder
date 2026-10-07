"""SSTV : Martin M1/M2, Scottie S1/S2/DX, Robot 36/72, PD 50/90/120/160/180/240/290, Wraase SC2-180.

Démodulation FM (1500 Hz noir – 2300 Hz blanc, synchro 1200 Hz), détection du code VIS (1900 Hz
pendant 300 ms, coupure 1200 Hz, 1900 Hz, bit de départ 1200 Hz, 7 bits de 30 ms LSB d'abord
— 1100 Hz = 1, 1300 Hz = 0 —, parité paire, bit d'arrêt). Le palier de 1900 Hz donne le décalage
d'accord (clic à ±100 Hz près).
Chronologie : chaque impulsion de synchro est repérée (filtre adapté) autour de sa position prévue ;
une droite des moindres carrés (avec rejet des valeurs aberrantes) donne la durée exacte de ligne
— correction de pente — et l'origine. Les lignes sont échantillonnées sur ce modèle ; quand il
s'affine, les lignes déjà envoyées sont recalculées et renvoyées si elles ont changé.
Couleur : RVB direct (Martin, Scottie, Wraase), YCbCr pleine échelle (JFIF) pour Robot et PD.
"""
import numpy as np

from ..dsp import Decoder
from .wefax import FMDemod, box_sample, b64, Buf


def _m(name, vis, fam, w, h, **kw):
    d = {"name": name, "vis": vis, "fam": fam, "w": w, "h": h}
    d.update(kw)
    return d


SSTV_MODES = {
    "martin1": _m("Martin M1", 44, "martin", 320, 256, sync=4.862, gap=0.572, scan=146.432),
    "martin2": _m("Martin M2", 40, "martin", 320, 256, sync=4.862, gap=0.572, scan=73.216),
    "scottie1": _m("Scottie S1", 60, "scottie", 320, 256, sync=9.0, scan=138.24),
    "scottie2": _m("Scottie S2", 56, "scottie", 320, 256, sync=9.0, scan=88.064),
    "scottiedx": _m("Scottie DX", 76, "scottie", 320, 256, sync=9.0, scan=345.6),
    "robot36": _m("Robot 36", 8, "robot", 320, 240, sync=9.0, yscan=88.0, cscan=44.0, scan=88.0),
    "robot72": _m("Robot 72", 12, "robot", 320, 240, sync=9.0, yscan=138.0, cscan=69.0, scan=138.0),
    "pd50": _m("PD 50", 93, "pd", 320, 256, sync=20.0, scan=91.52),
    "pd90": _m("PD 90", 99, "pd", 320, 256, sync=20.0, scan=170.24),
    "pd120": _m("PD 120", 95, "pd", 640, 496, sync=20.0, scan=121.6),
    "pd160": _m("PD 160", 98, "pd", 512, 400, sync=20.0, scan=195.584),
    "pd180": _m("PD 180", 96, "pd", 640, 496, sync=20.0, scan=183.04),
    "pd240": _m("PD 240", 97, "pd", 640, 496, sync=20.0, scan=244.48),
    "pd290": _m("PD 290", 94, "pd", 800, 616, sync=20.0, scan=228.8),
    "sc2_180": _m("Wraase SC2-180", 55, "wraase", 320, 256, sync=5.5225, porch=0.5, scan=235.0),
}
BY_VIS = {m["vis"]: k for k, m in SSTV_MODES.items()}


def layout(m):
    """Organisation temporelle d'une « unité » (ligne, ou paire de lignes en PD), relative au début
    de l'impulsion de synchro : (période ms, rangées par unité, [(début ms, durée ms, canal, rangée)],
    décalage de la 1re synchro après la fin du VIS)."""
    fam, c = m["fam"], m["scan"]
    if fam == "martin":
        s, g = m["sync"], m["gap"]
        ch = [(s + g, c, "G", 0), (s + 2 * g + c, c, "B", 0), (s + 3 * g + 2 * c, c, "R", 0)]
        return s + 4 * g + 3 * c, 1, ch, 0.0
    if fam == "scottie":
        ch = [(-(2 * c + 1.5), c, "G", 0), (-c, c, "B", 0), (10.5, c, "R", 0)]
        return 13.5 + 3 * c, 1, ch, 9.0 + 3.0 + 2 * c
    if fam == "wraase":
        s, p = m["sync"], m["porch"]
        ch = [(s + p, c, "R", 0), (s + p + c, c, "G", 0), (s + p + 2 * c, c, "B", 0)]
        return s + p + 3 * c, 1, ch, 0.0
    if fam == "robot":
        y, cc = m["yscan"], m["cscan"]
        if m["vis"] == 8:              # Robot 36 : chrominance alternée R-Y / B-Y
            ch = [(12.0, y, "Y", 0), (12.0 + y + 6.0, cc, "C", 0)]
            return 12.0 + y + 6.0 + cc, 1, ch, 0.0
        ch = [(12.0, y, "Y", 0), (12.0 + y + 6.0, cc, "V", 0), (12.0 + y + 12.0 + cc, cc, "U", 0)]
        return 12.0 + y + 12.0 + 2 * cc, 1, ch, 0.0
    if fam == "pd":
        o = 22.08
        ch = [(o, c, "Y", 0), (o + c, c, "V", 0), (o + 2 * c, c, "U", 0), (o + 3 * c, c, "Y", 1)]
        return o + 4 * c, 2, ch, 0.0
    raise ValueError(fam)


def rgb_to_ycc(img):
    img = np.asarray(img, np.float64)
    R, G, B = img[..., 0], img[..., 1], img[..., 2]
    Y = 0.299 * R + 0.587 * G + 0.114 * B
    Cb = 128 - 0.168736 * R - 0.331264 * G + 0.5 * B
    Cr = 128 + 0.5 * R - 0.418688 * G - 0.081312 * B
    return np.stack([Y, Cb, Cr], -1)


def ycc_to_rgb(Y, Cb, Cr):
    R = Y + 1.402 * (Cr - 128)
    G = Y - 0.344136 * (Cb - 128) - 0.714136 * (Cr - 128)
    B = Y + 1.772 * (Cb - 128)
    return np.clip(np.round(np.stack([R, G, B], -1)), 0, 255).astype(np.uint8)


class SSTV(Decoder):
    name = "SSTV"
    kind = "img"

    def __init__(self, fs, af=1900.0, mode="auto", timeout=4.0, span=100.0):
        super().__init__(fs, af)
        self.force = None if mode in (None, "auto") else mode
        self.timeout = float(timeout)
        self.span = float(span)
        self.af0 = float(af)
        # bande utile 1100–2300 Hz (centre 1700) : on se place au milieu, ±(600 + marge)
        self.dem = FMDemod(fs, af - 200.0, 600 + self.span + 150)
        self.fs2 = self.dem.fs2
        self.ms = self.fs2 / 1000.0
        self.off = 0.0
        self._fn = Buf(np.float32)            # fréquence « nominale » (1900 = palier VIS)
        self._zb = Buf(np.complex64)          # bande de base complexe (détection du VIS par énergie)
        self._fcb = Buf(np.float32)           # fréquence nominale correspondant au 0 Hz de zb
        self.fn, self.zb, self.fcb = self._fn.v, self._zb.v, self._fcb.v
        self.f0 = 0                          # indice absolu de fn[0]
        self.n = 0
        self.scan_pos = 0
        self.state = "idle"
        self.mode = None
        self.snr = 0.0
        self.raw = np.zeros(0)
        self.raw_n = 0
        self.present = 0.0                   # secondes depuis la dernière présence de signal
        self.ev = []

    def _presence(self):
        """Densité spectrale dans 1100–2300 Hz comparée aux bandes 300–900 et 2600–3200 Hz."""
        raw = self.raw
        if len(raw) < self.fs / 4:
            return
        sp = np.abs(np.fft.rfft(raw * np.hanning(len(raw)))) ** 2
        fq = np.fft.rfftfreq(len(raw), 1 / self.fs) - (self.af0 + self.off - 1900)
        ib = (fq > 1150) & (fq < 2300)
        ob = ((fq > 400) & (fq < 900)) | ((fq > 2600) & (fq < 3100))
        ob &= (fq + (self.af0 + self.off - 1900) < self.fs / 2 - 100)
        if not ib.any() or not ob.any():
            return
        ratio = np.mean(sp[ib]) / (np.mean(sp[ob]) + 1e-20)
        self.snr = float(10 * np.log10(max(ratio - 1, 1e-3) * 1150 / 2500))
        self.present = 0.0 if ratio > 1.3 else self.present + len(raw) / self.fs

    def set_af(self, af):
        super().set_af(af)
        self.af0 = float(af)
        self.dem.fc = af - 200.0

    def status(self):
        st = {"af": round(float(self.af0 + self.off), 1), "snr": round(float(self.snr), 1),
              "sync": 1 if self.state == "rx" else 0,
              "mode": SSTV_MODES[self.mode]["name"] if self.mode else "attente VIS"}
        if self.state == "rx":
            st["ligne"] = int(self.rendered_rows)
        return st

    # ---------------------------------------------------------------- flux
    def process(self, x):
        self.ev = []
        x = np.asarray(x, np.float64)
        self.raw = np.concatenate([self.raw, x])[-int(self.fs / 2):]
        self.raw_n += len(x)
        if self.raw_n >= self.fs / 2:
            self.raw_n = 0
            self._presence()
        f, z = self.dem.process(x)
        if len(f) == 0:
            return []
        fn = f + np.float32(self.dem.fc - (self.af0 - 1900.0))
        self._fn.append(fn)
        self._zb.append(z)
        self._fcb.append(np.full(len(z), self.dem.fc - (self.af0 - 1900.0), np.float32))
        self.fn, self.zb, self.fcb = self._fn.v, self._zb.v, self._fcb.v
        self.n += len(fn)
        self._search_vis()
        if self.state == "rx":
            self._rx()
        self._trim()
        return self.ev

    def _seg(self, a, b):
        """fn entre les indices absolus a et b (bornés au tampon)."""
        a = max(a, self.f0)
        b = min(b, self.n)
        return self.fn[a - self.f0:b - self.f0]

    def _trim(self):
        if self.state == "rx":
            keep_from = self.t_start - int(2 * self.ms * 300)
        else:
            keep_from = self.n - int(1.5 * self.fs2)
        cut = keep_from - self.f0
        if cut > self.fs2:
            for b in (self._fn, self._zb, self._fcb):
                b.drop(cut)
            self.fn, self.zb, self.fcb = self._fn.v, self._zb.v, self._fcb.v
            self.f0 += cut

    # ---------------------------------------------------------------- VIS
    def _tone(self, a, b, F, box=None):
        """Énergie de la tonalité nominale F sur [a, b) (indices absolus) ; box : énergies par cases."""
        z = self.zb[a - self.f0:b - self.f0]
        fc = self.fcb[a - self.f0:b - self.f0]
        t = np.arange(a, a + len(z)) / self.fs2
        w = z * np.exp(-2j * np.pi * (F - fc) * t)
        if box is None:
            return float(np.abs(np.sum(w)) ** 2 / max(1, len(w)))
        nb = len(w) // box
        return np.abs(w[:nb * box].reshape(nb, box).sum(axis=1)) ** 2 / box

    def _search_vis(self):
        """Repérage par énergie (cases de 5 ms) : palier à 1900 Hz suivi du bit de départ à 1200 Hz ;
        puis vérification complète (décalage par FFT du palier, bits par énergie à 1100/1200/1300 Hz)."""
        if self.n - getattr(self, "_vis_last", -10 ** 9) < 0.25 * self.fs2:
            return
        self._vis_last = self.n
        B = max(1, int(round(5 * self.ms)))
        a = max(self.scan_pos, self.f0)
        a0 = max(a - int(0.3 * self.fs2), self.f0)
        nb = (self.n - a0) // B
        if nb < 120:
            return
        e19 = self._tone(a0, a0 + nb * B, 1900.0, B)
        e12 = self._tone(a0, a0 + nb * B, 1200.0, B)
        r = e19 / (e19 + e12 + 1e-12)
        C = np.concatenate([[0], np.cumsum(r)])
        found = None
        for j in range(56, nb - 62):
            if a0 + j * B < a:
                continue
            lead = (C[j - 2] - C[j - 52]) / 50
            if lead < 0.65:
                continue
            st = (C[j + 5] - C[j + 1]) / 4
            if st > 0.35:
                continue
            res = self._vis_at(a0 + j * B, B)
            if res is not None:
                found = res
                break
        self.scan_pos = max(self.scan_pos, a0 + max(0, nb - 62) * B)
        if found is not None:
            mode, t_end, off = found
            if self.state == "rx":
                self._end()
            self._start(mode, t_end, off)

    def _vis_at(self, t, B):
        la, lb = t - int(280 * self.ms), t - int(15 * self.ms)
        if la < self.f0:
            return None
        # décalage : pic de la FFT du palier (ramené en fréquence nominale)
        z = self.zb[la - self.f0:lb - self.f0] * np.hanning(lb - la)
        N = 1 << 14
        sp = np.abs(np.fft.fft(z, N)) ** 2
        fq = np.fft.fftfreq(N, 1 / self.fs2) + float(self.fcb[la - self.f0])
        sel = np.abs(fq - 1900) < self.span + 30
        if not sel.any():
            return None
        i = int(np.argmax(np.where(sel, sp, 0)))
        if sp[i] < 6 * np.median(sp[sel]):
            return None
        y0, y1, y2 = sp[i - 1], sp[i], sp[(i + 1) % N]
        den = y0 - 2 * y1 + y2
        off = fq[i] - 1900 + (0.5 * (y0 - y2) / den * self.fs2 / N if den < 0 else 0)
        # front précis du bit de départ : énergie 1200 contre 1900 par cases de 1 ms
        b1 = max(1, int(self.ms))
        w0 = t - 3 * B
        e1 = self._tone(w0, t + 3 * B, 1200 + off, b1)
        e2 = self._tone(w0, t + 3 * B, 1900 + off, b1)
        k = np.nonzero(e1 > e2)[0]
        if len(k) == 0:
            return None
        # premier point d'une suite durable de 1200 Hz
        ts = None
        for kk in k:
            if np.mean(e1[kk:kk + 4] > e2[kk:kk + 4]) >= 0.75:
                ts = w0 + kk * b1 + b1 // 2
                break
        if ts is None:
            return None
        bits = []
        for i in range(10):
            sa, sb = ts + int((i * 30 + 3) * self.ms), ts + int((i * 30 + 27) * self.ms)
            if sb > self.n:
                return None
            e = [self._tone(sa, sb, F + off) for F in (1100.0, 1200.0, 1300.0)]
            bits.append(e)
        for i in (0, 9):
            if not (bits[i][1] > bits[i][0] and bits[i][1] > bits[i][2]):
                return None
        code, par = 0, 0
        for i in range(8):
            e = bits[1 + i]
            b = 1 if e[0] > e[2] else 0
            if max(e[0], e[2]) < 2 * e[1]:
                return None
            if i < 7:
                code |= b << i
            par ^= b
        if par != 0:
            return None
        mode = BY_VIS.get(code)
        if mode is None:
            return None
        if self.force and mode != self.force:
            return None
        return mode, ts + 300 * self.ms, float(off)

    # ---------------------------------------------------------------- réception
    def _start(self, mode, t_end, off):
        m = SSTV_MODES[mode]
        self.mode = mode
        self.m = m
        self.off = off
        self.P, self.rpu, self.chans, first = layout(m)
        self.units = m["h"] // self.rpu
        self.Pn = self.P * self.ms                    # période nominale en échantillons
        self.a = t_end + first * self.ms              # synchro de l'unité 0
        self.b = self.Pn
        self.t_start = int(t_end)
        self.syncs = {}                               # unité -> (instant mesuré, qualité)
        self.next_sync = 0
        self.rows = np.zeros((m["h"], m["w"], 3), np.uint8)
        self.rendered = {}                            # unité -> modèle (a, b) utilisé
        self.next_render = 0
        self.rendered_rows = 0
        self.pending = set()
        self.last_good = 0
        self.flush_at = self.n
        self.refit_at = 16
        self.state = "rx"
        self.scan_pos = max(self.scan_pos, int(t_end))
        self.ev.append({"t": "img", "op": "new", "w": m["w"], "h": m["h"], "fmt": "rgb", "title": m["name"]})

    def _pred(self, u):
        return self.a + self.b * u

    def _find_sync(self, u):
        Ls = int(round(self.m["sync"] * self.ms * self.b / self.Pn))
        tp = self._pred(u)
        nfit = sum(1 for v in self.syncs.values() if v[1] > 0)
        win = (20.0 if nfit < 4 else 6.0) * self.ms
        a, b = int(tp - win - Ls), int(tp + win + 2 * Ls)
        seg = self._seg(a, b)
        if len(seg) < b - a:
            return None
        s = np.clip((1450.0 - (seg - self.off)) / 200.0, 0, 1)
        h = max(1, Ls // 2)
        C = np.concatenate([[0], np.cumsum(s)])
        i = np.arange(h, len(s) - Ls - h)
        inside = C[i + Ls] - C[i]
        outside = (C[i] - C[i - h]) + (C[i + Ls + h] - C[i + Ls])
        score = inside / Ls - outside / (2 * h)
        j = int(np.argmax(score))
        q = float(score[j])
        t = a + i[j]
        # fréquence de la synchro : affine le décalage d'accord
        if q > 0.6:
            sy = seg[i[j] + Ls // 4:i[j] + 3 * Ls // 4]
            if len(sy):
                e = float(np.median(sy)) - 1200.0 - self.off
                if abs(e) < 60:
                    self.off += 0.05 * e
        return t, q

    def _fit(self):
        pts = [(u, t) for u, (t, q) in self.syncs.items() if q > 0.45]
        if len(pts) < 3:
            if pts:
                r = np.median([t - self.b * u for u, t in pts])
                if abs(r - self.a) < 25 * self.ms:
                    self.a = float(r)
            return
        u = np.array([p[0] for p in pts], np.float64)
        t = np.array([p[1] for p in pts], np.float64)
        keep = np.ones(len(u), bool)
        a, b = self.a, self.b
        for _ in range(4):
            if keep.sum() < 3:
                break
            if np.ptp(u[keep]) >= 4:
                A = np.vstack([u[keep], np.ones(keep.sum())]).T
                b_, a_ = np.linalg.lstsq(A, t[keep], rcond=None)[0]
                if abs(b_ / self.Pn - 1) > 0.015:          # pente invraisemblable : on garde la nominale
                    b_ = self.b
                    a_ = float(np.median(t[keep] - b_ * u[keep]))
            else:
                b_ = self.b
                a_ = float(np.median(t[keep] - b_ * u[keep]))
            a, b = a_, b_
            res = np.abs(t - (a + b * u))
            nk = res < max(2.0 * self.ms, 3 * np.median(res[keep]) + 0.3 * self.ms)
            if (nk == keep).all():
                break
            keep = nk
        self.a, self.b = float(a), float(b)

    def _rx(self):
        # 1) synchros disponibles
        Ls = self.m["sync"] * self.ms
        while self.next_sync < self.units and self._pred(self.next_sync) + 30 * self.ms + 2 * Ls < self.n:
            r = self._find_sync(self.next_sync)
            if r is None:
                break
            self.syncs[self.next_sync] = r
            if r[1] > 0.45:
                self.last_good = self.next_sync
            self.next_sync += 1
            if self.next_sync <= 8 or self.next_sync % 4 == 0:
                self._fit()
        # 2) lignes complètes (on attend deux synchros de plus pour un modèle stable)
        while self.next_render < self.units:
            u = self.next_render
            if self.next_sync < min(self.units, u + 3):
                break
            if self._unit_end(u) + 2 > self.n:
                break
            if self.m["vis"] == 8 and u % 2 == 0:
                if self._unit_end(u + 1) + 2 > self.n and u + 1 < self.units:
                    break
                self._render(u)
                self._render(u + 1)
                self.next_render += 2
            else:
                self._render(u)
                self.next_render += 1
        # 3) modèle affiné : on recalcule les lignes déjà envoyées
        if self.next_render >= self.refit_at:
            self.refit_at = self.next_render + 16
            self._rerender()
        # 4) fin
        done = self.next_render >= self.units
        lost_units = self.next_sync - 1 - self.last_good
        lost = lost_units * self.P / 1000.0 >= self.timeout and lost_units >= 4 and self.present >= self.timeout
        overdue = self.n > self._pred(self.units) + (self.P + 2000) * self.ms
        if self.n - self.flush_at > self.fs2 / 2:
            self._flush()
        if done or lost or overdue:
            self._end()

    def _unit_end(self, u):
        last = max(c[0] + c[1] for c in self.chans)
        return self._pred(u) + last * self.ms * self.b / self.Pn

    def _chan(self, u, start, dur):
        k = self.b / self.Pn
        W = self.m["w"]
        t = self._pred(u) + start * self.ms * k - self.f0
        step = dur * self.ms * k / W
        a = int(max(0, t - 2))
        b = int(min(len(self.fn), t + step * W + 3))
        seg = np.clip(self.fn[a:b] - self.off, 1500, 2300)
        v = box_sample(seg, t - a, step, W)
        if start + dur >= self.P - 1.0:
            # canal collé à la synchro suivante : le filtre étale le saut à 1200 Hz sur ~0,5 ms,
            # les derniers points sont remplacés par le dernier point sûr
            k = min(W // 4, int(np.ceil(0.5 / (dur / W))) - int((self.P - start - dur) / (dur / W)))
            if k > 0:
                v[-k:] = v[-k - 1]
        return np.clip((v - 1500.0) / 800.0 * 255.0, 0, 255)

    def _render(self, u, thr=0.0):
        if u >= self.units:
            return
        m = self.m
        ch = {}
        for (st, du, name, r) in self.chans:
            ch[(name, r)] = self._chan(u, st, du)
        fam = m["fam"]
        out = {}
        if fam in ("martin", "scottie", "wraase"):
            out[u] = np.stack([ch[("R", 0)], ch[("G", 0)], ch[("B", 0)]], -1)
            out[u] = np.clip(np.round(out[u]), 0, 255).astype(np.uint8)
        elif fam == "pd":
            V, U = ch[("V", 0)], ch[("U", 0)]
            out[2 * u] = ycc_to_rgb(ch[("Y", 0)], U, V)
            out[2 * u + 1] = ycc_to_rgb(ch[("Y", 1)], U, V)
        elif m["vis"] == 8:
            # Robot 36 : ligne paire = R-Y, impaire = B-Y ; chaque paire partage les deux
            self._r36 = getattr(self, "_r36", {})
            self._r36[u] = (ch[("Y", 0)], ch[("C", 0)])
            if u % 2 == 1 and (u - 1) in self._r36:
                Y0, V = self._r36[u - 1]
                Y1, U = self._r36[u]
                out[u - 1] = ycc_to_rgb(Y0, U, V)
                out[u] = ycc_to_rgb(Y1, U, V)
        else:
            out[u] = ycc_to_rgb(ch[("Y", 0)], ch[("U", 0)], ch[("V", 0)])
        self.rendered[u] = (self.a, self.b)
        for y, row in out.items():
            if y < m["h"]:
                if thr > 0 and np.mean(np.abs(self.rows[y].astype(np.int16) - row)) < thr:
                    continue
                if not np.array_equal(self.rows[y], row):
                    self.rows[y] = row
                    self.pending.add(y)
                self.rendered_rows = max(self.rendered_rows, y + 1)

    def _rerender(self):
        """Relit les unités déjà rendues avec le modèle courant (seulement si elles ont bougé)."""
        if not self.rendered:
            return
        W = self.m["w"]
        for u, (a, b) in list(self.rendered.items()):
            moved = abs((a + b * u) - self._pred(u)) + abs(b / self.Pn - self.b / self.Pn) * self.m['scan'] * 3 * self.ms
            px = moved / (self.m["scan"] * self.ms / W)
            if px > 0.3 and self.f0 <= self._pred(u) + min(c[0] for c in self.chans) * self.ms - 2:
                if self.m["vis"] == 8:
                    self._render(u - (u % 2), thr=2.0)
                    self._render(u - (u % 2) + 1, thr=2.0)
                else:
                    self._render(u, thr=2.0)

    def _flush(self):
        self.flush_at = self.n
        if not self.pending:
            return
        p = sorted(self.pending)
        self.pending = set()
        runs = [[p[0], p[0]]]
        for i in p[1:]:
            if i == runs[-1][1] + 1:
                runs[-1][1] = i
            else:
                runs.append([i, i])
        for a, b in runs:
            self.ev.append({"t": "img", "op": "rows", "y": a, "n": b - a + 1, "data": b64(self.rows[a:b + 1])})

    def _end(self):
        if self.state != "rx":
            return
        self._fit()
        self._rerender()
        self._flush()
        self.ev.append({"t": "img", "op": "end"})
        self.state = "idle"
        self.mode = None
        self._r36 = {}
