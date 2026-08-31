# Toolbox RAG — Doctor-Watt

Recherche documentaire sur le corpus Tesla Toolbox, avec filtrage par code
défaut, et une interface de chat aux couleurs Doctor-Watt.

```
harvest.py     aspiration Toolbox -> corpus.jsonl        (tourne SUR TA MACHINE)
faultcodes.py  détection/normalisation des codes défaut  (partagé)
embedders.py   backends d'embedding interchangeables
rag.py         indexation Qdrant + recherche
live.py        mode live : le corpus se construit à l'usage
conversations.py  liste de conversations persistée
extension/     extension Chrome qui transmet le jeton Toolbox
llm.py         couche d'abstraction du modèle (Mistral, API européenne)
config.py      chargement du .env, sans dépendance
serve.py       API HTTP + interface
static/        interface (charte reprise de doctor-watt-2.0)
```

## Installation

```bash
pip install -r requirements.txt
```

## Démarrage rapide : le chat

C'est le chemin recommandé. Aucune aspiration préalable n'est nécessaire :
l'index se remplit tout seul, question par question.

```bash
cp .env.example .env                # puis renseigner MISTRAL_API_KEY
uvicorn serve:app --host 127.0.0.1 --port 8000
```

Le `.env` est ignoré par git (`config.py` le charge, sans dépendance). Une
variable exportée dans le shell prime toujours sur le fichier. Sans
`MISTRAL_API_KEY`, l'assistant renvoie les extraits sourcés sans synthèse
rédigée : l'outil reste utilisable.

Le modèle par défaut est `mistral-medium-latest`, pas `mistral-large-latest`
comme dans ValiZia : sur un compte du tier gratuit, `large` part en read
timeout systématique (mesuré, plusieurs essais) alors que `medium` répond en
0,3 s. Si le modèle principal ne répond pas, `llm.py` bascule une fois sur
`MISTRAL_MODELE_REPLI` et l'interface signale que la synthèse vient du repli.
Quand aucun modèle ne répond, le chat renvoie quand même les extraits sourcés.

Puis ouvrir <http://127.0.0.1:8000>.

### Connexion Toolbox

Un bouton **« Se connecter à Toolbox »** ouvre `toolbox.tesla.com` dans un
nouvel onglet **de ton navigateur**, celui où tu es déjà authentifié. Une
extension Chrome y lit le jeton et le poste à l'outil ; la page le détecte et
bascule sur le chat. Rien à saisir, rien à réauthentifier.

Installation, une seule fois : `extension-toolbox.zip` en téléchargement depuis
l'écran de connexion (ou le dossier `extension/`), puis `chrome://extensions`
→ mode développeur → « Charger l'extension non empaquetée ». L'écran affiche le
chemin **Windows** du dossier sous WSL, puisque le navigateur est côté Windows.

Ensuite l'extension envoie automatiquement à chaque chargement d'un onglet
Toolbox, y compris après l'expiration des 24 h : le renouvellement ne demande
aucune action.

