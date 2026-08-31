"""
Backends d'embedding interchangeables.

- local   : FastEmbed / ONNX, multilingue (requêtes FR sur docs EN). Rien ne sort
            de ta machine — recommandé vu que le corpus est sous copyright Tesla.
- openai  : text-embedding-3-large. Plus simple, mais tu envoies le corpus chez OpenAI.
- hash    : embedder déterministe hors-ligne, UNIQUEMENT pour les tests du pipeline.

Les modèles E5 exigent des préfixes "query:" / "passage:" — c'est géré ici,
et l'oublier fait perdre ~10 points de rappel.
"""

from __future__ import annotations

import hashlib
import os
import re


class Embedder:
    name: str = "base"
    dim: int = 0

    def embed(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        raise NotImplementedError


# --------------------------------------------------------------------------
class LocalEmbedder(Embedder):
    """FastEmbed (ONNX, pas de torch). Premier appel = téléchargement du modèle."""

    # multilingual-e5-large : 1024 dims, gère les requêtes en français
    # sur un corpus technique anglais. Alternative légère et anglais-only :
    # "BAAI/bge-small-en-v1.5" (384 dims, ~10x plus rapide).
    def __init__(self, model: str = "intfloat/multilingual-e5-large"):
        from fastembed import TextEmbedding

        self.name = model
        self._e5 = "e5" in model.lower()
        self._m = TextEmbedding(model)
        self.dim = len(next(iter(self._m.embed(["dim probe"]))))

    def embed(self, texts, is_query=False):
        if self._e5:
            prefix = "query: " if is_query else "passage: "
            texts = [prefix + t for t in texts]
        return [v.tolist() for v in self._m.embed(texts)]


# --------------------------------------------------------------------------
class OpenAIEmbedder(Embedder):
    def __init__(self, model: str = "text-embedding-3-large"):
        from openai import OpenAI

        self.name = model
        self.dim = 3072 if "large" in model else 1536
        self._c = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        self._model = model

    def embed(self, texts, is_query=False):
        out = []
        # L'API plafonne autour de 300k tokens par requête : on envoie par lots.
        for i in range(0, len(texts), 128):
            batch = [t.replace("\n", " ")[:8000] for t in texts[i:i + 128]]
            r = self._c.embeddings.create(model=self._model, input=batch)
            out.extend(d.embedding for d in r.data)
        return out


# --------------------------------------------------------------------------
class HashEmbedder(Embedder):
    """Sac-de-mots haché, L2-normalisé. Déterministe, zéro dépendance.

    Sert à valider le pipeline (indexation, filtres, pagination) sans réseau.
    Ne JAMAIS s'en servir en production : aucune sémantique, uniquement du
    recouvrement lexical.
    """

    _TOK = re.compile(r"[a-z0-9_]+")

    def __init__(self, dim: int = 256):
        self.name = "hash-debug"
        self.dim = dim

    def embed(self, texts, is_query=False):
        vecs = []
        for t in texts:
            v = [0.0] * self.dim
            for tok in self._TOK.findall(t.lower()):
                h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:4], "big")
                v[h % self.dim] += 1.0
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            vecs.append([x / norm for x in v])
        return vecs


# --------------------------------------------------------------------------
def get_embedder(backend: str = "local", model: str | None = None) -> Embedder:
    if backend == "local":
        return LocalEmbedder(model) if model else LocalEmbedder()
    if backend == "openai":
        return OpenAIEmbedder(model) if model else OpenAIEmbedder()
    if backend == "hash":
        return HashEmbedder()
    raise ValueError(f"backend inconnu : {backend}")
