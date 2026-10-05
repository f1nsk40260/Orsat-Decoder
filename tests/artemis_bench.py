"""Banc sur signaux réels : fait passer les décodeurs d'Orsat-Decoder sur les enregistrements de la
base Artemis (sigidwiki), qui sont de vraies réceptions et non des mires générées.

    python3 tests/artemis_bench.py ~/Artemis-DB            # tous les modes connus
    python3 tests/artemis_bench.py ~/Artemis-DB rtty cw    # seulement certains

Artemis-DB : git clone --depth 1 https://github.com/AresValley/Artemis-DB (≈ 300 Mo, GPL-3).
Demande ffmpeg pour lire les .ogg. Les images (fax, SSTV, Hell) sont écrites dans ./artemis-bench/.
Les échantillons ne durent que 8 à 40 s : pas de taux d'erreur ici (on ne connaît pas le texte émis),
mais un texte lisible prouve que le décodeur tient sur un vrai signal d'ondes courtes.
"""
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from orsatdec.modes import BY_ID, default_params  # noqa: E402
from orsatdec.images import ImageStore  # noqa: E402

FS = 12000

# signal Artemis (pageid) -> modes et variantes de paramètres à essayer
CASES = {
    "psk": (140, [("psk31", {}), ("psk63", {}), ("psk125", {})]),
    "rtty": (197, [("rtty", {"baud": b, "shift": s, "reverse": r})
                   for b in (45.45, 50.0, 75.0) for s in (170.0, 425.0, 450.0, 850.0) for r in (False, True)]),
    "cw": (567, [("cw", {"bw": 80.0}), ("cw", {"bw": 150.0})]),
    "navtex": (620, [("navtex", {"reverse": r}) for r in (False, True)]),
    "ft8": (4702, [("ft8", {})]),
    "ft4": (5624, [("ft4", {})]),
    "mfsk": (200, [(m["id"], {"reverse": r}) for m in BY_ID.values() if m["id"].startswith("mfsk-") for r in (False, True)]),
    "dominoex": (832, [(m["id"], {"reverse": r, "fec": f}) for m in BY_ID.values() if m["id"].startswith("dominoex-")
                       for r in (False, True) for f in (False, True)]),
    "thor": (1421, [(m["id"], {"reverse": r}) for m in BY_ID.values() if m["id"].startswith("thor-") for r in (False, True)]),
    "olivia": (504, [("olivia", {"tones": t, "bw": b, "reverse": r}) for t in (4, 8, 16, 32, 64)
                     for b in (125, 250, 500, 1000, 2000) for r in (False, True)]),
    "contestia": (1236, [("contestia", {"tones": t, "bw": b, "reverse": r}) for t in (4, 8, 16, 32)
                         for b in (125, 250, 500, 1000) for r in (False, True)]),
    "mt63": (1370, [(m, {"interleave": i}) for m in ("mt63_500", "mt63_1000", "mt63_2000") for i in ("long", "court")]),
    # pour les images, le banc ne sait pas juger : à égalité c'est la première variante (la plus courante) qui reste
    "wefax": (99, [("wefax", {"ioc": i, "lpm": l}) for i in (576, 288) for l in (120, 60, 90, 240)]),
    "sstv": (960, [("sstv", {})]),
    "hell": (1309, [("hell", {"mode": m}) for m in ("feld", "slow", "x5", "x9", "fsk245", "fsk105", "hell80")]),
}


def load(db, pageid):
    media = Path(db) / "static" / str(pageid) / "media"
    ogg = next(iter(sorted(media.glob("*.ogg"))), None)
    if ogg is None:
        return None
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(ogg), "-ac", "1", "-ar", str(FS),
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, "<i2").astype(np.float64) / 32768
    return x / (np.max(np.abs(x)) + 1e-12) * 0.5


def with_tail(x, seconds=12.0):
    """Ajoute un peu de bruit faible après l'échantillon : les modes entrelacés (MT63, MFSK, THOR)
    rendent leur texte plusieurs secondes après le signal, comme en direct."""
    rng = np.random.default_rng(3)
    return np.concatenate([x, rng.normal(0, 1e-3, int(FS * seconds))])


