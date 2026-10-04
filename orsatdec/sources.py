"""Sources de signal d'Orsat-Decoder.

Deux familles :

- PhantomSource : un serveur PhantomSDR-Plus / Orsat-SDR. Waterfall large bande fourni par le serveur ;
  chaque canal ouvre son propre flux audio, accordé indépendamment.
- Sources « audio partagé » (TciSource, PulseSource) : un seul flux audio (la sortie BLU d'un récepteur),
  partagé par tous les canaux, chacun calé sur sa propre fréquence audio. Le waterfall est calculé ici,
  sur cet audio. Le contrôle CAT (TCI intégré, ou rigctld pour l'entrée audio) donne la fréquence du
  récepteur pour afficher les fréquences réelles, et permet de le réaccorder.

Interface commune : start/stop, attach/detach/retune d'un canal, summary() pour l'interface.
"""
import asyncio
import json
import logging
import re
import shutil
import struct
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

from .phantom import AudioChannel, Waterfall

log = logging.getLogger("orsat.sources")

TYPES = {
    "phantom": "PhantomSDR / Orsat-SDR",
    "tci": "TCI (AetherSDR, ExpertSDR, Thetis…)",
    "audio": "Entrée audio (PipeWire / PulseAudio)",
}


def _host(url):
    u = urlparse(url if "://" in url else "http://" + url)
    return u.hostname, u.port


class Source:
    kind = "?"
    shared = False

    def __init__(self, app, conf):
        self.app, self.conf = app, conf
        self.id = conf["id"]
        self.name = conf.get("name") or TYPES.get(conf.get("type"), "?")
        self.channels = {}
        self.connected = False
        self.error = None

    async def start(self):
        pass

    async def stop(self):
        pass

    def attach(self, ch):
        self.channels[ch.id] = ch

    async def detach(self, ch):
        self.channels.pop(ch.id, None)

    async def retune(self, ch):
        """Applique une nouvelle fréquence de canal. Renvoie True si le décodeur peut simplement se
        déplacer dans l'audio (pas de nouveau flux), False s'il faut le recréer."""
        return True

    async def set_view(self, f0, f1):
        pass

    def summary(self):
        return {"id": self.id, "kind": self.kind, "name": self.name, "shared": self.shared,
                "connected": self.connected, "error": self.error}

    # fréquence affichée d'un canal, et réglage depuis l'interface
    def chan_freq(self, ch):
        return ch.freq

    def set_chan_freq(self, ch, f):
        ch.freq = float(f)

    def decoder_af(self, ch):
        return ch.mode["af"] or 1500

    def changed(self):
        self.app.source_changed(self)


