#!/usr/bin/env bash
# État du crawl Toolbox, en un coup d'œil.
#
# Le crawl se fait en deux phases, et c'est ce qui rend son avancement
# trompeur : il énumère d'abord les identifiants d'articles par requêtes-graines,
# et ne télécharge qu'ensuite. Pendant toute la phase 1, le nombre d'articles en
# cache ne bouge pas, ce qui donne l'impression qu'il ne se passe rien.

LOG="$(dirname "$0")/crawl.log"
RAW="$(dirname "$0")/raw"

PID=$(pgrep -f "[h]arvest.py crawl" | head -1)
if [ -n "$PID" ]; then
  echo "état      : EN COURS  (pid $PID, depuis $(ps -o etime= -p "$PID" | tr -d ' '))"
else
  echo "état      : ARRÊTÉ"
fi

[ -r "$LOG" ] || { echo "journal introuvable : $LOG"; exit 1; }

GRAINES=$(grep -c '^  graine' "$LOG")
PAGES=$(grep -c 'p[0-9]*:' "$LOG")
COURANTE=$(grep '^  graine' "$LOG" | tail -1 | sed 's/.*« \(.*\) ».*/\1/')
CACHE=$(ls "$RAW"/*.json 2>/dev/null | wc -l)

if grep -q "articles à récupérer\|à télécharger" "$LOG"; then
  echo "phase     : 2 sur 2, téléchargement des articles"
else
  echo "phase     : 1 sur 2, énumération des identifiants"
fi
echo "graines   : $GRAINES / 33   (en cours : ${COURANTE:-—})"
echo "pages     : $PAGES"
echo "en cache  : $CACHE articles"
echo
echo "--- cinq dernières lignes ---"
tail -5 "$LOG"
echo
echo "Suivi continu :  tail -f $LOG"
