"""Client du protocole WebSocket de PhantomSDR-Plus / Orsat-SDR.

- AudioChannel : un flux /audio en PCM brut (int16), accordé sur une fréquence et une démodulation ;
  même méthode que le client interne autorun/audiotap.js du serveur.
- Waterfall : le flux /waterfall (zstd continu + CBOR), lignes d'octets sur la bande demandée.

Toutes les fréquences sont en Hz « affichées » (basefreq inclut un éventuel décalage LNB).
"""
import asyncio
import json
import logging
from urllib.parse import urlencode

import aiohttp
import cbor2
import numpy as np
import zstandard

from .codecs import FlacStream, OpusStream

log = logging.getLogger("orsat.phantom")

# Bande passante audio extraite autour de la fréquence d'accord, par démodulation (Hz)
SPANS = {"USB": (0, 3000), "LSB": (-3000, 0), "AM": (-5000, 5000), "FM": (-6000, 6000), "CW": (0, 3000)}


CLIENT_VERSION = 2          # marqueur ?v= attendu par les serveurs qui imposent min_client_version


def ws_url(server, path, rx=None, tap=None, version=None):
    base = server.rstrip("/")
    if base.startswith("http://"):
        base = "ws://" + base[7:]
    elif base.startswith("https://"):
        base = "wss://" + base[8:]
    elif not base.startswith(("ws://", "wss://")):
        base = "ws://" + base
    q = {}
    if rx:
        q["rx"] = rx
    if tap:
        q["tap"] = tap
    if version:
        q["v"] = version
    return f"{base}{path}" + (("?" + urlencode(q)) if q else "")


class AudioChannel:
    """Flux audio d'un canal. on_pcm(np.float32[], fs) est appelé pour chaque bloc décodé.

    On demande du PCM brut (Orsat-SDR) ; un serveur qui ne le propose pas (PhantomSDR-Plus) continue
    d'envoyer du FLAC ou de l'Opus, qu'on décode alors ici."""

    def __init__(self, server, freq, mode="USB", rx=None, tap=None, on_pcm=None, on_state=None, session=None,
                 ask_pcm=True):
        self.server, self.freq, self.mode, self.rx, self.tap = server, float(freq), mode.upper(), rx, tap
        self.on_pcm = on_pcm or (lambda x, fs: None)
        self.on_state = on_state or (lambda s, info=None: None)
        self.info = None
        self.sample_rate = None
        self.ask_pcm = ask_pcm
        self.codec = None              # codec effectivement reçu : pcm, flac ou opus
        self._dec = None
        self.with_version = False      # passe à True si le serveur exige ?v= (min_client_version)
        self.packets = 0               # paquets audio reçus (diagnostic)
        self.last_close = None
        self.ws = None
        self._session = session
        self._own_session = session is None
        self._task = None
        self._closing = False

    def start(self):
        self._task = asyncio.ensure_future(self._run())

    async def close(self):
        self._closing = True
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self._task:
            self._task.cancel()
        if self._own_session and self._session:
            await self._session.close()

    def _reset_decoder(self):
        if self._dec is not None:
            try:
                self._dec.close()
            except Exception:
                pass
        self._dec, self.codec = None, None

    def _emit(self, x, fs):
        self.sample_rate = fs
        self.on_pcm(x, fs)

    def _audio(self, codec, data):
        """Paquet audio : PCM int16, morceau de flux FLAC, ou trame Opus."""
        if codec is None:                                 # anciens serveurs : pas de champ codec
            if data[:4] == b"fLaC" or self.codec == "flac":
                codec = "flac"
            else:
                codec = (self.info or {}).get("audio_compression", "flac")
        codec = codec.lower()
        # Le serveur relance son encodeur FLAC à chaque changement de démodulation : un nouvel
        # en-tête « fLaC » arrive alors au milieu du flux, il faut repartir avec un décodeur neuf.
        if codec != self.codec or (codec == "flac" and data[:4] == b"fLaC" and self._dec is not None):
            first = codec != self.codec
            self._reset_decoder()
            self.codec = codec
            if first:
                self.on_state("codec", {"codec": codec})
            try:
                if codec == "flac":
                    self._dec = FlacStream(self._emit)
                elif codec == "opus":
                    self._dec = OpusStream(self._emit, (self.info or {}).get("audio_max_sps", 12000))
            except Exception as e:
                log.warning("décodeur audio %s indisponible : %s", codec, e)
                self.on_state("error", {"error": f"décodeur {codec} indisponible : {e}"})
                self._dec = None
        if codec == "pcm":
            n = len(data) // 2
            self._emit(np.frombuffer(data[:n * 2], "<i2").astype(np.float32) / 32768.0,
                       (self.info or {}).get("audio_max_sps", 12000))
        elif self._dec is not None:
            try:
                self._dec.feed(data)
            except Exception as e:
                # flux FLAC désynchronisé (ex. changement de mode stéréo) : on repart à zéro
                log.debug("audio %s : %s", codec, e)
                self._reset_decoder()

    def _bin(self, f):
        i = self.info
        return (f - i["basefreq"]) / i["total_bandwidth"] * i["fft_result_size"]

    async def retune(self, freq=None, mode=None):
        if freq is not None:
            self.freq = float(freq)
        if mode is not None:
            self.mode = mode.upper()
        await self._tune()

    async def _send(self, obj):
        if self.ws is not None and not self.ws.closed:
            await self.ws.send_str(json.dumps(obj))

    async def _tune(self):
        if not self.info:
            return
        lo, hi = SPANS.get(self.mode, (0, 3000))
        demod = "USB" if self.mode == "CW" else self.mode
        m = self._bin(self.freq)
        l, r = int(np.floor(self._bin(self.freq + lo))), int(np.ceil(self._bin(self.freq + hi)))
        await self._send({"cmd": "demodulation", "demodulation": demod})
        await self._send({"cmd": "window", "l": l, "m": m, "r": r})

    async def _run(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        delay = 1.0
        while not self._closing:
            try:
                # Comme le client web : /audio d'abord ; ?v= seulement si le serveur le demande
                # (les serveurs anciens ne reconnaissent pas /audio?v=…).
                url = ws_url(self.server, "/audio", self.rx, self.tap, CLIENT_VERSION if self.with_version else None)
                async with self._session.ws_connect(url, heartbeat=20, max_msg_size=0) as ws:
                    self.ws = ws
                    got_info = False
                    close_code, close_reason = None, ""
                    while True:
                        msg = await ws.receive()
                        if msg.type == aiohttp.WSMsgType.CLOSE:
                            close_code, close_reason = msg.data, str(msg.extra or "")
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT and not got_info:
                            self.info = json.loads(msg.data)
                            self.sample_rate = self.info.get("audio_max_sps")
                            got_info = True
                            self._reset_decoder()
                            if self.ask_pcm:
                                await self._send({"cmd": "set_codec", "codec": "pcm"})
                            await self._send({"cmd": "agc_enable", "enabled": True})
                            await self._tune()
                            # le serveur ignore une démodulation reçue dans ses 100 premières ms
                            await asyncio.sleep(0.3)
                            await self._tune()
                            self.on_state("connected", self.info)
                            delay = 1.0
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            try:
                                pkt = cbor2.loads(msg.data)
                            except Exception:
                                continue
                            data = pkt.get("data")
                            if not data:
                                continue
                            self.packets += 1
                            self._audio(pkt.get("codec"), bytes(data))
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                            break
                    code, reason = close_code or ws.close_code, close_reason
                    self.last_close = (code, reason)
                    if code == 4003 and "out of date" in reason and not self.with_version:
                        log.info("audio %s : le serveur exige ?v=%d, nouvelle tentative", self.server, CLIENT_VERSION)
                        self.with_version = True
                        delay = 0.2
                    elif code and code >= 4000:
                        msg_txt = reason or f"code {code}"
                        log.warning("audio %s refusé : %s", self.server, msg_txt)
                        self.on_state("error", {"error": f"refusé par le serveur : {msg_txt}"})
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.info("audio %s : %s", self.server, e)
                self.on_state("error", {"error": str(e)})
            self._reset_decoder()
            if self._closing:
                return
            self.on_state("reconnecting")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)


