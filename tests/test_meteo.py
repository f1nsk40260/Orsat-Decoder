"""Essais de la transcription des bulletins météo RTTY (orsatdec/meteo.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orsatdec.meteo import translate, Transcriber  # noqa: E402

CASES = [
    ("FEMM74 EDZW 060600", "en-tête : prévisions à moyenne échéance, Méditerranée, émis par DWD Offenbach, le 06 à 06 h 00 UTC"),
    ("GOLFE-LION (42.7N   4.5E) SST: 23 C", "zone golfe du Lion (42,7° N, 4,5° E), température de la mer 23 °C"),
    ("FR  9. 00Z: NW-N     7  9-10   4  M //", "vendredi 9 à 00 h UTC : vent nord-ouest à nord force 7, rafales 9 à 10, vagues 4 m"),
    # chiffres reçus en lettres (retour lettres après espace) : réparés
    ("FR  9. PPZ: NW-N     7  OAQP   R  M //", "vendredi 9 à 00 h UTC : vent nord-ouest à nord force 7, rafales 9 à 10, vagues 4 m"),
    ("WE  7. PPZ: SW-W   0-2        PMT M //", "mercredi 7 à 00 h UTC : vent sud-ouest à ouest force 0 à 2, vagues 0,5 m"),
    ("ADRIA-S (41.6N  17.7E) SST: WE C", "zone Adriatique sud (41,6° N, 17,7° E), température de la mer 23 °C"),
    ("DWD FORECAST OF TU/06/10.2026 PP UTC:", "prévision du DWD du mardi 06/10/2026, 00 h UTC"),
    ("ZCZC 891", "début du message n° 891"),
    ("CQ CQ DE F1NSK F1NSK PSE K", None),
]


def test_meteo():
    bad = 0
    for line, want in CASES:
        got = translate(line)
        ok = got == want
        bad += not ok
        print(("OK   " if ok else "ÉCHEC"), line, "\n       →", got)
    ev = Transcriber().feed("ZCZC 891\r\nNNNN\r\n")
    assert [e["t"] for e in ev] == ["text", "tr", "text", "tr", "text"], ev
    return bad == 0


if __name__ == "__main__":
    sys.exit(0 if test_meteo() else 1)
