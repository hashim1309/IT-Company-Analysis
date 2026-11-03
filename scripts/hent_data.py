"""Henter regnskap for IT-konsulentselskaper fra Brønnøysundregistrene
og lager site/data.json som nettsiden leser.

Bruk:
    python scripts/hent_data.py            # henter fra Brreg
    python scripts/hent_data.py --offline  # bruker data/raa/ fra forrige kjøring

Kun standardbiblioteket brukes, så det trengs ingen pip install.
"""

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path

ROT = Path(__file__).resolve().parent.parent
KONFIG = ROT / "config" / "selskaper.csv"
RAA = ROT / "data" / "raa"
ANSATT_HISTORIKK = ROT / "data" / "ansatte_historikk.csv"
UT = ROT / "site" / "data.json"

ENHET_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/{}"
REGNSKAP_URL = "https://data.brreg.no/regnskapsregisteret/regnskap/{}"

# Lønnskostnad per ansatt utenfor dette spennet (tusen kr) tyder på at
# ansatttallet i Brreg er feil, så selskapet holdes utenfor per-ansatt-mål.
LONN_PER_ANSATT_MIN = 500
LONN_PER_ANSATT_MAKS = 2500

# Kategorier som vises på siden, men holdes utenfor bransjetall og topplister
# fordi IT-delen av regnskapet ikke kan skilles ut (f.eks. revisjonshusene).
UTENFOR_BRANSJETALL = {"Rådgivningshus"}

# Vekst rangeres bare når fjorårets inntekt var minst 20 mill. kr (tusen kr),
# ellers dominerer nystartede og utskilte selskaper med flere hundre prosent vekst.
MIN_INNTEKT_VEKST = 20000


def hent_json(url, forsok=3):
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "itkonsulent-analyse/1.0"}
    )
    for i in range(forsok):
        try:
            with urllib.request.urlopen(req, timeout=30) as svar:
                return json.load(svar)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if i == forsok - 1:
                raise
        except urllib.error.URLError:
            if i == forsok - 1:
                raise
        time.sleep(2 * (i + 1))


