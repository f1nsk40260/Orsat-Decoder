"""Bancs des décodeurs du jalon 3 (modes utilitaires) sur mires générées.

    python3 tests/test_utility.py          # tous les cas, à plusieurs niveaux de bruit
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from harness import run, score  # noqa: E402
from orsatdec.gen.encoders import ascii_encode, add_noise  # noqa: E402
from orsatdec.gen import utility as U  # noqa: E402
from orsatdec.modes import BY_ID, default_params  # noqa: E402

FS = 12000


def make(mode, af, **p):
    m = BY_ID[mode]
    return m["make"](FS, af, {**default_params(m), **p})


def cases():
    """(libellé, signal, fabrique du décodeur, texte attendu, S/B de l'autotest)"""
    out = []
    t = "CQ CQ de F1NSK : ascii 0123456789"
    out.append(("ASCII 110/170", ascii_encode(t, af=1000), lambda: make("ascii", 1000), t, 0))
    t = "CQ CQ CQ DE FFL QSL 73 TEST SITOR ARQ 0123456789"
    out.append(("SITOR-A", U.sitora_encode(t, repeat=(5, 9), irs=0.5), lambda: make("sitora", 1000), t, 0))
    fr = U.ax25_bytes("F1NSK-9", "APRS", "!4903.50N/00201.75E>Orsat-Decoder", ["WIDE1-1"])
    t = "F1NSK-9>APRS,WIDE1-1: !4903.50N/00201.75E>Orsat-Decoder  (49.0583, 2.0292)\n"
    out.append(("Packet 1200", U.packet_encode([fr]), lambda: make("packet1200", 0), t, 12))
    out.append(("Packet 300", U.packet_encode([fr], baud=300.0, mark=1800.0, space=1600.0), lambda: make("packet300", 1700), t, 6))
    return out


def selftest_cases():
    return [(lab, sig, mk(), exp, snr) for lab, sig, mk, exp, snr in cases()]


def main():
    for lab, sig, mk, exp, _ in cases():
        x = np.concatenate([np.zeros(FS // 2), sig, np.zeros(FS // 2)])
        res = []
        for snr in (20, 10, 6, 3, 0, -3):
            res.append(f"{snr:>3} dB {score(exp, run(mk(), add_noise(x, snr, FS, seed=7))):4.0%}")
        print(f"{lab:<16}", "  ".join(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
