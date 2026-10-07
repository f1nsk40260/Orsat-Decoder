"""Banc d'essai des modes MFSK, DominoEX et THOR (bruit blanc, convention 2500 Hz).

Pour chaque sous-mode : taux de caractères corrects à plusieurs S/B, S/B le plus bas avec >= 90 %,
acquisition avec un clic décalé, dérive de 1 Hz/s, bruit seul (60 s) et vitesse (x temps réel, temps CPU).

    /tmp/claude-0/venv/bin/python tests/test_mfsk.py            # tous les sous-modes
    /tmp/claude-0/venv/bin/python tests/test_mfsk.py THOR       # filtre sur le nom
    /tmp/claude-0/venv/bin/python tests/test_mfsk.py --rapide   # balayage S/B seulement
"""
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from harness import run, score  # noqa: E402
from orsatdec.gen.encoders import add_noise  # noqa: E402
from orsatdec.gen.mfsk import mfsk_encode, dominoex_encode, thor_encode  # noqa: E402
from orsatdec.decoders.ifk_common import MFSK_MODES, DOMINO_MODES, THOR_MODES  # noqa: E402
from orsatdec.decoders.mfsk import MFSK  # noqa: E402
from orsatdec.decoders.dominoex import DominoEX  # noqa: E402
from orsatdec.decoders.thor import THOR  # noqa: E402

FS = 12000
AF = 1500.0
TEXT = "CQ CQ DE F1NSK F1NSK ORSAT TEST 0123456789"


class Case:
    """Un sous-mode : génération, décodeur, paramètres utiles au banc."""

    def __init__(self, family, mode, fec=False):
        self.family, self.mode, self.fec = family, mode, fec
        self.label = mode + (" FEC" if fec else "")
        if family == "MFSK":
            sr, symlen, symbits, depth, ntones, _ = MFSK_MODES[mode]
            self.baud, self.sp = sr / symlen, sr / symlen
            self.latency = 3 * depth + 30                      # symboles : entrelaceur + Viterbi
        elif family == "DominoEX":
            sr, symlen, ds = DOMINO_MODES[mode]
            self.baud, self.sp = sr / symlen, ds * sr / symlen
            self.latency = (40 if fec else 4) + 6
        else:
            sr, symlen, ds, depth, _, k15 = THOR_MODES[mode]
            self.baud, self.sp = sr / symlen, ds * sr / symlen
            self.latency = 3 * depth + (100 if k15 else 30)
        self.margin = max(2 * self.sp, 30.0)

    def signal(self, text=TEXT, af=AF, drift=0.0, fs=FS):
        if self.family == "MFSK":
            x = mfsk_encode(text, fs=fs, af=af, mode=self.mode, drift=drift)
        elif self.family == "DominoEX":
            x = dominoex_encode(text, fs=fs, af=af, mode=self.mode, fec=self.fec, secondary="ORSAT", drift=drift)
        else:
            x = thor_encode(text, fs=fs, af=af, mode=self.mode, secondary="ORSAT", drift=drift)
        # le décodeur n'affiche rien tant que son désentrelaceur et sa mesure de qualité ne sont pas remplis
        # (canal ouvert à l'instant) : on le laisse écouter un peu de bruit avant la mire
        d = self.decoder(fs=fs)
        inlv = getattr(d, "inlv", None)
        warm = inlv.n + inlv.warm + 2 if inlv is not None and (self.family != "DominoEX" or self.fec) else 0
        pre = np.zeros(int(fs * (1.0 + warm / self.baud)))
        post = np.zeros(int(fs * max(3.0, (self.latency + 10) / self.baud)))
        return np.concatenate([pre, x, post])

    def decoder(self, fs=FS, af=AF):
        if self.family == "MFSK":
            return MFSK(fs, af, mode=self.mode)
        if self.family == "DominoEX":
            return DominoEX(fs, af, mode=self.mode, fec=self.fec)
        return THOR(fs, af, mode=self.mode)

    def start_snr(self):
        """S/B de départ du balayage (au-dessus du seuil attendu)."""
        base = {"MFSK": -11, "DominoEX": -8, "THOR": -10}[self.family]
        return int(round(base + 10 * np.log10(self.baud / 15.625)))


