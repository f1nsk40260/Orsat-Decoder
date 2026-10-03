"""Banc d'essai : génère une mire, ajoute du bruit, la fait décoder par petits blocs (comme en direct)."""
import sys
import difflib
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orsatdec.gen.encoders import add_noise  # noqa: E402


def run(decoder, x, block=480):
    out = []
    for i in range(0, len(x), block):
        for ev in decoder.process(x[i:i + block]):
            if ev["t"] == "text":
                out.append(ev["text"])
            elif ev["t"] == "msg":
                out.append(ev.get("text", "") + "\n")
    return "".join(out)


def score(expected, got):
    """Taux de caractères corrects (0-1) par alignement de séquences."""
    e, g = expected.upper(), got.upper()
    sm = difflib.SequenceMatcher(None, e, g, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / max(1, len(e))


def sweep(make_decoder, make_signal, expected, snrs, fs=12000, seeds=(1,)):
    res = {}
    for snr in snrs:
        vals = []
        for sd in seeds:
            x = make_signal()
            pad = np.zeros(int(fs * 0.5))
            x = np.concatenate([pad, x, pad])
            if snr is not None:
                x = add_noise(x, snr, fs, seed=sd)
            vals.append(score(expected, run(make_decoder(), x)))
        res[snr] = float(np.mean(vals))
    return res
