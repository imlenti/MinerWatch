# Guardian — governor di frequenza a runtime — Documento di design

> Stato: **implementato (v1, solo frequenza)**. Backend in `backend/guardian.py`
> + config in `backend/config.py` (`GuardianCfg`) + cinque colonne per-miner in
> DB (`guardian_enabled`, `guardian_max_freq_mhz`, `guardian_freq_floor_mhz`,
> `guardian_temp_source`, `guardian_max_temp_c`) +
> endpoint in `backend/main.py`; UI nella tab **Advanced** del miner
> (`frontend-react/.../GuardianPanel.tsx`). Tutto dietro il flag
> `guardian.enabled` (globale) e l'opt-in per-miner. Famiglie target:
> **Bitaxe** (tutte le rev) e **Nerd\*** (NerdQAxe / NerdOctaxe).
>
> Sostituisce il vecchio **Tuner** (profili Performance / Eco), rimosso. Vedi
> CHANGELOG. Il Tuner era un ottimizzatore one-shot del punto operativo; il
> Guardian è il "guardiano" sempre attivo che mancava — quello che il design
> del Tuner stesso aveva parcheggiato come lavoro futuro.

## 1. Obiettivo

Mantenere automaticamente un miner **sicuro ed efficiente al variare delle
condizioni ambientali** (soprattutto d'estate), senza rifare uno sweep e senza
intervento manuale. Dato un **tetto di frequenza** ("max", di default la
frequenza attuale), il Guardian abbassa la frequenza quando il VR scotta o gli
errori salgono, e la **recupera** verso il tetto quando rinfresca.

Perché non basta un profilo statico (es. "Eco d'estate"): l'estate non è una
temperatura sola — tra notte e primo pomeriggio l'ambiente oscilla anche di
15 °C. Un profilo statico deve essere conservativo per l'ora più calda e quindi
**spreca hashrate** per il resto della giornata. Il Guardian, essendo dinamico,
si riprende quel margine.

## 2. I tre layer di controllo (come si incastrano)

Ognuno sul suo tempo e sul suo sensore — non litigano, si rinforzano:

| Layer | File | Sensore | Leva | Cadenza | Ruolo |
|---|---|---|---|---|---|
| Auto-fan PID | `auto_control.py` | chip (`temp_chip_c`) | ventola | 5 s (loop interno veloce) | tiene il chip al target |
| **Guardian** | `guardian.py` | **VR** (`temp_vr_c`) + reject % | **frequenza** | ~5 min (loop esterno lento) | tiene VR/rifiuti nei limiti |
| Watchdog overheat | `auto_control.py` | chip | ventola → 100% | 5 s | rete dura a 75 °C |

Il VR è terreno scoperto: oggi **nessun** loop in MinerWatch lo governa (il PID
e il watchdog guardano il chip; il VR compariva solo come cutoff hard del
Tuner). Quando il VR scotta il Guardian taglia frequenza → meno watt → si
raffredda sia il VR sia il chip → il PID ventola rallenta.

## 3. La legge di controllo (v1)

Valutata una volta ogni `interval_seconds` per ciascun miner abilitato:

```
temp      > high              → frequenza − step_down_vr_mhz      (sicurezza)
hashrate regredito            → frequenza − step_down_hashrate_mhz (sicurezza)
reject %  > reject_pct_max    → frequenza − step_down_err_mhz     (sicurezza)
temp      < low               → frequenza + step_up_mhz           (recupero)
altrimenti (banda morta)      → hold
```

La sorgente `temp` è VR (default) o chip per-miner; `high`/`low` sono le soglie
della sorgente (vedi §6). Il termine **hashrate regredito** (aggiunto in 1.10.5)
è il freno all'instabilità: l'hashrate efficace è caduto oltre `hashrate_drop_pct`
sotto il meglio che quel chip ha dimostrato di reggere a una frequenza pari o
superiore. Vedi §3.1.

Valori di default (`GuardianCfg`, tarati sul campo): `vr_high_c=70`,
`vr_low_c=67` (banda morta di 3 °C = isteresi che evita l'oscillazione al bordo),
`reject_pct_max=1.1`, `reject_min_shares=20`, `step_down_vr_mhz=20`,
`step_down_err_mhz=10`, `step_up_mhz=10`, `interval_seconds=300`,
`frequency_floor_mhz=400`.

Principi:

- **Le azioni in discesa (sicurezza) battono il recupero in salita**. L'ordine
  delle clausole codifica la priorità: prima la temperatura, poi gli errori,
  poi (solo se nient'altro) si risale.
- **Asimmetria dei passi** (−20 veloce / +10 piano): molla in fretta, recupera
  con calma → il loop si assesta invece di "cacciare".
- Ogni risultato è **clampato a `[floor, ceiling]`**. Il *ceiling* è il "max"
  dell'utente: il Guardian non ci va mai sopra (e se trova la frequenza sopra
  il tetto — es. overclock manuale — la riporta giù al tetto). Il *floor* evita
  di throttlare il miner fino a renderlo inutile.
- La funzione di decisione `decide_frequency(...)` è **pura** (nessun I/O), così
  la policy è testabile in isolamento — vedi `tests/test_guardian.py`.

### Il segnale di instabilità: reject rate (non l'HW error count)

In v1 il secondo segnale è il **tasso di share rifiutate**, calcolato a runtime
come **delta sui contatori** tra un tick e il precedente:
`Δrejected / Δ(accepted + rejected) × 100` (sorgenti: `sharesRejected` /
`sharesAccepted`, già in `MinerSample.rejected` / `accepted`). Guardie: la % si
calcola solo se nell'intervallo sono arrivate almeno `reject_min_shares` share
(altrimenti una singola share stale falserebbe il valore → si ritorna `None` e
governa il solo VR per quel tick), e un calo dei contatori (reboot) azzera la
baseline.

**Perché NON usiamo l'`errorCount` dell'`hashrateMonitor`.** Era il piano
iniziale (`errorCount / total`), ma su un miner reale il campo `total`
dell'`hashrateMonitor` si è rivelato essere **l'hashrate** (in GH/s, = somma dei
`domains`), non un contatore di lavoro. Dividere un conteggio errori cumulativo
per l'hashrate produceva valori assurdi (>100%, es. 478% / 7558% osservati). Il
reject rate non ha questo problema: contatori monotòni veri, nella scala giusta
(ben sotto l'1% su un miner sano), disponibili su **tutte** le famiglie AxeOS
(Bitaxe *e* Nerd\*). La soglia 1.1 % è volutamente più lasca dello standard di un
tuning one-shot, perché un governor a runtime deve tollerare più rumore prima di
reagire.

### 3.1 Il freno anti-regressione dell'hashrate (1.10.5)

Il reject rate cattura solo le share rifiutate **dal pool**. Ma il sintomo tipico
di un overclock spinto troppo è l'**errore hardware dell'ASIC** (nonce invalidi):
quei nonce falliscono il controllo di difficoltà in locale e **non vengono mai
inviati al pool**, quindi affossano l'hashrate *efficace* senza alzare il reject
%. Peggio: il chip instabile fa meno lavoro reale, assorbe meno watt e scalda
meno → il ramo di recupero legge "più margine termico" e **alza ancora** la
frequenza. Risultato osservato sul campo (Gamma): l'hashrate reale crollava
mentre il Guardian continuava a salire, col reject % che restava basso.

Il freno chiude il buco senza reintrodurre il problema del denominatore di
`errorCount`: invece di stimare una % d'errore, guarda direttamente l'**hashrate
efficace** (`MinerSample.hashrate_ths`, già l'EWMA reale di AxeOS). Lo stato
per-miner tiene il **picco** di hashrate visto e la frequenza a cui l'ha toccato.
Se a una frequenza pari o superiore al picco l'hashrate scende sotto
`peak × (1 − hashrate_drop_pct)` (default 15 %), il chip è instabile → si scende
di `step_down_hashrate_mhz` e si fissa un **soft ceiling** in memoria appena
sotto il punto che si è rotto, così il recupero si assesta lì invece di
ri-arrampicarsi (evita l'hunting al bordo). Il soft ceiling non tocca la config
utente e si azzera quando il miner esce dal loop (offline/disabilitato).

Guardie: la lettura dell'hashrate si ignora per `hashrate_settle_seconds`
(default 120 s) dopo un cambio, perché l'EWMA di AxeOS è in ritardo di un minuto
o due e scambieremmo l'assestamento per un crollo. Il ramo ha priorità appena
sotto la temperatura (vedi §3) e batte sia il reject sia il recupero. La conta
grezza `errorCount` (`MinerSample.hw_errors`) resta esposta come telemetria nel
readout live, ma **non** è il segnale di controllo — quello è l'hashrate.

Fatto chiave (confermato sul campo, sia Bitaxe sia Nerd\*): **AxeOS applica il
cambio di frequenza — e di voltaggio — a caldo, senza reboot.** Quindi non c'è
costo di downtime per nudge. Il vincolo diventa l'**inerzia termica del VR**:
dopo un cambio il VR continua a derivare per un minuto o due. Ticchettare più in
fretta di così significherebbe decidere su una lettura non ancora stabilizzata →
oscillazione. Per questo il loop gira su un intervallo lungo (≥ tempo di
risposta del VR); un `cooldown_seconds` opzionale può forzare settle extra.

**Usura NVS.** Un PATCH di frequenza persiste nella flash dell'ESP32. Il
Guardian scrive **solo quando il target è diverso** dalla frequenza live: dentro
la banda morta 65–70 °C si parcheggia su una frequenza d'equilibrio e smette di
scrivere. Le scritture avvengono solo quando l'ambiente deriva oltre soglia —
un numero limitato e sotto controllo.

### 3.2 Media mobile delle metriche (windowed metrics averaging)

Per prevenire falsi positivi causati da oscillazioni temporanee o transitorie delle metriche (es. picchi improvvisi di temperatura o cali temporanei dell'hashrate), il Guardian non lavora sulle metriche istantanee dell'ultimo poll, ma calcola una media dei valori registrati nel database in una finestra temporale (di default 30 secondi).

La finestra temporale è configurabile globalmente tramite la chiave `guardian.hashrate_average_window_seconds` nelle impostazioni generali. Impostando il valore a `0`, la funzionalità viene disattivata e il governor torna ad utilizzare i valori istantanei.

## 5. Stato per-miner e ciclo del controller

`GuardianController` (in `guardian.py`) è speculare ad `AutoFanController`:
`start()` / `stop()` nella lifespan dell'app, un loop `_run()` che ogni
`interval_seconds` chiama `_tick(poller.last_results)`. Per ogni miner abilitato
+ online + famiglia supportata, `_govern_one(...)`:

1. ricava la frequenza corrente dal sample live (fallback: ultima comandata);
2. calcola il reject % sull'intervallo (avanza la baseline dei contatori);
3. risolve `ceiling` (= `guardian_max_freq_mhz`, fallback alla freq corrente) e
   `floor` (= `guardian_freq_floor_mhz`, fallback al default globale);
4. chiama `decide_frequency(...)`;
5. se `target == corrente` → **non tocca nulla** (niente scrittura NVS);
6. altrimenti applica `set_frequency(target)` (a caldo), aggiorna lo stato e
   logga; pubblica un readout live per l'endpoint di status.

Lo stato per-miner (`_GuardianState`) tiene i contatori precedenti, l'ultima
frequenza comandata, il timestamp dell'ultimo cambio e l'ultima decisione. Lo
stato viene scartato quando un miner esce dalla lista (offline/disabilitato),
così al rientro riparte con una baseline reject pulita.

## 6. Modello dati e API

Per-miner, sulla riga `miners` (così arrivano gratis in `get_miner`/`list_miners`
via `SELECT *`):

- `guardian_enabled` (0/1) — opt-in per-miner;
- `guardian_max_freq_mhz` — il tetto "max"; di default = frequenza corrente al
  momento dell'abilitazione, **editabile** dall'utente esperto;
- `guardian_freq_floor_mhz` — override opzionale del floor (NULL = default);
- `guardian_temp_source` — quale sensore governa la frequenza: `vr` (default) o
  `chip`. NULL/sconosciuto = `vr` (comportamento legacy);
- `guardian_max_temp_c` — la **temperatura massima** per-miner (la soglia alta).
  Il punto di recupero (soglia bassa) è derivato a runtime come
  `max − deadband`, dove il deadband è quello della sorgente attiva
  (`high_default − low_default`). NULL = default globale della sorgente.

**Sorgente temperatura e max-temp per-miner.** Le soglie restano nel
`GuardianCfg` come **default per sorgente** (`vr_high_c/vr_low_c`,
`chip_high_c/chip_low_c`), ma ora ogni miner può scegliere il sensore e
impostare una sola soglia "max"; la legge di controllo è invariata e
**source-agnostic** (`decide_frequency` riceve un valore e due soglie, più
un'etichetta solo per il testo del motivo). Caveat della modalità **chip**: il
chip è già governato dal PID ventola (~60 °C) e dal watchdog 75 °C, quindi un
governor sul chip agisce solo a ventola satura; l'endpoint rifiuta un max chip
≥ 75 °C perché non scavalchi mai il watchdog. Il VR resta default e consigliato
(nessun altro loop lo governa).

Scrittura via `db.set_guardian_config(...)` (pattern COALESCE come
`set_fan_config`: aggiorna solo i campi passati).

Endpoint:

- `GET /api/miners/{id}/guardian/status` → flag globale, supporto
  (famiglia + capability), opt-in, max/floor, **sorgente + max-temp**, frequenza
  corrente, default (soglie VR *e* chip, watchdog, passi/intervallo) e readout
  live (che ora porta `temp_c` + `temp_source`);
- `POST /api/miners/{id}/guardian/config` → `{enabled?, max_freq_mhz?,
  freq_floor_mhz?, temp_source?, max_temp_c?}`. All'**abilitazione** senza `max`,
  il backend default il tetto alla frequenza corrente (409 se non ancora nota
  dal primo poll). `temp_source` validato a `vr`/`chip`; max chip ≥ 75 °C → 400.

UI: tab **Advanced** del miner (`GuardianPanel.tsx`): toggle di abilitazione
(con **dialog di conferma "a proprio rischio"** all'attivazione), campo **max
frequency** (default = corrente, editabile), **sorgente temperatura** (VR/chip)
e **max temperature** (con recupero derivato mostrato), floor opzionale,
riepilogo della policy (adattato alla sorgente), readout live e una nota di
rischio. Tutto gated su `capabilities.set_frequency` e sul supporto famiglia.

## 7. Sicurezza e reversibilità

- Il **watchdog 75 °C** sul chip resta sempre sotto a tutto come rete dura: il
  Guardian interviene prima e più gentilmente sul VR.
- Il Guardian **non tocca mai il voltaggio** in v1 (vedi §8).
- È un **bolt-on additivo**: vive in un modulo nuovo, usa solo metodi driver già
  esistenti (`set_frequency` / `poll`), tre colonne isolate e due endpoint.
  Dietro un feature flag. Si rimuove cancellando il modulo + le route + la tab +
  le colonne, senza toccare il resto.

## 8. Evoluzione v2 — leva sul voltaggio (NON attiva in v1)

Siccome AxeOS applica **anche il voltaggio** a caldo, si apre una seconda leva.
Il termine reject in v1 cura il sintomo abbassando la frequenza, ma
l'instabilità (rifiuti che salgono) è in realtà un problema di
**sotto-voltaggio**: la cura "giusta" sarebbe **+voltaggio**. La v2 potrebbe:

1. rispondere a un reject rate sostenuto **alzando `coreVoltage`** (di
   `v2_voltage_step_mv`, entro `v2_voltage_ceiling_mv`) invece di tagliare
   frequenza;
2. quando taglia frequenza, **abbassare anche il voltaggio** in coppia, per
   restare vicino a Vmin e preservare l'efficienza (J/TH).

Perché resta fuori dalla v1: alzare il voltaggio in automatico, 24/7, senza
nessuno davanti, è la leva **più rischiosa** (più calore/watt, più vicino ai
limiti dell'hardware). I parametri (`GuardianCfg.v2_voltage_*`, default
`v2_voltage_enabled=False`) e le cuciture nel codice sono già pronti, inerti
finché la v2 non li legge. Si abilita la v2 solo dopo che la v1 ha dimostrato di
comportarsi bene.

## 9. Limiti noti / non-obiettivi v1

- **Solo frequenza** (niente voltaggio — vedi §8).
- **Solo Bitaxe / Nerd\*** (espongono `vrTemp` e `set_frequency`).
- **Il termine reject si disattiva sugli intervalli con poche share**
  (< `reject_min_shares`): su un solo-miner a difficoltà alta, finestre con
  pochi share fanno governare il solo VR per quel tick. È voluto (evita falsi
  allarmi da una singola share stale), non un limite di famiglia: i contatori
  share esistono sia su Bitaxe sia su Nerd\*. Mitigato da 1.10.5: il freno
  anti-regressione dell'hashrate (§3.1) non dipende dal volume di share, quindi
  l'instabilità da overclock viene colta anche quando il reject è muto.
- **Nessuna finestra oraria** (l'amico n8n la usava per energia off-peak / fresco
  notturno; MinerWatch gira 24/7, quindi sempre attivo quando abilitato). Una
  finestra oraria è una possibile aggiunta futura.
- È un **throttle protettivo + recupero**, non un ottimizzatore di efficienza:
  scendere di frequenza tenendo il voltaggio fisso peggiora i J/TH in quel
  momento. Accettabile — la sicurezza viene prima (la v2 indirizza l'efficienza).
