"""Essai de bout en bout de l'identification, dans la vraie interface (navigateur sans écran).

Lance le faux serveur TCI (jeu « ident » : RTTY DWD inversé, Olivia 8/250, MFSK16, sans dire lesquels)
et Orsat-Decoder, choisit « Identifier », clique sur chaque signal du waterfall, attend que le canal du
bon mode s'ouvre et vérifie qu'il décode. Demande playwright (pip install playwright).

    python3 tests/e2e_ident.py [--shots DOSSIER]
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TCI_PORT, UI_PORT = 50061, 8097
DIAL = 10_100_000
EXPECT = [(10_100_800, "rtty", "WEATHER"), (10_101_700, "olivia", "OLIVIA"), (10_102_400, "mfsk-16", "MFSK16")]


def main():
    shots = Path(sys.argv[sys.argv.index("--shots") + 1]) if "--shots" in sys.argv else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    data = Path(tempfile.mkdtemp(prefix="orsat-e2e-"))
    (data / "config.json").write_text(json.dumps({
        "sources": [{"id": "fake", "type": "tci", "name": "Faux TCI", "url": f"ws://127.0.0.1:{TCI_PORT}", "trx": 0}],
        "source": "fake", "channels": [], "ui": {"mode": "ident"}}))
    env = dict(os.environ, FAKE_TCI_SET="ident", ORSAT_DATA=str(data), PYTHONPATH=str(ROOT))
    procs = [subprocess.Popen([sys.executable, str(ROOT / "tests" / "fake_tci.py"), str(TCI_PORT)], env=env),
             subprocess.Popen([sys.executable, "-m", "orsatdec", "--no-browser", "--no-autoquit", "--port", str(UI_PORT)],
                              env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=open(data / "stderr.txt", "w"))]
    from playwright.sync_api import sync_playwright
    ok_all = True
    try:
        time.sleep(4)
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            page = b.new_page(viewport={"width": 1500, "height": 950})
            page.goto(f"http://127.0.0.1:{UI_PORT}/")
            page.wait_for_function("S.view && WF.lastRow", timeout=20000)
            page.get_by_role("button", name="Identifier").first.click()
            time.sleep(3)                                   # quelques lignes de waterfall
            for f, mode, word in EXPECT:
                box = page.locator("#wf").bounding_box()
                f0, f1 = page.evaluate("S.view")
                x = box["x"] + (f - f0) / (f1 - f0) * box["width"]
                before = set(page.evaluate("[...S.chans.keys()]"))
                page.mouse.click(x, box["y"] + box["height"] * 0.5)
                t0, opened, text = time.time(), None, ""
                while time.time() - t0 < 120:
                    time.sleep(1)
                    chans = page.evaluate("[...S.chans.values()].map(c => ({id: c.id, mode: c.mode, text: c.out.innerText}))")
                    new = [c for c in chans if c["id"] not in before and c["mode"] != "ident"]
                    if new:
                        opened = new[0]
                        if word in opened["text"].upper().split("]", 1)[-1] or time.time() - t0 > 100:
                            text = opened["text"]
                            break
                if shots:
                    page.screenshot(path=str(shots / f"ident-{mode}.png"))
                ok = bool(opened and opened["mode"] == mode and word in text.upper())
                ok_all &= ok
                took = time.time() - t0
                print(f"  {'OK ' if ok else 'ÉCHEC'}  {f / 1000:.1f} kHz -> {opened['mode'] if opened else 'aucun canal'}"
                      f" en {took:.0f} s : {' '.join(text.split())[:70]!r}")
            b.close()
    finally:
        for p in procs:
            p.terminate()
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
