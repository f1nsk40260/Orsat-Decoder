"""Transcription en français des bulletins météo reçus en RTTY (DWD Pinneberg / Hambourg, etc.).

Traduit ligne par ligne :
  - l'enveloppe des messages (ZCZC, NNNN) et l'en-tête OMM (TTAAii CCCC YYGGgg) ;
  - le bulletin marin DWD à moyenne échéance (« MEDIUM RANGE - WEATHER AND SEA BULLETIN ») :
    zones avec température de la mer, puis lignes « jour date heure: direction force rafales vagues M » ;
  - le reste au moyen d'un glossaire anglais / allemand (expressions courantes des bulletins marins).
Les chiffres reçus en lettres (inversion lettres/chiffres du code Baudot : P=0, Q=1, W=2 … O=9)
sont rétablis dans les champs qui doivent être numériques.
"""
import re

from .tables import BAUDOT_LTRS, BAUDOT_FIGS

# lettre reçue à la place du chiffre (même code Baudot)
_FIGS = {l: f for l, f in zip(BAUDOT_LTRS, BAUDOT_FIGS) if l.isalpha()}


def figs(s):
    """Rétablit les chiffres d'un champ numérique reçu en lettres (« PMT » → « 0.5 »)."""
    return "".join(_FIGS.get(c, c) for c in s)


def num(s):
    """Nombre français (« 0.5 » → « 0,5 ») après réparation ; None si ce n'est pas un nombre."""
    s = figs(s.strip())
    if not re.fullmatch(r"\d+(\.\d+)?(-\d+(\.\d+)?)?", s):
        return None
    return s.replace(".", ",").replace("-", " à ")


# ------------------------------------------------------------------ en-têtes OMM
T1T2 = {
    "SA": "observations d'aérodrome (METAR)", "SP": "observations spéciales d'aérodrome (SPECI)",
    "SM": "observations synoptiques (SYNOP, heures principales)", "SI": "observations synoptiques (SYNOP, heures intermédiaires)",
    "SN": "observations synoptiques (SYNOP, heures non standard)",
    "FT": "prévisions d'aérodrome (TAF longue durée)", "FC": "prévisions d'aérodrome (TAF courte durée)",
    "FE": "prévisions à moyenne échéance", "FQ": "prévisions marines", "FP": "prévisions publiques",
    "FA": "prévisions de zone (aviation)", "WW": "avis et résumé météo", "WO": "avis",
    "WT": "avis de cyclone tropical", "WS": "SIGMET", "WA": "AIRMET",
    "US": "radiosondage (TEMP, partie A)", "UK": "radiosondage (TEMP, partie B)",
    "UL": "radiosondage (TEMP, partie C)", "UE": "radiosondage (TEMP, partie D)",
}
A1A2 = {"MM": "Méditerranée", "DL": "Allemagne", "FR": "France", "EU": "Europe", "NT": "Atlantique Nord",
        "UK": "Royaume-Uni", "IY": "Italie", "SP": "Espagne"}
CCCC = {"EDZW": "DWD Offenbach", "DWHA": "DWD Hambourg (météo marine)", "EGRR": "Met Office Exeter",
        "LFPW": "Météo-France Toulouse", "KWBC": "NWS Washington"}

DAYS = {"MO": "lundi", "TU": "mardi", "WE": "mercredi", "TH": "jeudi", "FR": "vendredi", "SA": "samedi",
        "SU": "dimanche", "DI": "mardi", "MI": "mercredi", "DO": "jeudi", "SO": "dimanche"}

DIRS = {"N": "nord", "NE": "nord-est", "E": "est", "SE": "sud-est", "S": "sud", "SW": "sud-ouest", "W": "ouest",
        "NW": "nord-ouest", "NNE": "nord-nord-est", "ENE": "est-nord-est", "ESE": "est-sud-est",
        "SSE": "sud-sud-est", "SSW": "sud-sud-ouest", "WSW": "ouest-sud-ouest", "WNW": "ouest-nord-ouest",
        "NNW": "nord-nord-ouest", "C": "calme", "V": "variable", "VAR": "variable"}

# zones des bulletins marins DWD (Méditerranée et autres)
AREAS = {"GOLFE-LION": "golfe du Lion", "MALLORCA": "Majorque", "LIGURIAN SEA": "mer Ligure",
         "CORS/SARD.": "Corse-Sardaigne", "TYRRHENIAN SEA": "mer Tyrrhénienne", "ADRIA": "Adriatique",
         "IONIAN SEA": "mer Ionienne", "AEGEAN SEA": "mer Égée", "TAURUS": "côte du Taurus (Turquie)",
         "ALBORAN": "mer d'Alboran", "BALEARIC SEA": "mer des Baléares", "SICILY": "Sicile", "MALTA": "Malte",
         "CRETE": "Crète", "CYPRUS": "Chypre", "LEVANT": "Levant", "TUNISIA": "Tunisie", "LIBYA": "Libye",
         "GERMAN BIGHT": "baie allemande", "DEUTSCHE BUCHT": "baie allemande", "ENGLISH CHANNEL": "Manche",
         "BISCAY": "golfe de Gascogne", "NORTH SEA": "mer du Nord", "BALTIC": "Baltique"}
