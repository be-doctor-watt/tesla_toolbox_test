#!/usr/bin/env python3
"""
Mode live : le corpus se construit à l'usage.

Le chat interroge d'abord l'index local. Si un code de la question n'y est
pas encore, on va le chercher sur Toolbox avec le jeton de la session en
cours, on met l'article en cache dans raw/ ET on l'indexe. La question
suivante sur le même code est instantanée et hors-ligne.

C'est ce qui rend l'outil utilisable sans avoir à aspirer plusieurs milliers
d'articles au préalable. Les requêtes restent séquentielles et espacées comme
dans harvest.py : un burst parallèle ferait sauter la session Akamai.

Deux régimes de recherche, et c'est le point de conception :

- la question porte un CODE défaut : on filtre sur la métadonnée `fault_codes`,
  ce qui évite de confondre BMS_a066 et BMS_a067 ;
- la question est LIBRE (procédure, symptôme, pièce) : elle est réécrite en
  mots-clés anglais courts avant d'interroger Toolbox, parce que son moteur est
  lexical, exige tous les termes et n'indexe que de l'anglais.

Le jeton est conservé dans `.session.json` (0600, ignoré par git) pour que la
session survive à un redémarrage. Il n'est jamais journalisé, et expire en
~24 h côté Tesla de toute façon.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import harvest
import rag
from faultcodes import extract_codes
from llm import ErreurLLM, get_client_llm

# La session survit à un redémarrage d'uvicorn : sans ça, il faut retourner
# chercher un jeton dans le DevTools à chaque relance, ce qui rend l'outil
# pénible. Contrepartie assumée : le jeton touche le disque. Fichier en 0600,
# ignoré par git, et effacé par « Fermer la session ».
FICHIER_SESSION = Path(os.environ.get(
    "TBX_SESSION", str(Path(__file__).parent / ".session.json")))

# Nombre d'articles récupérés par code inconnu. Chaque article coûte une
# requête plus une pause de 1,2 à 2,5 s : au-delà de 3, le chat devient
# désagréablement lent pour un gain de rappel faible.
MAX_ARTICLES_PAR_CODE = 3

# Questions sans code défaut : combien de requêtes Toolbox on s'autorise, et
# combien d'articles au total. On s'arrête dès le quota d'articles atteint.
MAX_REQUETES_LIBRES = 3
MAX_ARTICLES_LIBRES = 3

# Mots vides français et anglais, pour le repli sans modèle de langage.
_MOTS_VIDES = {
    "le", "la", "les", "un", "une", "des", "du", "de", "au", "aux", "et", "ou",
    "est", "sont", "quel", "quelle", "quels", "quelles", "que", "quoi", "qui",
    "comment", "pourquoi", "quand", "où", "ou", "sur", "pour", "avec", "sans",
    "dans", "par", "en", "il", "elle", "on", "je", "tu", "ce", "cet", "cette",
    "faire", "fait", "doit", "peut", "plus", "moins", "a", "à", "the", "of",
    "and", "or", "is", "are", "how", "what", "why", "when", "where", "to",
    "for", "with", "without", "in", "on", "do", "does", "can", "should",
}

# CONFIRMÉ sur l'API réelle : le moteur de recherche Toolbox est LEXICAL et
# exige que TOUS les termes correspondent. Mesuré : « coolant » 643 résultats,
# « coolant bleed » 9, « coolant fill bleed » 0, « coolant fill and bleed
# procedure » 0. Et il n'indexe que de l'anglais : une question en français
# renvoie 0. Envoyer la question brute ne peut donc RIEN trouver, d'où cette
# étape de réécriture.
SYSTEME_MOTS_CLES = (
    "Tu convertis la question d'un technicien Tesla en requêtes pour le moteur "
    "de recherche Toolbox. Ce moteur est lexical, exige que TOUS les termes "
    "correspondent, et n'indexe que de l'anglais : au-delà de deux ou trois "
    "mots il ne renvoie plus rien. "
    "Réponds par une à trois requêtes, une par ligne, DEUX OU TROIS MOTS "
    "MAXIMUM chacune, en anglais, sans ponctuation ni numérotation, de la plus "
    "précise à la plus générale. Emploie le vocabulaire technique Tesla "
    "(brick, LDU, iBooster, PCS, HVAC, harness...). Rien d'autre que ces lignes."
)

SYSTEME = (
    "Tu es un assistant technique pour un atelier de réparation Tesla. "
    "Réponds UNIQUEMENT à partir des extraits de documentation Toolbox fournis. "
    "Cite systématiquement le numéro d'article entre crochets, par exemple [6050000]. "
    "Recopie ces numéros EXACTEMENT comme ils apparaissent dans la documentation "
    "fournie, sans ajouter ni retirer un seul chiffre, et n'en invente jamais : "
    "un numéro faux envoie le technicien lire une procédure qui ne concerne pas "
    "son véhicule. Les numéros n'ont pas tous la même longueur. "
    "Si les extraits ne contiennent pas la réponse, dis-le explicitement au lieu "
    "de deviner : une procédure inventée sur un pack haute tension est dangereuse. "
    "Si la question porte sur un code défaut absent des extraits, indique que ce "
    "code n'est pas documenté dans ce qui a été récupéré, et NE CITE AUCUN numéro "
    "d'article dans cette phrase : une référence accolée à un constat d'absence "
    "laisse croire au technicien que l'article traite du code, et il ira le lire. "
    "Si la question demande COMMENT exécuter une opération (mise à jour, reset, "
    "calibration, appairage) et que les extraits n'en décrivent que les pannes, "
    "dis-le, et signale que l'opération est peut-être une Action Toolbox "
    "(routine ODIN) plutôt qu'un article : le technicien doit alors la chercher "
    "dans l'onglet Actions, véhicule connecté. "
    "Réponds en français, même si la documentation est en anglais. Conserve les "
    "codes défaut, les couples de serrage et les valeurs numériques à l'identique."
)


def expiration_jwt(token: str) -> float | None:
    """-> horodatage d'expiration lu dans le JWT, ou None si illisible.

    Le JWT Toolbox porte `expires_at` (~24 h). Le lire permet de ne pas
    restaurer une session déjà morte au démarrage, et d'afficher l'échéance
    plutôt que de laisser le technicien la découvrir en pleine recherche.
    """
    try:
        charge = token.split(".")[1]
        charge += "=" * (-len(charge) % 4)          # padding base64url
        data = json.loads(base64.urlsafe_b64decode(charge))
        brut = data.get("expires_at") or data.get("exp")
        if isinstance(brut, (int, float)):
            return float(brut)
        if isinstance(brut, str):
            d = datetime.fromisoformat(brut)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.timestamp()
    except Exception:
        return None
    return None


class SessionToolbox:
    """Jeton Toolbox du processus. Un seul utilisateur, un seul jeton."""

    def __init__(self):
        self._http = None
        self.ouverte_le: float | None = None
        self.expire_le: float | None = None

    @property
    def active(self) -> bool:
        return self._http is not None

    @property
    def secondes_restantes(self) -> float | None:
        if self.expire_le is None:
            return None
        return max(0.0, self.expire_le - time.time())

    # ----------------------------------------------------------------
    def _sauver(self, token: str, cookie: str):
        try:
            FICHIER_SESSION.write_text(json.dumps({
                "token": token, "cookie": cookie,
                "ouverte_le": self.ouverte_le, "expire_le": self.expire_le,
            }), encoding="utf-8")
            FICHIER_SESSION.chmod(0o600)
        except OSError as e:
            # Ne pas faire échouer une connexion valide parce que le disque
            # est en lecture seule : la session reste utilisable en mémoire.
            print(f"Session non persistée ({e}), elle sera perdue au redémarrage.")

    def _oublier_fichier(self):
        try:
            FICHIER_SESSION.unlink(missing_ok=True)
        except OSError:
            pass

    def restaurer(self) -> str | None:
        """Rouvre la session depuis le disque au démarrage. -> motif d'échec.

        On ne revalide PAS auprès de Toolbox : ça coûterait une requête à
        chaque démarrage. Un jeton périmé entre-temps produira un 401 à la
        première question, que /chat gère déjà en fermant la session.
        """
        if self.active or not FICHIER_SESSION.exists():
            return None
        try:
            d = json.loads(FICHIER_SESSION.read_text(encoding="utf-8"))
            token, cookie = d["token"], d["cookie"]
        except (OSError, KeyError, ValueError) as e:
            self._oublier_fichier()
            return f"fichier de session illisible ({e})"

        expire = d.get("expire_le") or expiration_jwt(token)
        if expire and expire < time.time():
            self._oublier_fichier()
            return "le jeton enregistré a expiré"

        self._http = harvest.make_session(token, cookie)
        self.ouverte_le = d.get("ouverte_le") or time.time()
        self.expire_le = expire
        return None

    @property
    def http(self):
        if self._http is None:
            raise harvest.ErreurToolbox(
                "Aucune session Toolbox ouverte. Saisis ton jeton pour continuer.", 401)
        return self._http

    def ouvrir(self, token: str, cookie: str, requete_test: str = "BMS_a066") -> dict:
        """Valide le jeton par une vraie requête, puis retient la session.

        On valide tout de suite plutôt qu'à la première question : autant
        découvrir un cookie mal copié sur l'écran de connexion, pas au milieu
        d'une recherche.

        Ce qui est validé, c'est le JETON, et rien d'autre : si Toolbox répond
        autre chose qu'un 401/403, l'authentification fonctionne et la session
        s'ouvre. Une structure de réponse non reconnue ou zéro résultat sont
        remontés comme AVERTISSEMENT, jamais comme refus. La version
        précédente bloquait à l'écran de connexion avec un jeton parfaitement
        valide, en accusant à tort l'API : la recherche renvoie l'enveloppe
        Elasticsearch, dont la clé `hits` est un dict et non une liste.
        """
        token, cookie = token.strip(), cookie.strip()
        http = harvest.make_session(token, cookie)
        data = harvest.search(http, requete_test, 0)   # lève ErreurToolbox si 401/403
        self._http = http
        self.ouverte_le = time.time()
        self.expire_le = expiration_jwt(token)
        self._sauver(token, cookie)

        hits, cle = harvest._hits_avec_cle(data)
        ids = harvest._ids_from(hits)
        avertissement = None
        if cle is None:
            avertissement = (
                "Le jeton fonctionne, mais je ne reconnais pas la structure de la "
                "réponse de recherche : la récupération d'articles ne marchera pas. "
                f"Clés renvoyées : {sorted(data)[:12] if isinstance(data, dict) else type(data).__name__}. "
                "Complète harvest.CLES_RESULTATS.")
        elif not hits:
            avertissement = (
                f"Le jeton fonctionne, mais la requête test « {requete_test} » ne "
                "renvoie aucun résultat. Ce n'est pas forcément un problème : ce "
                "code n'est peut-être pas dans ton abonnement ou dans cette locale.")
        elif not ids:
            avertissement = (
                f"Le jeton fonctionne et la recherche renvoie {len(hits)} résultats, "
                "mais je n'arrive pas à en extraire d'identifiant d'article. "
                "Complète harvest._ids_from().")
        return {
            "articles_test": len(hits),
            "avertissement": avertissement,
            "diagnostic": {"cle_resultats": cle, "resultats": len(hits),
                           "ids": ids[:5], "requete_test": requete_test},
        }

    def fermer(self, oublier: bool = True):
        """Ferme la session. `oublier=False` garde le fichier (arrêt du serveur).

        Un arrêt d'uvicorn ne doit pas effacer le jeton, c'est justement le
        cas que la persistance sert à couvrir. Seul un « Fermer la session »
        explicite, ou un jeton refusé, l'effacent.
        """
        if self._http is not None:
            self._http.close()
        self._http = None
        self.ouverte_le = None
        self.expire_le = None
        if oublier:
            self._oublier_fichier()


SESSION = SessionToolbox()


# --------------------------------------------------------------------------
def code_present(code: str, db: str) -> bool:
    """Le code est-il déjà dans l'index ?

    Un `count` filtré, pas un scroll complet : appelé à chaque question, et
    l'inventaire complet coûte un parcours de toute la collection.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchAny

    c = rag.client(db)
    if not c.collection_exists(rag.COLLECTION):
        return False
    flt = Filter(must=[FieldCondition(key="fault_codes", match=MatchAny(any=[code]))])
    return c.count(rag.COLLECTION, count_filter=flt).count > 0


