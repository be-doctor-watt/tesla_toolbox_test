#!/usr/bin/env python3
"""Non-régression sur les formes CONFIRMÉES de l'API Toolbox réelle.

    python tests/test_api_reelle.py

Chaque assertion correspond à un défaut constaté en branchant un vrai jeton.
Aucun réseau : les réponses sont figées dans tests/fake_toolbox.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harvest                                   # noqa: E402
from tests.fake_toolbox import ARTICLE_REEL, REPONSE_SEARCH_REELLE   # noqa: E402

ok = fail = 0


def verifier(cond, libelle, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ok   {libelle}")
    else:
        fail += 1
        print(f"  FAIL {libelle}  {detail}")


print("=== Enveloppe Elasticsearch : hits est un dict, pas une liste ===")
hits, cle = harvest._hits_avec_cle(REPONSE_SEARCH_REELLE)
verifier(cle == "hits.hits", f"clé détectée = {cle!r}")
verifier(len(hits) == 2, f"{len(hits)} documents extraits")

print("\n=== id d'article sous _source.article.id ===")
ids = harvest._ids_from(hits)
verifier(ids == [6050000, 6050001], f"ids = {ids}")
verifier(harvest._titre_hit(hits[0]) == "BMS_a066_SOC_Imbalance_Warning",
         f"titre = {harvest._titre_hit(hits[0])!r}")

print("\n=== une liste vide sous une clé connue n'est PAS une structure inconnue ===")
h, c = harvest._hits_avec_cle({"hits": {"hits": [], "total": {"value": 0}}})
verifier(c == "hits.hits" and h == [],
         "0 résultat se distingue d'une API changée", f"cle={c!r}")
h, c = harvest._hits_avec_cle({"totoResultats": [1, 2]})
verifier(c is None, "clé vraiment inconnue -> None")

print("\n=== « < » littéral non échappé : le seuil ne doit pas disparaître ===")
t = harvest.strip_html(ARTICLE_REEL["firmware_details"])
verifier("SOC_IMBALANCE_CLEAR_HYSTERESIS" in t,
         "le seuil de la Clear Condition survit", t[:160])
verifier("<" in t, "l'opérateur de comparaison survit")
verifier("Set Condition" in t and "Latching Alert" in t,
         "les lignes voisines de la table survivent")
verifier("<td" not in t and "tbody" not in t, "les vraies balises sont bien retirées")

print("\n=== article sans body : le texte vient des autres clés ===")
texte = harvest.flatten_article(ARTICLE_REEL)
verifier(len(texte) > 500, f"{len(texte)} caractères extraits (>500 attendu)")
verifier("SIGNAUX ET DÉTAILS FIRMWARE" in texte, "firmware_details est indexé")
verifier("CAUSES POSSIBLES" in texte, "causes_virtual est indexé")
verifier("EFFETS SUR LE VÉHICULE" in texte, "effects_virtual est indexé")

print("\n=== qualifiers Make/Model = applicabilité, pas conditions ===")
verifier("MODÈLES CONCERNÉS" in texte, "section MODÈLES CONCERNÉS présente")
verifier("CONDITIONS DE DÉCLENCHEMENT" not in texte,
         "pas de titre CONDITIONS DE DÉCLENCHEMENT sur du Make/Model")
verifier("Model 3" in texte, "les modèles sont bien listés")

print("\n=== summary identique à description : pas de doublon ===")
verifier(texte.count("early warning for a064") == 1,
         f"le texte apparaît {texte.count('early warning for a064')} fois, 1 attendu")

print("\n=== codes détectés, y compris dans les effets ===")
rows = harvest.article_to_rows(ARTICLE_REEL)
verifier(rows and rows[0]["fault_codes"] == ["BMS_a066", "BMS_a174"],
         f"codes = {rows[0]['fault_codes'] if rows else None}")

print(f"\n{ok} ok / {fail} fail")
sys.exit(1 if fail else 0)