SIDES = {"N": "nord", "S": "sud", "E": "est", "W": "ouest", "C": "centre", "NE": "nord-est", "NW": "nord-ouest",
         "SE": "sud-est", "SW": "sud-ouest"}


def area_name(raw):
    raw = raw.strip()
    m = re.fullmatch(r"(.+?)-(N|S|E|W|C|NE|NW|SE|SW)", raw)
    base, side = (m.group(1), m.group(2)) if m else (raw, None)
    fr = AREAS.get(base)
    if fr is None:
        return raw.title()
    return f"{fr} {SIDES[side]}" if side else fr


def wind_dir(tok):
    parts = tok.split("-")
    if all(p in DIRS for p in parts):
        return " à ".join(DIRS[p] for p in parts) if len(parts) > 1 else DIRS[parts[0]]
    return None


# ------------------------------------------------------------------ glossaire (expressions d'abord)
GLOSS = [
    # anglais
    ("MEDIUM RANGE - WEATHER AND SEA BULLETIN FOR THE MEDITERRANEAN SEA",
     "bulletin météo et état de la mer à moyenne échéance pour la Méditerranée"),
    ("WEATHER AND SEA BULLETIN", "bulletin météo et état de la mer"), ("MEDIUM RANGE", "moyenne échéance"),
    ("MEDITERRANEAN SEA", "Méditerranée"), ("ISSUED BY", "émis par"),
    ("MARINE WEATHER SERVICE HAMBURG", "le service météo marine de Hambourg"),
    ("GENERAL SYNOPTIC SITUATION", "situation générale"), ("CURRENTLY NOT AVAILABLE", "actuellement indisponible"),
    ("NOT AVAILABLE", "indisponible"), ("WIND FORCE", "force du vent"), ("WAVE HEIGHT", "hauteur des vagues"),
    ("SIGNIFICANT WAVE HEIGHT", "hauteur significative des vagues"), ("SEA STATE", "état de la mer"),
    ("NEAR GALE", "grand frais"), ("STRONG BREEZE", "vent frais"), ("SEVERE GALE", "fort coup de vent"),
    ("VIOLENT STORM", "violente tempête"), ("HURRICANE FORCE", "force ouragan"), ("GALE WARNING", "avis de coup de vent"),
    ("STORM WARNING", "avis de tempête"), ("COLD FRONT", "front froid"), ("WARM FRONT", "front chaud"),
    ("VALID UNTIL", "valable jusqu'à"), ("NO WARNINGS", "pas d'avis en cours"), ("FORECAST OF", "prévision du"),
    ("SEVERE THUNDERSTORMS", "orages violents"), ("HEAVY RAIN", "fortes pluies"),
    ("BEAUFORT", "Beaufort"), ("METRES", "mètres"), ("METRE", "mètres"), ("FORECAST", "prévision"),
    ("WARNINGS", "avis"), ("WARNING", "avis"), ("GALE", "coup de vent"), ("STORM", "tempête"),
    ("GUSTS", "rafales"), ("SQUALLS", "grains"), ("THUNDERSTORMS", "orages"), ("SHOWERS", "averses"),
    ("RAIN", "pluie"), ("HAIL", "grêle"), ("FOG", "brouillard"), ("MIST", "brume"), ("VISIBILITY", "visibilité"),
    ("GOOD", "bonne"), ("MODERATE", "moyenne"), ("POOR", "mauvaise"), ("INCREASING", "forcissant"),
    ("DECREASING", "faiblissant"), ("VEERING", "virant (sens horaire)"), ("BACKING", "revenant (sens antihoraire)"),
    ("LOCALLY", "localement"), ("OCCASIONALLY", "par moments"), ("LATER", "plus tard"), ("VARIABLE", "variable"),
    ("NORTHERLY", "de nord"), ("NORTHEASTERLY", "de nord-est"), ("EASTERLY", "d'est"), ("SOUTHEASTERLY", "de sud-est"),
    ("SOUTHERLY", "de sud"), ("SOUTHWESTERLY", "de sud-ouest"), ("WESTERLY", "d'ouest"), ("NORTHWESTERLY", "de nord-ouest"),
    ("SWELL", "houle"), ("WAVES", "vagues"), ("TROUGH", "thalweg"), ("RIDGE", "dorsale"), ("MOVING", "se déplaçant"),
    ("SLOWLY", "lentement"), ("QUICKLY", "rapidement"), ("EXPECTED", "attendu"), ("TONIGHT", "cette nuit"),
    ("TOMORROW", "demain"), ("TODAY", "aujourd'hui"), ("NIL", "néant"), ("SEA", "mer"), ("WIND", "vent"),
    ("HIGH", "anticyclone"), ("LOW", "dépression"),
    # allemand (DWD, texte en clair)
    ("SEEWETTERBERICHT", "bulletin météo marine"), ("STURMWARNUNG", "avis de tempête"),
    ("STARKWINDWARNUNG", "avis de vent fort"), ("WETTERLAGE", "situation générale"), ("VORHERSAGE", "prévision"),
    ("DEUTSCHE BUCHT", "baie allemande"), ("WESTLICHE OSTSEE", "Baltique occidentale"),
    ("SUEDOST", "sud-est"), ("SÜDOST", "sud-est"), ("SUEDWEST", "sud-ouest"), ("SÜDWEST", "sud-ouest"),
    ("NORDOST", "nord-est"), ("NORDWEST", "nord-ouest"), ("NORD", "nord"), ("SUED", "sud"), ("SÜD", "sud"),
    ("OST", "est"), ("WEST", "ouest"), ("ZUNEHMEND", "forcissant"), ("ABNEHMEND", "faiblissant"),
    ("DREHEND", "tournant"), ("BOEEN", "rafales"), ("BÖEN", "rafales"), ("SICHT", "visibilité"),
    ("MAESSIG", "moyenne"), ("MÄSSIG", "moyenne"), ("SCHLECHT", "mauvaise"), ("NEBEL", "brouillard"),
    ("SCHAUER", "averses"), ("GEWITTER", "orages"), ("REGEN", "pluie"), ("STURM", "tempête"), ("ORKAN", "ouragan"),
    ("TIEF", "dépression"), ("HOCH", "anticyclone"), ("ZEITWEISE", "par moments"), ("STRICHWEISE", "localement"),
    ("SPAETER", "plus tard"), ("SPÄTER", "plus tard"), ("ANFANGS", "d'abord"), ("UHR", "h"), ("BIS", "jusqu'à"),
    ("SEEGANG", "état de la mer"), ("METER", "m"), ("GUT", "bonne"),
    # mots outils (ne comptent pas comme traduction)
    ("AND", "et"), ("OR", "ou"), ("WITH", "avec"), ("FROM", "de"), ("TO", "à"), ("OVER", "sur"), ("THE", ""),
    ("OF", "de"), ("FOR", "pour"), ("IN", "en"), ("AT", "à"), ("BY", "par"), ("UND", "et"), ("IM", "dans le"),
]
_SMALL = {"et", "ou", "avec", "de", "à", "sur", "", "pour", "en", "par", "dans le"}
_GRE = re.compile(r"(?<![A-ZÄÖÜ])(" + "|".join(re.escape(k) for k, _ in GLOSS) + r")(?![A-ZÄÖÜ])")
_GMAP = dict(GLOSS)


