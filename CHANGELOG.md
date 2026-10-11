# Changements

## EQ MASTER

- Égaliseur paramétrique stéréo à six bandes : courbe interactive, cloches et plateaux, passe-haut/passe-bas, préampli et compensation des boosts.
- Traitement réel dans la route ALSA Loopback, transitions de 40 ms et bypass sans relance des lecteurs.
- Spectre mesuré après traitement, sauvegarde automatique et préréglages ; aperçu explicitement simulé.
- API authentifiée et validée ; dépendances NumPy/SciPy et nouveaux fichiers inclus dans l’installation et les mises à jour.

## 3.0

- SOLO effectif par source, sélection multiple et restauration des volumes/MUTE ; sélection conservée au redémarrage.
- Voyants MUTE rouges sur les sources et MASTER, voyant SOLO ambré distinct et boutons tactiles de 44 px.
- Détection des récepteurs Bluetooth et sorties ALSA, choix depuis la page web.
- Paquet sans adresse Bluetooth, appareil ou carte audio propres à une installation.
- Lecteur BlueALSA géré par l’application ; changement réel de sortie et restauration après échec.
- Mélange stéréo avec volume logiciel commun et gain, ou contrôle physique selon ses capacités.
- Formats de volume BlueALSA historique et actuel pris en charge.
- Hauteurs fixes pour les noms et commandes : alignements indépendants de leur longueur.
- Migration des préférences 2.x et retour arrière si le serveur ne démarre pas.
- Documentation générique, dépendances Python et contrôles GitHub Actions.
