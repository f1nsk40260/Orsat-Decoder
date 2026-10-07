"""Essai de bout en bout des sources KiwiSDR et OpenWebRX, dans la vraie interface (navigateur sans écran).

Lance les faux serveurs (tests/fake_websdr.py) et Orsat-Decoder, ajoute chaque serveur par la fenêtre
« Ajouter un serveur » (type reconnu automatiquement), ouvre des canaux PSK31, RTTY et CW et vérifie qu'ils
décodent ; vérifie aussi le changement de profil OpenWebRX et le refus d'un Kiwi plein.

    python3 tests/e2e_websdr.py [--shots DOSSIER]
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KIWI, OWRX, UI = 18073, 18074, 8096
EXPECT = [("psk31", 7_070_800, "F1NSK"), ("rtty", 7_071_500, "F1NSK"), ("cw", 7_069_900, "F1NSK")]


def main():
    shots = Path(sys.argv[sys.argv.index("--shots") + 1]) if "--shots" in sys.argv else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    data = Path(tempfile.mkdtemp(prefix="orsat-e2e-web-"))
    (data / "config.json").write_text(json.dumps({
        "sources": [{"id": "rien", "type": "phantom", "name": "Aucun", "url": "http://127.0.0.1:9"}],
        "source": "rien", "channels": [], "ui": {"mode": "psk31"}}))
    env = dict(os.environ, ORSAT_DATA=str(data), PYTHONPATH=str(ROOT), FAKE_KIWI_SLOTS="4")
    procs = [subprocess.Popen([sys.executable, str(ROOT / "tests" / "fake_websdr.py"), "kiwi", str(KIWI)], env=env),
             subprocess.Popen([sys.executable, str(ROOT / "tests" / "fake_websdr.py"), "owrx", str(OWRX)], env=env),
             subprocess.Popen([sys.executable, "-m", "orsatdec", "--no-browser", "--no-autoquit", "--port", str(UI)],
                              env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=open(data / "stderr.txt", "w"))]
    from playwright.sync_api import sync_playwright
    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        print(f"  {'OK  ' if ok else 'ÉCHEC'} {name}{' : ' + detail if detail else ''}", flush=True)

    try:
        time.sleep(4)
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            page = b.new_page(viewport={"width": 1500, "height": 950})
            page.goto(f"http://127.0.0.1:{UI}/")
            page.wait_for_function("S.sources && S.sources.length", timeout=20000)

            def add_server(url, expect_kind):
                page.select_option("#sourceSel", "+add")
                page.fill("#srvUrl", url)
                page.click("#srvOk")
                page.wait_for_function(f"S.server && S.server.kind === '{expect_kind}' && S.server.connected", timeout=20000)

            def open_and_check(label):
                for mode, f, word in EXPECT:
                    page.evaluate(f"S.mode = '{mode}'; renderModes(); addChannel('{mode}', {f})")
                    time.sleep(0.5)
                t0, txt = time.time(), {}
                while time.time() - t0 < 60:
                    time.sleep(2)
                    txt = {c["mode"]: c["text"] for c in page.evaluate(
                        "[...S.chans.values()].map(c => ({mode: c.mode, text: c.out.innerText}))")}
                    if all(w in txt.get(m, "") for m, _, w in EXPECT):
                        break
                for mode, _, word in EXPECT:
                    t = txt.get(mode, "")
                    check(f"{label} {mode}", word in t, repr(t[-60:]))

            def close_all():
                page.evaluate("[...S.chans.keys()].forEach(id => send({t: 'remove', ch: id}))")
                page.wait_for_function("S.chans.size === 0", timeout=10000)

            print("KiwiSDR :")
            add_server(f"127.0.0.1:{KIWI}", "kiwi")
            check("Kiwi reconnu et connecté", True, page.evaluate("S.server.name"))
            time.sleep(2)
            open_and_check("Kiwi")
            if shots:
                page.screenshot(path=str(shots / "kiwi.png"))
            # 4 connexions maxi : waterfall + 3 canaux ; un 4e canal doit être refusé proprement
            page.evaluate("addChannel('psk31', 7071000)")
            time.sleep(4)
            st = page.evaluate("[...S.chans.values()].map(c => c.card.innerText).join(' | ')")
            check("Kiwi plein : refus affiché", "plein" in st, "")
            close_all()

            print("OpenWebRX :")
            add_server(f"http://127.0.0.1:{OWRX}/", "owrx")
            check("OpenWebRX reconnu et connecté", True, page.evaluate("S.server.name"))
            open_and_check("OpenWebRX")
            if shots:
                page.screenshot(path=str(shots / "owrx.png"))
            close_all()
            page.select_option("#rxSel", "rtl|20m")
            page.wait_for_function("S.server.basefreq > 14000000", timeout=20000)
            check("OpenWebRX : changement de bande (profil 20 m)", True, str(page.evaluate("S.server.basefreq")))
            sources = page.evaluate("S.sources.map(s => s.type)")
            check("serveurs mémorisés", "kiwi" in sources and "owrx" in sources, str(sources))
            b.close()
    finally:
        for p in procs:
            p.terminate()
    saved = json.loads((data / "config.json").read_text())
    check("config.json contient les serveurs", {"kiwi", "owrx"} <= {s["type"] for s in saved["sources"]})
    print(f"{sum(results)}/{len(results)} vérifications réussies")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
