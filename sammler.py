#!/usr/bin/env python3
"""Goldberater-Sammler: holt stuendlich die Auktionshausdaten von WoW Forever
aus der Blizzard-API und speichert pro Item Preise, Menge und Abgang.

Braucht nur Python 3, keine Zusatzpakete.
Zugangsdaten kommen aus den Umgebungsvariablen BLIZZARD_CLIENT_ID und
BLIZZARD_CLIENT_SECRET (auf GitHub: Repository-Geheimnisse).

Ablage: data/haus-<Nr>/<Datum>/<HHMM>.csv, eine Datei pro Abruf (Zeit in UTC).
So waechst das Projekt nur um neue kleine Dateien, statt grosse Dateien
stuendlich neu zu speichern.

Spalten der CSV-Dateien (Preise in Kupfer pro Stueck):
  item_id       Item-Nummer
  min_preis     guenstigstes Angebot
  p15_preis     Preis, zu dem die guenstigsten 15 % der Menge zu haben sind
                (robust gegen einzelne Lockangebote)
  median_preis  mittlerer Preis ueber die gesamte Menge
  menge         angebotene Stueckzahl
  auktionen     Zahl der Angebote
  verschwunden  Stueck, die seit dem letzten Abruf verschwunden sind und sicher
                nicht ausgelaufen sein koennen (verkauft oder zurueckgezogen;
                leer, wenn kein Vergleich moeglich)
  verschwunden_unsicher
                Stueck, die verschwunden sind, aber auch ausgelaufen sein koennten
"""
import base64
import csv
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REGION = os.environ.get("REGION", "eu")
SPRACHE = "de_DE"
API = f"https://{REGION}.api.blizzard.com"
NAMENSRAUM = f"dynamic-forever-{REGION}"
DATEN = "data"
ZUSTAND = ".zustand"          # Gedaechtnis zwischen zwei Laeufen, wird nicht ins Projekt geschrieben
MAX_ABSTAND = 2 * 3600        # aelterer Vergleichsstand wird nicht mehr benutzt
MIN_ABSTAND = 40 * 60         # juengere Abrufe waeren fast doppelt und werden uebersprungen
# Bekannte Items, an denen du dein Auktionshaus im Spiel wiedererkennst.
VERGLEICH = [(2589, "Leinenstoff"), (2592, "Wollstoff"), (2770, "Kupfererz"), (2318, "Leichtes Leder")]


class NichtGefunden(Exception):
    pass


def hole_token():
    cid = os.environ.get("BLIZZARD_CLIENT_ID", "").strip()
    geheim = os.environ.get("BLIZZARD_CLIENT_SECRET", "").strip()
    if not cid or not geheim:
        sys.exit("FEHLER: BLIZZARD_CLIENT_ID oder BLIZZARD_CLIENT_SECRET fehlt (Schritt 4 der Anleitung).")
    anfrage = urllib.request.Request(
        "https://oauth.battle.net/token",
        data=b"grant_type=client_credentials",
        headers={"Authorization": "Basic " + base64.b64encode(f"{cid}:{geheim}".encode()).decode()},
    )
    try:
        with urllib.request.urlopen(anfrage, timeout=60) as antwort:
            return json.load(antwort)["access_token"]
    except urllib.error.HTTPError as fehler:
        sys.exit(f"FEHLER: Anmeldung bei Blizzard abgelehnt (HTTP {fehler.code}). Client-ID und Secret pruefen.")
    except Exception as fehler:
        sys.exit(f"FEHLER: Blizzard-Anmeldung nicht erreichbar ({fehler!r}). Der naechste Lauf versucht es erneut.")


def hole_json(pfad, token):
    """Ruft einen API-Pfad ab. Wirft NichtGefunden bei 404, versucht sonst dreimal."""
    trenner = "&" if "?" in pfad else "?"
    url = f"{API}{pfad}{trenner}namespace={NAMENSRAUM}&locale={SPRACHE}"
    letzter = None
    for versuch in range(3):
        try:
            anfrage = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(anfrage, timeout=180) as antwort:
                return json.load(antwort)
        except urllib.error.HTTPError as fehler:
            if fehler.code == 404:
                raise NichtGefunden(pfad)
            letzter = f"HTTP {fehler.code}"
        except Exception as fehler:  # Zeitueberschreitung, Verbindungsabbruch, kaputtes JSON
            letzter = repr(fehler)
        time.sleep(5 * (versuch + 1))
    raise RuntimeError(f"{pfad}: {letzter}")