def gloss(line):
    """Traduction mot à mot (expressions d'abord) ; None si trop peu de mots ont été reconnus."""
    words = re.findall(r"[A-ZÄÖÜ]{2,}", line)
    if not words:
        return None
    hits = [m.group(1) for m in _GRE.finditer(line)]
    real = [h for h in hits if _GMAP[h] not in _SMALL]
    covered = sum(len(re.findall(r"[A-ZÄÖÜ]{2,}", h)) for h in hits)
    if not real or covered < 0.5 * len(words):
        return None
    out = _GRE.sub(lambda m: _GMAP[m.group(1)], line)
    return re.sub(r"\s{2,}", " ", out).strip()


# ------------------------------------------------------------------ lignes reconnues
RE_HEAD = re.compile(r"^([A-Z]{2})([A-Z]{2})([0-9A-Z]{2}) ([A-Z]{4}) ([0-9A-Z]{6})(?: ([A-Z]{3}))?$")
RE_ZCZC = re.compile(r"^ZCZC\s*([0-9A-Z]*)$")
RE_AREA = re.compile(r"^(.+?)\s*\(\s*([0-9A-Z.]+)\s*N\s+([0-9A-Z.]+)\s*([EW])\s*\)\s*SST:\s*([0-9A-Z]+)\s*C$")
RE_FC = re.compile(r"^([A-Z]{2})\s+([0-9A-Z]{1,2})\.\s+([0-9A-Z]{2})Z:\s*(\S+)\s+(.*)$")
RE_ISSUED = re.compile(r"^([0-9A-Z]{2})\.([0-9A-Z]{2})\.([0-9A-Z]{2}),\s*([0-9A-Z]{2}) UTC:?$")
RE_DWDFC = re.compile(r"^DWD FORECAST OF ([A-Z]{2})/([0-9A-Z]{2})/([0-9A-Z]{2})\.([0-9A-Z]{4}) ([0-9A-Z]{2}) UTC:?$")


