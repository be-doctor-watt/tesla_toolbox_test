#!/usr/bin/env python3
"""
API + interface de recherche du corpus Toolbox.

    uvicorn serve:app --host 127.0.0.1 --port 8000
    -> http://127.0.0.1:8000   (interface complète, rien d'autre à lancer)

    POST /session/toolbox  {"collage": "<ligne Cookie ou jeton>"}  -> ouvre la session
                           (posté par l'extension navigateur)
    GET  /session/toolbox                                     -> état + échéance
    DELETE /session/toolbox                                   -> ferme et oublie
    POST /chat  {"question": "...", "conversation_id": "..."}  -> réponse rédigée
    GET/POST /conversations        GET/DELETE /conversations/{id}
    GET  /search?q=...&k=5      GET /codes      GET /article/6050000
    POST /answer  {"q": "..."}  -> contexte + prompt seuls, sans génération

Écoute sur 127.0.0.1 : le corpus est sous copyright Tesla, et la session
Toolbox est nominative. Ne l'expose pas sur le réseau.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

import config  # noqa: F401  charge .env AVANT que harvest lise TBX_RAW à l'import

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import conversations
import live
import rag
import harvest
from harvest import ErreurToolbox

BACKEND = os.environ.get("RAG_BACKEND", "local")
MODEL = os.environ.get("RAG_MODEL") or None
DB = os.environ.get("RAG_DB", rag.DB_PATH)

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Charge le modèle d'embedding et ouvre Qdrant au démarrage plutôt qu'à la
    # première requête. Au premier lancement, FastEmbed télécharge ~2 Go de
    # modèle ONNX : c'est ici que ça se passe, et ça peut durer plusieurs
    # minutes. Mieux vaut attendre au démarrage qu'un premier chat en timeout.
    print(f"Chargement de l'embedder ({BACKEND}, {MODEL or 'défaut'})...", flush=True)
    emb = rag.embedder(BACKEND, MODEL)
    print(f"Embedder prêt : {emb.name}, {emb.dim} dimensions", flush=True)
    rag.client(DB)
    motif = live.SESSION.restaurer()
    if live.SESSION.active:
        reste = live.SESSION.secondes_restantes
        print("Session Toolbox restaurée"
              + (f", valable encore {reste / 3600:.1f} h" if reste else ""), flush=True)
    elif motif:
        print(f"Session Toolbox non restaurée : {motif}", flush=True)
    yield
    # `oublier=False` : un arrêt du serveur ne doit pas effacer le jeton, c'est
    # précisément ce que la persistance sert à éviter.
    live.SESSION.fermer(oublier=False)


app = FastAPI(title="Toolbox RAG — Doctor-Watt", version="2.0", lifespan=lifespan)

# --------------------------------------------------------------------------
# Session Toolbox
# --------------------------------------------------------------------------
class Jeton(BaseModel):
    # Un seul collage suffit : le jeton de l'en-tête Authorization EST le
    # cookie tbx_token. `token`/`cookie` restent acceptés pour un appel direct.
    collage: str | None = None
    token: str | None = None
    cookie: str | None = None


@app.post("/session/toolbox")
def ouvrir_session(j: Jeton):
    """Valide le jeton par une vraie requête Toolbox, puis ouvre la session.

    Le jeton n'est jamais renvoyé au navigateur : le frontend ne sait que si
    la session est ouverte, et jusqu'à quand. Il est en revanche écrit dans
    `.session.json` (0600, ignoré par git) pour survivre à un redémarrage du
    serveur ; « Fermer la session » l'efface.
    """
    try:
        if j.collage and j.collage.strip():
            token, cookie = harvest.credentials_depuis(j.collage)
        elif j.token and j.token.strip():
            token = j.token.strip()
            cookie = (j.cookie or "").strip() or f"tbx_token={token}"
        else:
            raise HTTPException(400, "Colle la ligne « Cookie » ou le jeton "
                                     "« Authorization » du DevTools.")
    except ErreurToolbox as e:
        raise HTTPException(400, str(e))
    try:
        resume = live.SESSION.ouvrir(token, cookie)
    except ErreurToolbox as e:
        raise HTTPException(401, str(e))
    except Exception as e:
        raise HTTPException(502, f"Toolbox est injoignable : {e}")
    return {"active": True, **resume, **_inventaire()}


@app.get("/session/toolbox")
def etat_session():
    return {"active": live.SESSION.active,
            "ouverte_le": live.SESSION.ouverte_le,
            "expire_le": live.SESSION.expire_le,
            "secondes_restantes": live.SESSION.secondes_restantes,
            "modele_llm": getattr(live.get_client_llm(), "nom", None),
            **_inventaire()}


@app.delete("/session/toolbox")
def fermer_session():
    live.SESSION.fermer()
    return {"active": False}


def _inventaire() -> dict:
    """Compteurs affichés dans l'en-tête. Un seul parcours pour les deux.

    `list_codes` parcourt déjà toute la collection : en appeler deux versions
    ferait deux parcours à chaque rafraîchissement de l'interface.
    """
    c = rag.client(DB)
    if not c.collection_exists(rag.COLLECTION):
        return {"codes_indexes": 0, "articles_indexes": 0, "chunks_indexes": 0}
    codes, articles, chunks, offset = set(), set(), 0, None
    while True:
        pts, offset = c.scroll(rag.COLLECTION, limit=512, offset=offset, with_payload=True)
        for p in pts:
            chunks += 1
            articles.add(p.payload.get("article_id"))
            codes.update(p.payload.get("fault_codes") or [])
        if offset is None:
            break
    return {"codes_indexes": len(codes), "articles_indexes": len(articles),
            "chunks_indexes": chunks}


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------
class Question(BaseModel):
    question: str
    k: int = 6
    conversation_id: str | None = None
    # Conservé pour un appel direct à l'API sans conversation enregistrée.
    historique: list[dict] = []


@app.post("/chat")
def chat(q: Question):
    if not q.question.strip():
        raise HTTPException(400, "Question vide.")
    # L'historique vient de la conversation enregistrée dès qu'on en a une :
    # le frontend n'a plus à le renvoyer, et il survit à un rechargement.
    historique = (conversations.historique_llm(q.conversation_id)
                  if q.conversation_id else q.historique)
    try:
        r = live.repondre(q.question, q.k, BACKEND, MODEL, DB, historique)
    except ErreurToolbox as e:
        # Jeton expiré en cours de conversation : on ferme la session pour que
        # le frontend redemande un jeton frais au lieu de réessayer en boucle.
        if e.status_code in (401, 403):
            live.SESSION.fermer()
            raise HTTPException(401, str(e))
        raise HTTPException(502, str(e))

    try:
        c = conversations.ajouter_echange(q.conversation_id, q.question, r)
    except conversations.HistoriqueIllisible as e:
        # La réponse est bonne, seul l'enregistrement a échoué : on la rend
        # quand même, avec l'avertissement, plutôt que de la perdre.
        return {**r, "conversation_id": None, "conversation_titre": None,
                "avertissement_historique": str(e)}
    return {**r, "conversation_id": c["id"], "conversation_titre": c["titre"]}


# --------------------------------------------------------------------------
# Conversations
# --------------------------------------------------------------------------
@app.get("/conversations")
def lister_conversations():
    convs, avertissement = conversations.lister()
    return {"conversations": convs, "avertissement": avertissement}


@app.post("/conversations")
def creer_conversation():
    return conversations.creer()


@app.get("/conversations/{cid}")
def obtenir_conversation(cid: str):
    c = conversations.obtenir(cid)
    if c is None:
        raise HTTPException(404, "conversation inconnue")
    return c


@app.delete("/conversations/{cid}")
def supprimer_conversation(cid: str):
    if not conversations.supprimer(cid):
        raise HTTPException(404, "conversation inconnue")
    return {"supprimee": cid}


# --------------------------------------------------------------------------
# Recherche brute (inchangé)
# --------------------------------------------------------------------------
@app.get("/search")
def search(q: str, k: int = 5, strict: bool = False):
    return rag.search(q, k, BACKEND, MODEL, DB, strict=strict)


@app.get("/codes")
def codes():
    t = rag.list_codes(DB)
    return {"count": len(t), "codes": t}


@app.get("/article/{article_id}")
def article(article_id: int):
    """Renvoie tous les chunks d'un article, remis dans l'ordre."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    c = rag.client(DB)
    if not c.collection_exists(rag.COLLECTION):
        raise HTTPException(404, "Aucun index : pose d'abord une question dans le chat.")
    pts, _ = c.scroll(
        rag.COLLECTION,
        scroll_filter=Filter(must=[FieldCondition(
            key="article_id", match=MatchValue(value=article_id))]),
        limit=500, with_payload=True,
    )
    if not pts:
        raise HTTPException(404, f"article {article_id} absent de l'index")
    chunks = sorted(pts, key=lambda p: int(str(p.payload["chunk_id"]).split("#")[-1]))
    return {
        "article_id": article_id,
        "title": chunks[0].payload["title"],
        "url": chunks[0].payload["url"],
        "fault_codes": chunks[0].payload["fault_codes"],
        "text": "\n\n".join(c.payload["text"] for c in chunks),
    }


