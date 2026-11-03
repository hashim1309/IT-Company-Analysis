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
UTENFOR_BRANSJETALL = {"Rådgivningshus", "Drift og outsourcing", "IT-bemanning",
                       "Programvare og lisens"}

# Vekst rangeres bare når fjorårets inntekt var minst 20 mill. kr (tusen kr),
# ellers dominerer nystartede og utskilte selskaper med flere hundre prosent vekst.
MIN_INNTEKT_VEKST = 20000

# Topplister tar bare med selskaper med minst 20 mill. kr i inntekt siste år,
# så svært små selskaper ikke havner øverst på grunn av tilfeldigheter.
MIN_INNTEKT_TOPP = 20000

STORRELSER = [(0, 50, "Under 50 ansatte"), (50, 200, "50–199 ansatte"),
              (200, 1000, "200–999 ansatte"), (1000, 10**9, "1000 ansatte eller flere")]
LONNSGRUPPER = [(0, 1000, "Under 1,0 mill. kr"), (1000, 1200, "1,0–1,2 mill. kr"),
                (1200, 1400, "1,2–1,4 mill. kr"), (1400, 10**9, "1,4 mill. kr eller mer")]


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
        y = int(tildato[:4])
        if y in aar and aar[y]["_id"] > (r.get("id") or 0):
            continue  # behold nyeste innsending hvis et år er levert flere ganger
        aar[y] = {
            "_id": r.get("id") or 0,
            "inntekt": tall(drift.get("driftsinntekter", {}).get("sumDriftsinntekter")),
            "lonn": tall(drift.get("driftskostnad", {}).get("loennskostnad")),
            "driftskost": tall(drift.get("driftskostnad", {}).get("sumDriftskostnad")),
            "driftsresultat": tall(drift.get("driftsresultat")),
            "aarsresultat": tall(res.get("aarsresultat")),
            "egenkapital": tall(r.get("egenkapitalGjeld", {}).get("egenkapital", {}).get("sumEgenkapital")),
            "eiendeler": tall(r.get("eiendeler", {}).get("sumEiendeler")),
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
    """a / b avrundet, eller None hvis det ikke kan regnes ut."""
    if a is None or b is None or b == 0:
        return None
    return round(a / b, 4)


def median(verdier):
    v = sorted(x for x in verdier if x is not None)
    if not v:
        return None
    m = len(v) // 2
    return round(v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2, 4)


FELT = ["inntekt", "lonn", "driftskost", "driftsresultat", "aarsresultat", "egenkapital", "eiendeler"]


def summer(poster, felt):
    """Summerer et felt; None hvis ingen av postene har verdien."""
    v = [p[felt] for p in poster if p.get(felt) is not None]
    return sum(v) if v else None


def bygg_data(konfig):
    # 1. Les hver juridisk enhet
    enheter, enhet_aar = {}, {}
    for rad in konfig:
        enhet, aar = les_raa(rad["orgnr"])
        enheter[rad["orgnr"]] = enhet
        enhet_aar[rad["orgnr"]] = aar

    # 2. Grupper (konsern slås sammen)
    grupper = {}
    for rad in konfig:
        g = grupper.setdefault(rad["gruppe"], {"navn": rad["gruppe"], "kategori": rad["kategori"],
                                               "sikker": True, "orgnr": []})
        g["sikker"] = g["sikker"] and rad["sikker"] == "ja"
        g["orgnr"].append(rad["orgnr"])
        # Merknaden kan starte med navnet på den juridiske enheten (f.eks.
        # «Knowit Insight»); det tas bort, resten vises som forklaring.
        tekst = (rad.get("merknad") or "").strip()
        forste = rad["gruppe"].split()[0].lower()
        for skille in (". ", "; "):
            if tekst.lower().startswith(forste) and skille in tekst:
                tekst = tekst.split(skille, 1)[1]
        if tekst.lower().startswith(forste):
            tekst = ""
        if tekst and tekst not in g.setdefault("merknader", []):
            g["merknader"].append(tekst)
    for g in grupper.values():
        g["i_bransjetall"] = g["kategori"] not in UTENFOR_BRANSJETALL

    bransje = [g for g in grupper.values() if g["i_bransjetall"]]

    # 3. Siste regnskapsår: siste år der minst 80 % av bransjeselskapene har levert
    def har_aar(g, y):
        return any(y in enhet_aar[o] for o in g["orgnr"])

    alle_aar = sorted({y for g in bransje for o in g["orgnr"] for y in enhet_aar[o]})
    siste_aar = max(y for y in alle_aar if sum(har_aar(g, y) for g in bransje) >= 0.8 * len(bransje))
    aarene = [y for y in alle_aar if sum(har_aar(g, y) for g in bransje) >= 0.8 * len(bransje)]

    # 4. Tall per gruppe og år
    selskaper = []
    for g in grupper.values():
        aar = {}
        for y in aarene:
            poster = [enhet_aar[o][y] for o in g["orgnr"] if y in enhet_aar[o]]
            if not poster:
                continue
            sett = sorted(o for o in g["orgnr"] if y in enhet_aar[o])
            v = {f: summer(poster, f) for f in FELT}
            forrige_sett = sorted(o for o in g["orgnr"] if (y - 1) in enhet_aar[o])
            forrige_inntekt = summer([enhet_aar[o][y - 1] for o in forrige_sett], "inntekt") if forrige_sett else None
            # Vekst regnes bare når de samme juridiske enhetene har regnskap begge år
            sammenlignbar = forrige_sett == sett and forrige_inntekt
            aar[str(y)] = {
                **{f: (round(v[f]) if v[f] is not None else None) for f in FELT if f != "driftskost"},
                "margin": andel(v["driftsresultat"], v["inntekt"]),
                "vekst": round(v["inntekt"] / forrige_inntekt - 1, 4) if sammenlignbar and v["inntekt"] is not None else None,
                "lonnsandel": andel(v["lonn"], v["inntekt"]),
                "ek_andel": andel(v["egenkapital"], v["eiendeler"]),
                "orgnr": sett,
            }

        siste = aar.get(str(siste_aar))
        # Ansatte telles bare for enhetene som har regnskap for siste år
        sett = siste["orgnr"] if siste else []
        ansatt_liste = [(enheter[o] or {}).get("antallAnsatte") for o in sett]
        ansatte = sum(ansatt_liste) if sett and all(a is not None for a in ansatt_liste) else None
        lonn_per = siste["lonn"] / ansatte if siste and ansatte and siste["lonn"] else None
        ansatt_ok = lonn_per is not None and LONN_PER_ANSATT_MIN <= lonn_per <= LONN_PER_ANSATT_MAKS

        info = []
        for o in g["orgnr"]:
            e = enheter[o] or {}
            info.append({
                "orgnr": o,
                "navn": e.get("navn"),
                "kommune": (e.get("forretningsadresse") or {}).get("kommune"),
                "ansatte": e.get("antallAnsatte"),
                "stiftet": e.get("stiftelsesdato") or e.get("registreringsdatoEnhetsregisteret"),
                "naeringskode": (e.get("naeringskode1") or {}).get("kode"),
                "regnskapsaar": sorted(str(y) for y in enhet_aar[o]),
            })
        storste = max(info, key=lambda e: e["ansatte"] or 0)
        avvikende = any(p.get("slutt_maaned") != 12 for o in g["orgnr"] for p in enhet_aar[o].values())

        selskaper.append({
            "navn": g["navn"],
            "kategori": g["kategori"],
            "verifisert": g["sikker"],
            "merknad": "; ".join(g.get("merknader", [])),
            "i_bransjetall": g["i_bransjetall"],
            "ansatte": ansatte if ansatte is not None else sum(e["ansatte"] or 0 for e in info),
            "kommune": storste["kommune"],
            "enheter": info,
            "aar": aar,
            "lonnskost_per_ansatt": round(lonn_per) if ansatt_ok else None,
            "inntekt_per_ansatt": round(siste["inntekt"] / ansatte) if ansatt_ok and siste["inntekt"] else None,
            "resultat_per_ansatt": round(siste["driftsresultat"] / ansatte)
            if ansatt_ok and siste["driftsresultat"] is not None else None,
            "ansatttall_usikkert": not ansatt_ok,
            "avvikende_regnskapsaar": avvikende,
            "vekst_ikke_sammenlignbar": bool(siste) and siste["vekst"] is None
            and str(siste_aar - 1) in aar,
        })

    sy = str(siste_aar)
    i_bransje = [s for s in selskaper if s["i_bransjetall"]]

    # 5. Bransjetall per år. Vekst like mot like regnes på juridiske enheter som
    #    har regnskap begge år, så nye eller nedlagte selskaper ikke gir falsk vekst.
    bransje_orgnr = [o for g in bransje for o in g["orgnr"]]

    def like_mot_like(orgnr, y):
        felles = [o for o in orgnr if y in enhet_aar[o] and (y - 1) in enhet_aar[o]
                  and enhet_aar[o][y]["inntekt"] is not None and enhet_aar[o][y - 1]["inntekt"] is not None]
        a = sum(enhet_aar[o][y]["inntekt"] for o in felles)
        b = sum(enhet_aar[o][y - 1]["inntekt"] for o in felles)
        return (round(a / b - 1, 4) if b else None), len(felles)

    def nokkeltall(rader, y=sy):
        poster = [s["aar"][y] for s in rader if y in s["aar"]]
        inntekt = sum(p["inntekt"] or 0 for p in poster)
        drift = sum(p["driftsresultat"] or 0 for p in poster)
        lonn = sum(p["lonn"] or 0 for p in poster)
        orgnr = [o for s in rader for o in (s["aar"][y]["orgnr"] if y in s["aar"] else [])]
        vekst, n_vekst = like_mot_like(orgnr, int(y))
        return {
            "antall_selskaper": len(poster),
            "inntekt": inntekt,
            "lonn": lonn,
            "driftsresultat": drift,
            "margin": andel(drift, inntekt),
            "lonnsandel": andel(lonn, inntekt),
            "med_overskudd": sum(1 for p in poster if (p["driftsresultat"] or 0) > 0),
            "median_margin": median(p["margin"] for p in poster),
            "vekst_like_mot_like": vekst,
            "antall_i_vekst": n_vekst,
        }

    marked = {}
    for y in aarene:
        m = nokkeltall(i_bransje, str(y))
        if str(y - 1) not in [str(a) for a in aarene]:
            m["vekst_like_mot_like"] = None
            m["antall_i_vekst"] = 0
        marked[str(y)] = m

    totalt = marked[sy]["inntekt"]
    for s in selskaper:
        inn = s["aar"].get(sy, {}).get("inntekt")
        s["markedsandel"] = round(inn / totalt, 4) if inn and s["i_bransjetall"] else None

    # 6. Analyser for siste år
    med_siste = [s for s in i_bransje if sy in s["aar"]]

    def grupper_etter(nokkel_fn, rekkefolge=None):
        bøtter = defaultdict(list)
        for s in med_siste:
            k = nokkel_fn(s)
            if k is not None:
                bøtter[k].append(s)
        navn = rekkefolge or sorted(bøtter, key=lambda k: -sum(x["aar"][sy]["inntekt"] or 0 for x in bøtter[k]))
        ut = []
        for k in navn:
            if k not in bøtter:
                continue
            m = nokkeltall(bøtter[k])
            m["navn"] = k
            m["andel_av_inntekt"] = andel(m["inntekt"], totalt)
            ut.append(m)
        return ut

    def storrelse(s):
        if s["ansatttall_usikkert"]:
            return None
        for lo, hi, n in STORRELSER:
            if lo <= s["ansatte"] < hi:
                return n

    def lonnsgruppe(s):
        if not s["verifisert"] or s["lonnskost_per_ansatt"] is None:
            return None
        for lo, hi, n in LONNSGRUPPER:
            if lo <= s["lonnskost_per_ansatt"] < hi:
                return n

    # Kommuner med færre enn tre selskaper slås sammen, så ett selskap ikke
    # avgjør tallet for en hel kommune.
    teller = defaultdict(int)
    for s in med_siste:
        teller[(s["kommune"] or "Ukjent").title()] += 1
    kommune_gruppe = {s["navn"]: ((s["kommune"] or "Ukjent").title()
                                  if teller[(s["kommune"] or "Ukjent").title()] >= 3 else "Andre kommuner")
                      for s in med_siste}

    inntekter = sorted((s["aar"][sy]["inntekt"] or 0 for s in med_siste), reverse=True)
    ek = [s["aar"][sy]["ek_andel"] for s in med_siste]

    analyser = {
        "segmenter": grupper_etter(lambda s: s["kategori"]),
        "storrelse": grupper_etter(storrelse, [n for _, _, n in STORRELSER]),
        "lonn_mot_margin": grupper_etter(lonnsgruppe, [n for _, _, n in LONNSGRUPPER]),
        "geografi": grupper_etter(lambda s: kommune_gruppe[s["navn"]]),
        "konsentrasjon": {f"topp{n}": andel(sum(inntekter[:n]), totalt) for n in (1, 3, 5, 10)},
        "soliditet": {
            "median_ek_andel": median(ek),
            "negativ_egenkapital": sorted(s["navn"] for s in med_siste
                                          if (s["aar"][sy]["egenkapital"] or 0) < 0),
            "antall": sum(1 for x in ek if x is not None),
        },
        "underskudd": sorted(
            ({"navn": s["navn"], "driftsresultat": s["aar"][sy]["driftsresultat"],
              "margin": s["aar"][sy]["margin"], "verifisert": s["verifisert"]}
             for s in med_siste if (s["aar"][sy]["driftsresultat"] or 0) < 0),
            key=lambda x: x["driftsresultat"]),
    }

    # 7. Topplister: bare bekreftede selskaper i bransjetallene, med minst
    #    20 mill. kr i inntekt (og året før for vekst).
    def topp(verdi_fn, n=10, minst_forrige=False):
        kandidater = []
        for s in med_siste:
            if not s["verifisert"] or (s["aar"][sy]["inntekt"] or 0) < MIN_INNTEKT_TOPP:
                continue
            if minst_forrige and (s["aar"].get(str(siste_aar - 1), {}).get("inntekt") or 0) < MIN_INNTEKT_VEKST:
                continue
            v = verdi_fn(s)
            if v is not None:
                kandidater.append((v, s["navn"]))
        kandidater.sort(key=lambda x: (-x[0], x[1]))
        return [{"navn": n_, "verdi": v} for v, n_ in kandidater[:n]]

    topplister = {
        "margin": topp(lambda s: s["aar"][sy]["margin"]),
        "vekst": topp(lambda s: s["aar"][sy]["vekst"], minst_forrige=True),
        "lonnskost_per_ansatt": topp(lambda s: s["lonnskost_per_ansatt"]),
        "inntekt_per_ansatt": topp(lambda s: s["inntekt_per_ansatt"]),
        "resultat_per_ansatt": topp(lambda s: s["resultat_per_ansatt"]),
        "lonnsandel": topp(lambda s: s["aar"][sy]["lonnsandel"]),
        "ek_andel": topp(lambda s: s["aar"][sy]["ek_andel"]),
        "inntekt": topp(lambda s: s["aar"][sy]["inntekt"]),
    }

    return {
        "oppdatert": date.today().isoformat(),
        "siste_aar": siste_aar,
        "kilde": "Brønnøysundregistrene (Enhetsregisteret og Regnskapsregisteret)",
        "merknader": [
            "Beløp er i tusen kroner.",
            "Driftsinntekt, lønnskostnad og driftsresultat er hentet direkte fra selskapenes årsregnskap. Margin er driftsresultat delt på driftsinntekt.",
            "Lønnskostnad inkluderer arbeidsgiveravgift og pensjon, og er derfor høyere enn utbetalt lønn.",
            "Ansatttall er dagens tall fra Enhetsregisteret, ikke tallet ved årsslutt. Tall per ansatt er derfor omtrentlige.",
            "Selskaper i samme konsern er slått sammen. Vekst for ett selskap vises bare når de samme juridiske enhetene har regnskap begge år.",
            "Vekst like mot like for bransjen regnes bare på selskaper som har regnskap begge år.",
            "Noen selskaper har avvikende regnskapsår; året regnskapet slutter i brukes.",
            "Rådgivningshus (som Deloitte, PwC, KPMG og EY), drifts- og outsourcingselskaper, IT-bemanning, programvare- og lisensselskaper vises i tabellen, men er holdt utenfor bransjetallene, analysene og topplistene.",
            "Et selskap regnes som IT-konsulentselskap når formålet det har oppgitt i Brreg er konsulentvirksomhet, og over halvparten av inntekten går til lønn. Kjente konsulenthus er plassert direkte.",
            "Topplistene tar bare med selskaper med minst 20 mill. kr i inntekt.",
        ],
        "marked": marked,
        "analyser": analyser,
        "topplister": topplister,
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