def chercher(requete: str, types: str | None = None) -> tuple[list[dict], dict]:
    """Une recherche Toolbox. -> (candidats [{id, titre}], étape de journal)."""
    data = harvest.search(SESSION.http, requete, 0, types=types)
    hits = harvest._hits(data)
    candidats = []
    for h in hits:
        ids = harvest._ids_from([h])
        if ids:
            candidats.append({"id": ids[0], "titre": harvest._titre_hit(h)})
    total = ((data.get("hits") or {}).get("total") or {}).get("value")
    return candidats, {"etape": "recherche", "requete": requete,
                       "types": types, "trouves": total if total is not None else len(hits)}


def recuperer(ids: list[int], backend: str, model: str | None,
              db: str) -> list[dict]:
    """Récupère et indexe des articles déjà choisis. -> étapes de journal."""
    journal: list[dict] = []
    for aid in ids:
        deja = (harvest.RAW / f"{aid}.json").exists()
        try:
            art = harvest.fetch_article(SESSION.http, aid)
        except harvest.ErreurToolbox as e:
            # Un 401/403/429 en cours de route veut dire jeton expiré ou
            # throttling : on remonte, ça ne se règle pas en continuant.
            if e.status_code in (401, 403, 429):
                raise
            journal.append({"etape": "article", "article_id": aid, "erreur": str(e)})
            continue
        rows = harvest.article_to_rows(art, aid)
        if not rows:
            journal.append({"etape": "article", "article_id": aid,
                            "erreur": "aucun texte extractible"})
            continue
        n = rag.index_rows(rows, backend, model, db)
        journal.append({
            "etape": "article", "article_id": aid, "titre": rows[0]["title"],
            "codes": rows[0]["fault_codes"], "chunks": n,
            "source": "cache" if deja else "toolbox",
        })
    return journal


