"""Canal « Identifier » : écoute le signal quelques secondes, l'identifie (orsatdec/signal_id.py),
puis confirme le mode en le décodant. Le résultat part vers l'interface en deux temps :
candidats d'abord (en une seconde environ), confirmation par décodage ensuite.

Événement : {"t": "ident", "phase": "candidats" | "fin", "measure": {...}, "candidates": [...],
             "confirmed": {...} | None, "open": {...} | None}
"open" décrit le canal à ouvrir : mode, paramètres, et position du signal dans l'audio (af, Hz).
"""
import threading
import urllib.parse

import numpy as np

from ..dsp import Decoder

SIGID_URL = "https://www.sigidwiki.com/wiki/"
SPAN = 1450.0          # Hz analysés de part et d'autre du clic


def _clean(m):
    keep = ("f1", "f2", "fc", "bw", "snr", "ntones", "spacing", "baud", "psk", "acf", "envvar", "gaps")
    return {k: (round(float(m[k]), 2) if isinstance(m[k], (float, np.floating)) else int(m[k])) for k in keep}


def _cand(c):
    return {"id": c["id"], "title": c["title"], "score": c["score"], "why": c["why"],
            "decodable": c["decodable"], "variant": c.get("variant"),
            "url": SIGID_URL + urllib.parse.quote(c["title"].replace(" ", "_"), safe=":/()'")}


class Identifier(Decoder):
    name = "Identifier"
    kind = "ident"

    def __init__(self, fs, af=1500.0, seconds=10.0, rf=None):
        super().__init__(fs, af)
        self.seconds = float(seconds)
        self.rf = rf                 # fréquence radio du clic (Hz), si la source la connaît
        self.buf = []
        self.n = 0
        self.long = max(30.0, 2.5 * self.seconds)   # écoute prolongée pour les modes lents (Olivia, MFSK8…)
        self.state = "écoute"
        self.lock = threading.Lock()
        self.out = []
        self.prev = []               # candidats de la première analyse, repris pour l'écoute prolongée
        self.gen = 0                 # change à chaque réaccord : les résultats d'une analyse périmée sont jetés
        self.thread = None

    def status(self):
        p = min(1.0, self.n / (self.fs * (self.long if self.state == "prolonge" else self.seconds)))
        return {"af": round(self.af, 1), "ident": self.state, "progress": round(p, 2)}

    def process(self, x):
        # tout l'audio est gardé (jusqu'à l'écoute prolongée), même pendant une analyse : il reste continu
        if self.state != "terminé" and self.n < self.fs * self.long:
            self.buf.append(np.asarray(x, np.float64))
            self.n += len(x)
        due = (self.state == "écoute" and self.n >= self.fs * self.seconds) or \
              (self.state == "prolonge" and self.n >= self.fs * self.long)
        if due:
            last = self.state == "prolonge"
            self.state = "analyse"
            audio = np.concatenate(self.buf)
            self.buf = [audio]
            self.thread = threading.Thread(target=self._run, args=(audio, self.af, self.gen, last), daemon=True)
            self.thread.start()
        with self.lock:
            out, self.out = self.out, []
        return out

    def set_af(self, af):
        """Réaccord : on recommence l'écoute sur la nouvelle fréquence (une analyse en cours est abandonnée)."""
        super().set_af(af)
        self.gen += 1
        self.buf, self.n, self.state, self.prev = [], 0, "écoute", []

    def close(self):
        self.gen += 1

    def _emit(self, ev, gen):
        with self.lock:
            if gen == self.gen:
                self.out.append(ev)

    def _run(self, audio, af, gen, last=False):
        from ..signal_id import analyse
        from ..modes import BY_ID
        try:
            lo, hi = max(100.0, af - SPAN), min(self.fs / 2 - 100, af + SPAN)
            freq = None
            if self.rf and self.rf > 50e3:
                freq = self.rf

            def first(r):
                self._emit({"t": "ident", "phase": "candidats", "measure": _clean(r["measure"]),
                            "candidates": [_cand(c) for c in r["candidates"]], "confirmed": None, "open": None}, gen)
            r = analyse(audio, self.fs, freq=freq, modes=BY_ID, lo=lo, hi=hi, near=af, on_measure=first,
                        stop=lambda: gen != self.gen, also=self.prev if last else ())
            if r is None:
                self._emit({"t": "ident", "phase": "fin", "measure": None, "candidates": [], "confirmed": None,
                            "open": None, "error": "Pas de signal net autour du clic."}, gen)
            elif (not last and r["confirmed"] is None and len(audio) < self.fs * self.long
                  and any(c["decodable"] for c in r["candidates"][:3])):
                # un mode décodable est probable mais rien n'est sorti : écoute prolongée, l'audio déjà reçu est gardé
                with self.lock:
                    if gen == self.gen:
                        self.state = "prolonge"
                        self.prev = [c for c in r["candidates"][:3] if c["decodable"]]
                self._emit({"t": "ident", "phase": "prolonge", "measure": _clean(r["measure"]),
                            "candidates": [_cand(x) for x in r["candidates"]], "confirmed": None, "open": None,
                            "seconds": self.long}, gen)
                return
            else:
                c = r["confirmed"]
                conf = opn = None
                if c:
                    conf = {"id": c["id"], "mode": c["mode"], "label": BY_ID[c["mode"]]["label"],
                            "params": c["params"], "text": " ".join(c["text"].split())[:300], "score": c["score"]}
                    opn = {"mode": c["mode"], "params": c["params"], "af": round(float(c["af"]), 1)}
                self._emit({"t": "ident", "phase": "fin", "measure": _clean(r["measure"]),
                            "candidates": [_cand(x) for x in r["candidates"]], "confirmed": conf, "open": opn}, gen)
        except Exception as e:              # l'identification ne doit jamais faire tomber le canal
            self._emit({"t": "ident", "phase": "fin", "measure": None, "candidates": [], "confirmed": None,
                        "open": None, "error": f"Erreur d'analyse : {e}"}, gen)
        if gen == self.gen and self.state == "analyse":
            self.state = "terminé"