# =====================================================================================
# PhantomSDR-Plus / Orsat-SDR
# =====================================================================================
class PhantomSource(Source):
    kind = "phantom"

    def __init__(self, app, conf):
        super().__init__(app, conf)
        self.url = conf.get("url", "")
        self.rx = conf.get("rx") or None
        self.wf = None
        self.info = None
        self.streams = {}            # id canal -> AudioChannel
        self.codec = None

    def is_local(self):
        return _host(self.url)[0] in ("127.0.0.1", "localhost", "::1")

    def tap_token(self):
        """Sur la machine du serveur, le jeton .tap_token d'Orsat-SDR fait des canaux des clients
        internes (comme le client autorun) : ils ne comptent pas comme auditeurs."""
        if self.conf.get("tap"):
            return self.conf["tap"]
        if not self.is_local():
            return None
        for d in ("Orsat-SDR", "orsat-sdr", "PhantomSDR-Plus", "PhantomSDR-Plus-FR", "phantomsdr"):
            try:
                return (Path.home() / d / ".tap_token").read_text().strip() or None
            except OSError:
                continue
        return None

    async def start(self):
        self.wf = Waterfall(self.url, rx=self.rx, on_line=self.app.wf_line, on_info=self._on_info,
                            session=self.app.http)
        self.wf.start()

    async def stop(self):
        if self.wf:
            await self.wf.close()
        for a in list(self.streams.values()):
            await a.close()
        self.streams.clear()

    def _on_info(self, info):
        self.info = info
        self.connected = True
        self.changed()

    # Chaque canal reçoit une bande audio BLU de 3 kHz commençant à ch.dial. Tant que le signal reste
    # dans cette bande, on déplace seulement le décodeur ; sinon on réaccorde le flux du serveur.
    LO, HI = 150.0, 2850.0

    def decoder_af(self, ch):
        if ch.mode.get("whole"):
            return 1500
        if ch.dial is None:
            ch.dial = ch.freq - ch.mode["af"]
        return ch.freq - ch.dial

    def attach(self, ch):
        super().attach(ch)
        ch.dial = ch.freq if ch.mode.get("whole") else ch.freq - ch.mode["af"]
        a = AudioChannel(self.url, ch.dial, "USB", rx=self.rx, tap=self.tap_token(),
                         on_pcm=ch.feed, on_state=lambda s, i=None, ch=ch: self._on_state(ch, s, i),
                         session=self.app.http, ask_pcm=self.conf.get("pcm", True))
        self.streams[ch.id] = a
        a.start()

    async def detach(self, ch):
        await super().detach(ch)
        a = self.streams.pop(ch.id, None)
        if a:
            await a.close()

    async def retune(self, ch):
        a = self.streams.get(ch.id)
        if ch.mode.get("whole"):
            ch.dial = ch.freq
        else:
            af = ch.freq - (ch.dial if ch.dial is not None else ch.freq - ch.mode["af"])
            if self.LO <= af <= self.HI:
                return True                       # le signal est encore dans l'audio reçu
            ch.dial = ch.freq - ch.mode["af"]
        if a:
            await a.retune(ch.dial)
        return False

    def _on_state(self, ch, state, info):
        if state == "codec":
            self.codec = info.get("codec")
            ch.set_state("écoute", codec=self.codec)
            self.changed()
        elif state == "connected":
            ch.set_state("écoute", error=None)
        elif state == "reconnecting":
            ch.set_state("reconnexion")
        elif state == "error":
            ch.set_state("erreur", error=(info or {}).get("error"))

    async def set_view(self, f0, f1):
        if self.wf:
            await self.wf.set_view(f0, f1)

    def summary(self):
        i = self.info or {}
        return {**super().summary(), "url": self.url, "local": self.is_local(), "internal": bool(self.tap_token()),
                "basefreq": i.get("basefreq"), "total_bandwidth": i.get("total_bandwidth"),
                "rx": i.get("rx"), "rx_name": i.get("rx_name"), "receivers": i.get("receivers") or [],
                "codec": self.codec, "can_qsy": False}


