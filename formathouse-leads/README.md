# The Format House — leadgeneratie & outreach

Python-tool die potentiële klanten zoekt, verrijkt, scoort en benadert voor
The Format House (short-form video voor Reels, TikTok, Shorts).

> **Status:** Fase 1 (Discovery) is gebouwd en getest. Fase 2–6 volgen na goedkeuring.

## Spelregels die in de code zitten
- Alleen officiële API's en publieke bedrijfswebsites. **Geen** scraping van Instagram, TikTok of LinkedIn.
- Elk veld is `feit` (met bron-URL en datum), `inschatting` (met motivatie) of `onbekend`.
  De database weigert een `feit` zonder bron en een `inschatting` zonder motivatie,
  en een feit wordt nooit overschreven door iets zwakkers.
- Elke externe call komt in de tabel `api_calls` (bron, tijdstip, statuscode, resultaat). API-sleutels worden nooit gelogd.
- Rate limits worden gerespecteerd: bij 429/503 wacht de tool één keer zoals de server vraagt en stopt daarna. De voortgang blijft bewaard.

## Installatie
```bash
cd formathouse-leads
python3 -m venv .venv && source .venv/bin/activate      # Python 3.11+
pip install -r requirements.txt
cp .env.example .env                                    # vul daarna de sleutels in
python -m formathouse init
```
(Met `pip install -e .` kun je ook `fh …` gebruiken in plaats van `python -m formathouse …`.)

### Google Places API (Fase 1)
1. Ga naar <https://console.cloud.google.com/>, maak een project aan (bv. `formathouse-leads`) en koppel een factureringsaccount.
2. Ga naar **APIs & Services → Library**, zoek **"Places API (New)"** en klik op *Enable*.
3. Ga naar **APIs & Services → Credentials → Create credentials → API key**.
4. Beperk de sleutel via *Edit API key → API restrictions → Places API (New)*.
5. Zet de sleutel in `.env` als `GOOGLE_PLACES_API_KEY=…`.

**Kosten:** de opgevraagde velden (website, rating, reviews, prijsniveau, telefoon)
vallen onder de *Text Search Enterprise*-tariefcategorie. De pilot gebruikt maar een
handvol requests. Check het actuele gratis maandvolume en de prijs op de
prijspagina van Google Maps Platform en stel in Cloud Console een **budgetalert** in.

### Volgende fases (setup volgt per fase)
- **Meta-app + Instagram Graph API** (Fase 3): Instagram-bedrijfsaccount, gekoppelde Facebook-pagina, Meta-app, Business Discovery en Ad Library. Ik begeleid je hier stap voor stap bij.
- **KvK API** (Fase 2, alleen bij `REGIO=be_nl`), **Claude API** (Fase 4), **SMTP/Gmail** op een apart outreach-domein (Fase 5).

## Commando's (Fase 1)
| Commando | Wat |
|---|---|
| `python -m formathouse discover run -g horeca -c Antwerpen -n 30` | Pilot: 30 horecaleads in Antwerpen en omgeving |
| `python -m formathouse discover run` | Volledige run, alle groepen, steden in prioriteitsvolgorde, tot de quota bereikt zijn (120/105/75) |
| `python -m formathouse discover run -g overig -n 20` | Overige sectoren, alleen op aanvraag |
| `python -m formathouse discover status` | Voortgang per groep t.o.v. de quota |
| `python -m formathouse export [--include-excluded]` | Schrijft `output/leads.csv` + `output/leads.xlsx` |
| `python -m formathouse log --n 50` | Laatste externe calls |

Runs zijn **hervatbaar**: per *groep × zoekterm × stad* wordt bijgehouden hoeveel
pagina's al opgehaald zijn. Opnieuw starten gaat dus gewoon verder.

## Hoe Discovery werkt
1. **Werkgebied** via `REGIO` in `.env`:
   - `vlaanderen` (standaard): Antwerpen, Gent, Mechelen, Leuven, Hasselt, Brugge;
   - `belgie`: + Brussel;
   - `be_nl`: + Breda, Eindhoven, Rotterdam, Utrecht, Amsterdam.

   Per stad wordt elke zoekterm van de groep om de beurt bevraagd, met een cirkel van 15 km
   ("+ omgeving") en max. 3 pagina's van 20 resultaten.
2. **Voorfilter** (instelbaar in `formathouse/briefing.py`). Afgewezen kandidaten
   blijven met hun reden in de database, zodat ze niet opnieuw beoordeeld worden:
   buiten werkgebied (getoetst op postcode; Vlaanderen = 1500–3999 en 8000–9999, dus
   geen Brussel of Wallonië, ook niet aan de rand van een zoekcirkel) · niet operationeel · grote keten · geen website in het Google-profiel ·
   te weinig reviews (horeca < 100, fitness/beauty < 25, lifestyle < 15).
3. **Ontdubbeling** op Google place_id, eigen domein (social- en boekingsplatformen tellen niet mee),
   KBO-/KvK-nummer (vanaf Fase 2) en genormaliseerde naam + stad.
4. Opgeslagen als `feit` met de Google Maps-URL als bron: naam, adres, stad, website, Google-categorie,
   rating, aantal reviews, prijsniveau en het algemene telefoonnummer.
   De `sector` is een `inschatting` (afgeleid uit de zoekterm en de Google-categorie).

## Export
`leads.xlsx` bevat de tabbladen **Alle leads**, **Bronnen** en **Legenda**.
De cellen zijn gekleurd: groen = feit, geel = inschatting, grijs = onbekend.
De celopmerkingen tonen bron en motivatie. Alle kolommen van het eindoverzicht staan er al in;
wat nog niet verzameld is, staat op `onbekend`. Top 50, E-mail outreach en
Instagram-wachtrij komen erbij in de volgende fases.

## Tests
```bash
python -m pytest -q
```
De tests gebruiken een nagebootste Places API met fictieve bedrijven (er gebeuren geen echte calls).
