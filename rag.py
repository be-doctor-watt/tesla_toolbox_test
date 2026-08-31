#!/usr/bin/env python3
"""
Indexation et recherche du corpus Toolbox.

    python rag.py index corpus.jsonl --backend local
    python rag.py search "pourquoi BMS_a066 apparait apres remplacement module"
    python rag.py codes                      # inventaire des codes indexés

Le point clé : quand la requête contient un code défaut, on FILTRE sur la
métadonnée `fault_codes` avant la recherche vectorielle. Sans ça, la
similarité sémantique ne distingue pas BMS_a066 de BMS_a067 — les deux
chaînes sont quasi identiques pour un tokenizer, et tu récupères le mauvais
article avec un score de confiance élevé. C'est l'erreur classique sur ce
type de corpus.
"""

from __future__ import annotations

import argparse
import atexit
import json
import re
import sys
import uuid
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, FieldCondition, Filter, MatchAny, PointStruct, VectorParams,
)

from embedders import get_embedder
from faultcodes import extract_codes  # noqa: F401  (réexporté pour serve.py)

COLLECTION = "toolbox"
DB_PATH = "./qdrant_data"


# Qdrant en mode fichier pose un verrou exclusif : un seul client par process.
# On les met en cache, sinon l'API lève "storage folder is already accessed"
# à la deuxième requête.
_CLIENTS: dict[str, QdrantClient] = {}
_EMBEDDERS: dict[tuple, object] = {}


def client(path: str = DB_PATH) -> QdrantClient:
    if path not in _CLIENTS:
        _CLIENTS[path] = QdrantClient(path=path)
    return _CLIENTS[path]


def embedder(backend: str, model: str | None):
    key = (backend, model)
    if key not in _EMBEDDERS:
        _EMBEDDERS[key] = get_embedder(backend, model)
    return _EMBEDDERS[key]


@atexit.register
def _close_clients():
    # Sans ça, le __del__ de qdrant-client lève une ImportError cosmétique
    # pendant l'arrêt de l'interpréteur.
    for c in _CLIENTS.values():
        try:
            c.close()
        except Exception:
            pass
    _CLIENTS.clear()


# --------------------------------------------------------------------------
# Indexation
# --------------------------------------------------------------------------
def ensure_collection(db: str, dim: int) -> QdrantClient:
    """Crée la collection si besoin. Idempotent : sûr à appeler à chaque requête.

    Le mode live indexe article par article, la collection ne peut donc plus
    être créée uniquement par `index` : elle doit exister dès la première
    question posée dans le chat.
    """
    c = client(db)
    if not c.collection_exists(COLLECTION):
        c.create_collection(
            COLLECTION,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        # Sans effet sur la base fichier (qdrant-client le signale par un
        # warning), utile dès qu'on passe sur un Qdrant serveur.
        c.create_payload_index(COLLECTION, "fault_codes", field_schema="keyword")
        c.create_payload_index(COLLECTION, "article_id", field_schema="integer")
    return c


def index(corpus: str, backend: str = "local", model: str | None = None,
          db: str = DB_PATH, batch: int = 64, recreate: bool = True):
    rows = [json.loads(l) for l in Path(corpus).read_text(encoding="utf-8").splitlines() if l.strip()]
    if not rows:
        sys.exit(f"{corpus} est vide.")
    index_rows(rows, backend, model, db, batch, recreate, verbeux=True)


def index_rows(rows: list[dict], backend: str = "local", model: str | None = None,
               db: str = DB_PATH, batch: int = 64, recreate: bool = False,
               verbeux: bool = False) -> int:
    """Indexe des chunks déjà en mémoire. Renvoie le nombre de points écrits.

    C'est le point d'entrée du mode live : `index` (fichier) et l'ingestion
    d'un article fraîchement récupéré passent tous les deux par ici, donc la
    normalisation des codes et l'id déterministe ne sont écrits qu'une fois.
    """
    if not rows:
        return 0
    emb = embedder(backend, model)
    if verbeux:
        print(f"{len(rows)} chunks | embedder={emb.name} dim={emb.dim}")

    c = client(db)
    if recreate and c.collection_exists(COLLECTION):
        c.delete_collection(COLLECTION)
    ensure_collection(db, emb.dim)

    done = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        vecs = emb.embed([r["text"] for r in chunk], is_query=False)
        points = []
        for r, v in zip(chunk, vecs):
            # Filet de sécurité : si le harvest a raté des codes, on re-scanne
            # le texte du chunk au moment de l'indexation.
            codes = sorted(set(r.get("fault_codes") or []) | set(extract_codes(r["text"])))
            points.append(PointStruct(
                # UUID déterministe : réindexer le même chunk le met à jour
                # au lieu de le dupliquer.
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, str(r["id"]))),
                vector=v,
                payload={
                    "chunk_id": r["id"],
                    "article_id": r.get("article_id"),
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "fault_codes": codes,
                    "text": r["text"],
                },
            ))
        c.upsert(COLLECTION, points=points)
        done += len(points)
        if verbeux:
            print(f"  {done}/{len(rows)}", end="\r", flush=True)
    if verbeux:
        print(f"\nIndexé : {done} chunks dans {db}")
    return done