class Ask(BaseModel):
    q: str
    k: int = 6


@app.post("/answer")
def answer(a: Ask):
    """Contexte formaté + prompt système, sans génération.

    Conservé pour brancher un autre modèle que celui configuré : /chat, lui,
    génère la réponse via la couche llm.py.
    """
    r = rag.search(a.q, a.k, BACKEND, MODEL, DB)
    systeme, utilisateur, sources = live.construire_prompt(a.q, r["results"])
    return {
        "system": systeme,
        "user": utilisateur,
        "sources": sources,
        "codes_detected": r["codes_detected"],
        "fell_back_to_semantic": r["fell_back_to_semantic"],
    }


EXTENSION = Path(__file__).parent / "extension"


@app.get("/extension.zip")
def extension_zip():
    """L'extension empaquetée, pour éviter d'aller chercher le dossier à la main."""
    import io
    import zipfile

    if not EXTENSION.is_dir():
        raise HTTPException(404, "dossier extension/ absent")
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(EXTENSION.iterdir()):
            if f.is_file():
                z.write(f, f.name)
    return Response(
        tampon.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="extension-toolbox.zip"'},
    )


@app.get("/extension/chemin")
def extension_chemin():
    """Chemin du dossier, à coller dans « Charger l'extension non empaquetée ».

    Sous WSL, on donne AUSSI le chemin UNC : le navigateur est sous Windows et
    son sélecteur de fichiers ne sait pas ouvrir un chemin POSIX. Sans ça,
    l'utilisateur voit un chemin qu'il ne peut pas utiliser.
    """
    distro = os.environ.get("WSL_DISTRO_NAME")
    unc = None
    if distro:
        unc = "\\\\wsl.localhost\\" + distro + str(EXTENSION).replace("/", "\\")
    return {"chemin": str(EXTENSION), "chemin_windows": unc,
            "present": EXTENSION.is_dir()}


# --------------------------------------------------------------------------
# Interface. Montée en dernier : une route d'API doit primer sur un fichier.
# --------------------------------------------------------------------------
@app.get("/")
def accueil():
    return FileResponse(STATIC / "index.html")


class StatiquesSansCache(StaticFiles):
    """Sert les fichiers d'interface sans les laisser mettre en cache.

    Sur un outil local qu'on modifie en continu, un CSS gardé en cache donne
    une page à moitié à jour et fait chercher un bug qui n'existe pas : c'est
    arrivé, le logo restait affiché à l'ancienne taille alors que la feuille
    de style était bonne sur le disque.
    """

    def is_not_modified(self, *a, **k) -> bool:
        return False

    async def get_response(self, path, scope):
        reponse = await super().get_response(path, scope)
        reponse.headers["Cache-Control"] = "no-store, must-revalidate"
        return reponse


app.mount("/static", StatiquesSansCache(directory=STATIC), name="static")