# =====================================================================================
# Audio partagé : base commune (waterfall local, distribution aux canaux, fréquence du récepteur)
# =====================================================================================
class SharedSource(Source):
    shared = True
    NFFT = 2048

    def __init__(self, app, conf):
        super().__init__(app, conf)
        self.fs = None
        self.dial = None             # fréquence du récepteur (Hz, porteuse BLU) si connue
        self.mode = None
        self.can_qsy = False
        self.ring = np.zeros(self.NFFT, np.float32)
        self.since = 0
        self.level = 0.0

    def chan_freq(self, ch):
        return (self.dial or 0) + ch.af

    def set_chan_freq(self, ch, f):
        ch.af = float(f) - (self.dial or 0)

    def decoder_af(self, ch):
        return ch.af if not ch.mode.get("whole") else 1500

    def audio(self, x, fs):
        """Un bloc d'audio reçu : distribué à tous les canaux, et ajouté au waterfall."""
        if fs != self.fs:
            self.fs = fs
            self.changed()
        if not self.connected:
            self.connected = True
            self.error = None
            self.changed()
        for ch in list(self.channels.values()):
            ch.feed(x, fs)
        self.level = 0.95 * self.level + 0.05 * float(np.sqrt(np.mean(x * x))) if len(x) else self.level
        n = len(x)
        if n >= self.NFFT:
            self.ring[:] = x[-self.NFFT:]
        else:
            self.ring[:-n] = self.ring[n:]
            self.ring[-n:] = x
        self.since += n
        if self.since >= fs / 10:                     # 10 lignes par seconde
            self.since = 0
            win = np.hanning(self.NFFT)
            sp = (np.abs(np.fft.rfft(self.ring * win)) / win.sum()) ** 2
            db = 10 * np.log10(sp[:-1] + 1e-14)
            v = np.clip((db + 140) * 1.8, 0, 255).astype(np.uint8)
            base = self.dial or 0
            self.app.wf_line({"freq0": base, "freq1": base + fs / 2, "bins": v, "raw": True})

    async def qsy(self, freq, mode=None):
        return False

    def set_dial(self, f):
        """Le récepteur a changé de fréquence : chaque canal reste sur sa station (même fréquence
        réelle), donc sa position dans l'audio change. FT8/FT4 suivent toute la bande audio."""
        if f == self.dial:
            return
        old, self.dial = self.dial, f
        for ch in self.channels.values():
            if old is not None and not ch.mode.get("whole"):
                ch.af = (old + ch.af) - f
                ch.move_decoder(ch.af)
            self.app.chan_update(ch)
        self.changed()

    def summary(self):
        fs = self.fs or 12000
        return {**super().summary(), "basefreq": self.dial or 0, "total_bandwidth": fs / 2,
                "dial": self.dial, "nodial": self.dial is None, "mode": self.mode, "can_qsy": self.can_qsy,
                "receivers": [], "fs": self.fs}


