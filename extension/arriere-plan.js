/* Envoie le cookie tbx_token de toolbox.tesla.com vers l'outil local.
 *
 * Pourquoi une extension : une page servie depuis 127.0.0.1 ne peut pas lire
 * un cookie de toolbox.tesla.com (règle d'origine unique), et Tesla n'expose
 * aucun client OAuth pour Toolbox. Une extension, elle, a le droit de lire les
 * cookies d'un domaine pour lequel elle a la permission, y compris les cookies
 * HttpOnly que du JavaScript de page ne verrait pas.
 *
 * L'intérêt par rapport à un navigateur piloté par le serveur : ça se passe
 * dans TON navigateur, celui où tu es déjà connecté à Toolbox. Aucune
 * authentification à refaire.
 */

const DOMAINE = "https://toolbox.tesla.com";
const COOKIE = "tbx_token";
const SERVEURS_PAR_DEFAUT = ["http://127.0.0.1:8000", "http://localhost:8000"];

async function serveurs() {
  const { serveur } = await chrome.storage.local.get("serveur");
  return serveur ? [serveur, ...SERVEURS_PAR_DEFAUT] : SERVEURS_PAR_DEFAUT;
}

async function lireJeton() {
  /* getAll plutôt que get : on renvoie toute la ligne de cookie, au cas où
     Akamai exige _abck dans d'autres conditions que celles mesurées. */
  const tous = await chrome.cookies.getAll({ url: DOMAINE });
  const jeton = tous.find((c) => c.name === COOKIE && c.value);
  if (!jeton) return null;
  return tous.map((c) => `${c.name}=${c.value}`).join("; ");
}

async function envoyer({ silencieux = false } = {}) {
  const collage = await lireJeton();
  if (!collage) {
    const message = "Aucun jeton trouvé. Ouvre toolbox.tesla.com et connecte-toi.";
    if (!silencieux) await signaler("erreur", message);
    return { ok: false, message };
  }
  let derniere = "aucun serveur joignable";
  for (const base of await serveurs()) {
    try {
      const r = await fetch(`${base}/session/toolbox`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ collage }),
      });
      const corps = await r.json().catch(() => ({}));
      if (r.ok) {
        await chrome.storage.local.set({ serveur: base, dernier_envoi: Date.now() });
        await signaler("ok", "Session Toolbox ouverte dans l'outil.");
        return { ok: true, base, message: "Session ouverte." };
      }
      derniere = corps.detail || `HTTP ${r.status}`;
    } catch (e) {
      derniere = `${base} injoignable`;
    }
  }
  if (!silencieux) await signaler("erreur", derniere);
  return { ok: false, message: derniere };
}

async function signaler(etat, message) {
  await chrome.storage.local.set({ dernier_etat: etat, dernier_message: message });
  /* Le badge dit l'essentiel sans ouvrir la fenêtre : vert = jeton transmis. */
  await chrome.action.setBadgeText({ text: etat === "ok" ? "✓" : "!" });
  await chrome.action.setBadgeBackgroundColor({
    color: etat === "ok" ? "#134cc9" : "#a8071a",
  });
  setTimeout(() => chrome.action.setBadgeText({ text: "" }), 8000);
}

/* Envoi automatique dès qu'un onglet Toolbox finit de charger : le technicien
   n'a rien à cliquer, y compris après l'expiration des 24 h. Silencieux, pour
   ne pas afficher d'erreur quand l'outil local n'est pas lancé. */
chrome.tabs.onUpdated.addListener((id, info, tab) => {
  if (info.status === "complete" && tab.url?.startsWith(DOMAINE)) {
    void envoyer({ silencieux: true });
  }
});

chrome.runtime.onInstalled.addListener(() => void envoyer({ silencieux: true }));
chrome.runtime.onStartup.addListener(() => void envoyer({ silencieux: true }));

/* Déclenchement manuel depuis la fenêtre de l'extension. */
chrome.runtime.onMessage.addListener((msg, _exp, repondre) => {
  if (msg?.action === "envoyer") {
    envoyer().then(repondre);
    return true;   // réponse asynchrone
  }
});
