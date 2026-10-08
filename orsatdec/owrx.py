"""Client du protocole WebSocket d'OpenWebRX (/ws/).

Chaque connexion est un « client » du serveur : elle reçoit la FFT de la bande du profil en cours et,
si on démarre son démodulateur, un flux audio. On ouvre :
- OwrxWaterfall : une connexion sans démodulateur, pour le waterfall, la configuration et les profils ;
- OwrxAudio : une connexion par canal, démodulateur accordé dans la bande du profil.

La bande reçue est fixée par le profil (center_freq ± samp_rate/2). Changer de profil change la bande
pour tous les auditeurs du même récepteur.
"""
import asyncio
import json
import logging
from urllib.parse import urlparse

import aiohttp
import numpy as np

from .adpcm import ImaAdpcm

log = logging.getLogger("orsat.owrx")

AUDIO_RATE = 12000
FFT_PAD = 10
# démodulation Orsat -> (mode OpenWebRX, passe-bande autour de la fréquence envoyée)
MODES = {"USB": ("usb", 100, 3000), "CWN": ("usb", 600, 1400), "CW": ("usb", 100, 3000), "LSB": ("lsb", -3000, -100),
         "AM": ("am", -5000, 5000), "FM": ("nfm", -5500, 5500)}


def ws_url(server):
    u = urlparse(server if "://" in server else "http://" + server)
    scheme = "wss" if u.scheme in ("https", "wss") else "ws"
    port = f":{u.port}" if u.port else ""
    path = (u.path or "/").rstrip("/") + "/ws/"
    return f"{scheme}://{u.hostname}{port}{path}"