# =====================================================================================
# TCI (AetherSDR, ExpertSDR / SunSDR, Thetis, SDC…)
# =====================================================================================
class TciSource(SharedSource):
    kind = "tci"
    FORMATS = {0: ("<i2", 32768.0), 1: (None, 8388608.0), 2: ("<i4", 2147483648.0), 3: ("<f4", 1.0)}

    def __init__(self, app, conf):
        super().__init__(app, conf)
        url = conf.get("url") or "ws://127.0.0.1:50001"
        if "://" not in url:
            url = "ws://" + url
        self.url = url.replace("http://", "ws://").replace("https://", "wss://")
        self.trx = int(conf.get("trx", 0))
        self.ws = None
        self.task = None
        self.device = None
        self.closing = False
        self.can_qsy = True

    async def start(self):
        self.task = asyncio.ensure_future(self._run())

    async def stop(self):
        self.closing = True
        if self.ws is not None and not self.ws.closed:
            try:
                await self.ws.send_str(f"audio_stop:{self.trx};")
            except Exception:
                pass
            await self.ws.close()
        if self.task:
            self.task.cancel()

    async def _send(self, s):
        if self.ws is not None and not self.ws.closed:
            await self.ws.send_str(s)

    async def _start_audio(self):
        for cmd in ("audio_samplerate:12000;", "audio_stream_sample_type:float32;",
                    "audio_stream_channels:1;", f"audio_start:{self.trx};"):
            await self._send(cmd)

    async def _run(self):
        import aiohttp
        delay = 1.0
        while not self.closing:
            try:
                async with self.app.http.ws_connect(self.url, heartbeat=20, max_msg_size=0) as ws:
                    self.ws = ws
                    self.error = None
                    started = False
                    loop = asyncio.get_running_loop()
                    t0 = loop.time()
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            ready = self._text(msg.data)
                            if not started and (ready or loop.time() - t0 > 1.5):
                                started = True
                                await self._start_audio()
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            if not started:
                                started = True
                                await self._start_audio()
                            self._binary(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                    delay = 1.0
            except asyncio.CancelledError:
                return
            except Exception as e:
                self.error = f"TCI {self.url} : {e}"
                log.info(self.error)
            self.connected = False
            self.changed()
            if self.closing:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)

    def _text(self, data):
        ready = False
        for cmd in data.split(";"):
            cmd = cmd.strip()
            if not cmd:
                continue
            name, _, args = cmd.partition(":")
            name = name.lower()
            a = args.split(",")
            if name == "ready":
                ready = True
            elif name == "device":
                self.device = args
            elif name == "vfo" and len(a) >= 3 and a[0] == str(self.trx) and a[1] == "0":
                try:
                    self.set_dial(float(a[2]))
                except ValueError:
                    pass
            elif name == "dds" and len(a) >= 2 and a[0] == str(self.trx) and self.dial is None:
                try:
                    self.set_dial(float(a[1]))
                except ValueError:
                    pass
            elif name == "modulation" and len(a) >= 2 and a[0] == str(self.trx):
                if a[1].lower() != self.mode:
                    self.mode = a[1].lower()
                    self.changed()
        return ready

    def _binary(self, data):
        if len(data) < 64:
            return
        receiver, rate, fmt, codec, crc, length, typ, channels = struct.unpack_from("<8I", data, 0)
        if typ != 1 or receiver != self.trx:          # 1 = audio de réception
            return
        payload = data[64:]
        if fmt == 1:                                    # int24
            b = np.frombuffer(payload[: len(payload) // 3 * 3], np.uint8).reshape(-1, 3)
            v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
            x = ((v << 8) >> 8).astype(np.float32) / 8388608.0
        else:
            dt, scale = self.FORMATS.get(fmt, ("<f4", 1.0))
            size = np.dtype(dt).itemsize
            x = np.frombuffer(payload[: len(payload) // size * size], dt).astype(np.float32) / scale
        ch = max(1, channels)
        if ch > 1:
            x = x[: len(x) // ch * ch].reshape(-1, ch)[:, 0]
        self.audio(np.ascontiguousarray(x), rate or 12000)

    async def qsy(self, freq, mode=None):
        await self._send(f"vfo:{self.trx},0,{int(round(freq))};")
        if mode:
            await self._send(f"modulation:{self.trx},{mode};")
        self.set_dial(float(freq))
        return True

    def summary(self):
        return {**super().summary(), "url": self.url, "device": self.device}


# =====================================================================================
# Entrée audio PipeWire / PulseAudio (+ CAT rigctld facultatif)
# =====================================================================================
def list_audio_inputs():
    """Entrées audio disponibles : [{name, desc}], moniteurs de sortie compris."""
    out = []
    try:
        r = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True, text=True, timeout=5)
        for s in json.loads(r.stdout or "[]"):
            out.append({"name": s.get("name"), "desc": s.get("description") or s.get("name")})
        if out:
            return out
    except Exception:
        pass
    try:
        r = subprocess.run(["pactl", "list", "sources"], capture_output=True, text=True, timeout=5)
        for block in re.split(r"\n(?=\S)", r.stdout):
            n = re.search(r"^\s*Name:\s*(.*)$", block, re.M)
            d = re.search(r"^\s*Description:\s*(.*)$", block, re.M)
            if n:
                out.append({"name": n.group(1).strip(), "desc": (d.group(1) if d else n.group(1)).strip()})
    except Exception:
        pass
    return out


class Rigctl:
    """Client rigctld (Hamlib, protocole réseau). Port 4532 par défaut (AetherSDR, rigctld, flrig…)."""

    def __init__(self, hostport):
        hostport = (hostport or "").strip()
        host, sep, port = hostport.rpartition(":")
        if sep and port.isdigit():
            self.host, self.port = host or "127.0.0.1", int(port)
        else:
            self.host, self.port = hostport or "127.0.0.1", 4532
        self.reader = self.writer = None
        self.lock = asyncio.Lock()

    async def _cmd(self, line):
        async with self.lock:
            if self.writer is None:
                self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 3)
            self.writer.write((line + "\n").encode())
            await self.writer.drain()
            return (await asyncio.wait_for(self.reader.readline(), 3)).decode().strip()

    async def freq(self):
        r = await self._cmd("f")
        return float(r)

    async def set_freq(self, f):
        return await self._cmd(f"F {int(round(f))}")

    async def set_mode(self, mode):
        return await self._cmd(f"M {mode} 0")

    def close(self):
        if self.writer:
            self.writer.close()
        self.reader = self.writer = None


class PulseSource(SharedSource):
    kind = "audio"
    RATE = 12000

    def __init__(self, app, conf):
        super().__init__(app, conf)
        self.device = conf.get("device") or ""
        self.rig = Rigctl(conf["rigctl"]) if conf.get("rigctl") else None
        self.can_qsy = self.rig is not None
        self.proc = None
        self.tasks = []
        self.closing = False

    def _command(self):
        if shutil.which("parec"):
            cmd = ["parec", "--rate", str(self.RATE), "--channels", "1", "--format", "float32le", "--latency-msec", "60"]
            return cmd + (["--device", self.device] if self.device else [])
        if shutil.which("pw-record"):
            cmd = ["pw-record", "--rate", str(self.RATE), "--channels", "1", "--format", "f32"]
            return cmd + (["--target", self.device] if self.device else []) + ["-"]
        return None

    async def start(self):
        self.tasks = [asyncio.ensure_future(self._capture())]
        if self.rig:
            self.tasks.append(asyncio.ensure_future(self._poll_cat()))

    async def stop(self):
        self.closing = True
        for t in self.tasks:
            t.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
        if self.rig:
            self.rig.close()

    async def _capture(self):
        while not self.closing:
            cmd = self._command()
            if not cmd:
                self.error = "ni parec ni pw-record : installez pulseaudio-utils ou pipewire-bin"
                self.changed()
                return
            try:
                self.proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                                 stderr=asyncio.subprocess.PIPE)
                block = self.RATE // 10 * 4
                while True:
                    data = await self.proc.stdout.readexactly(block)
                    self.audio(np.frombuffer(data, "<f4").copy(), self.RATE)
            except asyncio.CancelledError:
                return
            except asyncio.IncompleteReadError:
                why = ""
                try:
                    why = (await asyncio.wait_for(self.proc.stderr.read(), 1)).decode(errors="replace").strip().splitlines()[-1]
                except Exception:
                    pass
                self.error = f"l'entrée audio « {self.device or 'par défaut'} » s'est arrêtée" + (f" ({why})" if why else "")
            except Exception as e:
                self.error = f"entrée audio : {e}"
            self.connected = False
            self.changed()
            if self.closing:
                return
            await asyncio.sleep(2)

    async def _poll_cat(self):
        while not self.closing:
            try:
                self.set_dial(await self.rig.freq())
                if self.error and self.error.startswith("CAT"):
                    self.error = None
                    self.changed()
            except asyncio.CancelledError:
                return
            except Exception as e:
                self.rig.close()
                msg = f"CAT rigctld {self.rig.host}:{self.rig.port} injoignable"
                if self.error != msg:
                    self.error = msg
                    self.changed()
                await asyncio.sleep(4)
            await asyncio.sleep(1)

    async def qsy(self, freq, mode=None):
        if not self.rig:
            return False
        try:
            await self.rig.set_freq(freq)
            if mode:
                await self.rig.set_mode(mode.upper())
            self.set_dial(float(freq))
            return True
        except Exception:
            self.rig.close()
            return False

    def summary(self):
        return {**super().summary(), "device": self.device, "rigctl": self.conf.get("rigctl") or ""}


def make_source(app, conf):
    return {"phantom": PhantomSource, "tci": TciSource, "audio": PulseSource}.get(conf.get("type"), PhantomSource)(app, conf)