def text(wert):
    """Namen kommen je nach Endpunkt als Text oder als Woerterbuch je Sprache."""
    if isinstance(wert, dict):
        return wert.get(SPRACHE) or wert.get("en_US") or next(iter(wert.values()), "")
    return str(wert) if wert is not None else ""


def item_id(eintrag):
    item = eintrag.get("item")
    if isinstance(item, dict):
        return item.get("id")
    return item


def stueckpreis(eintrag):
    """Preis pro Stueck in Kupfer. Auktionen ohne Sofortkauf haben keinen."""
    menge = eintrag.get("quantity") or 1
    if eintrag.get("unit_price"):
        return int(eintrag["unit_price"]), menge
    if eintrag.get("buyout"):
        return int(eintrag["buyout"]) // menge, menge
    return None, menge


def preis_bei(angebote, gesamt, anteil):
    """Preis, bei dem der gegebene Anteil der Gesamtmenge erreicht ist."""
    grenze, laufend = gesamt * anteil, 0
    for preis, menge in angebote:
        laufend += menge
        if laufend >= grenze:
            return preis
    return angebote[-1][0]


def zusammenfassen(eintraege):
    """Macht aus einzelnen Auktionen eine Zeile pro Item und merkt sich jede Auktion."""
    je_item, stand = {}, {}
    for eintrag in eintraege:
        if not isinstance(eintrag, dict):
            continue
        iid = item_id(eintrag)
        if iid is None:
            continue
        preis, menge = stueckpreis(eintrag)
        if eintrag.get("id") is not None:
            stand[str(eintrag["id"])] = [int(iid), int(menge), eintrag.get("time_left") or ""]
        if preis:
            je_item.setdefault(int(iid), []).append((preis, int(menge)))
    ergebnis = {}
    for iid, angebote in je_item.items():
        angebote.sort()
        gesamt = sum(menge for _, menge in angebote)
        ergebnis[iid] = (
            angebote[0][0],
            preis_bei(angebote, gesamt, 0.15),
            preis_bei(angebote, gesamt, 0.5),
            gesamt,
            len(angebote),
        )
    return ergebnis, stand


def abgang(alt, neu):
    """Stueck je Item, die seit dem letzten Abruf verschwunden sind: (sicher, unsicher).

    sicher:   Die Auktion hatte noch mehr als zwei Stunden Laufzeit (LONG, VERY_LONG)
              oder ihre Menge ist gesunken. Das kann kein Ablauf sein, nur Verkauf
              oder Rueckzug.
    unsicher: Die Auktion hatte hoechstens zwei Stunden Laufzeit (MEDIUM) und kann
              auch ausgelaufen sein.
    Auktionen kurz vor dem Ablauf (SHORT) zaehlen gar nicht.
    Beide Werte sind Obergrenzen fuer Verkaeufe, weil Rueckzuege mitzaehlen.
    """
    sicher, unsicher = {}, {}
    for nummer, (iid, menge, restzeit) in alt.items():
        jetzt = neu.get(nummer)
        if jetzt is not None:
            fehlt = max(0, menge - jetzt[1])
            if fehlt:
                sicher[iid] = sicher.get(iid, 0) + fehlt
        elif restzeit in ("LONG", "VERY_LONG"):
            sicher[iid] = sicher.get(iid, 0) + menge
        elif restzeit != "SHORT":
            unsicher[iid] = unsicher.get(iid, 0) + menge
    return sicher, unsicher


