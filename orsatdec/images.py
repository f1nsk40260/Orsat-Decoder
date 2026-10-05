"""Images reçues (fax, SSTV, Hell) : état courant de chaque canal et enregistrement PNG.

Le serveur garde l'image en cours pour la renvoyer entière à une interface qui se connecte,
et enregistre chaque image terminée dans ~/Orsat-Decoder/images/.
"""
import base64
import struct
import time
import zlib
from pathlib import Path

TAPE_MAX = 6000            # colonnes de bande Hell conservées


def png_bytes(w, h, data, rgb):
    """PNG sans dépendance (niveaux de gris 8 bits ou RVB 24 bits)."""
    bpp = 3 if rgb else 1
    raw = b"".join(b"\0" + data[y * w * bpp:(y + 1) * w * bpp] for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2 if rgb else 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


class ImageStore:
    def __init__(self, folder: Path, label):
        self.folder = folder
        self.label = label          # fonction () -> "sstv-14230kHz" pour le nom de fichier
        self.cur = None             # {"w","h","fmt","title","tape","buf","rows"}

    def handle(self, ev):
        """Met à jour l'état ; renvoie un éventuel événement supplémentaire (image enregistrée)."""
        op = ev.get("op")
        if op == "new":
            self._save()
            self.cur = {k: ev.get(k) for k in ("w", "h", "fmt", "title", "tape")}
            self.cur.update(buf=bytearray(), rows=0, t0=time.time())
        elif self.cur is None:
            return None
        elif op == "rows":
            c = self.cur
            bpp = 3 if c["fmt"] == "rgb" else 1
            line = c["w"] * bpp
            data = base64.b64decode(ev["data"])
            a = ev["y"] * line
            if len(c["buf"]) < a + len(data):
                c["buf"].extend(bytes(a + len(data) - len(c["buf"])))
            c["buf"][a:a + len(data)] = data
            c["rows"] = max(c["rows"], ev["y"] + ev["n"])
        elif op == "cols":
            c = self.cur
            c["buf"] += base64.b64decode(ev["data"])
            excess = len(c["buf"]) - TAPE_MAX * c["h"]
            if excess > 0:
                del c["buf"][:excess - excess % c["h"]]
        elif op == "end":
            path = self._save()
            self.cur = None
            if path:
                return {"t": "img", "op": "saved", "path": str(path), "name": path.name}
        return None

    def _save(self):
        c = self.cur
        if not c or c.get("tape") or c["rows"] < 16:
            return None
        rgb = c["fmt"] == "rgb"
        h = c["rows"]
        data = bytes(c["buf"][:h * c["w"] * (3 if rgb else 1)])
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            name = time.strftime("%Y-%m-%d_%H%M", time.localtime(c["t0"])) + f"_{self.label()}.png"
            path = self.folder / name
            path.write_bytes(png_bytes(c["w"], h, data, rgb))
            return path
        except OSError:
            return None
        finally:
            c["rows"] = 0

    def replay(self):
        """Événements qui redessinent l'image courante pour une interface qui arrive."""
        c = self.cur
        if not c:
            return []
        out = [{"t": "img", "op": "new", **{k: c[k] for k in ("w", "h", "fmt", "title", "tape")}}]
        if c.get("tape"):
            if c["buf"]:
                out.append({"t": "img", "op": "cols", "n": len(c["buf"]) // c["h"],
                            "data": base64.b64encode(bytes(c["buf"])).decode()})
        elif c["rows"]:
            bpp = 3 if c["fmt"] == "rgb" else 1
            out.append({"t": "img", "op": "rows", "y": 0, "n": c["rows"],
                        "data": base64.b64encode(bytes(c["buf"][:c["rows"] * c["w"] * bpp])).decode()})
        return out