def ingerer(requete: str, backend: str, model: str | None, db: str,
            max_articles: int = MAX_ARTICLES_PAR_CODE,
            types: str | None = None) -> list[dict]:
    """Cherche `requete` sur Toolbox, récupère et indexe les premiers articles.

    Utilisé tel quel pour un code défaut : le premier résultat d'une recherche
    par code EST la fiche du code, le classement d'Elasticsearch est bon. Pas
    de filtre par type non plus, puisqu'une fiche de code est de type Alert.
    """
    candidats, etape = chercher(requete, types)
    journal = [etape]
    if not candidats:
        return journal
    return journal + recuperer([c["id"] for c in candidats[:max_articles]],
                               backend, model, db)


SYSTEME_CHOIX = (
    "Tu tries des résultats de recherche Toolbox pour un technicien Tesla. "
    "On te donne sa question et une liste numérotée de titres d'articles. "
    "Réponds UNIQUEMENT par les numéros des articles qui répondent vraiment à "
    "la question, séparés par des virgules, du plus pertinent au moins "
    "pertinent, trois au maximum. Si aucun ne convient, réponds « aucun ». "
    "Un article qui décrit une PANNE pendant une opération ne documente pas "
    "cette opération : « Software update unsuccessful due to… » ne répond pas "
    "à « comment mettre à jour ». N'invente aucun numéro."
)


