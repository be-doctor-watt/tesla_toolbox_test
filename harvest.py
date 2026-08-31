#!/usr/bin/env python3
"""
Aspiration du corpus Toolbox -> corpus.jsonl.

À LANCER SUR TA MACHINE, dans le même réseau que le navigateur d'où viennent
les cookies. Depuis un serveur distant, Akamai détecte l'écart IP/session et
coupe ta session Toolbox.

    set TBX_TOKEN=eyJhbGci...
    set TBX_COOKIE=device_hash=...; tbx_token=...; _abck=...; bm_sz=...

    python harvest.py discover "BMS_a066"   # 1. valider les endpoints
    python harvest.py crawl                 # 2. aspirer (reprend où ça s'arrête)
    python harvest.py build                 # 3. -> corpus.jsonl

Le token expire en ~24 h. Le cache disque (raw/) fait qu'une reprise après
expiration ne retélécharge rien : tu remets un token frais et tu relances.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from html import unescape
from pathlib import Path

import requests

import config  # noqa: F401  .env avant la lecture de TBX_RAW / TBX_LOCALE ci-dessous
from faultcodes import extract_codes

# --------------------------------------------------------------------------
# CONFIRMÉ (DevTools) : la recherche est un GET, pagination à partir de 0.
#   GET /api/toolbox/articles/search?q=BMS_a066&page=0&locale=en_US
BASE = "https://toolbox.tesla.com"
SEARCH_URL = f"{BASE}/api/toolbox/articles/search"
LOCALE = os.environ.get("TBX_LOCALE", "en_US")

# CONFIRMÉ : le contenu d'un article passe par l'API v2 (pas /api/toolbox/).
#   GET /api/v2/articles/{id}?expand=...&locale=en_US
ARTICLE_URL = f"{BASE}/api/v2/articles/{{id}}"

# Ces expansions sont l'intérêt principal de l'endpoint : causes_virtual et
# effects_virtual portent les causes et effets structurés d'un code défaut,
# qualifiers les conditions de déclenchement. Sur un article d'alerte, c'est
# souvent plus dense et plus exploitable que le corps rédigé.
ARTICLE_EXPAND = ("categories,causes_virtual,created_by,effects_virtual,network,"
                  "qualifiers,status,system,updated_at,updated_by,updated_by_id,users")

RAW = Path(os.environ.get("TBX_RAW", "raw"))
DELAY = (1.2, 2.5)   # séquentiel et lent : un burst = session Akamai perdue


class ErreurToolbox(Exception):
    """Échec côté Toolbox. Porte le code HTTP pour que l'appelant décide.

    On lève au lieu de sortir : ce module est aussi importé par serve.py, et
    un `sys.exit` dans un handler HTTP tuerait le processus uvicorn. Le CLI,
    lui, rattrape et sort proprement (voir le bloc __main__).
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def make_session(token: str, cookie: str) -> requests.Session:
    """Session HTTP portant le JWT et les cookies récupérés dans le DevTools."""
    s = requests.Session()
    s.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Cookie": cookie,
        "Origin": BASE,
        "Referer": f"{BASE}/",
        # Doit être IDENTIQUE à l'UA du navigateur d'où viennent les cookies.
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/151.0.0.0 Safari/537.36"),
        "sec-ch-ua-platform": '"Windows"',
    })
    return s


def credentials_depuis(collage: str) -> tuple[str, str]:
    """Un collage quelconque -> (jeton, cookie). Lève ErreurToolbox si illisible.

    CONFIRMÉ sur l'API réelle : le jeton de l'en-tête `Authorization` EST la
    valeur du cookie `tbx_token`, et ce cookie seul suffit à s'authentifier
    (testé sans `_abck` ni `bm_sz`). Il n'y a donc qu'un secret à récupérer,
    et on accepte les trois formes qu'un technicien peut copier :

      - toute la ligne Cookie   (« device_hash=…; tbx_token=eyJ…; _abck=… »)
      - l'en-tête Authorization (« Bearer eyJ… », ou juste « eyJ… »)
      - le seul cookie tbx_token

    On conserve la ligne de cookie complète quand elle est fournie : Akamai
    peut exiger `_abck` dans d'autres conditions (autre IP, score de bot
    dégradé), autant ne pas jeter ce qu'on a.
    """
    texte = (collage or "").strip()
    # Les en-têtes copiés en entier gardent leur nom devant.
    texte = re.sub(r"(?i)^\s*(authorization|cookie)\s*:\s*", "", texte)
    texte = re.sub(r"(?i)^\s*bearer\s+", "", texte).strip().strip('"\'')
    if not texte:
        raise ErreurToolbox("Rien à lire : le collage est vide.")

    m = re.search(r"(?:^|;)\s*tbx_token\s*=\s*([^;\s]+)", texte)
    if m:
        jeton = m.group(1)
        # Une ligne de cookie contient au moins un « = » ailleurs que dans le
        # jeton ; sinon c'est le cookie tbx_token seul et on le normalise.
        cookie = texte if ";" in texte else f"tbx_token={jeton}"
        return jeton, cookie

    # Pas de nom de cookie : un JWT nu (trois segments séparés par des points).
    if re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*", texte):
        return texte, f"tbx_token={texte}"

    raise ErreurToolbox(
        "Je ne reconnais ni un jeton ni une ligne de cookie. Colle soit toute "
        "la ligne « Cookie » du DevTools, soit la valeur de « Authorization » "
        "(elle commence par « eyJ »).")


