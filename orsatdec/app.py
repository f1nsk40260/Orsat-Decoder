"""Orsat-Decoder : serveur local (aiohttp) entre le serveur PhantomSDR / Orsat-SDR et l'interface web.

- un flux waterfall vers le serveur choisi, relayé à l'interface en trames binaires ;
- un flux audio par canal de décodage, chacun avec son décodeur dans son propre fil d'exécution ;
- configuration et canaux mémorisés dans ~/.config/orsat-decoder/config.json.
"""
import argparse
import asyncio
import json
import logging
import os
import shutil
import signal
import struct
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from aiohttp import web, WSMsgType

from . import __version__
from .modes import BY_ID, public_catalog, default_params, bandwidth
from .phantom import AudioChannel, Waterfall

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
CONF_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "orsat-decoder"
CONF_FILE = CONF_DIR / "config.json"
DATA = Path(os.environ.get("ORSAT_DATA", Path.home() / ".local/share/orsat-decoder"))
log = logging.getLogger("orsat")

DEFAULT_CONF = {
    "servers": [
        {"name": "ORSAT", "url": "http://orsat.ddns.net:8080"},
        {"name": "Orsat-SDR local", "url": "http://127.0.0.1:9002"},
    ],
    "server": 0,
    "rx": None,
    "channels": [],
    "ui": {},
}


def load_conf():
    try:
        c = json.loads(CONF_FILE.read_text())
        return {**DEFAULT_CONF, **c}
    except Exception:
        return json.loads(json.dumps(DEFAULT_CONF))


def save_conf(c):
    try:
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONF_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(c, indent=2, ensure_ascii=False))
        tmp.replace(CONF_FILE)
    except Exception as e:
        log.warning("configuration non enregistrée : %s", e)


class Channel:
    """Un canal = une fréquence + un mode + un décodeur, alimenté par son propre flux audio."""

    def __init__(self, app, cid, mode_id, freq, params=None, paused=False):
        self.app, self.id = app, cid
        self.mode = BY_ID[mode_id]
        self.params = {**default_params(self.mode), **(params or {})}
        self.freq = float(freq)              # fréquence du signal (ou cadran pour FT8/FT4)
        self.paused = paused
        self.decoder = None
        self.state = "connexion"
        self.level = 0.0
        self.exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"ch{cid}")
        self.audio = None
        self.history = []                    # derniers événements, rejoués à une interface qui se connecte
        self._status_t = 0

    @property
    def dial(self):
        return self.freq - self.mode["af"]

    def describe(self):
        return {"id": self.id, "mode": self.mode["id"], "freq": self.freq, "params": self.params,
                "paused": self.paused, "state": self.state, "bw": bandwidth(self.mode, self.params),
                "af": self.mode["af"]}

    def start(self):
        srv = self.app.server
        self.audio = AudioChannel(srv["url"], self.dial, "USB", rx=self.app.rx, tap=srv.get("tap"),
                                  on_pcm=self._on_pcm, on_state=self._on_state, session=self.app.http)
        self.audio.start()

    async def stop(self):
        if self.audio:
            await self.audio.close()
        self.exec.shutdown(wait=False, cancel_futures=True)

    def _on_state(self, state, info=None):
        if state == "connected":
            fs = info.get("audio_max_sps", 12000)
            self.decoder = self.mode["make"](fs, self.mode["af"] or 1500, self.params)
            self.state = "écoute"
        elif state == "reconnecting":
            self.state = "reconnexion"
        elif state == "error":
            self.state = "erreur"
        self.app.broadcast({"t": "chan", **self.describe()})

    def _on_pcm(self, pcm):
        if self.paused or self.decoder is None:
            return
        self.level = 0.9 * self.level + 0.1 * float(np.sqrt(np.mean(pcm * pcm)) if len(pcm) else 0)
        fut = self.exec.submit(self.decoder.process, pcm)
        fut.add_done_callback(lambda f: self.app.loop.call_soon_threadsafe(self._done, f))

    def _done(self, fut):
        try:
            events = fut.result()
        except Exception as e:
            log.exception("décodeur %s : %s", self.mode["label"], e)
            return
        for ev in events:
            ev = {**ev, "ch": self.id}
            self.history.append(ev)
            if len(self.history) > 400:
                del self.history[:100]
            self.app.broadcast(ev)
        now = time.time()
        if now - self._status_t > 0.5 and self.decoder is not None:
            self._status_t = now
            st = {k: (float(v) if isinstance(v, (np.floating,)) else v) for k, v in self.decoder.status().items()}
            self.app.broadcast({"t": "cstat", "ch": self.id, "level": round(self.level, 4), **st})

    async def retune(self, freq=None, params=None):
        if params is not None:
            self.params.update(params)
            self.decoder = None
        if freq is not None:
            self.freq = float(freq)
        if self.audio and self.audio.info:
            fs = self.audio.info.get("audio_max_sps", 12000)
            if params is not None:
                self.decoder = self.mode["make"](fs, self.mode["af"] or 1500, self.params)
            await self.audio.retune(self.dial)


