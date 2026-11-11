# Timer og materialer

Lille app til at notere timer, materialer og noter på sager i løbet af dagen, så de er lige til at taste ind i firmaets system bagefter. Appen har **ingen forbindelse til andre systemer** – du opretter selv sagerne med sagsnummer og adresse.

Dataene gemmes på telefonen (virker uden dækning) og synkroniseres med Home Assistant, så du kan se det samme på telefon og computer. Hver Home Assistant-bruger har sine egne data.

## Installation

1. HACS → ⋮ → **Brugerdefinerede repositories** → tilføj `https://github.com/amigoogfutte/timer` som type **Integration**.
2. Installer **Timer og materialer** og genstart Home Assistant.
3. Indstillinger → Enheder og tjenester → **Tilføj integration** → *Timer og materialer*.

Appen findes derefter i sidemenuen under **Timer** og på adressen `https://<din-home-assistant>/timer-app/index.html`.
På iPhone: åbn adressen i Safari → Del → **Føj til hjemmeskærm**.

## Siri og CarPlay (Genveje)

Appen kan ikke vises i CarPlay, men du kan registrere med stemmen via Siri: *"Hej Siri, registrer timer"*.
Integrationen har to adresser til det (kræver version 0.8.0):

- `GET /api/timeregistrering/stemme/sager` → `{"sager": ["1234 Vejnavn 5", ...]}` (åbne sager, senest brugte først)
- `POST /api/timeregistrering/stemme` med fx `{"sag": "1234 Vejnavn 5", "type": "Timer", "timer": "to en halv", "tekst": "skiftet vinduer"}`
  - `type`: `Timer`, `Materiale` (felter `vare`, `antal`, `enhed`) eller `Note` (felt `tekst`)
  - Antal forstås som "2,5", "2.5", "to en halv", "halvanden" osv.
  - Uden `sag` bruges den senest brugte sag. Svaret `{"svar": "Gemt: 2,5 timer på sag 1234."}` kan Siri læse højt.

Begge kræver `Authorization: Bearer <langtidsadgangstoken>` fra **den samme Home Assistant-bruger**, som bruger appen
(Profil → Sikkerhed → Langtidsadgangstokens).

### Genvejen på iPhone

1. **Hent indhold af URL**: `https://<din-home-assistant>/api/timeregistrering/stemme/sager`, header `Authorization` = `Bearer <token>`.
2. **Hent ordbogsværdi** `sager`.
3. **Vælg fra liste** – "Hvilken sag?"
4. **Vælg fra menu** – "Hvad vil du registrere?" med punkterne *Timer*, *Materiale*, *Note*. I hvert punkt: **Spørg efter input** (tekst) for felterne, og til sidst en **Ordbog** med `sag` = *Valgt emne*, `type` = punktets navn og felterne.
5. **Hent indhold af URL**: `https://<din-home-assistant>/api/timeregistrering/stemme`, metode **POST**, samme header, anmodningsbrødtekst **Fil** = *Menuresultat*.
6. **Hent ordbogsværdi** `svar` → **Tal tekst**.

Kald genvejen "Registrer timer", så virker den med Siri – også i CarPlay.
