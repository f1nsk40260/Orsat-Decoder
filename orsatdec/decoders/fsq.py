"""FSQ et IFKP : 33 tonalités espacées de 8,8 Hz, codage incrémental (IFK) insensible à la dérive,
1,5 / 2 / 3 / 4,5 / 6 bauds. Une TFD de 4096 points toutes les 256 échantillons (12 kHz) ; une tonalité
nouvelle, stable sur quelques trames, est un symbole ; l'écart avec la précédente donne un quartet ;
Varicode FSQ à un ou deux quartets. Les messages dirigés (« indicatif:… ») s'affichent tels quels."""
import numpy as np

from ..dsp import Decoder

FSQ_SINGLE = [32, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116,
              117, 118, 119, 120, 121, 122, 46, 10]
FSQ_DOUBLE = [[64, 126, 61], [65, 49, 91], [66, 50, 92], [67, 51, 93], [68, 52, 94], [69, 53, 95], [70, 54, 123],
              [71, 55, 124], [72, 56, 125], [73, 57, 96], [74, 48, 177], [75, 33, 247], [76, 34, 176], [77, 35, 215],
              [78, 36, 163], [79, 37, -1], [80, 38, -1], [81, 39, -1], [82, 40, -1], [83, 41, -1], [84, 42, -1],
              [85, 43, -1], [86, 45, -1], [87, 47, -1], [88, 58, -1], [89, 59, -1], [90, 60, -1], [44, 62, 8],
              [63, 0, 127]]
IFKP_SINGLE = [0] + FSQ_SINGLE[1:28] + [32]
IFKP_DOUBLE = [r[:] for r in FSQ_DOUBLE]
IFKP_DOUBLE[28] = [63, 10, 127]
# variante : (fréquence d'échantillonnage de référence, nombre de cases examinées, tables)
VARIANTS = {"fsq": (12000.0, 144, FSQ_SINGLE, FSQ_DOUBLE), "ifkp": (16000.0, 151, IFKP_SINGLE, IFKP_DOUBLE)}
NFFT = 4096                          # FSQ à 12 kHz : 2,93 Hz par case ; IFKP à 16 kHz : 3,9 Hz ; écart 3 cases
SHIFT = 256
SR = 12000.0
SYMLEN = {1.5: 8192, 2.0: 6144, 3.0: 4096, 4.5: 3072, 6.0: 2048}


def nibble_of(diff_bins):
    n = int(np.floor(0.5 + diff_bins / 3.0))
    n %= 33
    return n - 1                      # l'émetteur ajoute 1 : -1 = même tonalité (pas de symbole)


class FSQ(Decoder):
    name = "FSQ"

    def __init__(self, fs, af=1500.0, baud=3.0, hits=3, movavg=4, variant="fsq"):
        super().__init__(fs, af)
        from scipy.signal import resample_poly
        from fractions import Fraction
        self.SR, self.nbins, self.single, self.double = VARIANTS[variant]
        if variant == "ifkp":
            self.name, hits = "IFKP", 4
        fr = Fraction(int(self.SR), int(round(self.fs)))
        self.up, self.down = fr.numerator, fr.denominator
        self.resample = resample_poly if (self.up, self.down) != (1, 1) else None
        self.buf = np.zeros(0)
        self.win = np.blackman(NFFT)
        self.hits = hits
        self.avg = []
        self.movavg = movavg
        self.prev_peak = self.last_peak = None
        self.count = 0
        self.prev_sym = None
        self.prev_nib = 0
        self.metric = 0.0
        self.af0 = float(af)

    def set_af(self, af):
        super().set_af(af)
        self.af0 = float(af)

    def status(self):
        return {"af": round(self.af0, 1), "snr": round(10 * np.log10(max(self.metric, 1e-3)), 1)}

    def process(self, x):
        x = np.asarray(x, np.float64)
        if self.resample is not None:
            x = self.resample(x, self.up, self.down)
        self.buf = np.concatenate([self.buf, x])
        out = []
        first = int(self.af0 * NFFT / self.SR) - self.nbins // 2
        while len(self.buf) >= NFFT:
            sp = np.abs(np.fft.rfft(self.buf[:NFFT] * self.win)) ** 2
            self.buf = self.buf[SHIFT:]
            band = sp[max(0, first):first + self.nbins]
            self.avg.append(band)
            if len(self.avg) > self.movavg:
                del self.avg[0]
            tones = np.mean(self.avg, axis=0)
            peak = int(np.argmax(tones))
            s3 = tones[max(0, peak - 1):peak + 2].sum()
            mn = np.convolve(tones, np.ones(3), mode="valid").min() + 1e-20
            self.metric += 0.1 * (s3 / mn - self.metric)
            if peak == self.prev_peak:
                self.count += 1
            else:
                self.count = 0
            if self.count >= self.hits and peak != self.last_peak and self.metric > 110:
                self._symbol(peak, out)
                self.count = 0
                self.last_peak = peak
            self.prev_peak = peak
        txt = "".join(out)
        return [{"t": "text", "text": txt}] if txt else []

    def _symbol(self, sym, out):
        if self.prev_sym is None:
            self.prev_sym = sym
            return
        nib = nibble_of(sym - self.prev_sym)
        self.prev_sym = sym
        if nib < 0:
            return
        ch = -1
        if self.prev_nib < 29 and nib < 29:
            ch = self.single[self.prev_nib]
        elif self.prev_nib < 29 and 29 <= nib <= 31:
            ch = self.double[self.prev_nib][nib - 29]
        self.prev_nib = nib
        if ch > 0:
            if ch in (10, 13):
                out.append("\n")
            elif ch == 8:
                out.append("\b")
            elif 32 <= ch < 127 or ch in (163, 176, 177, 215, 247):
                out.append(chr(ch))
