#!/usr/bin/env python3
"""Faux Toolbox : imite les deux endpoints confirmés, en local.

Sert à tester le mode live de bout en bout sans jeton Tesla et sans taper
l'API réelle. Reproduit la forme des réponses observée dans le DevTools :

    GET /api/toolbox/articles/search?q=&page=&locale=   -> {"results": [...]}
    GET /api/v2/articles/{id}?expand=&locale=           -> l'article + expand

Le comportement se pilote par attributs de classe, pour rejouer les pannes
qui comptent : jeton refusé (401), throttling (429), clé de tableau inconnue.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

ARTICLES = {
    6050000: {
        "id": 6050000,
        "title": "BMS_a066_SOC_Imbalance_Warning",
        "body": "<p>BMS_a066 is set when the battery management system detects a "
                "state of charge imbalance between bricks exceeding the calibration "
                "threshold.</p><p>Verify brick voltage deltas using the diagnostic tool. "
                "If the delta exceeds 0.15 V after a full balancing cycle, the affected "
                "module must be replaced.</p>",
        "causes_virtual": [
            {"name": "Module capacity mismatch", "description": "after a module replacement"},
            {"name": "Sense harness fault"},
        ],
        "effects_virtual": [{"name": "Charge rate limited"}],
        "qualifiers": [{"name": "Delta > 0.15 V after full balancing"}],
        "system": {"name": "High Voltage Battery"},
        "status": {"name": "Published"},
    },
    6050001: {
        "id": 6050001,
        "title": "BMS_a067_Brick_Overvoltage",
        "body": "BMS_a067 indicates brick overvoltage during charging. This alert "
                "latches immediately and inhibits charging. Inspect the sense harness "
                "for chafing at the module interface before condemning the pack.",
        "causes_virtual": [{"name": "Sense harness chafe at module interface"}],
        "effects_virtual": [{"name": "Charging inhibited"}],
        "system": {"name": "High Voltage Battery"},
    },
    6050003: {
        "id": 6050003,
        "title": "DI_a175_Drive_Inverter_Overtemp",
        "body": "DI_a175 is logged when the drive inverter exceeds thermal limits. "
                "Check coolant level and verify the pump is commanded on. Power is "
                "derated progressively.",
        "causes_virtual": [{"name": "Low coolant"}, {"name": "Pump not commanded"}],
        "system": {"name": "Drive Inverter"},
    },
}


class Handler(BaseHTTPRequestHandler):
    # Pilotage du scénario de test.
    statut_force: int | None = None      # 401, 429... appliqué à toute requête
    cle_resultats: str = "hits"          # clé interne de l'enveloppe ES ;
    #                                  la changer simule une API modifiée
    appels: list[str] = []

    def _json(self, code: int, corps: dict):
        data = json.dumps(corps).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        Handler.appels.append(self.path)

        if Handler.statut_force:
            self._json(Handler.statut_force, {"error": "refuse par le test"})
            return

        u = urlparse(self.path)
        params = parse_qs(u.query)

        # Le vrai Toolbox exige le JWT et les cookies : on vérifie que le
        # client les envoie, sinon le test passerait sans les transmettre.
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            self._json(401, {"error": "Authorization manquant"})
            return
        if "_abck" not in (self.headers.get("Cookie") or ""):
            self._json(403, {"error": "cookie Akamai manquant"})
            return

        if u.path == "/api/toolbox/articles/search":
            q = (params.get("q") or [""])[0]
            page = int((params.get("page") or ["0"])[0])
            # Une seule page de résultats : page 1 renvoie du vide, ce qui est
            # la condition d'arrêt de search_ids().
            # CONFIRMÉ sur l'API réelle : le moteur exige que TOUS les termes
            # correspondent (« coolant » 643 résultats, « coolant bleed » 9,
            # « coolant fill bleed » 0). Une question en langage naturel
            # renvoie donc presque toujours 0, et le test doit le reproduire,
            # sinon il valide un comportement que la vraie API n'a pas.
            termes = [t for t in q.lower().split() if t]

            def correspond(a):
                texte = (a["title"] + " " + a.get("body", "")).lower()
                return all(t in texte for t in termes)

            trouves = [] if page > 0 else [
                # Forme CONFIRMÉE : document Elasticsearch, id sous
                # _source.article.id.
                {"_id": str(aid), "_index": "articles", "_score": 40.0 - i,
                 "_source": {"article": {"id": aid, "title": a["title"]},
                             "category_ids": [291]}}
                for i, (aid, a) in enumerate(ARTICLES.items())
                if not termes or correspond(a)
            ]
            # Enveloppe ES : `hits` est un DICT, les documents sont dessous.
            self._json(200, {
                "_shards": {"failed": 0, "successful": 1, "total": 1},
                "errors": False, "timed_out": False, "took": 4,
                "aggregations": {},
                "hits": {"max_score": 40.0,
                         "total": {"relation": "eq", "value": len(trouves)},
                         Handler.cle_resultats: trouves},
            })
            return

        m = re.fullmatch(r"/api/v2/articles/(\d+)", u.path)
        if m:
            aid = int(m.group(1))
            if aid not in ARTICLES:
                self._json(404, {"error": "inconnu"})
                return
            # L'API v2 emballe parfois la ressource : on exerce ce cas.
            self._json(200, {"data": ARTICLES[aid]})
            return

        self._json(404, {"error": "route inconnue"})

    def log_message(self, *a):
        pass    # silence : le test a sa propre sortie


def demarrer(port: int = 0) -> tuple[HTTPServer, str]:
    """Lance le faux Toolbox dans un thread. -> (serveur, base_url)."""
    srv = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    srv, base = demarrer(8899)
    print(f"Faux Toolbox sur {base}  (Ctrl-C pour arrêter)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()


# --------------------------------------------------------------------------
# Formes CONFIRMÉES sur l'API réelle (relevées le 2026-08-28). Servent aux
# tests de non-régression : chacune correspond à un défaut réel corrigé.
REPONSE_SEARCH_REELLE = {
    "_shards": {"failed": 0, "successful": 1, "total": 1},
    "aggregations": {"by_category": {"buckets": [{"doc_count": 9, "key": "BMS"}]}},
    "errors": False,
    "timed_out": False,
    "took": 5,
    # La recherche est un Elasticsearch : `hits` est un DICT, et les documents
    # sont sous `hits.hits`. Un `isinstance(v, list)` sur `hits` échoue donc.
    "hits": {
        "max_score": 44.03,
        "total": {"relation": "eq", "value": 2},
        "hits": [
            {"_id": "6050000", "_index": "articles", "_score": 44.03,
             # L'id utile est sous _source.article.id, pas _source.id.
             "_source": {"article": {"id": 6050000,
                                     "title": "BMS_a066_SOC_Imbalance_Warning",
                                     "summary": "Early warning for a064."},
                         "category_ids": [291, 293], "marked_as_solved": 0}},
            {"_id": "6050001", "_index": "articles", "_score": 30.1,
             "_source": {"article": {"id": 6050001,
                                     "title": "BMS_a067_Brick_Overvoltage"}}},
        ],
    },
}

# Un article d'alerte réel n'a NI `body` NI `content`. Le texte est réparti sur
# description/summary/steps_to_*/firmware_details, et firmware_details (table de
# signaux en HTML) porte l'essentiel. Le « < » de la Clear Condition n'est PAS
# échappé dans le HTML de Tesla.
ARTICLE_REEL = {
    "id": 6050000,
    "title": "BMS_a066_SOC_Imbalance_Warning",
    "summary": "This alert is an early warning for a064 SOC Imbalance.",
    "description": "This alert is an early warning for a064 SOC Imbalance.",
    "steps_to_fix": None,
    "steps_to_test": None,
    "firmware_details": (
        '<table><tbody>'
        '<tr><td>Set Condition</td><td>imbalance &gt; SOC_IMBALANCE_WARNING_SET_THRESHOLD</td></tr>'
        '<tr><td>Clear Condition</td><td>The estimated imbalance between average SOC '
        'and min SOC < (SOC_IMBALANCE_WARNING_SET_THRESHOLD - SOC_IMBALANCE_CLEAR_HYSTERESIS)</td></tr>'
        '<tr><td>Latching Alert</td><td>False</td></tr>'
        '</tbody></table>'),
    "causes_virtual": [{"status": "approved", "title": "SOC imbalance corrected itself over time"}],
    "effects_virtual": [{"status": "approved", "title": "BMS_a174_SW_Charge_Failure"}],
    # Make/Model = applicabilité véhicule, PAS des conditions de déclenchement.
    "qualifiers": [
        {"class_type": "MakeQualifier", "value": "Vehicle", "name": None},
        {"class_type": "ModelQualifier", "value": "Model 3", "name": None},
        {"class_type": "ModelQualifier", "value": "Palladium S", "name": None},
    ],
    "system": {"name": "High Voltage", "id": 12},
    "status": {"name": "approved", "code": "approved"},
    "categories": [{"name": "BMS"}, {"name": "Alert"}],
}
