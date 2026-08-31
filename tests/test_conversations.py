#!/usr/bin/env python3
"""Liste de conversations et persistance de la session Toolbox.

    python tests/test_conversations.py

Aucun réseau : faux Toolbox local, embeddings `hash`, LLM sur le stub.
Vérifie surtout les deux points demandés : une conversation se recharge
telle quelle, et la session survit à un redémarrage du serveur.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))

TMP = Path(tempfile.mkdtemp(prefix="conv_test_"))
os.environ.update({
    "RAG_BACKEND": "hash",
    "RAG_DB": str(TMP / "qdrant"),
    "TBX_RAW": str(TMP / "raw"),
    "TBX_SESSION": str(TMP / "session.json"),
    "TBX_CONVERSATIONS": str(TMP / "conversations.json"),
    "MISTRAL_API_KEY": "",
})

# Garde-fou : aucun chemin de test ne doit pointer dans le dépôt. Une variable
# oubliée ici corrompt les données réelles de l'utilisateur, ça s'est produit.
for _v in ("RAG_DB", "TBX_RAW", "TBX_SESSION", "TBX_CONVERSATIONS"):
    assert str(TMP) in os.environ[_v], f"{_v} pointe hors du dossier de test !"

from fastapi.testclient import TestClient   # noqa: E402

import harvest      # noqa: E402
import live         # noqa: E402
from tests.fake_toolbox import demarrer     # noqa: E402

ok = fail = 0
JETON = {"token": "eyJhbGciOiJIUzI1NiJ9.eyJleHBpcmVzX2F0IjogIjIwOTktMDEtMDFUMDA6MDA6MDAifQ.x",
         "cookie": "device_hash=x; _abck=1; bm_sz=2"}


def verifier(cond, libelle, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ok   {libelle}")
    else:
        fail += 1
        print(f"  FAIL {libelle}  {detail}")


def main():
    srv, base = demarrer()
    harvest.SEARCH_URL = f"{base}/api/toolbox/articles/search"
    harvest.ARTICLE_URL = f"{base}/api/v2/articles/{{id}}"
    harvest.DELAY = (0.0, 0.0)
    harvest.RAW = Path(os.environ["TBX_RAW"])

    import serve
    with TestClient(serve.app) as c:
        print("=== Liste vide au départ ===")
        verifier(c.get("/conversations").json()["conversations"] == [],
                 "aucune conversation")

        print("\n=== Le 1er message crée la conversation et son titre ===")
        d = c.post("/chat", json={"question": "Pourquoi BMS_a066 revient apres un swap ?"}).json()
        cid = d.get("conversation_id")
        verifier(bool(cid), f"conversation_id renvoyé : {cid}")
        verifier("BMS_a066" in (d.get("conversation_titre") or ""),
                 f"titre tiré du code défaut : {d.get('conversation_titre')!r}")
        liste = c.get("/conversations").json()["conversations"]
        verifier(len(liste) == 1 and liste[0]["nb_messages"] == 2,
                 f"1 conversation, 2 messages : {liste}")

        print("\n=== 2e message dans la MÊME conversation ===")
        d2 = c.post("/chat", json={"question": "et les effets ?", "conversation_id": cid}).json()
        verifier(d2["conversation_id"] == cid, "même id conservé")
        conv = c.get(f"/conversations/{cid}").json()
        verifier(len(conv["messages"]) == 4, f"{len(conv['messages'])} messages (4 attendu)")
        verifier(conv["titre"] == d["conversation_titre"],
                 "le titre ne change pas au 2e message")

        print("\n=== Le tour est rechargeable à l'identique ===")
        assistant = [m for m in conv["messages"] if m["role"] == "assistant"]
        verifier(all("sources" in m for m in assistant), "sources conservées")
        verifier(all("extraits" in m for m in assistant), "extraits conservés")
        verifier(all("journal" in m for m in assistant), "journal conservé")
        verifier(conv["codes"] == ["BMS_a066"], f"codes de la conversation : {conv['codes']}")

        print("\n=== Une 2e conversation est indépendante ===")
        d3 = c.post("/chat", json={"question": "DI_a175 c'est quoi ?"}).json()
        verifier(d3["conversation_id"] != cid, "nouvel id")
        liste = c.get("/conversations").json()["conversations"]
        verifier(len(liste) == 2, f"{len(liste)} conversations")
        verifier(liste[0]["id"] == d3["conversation_id"],
                 "la plus récemment utilisée est en tête")

        print("\n=== POST /conversations puis suppression ===")
        vide = c.post("/conversations").json()
        verifier(vide["messages"] == [], "conversation créée vide")
        verifier(c.delete(f"/conversations/{vide['id']}").status_code == 200, "suppression")
        verifier(c.delete(f"/conversations/{vide['id']}").status_code == 404,
                 "2e suppression -> 404")
        verifier(c.get("/conversations/inconnue").status_code == 404, "id inconnu -> 404")
        verifier(len(c.get("/conversations").json()["conversations"]) == 2,
                 "les 2 vraies conversations sont intactes")

        print("\n=== Session Toolbox : échéance lue dans le JWT ===")
        r = c.post("/session/toolbox", json=JETON)
        verifier(r.status_code == 200, "ouverture", r.text[:120])
        etat = c.get("/session/toolbox").json()
        verifier(etat["expire_le"] is not None, f"expire_le = {etat['expire_le']}")
        verifier((etat["secondes_restantes"] or 0) > 0, "secondes_restantes > 0")
        verifier(Path(os.environ["TBX_SESSION"]).exists(), ".session.json écrit")
        mode = oct(Path(os.environ["TBX_SESSION"]).stat().st_mode)[-3:]
        verifier(mode == "600", f"permissions {mode} (600 attendu)")

    print("\n=== Redémarrage du serveur : la session est restaurée ===")
    live.SESSION.fermer(oublier=False)          # comme un arrêt d'uvicorn
    verifier(not live.SESSION.active, "session fermée en mémoire")
    verifier(Path(os.environ["TBX_SESSION"]).exists(),
             "le fichier survit à l'arrêt (oublier=False)")
    with TestClient(serve.app) as c:
        etat = c.get("/session/toolbox").json()
        verifier(etat["active"] is True, "session ACTIVE après redémarrage")
        verifier(len(c.get("/conversations").json()["conversations"]) == 2,
                 "les conversations survivent aussi")

        print("\n=== « Fermer la session » efface le fichier ===")
        c.delete("/session/toolbox")
        verifier(not Path(os.environ["TBX_SESSION"]).exists(), "fichier supprimé")
    with TestClient(serve.app) as c:
        verifier(c.get("/session/toolbox").json()["active"] is False,
                 "pas de restauration après fermeture explicite")

    print("\n=== Un jeton expiré n'est pas restauré ===")
    Path(os.environ["TBX_SESSION"]).write_text(
        '{"token": "x.eyJleHBpcmVzX2F0IjogIjIwMDAtMDEtMDFUMDA6MDA6MDAifQ.y",'
        ' "cookie": "c", "expire_le": 946684800}')
    live.SESSION.fermer(oublier=False)
    motif = live.SESSION.restaurer()
    verifier(not live.SESSION.active and motif and "expir" in motif,
             f"refus motivé : {motif!r}")
    verifier(not Path(os.environ["TBX_SESSION"]).exists(), "fichier périmé nettoyé")

    print("\n=== Titre : pas de doublon de codes ===")
    import conversations as C
    t = C._titre_depuis("BMS_a066, BMS_a064, BMS_a079 a quoi cela correspond ?")
    verifier(t.count("BMS_a066") == 1 and not t.startswith("BMS_a064, BMS_a066 —"),
             f"codes déjà dans le texte, pas de préfixe : {t!r}")
    t2 = C._titre_depuis("a quoi correspond le defaut du chargeur ?")
    verifier("—" not in t2, f"aucun code, aucun préfixe : {t2!r}")
    t3 = C._titre_depuis("")
    verifier(t3 == "Nouvelle conversation", f"question vide : {t3!r}")

    print("\n=== Un fichier illisible ne détruit pas l'historique ===")
    fichier = Path(os.environ["TBX_CONVERSATIONS"])
    avant = len(C._lire())
    verifier(avant > 0, f"{avant} conversations avant l'incident")
    fichier.write_text("{ ceci n'est pas du json", encoding="utf-8")
    try:
        C._lire()
        verifier(False, "aucune exception levée : l'historique serait écrasé")
    except C.HistoriqueIllisible:
        verifier(True, "HistoriqueIllisible levée au lieu de renvoyer une liste vide")
    verifier(fichier.with_suffix(".json.corrompu").exists(),
             "le fichier fautif est mis de côté, pas perdu")
    convs, avert = C.lister()
    verifier(convs == [] and avert is None,
             "lister() repart proprement au coup suivant")

    srv.shutdown()
    print(f"\n{ok} ok / {fail} fail")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
