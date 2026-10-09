"""Orsat-Decoder : serveur local (aiohttp) entre une source de signal et l'interface web.

Sources : serveur PhantomSDR / Orsat-SDR, TCI (AetherSDR…), entrée audio PipeWire (+ CAT rigctld).
Chaque canal = un décodeur dans son propre fil d'exécution.
Tout est dans ~/Orsat-Decoder : logiciel, environnement Python, config.json (serveurs, canaux), journal.
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
import time
import wave
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from aiohttp import web, WSMsgType

from . import VERSION_LABEL
from .modes import BY_ID, public_catalog, default_params, bandwidth
from .images import ImageStore
from .sources import make_source, list_audio_inputs, detect_type, TYPES

MAX_CHANNELS = 3          # canaux de décodage ouverts en même temps (chacun prend un tiers de la largeur)

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
DATA = Path(os.environ.get("ORSAT_DATA", Path.home() / "Orsat-Decoder"))
CONF_DIR = DATA
CONF_FILE = CONF_DIR / "config.json"
log = logging.getLogger("orsat")

DEFAULT_SOURCES = [
    {"id": "orsat", "type": "phantom", "name": "ORSAT", "url": "http://orsat.ddns.net:8080"},
    {"id": "local", "type": "phantom", "name": "Orsat-SDR local", "url": "http://127.0.0.1:9002"},
    {"id": "aether", "type": "tci", "name": "AetherSDR (TCI)", "url": "ws://127.0.0.1:50001", "trx": 0},
    {"id": "carte", "type": "audio", "name": "Entrée audio", "device": "", "rigctl": ""},
]


def load_conf():
    try:
        c = json.loads(CONF_FILE.read_text())
    except Exception:
        c = {}
    if "sources" not in c:
        # ancienne configuration : liste de serveurs PhantomSDR
        olds = c.get("servers")
        if olds:
            c["sources"] = [{"id": f"srv{i}", "type": "phantom", "name": s.get("name") or s.get("url"),
                             "url": s.get("url")} for i, s in enumerate(olds)] + DEFAULT_SOURCES[2:]
            c["source"] = f"srv{c.get('server', 0)}"
            by_url = {s["url"]: s["id"] for s in c["sources"] if s.get("url")}
            for ch in c.get("channels", []):
                if "server" in ch and "source" not in ch:
                    ch["source"] = by_url.get(ch.pop("server"))
        else:
            c["sources"] = json.loads(json.dumps(DEFAULT_SOURCES))
    c.setdefault("source", c["sources"][0]["id"])
    c.setdefault("channels", [])
    c.setdefault("ui", {})
    if "bookmarks" not in c:                      # premiers signets : les fréquences préréglées
        from .modes import PRESETS
        c["bookmarks"] = [{"label": p["label"], "mode": p["mode"], "freq": p["freq"], "ribbon": True,
                           **({"params": p["params"]} if p.get("params") else {})} for p in PRESETS]
    return c


def save_conf(c):
    try:
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        c = {k: v for k, v in c.items() if k not in ("servers", "server", "rx")}
        tmp = CONF_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(c, indent=2, ensure_ascii=False))
        tmp.replace(CONF_FILE)
    except Exception as e:
        log.warning("configuration non enregistrée : %s", e)


def _num(v):
    return float(v) if isinstance(v, (np.floating, np.integer)) else v


class Channel:
    """Un canal = un mode + une fréquence + un décodeur. L'audio arrive par feed(pcm, fs)."""

    def __init__(self, app, cid, mode_id, freq=0.0, af=None, params=None, paused=False):
        self.app, self.id = app, cid
        self.mode = BY_ID[mode_id]
        self.params = {**default_params(self.mode), **(params or {})}
        self.freq = float(freq)              # source PhantomSDR : fréquence du signal (Hz)
        self.dial = None                     # source PhantomSDR : fréquence de la porteuse BLU demandée au serveur
        self.af = float(af if af is not None else (0 if self.mode.get("whole") else self.mode["af"]))
        self.paused = paused
        self.decoder = None
        self.fs = None
        self.state = "connexion"
        self.extra = {}
        self.level = 0.0
        self.exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"ch{cid}")
        self.history = []
        self._status_t = 0
        self.ring = np.zeros(4096, np.float32)   # audio récent, pour le mini-spectre du canal
        self._spec_n = 0
        self.last_audio = 0.0                    # instant du dernier bloc audio reçu
        self.ms2 = 0.0                           # puissance audio moyenne (niveau en dBFS)
        self.n_recv = 0                          # échantillons reçus depuis le dernier état
        self.images = ImageStore(DATA / "images", self._file_label) if self.mode.get("kind") == "img" else None
        self.rec = None                          # enregistrement audio en cours : {"w", "path", "fs", "left"}

    def _file_label(self):
        src = self.app.src
        f = src.chan_freq(self) if src else self.freq
        return f"{self.mode['id']}-{f / 1000:.1f}kHz".replace(".", ",")

    def describe(self):
        src = self.app.src
        return {"id": self.id, "mode": self.mode["id"], "freq": src.chan_freq(self) if src else self.freq,
                "params": self.params, "paused": self.paused, "state": self.state,
                "bw": bandwidth(self.mode, self.params), "af": self.af, **self.extra}

    def set_state(self, state, **extra):
        self.state = state
        self.extra.update(extra)
        self.app.loop.call_soon_threadsafe(self.app.chan_update, self)

    def reset_decoder(self):
        dec, self.decoder = self.decoder, None
        if dec is not None and hasattr(dec, "close"):
            dec.close()

    def move_decoder(self, af):
        """Petit réaccord : le décodeur se déplace dans l'audio déjà reçu, sans être recréé."""
        dec = self.decoder
        if dec is None:
            return
        try:
            self.exec.submit(dec.set_af, float(af))
        except RuntimeError:
            pass

    def _spectrum(self, pcm, fs):
        """Mini-spectre du canal (≈5 par seconde), centré sur le décodeur, très résolu."""
        n = len(pcm)
        r = self.ring
        if n >= len(r):
            r[:] = pcm[-len(r):]
        else:
            r[:-n] = r[n:]
            r[-n:] = pcm
        self._spec_n += n
        if self._spec_n < fs / 5 or not self.app.clients:
            return
        self._spec_n = 0
        src = self.app.src
        if src is None:
            return
        af = src.decoder_af(self)
        if self.mode.get("whole"):
            a0, a1 = 100.0, 3100.0
        else:
            half = max(400.0, 3 * bandwidth(self.mode, self.params))
            a0, a1 = max(0.0, af - half), min(fs / 2, af + half)
        win = np.hanning(len(r))
        sp = (np.abs(np.fft.rfft(r * win)) / win.sum()) ** 2       # puissance normalisée (pleine échelle = 0 dB)
        f = np.fft.rfftfreq(len(r), 1 / fs)
        sel = (f >= a0) & (f <= a1)
        v = 10 * np.log10(sp[sel] + 1e-12)
        m = 300                                    # au plus 300 points
        if len(v) > m:
            v = v[: len(v) // (len(v) // m) * (len(v) // m)].reshape(-1, len(v) // m).max(axis=1)
        v = np.clip((v + 140) * 1.8, 0, 255).astype(np.uint8)
        base = src.chan_freq(self) - af            # fréquence affichée de l'audio 0 Hz
        dec = self.decoder
        mk = base + float(dec.status().get("af", af)) if dec is not None and not self.mode.get("whole") else base + af
        pkt = struct.pack("<B8sddd", 2, self.id.encode()[:8].ljust(8), base + a0, base + a1, mk) + v.tobytes()
        self.app.loop.call_soon_threadsafe(self.app.broadcast, pkt)

    # ------------------------------------------------------------ enregistrement de l'audio du canal
    def start_record(self, seconds=180):
        """Enregistre l'audio tel que le décodeur le reçoit (WAV 16 bits) dans ~/Orsat-Decoder/enregistrements/."""
        if self.rec:
            return
        folder = DATA / "enregistrements"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (time.strftime("%Y-%m-%d_%H%M%S") + f"_{self._file_label()}.wav")
        self.rec = {"w": None, "path": path, "fs": None, "left": float(seconds)}
        self.extra["rec"] = True
        self.app.chan_update(self)

    def stop_record(self, why=""):
        r, self.rec = self.rec, None
        if not r:
            return
        if r["w"] is not None:
            r["w"].close()
            txt = f"Audio enregistré : {r['path']}" + (f" ({why})" if why else "")
            log.info(txt)
        else:
            txt = "Aucun audio reçu : rien n'a été enregistré."
        self.extra["rec"] = False
        self.app.loop.call_soon_threadsafe(self.app.chan_update, self)
        self.app.loop.call_soon_threadsafe(self.app.broadcast, {"t": "notice", "level": "", "text": txt})

    def _record(self, pcm, fs):
        r = self.rec
        if r["w"] is None:
            r["w"] = wave.open(str(r["path"]), "wb")
            r["w"].setnchannels(1)
            r["w"].setsampwidth(2)
            r["w"].setframerate(int(fs))
            r["fs"] = fs
        if fs != r["fs"]:
            self.stop_record("changement de fréquence d'échantillonnage")
            return
        r["w"].writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())
        r["left"] -= len(pcm) / fs
        if r["left"] <= 0:
            self.stop_record()

    def feed(self, pcm, fs):
        """Appelé depuis la boucle principale ou un fil de décodage audio (FLAC)."""
        self.last_audio = time.time()
        if self.rec and len(pcm):
            try:
                self._record(np.asarray(pcm, np.float32), fs)
            except Exception as e:
                log.warning("enregistrement audio : %s", e)
                self.rec = None
        self.n_recv += len(pcm)
        if len(pcm):
            self.ms2 = 0.8 * self.ms2 + 0.2 * float(np.mean(pcm * pcm))
        if self.app.listen == self.id and self.app.clients:
            # écoute : l'audio du canal part vers l'interface (int16, à sa fréquence d'origine)
            pkt = struct.pack("<B8sI", 3, self.id.encode()[:8].ljust(8), int(fs)) + \
                (np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes()
            self.app.loop.call_soon_threadsafe(self.app.broadcast, pkt)
        if self.paused:
            return
        if self.decoder is None or fs != self.fs:
            self.fs = fs
            # « _src » : type de source, pour les décodeurs qui doivent défaire un traitement du serveur
            self.decoder = self.mode["make"](fs, self.app.src.decoder_af(self), {**self.params, "_src": self.app.src.kind})
            if self.mode.get("kind") == "ident":
                self.decoder.rf = self.app.src.chan_freq(self)     # fréquence radio, si la source la connaît
            if self.state in ("connexion", "reconnexion"):
                self.set_state("écoute")
        try:
            self._spectrum(pcm, fs)
        except Exception as e:
            log.debug("spectre canal : %s", e)
        dec = self.decoder
        try:
            fut = self.exec.submit(dec.process, pcm)
        except RuntimeError:                      # canal fermé pendant qu'un bloc audio arrivait
            return
        fut.add_done_callback(lambda f: self.app.loop.call_soon_threadsafe(self._done, f, dec))

    def shift_image(self, x):
        """Recalage manuel de l'image (fax) : fait dans le fil du décodeur, entre deux blocs audio."""
        dec = self.decoder
        if dec is None or not hasattr(dec, "shift_x"):
            return
        try:
            fut = self.exec.submit(dec.shift_x, float(x))
        except RuntimeError:
            return
        fut.add_done_callback(lambda f: self.app.loop.call_soon_threadsafe(self._done, f, dec))

    def _done(self, fut, dec):
        try:
            events = fut.result()
        except Exception as e:
            log.exception("décodeur %s : %s", self.mode["label"], e)
            return
        for ev in events:
            ev = {**ev, "ch": self.id}
            if ev.get("t") == "ident":
                self._ident(ev)
                continue
            if ev.get("t") == "img" and self.images is not None:
                self.app.broadcast(ev)
                extra = self.images.handle(ev)
                if extra:
                    log.info("image enregistrée : %s", extra["path"])
                    self.app.broadcast({**extra, "ch": self.id})
                continue
            self.history.append(ev)
            if len(self.history) > 400:
                del self.history[:100]
            self.app.broadcast(ev)
        now = time.time()
        if now - self._status_t > 0.5 and dec is self.decoder:
            self._status_t = now
            st = {k: _num(v) for k, v in dec.status().items()}
            st["dbfs"] = round(10 * np.log10(self.ms2 + 1e-12), 1)
            self.app.broadcast({"t": "cstat", "ch": self.id, **st})

    def _ident(self, ev):
        """Résultat d'identification : fréquence du signal trouvé, puis ouverture du mode si demandé."""
        src = self.app.src
        base = src.chan_freq(self) - src.decoder_af(self)          # fréquence de l'audio 0 Hz
        opn = ev.get("open")
        if opn:
            mode = BY_ID[opn["mode"]]
            opn["freq"] = base if mode.get("whole") else base + opn["af"]
        fc = (ev.get("measure") or {}).get("fc")
        for c in ev.get("candidates", []):             # candidats décodables : de quoi ouvrir leur mode
            v = c.get("variant")
            if v and fc is not None and v["mode"] in BY_ID:
                c["open"] = {"mode": v["mode"], "params": v["params"],
                             "freq": base if BY_ID[v["mode"]].get("whole") else base + fc}
        self.history = [h for h in self.history if h.get("t") != "ident"] + [ev]
        self.app.broadcast(ev)
        if ev.get("phase") == "fin" and opn and self.params.get("auto", True):
            c = ev["confirmed"]
            log.info("identifié : %s à %.1f kHz", c["label"], opn["freq"] / 1000)
            new = self.app._add_channel(opn["mode"], freq=opn["freq"], params=opn["params"])
            note = {"t": "text", "text": f"[Identifié : {c['label']}]\n{c['text']}\n", "ch": new.id}
            new.history.append(note)
            self.app.broadcast(note)
            self.app.broadcast({"t": "notice", "level": "",
                                "text": f"Signal identifié : {c['label']}. Canal ouvert."})
            asyncio.ensure_future(self.app.remove_channel(self.id))

    def stop(self):
        self.stop_record("canal fermé")
        if self.decoder is not None and hasattr(self.decoder, "close"):
            self.decoder.close()
        self.exec.shutdown(wait=False, cancel_futures=True)
        if self.images is not None:
            self.images.handle({"op": "end"})         # image en cours : enregistrée telle quelle


class App:
    def __init__(self, args):
        self.args = args
        self.conf = load_conf()
        self.clients = set()
        self.channels = {}
        self.src = None
        self.http = None
        self.loop = None
        self.quit = asyncio.Event()
        self.had_client = False
        self.last_client = time.time()
        self.listen = None                       # canal écouté dans l'interface

    async def audio_watch(self):
        """Signale les canaux qui ne reçoivent plus d'audio (serveur muet, connexion refusée…)."""
        while True:
            await asyncio.sleep(2)
            now = time.time()
            for ch in list(self.channels.values()):
                if ch.paused:
                    continue
                silent = now - ch.last_audio > 5
                if silent and ch.state == "écoute":
                    ch.state = "pas d'audio reçu"
                    self.chan_update(ch)
                elif not silent and ch.state == "pas d'audio reçu":
                    ch.state = "écoute"
                    self.chan_update(ch)

    # ------------------------------------------------------------------ diffusion
    def broadcast(self, msg):
        if not self.clients:
            return
        data = msg if isinstance(msg, bytes) else json.dumps(msg, ensure_ascii=False, default=_num)
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

    def wf_line(self, d):
        if not self.clients:
            return
        if d.get("raw"):
            b = np.asarray(d["bins"], np.uint8)
        else:
            b = (np.clip(d["bins"], -128, 127).astype(np.int16) + 128).astype(np.uint8)
        self.broadcast(struct.pack("<Bdd", 1, d["freq0"], d["freq1"]) + b.tobytes())

    def source_changed(self, src):
        if src is self.src:
            self.broadcast({"t": "source", "info": src.summary()})

    def chan_update(self, ch):
        if ch.id in self.channels:
            self.broadcast({"t": "chan", **ch.describe()})

    def src_conf(self, sid=None):
        sid = sid or self.conf["source"]
        for s in self.conf["sources"]:
            if s["id"] == sid:
                return s
        return self.conf["sources"][0]

    # ------------------------------------------------------------------ source et canaux
    async def connect_source(self):
        if self.src:
            await self.src.stop()
        for ch in list(self.channels.values()):
            ch.stop()
        self.channels.clear()
        self.broadcast({"t": "chans_reset"})
        conf = self.src_conf()
        self.conf["source"] = conf["id"]
        self.src = make_source(self, conf)
        self.broadcast({"t": "source", "info": self.src.summary()})
        await self.src.start()
        rx = conf.get("rx")
        for c in self.conf.get("channels", []):
            if len(self.channels) >= MAX_CHANNELS:
                break
            if c.get("source") == conf["id"] and c.get("rx") == rx and c.get("mode") in BY_ID:
                self._add_channel(c["mode"], freq=c.get("freq", 0), af=c.get("af"), params=c.get("params"),
                                  paused=c.get("paused", False), save=False)

    def _add_channel(self, mode_id, freq=None, af=None, params=None, paused=False, save=True):
        cid = uuid.uuid4().hex[:8]
        ch = Channel(self, cid, mode_id, freq or 0, af, params, paused)
        if self.src.shared and af is None and freq is not None:
            self.src.set_chan_freq(ch, freq)
        if ch.mode.get("whole") and self.src.shared:
            ch.af = 0.0
        self.channels[cid] = ch
        self.src.attach(ch)
        self.chan_update(ch)
        if save:
            self._save_channels()
        return ch

    async def remove_channel(self, cid):
        ch = self.channels.pop(cid, None)
        if ch:
            await self.src.detach(ch)
            ch.stop()
            self.broadcast({"t": "chan_removed", "ch": ch.id})
            self._save_channels()

    def _save_channels(self):
        conf = self.src_conf()
        rx = conf.get("rx")
        others = [c for c in self.conf.get("channels", []) if not (c.get("source") == conf["id"] and c.get("rx") == rx)]
        mine = [{"source": conf["id"], "rx": rx, "mode": ch.mode["id"], "freq": ch.freq, "af": ch.af,
                 "params": ch.params, "paused": ch.paused} for ch in self.channels.values()
                if ch.mode.get("kind") != "ident"]            # un canal d'identification ne se garde pas
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
        src = self.src
        if t == "add":
            rep = m.get("replace")                     # canal remplacé (identification -> mode trouvé)
            if m.get("mode") in BY_ID and await self._room(ws, rep):
                self._add_channel(m["mode"], freq=float(m["freq"]), params=m.get("params"))
                if rep in self.channels:
                    await self.remove_channel(rep)
        elif t == "preset":
            await self._preset(ws, m)
        elif t == "remove":
            await self.remove_channel(m.get("ch"))
        elif t == "retune":
            ch = self.channels.get(m.get("ch"))
            if ch:
                if m.get("params") is not None:
                    ch.params.update(m["params"])
                if m.get("freq") is not None:
                    src.set_chan_freq(ch, float(m["freq"]))
                if m.get("params") is not None or not await src.retune(ch):
                    ch.reset_decoder()
                else:
                    ch.move_decoder(src.decoder_af(ch))
                self.chan_update(ch)
                self._save_channels()
        elif t == "pause":
            ch = self.channels.get(m.get("ch"))
            if ch:
                ch.paused = bool(m.get("paused"))
                ch.reset_decoder()
                self.chan_update(ch)
                self._save_channels()
        elif t == "record":
            ch = self.channels.get(m.get("ch"))
            if ch:
                if m.get("on"):
                    ch.start_record(float(m.get("seconds") or 180))
                else:
                    ch.stop_record("arrêté")
        elif t == "imgshift":
            ch = self.channels.get(m.get("ch"))
            if ch and m.get("x") is not None:
                ch.shift_image(m["x"])
        elif t == "listen":
            self.listen = m.get("ch") or None
        elif t == "view":
            await src.set_view(float(m["f0"]), float(m["f1"]))
        elif t == "qsy":
            ok = await src.qsy(float(m["freq"])) if src.shared else False
            if not ok:
                await self._send(ws, json.dumps({"t": "notice", "level": "error",
                                                 "text": "Cette source ne permet pas de changer de fréquence (pas de CAT)."}))
        elif t == "select_source":
            if any(s["id"] == m.get("id") for s in self.conf["sources"]):
                self.conf["source"] = m["id"]
                save_conf(self.conf)
                await self.connect_source()
        elif t == "select_rx":
            self.src_conf()["rx"] = m.get("rx") or None
            save_conf(self.conf)
            await self.connect_source()
        elif t == "sources_set":
            new = []
            for s in m.get("sources", []):
                if s.get("type") not in TYPES:
                    continue
                s = {k: v for k, v in s.items() if isinstance(v, (str, int, float, bool)) or v is None}
                s["id"] = s.get("id") or uuid.uuid4().hex[:6]
                new.append(s)
            if new:
                old_cur = json.dumps(self.src_conf(), sort_keys=True)
                self.conf["sources"] = new
                if not any(s["id"] == self.conf["source"] for s in new):
                    self.conf["source"] = new[0]["id"]
                save_conf(self.conf)
                self.broadcast({"t": "sources", "sources": new, "current": self.conf["source"]})
                if json.dumps(self.src_conf(), sort_keys=True) != old_cur:
                    await self.connect_source()
        elif t == "detect":
            url = str(m.get("url") or "").strip()
            kind, name = await detect_type(self.http, url) if url else (None, None)
            await self._send(ws, json.dumps({"t": "detected", "url": url, "type": kind, "name": name}))
        elif t == "audio_inputs":
            await self._send(ws, json.dumps({"t": "audio_inputs", "inputs": await asyncio.to_thread(list_audio_inputs)}))
        elif t == "bookmarks":
            bms = []
            for b in m.get("list", []):
                try:
                    if b.get("mode") in BY_ID and float(b["freq"]) > 0:
                        bms.append({"label": str(b.get("label") or "")[:60], "mode": b["mode"], "freq": float(b["freq"]),
                                    "ribbon": bool(b.get("ribbon", True)),
                                    **({"params": b["params"]} if isinstance(b.get("params"), dict) else {})})
                except (TypeError, ValueError, KeyError):
                    continue
            self.conf["bookmarks"] = bms
            save_conf(self.conf)
            self.broadcast({"t": "bookmarks", "list": bms})
        elif t == "bookmarks_reset":
            from .modes import PRESETS
            self.conf["bookmarks"] = [{"label": p["label"], "mode": p["mode"], "freq": p["freq"], "ribbon": True,
                                       **({"params": p["params"]} if p.get("params") else {})} for p in PRESETS]
            save_conf(self.conf)
            self.broadcast({"t": "bookmarks", "list": self.conf["bookmarks"]})
        elif t == "prefs":
            self.conf.setdefault("ui", {}).update(m.get("prefs", {}))
            save_conf(self.conf)
        elif t == "quit":
            self.quit.set()

    async def _room(self, ws, replace=None):
        """Place pour un canal de plus ? (le canal remplacé ne compte pas)"""
        if sum(1 for cid in self.channels if cid != replace) < MAX_CHANNELS:
            return True
        await self._send(ws, json.dumps({"t": "notice", "level": "error",
            "text": f"{MAX_CHANNELS} canaux au maximum : fermez-en un pour en ouvrir un autre."}))
        return False

    async def _preset(self, ws, m):
        """Fréquence connue : canal direct (PhantomSDR), ou réaccord du récepteur puis canal (TCI, CAT)."""
        mode = BY_ID.get(m.get("mode"))
        if not mode:
            return
        f = float(m["freq"])
        src = self.src
        if not await self._room(ws):
            return
        if not src.shared:
            self._add_channel(mode["id"], freq=f, params=m.get("params"))
            return
        dial = f if mode.get("whole") else f - mode["af"]
        if await src.qsy(dial, mode.get("demod", "usb").lower()):
            self._add_channel(mode["id"], af=0.0 if mode.get("whole") else float(mode["af"]), params=m.get("params"))
        else:
            await self._send(ws, json.dumps({"t": "notice", "level": "error",
                "text": f"Pas de contrôle CAT sur cette source : réglez le récepteur sur {dial / 1000:.1f} kHz "
                        f"en {mode.get('demod', 'USB').upper()}, "
                        f"puis cliquez sur le signal dans le waterfall."}))

    async def hello(self, ws):
        await self._send(ws, json.dumps({
            "t": "hello", "version": VERSION_LABEL, "catalog": public_catalog(), "types": TYPES,
            "sources": self.conf["sources"], "current": self.conf["source"],
            "source": self.src.summary() if self.src else {}, "ui": self.conf.get("ui", {}),
            "bookmarks": self.conf.get("bookmarks", []),
            "channels": [ch.describe() for ch in self.channels.values()],
        }, ensure_ascii=False, default=_num))
        for ch in self.channels.values():
            for ev in ch.history[-150:]:
                await self._send(ws, json.dumps(ev, ensure_ascii=False, default=_num))
            if ch.images is not None:
                for ev in ch.images.replay():
                    await self._send(ws, json.dumps({**ev, "ch": ch.id}))


def make_web(app):
    w = web.Application()

    async def index(request):
        # app.js et style.css portent une marque de version : après une mise à jour, le navigateur
        # ne peut pas garder l'ancienne copie en cache avec la nouvelle page
        html = (WEB / "index.html").read_text(encoding="utf-8")
        for name in ("style.css", "app.js"):
            stamp = int((WEB / name).stat().st_mtime)
            html = html.replace(f'"{name}"', f'"{name}?v={stamp}"')
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def no_cache(request, response):
        if not request.path.startswith("/vendor/"):
            response.headers.setdefault("Cache-Control", "no-cache")

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
    w.on_response_prepare.append(no_cache)
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
    log.info("Orsat-Decoder %s : %s", VERSION_LABEL, url)
    await app.connect_source()
    tasks = [asyncio.create_task(app.idle_watch()), asyncio.create_task(app.audio_watch())]
    browser = open_window(url, args)
    if browser is not None and "--app=" in " ".join(map(str, browser.args)):
        async def wait_browser():
            await asyncio.to_thread(browser.wait)
            app.quit.set()
        tasks.append(asyncio.create_task(wait_browser()))
    await app.quit.wait()
    if app.src:
        await app.src.stop()
    for ch in list(app.channels.values()):
        ch.stop()
    for t in tasks:
        t.cancel()
    await app.http.close()
    await runner.cleanup()
    log.info("terminé")


def main():
    ap = argparse.ArgumentParser(prog="orsat-decoder", description="Décodeur multimode pour PhantomSDR, TCI, carte son")
    ap.add_argument("--port", type=int, default=8074, help="port de l'interface (défaut 8074)")
    ap.add_argument("--lan", action="store_true", help="interface accessible depuis le réseau local")
    ap.add_argument("--no-browser", action="store_true", help="ne pas ouvrir de fenêtre")
    ap.add_argument("--no-autoquit", action="store_true", help="ne pas s'arrêter quand la fenêtre est fermée")
    ap.add_argument("--probe", nargs="+", metavar=("ADRESSE", "FREQ_KHZ"),
                    help="diagnostic : se connecte à un serveur PhantomSDR, mesure l'audio reçu et l'enregistre")
    ap.add_argument("--identify", nargs="+", metavar=("FICHIER.wav", "FREQ_KHZ"),
                    help="identifie le signal d'un enregistrement audio (base de signaux intégrée)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"Orsat-Decoder {VERSION_LABEL} — F1NSK et Claude AI — GPL-3")
    args = ap.parse_args()
    if args.identify:
        from .signal_id import identify_file
        raise SystemExit(identify_file(args.identify[0], float(args.identify[1]) if len(args.identify) > 1 else None))
    if args.probe:
        from .probe import probe
        raise SystemExit(asyncio.run(probe(args.probe[0], float(args.probe[1]) * 1000 if len(args.probe) > 1 else None)))
    DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(), logging.FileHandler(DATA / "orsat-decoder.log", "w")])
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
