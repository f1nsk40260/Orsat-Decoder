"""Banc d'essai MT63 (500/1000/2000 Hz, entrelacement court/long ; bruit blanc, convention 2500 Hz).

Pour chaque sous-mode : taux de caractères corrects à plusieurs S/B, S/B le plus bas avec >= 90 %,
acquisition avec un clic décalé, dérive lente, bruit seul (60 s) et vitesse (x temps réel).

    /tmp/claude-0/venv/bin/python tests/test_mt63.py            # tout
    /tmp/claude-0/venv/bin/python tests/test_mt63.py 1000       # filtre sur le nom du sous-mode
    /tmp/claude-0/venv/bin/python tests/test_mt63.py --rapide   # balayage S/B seulement
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
from orsatdec.gen.mt63 import mt63_encode  # noqa: E402
from orsatdec.decoders.mt63 import MT63  # noqa: E402

FS = 12000
TEXT = ("CQ CQ CQ DE F1NSK F1NSK PSE K. THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG 0123456789. "
        "NOW IS THE TIME FOR ALL GOOD MEN TO COME TO THE AID OF THE PARTY, 73 ES GL.")
SUBMODES = [(bw, il) for bw in (500, 1000, 2000) for il in ("court", "long")]
SNRS = {500: [-10, -11, -12, -13], 1000: [-7, -8, -9, -10], 2000: [-4, -5, -6, -7]}
SEEDS = (1, 2, 3)


def name(bw, il):
    return f"MT63-{bw}{'L' if il == 'long' else 'S'}"


def tail(bw):
    """Silence final : laisse sortir les caractères encore dans les tuyaux (~5 s en 1000 Hz)."""
    return np.zeros(int(FS * 7 * 1000 / bw))


def signal(bw, il, text=TEXT, af=None, tones=0.0):
    af0 = 500 + bw / 2
    x = mt63_encode(text, FS, af if af is not None else af0, bw, il == "long", tones=tones)
    return np.concatenate([np.zeros(FS // 2), x, tail(bw)])


def decode(bw, il, x, af=None, **kw):
    d = MT63(FS, af if af is not None else 500 + bw / 2, bw, il, **kw)
    return run(d, x, block=480), d


def sweep(bw, il):
    x0 = signal(bw, il)
    res = {}
    for snr in SNRS[bw]:
        res[snr] = float(np.mean([score(TEXT, decode(bw, il, add_noise(x0, snr, FS, seed=s))[0]) for s in SEEDS]))
    ok = [s for s, v in res.items() if v >= 0.9]
    low = min(ok) if ok else None
    print(f"  {name(bw, il):<11} " + "  ".join(f"{s:>4} dB {v:4.0%}" for s, v in res.items())
          + f"   -> >=90 % jusqu'à {low} dB")
    return low


def offset_test(bw, il):
    sp = bw / 64                       # écart entre porteuses
    out = []
    for off in (-2.3 * sp, -1.2 * sp, 1.6 * sp, 3.1 * sp):
        x = add_noise(signal(bw, il, af=500 + bw / 2 + off), {500: -9, 1000: -6, 2000: -3}[bw], FS, seed=4)
        txt, d = decode(bw, il, x)
        out.append(f"{off:+.0f} Hz {score(TEXT, txt):4.0%}")
    print(f"  {name(bw, il):<11} clic décalé : " + "  ".join(out))


def drift_test(bw, il, rate):
    x = signal(bw, il)
    t = np.arange(len(x)) / FS
    f0 = 500 + bw / 2
    x = np.real(hilbert(x) * np.exp(2j * np.pi * 0.5 * rate * t ** 2))
    snr = {500: -9, 1000: -6, 2000: -3}[bw]
    sc = [score(TEXT, decode(bw, il, add_noise(x, snr, FS, seed=s), af=f0)[0]) for s in (1, 2)]
    print(f"  {name(bw, il):<11} dérive {rate} Hz/s ({rate * len(x) / FS:.0f} Hz en {len(x) / FS:.0f} s), "
          f"{snr} dB : {np.mean(sc):4.0%}")


def noise_test(bw, il):
    x = np.random.default_rng(7).normal(0, 0.25, FS * 60)
    txt, _ = decode(bw, il, x)
    print(f"  {name(bw, il):<11} bruit seul 60 s : {len(txt)} caractère(s) {txt[:30]!r}")
    return len(txt)


def speed_test(bw, il, fs=FS):
    x = add_noise(np.concatenate([np.zeros(fs // 2),
                                  mt63_encode(TEXT[:60], fs, 500 + bw / 2, bw, il == "long")]), -5, fs)
    d = MT63(fs, 500 + bw / 2, bw, il)
    t0 = time.process_time()
    run(d, x, block=int(fs * 0.04))
    dt = time.process_time() - t0
    return len(x) / fs / max(dt, 1e-9)


def selftest_cases():
    """Cas rapides pour l'autotest global : (libellé, signal, décodeur, texte attendu, S/B)."""
    msg = "CQ DE F1NSK ORSAT MT63 73"
    out = []
    for bw, il, snr in ((1000, "long", -6), (500, "court", -9)):
        x = np.concatenate([mt63_encode(msg, FS, 500 + bw / 2, bw, il == "long"), tail(bw)])
        out.append((name(bw, il), x, MT63(FS, 500 + bw / 2, bw, il), msg, snr))
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    quick = "--rapide" in sys.argv
    modes = [m for m in SUBMODES if not args or any(a in name(*m) for a in args)]
    print("Sensibilité (score moyen sur %d tirages, bruit dans 2500 Hz) :"
          % len(SEEDS))
    for m in modes:
        sweep(*m)
    if quick:
        return 0
    print("\nAcquisition (clic décalé) :")
    for m in modes:
        offset_test(*m)
    print("\nDérive lente :")
    for bw, il in modes:
        if il == "long":
            drift_test(bw, il, {500: 0.15, 1000: 1.0, 2000: 1.0}[bw])
    print("\nBruit seul :")
    nz = sum(noise_test(*m) for m in modes)
    print("\nVitesse (x temps réel, temps CPU, blocs de 40 ms) :")
    for bw, il in modes:
        sp = {fs: speed_test(bw, il, fs) for fs in (12000, 48000)}
        print(f"  {name(bw, il):<11} " + "  ".join(f"fs={fs}: x{v:.0f}" for fs, v in sp.items()))
    return 0 if nz <= 10 else 1


if __name__ == "__main__":
    sys.exit(main())
