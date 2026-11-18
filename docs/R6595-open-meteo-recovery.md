# Recupero delle acquisizioni Open-Meteo R6595

Il run 44244726894 del 18 novembre 2026 si è fermato alle 05:34 UTC
(06:34 italiane). Cinque richieste alla sonda TOS01000025 sono terminate
con read timeout di 35 secondi. Il riepilogo riportava invalid_daily_object
anche se la risposta giornaliera non era stata ricevuta. La sonda richiedeva
17 variabili per 18 giorni, compresi due giorni precedenti.

Il manifest Cloudflare letto prima dell'intervento attesta che il recupero
successivo aveva pubblicato R6-2026-11-18-062718Z, completo 418/418,
con forecast a +7/+15 e verifica fisica del commit. Questo successo documenta
il comportamento della base precedente, senza attestare una notte della patch.

## Modifiche

La sonda chiede soltanto pioggia, temperatura media ed ET0 per i 16 giorni
correnti/futuri. Usa al massimo due tentativi con timeout connessione/lettura
5/20 secondi. La validazione continua a richiedere ogni data D+1...D+15.
Il risultato minimale resta nel rapporto e non entra nella cache scientifica.
Il normale download successivo acquisisce tutte le 17 variabili, comprese
minime, massime, vento e umidità. Una sonda completa già presente nella cache
viene riutilizzata.

Gli errori HTTP portano motivo, stato e attributo retryable. Timeout,
connessione interrotta, HTTP 429 e HTTP 5xx prima delle 06:00 UTC producono
WAITING_FORECAST_TRANSPORT. Lo scheduler già presente esegue un nuovo tentativo.
Il rapporto conserva l'errore di trasporto. JSON malformato, schema inatteso
e HTTP permanenti continuano a bloccare il job. Alle 06:00 UTC, e per force
manuale, anche un errore di trasporto persistente resta un fallimento effettivo.
L'attesa per la sola coda D+15 segue la scadenza già esistente.

HTTP e parsing JSON condividono un unico budget di tentativi. Gli errori
permanenti terminano subito. Il client rispetta Retry-After e chiude le risposte
prima del nuovo tentativo. HTTP 429 e richieste permanenti non attivano
suddivisioni ricorsive delle coordinate.

Quando un forecast di una singola coordinata esaurisce i retry di trasporto,
il recupero scarica tutti i campi in tre gruppi da sei/sei/cinque variabili.
Ogni gruppo dispone al massimo di due tentativi con timeout 8/25 secondi.
La riunione richiede la stessa sequenza di date, stessa griglia, quota, fuso e
UTC offset, oltre a serie di lunghezza coerente. I null ricevuti restano null.
La scrittura D1 avviene dopo il completamento di tutti i gruppi. Le verifiche
scientifiche e l'assemblaggio finale continuano a esaminare ogni centralina.

## Collaudo

Eseguire dalla radice del repository.

```sh
python -m pip install -e . pytest pyyaml
python -m pytest -q
python -m compileall -q src tests
node worker/test-worker.mjs
node --test scheduler/test-scheduler.mjs radar-bridge/test-bridge.mjs
npm ci --no-audit --no-fund
npm run test:runtime
```

Le regressioni R6595 usano HTTP controllato per attraversare client reale,
sonda, stato giornaliero e D1 SQLite. Provano timeout, 429/503, scadenza e force,
parsing JSON, credenziali errate, mantenimento dell'ACTIVE, recupero di tutti i
campi, null opzionali, date e griglie discordanti e arresto prima della scrittura.
La prova di pubblicazione R6587 continua a validare tutte le 418 centraline,
pubblicare una volta e riutilizzare l'ACTIVE al tentativo successivo.

Dopo CI e integrazione in main, l'osservazione della prossima acquisizione
notturna deve controllare forecast-readiness.json, daily-receipt.json e manifest
ACTIVE. Una riuscita di oggi con DAILY_ALREADY_READY dimostra il riuso della
generazione già pubblicata. La verifica delle 45 mappe rimane una prova Android.
La puntualità di D+15 dipende dalla disponibilità del prodotto sul provider.

## Dipendenza dei fusi orari FIX2

Il collaudo Windows ha rilevato tzdata assente e ZoneInfoNotFoundError per
Europe/Rome. tzdata è ora una dipendenza dichiarata del progetto. La procedura
verifica il fuso subito dopo l’installazione delle dipendenze. Un test avvia un
nuovo processo senza database di fusi del sistema e verifica inverno, estate
e cambio giornata tramite il pacchetto IANA installato.
