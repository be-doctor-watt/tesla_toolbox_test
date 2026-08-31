#!/usr/bin/env python3
"""Extension navigateur : manifeste, scripts, empaquetage, cloisonnement.

    python tests/test_extension.py

L'extension est la SEULE voie de connexion. Ce qui est vérifiable sans
navigateur réel : validité du manifeste et des scripts, permissions,
empaquetage, et le fait qu'aucune page web ne puisse se substituer à elle
pour poster un jeton (l'extension est dispensée de CORS par ses
host_permissions, donc le serveur n'a aucune origine à autoriser).

Le comportement dans Chrome, lui, ne se teste qu'à la main.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))

TMP = Path(tempfile.mkdtemp(prefix="ext_test_"))
os.environ.update({
    "RAG_BACKEND": "hash",
    "RAG_DB": str(TMP / "qdrant"),
    "TBX_RAW": str(TMP / "raw"),
    "TBX_SESSION": str(TMP / "session.json"),
    "TBX_CONVERSATIONS": str(TMP / "conversations.json"),
    "MISTRAL_API_KEY": "",
})
for _v in ("RAG_DB", "TBX_SESSION", "TBX_CONVERSATIONS"):
    assert str(TMP) in os.environ[_v], f"{_v} pointe hors du dossier de test !"

from fastapi.testclient import TestClient   # noqa: E402

ok = fail = 0
EXT = RACINE / "extension"


def verifier(cond, libelle, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ok   {libelle}")
    else:
        fail += 1
        print(f"  FAIL {libelle}  {detail}")


print("=== Manifeste ===")
m = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
verifier(m["manifest_version"] == 3, f"manifest_version = {m['manifest_version']}")
verifier("cookies" in m["permissions"],
         "permission `cookies` (indispensable : elle donne accès même aux "
         "cookies HttpOnly, contrairement au JS de page)")
verifier(any("toolbox.tesla.com" in h for h in m["host_permissions"]),
         "host_permission sur toolbox.tesla.com")
verifier(any("127.0.0.1" in h for h in m["host_permissions"]),
         "host_permission sur 127.0.0.1 (dispense de CORS)")
for f in ("arriere-plan.js", "fenetre.html", "fenetre.js", "icone.png"):
    verifier((EXT / f).is_file(), f"{f} présent")
verifier(m["background"]["service_worker"] == "arriere-plan.js",
         "service worker déclaré")

print("\n=== Scripts syntaxiquement valides ===")
for f in ("arriere-plan.js", "fenetre.js"):
    r = subprocess.run(["node", "--check", str(EXT / f)], capture_output=True)
    verifier(r.returncode == 0, f"node --check {f}", r.stderr.decode()[:160])

print("\n=== Le script lit bien la ligne de cookie complète ===")
src = (EXT / "arriere-plan.js").read_text(encoding="utf-8")
verifier("chrome.cookies.getAll" in src, "getAll (et non get) : toute la ligne")
verifier('"tbx_token"' in src or "'tbx_token'" in src, "cookie tbx_token ciblé")
verifier("collage" in src, "poste bien le champ `collage` attendu par l'API")
verifier("silencieux" in src,
         "envoi silencieux sur onUpdated : pas d'erreur quand l'outil est éteint")

import serve   # noqa: E402

with TestClient(serve.app) as c:
    print("\n=== Empaquetage ===")
    r = c.get("/extension.zip")
    verifier(r.status_code == 200, f"GET /extension.zip -> {r.status_code}")
    verifier("attachment" in r.headers.get("content-disposition", ""),
             "en-tête de téléchargement")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    noms = set(z.namelist())
    verifier("manifest.json" in noms, f"contenu : {sorted(noms)}")
    verifier(z.testzip() is None, "archive intègre")
    verifier(json.loads(z.read("manifest.json")), "manifeste lisible dans l'archive")

    r = c.get("/extension/chemin")
    verifier(r.status_code == 200 and r.json()["present"] is True,
             f"GET /extension/chemin -> {r.json() if r.status_code == 200 else r.status_code}")

    print("\n=== Aucune origine web n'est autorisée à poster ici ===")
    # L'extension n'a PAS besoin de CORS : ses host_permissions l'en dispensent.
    # Le favori, qui en avait besoin, a été retiré — donc plus aucune page web
    # ne doit pouvoir poster le jeton, y compris toolbox.tesla.com.
    for origine in ("https://toolbox.tesla.com", "https://exemple-malveillant.test"):
        r = c.options("/session/toolbox", headers={
            "Origin": origine,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        })
        verifier(r.headers.get("access-control-allow-origin") is None,
                 f"{origine} : pas d'autorisation CORS",
                 str(r.headers.get("access-control-allow-origin")))
        r = c.options("/session/toolbox", headers={
            "Origin": origine,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Private-Network": "true",
        })
        verifier(r.headers.get("access-control-allow-private-network") is None,
                 f"{origine} : pas d'accès réseau privé accordé")

    print("\n=== Le format posté par l'extension est bien celui de l'API ===")
    r = c.post("/session/toolbox", json={"collage": "device_hash=x; tbx_token=eyJfaux; _abck=1"})
    verifier(r.status_code in (200, 401, 502),
             f"format accepté par l'API (réseau indisponible ici) : {r.status_code}",
             r.text[:120])
    verifier("collage" in str(serve.Jeton.model_fields),
             "le modèle expose bien `collage`")

print(f"\n{ok} ok / {fail} fail")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fail else 0)