def session() -> requests.Session:
    token, cookie = os.environ.get("TBX_TOKEN"), os.environ.get("TBX_COOKIE")
    if not token or not cookie:
        raise ErreurToolbox(
            "Définis TBX_TOKEN et TBX_COOKIE (copiés depuis le DevTools).")
    return make_session(token, cookie)


def nap():
    time.sleep(random.uniform(*DELAY))


def check(r: requests.Response) -> requests.Response:
    if r.status_code in (401, 403):
        raise ErreurToolbox(
            f"HTTP {r.status_code} : token expiré ou session Akamai perdue.\n"
            "Recopie un TBX_TOKEN et un TBX_COOKIE frais, puis relance "
            "`crawl` — le cache raw/ reprend où tu t'étais arrêté.",
            r.status_code)
    if r.status_code == 429:
        raise ErreurToolbox("HTTP 429 : tu vas trop vite. Augmente DELAY.", 429)
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        raise ErreurToolbox(f"HTTP {r.status_code} sur {r.url}", r.status_code) from e
    return r


# --------------------------------------------------------------------------
CLES_RESULTATS = ("results", "hits", "data", "articles", "items",
                  "documents", "records", "content", "rows", "elements")


def _hits_avec_cle(data) -> tuple[list, str | None]:
    """-> (résultats, nom de la clé trouvée). Clé None = structure non reconnue.

    Distinguer les deux cas est indispensable : une liste vide sous une clé
    connue veut dire « 0 résultat pour cette requête », alors qu'aucune clé
    connue veut dire « l'API a changé ». Confondre les deux fait accuser
    l'API quand la recherche n'a simplement rien trouvé.
    """
    if not isinstance(data, dict):
        return [], None
    for k in CLES_RESULTATS:
        v = data.get(k)
        if isinstance(v, list):
            return v, k
    # CONFIRMÉ : la recherche Toolbox est un Elasticsearch, et renvoie
    # l'enveloppe ES : {"hits": {"hits": [...], "total": ..., "max_score": ...}}.
    # La clé `hits` de premier niveau est donc un DICT, pas une liste, et c'est
    # `hits.hits` qui porte les documents.
    for conteneur in ("hits", "data", "result", "response", "payload"):
        interne = data.get(conteneur)
        if isinstance(interne, dict):
            for k in CLES_RESULTATS:
                v = interne.get(k)
                if isinstance(v, list):
                    return v, f"{conteneur}.{k}"
    return [], None


def _hits(data: dict) -> list:
    """Les API Toolbox varient : on tolère plusieurs noms de clés."""
    return _hits_avec_cle(data)[0]


def _body(art: dict) -> str:
    """Corps rédigé de l'article (peut être vide sur une fiche d'alerte)."""
    for k in ("body", "content", "html", "bodyHtml", "description", "text"):
        v = art.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _render_items(items) -> list[str]:
    """Rend une liste d'objets (causes, effets, qualifiers) en lignes lisibles.

    On ne connaît pas le nom exact des champs, donc on prend les clés les plus
    porteuses de texte et on ignore les ids et horodatages.
    """
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    lines = []
    for it in items:
        if isinstance(it, str):
            lines.append(it.strip())
            continue
        if not isinstance(it, dict):
            continue
        parts = []
        for k in ("name", "title", "label", "code", "value",
                  "description", "text", "summary", "body"):
            v = it.get(k)
            if isinstance(v, str) and v.strip() and v.strip() not in parts:
                parts.append(v.strip())
        if parts:
            lines.append(" — ".join(parts))
    return [l for l in lines if l]