def choisir(question: str, candidats: list[dict], maxi: int) -> list[dict]:
    """Le modèle choisit par TITRE, plutôt que de suivre le classement.

    Une recherche renvoie une quinzaine de titres pour une seule requête HTTP :
    les lire coûte un appel au modèle et rien de plus, alors que récupérer un
    article de trop coûte une requête Toolbox et une pause anti-Akamai.

    Mesuré sur « comment mettre à jour le BMS » : les dix premiers résultats
    étaient des articles de PANNE pendant une mise à jour (« Software update
    unsuccessful due to… »), et la documentation utile (« How to perform a
    manual BMS reset ») arrivait bien plus loin. Prendre les trois premiers
    était donc systématiquement faux sur ce genre de question.
    """
    if len(candidats) <= maxi:
        return candidats[:maxi]
    liste = "\n".join(f"{i}. {c['titre']}" for i, c in enumerate(candidats, 1))
    try:
        reponse = get_client_llm().complete([
            {"role": "system", "content": SYSTEME_CHOIX},
            {"role": "user", "content": f"Question : {question}\n\nRésultats :\n{liste}"},
        ])
    except ErreurLLM:
        return candidats[:maxi]          # repli : le classement d'origine

    choisis = []
    for n in re.findall(r"\d+", reponse or ""):
        i = int(n) - 1
        if 0 <= i < len(candidats) and candidats[i] not in choisis:
            choisis.append(candidats[i])
        if len(choisis) >= maxi:
            break
    return choisis


