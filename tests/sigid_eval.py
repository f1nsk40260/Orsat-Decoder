"""Mesure le taux de bonne identification sur les enregistrements de la base Artemis.

    python3 tests/sigid_eval.py ~/Artemis-DB

Chaque enregistrement est coupé en deux : l'empreinte de référence vient de la première moitié, la
question est posée avec la seconde. C'est la même émission, donc un résultat optimiste pour l'empreinte ;
la ligne « paramètres seuls » (largeur et modulation déclarées, sans empreinte) est, elle, honnête.
"""
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import make_sigid  # noqa: E402
from orsatdec.signal_id import measure, identify  # noqa: E402


def _measure_b(args):
    db_dir, pid = args
    x = make_sigid.load_audio(Path(db_dir) / "static" / str(pid), part="b")
    return pid, (measure(x, make_sigid.FS) if x is not None else None)


def ranks(db, tests, use_fp=True, with_freq=False):
    ref = [dict(s) for s in db]
    rng = np.random.default_rng(1)
    byid = {s["id"]: s for s in db}
    if not use_fp:
        for s in ref:
            s.pop("fp", None)
    out = {}
    for pid, m in tests.items():
        freq = None
        if with_freq and byid[pid].get("fmin") is not None:     # fréquence d'écoute tirée dans sa plage
            freq = float(rng.uniform(byid[pid]["fmin"], byid[pid]["fmax"]))
        cand = identify(m, freq=freq, top=len(ref), db=ref)
        ids = [c["id"] for c in cand]
        out[pid] = ids.index(pid) + 1 if pid in ids else 999
    return out


def summary(label, r):
    n = len(r)
    t = lambda k: sum(v <= k for v in r.values()) / n
    print(f"  {label:<26} premier : {t(1):5.0%}   dans les 3 : {t(3):5.0%}   dans les 10 : {t(10):5.0%}   ({n} signaux)")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    db_dir = sys.argv[1]
    db = make_sigid.build(db_dir, part="a")
    ids = [s["id"] for s in db if s.get("fp")]
    with ProcessPoolExecutor(8) as ex:
        tests = {pid: m for pid, m in ex.map(_measure_b, [(db_dir, i) for i in ids]) if m}
    print(f"Identification sur {len(tests)} enregistrements de signaux de bande audio (base de {len(db)}) :")
    summary("paramètres seuls", ranks(db, tests, use_fp=False))
    r = ranks(db, tests)
    summary("paramètres + empreinte", r)
    summary("+ fréquence d'écoute", ranks(db, tests, with_freq=True))
    title = {s["id"]: s["title"] for s in db}
    from orsatdec.signal_id import DECODABLE
    dec = {k: v for k, v in r.items() if k in DECODABLE}
    if dec:
        summary("modes décodables", dec)
        for pid, v in sorted(dec.items(), key=lambda t: t[1]):
            print(f"      rang {v:3}  {title[pid]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