Pourquoi une extension et pas une redirection : Tesla n'expose aucun client
OAuth pour Toolbox, et une page servie depuis `127.0.0.1` ne peut pas lire un
cookie de `toolbox.tesla.com` (règle d'origine unique). Une extension, elle, a
le droit de lire les cookies d'un domaine autorisé, **y compris les cookies
`HttpOnly`** invisibles au JavaScript de page.

Un seul secret est en jeu : **le jeton de l'en-tête `Authorization` EST la
valeur du cookie `tbx_token`**, vérifié sur l'API réelle, et ce cookie seul
suffit à s'authentifier (testé sans `_abck` ni `bm_sz`). L'extension transmet
néanmoins toute la ligne de cookie, au cas où Akamai exige `_abck` dans
d'autres conditions.

Le serveur n'autorise **aucune origine web** à poster un jeton : l'extension
est dispensée de CORS par ses `host_permissions`, donc rien n'a à être ouvert.

Si l'extension n'est pas encore installée, ou en cas de panne, l'API reste
joignable en ligne de commande :

```bash
curl -X POST http://127.0.0.1:8000/session/toolbox \
  -H 'Content-Type: application/json' \
  -d '{"collage": "<la ligne Cookie copiée depuis le DevTools>"}'
```

Le champ `collage` accepte indifféremment toute la ligne `Cookie`, l'en-tête
`Authorization` avec ou sans `Bearer`, le JWT nu, ou le seul cookie
`tbx_token`.

**Au premier lancement**, FastEmbed télécharge ~2 Go de modèle ONNX. C'est
visible dans la console (`Chargement de l'embedder...`) et ça peut prendre
plusieurs minutes. Pour un essai rapide, un modèle léger suffit, mais il est
anglais seulement, donc les questions en français perdent en rappel :

```bash
RAG_MODEL=BAAI/bge-small-en-v1.5 uvicorn serve:app --port 8000
```

### Comment ça marche

Quand la question contient un code défaut absent de l'index, le serveur
interroge Toolbox avec ton jeton, met l'article en cache dans `raw/` **et**
l'indexe. La question suivante sur le même code est instantanée et hors-ligne.
Le détail de ce qui a été récupéré est dépliable sous chaque réponse.

### Session et conversations

La session Toolbox **reste ouverte d'un redémarrage à l'autre** : le jeton est
enregistré dans `.session.json` (permissions 0600, ignoré par git), et rechargé
au démarrage du serveur. Il n'est jamais renvoyé au navigateur ni journalisé.
L'échéance est lue dans le JWT et affichée dans la barre latérale ; un jeton
déjà périmé n'est pas rechargé. « Fermer la session » efface le fichier.

C'est un compromis assumé : sans persistance, il faut retourner chercher un
jeton dans le DevTools à chaque relance d'uvicorn. Pour revenir au
comportement « rien sur le disque », il suffit de supprimer l'appel à
`_sauver()` dans `live.py`.

Les conversations sont listées à gauche, la plus récemment utilisée en tête.
Elles sont stockées côté serveur dans `conversations.json` (ignoré par git, il
contient des extraits Tesla), donc elles survivent à un vidage du cache du
navigateur. Chaque tour est réenregistré avec ses sources, ses extraits et son
journal, ce qui permet de recharger une conversation à l'identique. Le titre
est dérivé de la première question, codes défaut en tête.

Si le jeton expire en cours de conversation, le chat te renvoie à l'écran de
connexion et l'index déjà constitué reste interrogeable.

Si un code de la question n'est documenté nulle part, la réponse le dit
explicitement au lieu de servir l'article le plus proche. C'est le point qui
compte sur de la doc haute tension, voir plus bas.

Variables d'environnement :

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `MISTRAL_API_KEY` | — | sans elle, la couche LLM tombe sur un stub : extraits seuls |
| `MISTRAL_MODELE` | `mistral-medium-latest` | modèle de génération |
| `MISTRAL_MODELE_REPLI` | `mistral-small-latest` | essayé si le principal ne répond pas |
| `LLM_TIMEOUT_S` | `45` | délai par appel au modèle |
| `RAG_BACKEND` | `local` | `local`, `openai` ou `hash` |
| `RAG_MODEL` | `intfloat/multilingual-e5-large` | modèle d'embedding |
| `RAG_DB` | `./qdrant_data` | base Qdrant |
| `TBX_RAW` | `raw` | cache des articles bruts |
| `TBX_SESSION` | `.session.json` | jeton persisté (0600) |
| `TBX_CONVERSATIONS` | `conversations.json` | liste des conversations |

## 1. Aspiration (optionnel)

Le mode live suffit à l'usage courant. L'aspiration complète reste utile pour
travailler entièrement hors-ligne, ou pour un inventaire exhaustif des codes.

**À lancer sur ta machine**, dans le même réseau que le navigateur d'où viennent
les cookies. Depuis un serveur distant, Akamai détecte l'écart IP/session et
coupe ta session Toolbox.

Récupère `TBX_TOKEN` (le JWT de l'en-tête `Authorization`) et `TBX_COOKIE` (toute
la ligne `Cookie`) dans le DevTools, onglet Network.

```bash
export TBX_TOKEN="eyJhbGci..."
export TBX_COOKIE="device_hash=...; tbx_token=...; _abck=...; bm_sz=..."

python harvest.py discover "BMS_a066"   # valide recherche ET article, de bout en bout
python harvest.py crawl                 # aspire tout (reprise automatique)
python harvest.py build                 # -> corpus.jsonl
```

**Avant le `crawl`**, lance `discover` : il valide la recherche ET la
récupération d'un article, et s'arrête avec un message précis si une clé n'est
pas reconnue.

- **Recherche** (confirmé sur l'API réelle) :
  `GET /api/toolbox/articles/search?q=…&page=…&locale=…`, pagination à partir de 0.
  C'est un **Elasticsearch**, et il renvoie l'enveloppe ES :

  ```
  {"_shards": …, "aggregations": …, "errors": false, "took": 5,
   "hits": {"total": {"value": 15}, "max_score": 44.0,
            "hits": [{"_id": "6050000", "_score": 44.0,
                      "_source": {"article": {"id": 6050000, "title": "…"}}}]}}
  ```

  Deux pièges : la clé `hits` de premier niveau est un **dict**, pas une liste,
  et l'id d'article est sous **`_source.article.id`**, deux niveaux plus bas
  qu'on pourrait le croire. `_hits_avec_cle()` et `_ids_from()` gèrent les deux.
- **Article** (confirmé) : `GET /api/v2/articles/{id}?expand=…&locale=…` — noter
  le `/api/v2/`, pas `/api/toolbox/`.

  Un article d'alerte n'a **ni `body` ni `content`**. Le texte utile est réparti
  sur `description`, `summary`, `steps_to_test`, `steps_to_fix` et surtout
  **`firmware_details`**, une table de signaux en HTML qui peut peser 9 ko à elle
  seule, soit quinze fois le reste. Elle porte les `Set Condition` et
  `Clear Condition` du code défaut, c'est-à-dire ce qu'un technicien vient
  chercher. `flatten_article()` lit toutes ces clés.

  Les `qualifiers` ne sont pas des conditions de déclenchement : ce sont des
  `MakeQualifier` / `ModelQualifier`, donc l'applicabilité véhicule. Ils sont
  rendus sous « MODÈLES CONCERNÉS », séparément des vraies conditions.
- `_hits_avec_cle()` distingue « 0 résultat » de « structure non reconnue ».
  Confondre les deux fait accuser l'API quand la recherche n'a simplement rien
  trouvé, et bloque une connexion parfaitement valide.

**Le HTML de Tesla contient des `<` littéraux non échappés**, par exemple
`min SOC < (SOC_..._THRESHOLD - SOC_..._HYSTERESIS)`. Un `re.sub(r"<[^>]+>")`
avale alors depuis ce `<` jusqu'au `>` de la balise suivante, et supprime la
condition d'effacement du code défaut **sans lever d'erreur**. `strip_html()`
exige donc qu'une balise commence par une lettre ou `/` et ne contienne pas de
`<`. Test de non-régression dans `tests/test_api_reelle.py`.

L'`expand` de l'API v2 est le vrai gisement : `causes_virtual`,
`effects_virtual` et `qualifiers` portent causes, effets et conditions de
déclenchement sous forme structurée. `flatten_article()` les rend en sections
nommées (`CAUSES POSSIBLES`, `EFFETS SUR LE VÉHICULE`…) avant l'indexation :
sur une fiche d'alerte, c'est souvent plus dense que le corps rédigé.

Si `q=""` n'énumère pas le catalogue, `crawl` bascule automatiquement sur des
requêtes-graines (`SEED_QUERIES`) et fait l'union des résultats. Vérifie la
couverture ensuite avec `python rag.py codes`.

Le `crawl` est volontairement lent (1,2–2,5 s entre requêtes, séquentiel). Un
burst parallèle fait sauter la session Akamai et il faut retourner chercher des
cookies. Sur plusieurs milliers d'articles, prévois quelques heures en tâche de
fond, et un renouvellement de token en cours de route — le cache `raw/` fait
qu'une reprise ne retélécharge rien.

## 2. Indexation

```bash
python rag.py index corpus.jsonl --backend local
```

Backends : `local` (FastEmbed, multilingue, hors-ligne — défaut), `openai`
(`OPENAI_API_KEY` requis), `hash` (tests seulement, aucune sémantique).

Le corpus étant sous copyright Tesla, `local` est le choix par défaut : rien ne
sort de ta machine.

## 3. Recherche

```bash
python rag.py search "pourquoi BMS_a066 revient apres remplacement de module"
python rag.py codes            # inventaire code -> nb d'articles
```

```bash
uvicorn serve:app --host 127.0.0.1 --port 8000
# GET  /                     l'interface
# POST /chat    {"question": "..."}  -> réponse rédigée + sources + journal
# GET  /search?q=...&k=5     GET /codes     GET /article/6050000
# POST /answer {"q": "..."}  -> contexte + prompt prêts pour ton LLM
# POST/GET/DELETE /session/toolbox      -> le jeton Toolbox
```

`/chat` génère la réponse via `llm.py`. `/answer` est conservé pour brancher un
autre modèle : il renvoie le contexte formaté, le prompt système et les
sources, sans générer.

## Le point de conception qui compte

Quand la requête contient un code défaut, le pipeline **filtre sur la métadonnée
`fault_codes` avant** la recherche vectorielle.

Sans ce filtre, la similarité sémantique ne distingue pas `BMS_a066` de
`BMS_a067` : les deux chaînes sont quasi identiques pour un tokenizer, et tu
récupères le mauvais article avec un score de confiance élevé. Sur de la doc
d'atelier haute tension, c'est le mode de défaillance qui compte.

D'où aussi le choix d'indexer **tout le catalogue** puis de taguer par regex,
plutôt que de faire une recherche par code : un article couvre souvent plusieurs
codes, et le RAG répond ainsi également aux questions qui n'en contiennent aucun.

Si un code est absent du corpus, la recherche bascule en repli sémantique et le
signale (`fell_back_to_semantic`). `--strict` désactive ce repli.

## Questions sans code défaut

La base Toolbox ne contient pas que des fiches d'alerte : mesuré sur l'API
réelle, « procedure » renvoie **5987 articles**, répartis en `Issue` (5858),
`FAQ` (73), `Alert` (23), `Symptom` (11), `Action` (10) et `Topic` (7), sur des
catégories qui couvrent tout le véhicule (LV System, Connecteurs, HVAC, Câblage,
NVH, Sièges, Closures…). Les procédures vivent surtout dans les articles
`Issue`, sous `steps_to_test` et `steps_to_fix`.

Deux régimes de recherche, et c'est le point de conception :

| La question | Ce qu'on fait |
| --- | --- |
| porte un **code défaut** | filtrage sur `fault_codes`, le code sert de requête tel quel |
| est **libre** (procédure, symptôme, pièce) | réécriture en mots-clés anglais courts avant d'interroger Toolbox |

La réécriture n'est pas un raffinement, elle est nécessaire. **Le moteur de
recherche Toolbox est lexical, exige que TOUS les termes correspondent, et
n'indexe que de l'anglais.** Mesuré :

| Requête | Résultats |
| --- | --- |
| `coolant` | 643 |
| `coolant bleed` | 9 |
| `coolant fill bleed` | **0** |
| `coolant fill and bleed procedure` | **0** |
| `procedure de purge du circuit de refroidissement` | **0** |

Envoyer la question brute ne peut donc rien trouver. `live.mots_cles()` la fait
convertir par le modèle en une à trois requêtes de deux ou trois mots en
anglais, puis ingère jusqu'à `MAX_ARTICLES_LIBRES` articles. Une question donnée
n'interroge Toolbox qu'une fois (mémoire des requêtes déjà faites). Sans modèle
de langage, le repli heuristique retire les mots vides sans traduire, donc une
question en français ne trouvera rien : c'est signalé, pas maquillé.

Exemple réel : « Quelle est la procédure pour déverrouiller le capot
manuellement ? » devient `manual hood release`, `hood unlock procedure`,
`frunk release`, ramène les articles 43854, 45791 et 44688, et produit la
procédure complète étape par étape en 15 s.

### Numéros d'article vérifiés

Le modèle a été pris en flagrant délit : pour l'article **43854**, il a cité
**[4385400]**, deux zéros ajoutés par mimétisme avec les identifiants à sept
chiffres. Un technicien qui saisit ce numéro dans Toolbox tombe sur une autre
procédure.

`verifier_citations()` neutralise donc toute référence introuvable, la remplace
par `[référence non vérifiée]` dans le texte affiché et l'annonce en bandeau
rouge. Un numéro est tenu pour valable s'il est l'id d'une source **ou** s'il
figure tel quel dans les extraits : une référence croisée que l'article cite
lui-même (`#3199500`) est une information utile, pas une invention.

### Filtrage par type, et choix par titre

Deux étapes supplémentaires sur les questions libres, chacune corrigeant un
échec constaté.

**Filtrage par type.** `types=` est un paramètre confirmé de l'endpoint de
recherche : `BMS update` renvoie 1039 résultats sans filtre, 38 avec
`types=FAQ`, 45 avec `types=FAQ,Topic,Action`, ce qui recoupe exactement les
comptes de l'agrégation `by_type`. Types observés et leurs `type_id` :
`Alert` (310), `Issue` (306), `DTC` (1436), `FAQ` (313), `Action` (483),
`Symptom` (311), `Topic` (624). Une question sans code ne vise pas les fiches
d'alerte : on interroge d'abord `FAQ,Topic,Action`, puis on rouvre aux `Issue`
si la moisson est maigre, puisque ce sont eux qui portent les procédures dans
`steps_to_test` / `steps_to_fix`.

**Choix par titre.** Une recherche renvoie une quinzaine de titres pour une
seule requête HTTP. Les faire trier par le modèle coûte un appel et rien de
plus, alors que récupérer un article de trop coûte une requête Toolbox et une
pause anti-Akamai. C'est nécessaire : sur « comment mettre à jour le BMS », les
dix premiers résultats étaient des articles de **panne pendant** une mise à jour
(« Software update unsuccessful due to LVBMS CAN ECU exit code 6 »), et la
documentation utile arrivait bien plus loin. Prendre les trois premiers par
score était donc systématiquement faux sur ce type de question.

Si le modèle juge qu'aucun titre ne répond franchement, on récupère quand même
les plus proches et le journal l'indique : du contexte approximatif vaut mieux
qu'une réponse vide, à condition de le dire.

### Actions Toolbox : hors périmètre

Les Actions (routines ODIN, réseaux Autodiag) sont des **scripts exécutables sur
le véhicule**. Ce projet est strictement documentaire : il lit, il n'exécute
rien, et n'écrira jamais sur le réseau d'un véhicule.

Elles constituent une recherche **distincte** de celle des articles : l'onglet
Actions de Toolbox a son propre index, et la liste ne se peuple qu'une fois
connecté à un véhicule. Certaines opérations n'existent donc que là, sans
article correspondant. Quand une question demande comment exécuter une
opération et que seules ses pannes sont documentées, l'assistant le dit et
renvoie vers l'onglet Actions.

Indexer le *libellé et la description* des Actions serait légitime et utile.
Il manque l'endpoint : à relever dans le DevTools, véhicule connecté, onglet
Network filtré sur **Fetch/XHR**, en cherchant une requête contenant
« action ». Avec l'URL et une réponse, l'indexation s'ajoute comme pour les
articles.

Le repli seul ne suffit pas dans le chat : il renvoie les articles les plus
proches, et le modèle répondrait sur un **autre** code sans le signaler. `/chat`
vérifie donc, après toute tentative de récupération live, quels codes de la
question restent absents de l'index, l'affiche en bandeau rouge et l'injecte
dans le prompt. Mesuré sur le corpus synthétique : une requête `BMS_a067` sans
filtre remonte 3 résultats sur 5 portant un autre code, dont un `BMS_a066` à
0,69 de score.

## Tests

Aucun n'a besoin de réseau ni de jeton Tesla.

```bash
# 1. Pipeline d'indexation et de recherche
python tests/make_fake_corpus.py /tmp/fake.jsonl
python rag.py index /tmp/fake.jsonl --backend hash --db /tmp/qd
python rag.py search "BMS_a066 apres swap" --backend hash --db /tmp/qd

# 2. Chat en mode live, contre un faux Toolbox local (37 assertions)
python tests/test_chat_live.py

# 3. Formes confirmées de l'API réelle (19 assertions)
python tests/test_api_reelle.py

# 4. Liste de conversations + persistance de session (39 assertions)
python tests/test_conversations.py

# 5. Extension : manifeste, scripts, empaquetage, cloisonnement (27 assertions)
python tests/test_extension.py

# 6. Questions libres : réécriture en mots-clés, citations vérifiées (26 assertions)
python tests/test_recherche_libre.py
```

Le corpus synthétique contient volontairement `BMS_a066` et `BMS_a067` pour
vérifier que le filtre ne les confond pas.

`tests/fake_toolbox.py` imite les deux endpoints confirmés et se pilote par
attributs de classe (`statut_force = 401`, `cle_resultats`), ce qui permet de
rejouer un jeton expiré ou un changement de clé d'API. Il est aussi lançable
seul (`python tests/fake_toolbox.py`) pour développer l'interface sans jeton.

`python -c "from faultcodes import extract_codes; print(extract_codes('BMS_a066_SOC_Imbalance_Warning'))"`
doit renvoyer `['BMS_a066']`. Ce cas mérite son test : `_` est un caractère de
mot en regex, donc un `\b` final empêche de reconnaître un code suivi d'un
suffixe — c'est-à-dire la forme de presque tous les titres Toolbox.

## Rappel

Ton abonnement Toolbox est nominatif et le contenu est sous copyright Tesla.
Corpus interne, pas de rediffusion.

Piste à creuser : en Europe, le règlement 2018/858 impose aux constructeurs de
donner aux réparateurs indépendants un accès aux informations techniques (RMI).
Selon ton statut d'opérateur indépendant, il existe peut-être une voie d'accès
officielle plus stable qu'une aspiration qui casse à chaque changement d'API.
