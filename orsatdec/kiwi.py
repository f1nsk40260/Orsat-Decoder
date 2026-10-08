"""Client du protocole WebSocket des récepteurs KiwiSDR.

- KiwiAudio : une connexion /<horodatage>/SND par canal (un « utilisateur » du Kiwi), audio BLU en PCM
  16 bits non compressé (ou IMA ADPCM si le serveur l'impose) ;
- KiwiWaterfall : une connexion /<horodatage>/W/F, 1024 points par ligne, zoom de 0 (0-30 MHz) à 14.

Mêmes interfaces que phantom.AudioChannel / phantom.Waterfall (start, close, retune, set_view, info).
"""
import asyncio
import logging
import math
import struct
import time
from urllib.parse import unquote, urlparse

import aiohttp
import numpy as np

from .adpcm import ImaAdpcm

log = logging.getLogger("orsat.kiwi")

MAX_ZOOM, WF_BINS = 14, 1024
IDENT = "Orsat-Decoder"
# démodulation Orsat -> (mode Kiwi, passe-bande en Hz autour de la fréquence envoyée)
MODES = {"USB": ("usb", 100, 3000), "CWN": ("usb", 600, 1400), "CW": ("usb", 100, 3000), "LSB": ("lsb", -3000, -100),
         "AM": ("am", -5000, 5000), "FM": ("nbfm", -5500, 5500)}

_seq = 0


def ws_base(server):
    u = urlparse(server if "://" in server else "http://" + server)
    scheme = "wss" if u.scheme in ("https", "wss") else "ws"
    port = f":{u.port}" if u.port else (":8073" if scheme == "ws" else "")
    return f"{scheme}://{u.hostname}{port}"


def stamp():
    """Horodatage du chemin de connexion (unique pour chaque connexion de ce client)."""
    global _seq
    _seq += 1
    return int(time.time()) * 100 + _seq % 100


def parse_msg(body):
    """Corps d'un message MSG (après « MSG ») -> dict des paramètres clé=valeur."""
    out = {}
    for item in body.decode("utf-8", "replace").strip().split(" "):
        if not item:
            continue
        k, _, v = item.partition("=")
        out[k] = unquote(v)
    return out


def refusal(p):
    """Paramètres MSG qui signifient un refus du serveur -> texte, ou None."""
    if "too_busy" in p:
        return f"serveur plein ({p['too_busy']} canaux occupés)"
    if p.get("badp") not in (None, "0"):
        return "mot de passe refusé"
    if "down" in p:
        return "serveur indisponible (maintenance)"
    if "inactivity_timeout" in p and "expired" in p:
        return "délai d'inactivité du serveur atteint"
    if p.get("redirect"):
        return f"redirigé vers {p['redirect']}"
    return None