def les_konfig():
    with open(KONFIG, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def hent_alt(konfig):
    RAA.mkdir(parents=True, exist_ok=True)
    for rad in konfig:
        orgnr = rad["orgnr"]
        enhet = hent_json(ENHET_URL.format(orgnr))
        regnskap = hent_json(REGNSKAP_URL.format(orgnr)) or []
        (RAA / f"{orgnr}.json").write_text(
            json.dumps({"enhet": enhet, "regnskap": regnskap}, ensure_ascii=False),
            encoding="utf-8",
        )
        time.sleep(0.2)  # vær snill mot Brreg
        print(f"  hentet {orgnr} {rad['gruppe']}", file=sys.stderr)


def tall(x):
    return None if x is None else float(x) / 1000  # til tusen kr


def les_raa(orgnr):
    fil = RAA / f"{orgnr}.json"
    if not fil.exists():
        return None, {}
    d = json.loads(fil.read_text(encoding="utf-8"))
    aar = {}
    for r in d.get("regnskap") or []:
        if r.get("regnskapstype") != "SELSKAP":
            continue
        periode = r.get("regnskapsperiode", {})
        tildato = periode.get("tilDato", "")
        fradato = periode.get("fraDato", "")
        if not tildato or not fradato:
            continue
        # Noen (f.eks. Accenture og CGI) har avvikende regnskapsår. Vi bruker
        # året regnskapet slutter i, men hopper over korte/lange overgangsår.
        maaneder = (int(tildato[:4]) - int(fradato[:4])) * 12 + int(tildato[5:7]) - int(fradato[5:7]) + 1
        if maaneder != 12:
            continue
        res = r.get("resultatregnskapResultat", {})
        drift = res.get("driftsresultat", {})
        aar[int(tildato[:4])] = {
            "inntekt": tall(drift.get("driftsinntekter", {}).get("sumDriftsinntekter")),
            "lonn": tall(drift.get("driftskostnad", {}).get("loennskostnad")),
            "driftskost": tall(drift.get("driftskostnad", {}).get("sumDriftskostnad")),
            "driftsresultat": tall(drift.get("driftsresultat")),
            "aarsresultat": tall(res.get("aarsresultat")),
            "egenkapital": tall(r.get("egenkapitalGjeld", {}).get("egenkapital", {}).get("sumEgenkapital")),
            "slutt_maaned": int(tildato[5:7]),
        }
    return d.get("enhet"), aar


def oppdater_ansatthistorikk(enheter):
    """Brreg viser bare dagens ansatttall, så vi lagrer et øyeblikksbilde
    ved hver kjøring og bygger historikk selv."""
    eksisterende = {}
    if ANSATT_HISTORIKK.exists():
        with open(ANSATT_HISTORIKK, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                eksisterende[r["orgnr"]] = r["ansatte"]  # siste verdi vinner
    nye = []
    i_dag = date.today().isoformat()
    for orgnr, enhet in enheter.items():
        ansatte = (enhet or {}).get("antallAnsatte")
        if ansatte is not None and eksisterende.get(orgnr) != str(ansatte):
            nye.append({"dato": i_dag, "orgnr": orgnr, "ansatte": ansatte})
    if nye:
        ny_fil = not ANSATT_HISTORIKK.exists()
        with open(ANSATT_HISTORIKK, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["dato", "orgnr", "ansatte"])
            if ny_fil:
                w.writeheader()
            w.writerows(nye)


def andel(a, b):
    return None if a is None or not b else round(a / b, 4)


def bygg_data(konfig):
    grupper = defaultdict(lambda: {"enheter": [], "aar": defaultdict(dict), "ansatte": 0})
    enheter = {}
    for rad in konfig:
        enhet, aar = les_raa(rad["orgnr"])
        enheter[rad["orgnr"]] = enhet
        g = grupper[rad["gruppe"]]
        g["kategori"] = rad["kategori"]
        g["sikker"] = g.get("sikker", True) and rad["sikker"] == "ja"
        g["enheter"].append({
            "orgnr": rad["orgnr"],
            "navn": (enhet or {}).get("navn"),
            "kommune": ((enhet or {}).get("forretningsadresse") or {}).get("kommune"),
            "ansatte": (enhet or {}).get("antallAnsatte"),
        })
        g["ansatte"] += (enhet or {}).get("antallAnsatte") or 0
        for y, verdier in aar.items():
            if verdier.get("slutt_maaned") != 12:
                g["avvikende_aar"] = True
            for k, v in verdier.items():
                if v is None or k == "slutt_maaned":
                    continue
                g["aar"][y][k] = g["aar"][y].get(k, 0) + v
            g["aar"][y].setdefault("antall_enheter", 0)
            g["aar"][y]["antall_enheter"] += 1

    bransje = {n: g for n, g in grupper.items() if g["kategori"] not in UTENFOR_BRANSJETALL}
    alle_aar = sorted({y for g in bransje.values() for y in g["aar"]})
    # siste år der de fleste selskapene har levert
    dekning = {y: sum(1 for g in bransje.values() if y in g["aar"]) for y in alle_aar}
    siste_aar = max(y for y in alle_aar if dekning[y] >= 0.8 * len(bransje))

    selskaper = []
    for navn, g in grupper.items():
        aar = {}
        for y in sorted(g["aar"]):
            v = g["aar"][y]
            forrige = g["aar"].get(y - 1, {})
            aar[str(y)] = {
                "inntekt": round(v.get("inntekt", 0)),
                "lonn": round(v.get("lonn", 0)),
                "driftsresultat": round(v.get("driftsresultat", 0)),
                "aarsresultat": round(v.get("aarsresultat", 0)),
                "margin": andel(v.get("driftsresultat"), v.get("inntekt")),
                "vekst": round(v["inntekt"] / forrige["inntekt"] - 1, 4)
                if forrige.get("inntekt") and v.get("inntekt") is not None else None,
            }
        s = g["aar"].get(siste_aar, {})
        lonn_per = s.get("lonn") / g["ansatte"] if g["ansatte"] and s.get("lonn") else None
        ansatt_ok = lonn_per is not None and LONN_PER_ANSATT_MIN <= lonn_per <= LONN_PER_ANSATT_MAKS
        selskaper.append({
            "navn": navn,
            "kategori": g["kategori"],
            "verifisert": g["sikker"],
            "ansatte": g["ansatte"],
            "enheter": g["enheter"],
            "aar": aar,
            "lonnskost_per_ansatt": round(lonn_per) if ansatt_ok else None,
            "inntekt_per_ansatt": round(s["inntekt"] / g["ansatte"]) if ansatt_ok and s.get("inntekt") else None,
            "ansatttall_usikkert": not ansatt_ok,
            "avvikende_regnskapsaar": g.get("avvikende_aar", False),
            "i_bransjetall": navn in bransje,
        })

    marked = {}
    for y in alle_aar:
        med = [g["aar"][y] for g in bransje.values() if y in g["aar"]]
        if len(med) < 0.8 * len(bransje):
            continue
        inntekt = sum(v.get("inntekt", 0) for v in med)
        drift = sum(v.get("driftsresultat", 0) for v in med)
        marked[str(y)] = {
            "antall_selskaper": len(med),
            "inntekt": round(inntekt),
            "lonn": round(sum(v.get("lonn", 0) for v in med)),
            "driftsresultat": round(drift),
            "margin": andel(drift, inntekt),
            "med_overskudd": sum(1 for v in med if v.get("driftsresultat", 0) > 0),
        }
    # vekst for selskaper som finnes begge år (like mot like)
    for y in list(marked):
        f = int(y) - 1
        felles = [g for g in bransje.values() if int(y) in g["aar"] and f in g["aar"]]
        a = sum(g["aar"][int(y)].get("inntekt", 0) for g in felles)
        b = sum(g["aar"][f].get("inntekt", 0) for g in felles)
        marked[y]["vekst_like_mot_like"] = round(a / b - 1, 4) if b else None

    sy = str(siste_aar)
    totalt = marked[sy]["inntekt"]
    for s in selskaper:
        inn = s["aar"].get(sy, {}).get("inntekt")
        s["markedsandel"] = round(inn / totalt, 4) if inn and s["i_bransjetall"] else None

    def topp(nokkel, n=10, kilde=None):
        kandidater = []
        for s in selskaper:
            if not s["i_bransjetall"] or not s["verifisert"]:
                continue
            if nokkel == "vekst" and (s["aar"].get(str(siste_aar - 1), {}).get("inntekt") or 0) < MIN_INNTEKT_VEKST:
                continue
            v = s["aar"].get(sy, {}).get(nokkel) if kilde == "aar" else s.get(nokkel)
            if v is not None:
                kandidater.append((v, s["navn"]))
        return [{"navn": n_, "verdi": v} for v, n_ in sorted(kandidater, reverse=True)[:n]]

    return {
        "oppdatert": date.today().isoformat(),
        "siste_aar": siste_aar,
        "kilde": "Brønnøysundregistrene (Enhetsregisteret og Regnskapsregisteret)",
        "merknader": [
            "Beløp er i tusen kroner.",
            "Lønnskostnad inkluderer arbeidsgiveravgift og pensjon, og er derfor høyere enn utbetalt lønn.",
            "Ansatttall er dagens tall fra Enhetsregisteret og brukes bare for siste regnskapsår.",
            "Selskaper i samme konsern er slått sammen.",
            "Noen selskaper har avvikende regnskapsår; året regnskapet slutter i brukes.",
            "Rådgivningshus som Deloitte, PwC, KPMG og EY driver også med revisjon og skatt. De vises i tabellen, men er holdt utenfor bransjetallene og topplistene.",
        ],
        "marked": marked,
        "topplister": {
            "inntekt": topp("inntekt", kilde="aar"),
            "margin": topp("margin", kilde="aar"),
            "vekst": topp("vekst", kilde="aar"),
            "lonnskost_per_ansatt": topp("lonnskost_per_ansatt"),
            "inntekt_per_ansatt": topp("inntekt_per_ansatt"),
        },
        "selskaper": sorted(selskaper, key=lambda s: -(s["aar"].get(sy, {}).get("inntekt") or 0)),
    }, enheter


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--offline", action="store_true", help="bruk lagrede rådata i data/raa/")
    args = p.parse_args()

    konfig = les_konfig()
    if not args.offline:
        print(f"Henter {len(konfig)} enheter fra Brreg ...", file=sys.stderr)
        hent_alt(konfig)

    data, enheter = bygg_data(konfig)
    if not args.offline:
        oppdater_ansatthistorikk(enheter)

    UT.parent.mkdir(parents=True, exist_ok=True)
    UT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    m = data["marked"][str(data["siste_aar"])]
    print(f"Skrev {UT.relative_to(ROT)}: {len(data['selskaper'])} selskaper, "
          f"{data['siste_aar']}: {m['inntekt'] / 1e6:.2f} mrd kr inntekt", file=sys.stderr)


if __name__ == "__main__":
    main()
