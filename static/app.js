/* Interface de recherche Toolbox, Doctor-Watt.
   Pas de framework : deux écrans, une liste de conversations, un fil. Le jeton
   n'est jamais stocké côté navigateur : le serveur local le garde (et le
   persiste dans .session.json pour survivre à un redémarrage). Ici on ne
   connaît que l'état « session ouverte ou non » et son échéance. */

const $ = (id) => document.getElementById(id);

const vueConnexion = $("vue-connexion");
const vueChat = $("vue-chat");
const fil = $("fil");

/* Le serveur tient l'historique de chaque conversation : on ne garde ici que
   l'identifiant de celle qui est affichée. Un rechargement de page la retrouve
   donc intacte. */
let conversationCourante = null;
let envoiEnCours = false;

/* Le suffixe est avalé avec le code : « BMS_a066_SOC_Imbalance_Warning » est un
   seul identifiant, le couper en deux donne une pastille qui a l'air cassée. */
const CODE_DEFAUT = /\b([A-Z][A-Z0-9]{1,7}_[afwu]\d{3}(?:_[A-Za-z][A-Za-z0-9]*)*)/g;

function echapper(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/* Le texte du modèle est échappé d'abord, puis on réinjecte uniquement nos
   propres balises : mise en valeur des codes défaut et des citations
   d'article. Aucune balise venant du modèle n'est interprétée. */
function enrichir(texte) {
  let h = echapper(texte);
  h = h.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  /* Italique à une seule astérisque : sans ça les « * » du modèle fuitent en
     clair autour des noms d'alerte. Le gras est traité avant, donc il ne reste
     plus d'astérisque doublée à ce stade. */
  h = h.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  h = h.replace(CODE_DEFAUT, '<span class="code-defaut">$1</span>');
  h = h.replace(/\[(\d{5,10})\]/g,
    '<a class="source" style="padding:1px 8px;font-size:0.8rem;" target="_blank" rel="noopener" ' +
    'href="https://toolbox.tesla.com/articles/$1">[$1]</a>');
  return h;
}

function afficherErreurConnexion(message) {
  const p = $("erreur-connexion");
  p.textContent = message;
  p.hidden = !message;
}

function montrer(vue) {
  vueConnexion.hidden = vue !== "connexion";
  vueChat.hidden = vue !== "chat";
  /* Le focus va sur l'élément qui existe dans la vue affichée : le champ de
     collage a disparu avec la saisie manuelle. */
  ($(vue === "connexion" ? "bouton-connecter" : "champ-question"))?.focus();
}

function majCompteurs(etat) {
  const codes = etat.codes_indexes ?? 0;
  const articles = etat.articles_indexes ?? 0;
  $("compteur-codes").textContent = `${codes} code${codes === 1 ? "" : "s"} indexé${codes === 1 ? "" : "s"}`;
  $("compteur-articles").textContent = `${articles} article${articles === 1 ? "" : "s"}`;
  if (etat.modele_llm !== undefined) {
    $("etat-modele").textContent = etat.modele_llm && etat.modele_llm !== "stub"
      ? `Modèle ${etat.modele_llm}`
      : "Modèle non configuré : extraits seuls";
  }
}

/* --------------------------------------------------------------------- */
/* Rendu du fil                                                          */
/* --------------------------------------------------------------------- */
function ajouterQuestion(texte) {
  fil.querySelector(".accueil")?.remove();
  const d = document.createElement("div");
  d.className = "message technicien";
  d.textContent = texte;
  fil.appendChild(d);
  defiler();
}

function afficherReflexion() {
  const d = document.createElement("div");
  d.className = "reflexion";
  d.id = "reflexion";
  d.innerHTML = '<span class="spinner"></span><span>Recherche dans la documentation…</span>';
  fil.appendChild(d);
  defiler();
  /* Après quelques secondes, c'est presque toujours une récupération live :
     autant le dire, sinon l'attente paraît anormale. */
  setTimeout(() => {
    const t = document.querySelector("#reflexion span:last-child");
    if (t) t.textContent = "Code absent de l'index, consultation de Toolbox…";
  }, 2500);
}

function retirerReflexion() {
  $("reflexion")?.remove();
}

function ligneJournal(e) {
  if (e.etape === "mots_cles") {
    /* Le moteur Toolbox est lexical et anglophone : la question est réécrite en
       mots-clés. L'afficher évite de se demander pourquoi telle recherche a
       été lancée. */
    const r = (e.requetes || []).map((x) => `« ${echapper(x)} »`).join(", ");
    return r ? `Question traduite en mots-clés : ${r}` : "Aucun mot-clé exploitable";
  }
  if (e.etape === "choix") {
    const t = (e.retenus || []).map((x) => echapper(x)).join(" · ");
    const note = e.approximatif
      ? " (aucun titre ne répond franchement, les plus proches ont été pris)" : "";
    return `${e.examines} titres examinés${note}, retenus : ${t || "aucun"}`;
  }
  if (e.etape === "recherche") {
    return e.trouves
      ? `Recherche Toolbox « ${echapper(e.requete)} »${e.types ? ` (types ${echapper(e.types)})` : ""} : ${e.trouves} article(s)`
      : `Recherche Toolbox « ${echapper(e.requete)} » : aucun résultat`;
  }
  if (e.erreur) {
    return `Article ${e.article_id} : <span class="erreur">${echapper(e.erreur)}</span>`;
  }
  const origine = e.source === "toolbox"
    ? '<span class="depuis-toolbox">récupéré sur Toolbox</span>'
    : "déjà en cache";
  const codes = (e.codes || []).map((c) => `<span class="code-defaut">${echapper(c)}</span>`).join(" ");
  return `Article ${e.article_id} ${echapper(e.titre || "")} : ${origine}, ` +
         `${e.chunks} chunk(s) indexé(s) ${codes}`;
}

function ajouterReponse(d) {
  const tour = document.createElement("div");
  tour.className = "tour";

  /* L'avertissement passe AVANT la réponse : il qualifie tout ce qui suit.
     `avertissement` est le cas précis (code absent de l'index) ; le repli
     sémantique seul est le cas générique. Le premier prime. */
  const alerte = d.avertissement || (d.repli_semantique
    ? "Aucun article ne porte exactement ce code : les extraits ci-dessous viennent " +
      "d'une recherche sémantique et peuvent concerner un autre code."
    : null);
  if (alerte) {
    const avert = document.createElement("div");
    avert.className = "bandeau-securite danger";
    avert.style.fontSize = "0.9rem";
    avert.textContent = alerte;
    tour.appendChild(avert);
  }

  const bulle = document.createElement("div");
  bulle.className = "message assistant";
  bulle.innerHTML = enrichir(d.reponse);
  tour.appendChild(bulle);

  /* Repli de modèle : le dire, un modèle plus petit rédige moins bien. */
  if (d.modele && d.modele_prevu && d.modele !== d.modele_prevu) {
    const info = document.createElement("div");
    info.className = "bandeau-securite information";
    info.style.fontSize = "0.9rem";
    info.textContent = `${d.modele_prevu} n'a pas répondu, cette synthèse vient ` +
      `du modèle de repli ${d.modele}.`;
    tour.appendChild(info);
  }

  if (d.sources?.length) {
    const t = document.createElement("p");
    t.className = "titre-sources";
    t.textContent = "Voici les articles associés sur Toolbox :";
    tour.appendChild(t);

    const s = document.createElement("div");
    s.className = "sources";
    s.innerHTML = d.sources.map((src) =>
      `<a class="source" target="_blank" rel="noopener" href="${echapper(src.url)}">` +
      `<span class="num">${echapper(src.article_id)}</span>${echapper(src.titre)}</a>`
    ).join("");
    tour.appendChild(s);
  }

  if (d.extraits?.length) {
    const det = document.createElement("details");
    det.className = "detail";
    det.innerHTML =
      `<summary>Extraits utilisés (${d.extraits.length})</summary>` +
      d.extraits.map((e) =>
        `<div class="extrait"><span class="titre">${echapper(e.article_id)} ${echapper(e.titre)} ` +
        `<span class="discret">score ${echapper(e.score)}</span></span>${echapper(e.texte)}</div>`
      ).join("");
    tour.appendChild(det);
  }

  if (d.journal?.length) {
    const det = document.createElement("details");
    det.className = "detail";
    det.innerHTML =
      `<summary>Détail de la recherche (${d.journal.length} étape${d.journal.length === 1 ? "" : "s"})</summary>` +
      `<ul class="journal">${d.journal.map((e) => `<li>${ligneJournal(e)}</li>`).join("")}</ul>`;
    tour.appendChild(det);
  }

  fil.appendChild(tour);
  defiler();
}

function ajouterErreur(message) {
  const d = document.createElement("div");
  d.className = "bandeau-securite danger";
  d.textContent = message;
  fil.appendChild(d);
  defiler();
}

function defiler() {
  fil.scrollTop = fil.scrollHeight;
}

/* --------------------------------------------------------------------- */
/* Appels réseau                                                         */
/* --------------------------------------------------------------------- */
async function appel(chemin, options = {}) {
  const r = await fetch(chemin, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const corps = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = new Error(corps.detail || `Erreur ${r.status}`);
    e.status = r.status;
    throw e;
  }
  return corps;
}

async function envoyer(question) {
  if (!question.trim() || envoiEnCours) return;
  envoiEnCours = true;
  $("bouton-envoyer").disabled = true;
  ajouterQuestion(question);
  afficherReflexion();
  try {
    const d = await appel("/chat", {
      method: "POST",
      body: JSON.stringify({ question, conversation_id: conversationCourante }),
    });
    retirerReflexion();
    ajouterReponse(d);
    /* Le serveur crée la conversation au premier message : on récupère son id
       et on rafraîchit la liste, dont le titre vient d'être calculé. */
    conversationCourante = d.conversation_id;
    majTitreCourant(d.conversation_titre);
    await chargerListe();
    majCompteurs(await appel("/session/toolbox"));
  } catch (e) {
    retirerReflexion();
    if (e.status === 401) {
      /* Jeton expiré : le serveur a déjà fermé la session. */
      afficherErreurConnexion(
        "La session Toolbox a expiré. Clique « Se connecter à Toolbox » pour " +
        "la renouveler. Les articles déjà récupérés restent indexés."
      );
      montrer("connexion");
      demarrerSondeLente();
    } else {
      ajouterErreur(e.message || "Le serveur local est injoignable.");
    }
  } finally {
    envoiEnCours = false;
    $("bouton-envoyer").disabled = false;
    $("champ-question").focus();
  }
}


/* --------------------------------------------------------------------- */
/* Liste des conversations                                               */
/* --------------------------------------------------------------------- */
/* Regroupement par date, comme la console : le repère temporel vit dans le
   libellé de groupe, pas sur chaque ligne. Une heure par item n'aide jamais à
   choisir une conversation, elle ne fait que charger la liste. */
function groupeDe(ts) {
  if (!ts) return "Plus ancien";
  const j = 86400;
  const debutAuj = new Date().setHours(0, 0, 0, 0) / 1000;
  if (ts >= debutAuj) return "Aujourd'hui";
  if (ts >= debutAuj - j) return "Hier";
  if (ts >= debutAuj - 7 * j) return "7 derniers jours";
  if (ts >= debutAuj - 30 * j) return "30 derniers jours";
  return "Plus ancien";
}

const ORDRE_GROUPES = ["Aujourd'hui", "Hier", "7 derniers jours",
                       "30 derniers jours", "Plus ancien"];

function afficherAccueil() {
  const t = $("modele-accueil").content.cloneNode(true);
  t.querySelector(".tour").classList.add("accueil");
  fil.appendChild(t);
}

function majTitreCourant(titre) {
  $("titre-conversation").textContent = titre || "Nouvelle conversation";
}

async function chargerListe() {
  const { conversations } = await appel("/conversations");
  const nav = $("liste-conversations");
  if (!conversations.length) {
    nav.innerHTML = '<p class="liste-vide">Aucune conversation pour le moment. ' +
      'Pose une question pour en démarrer une.</p>';
    return;
  }
  const paquets = new Map();
  for (const c of conversations) {
    const g = groupeDe(c.maj_le);
    if (!paquets.has(g)) paquets.set(g, []);
    paquets.get(g).push(c);
  }
  nav.innerHTML = ORDRE_GROUPES.filter((g) => paquets.has(g)).map((g) =>
    `<p class="groupe-date">${g}</p>` + paquets.get(g).map((c) =>
      `<div class="conversation-item${c.id === conversationCourante ? " courante" : ""}" data-id="${echapper(c.id)}">
        <button class="ouvrir" title="${echapper(c.titre)}">${echapper(c.titre)}</button>
        <button class="supprimer" title="Supprimer" aria-label="Supprimer cette conversation">×</button>
      </div>`).join("")
  ).join("");
}

/* Réaffiche une conversation enregistrée. Les tours sont reconstitués avec
   leurs sources, extraits et journal : c'est pour ça que le serveur les
   conserve, et non seulement le texte. */
async function ouvrirConversation(cid) {
  if (envoiEnCours) return;
  const c = await appel(`/conversations/${cid}`);
  conversationCourante = c.id;
  majTitreCourant(c.titre);
  fil.innerHTML = "";
  const messages = c.messages || [];
  if (!messages.length) {
    afficherAccueil();
  } else {
    for (const m of messages) {
      if (m.role === "user") {
        const d = document.createElement("div");
        d.className = "message technicien";
        d.textContent = m.content;
        fil.appendChild(d);
      } else {
        ajouterReponse({ reponse: m.content, sources: m.sources,
          extraits: m.extraits, journal: m.journal,
          avertissement: m.avertissement, repli_semantique: m.repli_semantique,
          modele: m.modele, modele_prevu: m.modele_prevu });
      }
    }
  }
  await chargerListe();
  fermerPanneauMobile();
  defiler();
  $("champ-question").focus();
}

async function nouvelleConversation() {
  if (envoiEnCours) return;
  /* On ne crée rien côté serveur tout de suite : une conversation vide qu'on
     abandonnerait encombrerait la liste. Le serveur la crée au 1er message. */
  conversationCourante = null;
  majTitreCourant(null);
  fil.innerHTML = "";
  afficherAccueil();
  await chargerListe();
  fermerPanneauMobile();
  $("champ-question").focus();
}

function fermerPanneauMobile() {
  $("panneau").classList.remove("ouvert");
}

$("liste-conversations").addEventListener("click", async (ev) => {
  const item = ev.target.closest(".conversation-item");
  if (!item) return;
  const cid = item.dataset.id;
  if (ev.target.closest(".supprimer")) {
    const titre = item.querySelector(".ouvrir")?.textContent || "cette conversation";
    /* Irréversible : le fichier est réécrit sans elle. */
    if (!window.confirm(`Supprimer « ${titre} » ?`)) return;
    await appel(`/conversations/${cid}`, { method: "DELETE" }).catch(() => {});
    if (cid === conversationCourante) await nouvelleConversation();
    else await chargerListe();
    return;
  }
  if (cid !== conversationCourante) await ouvrirConversation(cid);
  else fermerPanneauMobile();
});

$("bouton-nouvelle").addEventListener("click", () => void nouvelleConversation());
$("bascule-panneau").addEventListener("click", () =>
  $("panneau").classList.toggle("ouvert"));

function majEtatSession(etat) {
  const p = $("etat-session");
  if (!etat.active) { p.textContent = ""; return; }
  const s = etat.secondes_restantes;
  p.textContent = s == null
    ? "Session Toolbox ouverte."
    : s > 3600
      ? `Session Toolbox valable ${Math.floor(s / 3600)} h encore.`
      : s > 0
        ? `Session Toolbox : moins d'une heure restante.`
        : "Session Toolbox expirée, ressaisis un jeton.";
}



/* --------------------------------------------------------------------- */
/* Connexion : l'extension pousse le jeton, la page l'attend               */
/* --------------------------------------------------------------------- */
/* La page ne peut pas lire le cookie de toolbox.tesla.com (règle d'origine
   unique) : c'est l'extension qui le fait, dans TON navigateur, et qui poste
   ici. Le rôle du bouton est donc d'ouvrir Toolbox, ce qui déclenche l'envoi
   automatique de l'extension ; ensuite on sonde jusqu'à voir la session
   ouverte. */
const DELAI_ATTENTE_MS = 180000;
let sonde = null;
let sondeLente = null;
let debutAttente = 0;

function afficherProgression(message) {
  $("progression").hidden = !message;
  if (message) $("message-progression").textContent = message;
}

function arreterSonde() {
  if (sonde) clearInterval(sonde);
  sonde = null;
  afficherProgression(null);
  $("bouton-connecter").disabled = false;
}

async function sessionOuverte() {
  try {
    const etat = await appel("/session/toolbox");
    return etat.active === true;
  } catch {
    return false;
  }
}

async function verifierSession({ actif }) {
  if (!(await sessionOuverte())) {
    if (actif && Date.now() - debutAttente > DELAI_ATTENTE_MS) {
      arreterSonde();
      afficherErreurConnexion(
        "Aucun jeton reçu. Vérifie que l'extension est installée et activée, "
        + "puis recharge l'onglet Toolbox. Son icône doit afficher un ✓ bleu."
      );
      $("aide-extension").open = true;
    }
    return;
  }
  arreterSonde();
  arreterSondeLente();
  afficherErreurConnexion("");
  await entrerDansLeChat();
}

/* Sondage lent en fond, écran de connexion affiché : si l'extension pousse le
   jeton depuis un onglet Toolbox déjà ouvert, on ne veut pas obliger à
   cliquer quoi que ce soit. */
function demarrerSondeLente() {
  if (sondeLente) return;
  sondeLente = setInterval(() => void verifierSession({ actif: false }), 3000);
}

function arreterSondeLente() {
  if (sondeLente) clearInterval(sondeLente);
  sondeLente = null;
}

$("bouton-connecter").addEventListener("click", () => {
  afficherErreurConnexion("");
  $("bouton-connecter").disabled = true;
  debutAttente = Date.now();
  afficherProgression("Toolbox ouvert dans un nouvel onglet, j'attends le jeton…");
  /* Onglet plutôt que fenêtre : on reste dans le même navigateur, donc dans la
     session Tesla déjà établie. C'était l'erreur de la version précédente. */
  window.open("https://toolbox.tesla.com/", "_blank", "noopener");
  sonde = setInterval(() => void verifierSession({ actif: true }), 2000);
});

$("annuler-connexion").addEventListener("click", () => {
  arreterSonde();
});

/* Bascule vers le chat, appelée dès que la session est ouverte. */
async function entrerDansLeChat() {
  const etat = await appel("/session/toolbox");
  majCompteurs(etat);
  majEtatSession(etat);
  montrer("chat");
  const { conversations } = await appel("/conversations");
  if (conversations.length) await ouvrirConversation(conversations[0].id);
  else await nouvelleConversation();
}

/* --------------------------------------------------------------------- */
/* Câblage                                                               */
/* --------------------------------------------------------------------- */
$("bouton-deconnexion").addEventListener("click", async () => {
  await appel("/session/toolbox", { method: "DELETE" }).catch(() => {});
  afficherErreurConnexion("");
  montrer("connexion");
  demarrerSondeLente();
});

$("formulaire-question").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const champ = $("champ-question");
  const q = champ.value;
  champ.value = "";
  champ.style.height = "auto";
  void envoyer(q);
});