_DEJA_CHERCHE: set[str] = set()


def ingerer_libre(question: str, backend: str, model: str | None,
                  db: str) -> list[dict]:
    """Question sans code : mots-clés, recherche typée, choix par titre.

    Une seule fois par question : la mémoire évite de retaper Toolbox à chaque
    reformulation de la même demande dans une conversation.
    """
    cle = " ".join(question.lower().split())
    if cle in _DEJA_CHERCHE:
        return []
    _DEJA_CHERCHE.add(cle)

    requetes = mots_cles(question)
    journal: list[dict] = [{"etape": "mots_cles", "requetes": requetes}]
    if not requetes:
        return journal

    # On écarte d'abord les fiches d'alerte et les DTC : une question sans code
    # ne les vise pas, et elles saturent le classement. Si la moisson est
    # maigre, on rouvre aux articles de panne, qui portent les procédures dans
    # leurs champs steps_to_test / steps_to_fix.
    candidats: list[dict] = []
    vus: set[int] = set()
    for types in (harvest.TYPES_PROCEDURE, harvest.TYPES_LARGES):
        for requete in requetes[:MAX_REQUETES_LIBRES]:
            trouves, etape = chercher(requete, types)
            journal.append(etape)
            for c in trouves:
                if c["id"] not in vus:
                    vus.add(c["id"])
                    candidats.append(c)
        if len(candidats) >= MAX_ARTICLES_LIBRES:
            break

    if not candidats:
        return journal
    retenus = choisir(question, candidats, MAX_ARTICLES_LIBRES)
    approximatif = False
    if not retenus:
        # Le modèle juge qu'aucun titre ne répond vraiment. Renvoyer le vide
        # serait pire : les articles les plus proches restent du contexte utile
        # au technicien, et l'avertissement dira qu'ils sont approximatifs.
        approximatif = True
        retenus = candidats[:MAX_ARTICLES_LIBRES]
    journal.append({"etape": "choix", "examines": len(candidats),
                    "approximatif": approximatif,
                    "retenus": [c["titre"] for c in retenus]})
    return journal + recuperer([c["id"] for c in retenus], backend, model, db)


def _mots_cles_repli(question: str) -> list[str]:
    """Sans modèle de langage : on retire les mots vides et on garde le plus long.

    Ne traduit pas, donc une question en français ne trouvera rien. C'est
    assumé et signalé : mieux vaut un repli honnête qu'une requête inventée.
    """
    mots = [m.strip(".,;:!?«»\"'()") for m in question.split()]
    mots = [m for m in mots if len(m) > 2 and m.lower() not in _MOTS_VIDES]
    mots.sort(key=len, reverse=True)
    return [" ".join(mots[:2])] if mots else []


def mots_cles(question: str) -> list[str]:
    """Question en langage naturel -> requêtes que le moteur Toolbox peut matcher.

    Un code défaut est déjà la meilleure requête possible : on ne le réécrit
    pas, ça ne ferait que risquer de le déformer.
    """
    if (codes := extract_codes(question)):
        return codes
    client = get_client_llm()
    try:
        texte = client.complete([
            {"role": "system", "content": SYSTEME_MOTS_CLES},
            {"role": "user", "content": question},
        ])
    except ErreurLLM:
        return _mots_cles_repli(question)

    requetes = []
    for ligne in (texte or "").splitlines():
        ligne = ligne.strip().strip("-•*0123456789. ").strip('"\'')
        mots = ligne.split()
        # Au-delà de trois mots le moteur ne renvoie plus rien : on tronque
        # plutôt que d'envoyer une requête vouée à zéro résultat.
        if 1 <= len(mots) <= 6:
            requetes.append(" ".join(mots[:3]))
        if len(requetes) >= MAX_REQUETES_LIBRES:
            break
    return requetes or _mots_cles_repli(question)