CASES = ([Case("MFSK", m) for m in MFSK_MODES if not m.endswith("L")]
         + [Case("DominoEX", m) for m in DOMINO_MODES]
         + [Case("DominoEX", m, fec=True) for m in ("DominoEX 8", "DominoEX 11", "DominoEX 16", "DominoEX 22")]
         + [Case("THOR", m) for m in THOR_MODES])



def decode(case, x, af=AF):
    d = case.decoder(af=af)
    t = time.process_time()
    got = run(d, x, block=480)
    dt = time.process_time() - t
    return got, dt


def sweep(case, quick=False):
    sig = case.signal()
    res = []
    snr = case.start_snr()
    fails = 0
    lowest = None
    speed = None
    while fails < 2 and snr > case.start_snr() - 14:
        x = add_noise(sig, snr, FS, seed=snr + 100)
        got, dt = decode(case, x)
        sc = score(TEXT, got)
        if speed is None:
            speed = len(x) / FS / max(dt, 1e-9)
        res.append((snr, sc))
        if sc >= 0.9:
            lowest = snr
            fails = 0
        else:
            fails += 1
        snr -= 1
    return res, lowest, speed


def offset_test(case, snr):
    out = []
    for sgn in (-1, 1):
        off = sgn * 0.9 * case.margin
        x = add_noise(case.signal(af=AF + off), snr, FS, seed=7)
        got, _ = decode(case, x, af=AF)
        out.append((off, score(TEXT, got)))
    return out


def drift_test(case, snr, rate=1.0):
    x = add_noise(case.signal(drift=rate), snr, FS, seed=8)
    got, _ = decode(case, x)
    return score(TEXT, got)


def noise_test(case, seconds=60):
    rng = np.random.default_rng(42)
    x = rng.normal(0, 0.1, FS * seconds)
    got, dt = decode(case, x)
    return len(got.strip()), got


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    quick = "--rapide" in sys.argv
    cases = [c for c in CASES if not args or any(a.lower() in c.label.lower() for a in args)]
    total_ok = True
    summary = []
    for c in cases:
        res, lowest, speed = sweep(c, quick)
        line = " ".join(f"{s:+d}:{v:.0%}" for s, v in res)
        print(f"{c.label:<16} {line}")
        print(f"{'':<16} seuil 90 % : {lowest if lowest is not None else '?'} dB" + f"   vitesse x{speed:.0f}")
        row = [c.label, lowest, speed]
        if not quick and lowest is not None:
            snr = lowest + 4
            offs = offset_test(c, snr)
            rate = 1.0 if c.baud >= 7 else 0.25
            dr = drift_test(c, snr, rate)
            n, txt = noise_test(c)
            print(f"{'':<16} clic décalé ({snr} dB) : " + ", ".join(f"{o:+.0f} Hz {v:.0%}" for o, v in offs)
                  + f"   dérive {rate:g} Hz/s : {dr:.0%}   bruit seul 60 s : {n} car. {txt[:30]!r}")
            row += [offs, dr, n]
            total_ok &= all(v >= 0.9 for _, v in offs) and n <= 10
        total_ok &= lowest is not None
        summary.append(row)
        sys.stdout.flush()
    return 0 if total_ok else 1


def selftest_cases():
    """Cas rapides pour l'autotest global : (libellé, signal, décodeur, texte attendu, S/B)."""
    msg = "CQ DE F1NSK ORSAT 73"
    out = []
    for fam, mode, snr in (("MFSK", "MFSK32", -9), ("THOR", "THOR 22", -8), ("DominoEX", "DominoEX 22", -5)):
        c = Case(fam, mode)
        out.append((mode, c.signal(text=msg), c.decoder(), msg, snr))
    return out


if __name__ == "__main__":
    sys.exit(main())