/* Entrée envoie, Maj+Entrée passe à la ligne. */
$("champ-question").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    $("formulaire-question").requestSubmit();
  }
});

$("champ-question").addEventListener("input", (ev) => {
  ev.target.style.height = "auto";
  ev.target.style.height = Math.min(ev.target.scrollHeight, 180) + "px";
});

fil.addEventListener("click", (ev) => {
  const b = ev.target.closest(".exemple");
  if (b) void envoyer(b.textContent.trim());
});

/* Au chargement : la session est-elle déjà ouverte côté serveur ? */
(async () => {
  appel("/extension/chemin").then((d) => {
    /* Sous WSL, le navigateur est sous Windows : c'est le chemin UNC qui est
       utilisable dans son sélecteur de fichiers, pas le chemin POSIX. */
    const c = d?.chemin_windows || d?.chemin;
    if (c) $("chemin-extension").textContent = c;
  }).catch(() => {});

  try {
    const etat = await appel("/session/toolbox");
    majCompteurs(etat);
    majEtatSession(etat);
    if (etat.active) {
      await entrerDansLeChat();
    } else {
      montrer("connexion");
      demarrerSondeLente();
    }
  } catch {
    afficherErreurConnexion(
      "Le serveur local ne répond pas. Vérifie que `uvicorn serve:app` tourne."
    );
    montrer("connexion");
  }
})();