# Deux formes : « [43854] », que le modèle présente comme SA source, et
# « #3199500 », qui est le plus souvent une référence croisée recopiée depuis
# le corps d'un article. La seconde est légitime si elle figure vraiment dans
# les extraits, d'où le contrôle sur leur texte et non sur les seuls ids.
CITATION = re.compile(r"\[(\d{3,12})\]|#\s?(\d{3,12})")
NOMBRE = re.compile(r"\d{3,12}")


def verifier_citations(texte: str, sources: list[dict],
                       extraits: str = "") -> tuple[str, list[str]]:
    """Neutralise les numéros d'article introuvables dans ce qui a été fourni.

    -> (texte corrigé, numéros inventés).

    Mesuré sur une vraie réponse : pour l'article 43854, le modèle a cité
    « [4385400] », en rajoutant deux zéros par mimétisme avec les identifiants
    à sept chiffres. Un technicien qui saisit ce numéro dans Toolbox tombe sur
    une autre procédure. On ne peut pas laisser passer ça sur de la
    documentation d'atelier, donc la référence fautive est remplacée dans le
    texte affiché, et signalée.

    Un numéro est tenu pour valable s'il est l'id d'une source OU s'il figure
    tel quel dans les extraits : une référence croisée que l'article cite
    lui-même est une information utile, pas une invention.
    """
    connus = {str(s.get("article_id")) for s in sources}
    connus |= set(NOMBRE.findall(extraits or ""))
    inventes: list[str] = []

    def remplacer(m):
        num = m.group(1) or m.group(2)
        if num in connus:
            return m.group(0)
        if num not in inventes:
            inventes.append(num)
        return "[référence non vérifiée]"

    return CITATION.sub(remplacer, texte or ""), inventes


def construire_prompt(question: str, resultats: list[dict]) -> tuple[str, str, list[dict]]:
    """-> (message système, message utilisateur, sources dédoublonnées)."""
    ctx = "\n\n".join(
        f"[Article {h['article_id']} — {h['title']}]\n"
        f"Codes: {', '.join(h['fault_codes']) or '—'}\n{h['text']}"
        for h in resultats
    )
    # Plusieurs chunks viennent souvent du même article : on dédoublonne en
    # gardant l'ordre de pertinence, sinon le technicien voit trois fois le
    # même numéro d'article.
    sources = list({
        h["article_id"]: {"article_id": h["article_id"], "titre": h["title"],
                          "url": h["url"], "codes": h["fault_codes"]}
        for h in resultats
    }.values())
    return SYSTEME, f"Documentation :\n\n{ctx}\n\nQuestion : {question}", sources


