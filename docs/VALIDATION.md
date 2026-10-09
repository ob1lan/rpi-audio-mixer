# Vérification de la version 3.0

Vérification locale du 8 octobre 2026, sans Raspberry Pi connecté.

- 87 tests Python réussis : 25 pour le moteur ALSA et le lecteur, 53 pour les contrôles Bluetooth et web, 9 pour l’installation, la migration et la désinstallation simulées.
- Syntaxe JavaScript et tous les scripts Bash vérifiés.
- Aperçu dans le navigateur : cinq sources, noms courts et longs, format ordinateur 1440 × 1000 et mobile 390 × 844.
- Bandeaux de 78 px sur ordinateur et 56 px sur mobile. Dans chaque rangée, faders et boutons MUTE se trouvent aux mêmes hauteurs, y compris MASTER.
- Choix d’une carte USB et de son contrôle sans gain positif, application et rechargement : destination et limite de gain conservées.
- Renommage d’un récepteur : nom visible et sélections mis à jour.
- Première configuration sur mobile : panneau ouvert automatiquement, sortie HDMI choisie, MASTER rendu disponible, volume et MUTE vérifiés.
- Aucun débordement horizontal observé au format mobile ; commandes du formulaire de 44 px de hauteur.

Les tests utilisent des objets D-Bus, cartes ALSA et processus simulés. Ils ne valident ni la qualité sonore, ni la latence, ni le nombre maximal de flux simultanés sur une radio physique. La configuration ALSA générée et la migration restent à tester sur chaque matériel cible.
