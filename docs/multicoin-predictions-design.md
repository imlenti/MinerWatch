# Stime per moneta su fleet misto — Documento di design

> Stato: **implementato**. Classificazione in `backend/coin.py`, matematica in
> `backend/predictions.py` (entrambi puri e testati), colonna per-miner
> `coin_override` in DB, endpoint `POST /api/miners/{id}/coin`, campi `coin` /
> `coin_source` / `coin_override` su `GET /api/pools`, `groups[]` su
> `GET /api/fleet/prediction`. UI: sotto-tab per moneta nella card
> **Predictions** della tab Analytics, badge + picker nella tab **Pools**.
> Correzioni collegate in `backend/halo.py` e `backend/poller.py`.

## 1. Il problema

MinerWatch dava per scontato che tutto il fleet minasse la stessa moneta. Su un
fleet misto — tipicamente qualche device su BTC e qualcuno su BCH — ogni numero
che accoppia **hashrate** e **difficoltà di rete** era sbagliato, perché i due
lati si riferivano a catene diverse.

Tre punti erano interessati, con tre regole diverse e incompatibili tra loro:

| Punto | Regola precedente | Effetto su fleet misto |
|---|---|---|
| `/api/fleet/prediction` | hashrate totale × **prima** `network_difficulty` trovata iterando i miner | risultato **arbitrario**: dipendeva dall'ordine di iterazione e poteva cambiare tra un poll e l'altro |
| `/api/halo` | hashrate totale × difficoltà **più alta** del fleet | sempre BTC, quindi ogni share BCH sembrava ~1000× più lontano da un blocco di quanto fosse |
| block-found (`poller.py`) | difficoltà stratum del miner, con fallback a **mempool.space (solo BTC)** | su firmware che non riporta la difficoltà (Braiins, LuxOS, Canaan) un miner BCH veniva confrontato con il target BTC: **un blocco BCH reale non avrebbe mai fatto scattare la notifica** |

Il terzo era il più grave: gli altri due mostrano un numero sbagliato, quello
perde silenziosamente l'evento per cui esiste tutto il progetto.

## 2. Il modello, e perché va partizionato

Un miner che prova hash è un processo di Poisson. Per uno share di difficoltà
`D` servono in media `D · 2^32` hash, quindi un fleet a `H` hash/s li produce a:

```
rate = H / (D · 2^32)
E[T] = 1 / rate
P(t) = 1 - exp(-rate · t)
```

Trovare un blocco è il caso in cui `D` è la difficoltà di rete.

`rate` ha senso **solo se `H` e `D` descrivono la stessa catena**. Un fleet
diviso tra BTC e BCH sono due processi indipendenti che girano in parallelo:
collassarli in uno solo non dà un numero impreciso, dà un numero che non
appartiene a nessuna delle due monete. Da qui la partizione: si raggruppa il
fleet per moneta e si applica la formula una volta per gruppo, ciascuno con il
proprio hashrate e la propria difficoltà.

**"Beat all-time best" resta invece fleet-wide**, ed è voluto: la difficoltà di
uno share misura quanto in basso è finito un hash rispetto al target, che è una
proprietà del solo hash. Qualunque miner SHA-256 su qualunque catena può battere
il record, quindi tutto l'hashrate conta legittimamente.

## 3. Rilevamento della moneta

`backend/coin.py`, cascata a quattro livelli — vince il primo che risponde con
sicurezza. Ogni risposta porta con sé la **fonte**, esposta in API come
`coin_source` così la UI può distinguere "ce l'ha detto la pool" da "l'abbiamo
indovinato dall'hostname".

1. **`override`** — l'utente l'ha dichiarato (`miners.coin_override`). Vince
   sempre, anche su una lettura stratum viva, perché è la via di fuga per
   qualsiasi cosa i passi automatici sbaglino. Vale anche a miner spento, così
   un device offline non sfarfalla su "unknown".