class App:
    def __init__(self, args):
        self.args = args
        self.conf = load_conf()
        self.clients = set()
        self.channels = {}
        self.waterfall = None
        self.wf_info = None
        self.http = None
        self.loop = None
        self.quit = asyncio.Event()
        self.had_client = False
        self.last_client = time.time()

    @property
    def server(self):
        s = self.conf["servers"]
        return s[min(self.conf.get("server", 0), len(s) - 1)]

    @property
    def rx(self):
        return self.conf.get("rx")

    # ------------------------------------------------------------------ diffusion
    def broadcast(self, msg):
        if not self.clients:
            return
        data = json.dumps(msg, ensure_ascii=False, default=float)
        for ws in list(self.clients):
            if not ws.closed:
                asyncio.ensure_future(self._send(ws, data))

    @staticmethod
    async def _send(ws, data):
        try:
            if isinstance(data, bytes):
                await ws.send_bytes(data)
            else:
                await ws.send_str(data)
        except Exception:
            pass

    def _wf_line(self, d):
        if not self.clients:
            return
        b = np.clip(d["bins"], -128, 127).astype(np.int16) + 128
        payload = struct.pack("<Bdd", 1, d["freq0"], d["freq1"]) + b.astype(np.uint8).tobytes()
        for ws in list(self.clients):
            if not ws.closed:
                asyncio.ensure_future(self._send(ws, payload))

    def _wf_info(self, info):
        self.wf_info = info
        self.broadcast({"t": "server", "info": self._server_summary()})

    def _server_summary(self):
        i = self.wf_info or {}
        return {"name": self.server.get("name"), "url": self.server.get("url"),
                "basefreq": i.get("basefreq"), "total_bandwidth": i.get("total_bandwidth"),
                "rx": i.get("rx"), "rx_name": i.get("rx_name"),
                "receivers": i.get("receivers") or [], "connected": bool(i)}

    # ------------------------------------------------------------------ cycle de vie
    async def connect_server(self):
        if self.waterfall:
            await self.waterfall.close()
        for ch in list(self.channels.values()):
            await ch.stop()
        self.channels.clear()
        self.wf_info = None
        self.broadcast({"t": "server", "info": self._server_summary()})
        self.waterfall = Waterfall(self.server["url"], rx=self.rx, on_line=self._wf_line,
                                   on_info=self._wf_info, session=self.http)
        self.waterfall.start()
        for c in self.conf.get("channels", []):
            if c.get("server") == self.server["url"] and c.get("rx") == self.rx and c.get("mode") in BY_ID:
                self._add_channel(c["mode"], c["freq"], c.get("params"), c.get("paused", False), save=False)

    def _add_channel(self, mode_id, freq, params=None, paused=False, save=True):
        cid = uuid.uuid4().hex[:8]
        ch = Channel(self, cid, mode_id, freq, params, paused)
        self.channels[cid] = ch
        ch.start()
        self.broadcast({"t": "chan", **ch.describe()})
        if save:
            self._save_channels()
        return ch

    def _save_channels(self):
        others = [c for c in self.conf.get("channels", [])
                  if not (c.get("server") == self.server["url"] and c.get("rx") == self.rx)]
        mine = [{"server": self.server["url"], "rx": self.rx, "mode": ch.mode["id"], "freq": ch.freq,
                 "params": ch.params, "paused": ch.paused} for ch in self.channels.values()]
        self.conf["channels"] = others + mine
        save_conf(self.conf)

    async def idle_watch(self):
        if self.args.no_autoquit:
            return
        while True:
            await asyncio.sleep(2)
            if self.clients:
                self.last_client = time.time()
            elif self.had_client and time.time() - self.last_client > 15:
                log.info("interface fermée : arrêt")
                self.quit.set()
                return

    # ------------------------------------------------------------------ messages de l'interface
    async def handle(self, ws, m):
        t = m.get("t")
        if t == "add":
            mode = BY_ID.get(m.get("mode"))
            if mode:
                self._add_channel(mode["id"], float(m["freq"]), m.get("params"))
        elif t == "remove":
            ch = self.channels.pop(m.get("ch"), None)
            if ch:
                await ch.stop()
                self.broadcast({"t": "chan_removed", "ch": ch.id})
                self._save_channels()
        elif t == "retune":
            ch = self.channels.get(m.get("ch"))
            if ch:
                await ch.retune(freq=m.get("freq"), params=m.get("params"))
                self.broadcast({"t": "chan", **ch.describe()})
                self._save_channels()
        elif t == "pause":
            ch = self.channels.get(m.get("ch"))
            if ch:
                ch.paused = bool(m.get("paused"))
                self.broadcast({"t": "chan", **ch.describe()})
                self._save_channels()
        elif t == "view":
            if self.waterfall:
                await self.waterfall.set_view(float(m["f0"]), float(m["f1"]))
        elif t == "select_server":
            idx = int(m.get("index", 0))
            if 0 <= idx < len(self.conf["servers"]):
                self.conf["server"] = idx
                self.conf["rx"] = None
                save_conf(self.conf)
                await self.connect_server()
        elif t == "select_rx":
            self.conf["rx"] = m.get("rx") or None
            save_conf(self.conf)
            await self.connect_server()
        elif t == "servers_set":
            servers = [s for s in m.get("servers", []) if s.get("url")]
            if servers:
                cur = self.server["url"]
                self.conf["servers"] = servers
                urls = [s["url"] for s in servers]
                self.conf["server"] = urls.index(cur) if cur in urls else 0
                save_conf(self.conf)
                if cur not in urls:
                    await self.connect_server()
                self.broadcast({"t": "servers", "servers": servers, "current": self.conf["server"]})
        elif t == "prefs":
            self.conf.setdefault("ui", {}).update(m.get("prefs", {}))
            save_conf(self.conf)
        elif t == "quit":
            self.quit.set()

    async def hello(self, ws):
        await self._send(ws, json.dumps({
            "t": "hello", "version": __version__, "catalog": public_catalog(),
            "servers": self.conf["servers"], "current": self.conf.get("server", 0),
            "server": self._server_summary(), "ui": self.conf.get("ui", {}),
            "channels": [ch.describe() for ch in self.channels.values()],
        }, ensure_ascii=False, default=float))
        for ch in self.channels.values():
            for ev in ch.history[-150:]:
                await self._send(ws, json.dumps(ev, ensure_ascii=False, default=float))


