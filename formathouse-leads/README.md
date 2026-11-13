# The Format House — leadgeneratie & outreach

Python-tool die potentiële klanten zoekt, verrijkt, scoort en benadert voor
The Format House (short-form video voor Reels, TikTok, Shorts).

## Snel starten (zonder terminal)
Dubbelklik op **`Start Format House.command`** (Mac) of **`Start Format House.bat`** (Windows).
De eerste keer installeert de app zichzelf. Daarna opent hij in je browser op
`http://127.0.0.1:8765` (alleen op je eigen computer). Zie ook `LEES MIJ.txt`.

De app heeft tabbladen voor alle stappen: Start (zoeken, verwerken, Excel), Leads, E-mail,
Instagram en Instellingen (sleutels invullen). Zolang de app openstaat, vertrekken
goedgekeurde mails automatisch op hun geplande tijdstip.

## Updates en je data
- **Data:** leads, instellingen (`.env`) en exports staan in `Documenten/Format House Leads`, los van de app. Een update raakt ze nooit.
- **Updates:** bij elke start haalt de app de nieuwste versie van GitHub (`formathouse/updater.py`). Ook de knop "Bijwerken" doet dat, waarna de app zichzelf herstart.
- **Privé-repository:** vul één keer een GitHub-token met alleen leesrechten in bij Instellingen. Zie `LEES MIJ.txt`.

## Sectoren en filters
- **Sectoren:** horeca, fitness/wellness/beauty, lifestyle, immo, verzekeringsmakelaars, interieur, bouw, esthetiek, padel/golf, events, food, tuin, auto en overig (op aanvraag). Zie `formathouse/briefing.py`.
- **Altijd geweerd:**
  - grote ketens;
  - snackzaken en kroegen (horeca);
  - sportclubs zoals voetbal en tennis (fitness);
  - bankkantoren (verzekeringen);
  - zaken zonder eigen website (alleen Facebook of een boekingsplatform telt niet).
- **Geen social media:** vindt de app na het websitebezoek geen enkele link naar Instagram, TikTok, Facebook of LinkedIn, dan wordt de lead uitgesloten.

## Gratis online (via een link, met wachtwoord)
Zie **`ONLINE ZETTEN.txt`**. In het kort:
- **Render** (gratis) draait de app.
- **Supabase** (gratis) bewaart de leads.
- **Brevo** (gratis) verstuurt de mails over HTTPS. Render blokkeert SMTP op gratis servers.
- Een **GitHub Actions**-taak (`.github/workflows/formathouse-mail-tick.yml`) wekt de app op werkdagen elk kwartier en laat de geplande mails vertrekken.

Met `DATABASE_URL` ingesteld gebruikt de app Postgres in plaats van `leads.db`, en bewaart hij de instellingen in de database. Met `APP_PASSWORD` ingesteld moet je eerst inloggen.
Test tegen Postgres met `FH_TEST_PG=postgresql://… python -m pytest`.