2. **`stratum`** — il miner riporta la difficoltà di rete contro cui sta minando
   (AxeOS `networkDifficulty`, NMAxe `networkDiff`). Si confronta il valore con
   le difficoltà di riferimento in **scala logaritmica**: conta il *rapporto*,
   non la distanza assoluta, che altrimenti favorirebbe sempre la moneta più
   grande. Tolleranza 0.7 decadi (~5× in entrambi i sensi) — larghissima
   rispetto a qualunque retarget, strettissima rispetto ai ~3 ordini di
   grandezza che separano BTC e BCH. È il segnale automatico più forte: viene
   dalla pool, e segue un failover su un'altra catena immediatamente.
3. **`address`** — la user della pool spesso inizia con l'indirizzo di payout, e
   due formati dichiarano la catena senza ambiguità: il prefisso CashAddr
   `bitcoincash:` è BCH, un bech32 `bc1` è BTC. Gli indirizzi legacy `1…`/`3…`
   sono validi su **entrambe** le catene e non vengono mai usati; idem CashAddr
   "nudo" senza prefisso.
4. **`pool`** — match sui token dell'hostname (`bch.example.org`,
   `bitcoincash-solo.example`). "bitcoincash"/"bcash" sono testati prima di
   "bitcoin"/"btc" così un host BCH non può essere letto come BTC. È il segnale
   più debole, ed è la ragione per cui esiste l'override.

Se nessun passo risponde, il miner resta **non classificato** (`None`).

### 3.1 Perché "non classificato" e non un'ipotesi

Attribuire hashrate sconosciuto a una moneta ne gonfia le probabilità in
silenzio; lasciarlo fuori produce invece un gruppo visibile in UI, a un click
dall'essere sistemato. Il gruppo `coin: null` mostra il suo hashrate ma **nessuna
stima**, e il suo hashrate non entra in nessun altro gruppo. Compare comunque nel
totale fleet, così i numeri che l'utente vede continuano a tornare.

### 3.2 Chi ha davvero bisogno dell'override

Solo la famiglia di driver Bitaxe (`BitaxeDriver` e le sue sottoclassi:
NerdQAxe/NMAxe, NerdOctaxe, BitForge) riporta oggi una difficoltà stratum.
**Braiins, LuxOS e Canaan** lasciano `network_difficulty` a `None`, quindi
dipendono dai passi 3–4 o da un override esplicito. Un fleet interamente
Bitaxe-family non vedrà mai lo stato "non classificato".

## 4. Difficoltà di riferimento

`backend/coin_difficulty.py` già recuperava la difficoltà per moneta da un
explorer pubblico con cache TTL a 15 minuti. Aggiunti:

- `warm_cache()` — refresh best-effort di tutte le monete;
- `cached_references()` — mappa moneta → difficoltà, **senza rete**;
- `references()` — come sopra ma con refresh prima.

Il poller chiama `warm_cache()` in **fire-and-forget** a ogni ciclo (guardia
"in volo" per non impilare task): la cache TTL fa sì che di solito non tocchi la
rete, e un explorer lento non può mai ritardare un poll. Così i consumer che non
possono bloccare — `/api/halo` a 1 Hz, la tabella Pools, il check block-found —
trovano sempre un riferimento pronto.