def occupied(x, lo=150, hi=3500):
    """Centre et largeur de la zone où le spectre moyen dépasse le bruit de 10 dB (signal principal)."""
    n = 4096
    seg = np.lib.stride_tricks.sliding_window_view(x, n)[::n // 2]
    p = np.mean(np.abs(np.fft.rfft(seg * np.hanning(n), axis=1)) ** 2, axis=0)
    f = np.fft.rfftfreq(n, 1 / FS)
    k = (f >= lo) & (f <= hi)
    db = 10 * np.log10(p[k] + 1e-20)
    db = np.convolve(db, np.ones(9) / 9, mode="same")
    fk = f[k]
    peak = int(np.argmax(db))
    thr = max(np.median(db) + 10, db[peak] - 25)
    a = b = peak
    while a > 0 and db[a - 1] > thr:
        a -= 1
    while b < len(db) - 1 and db[b + 1] > thr:
        b += 1
    return (fk[a] + fk[b]) / 2, fk[b] - fk[a]


def plausible(text):
    """Note de lisibilité : longueur du texte fait de lettres, chiffres, ponctuation courante."""
    if not text:
        return 0.0
    ok = sum(c.isalnum() or c in " .,:;-/?'()=+\n\r" for c in text)
    words = [w for w in text.split() if len(w) >= 3 and w.isalpha()]
    return ok * (ok / len(text)) ** 3 + 5 * len(words)


def run(mode_id, params, x, af, outdir, tag):
    m = BY_ID[mode_id]
    p = dict(default_params(m), **params)
    dec = m["make"](FS, af, p)
    text, imgs = [], []
    if hasattr(dec, "period"):      # FT8/FT4 : créneau horaire, on décode l'échantillon comme un créneau
        dec._decode(x, 0)
        return "".join(r.get("text", "") + "\n" for r in dec.results), []
    store = ImageStore(outdir, lambda: tag)
    for i in range(0, len(x), 480):
        for ev in dec.process(x[i:i + 480]):
            if ev["t"] == "text":
                text.append(ev["text"])
            elif ev["t"] == "msg":
                text.append(ev.get("text", "") + "\n")
            elif ev["t"] == "img":
                r = store.handle(ev)
                if ev.get("op") == "new":
                    imgs.append(ev.get("title") or "image")
                if r:
                    imgs.append(r["path"])
    if hasattr(dec, "flush"):
        for ev in dec.flush() or []:
            if ev.get("t") == "text":
                text.append(ev["text"])
    c = store.cur
    if c is not None and c.get("tape") and c["buf"]:     # bande Hell : colonnes de h pixels
        from orsatdec.images import png_bytes
        h = c["h"]
        cols = np.frombuffer(bytes(c["buf"][:len(c["buf"]) // h * h]), np.uint8).reshape(-1, h)
        p = outdir / f"{tag}.png"
        p.write_bytes(png_bytes(cols.shape[0], h, np.ascontiguousarray(cols.T).tobytes(), False))
        imgs.append(str(p))
    elif c is not None:
        r = store.handle({"t": "img", "op": "end"})
        if r:
            imgs.append(r["path"])
    return "".join(text), imgs


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    db, only = sys.argv[1], [a.lower() for a in sys.argv[2:]]
    outdir = Path("artemis-bench")
    outdir.mkdir(exist_ok=True)
    for name, (pageid, variants) in CASES.items():
        if only and name not in only:
            continue
        x = load(db, pageid)
        if x is None:
            print(f"{name:<10} échantillon absent")
            continue
        fc, bw = occupied(x)
        dur = len(x) / FS
        x = with_tail(x)
        best = (-1, None, "", [])
        for mode_id, params in variants:
            m = BY_ID[mode_id]
            # fax et SSTV ont des fréquences audio normalisées (1500-2300 Hz) : on garde celles du mode
            af = 0 if m.get("whole") else (m["af"] if mode_id in ("wefax", "sstv") else fc)
            try:
                text, imgs = run(mode_id, params, x, af, outdir, f"{name}-{mode_id}")
            except Exception as e:  # un décodeur qui plante sur un vrai signal est un résultat aussi
                text, imgs = f"[exception {type(e).__name__}: {e}]", []
            sc = plausible(text) + 50 * len(imgs)
            if sc > best[0]:
                best = (sc, (mode_id, params), text, imgs)
        sc, var, text, imgs = best
        one = " ".join(text.split())
        print(f"{name:<10} {dur:4.0f} s  signal ≈ {fc:6.0f} Hz / {bw:5.0f} Hz   meilleur : {var[0]} {var[1]}")
        print(f"           texte : {one[:300] or '(rien)'}")
        if imgs:
            print(f"           images : {', '.join(map(str, imgs))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
