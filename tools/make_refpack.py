"""Construit le pack de références d'Orsat-Decoder (bibliothèque de signaux hors ligne) à partir
d'une copie de la base Artemis (Artemis-DB : static/<id>/signal.json + media/1.png, media/1.ogg).

    python3 tools/make_refpack.py <Artemis-DB> [sortie.tar]

Pour chaque signal, le pack contient :
    <id>/signal.json     fiche (titre, catégories, fréquences, largeur, modulation, mode, ACF, lieux, descriptions)
    <id>/spectre.webp    image du spectre / waterfall de référence
    <id>/son.opus        extrait sonore de référence, mono, 30 s au plus
et à la racine index.json (liste résumée de tous les signaux) et VERSION.

Le pack s'installe dans ~/Orsat-Decoder/references ; Orsat-Decoder le lit sans jamais aller sur Internet.
Demande ffmpeg (libopus) et Pillow (WebP).
"""
import io
import json
import subprocess
import sys
import tarfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from PIL import Image

AUDIO_SECONDS = 30          # extrait gardé : assez pour écouter et comparer
AUDIO_KBPS = 32             # Opus mono 32 kb/s : tonalités et vitesses bien rendues
WEBP_QUALITY = 82


def nums(items):
    return [float(i["value"]) for i in items if isinstance(i.get("value"), (int, float))]


def card(s):
    """Fiche réduite : uniquement ce que l'interface affiche."""
    fr, bw = nums(s.get("frequency", [])), nums(s.get("bandwidth", []))
    return {
        "id": int(s["pageid"]), "title": s["title"], "cat": s.get("category", []),
        "freqs": sorted({f for f in fr}), "fmin": min(fr) if fr else None, "fmax": max(fr) if fr else None,
        "bw": max(bw) if bw else None,
        "mod": [m["value"] for m in s.get("modulation", [])], "mode": [m["value"] for m in s.get("mode", [])],
        "acf": [a for a in nums(s.get("acf", [])) if a > 0],
        "loc": [x["value"] for x in s.get("location", []) if x.get("value")],
        "short": (s.get("short description") or "").strip(),
        "desc": (s.get("description") or "").strip(),
        "page": f"https://www.sigidwiki.com/wiki/{s['title'].replace(' ', '_')}",
    }


def build_one(sig_dir):
    """Renvoie (id, fiche, octets image, octets son) ; image ou son à None s'ils manquent."""
    s = json.loads((sig_dir / "signal.json").read_text(encoding="utf-8"))
    c = card(s)
    media = sig_dir / "media"
    img = None
    png = media / "1.png"
    if png.is_file():
        im = Image.open(png).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=WEBP_QUALITY, method=6)
        img = buf.getvalue()
        c["img"] = list(im.size)
    snd = None
    ogg = media / "1.ogg"
    if ogg.is_file():
        r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(ogg), "-t", str(AUDIO_SECONDS), "-ac", "1",
                            "-c:a", "libopus", "-b:a", f"{AUDIO_KBPS}k", "-application", "audio",
                            "-map_metadata", "-1", "-f", "ogg", "-"], capture_output=True)
        if r.returncode == 0 and r.stdout:
            snd = r.stdout
            d = subprocess.run(["ffprobe", "-loglevel", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", str(ogg)], capture_output=True, text=True)
            try:
                c["dur"] = round(min(float(d.stdout.strip()), AUDIO_SECONDS), 1)
            except ValueError:
                pass
    c["has_img"], c["has_snd"] = img is not None, snd is not None
    return c["id"], c, img, snd


def add(tar, name, data, mtime):
    ti = tarfile.TarInfo(name)
    ti.size, ti.mtime, ti.mode = len(data), mtime, 0o644
    tar.addfile(ti, io.BytesIO(data))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    db = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("orsat-references.tar")
    dirs = sorted(d for d in (db / "static").iterdir() if (d / "signal.json").is_file())
    with ProcessPoolExecutor() as ex:
        res = list(ex.map(build_one, dirs))
    res.sort(key=lambda r: r[1]["title"].lower())
    version = time.strftime("%Y.%m.%d")
    mtime = int(time.time())
    index = []
    with tarfile.open(out, "w") as tar:
        for pid, c, img, snd in res:
            add(tar, f"{pid}/signal.json", json.dumps(c, ensure_ascii=False).encode(), mtime)
            if img:
                add(tar, f"{pid}/spectre.webp", img, mtime)
            if snd:
                add(tar, f"{pid}/son.opus", snd, mtime)
            index.append({k: c[k] for k in ("id", "title", "cat", "fmin", "fmax", "bw", "mod", "mode",
                                            "has_img", "has_snd")})
        add(tar, "index.json", json.dumps({"version": version, "source": "Artemis-DB / Signal Identification Wiki",
                                           "signals": index}, ensure_ascii=False).encode(), mtime)
        add(tar, "VERSION", (version + "\n").encode(), mtime)
    n_img = sum(1 for r in res if r[2])
    n_snd = sum(1 for r in res if r[3])
    print(f"{len(res)} signaux, {n_img} images, {n_snd} sons -> {out} ({out.stat().st_size / 1e6:.1f} Mo)")


if __name__ == "__main__":
    main()
