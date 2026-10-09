# Orsat-Decoder

![Version](https://img.shields.io/badge/version-Beta%201.0.0-orange) ![Licence](https://img.shields.io/badge/licence-GPL--3.0-blue) ![Linux](https://img.shields.io/badge/plateforme-Linux-lightgrey)

**Décodeur multimode pour Linux**, en réception uniquement. Il reçoit l'audio d'un récepteur web
(PhantomSDR / Orsat-SDR, KiwiSDR, OpenWebRX, UberSDR), d'un récepteur TCI ou de la carte son, et décode
plusieurs canaux en même temps. Il sait aussi reconnaître un signal inconnu et ouvrir le bon décodeur.

![Orsat-Decoder : trois canaux CW, PSK31 et RTTY décodés en même temps sur la bande des 40 m](docs/images/interface.jpg)

## Installation

```bash
curl -fsSL https://raw.githubusercontent.com/f1nsk40260/Orsat-Decoder/main/get.sh | bash
```

La même ligne fait la mise à jour (configuration et canaux conservés).
Désinstallation : `~/Orsat-Decoder/uninstall.sh`.

## Fonctions

- **Plus de 90 modes** en réception (tableau ci-dessous).
- **Plusieurs canaux simultanés**, chacun avec son mode, sa fréquence, son mini-spectre et son texte.
- **Récepteurs web** : PhantomSDR / Orsat-SDR, KiwiSDR, OpenWebRX et UberSDR, avec un flux audio par canal,
  accordé indépendamment dans la bande du serveur.
- **Récepteurs locaux** : TCI (audio et fréquence du récepteur), entrée audio avec CAT rigctld facultatif.
- **Carnet de serveurs** : on ajoute l'adresse d'un récepteur web, son type est reconnu tout seul, et il
  reste dans la liste des sources pour les fois suivantes.
- **Identification automatique** des signaux inconnus, confirmée par décodage.
- **Comparaison avec la bibliothèque de signaux** (583 signaux de la base Artemis) : waterfall et son de référence à côté de votre signal, hors ligne.
- **Mémoires** modifiables, classées par utilisation (amateur, aviation, marine…), affichées sous l'échelle du waterfall.
- **Annuaire** de 257 signaux identifiables avec leurs fréquences connues.
- **Images** (fax, SSTV, Hellschreiber) affichées en direct et enregistrées en PNG.
- **Carte HFDL** des avions et des stations au sol.
- **Interface web locale**, utilisable aussi depuis un autre PC du réseau.

## Modes décodés

| Famille | Modes |
|---|---|
| PSK | PSK31 à PSK1000, QPSK31 à QPSK500, PSK-R 125 à 1000 |
| RTTY et télex | RTTY 45 à 100 bauds, ASCII 110 bauds ; bulletins météo (DWD) traduits en français |
| CW | 5 à 60 mots/min, vitesse automatique |
| MFSK | MFSK4 à 128, DominoEX, THOR |
| Olivia | Olivia et Contestia, 4 à 64 tonalités, 125 à 2000 Hz |
| MT63 | 500, 1000, 2000 Hz, entrelacement court ou long |
| Autres modes | THROB, THROBX, FSQ, IFKP |
| Signaux faibles | FT8, FT4, WSPR, JT65A/B, JT9, JS8 (normal, rapide, turbo, lent) |
| Images | fax météo, SSTV (Martin, Scottie, Robot, PD, Wraase), Hellschreiber |
| Maritime | Navtex / SITOR-B, SITOR-A, DSC HF-MF et VHF, DGPS |
| TOR / ARQ | PACTOR I (écoute) |
| Packet | AX.25 300 et 1200 bauds, APRS |
| Aviation | HFDL, ACARS VHF, Selcal OACI |
| Appels sélectifs | DTMF, 5 tons, selcall CCIR 493-4, POCSAG |
| ALE | ALE 2G |
| Signaux horaires | DCF77, MSF, TDF, WWVB, JJY, WWV/WWVH, CHU |

## Utilisation

1. Choisissez la source en haut à gauche, ou **＋ Ajouter un serveur…** : collez l'adresse de la page
   web du récepteur, donnez-lui un nom (et le mot de passe d'un Kiwi qui en demande un).
2. Choisissez un mode, puis **cliquez sur un signal** dans le waterfall : un canal s'ouvre.
3. Ou ouvrez une **mémoire** : avec TCI ou CAT, le récepteur est réaccordé.

**Waterfall** : molette = accord du canal actif (Maj 1 Hz, Alt 100 Hz), Ctrl+molette = zoom,
glisser = se déplacer, glisser un marqueur = réaccorder ce canal.

**Carte de canal** : mini-spectre fin pour l'accord précis, fréquence modifiable, paramètres du mode,
pause, effacement, enregistrement du texte, écoute de l'audio du décodeur.

### Récepteurs web

| Serveur | Bande | À savoir |
|---|---|---|
| PhantomSDR / Orsat-SDR | toute la bande du serveur | souvent 3 auditeurs par adresse IP |
| KiwiSDR | 0 à 30 MHz | 4 à 8 canaux par Kiwi ; le waterfall et chaque canal en occupent un |
| OpenWebRX | bande du profil en cours | le second sélecteur en haut change de profil, pour tous les auditeurs du récepteur |
| UberSDR | 10 kHz à 30 MHz (ou plus) | adresse de la page du récepteur (…/v2/ convient) ; en général 2 canaux, le serveur limite à deux utilisateurs par adresse IP |

Les WebSDR de websdr.org utilisent un format audio fermé : pour eux, écoutez dans le navigateur et
choisissez la source **Entrée audio** sur le « Monitor » de la carte son.

### Mémoires

![Mémoires](docs/images/annuaire.jpg)

- **+ Nouvelle mémoire** ou **+ Canal actif** pour en ajouter ; **✎** modifier, **✕** supprimer.
- **⚑** affiche la mémoire sous l'échelle du waterfall : un clic ouvre le canal.
- **Mémoires par défaut** remet la liste d'origine.

Le menu **MÉMOIRES** ne montre que des signaux qu'Orsat-Decoder décode, classés par utilisation (Amateur,
Aviation, Marine, Météo, Signaux horaires…). Sous vos mémoires, chaque rubrique propose aussi les signaux
décodables de la bibliothèque : le **+** à côté d'une fréquence l'ajoute à vos mémoires.

### Identification automatique

Cliquez sur **IDENTIFIER UN SIGNAL SUR LE WATERFALL**, puis sur un signal inconnu. Orsat-Decoder affiche
ses mesures et les candidats les plus proches, essaie les décodeurs possibles et ouvre le bon canal dès
qu'un texte lisible sort.

Le bouton **Comparer** d'un candidat ouvre votre signal et la référence côte à côte : les deux waterfalls à
la même échelle, l'image d'origine, les deux sons et la fiche du signal. « C'est ce signal » ouvre le
décodeur ; vos choix sont notés dans `~/Orsat-Decoder/confirmations.jsonl`. La bibliothèque vient de la base
Artemis (AresValley) et du Signal Identification Wiki ; elle est installée avec Orsat-Decoder et lue sans Internet.

### Ligne de commande

| Commande | Effet |
|---|---|
| `orsat-decoder` | lance l'application |
| `orsat-decoder --lan` | interface accessible depuis le réseau local (`http://ip:8074/`) |
| `orsat-decoder --check` | autotest des décodeurs |
| `orsat-decoder --identify fichier.wav [FREQ_kHz]` | identifie le signal d'un enregistrement |
| `orsat-decoder --probe URL FREQ_kHz` | diagnostic de connexion à un serveur |
| `orsat-decoder --version` | version |

## Licence

Orsat-Decoder est développé par **F1NSK** et **Claude AI**, et distribué sous licence
**GNU GPL version 3** (fichier [LICENSE](LICENSE)).
