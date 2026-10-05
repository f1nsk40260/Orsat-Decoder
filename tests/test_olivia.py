"""Banc d'essai Olivia / Contestia : sensibilité, acquisition hors accord, dérive, bruit seul, vitesse.

    /tmp/claude-0/venv/bin/python tests/test_olivia.py            # sous-modes principaux
    /tmp/claude-0/venv/bin/python tests/test_olivia.py --tous     # tous les sous-modes
    /tmp/claude-0/venv/bin/python tests/test_olivia.py olivia:32/1000 contestia:8/250
"""
import sys
import time
from pathlib import Path

import numpy as np
from scipy.signal import hilbert

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from harness import run, score  # noqa: E402
from orsatdec.gen.encoders import add_noise  # noqa: E402
from orsatdec.gen.olivia import olivia_encode, contestia_encode, mode_params  # noqa: E402
from orsatdec.decoders.olivia import Olivia, Contestia  # noqa: E402

FS = 12000
AF = 1500.0
MSG = "CQ CQ DE F1NSK F1NSK JN03 PSE K 73"

PRINCIPAUX = [("olivia", 8, 250), ("olivia", 8, 500), ("olivia", 16, 500), ("olivia", 32, 1000),
              ("contestia", 8, 250), ("contestia", 16, 500)]
TOUS = ([("olivia", t, b) for t, b in ((4, 125), (4, 250), (8, 250), (8, 500), (16, 500), (16, 1000),
                                        (32, 1000), (4, 500), (8, 1000), (64, 2000))]
        + [("contestia", t, b) for t, b in ((4, 125), (4, 250), (8, 250), (8, 500), (16, 500), (16, 1000),
                                             (32, 1000))])


class Case:
    def __init__(self, fam, tones, bw):
        self.fam, self.tones, self.bw = fam, tones, bw
        self.cont = fam == "contestia"
        p = mode_params(tones, bw, self.cont)
        self.block = p["nsym"] * p["sep"] / 8000.0           # durée d'un bloc FEC (s)
        self.spacing = p["bw"] / p["tones"]

    def label(self):
        return f"{'Contestia' if self.cont else 'Olivia'} {self.tones}/{self.bw}"

    def signal(self, text=MSG, off=0.0, drift=0.0):
        enc = contestia_encode if self.cont else olivia_encode
        x = enc(text, fs=FS, af=AF + off, tones=self.tones, bw=self.bw)
        if drift:                                            # dérive linéaire (Hz/s)
            t = np.arange(len(x)) / FS
            x = np.real(hilbert(x) * np.exp(2j * np.pi * 0.5 * drift * t * t))
        # le décodeur attend les blocs suivants pour confirmer la synchro : silence final de ~3 blocs
        return np.concatenate([x, np.zeros(int(FS * (3 * self.block + 1)))])

    def decoder(self, **kw):
        return (Contestia if self.cont else Olivia)(FS, AF, tones=self.tones, bw=self.bw, **kw)

    def trial(self, snr, off=0.0, drift=0.0, seed=1, text=MSG):
        x = np.concatenate([np.zeros(FS // 2), self.signal(text, off, drift)])
        if snr is not None:
            x = add_noise(x, snr, FS, seed=seed)
        return score(text, run(self.decoder(), x, block=480))


def test_case(c, snrs=(-6, -10, -12, -13, -14, -15, -16), seeds=(1, 2)):
    print(f"\n{c.label()}  (bloc {c.block:.2f} s, espacement {c.spacing:.2f} Hz)")
    res = {}
    for snr in snrs:
        res[snr] = float(np.mean([c.trial(snr, seed=s) for s in seeds]))
    print("  S/B (2500 Hz) : " + "  ".join(f"{s:+d} dB {v:4.0%}" for s, v in res.items()))
    ok = [s for s, v in res.items() if v >= 0.9]
    print(f"  S/B mini (>= 90 %) : {min(ok):+d} dB" if ok else "  S/B mini : -")
    m = max(c.spacing, 30.0)
    offs = [-m, -0.6 * m, 0.4 * m, m]
    print("  hors accord (-8 dB) : " + "  ".join(f"{o:+.0f} Hz {c.trial(-8, off=o):4.0%}" for o in offs))
    print("  dérive (-8 dB) : " + "  ".join(f"{d:+.1f} Hz/s {c.trial(-8, drift=d):4.0%}" for d in (1.0, -1.0)))
    noise = np.random.default_rng(7).normal(0, 0.3, FS * 60)
    d = c.decoder()
    t0 = time.process_time()
    junk = run(d, noise, block=480)
    dt = time.process_time() - t0
    print(f"  bruit seul 60 s : {len(junk)} caractères {junk[:40]!r}")
    print(f"  vitesse : x{60 / dt:.0f} temps réel (temps CPU, fs = {FS} Hz)")
    return res


def selftest_cases():
    """Cas rapides pour l'autotest global : (libellé, signal, décodeur, texte attendu, S/B)."""
    msg = "CQ DE F1NSK K"
    out = []
    for fam, t, b, snr in (("olivia", 32, 1000, -12), ("contestia", 8, 250, -11)):
        c = Case(fam, t, b)
        out.append((c.label(), c.signal(text=msg), c.decoder(), msg, snr))
    return out


def main():
    args = sys.argv[1:]
    if "--tous" in args:
        cases = TOUS
    elif args:
        cases = []
        for a in args:
            fam, tb = a.split(":")
            t, b = tb.split("/")
            cases.append((fam.lower(), int(t), int(b)))
    else:
        cases = PRINCIPAUX
    for fam, t, b in cases:
        test_case(Case(fam, t, b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
