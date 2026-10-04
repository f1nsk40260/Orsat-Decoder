# Orsat-Decoder

Décodeur multimode natif Linux. Un seul logiciel, sans Wine, qui se branche sur plusieurs types de sources.

## Sources

Le menu **Source**, en haut à gauche, choisit d'où vient le signal. Les sources se règlent dans les réglages (bouton en forme de roue).

| Source | Pour | Fonctionnement |
|---|---|---|
| **PhantomSDR / Orsat-SDR** | ton serveur ORSAT, Orsat-SDR, tout PhantomSDR-Plus | waterfall large bande du serveur ; chaque canal a son propre flux audio et son propre accord. Audio PCM, FLAC ou Opus. |
| **TCI** | AetherSDR (port 50001), ExpertSDR / SunSDR, Thetis… | audio et fréquence du récepteur sur une seule connexion ; le champ de fréquence en haut réaccorde le récepteur. |
| **Entrée audio** | carte son du transceiver, sortie de n'importe quel logiciel (son « Monitor »), entrées DAX d'AetherSDR | CAT rigctld facultatif (port 4532 : AetherSDR, rigctld, flrig) pour afficher les vraies fréquences et réaccorder. |

Avec TCI et l'entrée audio, tous les canaux partagent l'audio BLU du récepteur, et le waterfall montre cette bande audio. Quand on réaccorde le récepteur, chaque canal reste sur sa station. Sans CAT, les fréquences affichées sont les fréquences audio (Hz).

## Installation

```bash
git clone https://github.com/f1nsk40260/Orsat-Decoder.git
cd Orsat-Decoder
./install.sh
```

Mise à jour : `cd Orsat-Decoder && git pull && ./install.sh`.

L'installateur :
- installe Python et un compilateur C s'ils manquent (sudo demandé une fois) ;
- crée son environnement Python dans `~/.local/share/orsat-decoder` ;
- compile les décodeurs natifs embarqués ;
- ajoute **Orsat-Decoder** au menu des applications ;
- termine par un autotest de chaque décodeur.

Relancer `./install.sh` met à jour en gardant la configuration et les canaux.

## Utilisation

1. Choisissez la source en haut à gauche.
2. Choisissez un mode dans la colonne de gauche, puis **cliquez sur un signal** dans le waterfall : un canal s'ouvre. Chaque décodeur retrouve seul son signal dans environ ±100 Hz autour du clic.
3. Ou ouvrez directement une **fréquence connue** : Navtex 518 et 490 kHz, météo DWD, FT8 sur toutes les bandes… Avec TCI ou CAT, le récepteur est réaccordé automatiquement.

Dans le waterfall :
- **molette : accorde le canal actif**, par pas de 10 Hz (Maj : 1 Hz, Alt : 100 Hz) ;
- Ctrl+molette : zoom ;
- glisser : se déplacer dans la bande ;
- glisser un marqueur de canal : réaccorder ce canal ;
- si la résolution est trop faible pour viser (plus de 20 Hz par pixel), un premier clic zoome autour du signal, le second ouvre le canal.

Chaque carte de canal a un **mini-spectre** très résolu (environ 1 Hz par pixel), centré sur le décodeur. Le trait de couleur marque la fréquence demandée, le pointillé blanc l'endroit où le décodeur s'est calé. Cliquez sur le signal, ou tournez la molette sur le mini-spectre ou sur la fréquence, pour un accord précis. Les petits déplacements se font dans l'audio déjà reçu, sans coupure. La carte active (la dernière touchée) est entourée de sa couleur.

Dans une carte de canal : fréquence modifiable au clavier, paramètres du mode, pause, enregistrement du texte, et bouton **Écouter** pour entendre l'audio que reçoit ce décodeur. La ligne du bas affiche le niveau audio reçu (dBFS). Si rien n'arrive pendant 5 s, la carte passe en rouge « pas d'audio reçu » et indique la raison donnée par le serveur.

| Commande | Effet |
|---|---|
| `orsat-decoder` | lance l'application |
| `orsat-decoder --check` | autotest des décodeurs (sans serveur) |
| `orsat-decoder --lan` | interface accessible depuis le réseau local (`http://ip:8074/`) |
| `orsat-decoder --log` | journal de la dernière session |
| `orsat-decoder --probe URL FREQ_kHz` | diagnostic de connexion : ce que le serveur envoie, niveau, enregistrement de 12 s dans `~/orsat-probe.wav` |

Les serveurs Orsat-SDR récents refusent les clients qui ne déclarent pas leur version (`min_client_version`). Orsat-Decoder la déclare (`?v=2`) automatiquement quand le serveur l'exige.

## Modes du jalon 1

| Mode | Décodeur | Sensibilité mesurée (bruit dans 2500 Hz) |
|---|---|---|
| PSK31 / 63 / 125 | écrit pour Orsat-Decoder | ≈ −11 / −8 / −5 dB |
| RTTY (45 à 100 bd, shift réglable) | écrit pour Orsat-Decoder | ≈ −8 dB à 45 bd |
| CW, vitesse automatique 5-60 mpm | écrit pour Orsat-Decoder | ≈ −8 dB à 20 mpm |
| Navtex / SITOR-B | écrit pour Orsat-Decoder | ≈ −8 dB |
| FT8 / FT4 | ft8_lib (MIT), embarqué | ≈ −18 dB |

Comparaison faite sur les mêmes enregistrements avec MultiPSK : Orsat-Decoder est meilleur en PSK31, CW et Navtex, et à moins de 1 dB en RTTY.

## Limite de connexions

Chaque canal est un auditeur pour le serveur. Un PhantomSDR-Plus limite en général à **3 auditeurs par adresse IP** (`per_ip` dans `config.toml`).

Lancé **sur la machine du serveur**, Orsat-Decoder n'a pas cette limite. S'il trouve le fichier `.tap_token` d'Orsat-SDR (dans `~/Orsat-SDR`), ses canaux sont même comptés comme clients internes, comme le client autorun, et n'apparaissent pas parmi les auditeurs.

## Pour le développement

- `tests/selftest.py` : autotest rapide.
- `tests/harness.py` : banc de mesure de sensibilité.
- `tests/bandgen.py` : génère une bande HF synthétique en IQ pour alimenter un `spectrumserver` (driver `stdin`, `f32`, `iq`) et tester tout le logiciel sans antenne.
- `tests/fake_tci.py` : faux serveur TCI (audio et VFO) pour tester la source TCI sans AetherSDR.
- `orsatdec/sources.py` : les sources ; `orsatdec/codecs.py` : décodage FLAC et Opus.
- `orsatdec/decoders/` : un fichier par famille de décodeurs.
- `orsatdec/modes.py` : catalogue des modes et fréquences connues.
