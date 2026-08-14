# Lesbord

Digitaal lesbord voor wiskundelessen op het smartboard. Vervangt de rommelige
OneNote-workflow: leerdoelen altijd bovenin, een theorieblok, per opgave een
snippet uit het boek links en een oneindig ruitjesbord rechts, plus een
lestimer rechtsonder. Lessen zijn per klas geordend en worden automatisch
opgeslagen.

## Bediening tijdens de les

- **Startscherm**: verschijnt bij het openen van een les — links de
  afsprakenposter (eenmalig instellen, geldt voor alle lessen), rechts de
  startopdracht (per les). "Start de les" sluit het scherm en start meteen
  de timer op de ingestelde lesduur. Later opnieuw oproepen via de
  **Start**-knop links bovenin.
- **Leerdoelen**: typ direct in de balk bovenaan.
- **Theorie**: typ of plak screenshots (Ctrl+V). Inklapbaar via het kopje.
- **Opgaven**: tabs boven het linkerpaneel. `+` voor een nieuwe opgave,
  dubbelklik om te hernoemen. Klik in het linkerpaneel en plak (Ctrl+V) een
  screenshot van de opgave. Elke opgave heeft een eigen bord.
- **Bord**: pen (P), gum (G), verschuiven (H of rechtermuisknop slepen).
  Scrollen = omlaag, Shift+scroll = opzij, Ctrl+scroll = zoomen.
  `⌂` springt terug naar het beginpunt. Ctrl+Z = ongedaan maken.
- **Groepjes**: 👥 rechtsboven op het bord opent een paneel met
  aanwezigheid (tik een naam aan = afwezig) en een hussel-knop voor
  groepjes van 2, 3 of 4. Namen zet je per klas op het beginscherm — of
  neem ze over uit de huiswerkcontrole (knop **↓ Uit huiswerkcontrole** in
  het namenvenster). Onder in het paneel staat ook het blok **Huiswerk** met
  het percentage gemaakt huiswerk per leerling (uit de huiswerkcontrole).
- **Timer**: klik op de klok rechtsonder, kies een preset of aantal minuten.
  Onder de 5 minuten wordt hij rood.
- **Les kopiëren**: in het overzicht via ⧉ — handig als je dezelfde les aan
  meerdere klassen geeft (bord-inkt gaat mee, dus meestal kopieer je vóór de les).

## Huiswerkcontrole

De huiswerkcontrole-app draait mee onder **`/huiswerk/`** (op de VPS dus
`https://…/lesbord/huiswerk/`). Handig tijdens de les: open het lesbord op je
laptop en doe de huiswerkcheck op je telefoon — beide zitten achter hetzelfde
wachtwoord, dus je logt maar één keer in. Je opent het via de link
**Huiswerkcontrole ↗** op het startscherm of de knop **📋 Huiswerk** in de
balk bovenin een les.

**Koppeling lesbord ↔ huiswerkcontrole** loopt via de **klasnaam** (dezelfde
naam in beide, hoofdletterongevoelig). Zolang die overeenkomt:

- neem je de leerlingnamen in het lesbord over uit de huiswerkcontrole met
  **↓ Uit huiswerkcontrole** in het namenvenster (👥 op het beginscherm), en
- zie je in het groepjespaneel (👥 op het bord) per leerling het percentage
  gemaakt huiswerk plus het klasgemiddelde.

Technisch is het de originele Flask-app (`huiswerk/`) die als sub-app in de
FastAPI-server is gemount; de data staat in `huiswerk.db` naast `lesbord.db`.
De eigen pincode van de huiswerk-app staat uit (de wachtwoordpagina van het
lesbord beveiligt alles). Wil je hem tóch los draaien: `python huiswerk/app.py`.

## Lokaal draaien

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --port 8600
# open http://localhost:8600
```

## Deploy op de VPS

```bash
sudo mkdir -p /opt/lesbord && sudo chown streamlit:streamlit /opt/lesbord
# kopieer main.py, requirements.txt, static/ en huiswerk/ naar /opt/lesbord (git of scp)
# (wil je bestaande huiswerkdata meenemen? kopieer dan ook huiswerk.db mee)
cd /opt/lesbord
python3 -m venv venv
venv/bin/pip install -r requirements.txt

sudo cp lesbord.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lesbord
```

### Nginx (subpad)

De frontend gebruikt uitsluitend relatieve URLs, dus een subpad werkt —
mits met slash aan het eind. Voeg toe aan je bestaande serverblok:

```nginx
location /lesbord/ {
    proxy_pass http://127.0.0.1:8600/;   # let op: slash aan het eind
    proxy_set_header Host $host;
    client_max_body_size 12m;            # voor het plakken van screenshots
}
# nette redirect zonder slash:
location = /lesbord { return 301 /lesbord/; }
```

**Belangrijk**: open de app altijd als `https://…/lesbord/` (mét slash),
anders kloppen de relatieve paden niet. De redirect hierboven vangt dat af.

### Beveiliging

Er zit een eenvoudige **wachtwoordpagina** vóór het lesbord (één gedeeld
wachtwoord, geen accounts). Zonder geldig cookie geeft `/` een redirect naar
`/login` en geven de `/api/*`-endpoints `401`. Na inloggen zet de server een
ondertekend cookie (30 dagen geldig); het geheim waarmee dat cookie wordt
ondertekend staat per installatie in `lesbord.db`.

Zet in productie een eigen wachtwoord via de omgevingsvariabele
`LESBORD_WACHTWOORD` (standaard `lesbord`). In de systemd-unit:

```ini
[Service]
Environment=LESBORD_WACHTWOORD=kies-hier-iets-sterks
```

Uitloggen kan via `/logout`. Omdat de app achter https draait, is het cookie
`HttpOnly` + `SameSite=Lax`.

Wil je liever géén wachtwoordpagina maar bijv. basic auth in nginx, dan kan dat
ook nog steeds:

```nginx
location /lesbord/ {
    auth_basic "Lesbord";
    auth_basic_user_file /etc/nginx/.htpasswd-lesbord;
    proxy_pass http://127.0.0.1:8600/;
    proxy_set_header Host $host;
    client_max_body_size 12m;
}
```

(`sudo htpasswd -c /etc/nginx/.htpasswd-lesbord jasper`)

## Data & back-up

- `lesbord.db` — SQLite met klassen en lessen (inkt als JSON).
- `uploads/` — geplakte afbeeldingen.

Back-up = die twee dingen kopiëren. Eén cronregel is genoeg:

```bash
0 3 * * * tar czf /opt/backups/lesbord-$(date +\%F).tar.gz -C /opt/lesbord lesbord.db uploads
```

## Ideeën voor v2

- Roosterkoppeling: timer start automatisch op de lesduur van het uur.
- PDF-export van een les (doelen + theorie + borden) om te delen met leerlingen.
- Afbeeldingen ook óp het bord kunnen plakken en verschuiven.
- Magic-link login hergebruiken van bovenbouwsucces.nl i.p.v. basic auth.