# --------------------------------------------------------------------------
# Recherche
# --------------------------------------------------------------------------
def search(query: str, top_k: int = 5, backend: str = "local",
           model: str | None = None, db: str = DB_PATH,
           codes: list[str] | None = None, strict: bool = False) -> dict:
    emb = embedder(backend, model)
    c = client(db)

    # `codes is None` = « détecte-les toi-même » ; `codes=[]` = « pas de filtre ».
    # Un `or` traiterait la liste vide comme non renseignée et re-filtrerait.
    codes = extract_codes(query) if codes is None else codes

    # La collection peut ne pas exister encore (première question du chat,
    # avant tout crawl). On renvoie un résultat vide au lieu de lever.
    if not c.collection_exists(COLLECTION):
        return {"query": query, "codes_detected": codes, "filtered": False,
                "fell_back_to_semantic": False, "index_vide": True, "results": []}

    qvec = emb.embed([query], is_query=True)[0]

    flt = None
    if codes:
        flt = Filter(must=[FieldCondition(key="fault_codes", match=MatchAny(any=codes))])

    hits = c.query_points(COLLECTION, query=qvec, limit=top_k,
                          query_filter=flt, with_payload=True).points

    # Un code peut être absent du corpus (article non publié, code trop récent).
    # Sans repli, l'utilisateur reçoit zéro résultat et croit que le RAG est cassé.
    fell_back = False
    if codes and not hits and not strict:
        fell_back = True
        hits = c.query_points(COLLECTION, query=qvec, limit=top_k,
                              with_payload=True).points

    return {
        "query": query,
        "codes_detected": codes,
        "filtered": bool(flt) and not fell_back,
        "fell_back_to_semantic": fell_back,
        "index_vide": False,
        "results": [{
            "score": round(h.score, 4),
            "article_id": h.payload.get("article_id"),
            "title": h.payload.get("title"),
            "url": h.payload.get("url"),
            "fault_codes": h.payload.get("fault_codes"),
            "text": h.payload.get("text"),
        } for h in hits],
    }


def list_codes(db: str = DB_PATH) -> dict[str, int]:
    """Inventaire code -> nombre d'articles distincts."""
    c = client(db)
    if not c.collection_exists(COLLECTION):
        return {}
    tally: dict[str, set] = {}
    offset = None
    while True:
        pts, offset = c.scroll(COLLECTION, limit=512, offset=offset, with_payload=True)
        for p in pts:
            for code in p.payload.get("fault_codes") or []:
                tally.setdefault(code, set()).add(p.payload.get("article_id"))
        if offset is None:
            break
    return {k: len(v) for k, v in sorted(tally.items())}


# --------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index")
    pi.add_argument("corpus")
    pi.add_argument("--backend", default="local", choices=["local", "openai", "hash"])
    pi.add_argument("--model", default=None)
    pi.add_argument("--db", default=DB_PATH)

    ps = sub.add_parser("search")
    ps.add_argument("query")
    ps.add_argument("-k", type=int, default=5)
    ps.add_argument("--backend", default="local", choices=["local", "openai", "hash"])
    ps.add_argument("--model", default=None)
    ps.add_argument("--db", default=DB_PATH)
    ps.add_argument("--strict", action="store_true",
                    help="pas de repli sémantique si le code est absent du corpus")

    pc = sub.add_parser("codes")
    pc.add_argument("--db", default=DB_PATH)

    a = ap.parse_args()

    if a.cmd == "index":
        if not Path(a.corpus).exists():
            sys.exit(f"{a.corpus} n'existe pas. Lance d'abord "
                     "`python harvest.py crawl` puis `python harvest.py build`.")
        index(a.corpus, a.backend, a.model, a.db)

    elif a.cmd == "search":
        r = search(a.query, a.k, a.backend, a.model, a.db, strict=a.strict)
        if r["index_vide"]:
            sys.exit(f"Aucun index dans {a.db}. Lance d'abord "
                     "`python rag.py index corpus.jsonl`, ou utilise le chat "
                     "(`uvicorn serve:app`) qui alimente l'index à l'usage.")
        print(f"\nCodes détectés : {r['codes_detected'] or '—'}"
              f" | filtré : {r['filtered']}"
              f"{'  (REPLI sémantique : code absent du corpus)' if r['fell_back_to_semantic'] else ''}\n")
        if not r["results"]:
            print("Aucun résultat. Le code n'est pas dans le corpus indexé "
                  "(relance sans --strict pour un repli sémantique).\n")
        for h in r["results"]:
            print(f"[{h['score']}] #{h['article_id']} {h['title']}")
            print(f"        codes: {', '.join(h['fault_codes']) or '—'}")
            print(f"        {h['url']}")
            print(f"        {h['text'][:220].replace(chr(10), ' ')}...\n")

    elif a.cmd == "codes":
        t = list_codes(a.db)
        if not t:
            sys.exit(f"Index vide ou absent dans {a.db}.")
        for k, v in t.items():
            print(f"{k:16s} {v} article(s)")
        print(f"\n{len(t)} codes distincts")