class _KiwiConn:
    which = "SND"

    def __init__(self, server, password="", session=None, on_state=None):
        self.server, self.password = server, password or ""
        self.on_state = on_state or (lambda s, info=None: None)
        self.ws = None
        self.info = None
        self._session = session
        self._own_session = session is None
        self._task = None
        self._closing = False
        self._busy = False

    def start(self):
        self._task = asyncio.ensure_future(self._run())

    async def close(self):
        self._closing = True
        if self.ws is not None and not self.ws.closed:
            try:
                await self.ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
        if self._own_session and self._session:
            await self._session.close()

    async def send(self, text):
        if self.ws is not None and not self.ws.closed:
            await self.ws.send_str(text)

    async def _keepalive(self):
        while self.ws is not None and not self.ws.closed:
            await asyncio.sleep(2)
            try:
                await self.send("SET keepalive")
            except Exception:
                return

    async def _run(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        delay = 1.0
        while not self._closing:
            ka = None
            try:
                url = f"{ws_base(self.server)}/{stamp()}/{self.which}"
                async with self._session.ws_connect(url, heartbeat=None, max_msg_size=0) as ws:
                    self.ws = ws
                    self._busy = False
                    await self.send(f"SET auth t=kiwi p={self.password}")
                    await self._opened()
                    ka = asyncio.ensure_future(self._keepalive())
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            await self._message(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                    delay = 1.0
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.info("kiwi %s %s : %s", self.server, self.which, e)
                self.on_state("error", {"error": str(e) or e.__class__.__name__})
            finally:
                if ka:
                    ka.cancel()
            if self._closing:
                return
            self.on_state("reconnecting")
            await asyncio.sleep(30 if self._busy else delay)       # serveur plein : on ne le harcèle pas
            delay = min(delay * 2, 15)

    async def _message(self, data):
        tag, body = data[:3], data[3:]
        if tag == b"MSG":
            p = parse_msg(body[1:])
            why = refusal(p)
            if why:
                self._busy = "too_busy" in p or "down" in p
                self.on_state("error", {"error": why})
                await self.ws.close()
                return
            await self._params(p)
        else:
            await self._data(tag, body)

    async def _opened(self):
        pass

    async def _params(self, p):
        pass

    async def _data(self, tag, body):
        pass


class KiwiAudio(_KiwiConn):
    """Flux audio d'un canal. on_pcm(np.float32[], fs) pour chaque bloc."""
    which = "SND"

    def __init__(self, server, freq, mode="USB", password="", on_pcm=None, on_state=None, session=None):
        super().__init__(server, password, session, on_state)
        self.freq, self.mode = float(freq), mode.upper()
        self.on_pcm = on_pcm or (lambda x, fs: None)
        self.fs = None
        self.smeter = None
        self.packets = 0
        self._adpcm = ImaAdpcm()
        self._setup = False

    async def retune(self, freq=None, mode=None):
        if freq is not None:
            self.freq = float(freq)
        if mode is not None:
            self.mode = mode.upper()
        await self._tune()

    async def _tune(self):
        if not self._setup:
            return
        mod, lo, hi = MODES.get(self.mode, MODES["USB"])
        await self.send(f"SET mod={mod} low_cut={lo} high_cut={hi} freq={self.freq / 1000:.3f}")

    async def _params(self, p):
        if "audio_rate" in p:
            await self.send(f"SET AR OK in={int(float(p['audio_rate']))} out=44100")
        if "sample_rate" in p:
            self.fs = float(p["sample_rate"])
            self.info = {"sample_rate": self.fs}
            self._setup = True
            await self.send("SET squelch=0 max=0")
            await self.send("SET gen=0 mix=-1")
            await self.send("SET compression=0")
            await self.send("SET agc=1 hang=0 thresh=-100 slope=6 decay=1000 manGain=50")
            await self._tune()
            await self.send(f"SET ident_user={IDENT}")
            self.on_state("connected", self.info)

    async def _data(self, tag, body):
        if tag != b"SND" or len(body) < 7 or not self.fs:
            return
        flags = body[0]
        self.smeter = struct.unpack(">H", body[5:7])[0] * 0.1 - 127
        data = body[7:]
        if flags & 0x10:                                   # compressé (IMA ADPCM)
            x = self._adpcm.decode(data)
        else:
            dt = "<i2" if flags & 0x80 else ">i2"
            x = np.frombuffer(data[:len(data) // 2 * 2], dt)
        if flags & 0x08:                                   # IQ stéréo : on garde la voie I
            x = x[::2]
        self.packets += 1
        self.on_pcm(x.astype(np.float32) / 32768.0, int(round(self.fs)))


class KiwiWaterfall(_KiwiConn):
    """Waterfall. on_line({freq0, freq1, bins, raw}) ; info = {basefreq, total_bandwidth, ...}."""
    which = "W/F"

    def __init__(self, server, password="", on_line=None, on_info=None, on_state=None, session=None):
        super().__init__(server, password, session, on_state)
        self.on_line = on_line or (lambda d: None)
        self.on_info = on_info or (lambda i: None)
        self.bw = 30e6
        self.view = None
        self.name = None
        self._ready = False

    def _zoom_for(self, f0, f1):
        span = max(f1 - f0, 1.0)
        z = int(math.floor(math.log2(self.bw / span))) if span < self.bw else 0
        z = max(0, min(MAX_ZOOM, z))
        half = self.bw / 2 ** z / 2
        cf = min(max((f0 + f1) / 2, half), self.bw - half)
        return z, cf

    async def set_view(self, f0, f1):
        self.view = (f0, f1)
        if self._ready:
            z, cf = self._zoom_for(f0, f1)
            await self.send(f"SET zoom={z} cf={cf / 1000:.3f}")

    async def _opened(self):
        self._ready = False
        asyncio.get_event_loop().call_later(2.0, lambda: asyncio.ensure_future(self._setup()))

    async def _setup(self):
        if self._ready or self.ws is None or self.ws.closed:
            return
        self._ready = True
        await self.send("SET maxdb=-10 mindb=-110")
        await self.send("SET wf_speed=3")
        await self.send("SET wf_comp=0")
        await self.set_view(*(self.view or (0, self.bw)))
        await self.send(f"SET ident_user={IDENT}")
        self.info = {"basefreq": 0.0, "total_bandwidth": self.bw, "fft_result_size": WF_BINS << MAX_ZOOM,
                     "name": self.name}
        self.on_info(self.info)
        self.on_state("connected", self.info)

    async def _params(self, p):
        if "bandwidth" in p:
            try:
                self.bw = float(p["bandwidth"])
            except ValueError:
                pass
        if "rx_chan" in p or "name" in p:
            self.name = p.get("name") or self.name
        if "wf_setup" in p:
            await self._setup()

    async def _data(self, tag, body):
        if tag != b"W/F" or len(body) < 13:
            return
        if not self._ready:
            await self._setup()
        x_bin, zf, _seq = struct.unpack("<III", body[1:13])
        data = body[13:]
        if not data:
            return
        z = zf & 0xFFFF
        span = self.bw / 2 ** z
        f0 = x_bin * self.bw / (WF_BINS << MAX_ZOOM)
        dbm = np.frombuffer(data, np.uint8).astype(np.float32) - 255.0
        v = np.clip((dbm + 150.0) * 2.0, 0, 255).astype(np.uint8)
        self.on_line({"freq0": f0, "freq1": f0 + span, "bins": v, "raw": True})