Alles hieronder (de commando's met `fh`) is voor wie liever met de terminal werkt.

**Alle zes fases zijn gebouwd en werken zonder API-sleutels.** Ontbreekt een sleutel,
dan valt de tool terug op een alternatief of blijft het veld op `onbekend` staan:

| Fase | Met sleutel | Zonder sleutel |
|---|---|---|
| 1 Discovery | Google Places (met reviews/rating) | **OpenStreetMap** (gratis; reviews/rating `onbekend`) |
| 2 Verrijking | — (geen sleutel nodig) | bedrijfswebsites + KBO Open Data |
| 3 Social | Instagram Graph API + Meta Ad Library | IG-handle uit de website; cijfers `onbekend` + link voor handmatige Ad Library-check |
| 4 Scoring | Claude API | transparante **regelgebaseerde** score |
| 5 Outreach | Claude schrijft de teksten; SMTP verstuurt | vaste sjabloonteksten; alleen concepten |
| 6 Export | — | — |

## Spelregels die in de code zitten
- Alleen officiële API's en publieke bedrijfswebsites. **Geen** scraping van Instagram, TikTok of LinkedIn en geen automatische logins.
- Websites: robots.txt (incl. Crawl-delay) wordt gerespecteerd, met een vaste pauze (standaard 3 s) en max. 4 pagina's per site.
- AVG: alleen **algemene** adressen (info@, hello@, contact@ …, vaste lijst in `enrich.py`). Persoonlijke adressen worden nooit opgeslagen. Van de beslisser bewaren we alleen de functie.
- Elk veld is `feit` (met bron-URL en datum), `inschatting` (met motivatie) of `onbekend`. De database weigert een `feit` zonder bron en een `inschatting` zonder motivatie. Een feit wordt nooit overschreven door iets zwakkers.
- Elke externe call komt in de tabel `api_calls` (`fh log`). API-sleutels worden nooit gelogd.
- Rate limits worden gerespecteerd: bij 429/503 wacht de tool één keer en stopt daarna. De voortgang blijft bewaard.
- E-mails: niets wordt verstuurd zonder `--confirm` per batch. Hoogstens 30 per dag, willekeurig verspreid over ma–vr 9–17u. Een suppressielijst wordt nooit gemaild. Opvolgmails stoppen bij een antwoord.
- Instagram: de tool verstuurt **nooit** zelf DM's. Hij maakt alleen een wachtrij van max. 15 nieuwe items per dag.

## Installatie
Je hebt Python 3.11 of nieuwer nodig ([python.org](https://www.python.org/downloads/)).
```bash
cd formathouse-leads
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e .                     # installeert ook het commando `fh`
cp .env.example .env                 # vul in wat je hebt (alles optioneel)
fh init                              # maakt de database en toont welke sleutels ontbreken
```
Zonder `pip install -e .` werkt `python -m formathouse …` op dezelfde manier.

## Sleutels instellen (wanneer je ze hebt)

### Google Places API (Fase 1, optioneel)
1. Ga naar <https://console.cloud.google.com/>, maak een project aan en koppel een factureringsaccount.
2. Ga naar **APIs & Services → Library**, zoek **"Places API (New)"** en klik op *Enable*.
3. Ga naar **Credentials → Create credentials → API key**. Beperk de sleutel tot *Places API (New)*.
4. Zet de sleutel in `.env` als `GOOGLE_PLACES_API_KEY`. Stel in Billing ook een **budgetalert** in (bv. €10).

Je krijgt elke maand 1.000 gratis zoekopdrachten in deze tariefcategorie. Een volledige run voor Vlaanderen gebruikt er ruim minder.

### Meta: Instagram Graph API + Ad Library (Fase 3, optioneel)
1. Zet het Instagram-account van The Format House om naar een **professioneel account** (Bedrijf of Creator) en koppel het aan een **Facebook-pagina**.
2. Maak op <https://developers.facebook.com/> een app aan (type *Business*). Voeg het product **Instagram** toe ("API setup with Facebook login").
3. Genereer in de **Graph API Explorer** een gebruikerstoken met de rechten `instagram_basic`, `pages_show_list`, `pages_read_engagement` en `business_management`. Zet het om naar een token met lange levensduur.
4. Vraag `GET /me/accounts?fields=instagram_business_account` op. Het `id` dat je terugkrijgt, is `META_IG_BUSINESS_ACCOUNT_ID`.
5. Voor de **Ad Library API**: bevestig je identiteit op <https://www.facebook.com/ID> en aanvaard de voorwaarden op <https://www.facebook.com/ads/library/api>.
6. Vul `META_ACCESS_TOKEN` en `META_IG_BUSINESS_ACCOUNT_ID` in `.env` in.

Business Discovery werkt alleen voor andere **zakelijke** Instagram-accounts. Geeft de API niets terug, dan blijft het veld `onbekend`.

### KBO Open Data (Fase 2, optioneel maar gratis)
1. Registreer je via <https://economie.fgov.be/nl/themas/ondernemingen/kruispuntbank-van/diensten-voor-iedereen/hergebruik-van-publieke/kruispuntbank-van-0> (KBO Open Data).
2. Download de volledige extractie (zip) en pak hem uit.
3. Voer `fh enrich kbo-import /pad/naar/map` uit. Daarna krijgen bedrijven met een ondernemingsnummer op hun website automatisch rechtsvorm, status en startdatum als `feit`.

Omzet en personeelsaantal (jaarrekening, Nationale Bank/CBSO) haalt de tool niet automatisch op. Die blijven `onbekend` tot iemand ze geverifieerd invult.

### Claude API (Fase 4–5, optioneel)
Maak een sleutel aan op <https://platform.claude.com/> en zet hem in `.env` als `ANTHROPIC_API_KEY`.
Standaard wordt `claude-opus-5-5` gebruikt. Met `CLAUDE_MODEL=claude-sonnet-5-5` is het ongeveer de helft goedkoper.
Wijst het model een verzoek af, dan schakelt de API automatisch over naar een ander model (`fallbacks: "default"`).

### E-mail (Fase 5)
Gebruik een **apart outreach-domein** (bv. `theformathouse-studio.be`) met een eigen mailbox, en stel SPF, DKIM en DMARC in.
Zo blijft je hoofddomein buiten schot als een mail als spam gemarkeerd wordt.
Vul `SMTP_*`, `OUTREACH_FROM_EMAIL` en `OUTREACH_SIGNATURE` in.
Bij Gmail/Google Workspace: host `smtp.gmail.com`, poort 587, met een app-wachtwoord.

## Commando's
| Commando | Wat |
|---|---|
| `fh init` | database aanmaken, ontbrekende sleutels tonen |
| `fh discover run -g horeca -c Antwerpen -n 30` | pilot: 30 horecaleads Antwerpen (OSM als er geen Google-sleutel is) |
| `fh discover run [--bron osm\|google]` | volledige run tot de quota bereikt zijn (120/105/75) |
| `fh discover status` | voortgang t.o.v. de quota |
| `fh enrich run [-n 30]` | websites + KBO (hervatbaar) |
| `fh enrich test-url https://…` | één website analyseren zonder iets op te slaan |
| `fh enrich kbo-import <map>` | KBO Open Data inladen |
| `fh social run` | Instagram-cijfers + advertenties |
| `fh score run [--redo]` / `fh score top` | scoren, rangschikken en de top 50 markeren |
| `fh pipeline` | enrich + social + score in één keer |
| `fh outreach draft --n 25` | batch concept-mails → `output/outreach_batch_<id>.xlsx` |
| `fh outreach send --batch <id>` | dry-run: toont wat er zou gebeuren |
| `fh outreach send --batch <id> --confirm` | goedkeuren, inplannen en versturen wat aan de beurt is |
| `fh outreach tick` | geplande mails versturen (laat dit elke 10 min lopen, zie hieronder) |
| `fh outreach replied <e-mail>` | antwoord ontvangen → opvolging stopt |
| `fh outreach unsubscribe <e-mail\|@domein>` | op de suppressielijst zetten |
| `fh outreach status` | overzicht per batch |
| `fh ig_queue build` | max. 15 nieuwe Instagram-leads per dag in de wachtrij |
| `fh ig_queue today` | lijst van vandaag + `output/ig_queue_today.csv` |
| `fh ig_queue mark <handle> <status>` | `te_versturen` / `verstuurd` / `antwoord` / `geen_interesse` |
| `fh ig_queue sync` | statussen uit `output/ig_queue_today.csv` overnemen |
| `fh export [--include-excluded]` | `output/leads.xlsx` + `output/leads.csv` |
| `fh log --n 50` | laatste externe calls |

## Eerste keer: alles zonder sleutels
```bash
fh discover run -g horeca -c Antwerpen -n 30     # pilot via OpenStreetMap
fh pipeline                                      # websites, socials, scoring
fh export                                        # → output/leads.xlsx
```
Bekijk `output/leads.xlsx`. Daarna volgt de volledige run met `fh discover run`.

## Dagelijkse workflow
1. **'s Ochtends:**
   ```bash
   fh pipeline                    # nieuwe leads verwerken
   fh outreach draft --n 25       # concept-mails
   fh ig_queue build              # Instagram-wachtrij aanvullen
   fh ig_queue today              # lijst van vandaag
   ```
2. **E-mail:** bekijk `output/outreach_batch_<id>.xlsx`. Is alles in orde, voer dan `fh outreach send --batch <id> --confirm` uit.
   Laat daarna `fh outreach tick` elke 10 minuten lopen. Op Mac/Linux kan dat met `crontab -e`:
   ```
   */10 * * * 1-5 cd /pad/naar/formathouse-leads && .venv/bin/fh outreach tick >> output/tick.log 2>&1
   ```
   Op Windows gebruik je Taakplanner.
3. **Antwoorden:** `fh outreach replied info@bedrijf.be`. Uitschrijvingen: `fh outreach unsubscribe info@bedrijf.be`.
4. **Instagram:** start in de Claude-desktopapp een sessie met computer use en geef deze opdracht:

   > Open `formathouse-leads/output/ig_queue_today.csv`. Toon me de lijst; ik keur hem één keer goed.
   > Daarna, per rij met status `te_versturen`: open de profiel-URL in Instagram, controleer dat het
   > juiste bedrijfsaccount open staat, stuur de DM-tekst exact zoals hij in het bestand staat, en zet
   > de status in het bestand op `verstuurd`. Twijfel je over het account, sla de rij dan over.
   > Na afloop: voer `fh ig_queue sync` uit.

   Elke statuswijziging komt in `output/ig_verstuurd_log.csv` en in de database. Een handle die ooit in de wachtrij stond, wordt nooit opnieuw toegevoegd. Een DM die op `verstuurd` staat, kan niet terug naar `te_versturen`.
5. **Overzicht:** `fh export` maakt `output/leads.xlsx` met de tabbladen Alle leads, Top 50, E-mail outreach, Instagram-wachtrij, Bronnen en Legenda.

## Hoe de stappen werken
- **Discovery:** zoekt in Antwerpen, Gent, Mechelen, Leuven, Hasselt en Brugge, telkens met een cirkel van 15 km.
  De voorfilter wijst af:
  - zaken buiten het werkgebied (getoetst op postcode);
  - zaken die niet operationeel zijn;
  - grote ketens;
  - zaken zonder website;
  - bij Google: zaken onder een minimum aantal reviews (horeca 100, fitness/beauty 25, lifestyle 15).

  Afgewezen kandidaten blijven met hun reden in de database. Dubbels worden samengevoegd op bron-ID, eigen domein en naam + stad.
- **Verrijking:** bezoekt de homepage plus de contact-, over-ons- en vacaturepagina. Daar haalt hij uit:
  - links naar Instagram, TikTok, LinkedIn en Facebook (`feit`);
  - algemene e-mailadressen (`feit`);
  - het ondernemingsnummer, gecontroleerd met het controlegetal (`feit`);
  - het aantal vestigingen, op basis van verschillende adressen (`inschatting`);
  - vacatures en lanceringen of events van dit of volgend jaar (`inschatting`).
- **Social:** zoekt per account de volgers, het aantal Reels en posts van de laatste 30 dagen, de postfrequentie en onregelmatigheid op.
  Het **koopsignaal** is: actief in de laatste 30 dagen met 0–8 Reels, of onregelmatig posten.
  Advertenties worden alleen als `feit` genoteerd bij een eenduidige paginamatch. In alle andere gevallen staan ze op `onbekend`.
- **Scoring:** zes criteria (25/20/20/15/10/10). Het totaal en de prioriteit (≥ 75 hoog, 55–74 interessant, < 55 laag) worden in de code berekend.
  Claude krijgt alleen de verzamelde velden met hun status en mag niets verzinnen.
  De rangschikking gebeurt op leadscore, dan maandelijks potentieel, dan contentbehoefte. De beste 50 krijgen de markering `Top 50`.
- **Outreach:** leads met een algemeen e-mailadres gaan naar e-mail, leads met alleen Instagram naar de Instagram-wachtrij. Een bedrijf krijgt nooit beide.
  Elke mail eindigt met een vaste voettekst die de code toevoegt: wie we zijn, waar we het adres vonden en hoe je je uitschrijft.

## Tests
```bash
pip install pytest && python -m pytest -q
```
De tests bootsen websites en API's na met fictieve bedrijven. Er gebeuren geen echte calls.

Brongegevens: © OpenStreetMap-bijdragers (ODbL), Google Maps Platform, KBO Open Data, Meta.