def make_web(app):
    w = web.Application()

    async def index(request):
        return web.FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=1 << 20)
        await ws.prepare(request)
        app.clients.add(ws)
        app.had_client = True
        await app.hello(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        asyncio.ensure_future(app.handle(ws, json.loads(msg.data)))
                    except ValueError:
                        pass
        finally:
            app.clients.discard(ws)
        return ws

    w.router.add_get("/", index)
    w.router.add_get("/ws", ws_handler)
    w.router.add_static("/", WEB, show_index=False)
    return w


def open_window(url, args):
    if args.no_browser:
        return None
    profile = DATA / "browser-profile"
    for b in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "brave-browser",
              "microsoft-edge", "vivaldi"):
        exe = shutil.which(b)
        if exe:
            profile.mkdir(parents=True, exist_ok=True)
            return subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}", "--class=orsat-decoder",
                                     "--window-size=1500,950", "--no-first-run", "--no-default-browser-check"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for b in ("firefox", "xdg-open"):
        if shutil.which(b):
            return subprocess.Popen([b, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"Ouvrez {url} dans votre navigateur.")
    return None


async def main_async(args):
    import aiohttp
    app = App(args)
    app.loop = asyncio.get_running_loop()
    app.http = aiohttp.ClientSession()
    for sig in (signal.SIGINT, signal.SIGTERM):
        app.loop.add_signal_handler(sig, app.quit.set)
    runner = web.AppRunner(make_web(app), access_log=None)
    await runner.setup()
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    url = f"http://127.0.0.1:{args.port}/"
    try:
        await web.TCPSite(runner, host, args.port).start()
    except OSError:
        print(f"Orsat-Decoder semble déjà lancé (port {args.port} occupé) : ouverture de la fenêtre.")
        open_window(url, args)
        await app.http.close()
        return
    log.info("Orsat-Decoder %s : %s", __version__, url)
    await app.connect_server()
    tasks = [asyncio.create_task(app.idle_watch())]
    browser = open_window(url, args)
    if browser is not None and "--app=" in " ".join(map(str, browser.args)):
        async def wait_browser():
            await asyncio.to_thread(browser.wait)
            app.quit.set()
        tasks.append(asyncio.create_task(wait_browser()))
    await app.quit.wait()
    for ch in list(app.channels.values()):
        await ch.stop()
    if app.waterfall:
        await app.waterfall.close()
    for t in tasks:
        t.cancel()
    await app.http.close()
    await runner.cleanup()
    log.info("terminé")


def main():
    ap = argparse.ArgumentParser(prog="orsat-decoder", description="Décodeur multimode pour PhantomSDR / Orsat-SDR")
    ap.add_argument("--port", type=int, default=8074, help="port de l'interface (défaut 8074)")
    ap.add_argument("--lan", action="store_true", help="interface accessible depuis le réseau local")
    ap.add_argument("--no-browser", action="store_true", help="ne pas ouvrir de fenêtre")
    ap.add_argument("--no-autoquit", action="store_true", help="ne pas s'arrêter quand la fenêtre est fermée")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(), logging.FileHandler(DATA / "orsat-decoder.log", "w")])
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