Nota: la difficoltà **stratum del gruppo batte il riferimento** quando c'è. È il
valore con cui la pool sta effettivamente valutando gli share di quei miner, non
costa una chiamata di rete, e rende la feature più robusta di prima invece che
più fragile. Con più miner sulla stessa moneta si prende la più alta: a cavallo
di un retarget è la più fresca (un miner non ancora riconnesso può ancora
pubblicizzare l'epoca precedente).

## 5. Forma dell'API

`GET /api/fleet/prediction?coin=auto|btc|bch`

- **`auto`** (default) — le probabilità reali. `groups[]` contiene una voce per
  moneta effettivamente minata, ciascuna con `hashrate_ths`, `miner_count`,
  `miner_ids`, `network_difficulty`, `difficulty_source`
  (`stratum` | `explorer`), `coin_sources[]` e `find_block`.
- **`btc` / `bch`** — un *what-if*: l'hashrate **totale** del fleet contro la
  difficoltà di quella moneta, per rispondere a "quanto guadagnerei spostando
  tutto sull'altra catena". `groups[]` viene restituito comunque.

`predictions.find_block` e `network_difficulty` di primo livello restano
popolati (gruppo dominante in modalità auto, what-if in modalità forzata) per
**retrocompatibilità**: durante un aggiornamento Umbrel può essere servito per
un attimo un bundle vecchio, e un frontend che non conosce `groups` deve
continuare a funzionare.

I gruppi arrivano ordinati per hashrate decrescente, con il non classificato
**sempre in fondo** a prescindere dalla dimensione: è una cosa da sistemare, non
un risultato, e non deve scavalcare una moneta vera nella striscia di sotto-tab.

## 6. UI

**Analytics → Predictions.** In modalità Auto il blocco "Find a block (solo)"
mostra una sotto-tab per moneta rilevata, ciascuna con hashrate e numero di
miner del gruppo, e la difficoltà usata nel sottotitolo. Con un solo gruppo la
striscia sparisce e l'aspetto è quello di sempre. Il toggle BTC/BCH resta e
diventa il what-if, etichettato esplicitamente ("your entire N TH/s pointed
at …") così non si confonde con le stime reali. Il gruppo non classificato
mostra un badge "Not counted" e rimanda alla pagina Pools.

**Pools.** Colonna "Coin" con badge (`pinned` / `detected` / `Not set`), tooltip
che spiega da dove viene la risposta, e picker inline Auto/BTC/BCH. È la pagina
giusta perché è dove l'informazione nasce: la pool a cui punta un miner è ciò
che decide dove va il suo hashrate.

## 7. Correzioni collegate

**Halo** (`backend/halo.py`). Il gauge disegna `last_diff` contro `net_diff`,
quindi i due devono appartenere alla stessa catena. Ora `net_diff` segue la
moneta del **miner che ha prodotto lo share mostrato**: prima la sua lettura
stratum, poi il riferimento della sua moneta, e solo in mancanza di entrambi si
torna al vecchio comportamento fleet-wide (che resta il default quando
`references` non viene passato, es. cache fredda). `_pick_last_share` ora
restituisce anche il `miner_id`.

**Block-found** (`backend/poller.py`). Il fallback risolve prima la moneta del
miner e usa la difficoltà di quella; si passa da `alerts.get_network_difficulty()`
(solo Bitcoin) unicamente quando la moneta è davvero ignota.

## 8. Test

- `tests/test_coin.py` — cascata di classificazione, match logaritmico e suoi
  rifiuti, formati di indirizzo, token hostname, selezione della pool attiva.
- `tests/test_predictions.py` — verifica della forma chiusa del Poisson, split
  per moneta, sorgente della difficoltà per gruppo, isolamento del gruppo
  ignoto, chiavi legacy, modalità what-if.
- `tests/test_coin_api.py` — validazione del payload, round-trip DB,
  idempotenza della migration.
- `tests/test_halo.py` — nuovi casi per `net_diff` su fleet misto, più il
  fallback fleet-wide senza `references`.

## 9. Cose deliberatamente NON fatte

- **Split pesato sul tempo.** Lo split hashrate è istantaneo, coerente con il
  resto del widget che è live. "Quanto hash ho dato a ciascuna moneta nell'ultima
  ora" richiederebbe una media pesata sui sample storici ed è un lavoro diverso.
- **Record per moneta.** "Beat all-time best" resta fleet-wide (vedi §2).
  Renderlo per-moneta richiederebbe di taggare gli share salvati e varrebbe solo
  da lì in avanti.
- **Monete oltre BTC/BCH.** La cascata e la mappa degli endpoint sono già
  generiche su `coin.COINS`; aggiungere una catena SHA-256 significa aggiungere
  un endpoint explorer e un token hostname, non toccare la logica.