class _OwrxConn:
    def __init__(self, server, session=None, on_state=None):
        self.server = server
        self.on_state = on_state or (lambda s, info=None: None)
        self.config = {}
        self.ws = None
        self._session = session
        self._own_session = session is None
        self._task = None
        self._closing = False
        self._fft = ImaAdpcm()

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

    async def send(self, obj):
        if self.ws is not None and not self.ws.closed:
            await self.ws.send_str(obj if isinstance(obj, str) else json.dumps(obj))

    @property
    def band(self):
        c = self.config
        if "center_freq" not in c or "samp_rate" not in c:
            return None
        return float(c["center_freq"]) - c["samp_rate"] / 2, float(c["samp_rate"])

    async def _run(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        delay = 1.0
        while not self._closing:
            try:
                async with self._session.ws_connect(ws_url(self.server), heartbeat=20, max_msg_size=0) as ws:
                    self.ws = ws
                    self.config = {}
                    self._on_connect()
                    await self.send("SERVER DE CLIENT client=openwebrx.js type=receiver")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._text(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY and msg.data:
                            self._binary(msg.data[0], msg.data[1:])
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                    delay = 1.0
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.info("openwebrx %s : %s", self.server, e)
                self.on_state("error", {"error": str(e) or e.__class__.__name__})
            if self._closing:
                return
            self.on_state("reconnecting")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)

    async def _text(self, data):
        if data.startswith("CLIENT DE SERVER"):
            await self.send({"type": "connectionproperties",
                             "params": {"output_rate": AUDIO_RATE, "hd_output_rate": 4 * AUDIO_RATE}})
            return
        try:
            m = json.loads(data)
        except ValueError:
            return
        t, v = m.get("type"), m.get("value")
        if t == "config" and isinstance(v, dict):
            old = self.band
            self.config.update(v)
            await self._on_config(old != self.band)
        elif t == "profiles" and isinstance(v, list):
            self._on_profiles(v)
        elif t == "backoff":
            self.on_state("error", {"error": "serveur plein (trop d'auditeurs)"})
        elif t == "sdr_error":
            self.on_state("error", {"error": f"erreur du récepteur : {v}"})

    def _on_connect(self):
        pass

    async def _on_config(self, band_changed):
        pass

    def _on_profiles(self, profiles):
        pass

    def _binary(self, kind, data):
        pass


class OwrxWaterfall(_OwrxConn):
    def __init__(self, server, profile=None, on_line=None, on_info=None, on_state=None, session=None):
        super().__init__(server, session, on_state)
        self.profile = profile
        self.on_line = on_line or (lambda d: None)
        self.on_info = on_info or (lambda i: None)
        self.profiles = []
        self.info = None
        self._asked = False

    def _on_connect(self):
        self._asked = False

    async def set_view(self, f0, f1):
        pass                                      # la FFT couvre toujours toute la bande du profil

    def current_profile(self):
        c = self.config
        return f"{c['sdr_id']}|{c['profile_id']}" if c.get("sdr_id") and c.get("profile_id") else None

    async def _on_config(self, band_changed):
        cur = self.current_profile()
        if self.profile and cur and cur != self.profile and not self._asked:
            self._asked = True                     # une seule demande par connexion
            await self.send({"type": "selectprofile", "params": {"profile": self.profile}})
        if self.band and (band_changed or self.info is None):
            f0, bw = self.band
            self.info = {"basefreq": f0, "total_bandwidth": bw, "fft_size": self.config.get("fft_size"),
                         "profile": cur, "profiles": self.profiles}
            self.on_info(self.info)
            self.on_state("connected", self.info)
        elif self.info is not None and self.info.get("profile") != cur:
            self.info["profile"] = cur
            self.on_info(self.info)

    def _on_profiles(self, profiles):
        self.profiles = [{"id": p.get("id"), "name": p.get("name") or p.get("id")} for p in profiles if p.get("id")]
        if self.info is not None:
            self.info["profiles"] = self.profiles
            self.on_info(self.info)

    def _binary(self, kind, data):
        if kind != 1 or not self.band:
            return
        if self.config.get("fft_compression", "adpcm") == "adpcm":
            self._fft.reset()
            db = self._fft.decode(data)[FFT_PAD:].astype(np.float32) / 100.0
        else:
            db = np.frombuffer(data[:len(data) // 4 * 4], "<f4")
        if not len(db):
            return
        f0, bw = self.band
        v = np.clip((db + 130.0) * 2.0, 0, 255).astype(np.uint8)
        self.on_line({"freq0": f0, "freq1": f0 + bw, "bins": v, "raw": True})


class OwrxAudio(_OwrxConn):
    """Flux audio d'un canal. on_pcm(np.float32[], fs)."""

    def __init__(self, server, freq, mode="USB", on_pcm=None, on_state=None, session=None):
        super().__init__(server, session, on_state)
        self.freq, self.mode = float(freq), mode.upper()
        self.on_pcm = on_pcm or (lambda x, fs: None)
        self.info = None
        self.packets = 0
        self._adpcm = ImaAdpcm()
        self._started = False

    async def retune(self, freq=None, mode=None):
        if freq is not None:
            self.freq = float(freq)
        if mode is not None:
            self.mode = mode.upper()
        await self._tune()

    def in_band(self):
        b = self.band
        return b is None or b[0] <= self.freq <= b[0] + b[1]

    async def _tune(self):
        b = self.band
        if not b:
            return
        if not self.in_band():
            self.on_state("error", {"error": f"{self.freq / 1e3:.1f} kHz est hors de la bande du serveur "
                                             f"({b[0] / 1e3:.0f} à {(b[0] + b[1]) / 1e3:.0f} kHz) : changez de bande"})
            return
        mod, lo, hi = MODES.get(self.mode, MODES["USB"])
        center = float(self.config["center_freq"])
        await self.send({"type": "dspcontrol", "params": {
            "low_cut": lo, "high_cut": hi, "offset_freq": int(round(self.freq - center)), "mod": mod,
            "squelch_level": -150}})
        if not self._started:
            self._started = True
            await self.send({"type": "dspcontrol", "action": "start"})
            self.on_state("connected", self.info)

    def _on_connect(self):
        self._started = False
        self._adpcm.reset()

    async def _on_config(self, band_changed):
        if self.band:
            self.info = {"band": self.band}
            if band_changed or not self._started:
                await self._tune()

    def _binary(self, kind, data):
        if kind != 2:
            return
        if self.config.get("audio_compression", "adpcm") == "adpcm":
            x = self._adpcm.decode_sync(data)
        else:
            x = np.frombuffer(data[:len(data) // 2 * 2], "<i2")
        if len(x):
            self.packets += 1
            self.on_pcm(x.astype(np.float32) / 32768.0, AUDIO_RATE)