# CONFIRMÉ sur l'API réelle : un article d'alerte n'a NI `body` NI `content`.
# Le texte utile est réparti sur ces clés, et `firmware_details` (une table de
# signaux en HTML) peut peser 9 ko à elle seule, soit quinze fois le reste.
# Les ignorer, c'était indexer 4 % de l'article.
SECTIONS_TEXTE = (
    ("steps_to_test", "PROCÉDURE DE CONTRÔLE"),
    ("steps_to_fix", "PROCÉDURE DE CORRECTION"),
    ("firmware_details", "SIGNAUX ET DÉTAILS FIRMWARE"),
    ("engineering_notes", "NOTES D'INGÉNIERIE"),
    ("phone_support_notes", "NOTES DU SUPPORT"),
    ("additional_info", "INFORMATIONS COMPLÉMENTAIRES"),
)

# Les qualifiers ne sont pas tous de même nature : Make/Model disent sur quels
# véhicules l'article s'applique, ce qui n'est PAS une condition de
# déclenchement. Les ranger sous le même titre induit le technicien en erreur.
QUALIFIERS_VEHICULE = ("MakeQualifier", "ModelQualifier", "YearQualifier",
                       "TrimQualifier", "RegionQualifier", "CountryQualifier")


def _qualifiers_par_nature(items) -> list[tuple[str, list[str]]]:
    """-> [(titre de section, lignes)] en séparant applicabilité et conditions."""
    if not isinstance(items, list):
        return [("CONDITIONS DE DÉCLENCHEMENT", _render_items(items))] if items else []
    vehicule, conditions = [], []
    for it in items:
        (vehicule if isinstance(it, dict)
         and it.get("class_type") in QUALIFIERS_VEHICULE else conditions).append(it)
    out = []
    for lot, titre in ((vehicule, "MODÈLES CONCERNÉS"),
                       (conditions, "CONDITIONS DE DÉCLENCHEMENT")):
        lignes = _render_items(lot)
        if lignes:
            out.append((titre, lignes))
    return out


def flatten_article(art: dict) -> str:
    """Article -> texte pour le RAG : corps + champs structurés.

    Les sections sont nommées en clair ('CAUSES POSSIBLES', 'EFFETS') parce que
    l'embedder et le LLM en aval s'en servent : un extrait qui commence par
    « CAUSES POSSIBLES » répond bien mieux à « pourquoi ce code apparaît ».
    """
    out = []
    vus: set[str] = set()

    def ajouter(texte: str, titre: str | None = None):
        """Ajoute une section, en sautant les doublons littéraux.

        `summary` et `description` sont souvent identiques mot pour mot : les
        indexer deux fois gonfle le chunk et fait remonter le même passage
        deux fois dans les résultats.
        """
        texte = (strip_html(texte) if "<" in texte else texte).strip()
        if not texte or texte in vus:
            return
        vus.add(texte)
        out.append(f"{titre} :\n{texte}" if titre else texte)

    raw = _body(art)
    if raw:
        ajouter(raw)
    # `summary` n'est pas dans _body() : sur certains articles c'est le seul
    # texte rédigé, et il diffère de `description`.
    if isinstance(art.get("summary"), str):
        ajouter(art["summary"])

    for key, heading in SECTIONS_TEXTE:
        v = art.get(key)
        if isinstance(v, str) and v.strip():
            ajouter(v, heading)
        elif isinstance(v, list) and v:
            lignes = _render_items(v)
            if lignes:
                ajouter("\n".join(f"- {l}" for l in lignes), heading)

    for key, heading in (
        ("causes_virtual", "CAUSES POSSIBLES"),
        ("causes", "CAUSES POSSIBLES"),
        ("effects_virtual", "EFFETS SUR LE VÉHICULE"),
        ("effects", "EFFETS SUR LE VÉHICULE"),
    ):
        lines = _render_items(art.get(key))
        if lines:
            out.append(heading + " :\n" + "\n".join(f"- {l}" for l in lines))

    for titre, lignes in _qualifiers_par_nature(art.get("qualifiers")):
        out.append(titre + " :\n" + "\n".join(f"- {l}" for l in lignes))

    meta = []
    for key, label in (("system", "Système"), ("network", "Réseau"),
                       ("categories", "Catégories"), ("status", "Statut")):
        v = art.get(key)
        if isinstance(v, dict):
            v = v.get("name") or v.get("title") or v.get("code")
        elif isinstance(v, list):
            v = ", ".join(filter(None, (
                x.get("name") or x.get("title") if isinstance(x, dict) else str(x)
                for x in v)))
        if v:
            meta.append(f"{label}: {v}")
    if meta:
        out.append(" | ".join(meta))

    return "\n\n".join(p for p in out if p and p.strip())


