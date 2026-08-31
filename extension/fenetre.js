const $ = (id) => document.getElementById(id);

function afficher(etat, message) {
  const p = $("etat");
  p.hidden = !message;
  p.textContent = message || "";
  p.className = etat === "erreur" ? "erreur" : "";
}

$("envoyer").addEventListener("click", async () => {
  const bouton = $("envoyer");
  bouton.disabled = true;
  afficher("info", "Lecture du cookie…");
  const r = await chrome.runtime.sendMessage({ action: "envoyer" });
  afficher(r?.ok ? "info" : "erreur", r?.message || "Aucune réponse.");
  bouton.disabled = false;
});

/* L'adresse n'est mémorisée que si elle est renseignée : par défaut
   l'extension essaie 127.0.0.1:8000 puis localhost:8000. */
$("serveur").addEventListener("change", async (ev) => {
  const v = ev.target.value.trim().replace(/\/+$/, "");
  await chrome.storage.local.set({ serveur: v || null });
  afficher("info", v ? `Adresse retenue : ${v}` : "Adresse par défaut rétablie.");
});

(async () => {
  const d = await chrome.storage.local.get(
    ["serveur", "dernier_etat", "dernier_message", "dernier_envoi"]);
  if (d.serveur) $("serveur").value = d.serveur;
  if (d.dernier_message) {
    const quand = d.dernier_envoi
      ? new Date(d.dernier_envoi).toLocaleTimeString("fr-FR",
          { hour: "2-digit", minute: "2-digit" })
      : null;
    afficher(d.dernier_etat === "erreur" ? "erreur" : "info",
             d.dernier_message + (quand && d.dernier_etat === "ok" ? ` (${quand})` : ""));
  }
})();
