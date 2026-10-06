"""Génère orsatdec/data/sigid.json à partir d'Artemis-DB (base sigidwiki d'Artemis, GPL-3).

    git clone --depth 1 https://github.com/AresValley/Artemis-DB ~/Artemis-DB
    python3 tools/make_sigid.py ~/Artemis-DB

Pour chaque signal : titre, catégories, fréquences, largeur, modulation, mode, ACF (paramètres déclarés),
et, quand l'enregistrement de référence passe dans une bande BLU, son « empreinte » : les mesures
d'orsatdec.signal_id.measure() faites sur cet enregistrement. Demande ffmpeg.
"""
import json
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from orsatdec.signal_id import measure, FEATS  # noqa: E402

FS = 12000
AUDIO_MAX_BW = 4000      # au-delà, le signal ne tient pas dans l'audio d'un récepteur BLU


def nums(items):
    return [float(i["value"]) for i in items if isinstance(i.get("value"), (int, float))]


def load_audio(sig_dir, part=None):
    media = sig_dir / "media"
    ogg = next(iter(sorted(media.glob("*.ogg"))), None) if media.is_dir() else None
    if ogg is None:
        return None
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(ogg), "-ac", "1", "-ar", str(FS),
                        "-f", "s16le", "-"], capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return None
    x = np.frombuffer(r.stdout, "<i2").astype(np.float64) / 32768
    if part == "a":
        x = x[: len(x) // 2]
    elif part == "b":
        x = x[len(x) // 2:]
    return x


def record(sig_dir):
    s = json.loads((sig_dir / "signal.json").read_text(encoding="utf-8"))
    fr, bw = nums(s["frequency"]), nums(s["bandwidth"])
    return {
        "id": int(s["pageid"]), "title": s["title"], "cat": s["category"],
        "fmin": min(fr) if fr else None, "fmax": max(fr) if fr else None,
        "freqs": sorted({float(f) for f in fr}),
        "bw": max(bw) if bw else None,
        "mod": [m["value"] for m in s["modulation"]], "mode": [m["value"] for m in s["mode"]],
        "acf": [a for a in nums(s["acf"]) if a > 0],
    }


def audio_candidate(rec):
    """Signal susceptible d'arriver dans l'audio BLU d'un récepteur HF."""
    if rec["bw"] is not None and rec["bw"] > AUDIO_MAX_BW:
        return False
    return rec["fmin"] is None or rec["fmin"] < 30e6


def fingerprint(sig_dir, part=None):
    x = load_audio(sig_dir, part)
    if x is None:
        return None
    try:
        m = measure(x, FS)
    except Exception:
        return None
    if m is None:
        return None
    return {k: (round(m[k], 3) if isinstance(m[k], float) else m[k]) for k in FEATS}


def synth_fps(pageid, snrs=(15.0, 5.0, 0.0), af=1500.0):
    """Empreintes des sous-modes générés (orsatdec/gen/sigid_synth.py) pour un signal décodable, à
    plusieurs rapports S/B : certaines mesures (nombre de tonalités surtout) changent avec le bruit."""
    from orsatdec.gen.sigid_synth import SYNTH, TEXT
    from orsatdec.gen.encoders import add_noise
    out = []
    for label, mode_id, params, make in SYNTH.get(pageid, []):
        x = make(TEXT, FS, af)
        x = np.concatenate([np.zeros(FS // 2), x, np.zeros(FS // 2)])
        for snr in snrs:
            m = measure(add_noise(x, snr, FS, seed=7), FS)
            if m:
                fp = {k: (round(m[k], 3) if isinstance(m[k], float) else m[k]) for k in FEATS}
                out.append({"label": label, "mode": mode_id, "params": params, "snr": snr, **fp})
    return out


def _job(args):
    d, part = args
    rec = record(d)
    if audio_candidate(rec):
        rec["fp"] = fingerprint(d, part)
        fps = synth_fps(rec["id"])
        if fps:
            rec["fps"] = fps
    return rec


def build(db_dir, part=None, workers=8):
    dirs = sorted(p for p in (Path(db_dir) / "static").iterdir() if (p / "signal.json").exists())
    with ProcessPoolExecutor(workers) as ex:
        recs = list(ex.map(_job, [(d, part) for d in dirs]))
    return [r for r in recs if audio_candidate(r)]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    recs = build(sys.argv[1])
    out = ROOT / "orsatdec" / "data" / "sigid.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "source": "Artemis-DB (https://github.com/AresValley/Artemis-DB), données sigidwiki.com, GPL-3",
        "signals": recs}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    n = sum(1 for r in recs if r.get("fp"))
    ns = len({(r["id"], f["label"]) for r in recs for f in r.get("fps", [])})
    print(f"{len(recs)} signaux de bande audio, dont {n} avec empreinte, + {ns} sous-modes générés (3 niveaux de bruit) -> {out} ({out.stat().st_size // 1024} Ko)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