# CONFIRMÉ sur l'API réelle : le paramètre `types` filtre par type d'article,
# valeurs séparées par des virgules. Vérifié sur « BMS update » : 1039 résultats
# sans filtre, 38 avec `types=FAQ`, 45 avec `types=FAQ,Topic,Action`, ce qui
# correspond exactement aux comptes de l'agrégation `by_type`.
#
# Types observés, avec leur type_id : Alert (310), Issue (306), DTC (1436),
# FAQ (313), Action (483), Symptom (311), Topic (624).
#
# Ça compte : sur une question « comment faire… », Elasticsearch classe en tête
# les fiches d'alerte et les articles de panne dont le titre contient les mêmes
# mots (« Software update unsuccessful due to… »), et la vraie documentation
# (« How to perform a manual BMS reset ») se retrouve au-delà du 15e rang.
TYPES_PROCEDURE = "FAQ,Topic,Action"
TYPES_LARGES = "FAQ,Topic,Action,Issue,Symptom"


def search(s, query="", page=0, types: str | None = None) -> dict:
    """GET /api/toolbox/articles/search?q=...&page=N&locale=en_US (page 0-indexée).

    `types` restreint aux types d'articles voulus (voir TYPES_PROCEDURE).
    """
    params = {"q": query, "page": page, "locale": LOCALE}
    if types:
        params["types"] = types
    return check(s.get(SEARCH_URL, params=params)).json()


def _id_dans(obj) -> int | None:
    """Cherche un id d'article dans un dict, sans descendre récursivement."""
    if not isinstance(obj, dict):
        return None
    for k in ("id", "entity_id", "articleId", "article_id"):
        v = obj.get(k)
        if isinstance(v, (int, str)) and str(v).strip().isdigit():
            return int(v)
    return None


def _ids_from(hits: list) -> list[int]:
    """Ids d'article d'une page de résultats.

    CONFIRMÉ sur l'API réelle : un document ES a la forme
    {"_id": ..., "_source": {"article": {"id": ..., "title": ...}, ...}}.
    L'id utile est donc sous `_source.article.id`, deux niveaux plus bas que
    ce qu'on pouvait supposer. Les autres chemins restent essayés, l'API a
    déjà changé une fois.
    """
    out = []
    for h in hits:
        if not isinstance(h, dict):
            continue
        src = h.get("_source") if isinstance(h.get("_source"), dict) else {}
        aid = (_id_dans(h)
               or _id_dans(src)
               or _id_dans(src.get("article"))
               or _id_dans(h.get("article")))
        # `_id` d'Elasticsearch en dernier recours : sur Toolbox il vaut l'id
        # de l'article, mais rien ne le garantit sur un autre index.
        if aid is None and str(h.get("_id", "")).isdigit():
            aid = int(h["_id"])
        if aid:
            out.append(aid)
    return out


def _titre_hit(h: dict) -> str:
    """Titre porté par un résultat de recherche (avant récupération complète)."""
    src = h.get("_source") if isinstance(h.get("_source"), dict) else {}
    for conteneur in (src.get("article"), src, h):
        if isinstance(conteneur, dict):
            for k in ("title", "name", "titre"):
                v = conteneur.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return ""


def search_ids(s, query="", max_pages=400) -> list[int]:
    """Pagine une requête jusqu'à épuisement."""
    ids, page = [], 0
    while page < max_pages:
        hits = _hits(search(s, query, page))
        if not hits:
            break
        new = _ids_from(hits)
        ids.extend(new)
        print(f"    p{page}: +{len(new)} (cumul {len(set(ids))})", flush=True)
        page += 1
        nap()
    return sorted(set(ids))


