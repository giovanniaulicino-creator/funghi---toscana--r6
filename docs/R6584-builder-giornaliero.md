# R6584 — aggiornamento scientifico giornaliero Toscana

## Comportamento

Lo scheduler Cloudflare separato `funghi-toscana-r6-scheduler` controlla ogni dieci minuti
(minuti 05/15/25/35/45/55 UTC). Usa Europe/Rome per la giornata scientifica e inizia alle
01:15 locali, anche con ora legale. Cerca un ACTIVE completo 418/418 e verificato in D1.
Se manca, controlla le esecuzioni GitHub del builder su main e richiede un full.
Un lease D1 di 15 minuti impedisce dispatch sovrapposti: con il cron di dieci minuti,
un tentativo senza esito viene ripreso normalmente dopo venti minuti.
Un run ancora in corso viene atteso; un tentativo fallito può essere ripetuto.

Il workflow mantiene la stessa concurrency globale del builder. Ogni avvio usa
`--daily`: se oggi è già completo scrive DAILY_ALREADY_READY e non chiama SIR,
Open-Meteo o Copernicus. Un HTTP200/204 di dispatch certifica soltanto l'accettazione;
il completamento richiede il vero ACTIVE di quella giornata. Il workflow GitHub
rimane come fallback alle 01:17,03:17,06:17,09:17 Europe/Rome.
Non vengono più eseguiti refresh scientifici automatici a mezzogiorno/sera.

I tentativi incompleti riutilizzano i componenti sani della giornata, anche oltre
il confine delle sei ore. I payload current/soil/forecast incompleti vengono
esclusi dal riuso e ritentati selettivamente. Il forecast richiede tutte le
date +1…+15 e valori numerici pioggia/temperatura media/ET0 per ciascuna data.
Anche uno storico lungo ma numericamente insufficiente attiva il recupero.
Questo è un controllo di acquisizione: assemble.py e le formule restano identici.

Richieste per una data scaduta/futura non contattano D1 o le fonti.
Un cambio giornata durante acquisizione/staging impedisce la promozione.
ACTIVE precedente e relativo snapshot restano disponibili se la preparazione fallisce.
`force_rebuild=true` è una deroga manuale esplicita per manutenzione.

## Isolamento e autorizzazioni

Repository fisso: giovanniaulicino-creator/funghi---toscana--r6.
D1 fisso: b198c62b-1165-4626-9bd9-40f10c6ff432, funghi-toscana-r6.
Il gateway worker/src/index.js, il radar, lo schema D1 e le fonti scientifiche
restano invariati. Lo scheduler scrive soltanto la chiave R6584_SCHEDULER di
system_state. Nessun dato, credenziale o riferimento Basilicata.

Secret richiesto sul nuovo Worker: GITHUB_ACTIONS_TOKEN, fine-grained token con
accesso al solo repository Toscana e Actions read/write. Nessun valore nel git,
in wrangler.toml, nell'URL o nel rapporto. Il token scade secondo la scelta fatta
su GitHub: sostituirlo prima della scadenza; errori401/403 sono visibili nella salute.
L'endpoint pubblico GET /health è in sola lettura; non esiste una route pubblica
per avviare lavori.

## Attivazione e verifica

1. Pubblicare questi sorgenti su main dopo test e audit; attendere R6 CI verde.
2. Autenticarsi a Cloudflare e dalla cartella scheduler eseguire:
   `npx --yes --package=wrangler@4.157.0 wrangler deploy`.
3. Impostare il secret con `npx --yes --package=wrangler@4.157.0 wrangler secret put GITHUB_ACTIONS_TOKEN`
   usando il prompt del terminale, mai un messaggio di chat.
4. Controllare /health del nuovo Worker: revision R6584, token_configured true,
   giornata corretta. ready=true richiede ACTIVE418/418 verificato.
5. Verificare l'avvio automatico e l'ACTIVE della nuova giornata dalla prima notte;
   sul Pixel verificare separatamente download,9CURRENT,45/45 prima dell'apertura.

Il pacchetto di attivazione contiene un programma locale che prepara un checkout
isolato, controlla gli hash, ripete i test e può pubblicare senza forzare la branch.
Per l'autenticazione usa gli strumenti ufficiali sul PC dell'utente.

## Limiti operativi

La serializzazione dei produttori è quella del workflow GitHub: gli eventuali
avvii CLI esterni devono rispettare la stessa esclusione. Non è stata aggiunta una
nuova garanzia di transazione al preesistente batch REST D1. Il controllo giornaliero
legge metadati e struttura dei chunk, riusando la verifica fisica del commit;
non ricalcola continuamente SHA19MB e non certifica bitrot successivo al commit.

Cron e runner esterni non danno una garanzia assoluta delle04:00. L'innesco esterno
rimuove la dipendenza dal solo cron GitHub osservato in forte ritardo; resta da
misurare una notte reale, compreso il completamento delle mappe Android.
Nessun nuovo servizio a pagamento richiesto. Lo scheduler effettua al massimo144
controlli/giorno e non scarica snapshot meteo o raster.
