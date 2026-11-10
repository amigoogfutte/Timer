# Timer og materialer

Lille app til at notere timer, materialer og noter på sager i løbet af dagen, så de er lige til at taste ind i firmaets system bagefter. Appen har **ingen forbindelse til andre systemer** – du opretter selv sagerne med sagsnummer og adresse.

Dataene gemmes på telefonen (virker uden dækning) og synkroniseres med Home Assistant, så du kan se det samme på telefon og computer. Hver Home Assistant-bruger har sine egne data.

## Installation

1. HACS → ⋮ → **Brugerdefinerede repositories** → tilføj `https://github.com/amigoogfutte/timer` som type **Integration**.
2. Installer **Timer og materialer** og genstart Home Assistant.
3. Indstillinger → Enheder og tjenester → **Tilføj integration** → *Timer og materialer*.

Appen findes derefter i sidemenuen under **Timer** og på adressen `https://<din-home-assistant>/timer-app/index.html`.
På iPhone: åbn adressen i Safari → Del → **Føj til hjemmeskærm**.
