# Funghi Toscana AI — R6

R6 separa definitivamente **acquisizione/elaborazione** da **servizio dati all'APK**.

## Stato

- R5.2 resta in produzione finché R6 non genera un vero snapshot **418/418 scientificamente completo**.
- Il radar resta separato e non viene modificato.
- R6 usa GitHub Actions come Data Builder esterno.
- Cloudflare D1 conserva catalogo, componenti riusciti, storico giornaliero e generazioni.
- Il Worker R6 è un gateway leggero: legge D1 e serve manifest/snapshot; non esegue ETL meteorologico pesante.
- L'APK non viene modificato durante il bootstrap R6.

## Garanzie progettuali

1. **Nessuna regressione a 0/418:** un nuovo ACTIVE viene pubblicato soltanto se la generazione candidata è scientificamente completa.
2. **ACTIVE atomico:** `ACTIVE_PREVIOUS` resta disponibile; il puntatore cambia solo a fine validazione.
3. **Resume per batch:** ogni batch Open-Meteo riuscito viene scritto subito in D1 con `cycle_key`; un retry nello stesso ciclo scarica soltanto i componenti mancanti.
4. **Storico incrementale:** 35 giorni vengono scaricati solo in `bootstrap`; i cicli normali aggiungono i giorni nuovi dal piccolo `past_days=2` già incluso nella pipeline forecast.
5. **Pipeline indipendenti:** `current`, `forecast`, `soil/ET0` proseguono indipendentemente; un errore non cancella gli altri componenti.
6. **SIR/CFR prioritario ma non bloccante:** per le finestre pioggia 5/7/15/30 il dato ufficiale prevale; Open-Meteo exact-point è fallback dichiarato.
7. **Provenienza esplicita:** i campi scientifici principali portano fonte, timestamp e qualità.
8. **Diagnostica separata:** raggiungibilità SIR/CFR, acquisizione, freschezza e coperture scientifiche sono concetti distinti.

## Coperture richieste prima della pubblicazione

Una generazione può diventare ACTIVE solo quando risultano tutte 418/418:

- copertura strutturale;
- corrente;
- pioggia 5/7/15/30;
- storico giornaliero >= 28 giorni;
- forecast +7;
- forecast +15;
- ET0 forecast;
- suolo;
- completezza scientifica complessiva.

Il semplice fatto di avere 418 righe **non** equivale a dataset completo.

## Modalità builder

```bash
python -m funghi_r6.cli --mode bootstrap
python -m funghi_r6.cli --mode full
python -m funghi_r6.cli --mode light
```

- `bootstrap`: unico ciclo autorizzato a scaricare la finestra storica di 35 giorni.
- `full`: ciclo giornaliero; aggiorna SIR/CFR, corrente, forecast, suolo/ET0 e aggiunge solo i giorni recenti allo storico.
- `light`: refresh infragiornaliero corrente/forecast/suolo senza ricostruire 30 giorni da Internet.

## GitHub Actions

Il workflow `r6-builder.yml` prevede:

- 06:05 Europe/Rome: `full`;
- 12:05 Europe/Rome: `light`;
- 18:05 Europe/Rome: `light`;
- avvio manuale `workflow_dispatch`, compreso `bootstrap`.

Finché i secret Cloudflare non sono configurati, il builder lavora in modalità **ARTIFACT_ONLY** e carica `manifest.json`, `snapshot.json.gz` e diagnostica come artifact GitHub. Questo consente di verificare il primo 418/418 senza toccare la produzione.

## Secret Cloudflare — da configurare solo dopo il primo audit artifact-only

- `CF_ACCOUNT_ID`
- `CF_D1_DATABASE_ID`
- `CF_API_TOKEN` con permessi D1 Read/Write sul database R6

Usare un **database D1 nuovo per R6**. Non puntare al D1 della R5.2 durante il bootstrap.

## Worker R6

Cartella `worker/`. Route iniziali:

- `GET /health`
- `GET /api/v6/manifest`
- `GET /api/v6/snapshot`
- `GET /api/v6/source-health`
- bridge compatibilità `GET /api/v1/stations`
- bridge diagnostica `GET /api/v1/cache-status`

Il bridge `/api/v1/stations` esiste per il successivo passaggio APK, ma **non deve essere usato dall'APK finché il primo ACTIVE 418/418 non è stato verificato**.

## Radar

Resta invariato e indipendente:

`https://funghi-toscana-radar.porcinitoscanaai.workers.dev`
