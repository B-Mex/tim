#!/bin/bash
# Riegel gegen private Daten im oeffentlichen Repo (github.com/B-Mex/tim).
#
# Warum es ihn gibt: An diesem Ordner arbeiten mehrere Sitzungen
# gleichzeitig, und der Betriebsordner hat eine Zwillingsablage unter
# ~/Desktop/M1_DEPLOYMENT, in der die Bereinigung NICHT stattgefunden hat.
# Wer von dort herueberkopiert, holt private Angaben zurueck ins
# oeffentliche Repo, ohne es zu merken. Eine Absprache haelt das nicht
# auf - ein Riegel schon.
#
# Geprueft wird der ANGEMELDETE Inhalt (git diff --cached), also genau
# das, was der Commit veroeffentlichen wuerde - nicht die Arbeitskopie.
#
# Die Suchmuster stehen bewusst NICHT in dieser Datei, sondern in
# config/datenschutz_muster.txt (steht in .gitignore). Grund: Die Muster
# SIND die privaten Angaben. Stuenden sie hier, wuerde ausgerechnet der
# Waechter sie veroeffentlichen - genau das ist beim ersten Entwurf
# passiert, der Hook hat sich selbst abgelehnt.
#
# Einbauen:  ln -sf ../../scripts/git_hook_datenschutz.sh .git/hooks/pre-commit
# Umgehen (nur mit gutem Grund):  git commit --no-verify

set -u

WURZEL="$(git rev-parse --show-toplevel)"
MUSTERDATEI="$WURZEL/config/datenschutz_muster.txt"

if [ ! -f "$MUSTERDATEI" ]; then
    echo "ABGELEHNT: $MUSTERDATEI fehlt - der Datenschutz-Riegel kann nicht pruefen."
    echo "Anlegen mit:  cp config/datenschutz_muster.txt.example config/datenschutz_muster.txt"
    echo "Danach die eigenen Angaben eintragen (die Datei bleibt lokal)."
    exit 1
fi

# Musterdatei lesen: '#' ist Kommentar, '!' beginnt eine Ausnahme.
VERBOTEN=()
AUSNAHMEN=()
while IFS= read -r zeile; do
    case "$zeile" in
        ""|\#*) continue ;;
        !*)     AUSNAHMEN+=("${zeile#!}") ;;
        *)      VERBOTEN+=("$zeile") ;;
    esac
done < "$MUSTERDATEI"

if [ "${#VERBOTEN[@]}" -eq 0 ]; then
    echo "ABGELEHNT: $MUSTERDATEI enthaelt kein einziges Muster."
    exit 1
fi

# Nur hinzugefuegte Zeilen betrachten - was schon im Repo steht, ist
# bereits geprueft, und geloeschte Zeilen sind ungefaehrlich.
ANGEMELDET="$(git diff --cached -U0 | grep -E "^\+" | grep -v "^+++")"

FEHLER=0
for muster in "${VERBOTEN[@]}"; do
    # Mit Umfeld suchen, damit die Ausnahmen greifen koennen.
    # Bewusst OHNE -i: sonst traefe das Muster fuer den Vornamen auch
    # jedes "max(" im Python-Code. Wer Schreibweisen abdecken will,
    # schreibt das ins Muster ([Mm]uster).
    treffer="$(printf '%s\n' "$ANGEMELDET" \
               | grep -oE ".{0,14}${muster}.{0,16}" 2>/dev/null)"
    for ausnahme in "${AUSNAHMEN[@]}"; do
        treffer="$(printf '%s\n' "$treffer" | grep -viE "$ausnahme")"
    done
    treffer="$(printf '%s\n' "$treffer" | grep -v "^$" | head -3)"
    if [ -n "$treffer" ]; then
        echo "ABGELEHNT: private Angabe im angemeldeten Inhalt:"
        printf '%s\n' "$treffer" | sed 's/^/    .../'
        FEHLER=1
    fi
done

if [ "$FEHLER" -ne 0 ]; then
    echo
    echo "Nichts wurde committet. Haeufigste Ursache: eine Datei wurde aus"
    echo "~/Desktop/M1_DEPLOYMENT herueberkopiert - dort ist die Bereinigung"
    echo "nicht passiert. Fundstellen richtigstellen (die Person heisst im"
    echo "Repo 'Mexla'), dann erneut anmelden."
    exit 1
fi

exit 0
