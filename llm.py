"""Couche d'abstraction du modèle de langage.

Reprise de la couche ValiZia (doctor-watt-2.0/backend/app/core/llm.py) : le
reste du code ne connaît jamais le fournisseur, et le choix reste réversible.
Implémentation par défaut : Mistral Large via API européenne.

Configuration par variables d'environnement, comme le reste de ce dépôt :

    MISTRAL_API_KEY       sans elle, on tombe sur le stub (aucune génération)
    MISTRAL_ENDPOINT      défaut https://api.mistral.ai/v1/chat/completions
    MISTRAL_MODELE        défaut mistral-medium-latest
    MISTRAL_MODELE_REPLI  défaut mistral-small-latest, essayé si le principal
                          ne répond pas
    LLM_TIMEOUT_S         défaut 45

`mistral-medium-latest` est le défaut et non `mistral-large-latest` (celui de
ValiZia) parce que `large` part en read timeout systématique sur un compte du
tier gratuit, mesuré, alors que `medium` répond en 0,3 s. Mistral recommande
d'ailleurs Medium par défaut. Repasser à `large` est une variable
d'environnement.

Timeout, retry et journalisation des tokens sont gérés ici, jamais dans les
modules appelants.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Protocol

import httpx

logger = logging.getLogger("toolbox_rag.llm")

Message = dict[str, str]


class ErreurLLM(Exception):
    """Échec d'appel au modèle après épuisement des tentatives."""


class ClientLLM(Protocol):
    def complete(self, messages: list[Message]) -> str:
        """Réponse du modèle à partir d'une liste de messages {role, content}."""
        ...


class ClientLLMStub:
    """Aucun appel réseau. Utilisé quand MISTRAL_API_KEY est absente.

    Renvoie un message explicite plutôt qu'une réponse inventée : sur de la
    doc haute tension, un silence honnête vaut mieux qu'une procédure
    plausible mais fausse.
    """

    nom = "stub"

    def complete(self, messages: list[Message]) -> str:
        # Surtout ne rien affirmer sur la pertinence des extraits : le stub ne
        # les a pas lus, et un code peut être absent du corpus. C'est
        # l'avertissement calculé en amont qui dit ce qu'ils valent.
        return (
            "Le modèle de langage n'est pas configuré (MISTRAL_API_KEY absente), "
            "je ne peux donc pas rédiger de synthèse. Les extraits trouvés sont "
            "listés ci-dessous, à lire en tenant compte des avertissements."
        )


class ClientLLMMistral:
    """Mistral via API européenne (format chat/completions).

    Deux modèles : le principal, et un repli essayé si le principal ne répond
    pas. Ce n'est pas de la précaution abstraite, c'est mesuré :
    `mistral-large-latest` part en read timeout systématique sur un compte du
    tier gratuit alors que `mistral-medium-latest` répond en 0,3 s. Sans repli,
    l'atelier n'a pas de réponse du tout.

    `dernier_modele` dit lequel a effectivement répondu, pour que l'interface
    puisse l'afficher : un technicien doit savoir si sa réponse vient du modèle
    prévu ou du repli.
    """

    def __init__(self, api_key: str, endpoint: str, modele: str,
                 modele_repli: str | None = None,
                 timeout_s: float = 45.0, tentatives_max: int = 2):
        self.nom = modele
        self.dernier_modele: str | None = None
        self._api_key = api_key
        self._endpoint = endpoint
        self._modele = modele
        self._repli = modele_repli if modele_repli != modele else None
        self._tentatives_max = tentatives_max
        self._http = httpx.Client(timeout=timeout_s)

    def _appeler(self, modele: str, messages: list[Message]) -> str:
        """Un modèle, avec ses tentatives. Lève la dernière erreur rencontrée."""
        derniere: Exception | None = None
        for tentative in range(1, self._tentatives_max + 1):
            try:
                r = self._http.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": modele,
                        "messages": messages,
                        # Doc d'atelier : on veut la citation fidèle, pas de style.
                        "temperature": 0.1,
                    },
                )
                r.raise_for_status()
                corps = r.json()
                usage = corps.get("usage", {})
                logger.info("LLM %s : %s tokens entrée, %s sortie", modele,
                            usage.get("prompt_tokens", "?"),
                            usage.get("completion_tokens", "?"))
                self.dernier_modele = modele
                return corps["choices"][0]["message"]["content"]
            except httpx.ReadTimeout as exc:
                # Le serveur a accepté la requête et ne répond pas. Réessayer
                # le MÊME modèle ne fait qu'ajouter un timeout : on rend la
                # main tout de suite pour laisser sa chance au repli.
                logger.warning("LLM %s : pas de réponse avant expiration du délai", modele)
                raise exc
            except httpx.HTTPStatusError as exc:
                derniere = exc
                code = exc.response.status_code
                # Une erreur client (hors 429) ne se répare pas en réessayant.
                if code < 500 and code != 429:
                    break
            except httpx.TransportError as exc:
                derniere = exc
            if tentative < self._tentatives_max:
                time.sleep(min(0.5 * 2 ** tentative, 4.0))
        raise derniere if derniere else ErreurLLM(f"{modele} : échec inexpliqué")

    def complete(self, messages: list[Message]) -> str:
        derniere: Exception | None = None
        for modele in filter(None, (self._modele, self._repli)):
            try:
                return self._appeler(modele, messages)
            except Exception as exc:
                derniere = exc
                if self._repli and modele == self._modele:
                    logger.warning("Bascule sur le modèle de repli %s", self._repli)
        logger.error("Échec de l'appel au modèle : %s", derniere)
        raise ErreurLLM("Le modèle de langage est injoignable ou en erreur.") from derniere


_CLIENT: ClientLLM | None = None


def get_client_llm() -> ClientLLM:
    """Client configuré, mis en cache. Repli sur le stub sans clé API."""
    global _CLIENT
    if _CLIENT is None:
        cle = os.environ.get("MISTRAL_API_KEY")
        if cle:
            _CLIENT = ClientLLMMistral(
                api_key=cle,
                endpoint=os.environ.get(
                    "MISTRAL_ENDPOINT", "https://api.mistral.ai/v1/chat/completions"),
                modele=os.environ.get("MISTRAL_MODELE", "mistral-medium-latest"),
                modele_repli=os.environ.get("MISTRAL_MODELE_REPLI", "mistral-small-latest"),
                timeout_s=float(os.environ.get("LLM_TIMEOUT_S", "45")),
            )
        else:
            _CLIENT = ClientLLMStub()
    return _CLIENT
