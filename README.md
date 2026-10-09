# Bureau Audio 3.0

Console web locale pour recevoir plusieurs sources Bluetooth, les mélanger en stéréo sur un Raspberry Pi et écouter le résultat sur une seule sortie audio. Chaque source possède son fader et son MUTE. MASTER règle le volume commun et le gain disponible.

```text
Téléphone / ordinateurs → récepteurs Bluetooth du Pi → BlueALSA
                         → mélange ALSA stéréo → jack / USB / HDMI
```

Pour des écouteurs sans fil, branche un émetteur Bluetooth à la sortie du Pi. L’application ne gère pas la transmission Bluetooth sortante ni le micro.

Le paquet ne contient aucune adresse Bluetooth, source ou carte audio imposée. Leur détection et leur sélection se font dans la page web. Il peut servir sur d’autres Pi sous Debian / Raspberry Pi OS avec systemd ; la compatibilité sonore avec toutes les radios et cartes n’est pas garantie par les tests simulés.

## Installation

Prérequis : Python 3.9+, BlueZ, ALSA et BlueALSA avec `bluealsa-aplay`, un récepteur Bluetooth A2DP et une sortie ALSA stéréo. Une carte USB peut remplacer le jack absent de certains modèles. L’application accepte les formats de volume BlueALSA `uint16` et tableau d’octets, ainsi que les propriétés `Sampling` et `Rate`.