def lade_stand(hid, jetzt_sek):
    pfad = os.path.join(ZUSTAND, f"haus-{hid}.json")
    try:
        with open(pfad, encoding="utf-8") as datei:
            inhalt = json.load(datei)
        if jetzt_sek - inhalt["zeit"] <= MAX_ABSTAND:
            return inhalt["auktionen"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def speichere_stand(hid, jetzt_sek, stand):
    os.makedirs(ZUSTAND, exist_ok=True)
    with open(os.path.join(ZUSTAND, f"haus-{hid}.json"), "w", encoding="utf-8") as datei:
        json.dump({"zeit": jetzt_sek, "auktionen": stand}, datei, separators=(",", ":"))


def muenzen(kupfer):
    if kupfer is None:
        return "-"
    gold, rest = divmod(int(kupfer), 10000)
    silber, kupfer = divmod(rest, 100)
    teile = []
    if gold:
        teile.append(f"{gold}g")
    if silber:
        teile.append(f"{silber}s")
    if kupfer or not teile:
        teile.append(f"{kupfer}k")
    return " ".join(teile)


def gewuenschte_haeuser():
    """haeuser.txt: eine Haus-Nummer pro Zeile. Fehlt die Datei, werden alle Haeuser gesammelt."""
    if not os.path.exists("haeuser.txt"):
        return None
    nummern = set()
    with open("haeuser.txt", encoding="utf-8") as datei:
        for zeile in datei:
            zeile = zeile.split("#")[0].strip()
            if zeile.isdigit():
                nummern.add(int(zeile))
    return nummern or None


def kuerzen(wert):
    """Verkleinert eine API-Antwort auf ihre Struktur, fuer die Fehlersuche."""
    if isinstance(wert, dict):
        return {k: kuerzen(v) for k, v in list(wert.items())[:12]}
    if isinstance(wert, list):
        return [kuerzen(v) for v in wert[:2]] + ([f"... {len(wert)} Eintraege"] if len(wert) > 2 else [])
    return wert


def finde_haeuser(token, probe):
    index = hole_json("/data/wow/ruleset/index", token)
    probe["ruleset_index"] = kuerzen(index)
    regelwerke = index.get("rulesets") or index.get("ruleset") or []
    for regelwerk in regelwerke:
        rid = regelwerk.get("id")
        if rid is None:
            continue
        try:
            haeuser = hole_json(f"/data/wow/ruleset/{rid}/auctions/", token)
        except NichtGefunden:
            continue
        liste = haeuser.get("auctions") or []
        if liste:
            probe["haus_index"] = kuerzen(haeuser)
            return rid, liste
    sys.exit("FEHLER: Die API hat kein Auktionshaus geliefert. Blizzard hat vermutlich etwas geaendert.")


def letzter_abruf(hid):
    """Zeitpunkt der juengsten gespeicherten Datei eines Hauses, oder None."""
    ordner = os.path.join(DATEN, f"haus-{hid}")
    try:
        tage = sorted(t for t in os.listdir(ordner) if os.path.isdir(os.path.join(ordner, t)))
        for tag in reversed(tage):
            dateien = sorted(d for d in os.listdir(os.path.join(ordner, tag)) if d.endswith(".csv"))
            if dateien:
                zeit = datetime.datetime.strptime(tag + dateien[-1][:4], "%Y-%m-%d%H%M")
                return zeit.replace(tzinfo=datetime.timezone.utc)
    except (OSError, ValueError):
        pass
    return None


def schreibe_haus(hid, jetzt, zeilen, weg):
    """Schreibt die Datei dieses Abrufs. Gibt False zurueck, wenn der letzte Abruf zu frisch ist."""
    vorher = letzter_abruf(hid)
    if vorher is not None and (jetzt - vorher).total_seconds() < MIN_ABSTAND:
        return False
    ordner = os.path.join(DATEN, f"haus-{hid}", jetzt.strftime("%Y-%m-%d"))
    os.makedirs(ordner, exist_ok=True)
    with open(os.path.join(ordner, jetzt.strftime("%H%M") + ".csv"), "w", encoding="utf-8", newline="") as datei:
        schreiber = csv.writer(datei)
        schreiber.writerow(["item_id", "min_preis", "p15_preis", "median_preis", "menge", "auktionen",
                            "verschwunden", "verschwunden_unsicher"])
        for iid in sorted(zeilen):
            if weg is None:
                abgaenge = ["", ""]
            else:
                abgaenge = [weg[0].get(iid, 0), weg[1].get(iid, 0)]
            schreiber.writerow([iid, *zeilen[iid], *abgaenge])
    return True


def main():
    jetzt = datetime.datetime.now(datetime.timezone.utc)
    jetzt_sek = int(jetzt.timestamp())
    token = hole_token()
    probe = {}
    try:
        rid, haeuser = finde_haeuser(token, probe)
    except RuntimeError as problem:
        sys.exit(f"FEHLER: Die Blizzard-API antwortet nicht ({problem}). Der naechste Lauf versucht es erneut.")
    auswahl = gewuenschte_haeuser()
    if auswahl and not any(str(haus.get("id")).isdigit() and int(haus["id"]) in auswahl for haus in haeuser):
        sys.exit("FEHLER: Keine Haus-Nummer aus haeuser.txt kommt in der API vor. Nummern pruefen.")
    uebersicht, gespeichert, fehler = [], 0, []

    for haus in haeuser:
        hid, name = haus.get("id"), text(haus.get("name"))
        if hid is None or (auswahl and int(hid) not in auswahl):
            continue
        try:
            eintraege = []
            try:
                antwort = hole_json(f"/data/wow/ruleset/{rid}/auctions/{hid}", token)
                if isinstance(antwort.get("auctions"), list):
                    eintraege += antwort["auctions"]
                probe.setdefault("haus", kuerzen(antwort))
            except NichtGefunden:
                pass
            try:
                waren = hole_json(f"/data/wow/ruleset/{rid}/auctions/{hid}/commodities", token)
                if isinstance(waren.get("auctions"), list):
                    eintraege += waren["auctions"]
                probe.setdefault("waren", kuerzen(waren))
            except NichtGefunden:
                pass
            zeilen, stand = zusammenfassen(eintraege)
            alt = lade_stand(hid, jetzt_sek)
            weg = abgang(alt, stand) if alt is not None else None
            if zeilen and schreibe_haus(hid, jetzt, zeilen, weg):
                speichere_stand(hid, jetzt_sek, stand)
                gespeichert += 1
            uebersicht.append((hid, name, len(eintraege), zeilen))
            print(f"Haus {hid} ({name}): {len(eintraege)} Auktionen, {len(zeilen)} Items, "
                  f"Vergleich mit letztem Abruf: {'ja' if weg is not None else 'nein'}")
        except Exception as problem:
            fehler.append(f"Haus {hid}: {problem}")
            print(f"WARNUNG Haus {hid}: {problem}")

    os.makedirs(DATEN, exist_ok=True)
    with open(os.path.join(DATEN, "uebersicht.md"), "w", encoding="utf-8") as datei:
        datei.write(f"# Auktionshaeuser {REGION.upper()}\n\nStand: {jetzt.strftime('%d.%m.%Y %H:%M')} UTC\n\n")
        datei.write("Tiefstpreise pro Stueck. Vergleiche sie im Spiel mit deinem Auktionshaus.\n\n")
        datei.write("| Haus-Nr. | Name | Auktionen | Items | " + " | ".join(n for _, n in VERGLEICH) + " |\n")
        datei.write("| --- | --- | --- | --- | " + " | ".join("---" for _ in VERGLEICH) + " |\n")
        for hid, name, anzahl, zeilen in sorted(uebersicht, key=lambda eintrag: eintrag[0]):
            preise = " | ".join(muenzen(zeilen[i][0]) if i in zeilen else "-" for i, _ in VERGLEICH)
            datei.write(f"| {hid} | {name} | {anzahl} | {len(zeilen)} | {preise} |\n")
    with open(os.path.join(DATEN, "_probe.json"), "w", encoding="utf-8") as datei:
        json.dump(probe, datei, ensure_ascii=False, indent=1)

    print(f"Fertig: {gespeichert} Haeuser neu gespeichert, {len(fehler)} Fehler.")
    if not uebersicht or all(not zeilen for _, _, _, zeilen in uebersicht):
        sys.exit("FEHLER: Kein einziges Haus hat verwertbare Auktionen geliefert.")


if __name__ == "__main__":
    main()
