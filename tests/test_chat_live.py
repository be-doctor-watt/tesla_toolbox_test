#!/usr/bin/env python3
"""Test d'intégration du chat en mode live, contre le faux Toolbox.

    python tests/test_chat_live.py

Exerce le chemin complet sans jeton Tesla : ouverture de session, ingestion
live d'un code absent, indexation incrémentale, deuxième question servie par
le cache, expiration du jeton en cours de conversation.

Backend d'embedding `hash` : déterministe, hors-ligne, aucun modèle à
télécharger. On teste la mécanique du pipeline, pas la qualité sémantique.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))

TMP = Path(tempfile.mkdtemp(prefix="toolbox_rag_test_"))
os.environ["RAG_BACKEND"] = "hash"
os.environ["RAG_DB"] = str(TMP / "qdrant")
os.environ["TBX_RAW"] = str(TMP / "raw")
# IMPÉRATIF : rediriger AUSSI la session et les conversations. Sans ça, la
# suite écrase le .session.json du projet avec le faux jeton ci-dessous, et
# l'utilisateur retrouve une session morte au démarrage suivant.
os.environ["TBX_SESSION"] = str(TMP / "session.json")
os.environ["TBX_CONVERSATIONS"] = str(TMP / "conversations.json")
# Chaîne vide, pas `pop` : config.py charge .env et ne réécrit que les variables
# ABSENTES de l'environnement. Un `pop` laisserait donc la vraie clé revenir par
# le .env, et le test partirait appeler l'API pour de vrai.
os.environ["MISTRAL_API_KEY"] = ""

# Garde-fou : aucun chemin de test ne doit pointer dans le dépôt. Une variable
# oubliée ici corrompt les données réelles de l'utilisateur, ça s'est produit.
for _v in ("RAG_DB", "TBX_RAW", "TBX_SESSION", "TBX_CONVERSATIONS"):
    assert str(TMP) in os.environ[_v], f"{_v} pointe hors du dossier de test !"

from fastapi.testclient import TestClient   # noqa: E402

import harvest   # noqa: E402
import live      # noqa: E402
from tests.fake_toolbox import Handler, demarrer   # noqa: E402

ok = fail = 0


def verifier(condition, libelle, detail=""):
    global ok, fail
    if condition:
        ok += 1
        print(f"  ok   {libelle}")
    else:
        fail += 1
        print(f"  FAIL {libelle}  {detail}")


def main():
    srv, base = demarrer()
    # On détourne harvest vers le faux Toolbox et on supprime les pauses :
    # le délai anti-Akamai n'a pas de sens face à un serveur local.
    harvest.SEARCH_URL = f"{base}/api/toolbox/articles/search"
    harvest.ARTICLE_URL = f"{base}/api/v2/articles/{{id}}"
    harvest.DELAY = (0.0, 0.0)
    harvest.RAW = Path(os.environ["TBX_RAW"])

    import serve
    print(f"Base de test : {TMP}\n")

    with TestClient(serve.app) as c:
        # ---------------------------------------------------------------
        print("=== Session : refus, puis ouverture ===")
        r = c.post("/session/toolbox", json={"token": "", "cookie": ""})
        verifier(r.status_code == 400, "jeton vide -> 400", r.text[:120])
        r = c.post("/session/toolbox", json={"collage": "bonjour n'importe quoi"})
        verifier(r.status_code == 400, "collage illisible -> 400", r.text[:120])
        r = c.post("/session/toolbox", json={"collage": ""})
        verifier(r.status_code == 400, "collage vide -> 400", r.text[:120])

        r = c.post("/session/toolbox", json={"token": "eyJfaux", "cookie": "pas_de_akamai=1"})
        verifier(r.status_code == 401, "cookie sans _abck -> 401", r.text[:120])
        verifier(not live.SESSION.active, "session non ouverte apres refus")

        Handler.statut_force = 401
        r = c.post("/session/toolbox", json={"token": "eyJfaux", "cookie": "_abck=1"})
        verifier(r.status_code == 401, "Toolbox renvoie 401 -> 401 propre, pas de crash")
        Handler.statut_force = None

        # Champ unique : une ligne de cookie complète, comme collée du DevTools.
        bon = {"collage": "device_hash=x; tbx_token=eyJfaux; _abck=1; bm_sz=2"}
        r = c.post("/session/toolbox", json=bon)
        verifier(r.status_code == 200 and r.json()["active"],
                 "ouverture par collage unique", r.text[:160])
        verifier(r.json()["articles_indexes"] == 0, "index vide au depart", r.text[:160])

        r = c.get("/session/toolbox")
        verifier(r.json()["active"] is True, "etat de session persistant")
        verifier(r.json()["modele_llm"] == "stub", "LLM = stub sans cle API", r.text[:120])

        # ---------------------------------------------------------------
        print("\n=== Première question : ingestion live ===")
        r = c.post("/chat", json={"question": "pourquoi BMS_a066 revient apres remplacement de module"})
        verifier(r.status_code == 200, "HTTP 200", r.text[:200])
        d = r.json()
        verifier(d["codes_detectes"] == ["BMS_a066"], f"code detecte : {d['codes_detectes']}")
        verifier(bool(d["sources"]), "au moins une source", str(d["sources"])[:120])
        verifier(any(s["article_id"] == 6050000 for s in d["sources"]),
                 "l'article 6050000 est remonte", str(d["sources"])[:160])
        depuis_toolbox = [e for e in d["journal"] if e.get("source") == "toolbox"]
        verifier(bool(depuis_toolbox), "journal : article recupere sur Toolbox",
                 str(d["journal"])[:200])
        verifier((harvest.RAW / "6050000.json").exists(), "article mis en cache dans raw/")
        for e in d["journal"]:
            print(f"       journal: {e}")

        # ---------------------------------------------------------------
        print("\n=== Deuxième question, même code : servie par l'index ===")
        avant = len(Handler.appels)
        r = c.post("/chat", json={"question": "BMS_a066 quelles causes possibles"})
        d2 = r.json()
        verifier(r.status_code == 200, "HTTP 200")
        verifier(d2["journal"] == [], "aucune requete Toolbox (journal vide)",
                 str(d2["journal"])[:160])
        verifier(len(Handler.appels) == avant, "aucun appel reseau supplementaire",
                 f"{len(Handler.appels) - avant} appel(s)")
        verifier(bool(d2["sources"]), "reponse quand meme sourcee")

        # ---------------------------------------------------------------
        print("\n=== Un autre code déclenche une nouvelle ingestion ===")
        r = c.post("/chat", json={"question": "que veut dire DI_a175"})
        d3 = r.json()
        verifier(any(e.get("article_id") == 6050003 for e in d3["journal"]),
                 "DI_a175 : article 6050003 ingere", str(d3["journal"])[:200])
        verifier(any(s["article_id"] == 6050003 for s in d3["sources"]),
                 "et remonte comme source")

        # ---------------------------------------------------------------
        print("\n=== Le filtre par code tient toujours (066 vs 067) ===")
        r = c.post("/chat", json={"question": "BMS_a067 charge bloquee"})
        d4 = r.json()
        arts = {s["article_id"] for s in d4["sources"]}
        verifier(arts == {6050001}, f"seul l'article BMS_a067 remonte : {arts}")

        # ---------------------------------------------------------------
        print("\n=== Inventaire et endpoints annexes ===")
        etat = c.get("/session/toolbox").json()
        print(f"       {etat['articles_indexes']} articles, {etat['codes_indexes']} codes, "
              f"{etat['chunks_indexes']} chunks")
        verifier(etat["articles_indexes"] == 3, "3 articles indexes a l'usage")
        verifier(etat["codes_indexes"] >= 3, "au moins 3 codes")

        r = c.get("/codes")
        verifier(r.status_code == 200 and "BMS_a066" in r.json()["codes"],
                 "GET /codes", r.text[:120])
        r = c.get("/article/6050000")
        verifier(r.status_code == 200 and "imbalance" in r.json()["text"].lower(),
                 "GET /article/6050000", r.text[:120])
        verifier(c.get("/article/9999999").status_code == 404, "article absent -> 404")
        r = c.post("/answer", json={"q": "BMS_a066"})
        verifier(r.status_code == 200 and "system" in r.json(), "POST /answer inchange")
        r = c.get("/")
        verifier(r.status_code == 200 and "Recherche Toolbox" in r.text,
                 "GET / sert l'interface")
        r = c.get("/static/doctor-watt.css")
        verifier(r.status_code == 200 and "#134cc9" in r.text,
                 "la charte Doctor-Watt est servie")

        # ---------------------------------------------------------------
        print("\n=== Jeton expiré en cours de conversation ===")
        Handler.statut_force = 401
        r = c.post("/chat", json={"question": "et CP_a004 alors"})
        verifier(r.status_code == 401, f"-> 401 (et non 500) : {r.status_code}", r.text[:160])
        verifier(not live.SESSION.active, "la session est fermee automatiquement")
        Handler.statut_force = None

        print("\n=== Sans session, l'index déjà construit reste interrogeable ===")
        r = c.post("/chat", json={"question": "BMS_a066 causes"})
        d5 = r.json()
        verifier(r.status_code == 200, "HTTP 200 sans session Toolbox")
        verifier(bool(d5["sources"]), "reponse toujours sourcee depuis le cache local")
        r = c.post("/chat", json={"question": "VCFRONT_a191 inconnu du cache"})
        d6 = r.json()
        verifier(d6["codes_absents"] == ["VCFRONT_a191"],
                 f"code absent identifie : {d6['codes_absents']}")
        verifier(d6["avertissement"] and "jeton" in d6["avertissement"].lower(),
                 "avertissement : invite a saisir un jeton",
                 str(d6["avertissement"])[:200])

        print("\n=== Un code absent NE DOIT PAS etre repondu par un autre code ===")
        # Avec session ouverte, le faux Toolbox ne connait pas CP_a004 : le
        # pipeline doit le dire au lieu de servir l'article BMS le plus proche.
        c.post("/session/toolbox", json=bon)
        r = c.post("/chat", json={"question": "CP_a004 verrou de trappe"})
        d7 = r.json()
        verifier(d7["codes_absents"] == ["CP_a004"],
                 f"CP_a004 signale comme absent : {d7['codes_absents']}")
        verifier(d7["avertissement"] and "CP_a004" in d7["avertissement"],
                 "avertissement nomme le code", str(d7["avertissement"])[:200])

    srv.shutdown()
    print(f"\n{ok} ok / {fail} fail")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
