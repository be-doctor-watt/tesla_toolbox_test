"""Liste de conversations, persistée dans un fichier JSON.

Côté serveur et non dans le navigateur : c'est un outil mono-utilisateur, le
serveur possède déjà l'état (Qdrant, raw/), et les conversations survivent
ainsi à un vidage du cache du navigateur ou à un changement de poste.

Le fichier contient des extraits de documentation Tesla sous copyright : il est
ignoré par git, comme raw/ et qdrant_data/.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from faultcodes import extract_codes

FICHIER = Path(os.environ.get(
    "TBX_CONVERSATIONS", str(Path(__file__).parent / "conversations.json")))

# Au-delà, on tronque : le fichier est relu et réécrit en entier à chaque
# message, et un historique illimité finirait par coûter cher à chaque question.
MAX_CONVERSATIONS = 200
MAX_MESSAGES = 200


class HistoriqueIllisible(Exception):
    """Le fichier existe mais n'a pas pu être lu. On refuse d'écrire par-dessus."""


def _lire() -> list[dict]:
    """-> conversations. Lève si le fichier existe et résiste à la lecture.

    Ne JAMAIS renvoyer une liste vide sur erreur de lecture : l'appelant
    écrirait ensuite par-dessus, et tout l'historique disparaîtrait sans un
    seul message. On met le fichier de côté et on lève, ce qui donne une
    erreur visible une fois, puis un repart propre au coup suivant.
    """
    if not FICHIER.exists():
        return []
    try:
        d = json.loads(FICHIER.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        sauvegarde = FICHIER.with_suffix(".json.corrompu")
        try:
            FICHIER.replace(sauvegarde)
        except OSError:
            pass
        raise HistoriqueIllisible(
            f"{FICHIER.name} illisible ({e}). Déplacé vers {sauvegarde.name} ; "
            "les conversations suivantes repartiront d'une liste vide.") from e
    if not isinstance(d, list):
        raise HistoriqueIllisible(
            f"{FICHIER.name} ne contient pas une liste. Fichier laissé en place.")
    return d


def _ecrire(convs: list[dict]):
    convs = convs[:MAX_CONVERSATIONS]
    # Écriture atomique : une coupure en pleine écriture ne doit pas laisser un
    # JSON tronqué, qui ferait perdre tout l'historique.
    tmp = FICHIER.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(convs, ensure_ascii=False), encoding="utf-8")
    tmp.replace(FICHIER)


def _titre_depuis(question: str) -> str:
    """Titre lisible : les codes défaut d'abord, ils identifient la conversation.

    On ne préfixe qu'avec les codes ABSENTS du texte conservé. Comparer le
    préfixe entier à la question produisait « BMS_a064, BMS_a066 — BMS_a066,
    BMS_a064, ... » : les codes y étaient, mais dans un autre ordre.
    """
    texte = " ".join(question.split())
    if len(texte) > 60:
        texte = texte[:57].rstrip() + "…"
    manquants = [c for c in extract_codes(question) if c.lower() not in texte.lower()]
    if manquants:
        return f"{', '.join(manquants[:2])} — {texte}"
    return texte or "Nouvelle conversation"


def _resume(c: dict) -> dict:
    """Ligne de liste : sans les messages, qui peuvent peser lourd."""
    return {
        "id": c["id"],
        "titre": c.get("titre") or "Nouvelle conversation",
        "cree_le": c.get("cree_le"),
        "maj_le": c.get("maj_le"),
        "nb_messages": len(c.get("messages") or []),
        "codes": c.get("codes") or [],
    }


# --------------------------------------------------------------------------
def lister() -> tuple[list[dict], str | None]:
    """-> (résumés récents d'abord, avertissement éventuel).

    Seule fonction tolérante : afficher une liste vide est acceptable, alors
    qu'écrire une liste vide détruirait l'historique. L'écriture, elle, passe
    toujours par `_lire()` qui lève.
    """
    try:
        convs = _lire()
    except HistoriqueIllisible as e:
        return [], str(e)
    convs.sort(key=lambda c: c.get("maj_le") or 0, reverse=True)
    return [_resume(c) for c in convs], None


def creer(titre: str | None = None) -> dict:
    maintenant = time.time()
    c = {"id": uuid.uuid4().hex[:12], "titre": titre or "Nouvelle conversation",
         "cree_le": maintenant, "maj_le": maintenant, "messages": [], "codes": []}
    convs = _lire()
    convs.insert(0, c)
    _ecrire(convs)
    return c


def obtenir(cid: str) -> dict | None:
    for c in _lire():
        if c["id"] == cid:
            return c
    return None


def supprimer(cid: str) -> bool:
    convs = _lire()
    restant = [c for c in convs if c["id"] != cid]
    if len(restant) == len(convs):
        return False
    _ecrire(restant)
    return True


def ajouter_echange(cid: str | None, question: str, reponse: dict) -> dict:
    """Enregistre un aller-retour. Crée la conversation si `cid` est absent.

    -> la conversation complète, pour que le frontend rafraîchisse la liste
    sans un second appel.
    """
    convs = _lire()
    c = next((x for x in convs if x["id"] == cid), None) if cid else None
    if c is None:
        c = creer(_titre_depuis(question))
        convs = _lire()
        c = next(x for x in convs if x["id"] == c["id"])
    elif not c.get("messages"):
        # Première question d'une conversation créée vide : elle donne le titre.
        c["titre"] = _titre_depuis(question)

    c.setdefault("messages", []).append({
        "role": "user", "content": question, "le": time.time()})
    c["messages"].append({
        "role": "assistant", "content": reponse.get("reponse", ""), "le": time.time(),
        # On garde de quoi réafficher le tour à l'identique au rechargement.
        "sources": reponse.get("sources") or [],
        "extraits": reponse.get("extraits") or [],
        "journal": reponse.get("journal") or [],
        "avertissement": reponse.get("avertissement"),
        "repli_semantique": reponse.get("repli_semantique", False),
        "modele": reponse.get("modele"),
        "modele_prevu": reponse.get("modele_prevu"),
    })
    c["messages"] = c["messages"][-MAX_MESSAGES:]
    c["codes"] = sorted(set(c.get("codes") or []) | set(reponse.get("codes_detectes") or []))
    c["maj_le"] = time.time()
    _ecrire(convs)
    return c


def historique_llm(cid: str | None, limite: int = 6) -> list[dict]:
    """Les derniers tours au format attendu par la couche LLM."""
    if not cid:
        return []
    c = obtenir(cid)
    if not c:
        return []
    return [{"role": m["role"], "content": m["content"]}
            for m in (c.get("messages") or [])[-limite:]]