def _isnum(s):
    return num(s) is not None


def _lo(s):
    return float(figs(s).split("-")[0])


def _hi(s):
    return float(figs(s).split("-")[-1])


def translate(line):
    """Traduction d'une ligne, ou None (rien d'utile à ajouter)."""
    s = re.sub(r"\s+", " ", line.strip().upper())
    if not s:
        return None
    if s.startswith("NNNN"):
        return "fin du message"
    m = RE_ZCZC.match(s)
    if m:
        return f"début du message n° {figs(m.group(1))}" if m.group(1) else "début du message"
    m = RE_HEAD.match(s)
    if m and (m.group(1) + m.group(2)) and figs(m.group(3)).isdigit() and figs(m.group(5)).isdigit():
        t, a, cccc, dt = m.group(1), m.group(2), m.group(4), figs(m.group(5))
        kind = T1T2.get(t, f"bulletin {t}")
        zone = A1A2.get(a, a)
        who = CCCC.get(cccc, cccc)
        extra = {"RRA": " (rectificatif)", "CCA": " (correction)", "AAA": " (modification)"}.get(m.group(6) or "", "")
        return f"en-tête : {kind}, {zone}, émis par {who}, le {dt[:2]} à {dt[2:4]} h {dt[4:]} UTC{extra}"
    m = RE_AREA.match(s)
    if m:
        lat, lon, sst = num(m.group(2)), num(m.group(3)), num(m.group(5))
        side = "E" if m.group(4) == "E" else "O"
        pos = f" ({lat}° N, {lon}° {side})" if lat and lon else ""
        t = f", température de la mer {sst} °C" if sst else ""
        return f"zone {area_name(m.group(1))}{pos}{t}"
    m = RE_FC.match(s)
    if m and m.group(1) in DAYS:
        return _forecast(m)
    m = RE_ISSUED.match(s)
    if m and all(_isnum(g) for g in m.groups()):
        d, mo, y, h = (figs(g) for g in m.groups())
        return f"émis le {d}/{mo}/20{y} à {h} h UTC"
    m = RE_DWDFC.match(s)
    if m:
        day = DAYS.get(m.group(1), m.group(1))
        d, mo, y, h = (figs(m.group(k)) for k in (2, 3, 4, 5))
        return f"prévision du DWD du {day} {d}/{mo}/{y}, {h} h UTC"
    if s == "WIND FORCE: BEAUFORT, WAVE HEIGHT: METRE":
        return "vent en échelle de Beaufort, hauteur des vagues en mètres"
    return gloss(s)


def _forecast(m):
    day = DAYS[m.group(1)]
    date, hour = figs(m.group(2)), figs(m.group(3))
    d = wind_dir(m.group(4))
    toks = m.group(5).split()
    # vagues : nombre suivi de « M » (ou nombre et M collés, ou un M perdu)
    wave = rest = None
    for i, tk in enumerate(toks):
        if tk == "M" and i > 0 and _isnum(toks[i - 1]):
            wave, rest, toks = num(toks[i - 1]), toks[i + 1:], toks[:i - 1]
            break
    # force puis rafales : les seuls champs numériques restants (les parasites sont ignorés) ;
    # des rafales qui ne dépassent pas la force sont un parasite
    nums = [tk for tk in toks if _isnum(tk)]
    force = num(nums[0]) if nums else None
    gust = num(nums[1]) if len(nums) > 1 else None
    if gust and _hi(nums[0]) >= _lo(nums[1]):
        gust = None
    if not date.isdigit() or not hour.isdigit() or (d is None and force is None):
        return None
    parts = [f"{day} {int(date)} à {hour} h UTC :"]
    if d == "calme":
        parts.append("calme")
    else:
        parts.append(f"vent {d or '(direction illisible)'}" + (f" force {force}" if force else ""))
    if gust:
        parts.append(f"rafales {gust}")
    if wave:
        parts.append(f"vagues {wave} m")
    return parts[0] + " " + ", ".join(parts[1:])


class Transcriber:
    """Reçoit le texte décodé au fil de l'eau ; renvoie la liste d'événements à diffuser :
    le texte, découpé en fin de ligne, suivi de la traduction de chaque ligne terminée."""

    def __init__(self):
        self.line = ""

    def feed(self, text):
        ev = []
        buf = ""
        for ch in text:
            buf += ch
            if ch in "\r\n":
                done, self.line = self.line, ""
                tr = translate(done) if done.strip() else None
                if tr:
                    ev.append({"t": "text", "text": buf})
                    ev.append({"t": "tr", "text": tr})
                    buf = ""
            elif ch == "\b":
                self.line = self.line[:-1]
            else:
                self.line += ch
        if buf:
            ev.append({"t": "text", "text": buf})
        return ev
