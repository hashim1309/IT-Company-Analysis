"""Uavhengig kontroll av site/data.json.

Regner tallene på nytt direkte fra rådataene i data/raa/ og config/selskaper.csv,
med egen kode (ikke funksjonene i hent_data.py), og sammenligner med det som
står i data.json. Avslutter med feilkode 1 hvis noe ikke stemmer, slik at
GitHub Actions ikke publiserer feil tall.

Bruk:
    python scripts/kontroller.py
"""

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROT = Path(__file__).resolve().parent.parent
feil = []


def sjekk(betingelse, melding):
    if not betingelse:
        feil.append(melding)


def lik(a, b, tol):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol


data = json.loads((ROT / "site" / "data.json").read_text(encoding="utf-8"))
konfig = list(csv.DictReader(open(ROT / "config" / "selskaper.csv", encoding="utf-8")))
Y = data["siste_aar"]
sy = str(Y)


# --- Les rådata på nytt ------------------------------------------------------
def regnskap_per_aar(orgnr):
    raa = json.loads((ROT / "data" / "raa" / f"{orgnr}.json").read_text(encoding="utf-8"))
    beste = {}
    for r in raa.get("regnskap") or []:
        if r.get("regnskapstype") != "SELSKAP":
            continue
        fra, til = r["regnskapsperiode"]["fraDato"], r["regnskapsperiode"]["tilDato"]
        mnd = (int(til[:4]) * 12 + int(til[5:7])) - (int(fra[:4]) * 12 + int(fra[5:7])) + 1
        if mnd != 12:
            continue
        aar = int(til[:4])
        if aar not in beste or (r.get("id") or 0) > (beste[aar].get("id") or 0):
            beste[aar] = r
    ut = {}
    for aar, r in beste.items():
        rr = r["resultatregnskapResultat"]
        ut[aar] = {
            "inntekt": rr["driftsresultat"]["driftsinntekter"].get("sumDriftsinntekter"),
            "lonn": rr["driftsresultat"]["driftskostnad"].get("loennskostnad"),
            "driftsresultat": rr["driftsresultat"].get("driftsresultat"),
            "aarsresultat": rr.get("aarsresultat"),
        }
    return ut, raa.get("enhet") or {}


per_enhet, enhetsinfo, grupper = {}, {}, defaultdict(list)
kategori, verifisert = {}, defaultdict(lambda: True)
for rad in konfig:
    per_enhet[rad["orgnr"]], enhetsinfo[rad["orgnr"]] = regnskap_per_aar(rad["orgnr"])
    grupper[rad["gruppe"]].append(rad["orgnr"])
    kategori[rad["gruppe"]] = rad["kategori"]
    verifisert[rad["gruppe"]] = verifisert[rad["gruppe"]] and rad["sikker"] == "ja"

selskaper = {s["navn"]: s for s in data["selskaper"]}
sjekk(set(selskaper) == set(grupper), "Selskapene i data.json samsvarer ikke med selskaper.csv")


# --- Hvert selskap -------------------------------------------------------------
def kr(x):  # rådata er i kroner, data.json i tusen kroner
    return None if x is None else x / 1000


for navn, orgnr in grupper.items():
    s = selskaper.get(navn)
    if not s:
        continue
    sjekk(s["kategori"] == kategori[navn], f"{navn}: feil kategori")
    sjekk(s["verifisert"] == verifisert[navn], f"{navn}: feil verifisert-merke")
    sjekk(s["i_bransjetall"] == (kategori[navn] != "Rådgivningshus"), f"{navn}: feil i_bransjetall")
    for y_str, v in s["aar"].items():
        y = int(y_str)
        med = [o for o in orgnr if y in per_enhet[o]]
        sjekk(sorted(med) == v["orgnr"], f"{navn} {y}: feil sett av juridiske enheter")
        for felt in ("inntekt", "lonn", "driftsresultat", "aarsresultat"):
            verdier = [per_enhet[o][y][felt] for o in med if per_enhet[o][y][felt] is not None]
            forventet = kr(sum(verdier)) if verdier else None
            sjekk(lik(v[felt], forventet, 1), f"{navn} {y}: {felt} er {v[felt]}, ventet {forventet}")
        if v["inntekt"]:
            sjekk(lik(v["margin"], v["driftsresultat"] / v["inntekt"], 0.0005), f"{navn} {y}: feil margin")
        forrige = [o for o in orgnr if (y - 1) in per_enhet[o]]
        if v["vekst"] is not None:
            sjekk(sorted(forrige) == sorted(med), f"{navn} {y}: vekst regnet på ulike enheter")
            b = kr(sum(per_enhet[o][y - 1]["inntekt"] or 0 for o in forrige))
            sjekk(lik(v["vekst"], kr(sum(per_enhet[o][y]["inntekt"] or 0 for o in med)) / b - 1, 0.0005),
                  f"{navn} {y}: feil vekst")
        sjekk(v["margin"] is None or -5 < v["margin"] < 1, f"{navn} {y}: urimelig margin {v['margin']}")
    # Per ansatt
    if s["lonnskost_per_ansatt"] is not None:
        sett = s["aar"][sy]["orgnr"]
        ansatte = sum(enhetsinfo[o].get("antallAnsatte") or 0 for o in sett)
        sjekk(ansatte == s["ansatte"], f"{navn}: ansatte {s['ansatte']} ≠ {ansatte}")
        sjekk(lik(s["lonnskost_per_ansatt"], s["aar"][sy]["lonn"] / ansatte, 1), f"{navn}: feil lønn per ansatt")
        sjekk(500 <= s["lonnskost_per_ansatt"] <= 2500, f"{navn}: lønn per ansatt utenfor gyldig spenn")


