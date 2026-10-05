"""Identification automatique sur des mires générées (texte, fréquence audio et bruit différents de ceux
qui ont servi aux empreintes), puis confirmation par les décodeurs.

    python3 tests/test_sigid.py           # tous les sous-modes, à +6 et 0 dB
    python3 tests/test_sigid.py -q        # cas rapides de l'autotest
"""
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from orsatdec.signal_id import measure, identify, analyse, load_db  # noqa: E402
from orsatdec.gen.sigid_synth import SYNTH  # noqa: E402
from orsatdec.gen.encoders import add_noise  # noqa: E402
from orsatdec.modes import BY_ID  # noqa: E402

FS = 12000
MSG = "VVV DE F1NSK ORSAT DECODER IDENTIFICATION 73 GL "


def one(pageid, label, make, snr, af=1230.0, seed=4):
    x = make(MSG * 4, FS, af)
    x = np.concatenate([np.zeros(FS), x, np.zeros(FS)])
    x = add_noise(x, snr, FS, seed=seed)
    m = measure(x, FS)
    if m is None:
        return None, None, None
    cand = identify(m, top=5)
    ids = [c["id"] for c in cand]
    rank = ids.index(pageid) + 1 if pageid in ids else None
    var = cand[rank - 1]["variant"]["label"] if rank and cand[rank - 1].get("variant") else None
    return rank, var, (x, m, cand)


def selftest_cases():
    """Pour tests/selftest.py : chaîne complète (mesures, candidats, confirmation par décodage)."""
    out = []
    for pageid, label in ((140, "PSK31"), (197, "RTTY 45.45 bd / 170 Hz"), (200, "MFSK16"), (504, "Olivia 8/250")):
        mode_id, make = next((md, mk) for lb, md, _, mk in SYNTH[pageid] if lb == label)

        def fn(pageid=pageid, label=label, mode_id=mode_id, make=make):
            rank, var, extra = one(pageid, label, make, snr=6)
            a = analyse(extra[0], FS, modes=BY_ID) if extra else None
            c = a["confirmed"] if a else None
            ok = bool(c and c["id"] == pageid and c["mode"] == mode_id)
            return ok, f"rang {rank} avant décodage, confirmé : {BY_ID[c['mode']]['label'] if c else 'non'}"
        out.append((f"Ident. {label[:9]}", fn))
    return out


def main():
    quick = "-q" in sys.argv
    load_db()
    tot = good = goodvar = final = 0
    for snr in ((6,) if quick else (6, 0)):
        print(f"--- S/B {snr} dB (2500 Hz)")
        for pageid, variants in SYNTH.items():
            for label, mode_id, params, make in variants:
                if quick and label not in ("PSK31", "MFSK16", "Olivia 8/250"):
                    continue
                t0 = time.time()
                rank, var, extra = one(pageid, label, make, snr)
                tot += 1
                good += rank == 1
                goodvar += rank == 1 and var == label
                conf = ""
                if extra and BY_ID.get(mode_id, {}).get("kind") not in ("img",):
                    a = analyse(extra[0], FS, modes=BY_ID)
                    r = a["confirmed"] if a else None
                    final += bool(r and r["id"] == pageid)
                    conf = (f" -> décodé en {r['mode']} {r['params'] or ''} : {' '.join(r['text'].split())[:32]!r}"
                            if r else " -> non confirmé")
                else:
                    final += rank == 1
                top = extra[2][0]["title"][:22] if extra else "-"
                print(f"  {label:<24} rang {rank or '>5':>3}  variante {var or '-':<24} (1er : {top}) {time.time() - t0:4.1f} s{conf}")
    print(f"{good}/{tot} bien identifiés (famille), {goodvar}/{tot} avec le bon sous-mode, "
          f"{final}/{tot} après confirmation par décodage.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
