# IT-konsulentbransjen i tall

Data fra Brønnøysundregistrene om norske IT-konsulentselskaper.

## Struktur

- `config/selskaper.csv`: håndplukket liste over selskaper. Selskaper i samme konsern har samme `gruppe` og slås sammen. `sikker=nei` betyr at det bør sjekkes om selskapet hører hjemme her.
- `scripts/hent_data.py`: henter fra Brreg og skriver `site/data.json`. Bruker bare Pythons standardbibliotek.
- `site/data.json`: ferdige tall som nettsiden leser.
- `data/raa/`: rådata fra siste henting (én fil per org.nr.).
- `data/ansatte_historikk.csv`: bygges opp over tid, fordi Brreg bare viser dagens ansatttall.
- `.github/workflows/oppdater-og-publiser.yml`: henter nye tall hver mandag, lagrer dem og publiserer siden på GitHub Pages. Siden publiseres også ved hver endring på `main`.

## Kjøre lokalt

    python scripts/hent_data.py            # henter fra Brreg
    python scripts/hent_data.py --offline  # regner på nytt fra data/raa/

## Legge til et selskap

Legg til en linje i `config/selskaper.csv` med org.nr. (finn det på brreg.no) og kjør skriptet.