# --- Bransjetall -----------------------------------------------------------------
bransje = [n for n in grupper if kategori[n] != "Rådgivningshus"]
for y_str, m in data["marked"].items():
    y = int(y_str)
    med = [n for n in bransje if y_str in selskaper[n]["aar"]]
    inntekt = sum(selskaper[n]["aar"][y_str]["inntekt"] or 0 for n in med)
    drift = sum(selskaper[n]["aar"][y_str]["driftsresultat"] or 0 for n in med)
    sjekk(m["antall_selskaper"] == len(med), f"Bransje {y}: feil antall selskaper")
    sjekk(lik(m["inntekt"], inntekt, 1), f"Bransje {y}: feil inntekt")
    sjekk(lik(m["driftsresultat"], drift, 1), f"Bransje {y}: feil driftsresultat")
    sjekk(lik(m["margin"], drift / inntekt, 0.0005), f"Bransje {y}: feil margin")
    sjekk(m["med_overskudd"] == sum(1 for n in med if (selskaper[n]["aar"][y_str]["driftsresultat"] or 0) > 0),
          f"Bransje {y}: feil antall med overskudd")
    if m["vekst_like_mot_like"] is not None:
        enheter = [o for n in med for o in grupper[n] if y in per_enhet[o] and (y - 1) in per_enhet[o]
                   and per_enhet[o][y]["inntekt"] is not None and per_enhet[o][y - 1]["inntekt"] is not None]
        a = sum(per_enhet[o][y]["inntekt"] for o in enheter)
        b = sum(per_enhet[o][y - 1]["inntekt"] for o in enheter)
        sjekk(lik(m["vekst_like_mot_like"], a / b - 1, 0.0005), f"Bransje {y}: feil vekst like mot like")
        sjekk(m["antall_i_vekst"] == len(enheter), f"Bransje {y}: feil antall i vekstberegningen")

# --- Analyser: delene må summere seg til helheten -----------------------------
m = data["marked"][sy]
A = data["analyser"]
for navn in ("segmenter", "geografi"):
    sjekk(sum(x["antall_selskaper"] for x in A[navn]) == m["antall_selskaper"], f"{navn}: antall summerer ikke")
    sjekk(lik(sum(x["inntekt"] for x in A[navn]), m["inntekt"], len(A[navn])), f"{navn}: inntekt summerer ikke")
for x in A["segmenter"]:
    med = [n for n in bransje if kategori[n] == x["navn"] and sy in selskaper[n]["aar"]]
    sjekk(x["antall_selskaper"] == len(med), f"Segment {x['navn']}: feil antall")
    sjekk(lik(x["inntekt"], sum(selskaper[n]["aar"][sy]["inntekt"] or 0 for n in med), 1),
          f"Segment {x['navn']}: feil inntekt")
for navn in ("segmenter", "storrelse", "lonn_mot_margin", "geografi"):
    for x in A[navn]:
        sjekk(lik(x["margin"], x["driftsresultat"] / x["inntekt"], 0.0005), f"{navn} {x['navn']}: feil margin")

inntekter = sorted((selskaper[n]["aar"][sy]["inntekt"] or 0 for n in bransje if sy in selskaper[n]["aar"]),
                   reverse=True)
for k, v in A["konsentrasjon"].items():
    n = int(k[4:])
    sjekk(lik(v, sum(inntekter[:n]) / m["inntekt"], 0.0005), f"Konsentrasjon {k}: feil")
underskudd = {n for n in bransje if sy in selskaper[n]["aar"] and (selskaper[n]["aar"][sy]["driftsresultat"] or 0) < 0}
sjekk({x["navn"] for x in A["underskudd"]} == underskudd, "Listen over selskaper med underskudd stemmer ikke")

# --- Topplister ----------------------------------------------------------------
for navn, liste in data["topplister"].items():
    verdier = [x["verdi"] for x in liste]
    sjekk(verdier == sorted(verdier, reverse=True), f"Toppliste {navn}: ikke sortert")
    for x in liste:
        s = selskaper[x["navn"]]
        sjekk(s["verifisert"] and s["i_bransjetall"], f"Toppliste {navn}: {x['navn']} skal ikke være med")
        sjekk((s["aar"][sy]["inntekt"] or 0) >= 20000, f"Toppliste {navn}: {x['navn']} har for lav inntekt")

if feil:
    print(f"KONTROLL FEILET ({len(feil)} avvik):")
    for f in feil[:50]:
        print("  -", f)
    sys.exit(1)
print(f"Kontroll OK: {len(selskaper)} selskaper og alle bransjetall stemmer med rådataene.")
