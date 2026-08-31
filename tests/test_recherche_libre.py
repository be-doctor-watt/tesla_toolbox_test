#!/usr/bin/env python3
"""Questions SANS code défaut : réécriture en mots-clés et citations vérifiées.

    python tests/test_recherche_libre.py

Le moteur Toolbox est lexical, exige TOUS les termes et n'indexe que de
l'anglais (mesuré : « coolant » 643 résultats, « coolant fill bleed » 0).
Envoyer la question brute ne trouve donc rien : ces tests verrouillent
l'étape de réécriture, et le garde-fou sur les numéros d'article inventés.

Aucun réseau : faux Toolbox local à sémantique AND, LLM remplacé par un stub
déterministe.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))

TMP = Path(tempfile.mkdtemp(prefix="libre_test_"))
os.environ.update({
    "RAG_BACKEND": "hash",
    "RAG_DB": str(TMP / "qdrant"),
    "TBX_RAW": str(TMP / "raw"),
    "TBX_SESSION": str(TMP / "session.json"),
    "TBX_CONVERSATIONS": str(TMP / "conversations.json"),
    "MISTRAL_API_KEY": "",
})
for _v in ("RAG_DB", "TBX_RAW", "TBX_SESSION", "TBX_CONVERSATIONS"):
    assert str(TMP) in os.environ[_v], f"{_v} pointe hors du dossier de test !"

import harvest   # noqa: E402
import live      # noqa: E402
from tests.fake_toolbox import demarrer   # noqa: E402

ok = fail = 0


def verifier(cond, libelle, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ok   {libelle}")
    else:
        fail += 1
        print(f"  FAIL {libelle}  {detail}")


class LLMBidon:
    """Renvoie des mots-clés fixes, pour un test déterministe."""

    nom = "bidon"

    def __init__(self, reponse):
        self.reponse = reponse
        self.appels = []

    def complete(self, messages):
        self.appels.append(messages)
        return self.reponse


def main():
    srv, base = demarrer()
    harvest.SEARCH_URL = f"{base}/api/toolbox/articles/search"
    harvest.ARTICLE_URL = f"{base}/api/v2/articles/{{id}}"
    harvest.DELAY = (0.0, 0.0)
    harvest.RAW = Path(os.environ["TBX_RAW"])
    db = os.environ["RAG_DB"]

    print("=== Le faux Toolbox reproduit la sémantique AND du vrai ===")
    live.SESSION.ouvrir("eyJfaux", "device_hash=x; _abck=1")
    s = live.SESSION.http
    def compte(q):
        d = harvest.check(s.get(harvest.SEARCH_URL, params={
            "q": q, "page": 0, "locale": harvest.LOCALE})).json()
        return len(harvest._hits(d))
    verifier(compte("coolant") >= 1, f"« coolant » -> {compte('coolant')}")
    # Termes présents séparément mais jamais ensemble : c'est ce que le AND
    # rejette, et c'est ce qui fait qu'une phrase entière ne trouve rien.
    verifier(compte("coolant") >= 1 and compte("brick") >= 1
             and compte("coolant brick overvoltage") == 0,
             f"« coolant brick overvoltage » -> {compte('coolant brick overvoltage')} "
             "(0 attendu, les termes ne cooccurrent pas)")
    verifier(compte("purge du circuit de refroidissement") == 0,
             "une question en français -> 0, comme sur le vrai moteur")

    print("\n=== Un code n'est jamais réécrit ===")
    live._CLIENT = None
    verifier(live.mots_cles("pourquoi BMS_a066 revient ?") == ["BMS_a066"],
             "le code est utilisé tel quel comme requête")

    print("\n=== Réécriture par le modèle ===")
    import llm
    faux = LLMBidon("coolant level\ndrive inverter overtemp\nsomething far too long to be useful here ok")
    llm._CLIENT = faux
    reqs = live.mots_cles("Comment vérifier le circuit de refroidissement ?")
    verifier(len(reqs) >= 1, f"requêtes produites : {reqs}")
    verifier(all(len(r.split()) <= 3 for r in reqs),
             f"aucune requête de plus de 3 mots : {reqs}")
    verifier(len(reqs) <= live.MAX_REQUETES_LIBRES,
             f"au plus {live.MAX_REQUETES_LIBRES} requêtes : {len(reqs)}")

    print("\n=== Repli sans modèle : honnête, sans invention ===")
    llm._CLIENT = llm.ClientLLMStub()
    live._CLIENT = None
    r = live.mots_cles("procédure de purge du liquide de refroidissement")
    verifier(len(r) == 1 and len(r[0].split()) <= 2,
             f"repli heuristique borné : {r}")

    print("\n=== Question libre : Toolbox est interrogé, sans attendre zéro local ===")
    llm._CLIENT = LLMBidon("coolant level")
    d = live.repondre("Comment vérifier le niveau de liquide de refroidissement ?",
                      6, "hash", None, db)
    etapes = [e.get("etape") for e in d["journal"]]
    verifier("mots_cles" in etapes, f"étape de réécriture présente : {etapes}")
    verifier("recherche" in etapes, "Toolbox a bien été interrogé")
    ingeres = [e for e in d["journal"] if e.get("etape") == "article"]
    verifier(bool(ingeres), f"au moins un article ingéré : {len(ingeres)}")
    verifier(any(e.get("article_id") == 6050003 for e in ingeres),
             f"l'article coolant est remonté : {[e.get('article_id') for e in ingeres]}")

    print("\n=== L'index local a maintenant du contenu : pas de 2e appel Toolbox ===")
    avant = len([1 for e in d["journal"] if e.get("etape") == "recherche"])
    d2 = live.repondre("Comment vérifier le niveau de liquide de refroidissement ?",
                       6, "hash", None, db)
    verifier(d2["journal"] == [],
             f"question identique -> aucune requête (mémoire) : {d2['journal']}")
    verifier(bool(d2["sources"]), "réponse quand même sourcée depuis l'index")

    print("\n=== Numéros d'article inventés : neutralisés et signalés ===")
    srcs = [{"article_id": 43854}, {"article_id": 6050000}]
    txt, inv = live.verifier_citations(
        "Voir l'article [4385400] et [43854] et [3199500].", srcs)
    verifier(inv == ["4385400", "3199500"], f"détectés : {inv}")
    verifier("[43854]" in txt, "le numéro correct est conservé")
    verifier("4385400" not in txt and "3199500" not in txt,
             f"les faux ne sont plus lisibles : {txt}")
    verifier(txt.count("[référence non vérifiée]") == 2,
             f"remplacés par un marqueur explicite : {txt}")
    txt2, inv2 = live.verifier_citations("Rien à citer ici.", srcs)
    verifier(inv2 == [] and txt2 == "Rien à citer ici.",
             "texte sans citation inchangé")

    print("\n=== Référence croisée citée par l'article : légitime, conservée ===")
    extraits = "Pour la pompe à chaleur, voir l'article #3199500 du manuel."
    txt3, inv3 = live.verifier_citations("Voir [43854] puis #3199500.", srcs, extraits)
    verifier(inv3 == [], f"#3199500 figure dans les extraits : {inv3}")
    verifier("#3199500" in txt3, "la référence croisée est conservée telle quelle")
    txt4, inv4 = live.verifier_citations("Voir #9999999.", srcs, extraits)
    verifier(inv4 == ["9999999"], f"un # absent des extraits est signalé : {inv4}")
    verifier("9999999" not in txt4, f"et neutralisé : {txt4}")

    print("\n=== L'avertissement remonte jusqu'à la réponse ===")
    llm._CLIENT = LLMBidon("La procédure est dans l'article [9999999].")
    d3 = live.repondre("BMS_a066 quelles causes", 6, "hash", None, db)
    verifier(d3.get("citations_inventees") == ["9999999"],
             f"citations_inventees = {d3.get('citations_inventees')}")
    verifier(d3.get("avertissement") and "9999999" in d3["avertissement"],
             f"avertissement : {str(d3.get('avertissement'))[:140]}")
    verifier("[référence non vérifiée]" in d3["reponse"],
             f"réponse assainie : {d3['reponse'][:120]}")

    srv.shutdown()
    print(f"\n{ok} ok / {fail} fail")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