class Waterfall:
    """Flux waterfall. on_line(dict) reçoit {freq0, freq1, bins: list[int]}."""

    def __init__(self, server, rx=None, on_line=None, on_info=None, session=None):
        self.server, self.rx = server, rx
        self.on_line = on_line or (lambda d: None)
        self.on_info = on_info or (lambda i: None)
        self.info = None
        self.ws = None
        self._session = session
        self._own_session = session is None
        self._task = None
        self._closing = False
        self.view = None            # (l, r) en bins de la FFT complète

    def start(self):
        self._task = asyncio.ensure_future(self._run())

    async def close(self):
        self._closing = True
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self._task:
            self._task.cancel()
        if self._own_session and self._session:
            await self._session.close()

    def freq_of_bin(self, b):
        i = self.info
        return i["basefreq"] + b / i["fft_result_size"] * i["total_bandwidth"]

    def bin_of_freq(self, f):
        i = self.info
        return (f - i["basefreq"]) / i["total_bandwidth"] * i["fft_result_size"]

    async def set_view(self, f0, f1):
        """Demande au serveur la portion de bande [f0, f1] Hz (zoom)."""
        if not self.info:
            return
        n = self.info["fft_result_size"]
        l = int(max(0, min(n - 1, self.bin_of_freq(f0))))
        r = int(max(l + 1, min(n, self.bin_of_freq(f1))))
        self.view = (l, r)
        if self.ws is not None and not self.ws.closed:
            await self.ws.send_str(json.dumps({"cmd": "window", "l": l, "r": r}))

    async def _run(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        delay = 1.0
        while not self._closing:
            try:
                url = ws_url(self.server, "/waterfall", self.rx)
                async with self._session.ws_connect(url, heartbeat=20, max_msg_size=0) as ws:
                    self.ws = ws
                    dctx = zstandard.ZstdDecompressor().decompressobj()
                    buf = b""
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            if self.info is None or not buf:
                                try:
                                    self.info = json.loads(msg.data)
                                    self.on_info(self.info)
                                    if self.view:
                                        await ws.send_str(json.dumps({"cmd": "window", "l": self.view[0], "r": self.view[1]}))
                                except ValueError:
                                    pass
                        elif msg.type == aiohttp.WSMsgType.BINARY and self.info:
                            try:
                                buf += dctx.decompress(msg.data)
                            except zstandard.ZstdError:
                                dctx = zstandard.ZstdDecompressor().decompressobj()
                                buf = b""
                                continue
                            # un paquet zstd vidé (flush) contient exactement un objet CBOR
                            try:
                                pkt = cbor2.loads(buf)
                            except Exception:
                                continue
                            buf = b""
                            data = pkt.get("data")
                            if data is None:
                                continue
                            l, r = pkt.get("l", 0), pkt.get("r", 0)
                            bins = np.frombuffer(bytes(data), np.int8).astype(np.int16)
                            self.on_line({"freq0": self.freq_of_bin(l), "freq1": self.freq_of_bin(r),
                                          "bins": bins})
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                    delay = 1.0
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.info("waterfall %s : %s", self.server, e)
            if self._closing:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)
