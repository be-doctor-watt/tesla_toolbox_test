"""Chargement du fichier .env, sans dépendance.

ValiZia utilise pydantic-settings avec `env_file=".env"` ; ici on garde la
même convention de fichier mais sans la dépendance, parce que le reste du
dépôt lit `os.environ` directement.

À importer AVANT tout module qui lit une variable d'environnement au moment de
l'import (harvest.py le fait pour TBX_RAW et TBX_LOCALE).

Une variable déjà définie dans l'environnement n'est jamais écrasée : un
`export` explicite ou une variable de conteneur doit primer sur le fichier.
"""

from __future__ import annotations

import os
from pathlib import Path

FICHIER = Path(__file__).parent / ".env"


def charger(chemin: Path = FICHIER) -> list[str]:
    """-> noms des variables effectivement posées."""
    if not chemin.exists():
        return []
    poses = []
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, _, valeur = ligne.partition("=")
        cle, valeur = cle.strip(), valeur.strip()
        # Les guillemets sont un artefact de shell, pas une partie de la valeur.
        if len(valeur) >= 2 and valeur[0] == valeur[-1] and valeur[0] in "\"'":
            valeur = valeur[1:-1]
        if cle and cle not in os.environ:
            os.environ[cle] = valeur
            poses.append(cle)
    return poses


CHARGEES = charger()