Le lecteur doit proposer `--volume=software` (notamment BlueALSA 4.3.1 et les versions actuelles). Le BlueALSA 4.0 de certains systèmes Bookworm doit être mis à jour avant cette installation. Les scripts vérifient cette capacité avant le transfert du lecteur ; ils ne remplacent pas une version compilée déjà installée. Les [options du lecteur 4.3.1](https://github.com/arkq/bluez-alsa/blob/v4.3.1/doc/bluealsa-aplay.1.rst) décrivent ce mode.

Depuis le dossier du projet copié sur le Pi :

```sh
sudo bash install.sh
```

Le script installe les dépendances Debian disponibles et conserve une installation BlueALSA existante. Il affiche l’adresse `http://NOM-DU-PI.local:8088` et le code d’accès propre à cette installation.

1. Ouvre **Configuration de la sortie**, choisis la carte, la sortie et le contrôle de volume, puis **Appliquer la sortie**. Le contrôle logiciel **Bureau Audio Master** est proposé en premier : volume, MUTE et gain jusqu’à +4 dB.
2. Dans **Bluetooth et appareils**, renomme les récepteurs si nécessaire : c’est le nom visible depuis les sources.
3. Clique sur **Appairer** sur le récepteur souhaité. Pendant trois minutes, sélectionne ce récepteur dans les réglages Bluetooth du téléphone ou de l’ordinateur.
4. Sélectionne-le comme sortie son sur cet appareil et lance une lecture. Sa tranche apparaît automatiquement.

Sans daemon BlueALSA actif, l’installation crée son service de réception A2DP. Un daemon actif est conservé. L’application gère le lecteur `bluealsa-aplay` : un ancien service de lecture standard est arrêté et désactivé, avec son état sauvegardé pour la désinstallation.

Un daemon préexistant doit exposer `org.bluealsa`, accepter A2DP sink sur les récepteurs utilisés et démarrer automatiquement avec le Pi. Un lecteur lancé manuellement ou sous une autre unité doit être arrêté avant de choisir une sortie.

L’installation nécessite root. Ensuite, l’interface et le lecteur fonctionnent sous `bureau-audio`, membre du groupe `audio`. Le bouton **Activer** une radio utilise une unité privilégiée à commande fixe.

## Mise à jour depuis 2.x ou 3.0

Copie le nouveau dossier sur le Pi puis lance :

```sh
sudo bash upgrade.sh
```

Le code d’accès, les appairages, les affectations, les noms Bluetooth, les appareils retirés et les volumes sont conservés. Une ancienne route ALSA personnalisée est conservée quand `BUREAU_CARD` est présent ; `BUREAU_PCM` précise son nom, avec `hub_mix` comme valeur de migration par défaut.

**Le lecteur audio change de propriétaire : une brève interruption est attendue.** Le service web lance désormais le lecteur pour appliquer réellement les changements de sortie. Le script sauvegarde code, préférences, unité web et état immédiat du lecteur. Si le serveur ne démarre pas, il restaure la version précédente. `/health` confirme la version web, pas la réception ni l’écoute effective : vérifie les flux après la migration.

Les fichiers ALSA globaux ne sont pas réécrits. Les sauvegardes restent dans `/var/backups/bureau-audio`. Recharge la page et saisis le code habituel. `update-ui.sh` remplace uniquement la présentation d’un serveur déjà en 3.0.

## Sources et interface

Les tranches utilisent **Name**, le nom distant connu par BlueZ, et non un alias personnalisé local. Le nom est mémorisé pour les sources déconnectées ; l’adresse est affichée si aucun nom n’est disponible. Seuls les appareils capables d’envoyer un flux A2DP sont ajoutés automatiquement.

Les affectations persistent après redémarrage. Les adresses des récepteurs les identifient indépendamment des numéros `hci`. Pour déplacer une source, choisis son récepteur dans **Tes appareils** puis **Appliquer**. L’ancien lien est déconnecté et bloqué ; appaire ensuite la source sur le nouveau récepteur si nécessaire. Un récepteur absent n’entraîne pas de bascule automatique.

**Retirer** masque et déconnecte une source, en conservant son appairage. **Rétablir** la réactive sur le récepteur choisi. Une nouvelle source ou un remplacement de téléphone ne nécessite aucune modification du code.

Le volume par source agit sur son flux BlueALSA stéréo. Lance une lecture si son fader est indisponible. MASTER agit sur le contrôle choisi pour la sortie, avec le gain permis par celui-ci. Les voyants indiquent un flux actif sans mesurer son niveau : il peut transporter du silence.

Les bandeaux réservent deux lignes au nom et une au récepteur. Les hauteurs fixes empêchent les noms longs de déplacer les faders et MUTE. Sur mobile, deux tranches par rangée et MASTER en tête. Le nombre de rangées et la longueur des faders s’adaptent à l’espace disponible.

## Audio et limites

Pour une nouvelle sortie, le moteur crée une configuration ALSA privée : conversion `plug`, volume commun `softvol` si choisi, puis mélange `dmix`. La sortie matérielle est testée en **stéréo S16_LE, 48 kHz**, avec périodes de 40 ms et cinq périodes de tampon. Une carte incompatible échoue avec un diagnostic. Un changement raté restaure la sortie précédente.

Changer de destination interrompt brièvement le lecteur ; renommer la même destination ne le redémarre pas. Le gain matériel dépend du maximum dB déclaré par ALSA ; les contrôles sans mute utilisable ne sont pas proposés. Le volume logiciel reste disponible sur une sortie sans contrôle physique.

Le volume logiciel ne modifie pas les contrôles matériels de la carte. Si l’un d’eux est observé muet ou à zéro, un diagnostic le signale : il peut bloquer l’écoute, selon son lien avec la sortie choisie. Vérifie ce contrôle ou sélectionne-le comme contrôle de volume ; l’application n’ouvre pas arbitrairement toutes les sorties de la carte.

Le gain positif et la somme de sources fortes peuvent saturer : réduis les faders si tu entends une distorsion. Aucun limiteur ou vumètre audio n’est intégré. La fluidité, le nombre de flux et la latence dépendent des radios et des sources. Plusieurs récepteurs permettent de répartir les flux, sans garantir un nombre illimité de lectures simultanées. Une console de jeu sans sortie A2DP nécessite une entrée ou un émetteur séparé.

## Données de chaque installation

| Emplacement | Rôle |
| --- | --- |
| `/opt/bureau-audio` | Code et interface |
| `/etc/bureau-audio.env` | Code d’accès, adresse et port web |
| `/etc/bureau-audio-routes.json` | Routes initiales, facultatives sur une installation neuve |
| `/var/lib/bureau-audio/settings.json` | Récepteurs, sources, volumes et destination choisis |
| `/var/lib/bureau-audio/alsa-playback.conf` | Configuration ALSA privée |
| `/var/lib/bureau-audio-install` | Métadonnées root pour restaurer les services |

Le `routing.json` distribué est vide. N’ajoute pas les réglages d’un Pi réel au dépôt. Le `.gitignore` exclut préférences, environnements, états de démonstration et sauvegardes usuels. L’application est destinée au réseau local ; l’installation ne configure ni accès Internet ni HTTPS.

## Développement et dépôt GitHub

Copie ce dossier tel quel dans ton dépôt privé : code, tests, scripts, documentation et workflow GitHub Actions sont inclus. Aucun remote ou dépôt n’est imposé. Le workflow vérifie Python, JavaScript et shell ; il ne déploie pas sur un Pi.

Sur Mac ou Linux, pour l’aperçu sans matériel :

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
BUREAU_STATE=./demo-state BUREAU_DEMO_EXTRA=2 .venv/bin/python app.py --demo --host 127.0.0.1 --port 8088
```

Ouvre `http://127.0.0.1:8088`, code `demo`. Le bandeau signale que ces commandes ne pilotent aucun matériel. Pour tester la première configuration, ajoute `BUREAU_DEMO_SETUP=1` avec un nouveau dossier d’état.

```sh
.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js
for script in *.sh; do bash -n "$script"; done
```

Les tests couvrent les contrats D-Bus, les deux formats de volume, les routes dynamiques, les accès web, le lecteur, ses échecs et la migration avec retour arrière. Les radios, cartes et commandes sont simulées. La version 3.0 a également été inspectée dans le navigateur sur ordinateur et mobile. Sa validation sonore reste à effectuer sur le matériel cible.

## Diagnostic et désinstallation

```sh
sudo systemctl status bureau-audio --no-pager
sudo journalctl -u bureau-audio -n 60 --no-pager
sudo sed -n 's/^BUREAU_KEY=//p' /etc/bureau-audio.env
```

Pour désinstaller, garde les récepteurs branchés puis lance `sudo bash uninstall.sh`. Les drapeaux Bluetooth encore contrôlés par l’application et l’état sauvegardé de l’ancien lecteur sont restaurés. Les données et appairages sont conservés. Le daemon créé par cette installation est retiré ; un daemon préexistant est conservé. Si un récepteur est absent, rebranche-le et relance la désinstallation pour terminer sa restauration.

## Références techniques

- [BlueZ : propriétés des appareils](https://github.com/bluez/bluez/blob/master/doc/org.bluez.Device.rst).
- [API PCM BlueALSA 4.3.1](https://github.com/arkq/bluez-alsa/blob/v4.3.1/doc/org.bluealsa.PCM1.7.rst) et [API actuelle](https://github.com/arkq/bluez-alsa/blob/master/doc/org.bluealsa.PCM1.7.rst).
- [ALSA dmix](https://github.com/alsa-project/alsa-lib/blob/master/src/pcm/pcm_dmix.c) et [softvol](https://github.com/alsa-project/alsa-lib/blob/master/src/pcm/pcm_softvol.c).
- [GitHub Actions checkout](https://github.com/actions/checkout) et [setup-python](https://github.com/actions/setup-python).