# Si q="" ne renvoie rien, on ne peut pas énumérer le catalogue d'un coup.
# On l'attaque alors par familles de codes : l'union des résultats couvre
# l'essentiel du corpus de diagnostic. Complète cette liste au besoin —
# `python rag.py codes` te dira ce que tu as effectivement ramassé.
SEED_QUERIES = [
    "BMS", "DI", "CP", "CC", "UI", "VCFRONT", "VCSEC", "VCRIGHT", "VCLEFT",
    "EPBL", "EPBR", "ESP", "APP", "GTW", "HVP", "PCS", "CHG", "CHGDCDC",
    "THC", "PM", "SDM", "OCS", "TAS", "DIS", "DIR", "MCU", "ICE", "BATT",
    "alert", "fault", "diagnostic", "procedure", "service",
]


def crawl_ids(s, query=None) -> list[int]:
    if query:
        return search_ids(s, query)

    print("  test d'une requête vide (énumération complète)...")
    ids = search_ids(s, "")
    if ids:
        print(f"  -> catalogue énuméré directement : {len(ids)} articles")
        return ids

    print("  -> requête vide non supportée, passage par les requêtes-graines")
    allids: set[int] = set()
    for q in SEED_QUERIES:
        print(f"  graine « {q} »")
        before = len(allids)
        allids |= set(search_ids(s, q))
        print(f"    +{len(allids) - before} nouveaux (total {len(allids)})")
    return sorted(allids)


def fetch_article(s, aid: int) -> dict:
    RAW.mkdir(exist_ok=True)   # le mode live n'a pas de phase d'init
    path = RAW / f"{aid}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    data = check(s.get(ARTICLE_URL.format(id=aid),
                       params={"expand": ARTICLE_EXPAND, "locale": LOCALE})).json()
    # Certaines API v2 emballent la ressource dans {"data": {...}} ou {"article": {...}}
    if isinstance(data, dict) and len(data) <= 2:
        for k in ("data", "article", "result"):
            if isinstance(data.get(k), dict):
                data = data[k]
                break
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    nap()
    return data


# --------------------------------------------------------------------------
# Une balise commence par une lettre ou « / », et ne peut pas contenir de « < ».
# Le motif naïf `<[^>]+>` détruit du contenu réel : le HTML Toolbox contient des
# « < » littéraux non échappés, par exemple
#   « imbalance between average SOC and min SOC < (SOC_..._THRESHOLD - ...) </td> »
# où `<[^>]+>` avale depuis le « < » jusqu'au « > » de `</td>`, donc la
# condition d'extinction du code défaut entière. Sur de la doc d'atelier, une
# valeur de seuil qui disparaît sans erreur est exactement ce qu'il ne faut pas.
BALISE = re.compile(r"</?[A-Za-z][^<>]*>|<!--.*?-->|<!\[CDATA\[.*?\]\]>|<![^<>]*>", re.S)


def strip_html(html: str) -> str:
    txt = re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    txt = re.sub(r"(?i)<br\s*/?>", "\n", txt)
    txt = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|td)>", "\n", txt)
    txt = BALISE.sub(" ", txt)
    txt = unescape(txt)
    txt = re.sub(r"[ \t]{2,}", " ", txt)
    return re.sub(r"\n{3,}", "\n\n", txt).strip()


def chunk(text: str, size=1200, overlap=200) -> list[str]:
    """Découpage par paragraphes regroupés, avec recouvrement.

    On ne coupe jamais au milieu d'un paragraphe : sur de la doc de
    procédure, couper une étape en deux produit des réponses tronquées
    du type « serrer à ... » sans le couple.
    """
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    out, buf = [], ""
    for p in paras:
        if buf and len(buf) + len(p) > size:
            out.append(buf)
            buf = (buf[-overlap:] + "\n" + p).strip()
        else:
            buf = f"{buf}\n{p}".strip()
    if buf:
        out.append(buf)
    return out or [text]


def article_to_rows(art: dict, aid: int | None = None) -> list[dict]:
    """Article brut -> chunks prêts à indexer. Liste vide si aucun texte.

    Partagé par `build` (corpus complet vers fichier) et par le mode live du
    chat, qui indexe un article à la volée : le format des chunks ne doit
    exister qu'ici, sinon les deux chemins divergent.
    """
    aid = int(aid or art.get("id") or 0)
    title = art.get("title") or art.get("name") or f"Article {aid}"
    text = flatten_article(art)
    if not text.strip():
        return []

    codes = set(extract_codes(f"{title}\n{text}"))
    for k in ("faultCodes", "alerts", "tags", "keywords"):
        for v in art.get(k) or []:
            codes |= set(extract_codes(str(v)))

    return [{
        "id": f"{aid}#{i}",
        "article_id": aid,
        "title": title,
        "fault_codes": sorted(codes),
        "url": f"{BASE}/articles/{aid}",
        # Le titre est répété dans chaque chunk : il porte souvent le code
        # défaut, et sans lui un chunk du milieu d'article devient non
        # identifiable pour l'embedder.
        "text": f"{title}\n\n{c}",
    } for i, c in enumerate(chunk(text))]