def repondre(question: str, k: int = 6, backend: str = "local",
             model: str | None = None, db: str = rag.DB_PATH,
             historique: list[dict] | None = None) -> dict:
    """Cycle complet : index local, complément live si besoin, puis réponse."""
    journal: list[dict] = []
    codes = extract_codes(question)

    # 1. Ce qui manque à l'index, et qu'on peut aller chercher.
    if SESSION.active:
        if codes:
            # Question portant un code : le filtrage par code est ce qui évite
            # de confondre BMS_a066 et BMS_a067. On ne cherche que les codes.
            for code in [c for c in codes if not code_present(c, db)]:
                journal += ingerer(code, backend, model, db)
        else:
            # Question libre (procédure, symptôme, pièce). On interroge Toolbox
            # une fois par question nouvelle, sans attendre que l'index local
            # rende zéro : dès qu'il contient quelques articles, la recherche
            # sémantique renvoie TOUJOURS quelque chose, donc l'ancienne
            # condition « aucun résultat local » ne se déclenchait plus jamais
            # et rien n'était ramené de Toolbox.
            journal += ingerer_libre(question, backend, model, db)

    # 2. Recherche locale (filtrée par code si la question en contient un).
    r = rag.search(question, k, backend, model, db)

    # 3. Toujours rien : dernier recours, la question brute telle quelle.
    if not r["results"] and not codes and SESSION.active:
        journal += ingerer(question, backend, model, db)
        r = rag.search(question, k, backend, model, db)

    # 4. Quels codes de la question restent absents de l'index après tout ça ?
    #    Sans ce contrôle, le repli sémantique renvoie les articles les plus
    #    proches et le modèle répond sur un AUTRE code sans le signaler : c'est
    #    précisément le mode de défaillance que le filtrage par code évite.
    absents = [c for c in codes if not code_present(c, db)]
    avertissement = None
    if absents:
        liste = ", ".join(absents)
        if not SESSION.active:
            avertissement = (
                f"{liste} n'est pas dans l'index local et aucune session Toolbox "
                "n'est ouverte. Saisis ton jeton pour que je puisse récupérer sa "
                "fiche. Les extraits ci-dessous sont les plus proches trouvés "
                "localement et concernent d'autres codes.")
        else:
            avertissement = (
                f"Aucun article Toolbox ne documente {liste}. Le code n'est peut-être "
                "pas publié, ou il est trop récent. Les extraits ci-dessous "
                "concernent d'autres codes.")

    systeme, utilisateur, sources = construire_prompt(question, r["results"])
    if absents:
        # Le modèle doit le dire lui aussi, pas seulement le bandeau.
        utilisateur += (
            f"\n\nATTENTION : la documentation ci-dessus ne contient aucun article "
            f"portant le ou les codes suivants : {', '.join(absents)}. Commence ta "
            "réponse en le disant explicitement, et n'attribue jamais à ces codes "
            "le contenu d'un article qui en porte un autre.")

    if not r["results"]:
        manque_jeton = not SESSION.active
        return {
            "reponse": (
                "Je n'ai trouvé aucune documentation correspondant à cette question."
                + (" Aucune session Toolbox n'est ouverte, je n'ai donc pu chercher "
                   "que dans ce qui est déjà indexé : saisis ton jeton pour que je "
                   "puisse interroger Toolbox." if manque_jeton else
                   " Le code n'est peut-être pas publié dans Toolbox, ou la "
                   "formulation est trop éloignée du vocabulaire de la doc.")
            ),
            "sources": [], "codes_detectes": codes, "journal": journal,
            "modele": None, "index_vide": r["index_vide"],
            "avertissement": avertissement, "extraits": [],
            "repli_semantique": False,
        }

    messages = [{"role": "system", "content": systeme}]
    # L'historique donne le fil de la conversation au modèle. La recherche,
    # elle, ne porte que sur la question courante : réutiliser les anciennes
    # questions comme requête dégrade nettement le filtrage par code.
    for tour in (historique or [])[-6:]:
        if tour.get("role") in ("user", "assistant") and tour.get("content"):
            messages.append({"role": tour["role"], "content": tour["content"]})
    messages.append({"role": "user", "content": utilisateur})

    client = get_client_llm()
    try:
        texte = client.complete(messages)
    except ErreurLLM:
        texte = ("Le modèle de langage est injoignable, je ne peux pas rédiger de "
                 "synthèse. Les extraits trouvés sont listés ci-dessous, ils "
                 "restent exploitables tels quels.")

    texte, citations_inventees = verifier_citations(
        texte, sources, " ".join(h["text"] for h in r["results"]))
    if citations_inventees:
        avertissement = ((avertissement + " ") if avertissement else "") + (
            "Le modèle a cité des numéros d'article absents des extraits ("
            + ", ".join(citations_inventees)
            + "). Ils ont été neutralisés dans la réponse : ne les saisis pas "
              "dans Toolbox, ils ne correspondent à rien de vérifié.")

    return {
        "reponse": texte,
        "sources": sources,
        "citations_inventees": citations_inventees,
        "codes_detectes": codes,
        "codes_absents": absents,
        "avertissement": avertissement,
        "journal": journal,
        # Le modèle qui a RÉPONDU, pas celui qui était configuré : sur un repli
        # le technicien doit savoir qu'un modèle plus petit a rédigé.
        "modele": getattr(client, "dernier_modele", None) or getattr(client, "nom", None),
        "modele_prevu": getattr(client, "nom", None),
        "index_vide": r["index_vide"],
        "extraits": [{"article_id": h["article_id"], "titre": h["title"],
                      "score": h["score"], "texte": h["text"]} for h in r["results"]],
        "repli_semantique": r["fell_back_to_semantic"],
    }