def build(out_path="corpus.jsonl"):
    files = sorted(RAW.glob("*.json"))
    if not files:
        sys.exit("raw/ est vide — lance d'abord `python harvest.py crawl`.")
    n, skipped = 0, []
    with open(out_path, "w", encoding="utf-8") as f:
        for path in files:
            art = json.loads(path.read_text(encoding="utf-8"))
            rows = article_to_rows(art, art.get("id") or int(path.stem))
            if not rows:
                skipped.append(art.get("id") or int(path.stem))
                continue
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                n += 1
    print(f"{n} chunks depuis {len(files) - len(skipped)} articles -> {out_path}")
    if skipped:
        # Un article vide signale presque toujours une clé non reconnue dans
        # _body()/flatten_article(), pas un article réellement vide.
        print(f"!! {len(skipped)} articles sans texte extractible, ex. {skipped[:5]}")
        print("   Inspecte raw/<id>.json et complète flatten_article().")


# --------------------------------------------------------------------------
def _cli():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "discover"
    RAW.mkdir(exist_ok=True)

    if cmd == "discover":
        s = session()
        q = sys.argv[2] if len(sys.argv) > 2 else "BMS_a066"
        data = search(s, q)
        print("=== RECHERCHE ===")
        print(json.dumps(data, indent=2, ensure_ascii=False)[:3000])
        hits = _hits(data)
        print(f"\n--> {len(hits)} résultats reconnus par _hits()")
        if not hits:
            sys.exit("0 résultat : la clé du tableau n'est pas reconnue. "
                     "Regarde les clés ci-dessus et ajoute-la dans _hits().")
        ids = _ids_from(hits)
        print(f"--> ids extraits : {ids[:10]}")
        if not ids:
            sys.exit("Aucun id extrait : ajoute la bonne clé dans _ids_from().")

        print("\n=== ARTICLE (api/v2 + expand) ===")
        art = fetch_article(s, ids[0])
        print("clés :", list(art)[:40])
        for k in ("causes_virtual", "effects_virtual", "qualifiers",
                  "system", "network", "categories"):
            v = art.get(k)
            kind = (f"liste de {len(v)}" if isinstance(v, list)
                    else type(v).__name__ if v is not None else "absent")
            print(f"  {k:18s} {kind}")
            if isinstance(v, list) and v:
                print(f"       1er élément : {json.dumps(v[0], ensure_ascii=False)[:200]}")

        text = flatten_article(art)
        print(f"\n--> texte assemblé : {len(text)} caractères")
        if not text:
            sys.exit("Texte vide : complète _body() / flatten_article() "
                     f"à partir des clés ci-dessus (voir raw/{ids[0]}.json).")
        print("--- début ---")
        print(text[:800])
        print("--- fin ---")
        print(f"\ncodes détectés : {extract_codes(art.get('title', '') + chr(10) + text)}")
        print("\nTout est bon, tu peux lancer : python harvest.py crawl")

    elif cmd == "crawl":
        s = session()
        q = sys.argv[2] if len(sys.argv) > 2 else None
        ids = crawl_ids(s, q)
        Path("ids.json").write_text(json.dumps(ids))
        todo = [i for i in ids if not (RAW / f"{i}.json").exists()]
        print(f"{len(ids)} articles, {len(todo)} à télécharger")
        for i, aid in enumerate(todo, 1):
            try:
                fetch_article(s, aid)
            except ErreurToolbox as e:
                # Token expiré ou throttling : fatal, on arrête tout. Une autre
                # erreur Toolbox ne concerne qu'un article, on continue.
                if e.status_code in (401, 403, 429):
                    raise
                print(f"  !! {aid}: {e}")
            except Exception as e:
                print(f"  !! {aid}: {e}")
            if i % 25 == 0:
                print(f"  {i}/{len(todo)}", flush=True)
        print("Terminé. Lance maintenant : python harvest.py build")

    elif cmd == "build":
        build()

    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        _cli()
    except ErreurToolbox as e:
        # Le CLI sort proprement ; l'exception, elle, reste rattrapable par
        # serve.py qui ne peut pas se permettre de tuer son processus.
        sys.exit(str(e))
