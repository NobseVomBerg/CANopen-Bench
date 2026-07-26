# A-02 — Scan

## Zweck und Auslöser

Den CAN-Bus nach CANopen-Knoten absuchen, jeden Responder identifizieren
(Identity-Objekt 0x1018) und automatisch das passende EDS-Profil zuordnen.
Der Scan ist die Grundlage für alles Weitere: Geräteliste, Objects-Seite,
Testläufe, Machine-Control-Verify (A-03).

- Auslöser: „Scan"-Button in der Devices-Box (`send('scan')`,
  `static/app.js`) → `act_scan` (`canopen_bench/core.py`).
- [Soll] Außerdem als erster Schritt von „Scan & verify" (A-03).

## Vorbedingungen

- Verbunden (A-01); `act_scan` bricht sonst kommentarlos ab (Guard
  `connected`/`scan_busy`).
- DUTs versorgt und in einem Zustand, in dem SDO beantwortet wird
  (Pre-Operational oder Operational — **nicht** Stopped, siehe
  Fehlerpfad 3).
- Für die EDS-Auto-Zuordnung: passende EDS-Dateien hochgeladen und
  aktiviert (enabled).

## Akteure

UI → `Bench.act_scan` → `CanopenBus.scan` (`bus/canopen_bus.py`,
nutzt `canopen.Network.scanner`) → Adapter → alle DUTs.

## Ablauf

### Phase 0 — Orchestrierung [Ist]

`act_scan` setzt `scan_busy`, loggt `SCAN node-id <von>…<bis>` (die im
Setup konfigurierte, in der DB persistierte Scan-Range) und startet
einen asyncio-Task: Die künstliche Verzögerung `SCAN_DELAY_S = 1.1 s`
(`core.py`) gilt nur noch für simulierte Busse
(`BusInterface.simulated`); am echten Adapter läuft die gesamte
Busarbeit (Probe, Identity- und Varianten-Reads) ohne Kunstpause in
einem Worker-Thread (`asyncio.to_thread`, siehe F-2). Wirft die
Busarbeit eine Ausnahme, wird `SCAN failed — <fehler>` (`emcy0`)
geloggt statt den Server zu treffen.

### Phase 1 — Passiv mithören **[Soll]**

Bevor aktiv gesendet wird, ca. 1–2 s den Empfang beobachten (der
Trace-Listener läuft bereits):

- **Heartbeats** `COB-ID 0x700+Node`, 1 Datenbyte = NMT-Zustand:
  `0x00` Boot-up, `0x04` Stopped, `0x05` Operational, `0x7F`
  Pre-Operational.
- Nutzen: (a) Knoten im **Stopped**-Zustand werden erkannt, obwohl sie auf
  SDO nicht antworten; (b) der NMT-Zustand kommt direkt aus dem Heartbeat
  statt aus Vermutung; (c) funktioniert ohne jede Buslast.
- Grenze: Geräte ohne konfigurierten Heartbeat-Producer (0x1017 = 0)
  bleiben hier unsichtbar — deshalb nur Ergänzung zur aktiven Probe, kein
  Ersatz.

### Phase 2 — Aktive Probe [Ist]

`net.scanner.reset()` + `net.scanner.search(limit=127)`
(`canopen_bus.py`): sendet an jede Node-ID 1…127 einen
SDO-Upload-Request auf **0x1000:00** (Device Type) — Frame
`40 00 10 00 00 00 00 00` auf `COB-ID 0x600+i`. Antworten (`0x580+i`)
sammelt der Scanner in `scanner.nodes`. Danach `_SCAN_SETTLE_S = 0.5 s`
Wartezeit, damit langsame Antworten noch eintreffen.

Buslast: 127 Frames à ~111 Bit ≈ 28 ms bei 500 kbit/s (≈ 112 ms bei
125 kbit/s) — unkritisch, aber der Burst kommt ohne Pausen.

**[Soll]** Settle-Zeit und eine einmalige Wiederholung der Probe für
Knoten, die erst in der passiven Phase gesehen wurden, aber nicht auf die
Probe geantwortet haben.

### Phase 3 — Identifikation je Responder [Ist]

Für jede antwortende Node-ID aufsteigend (`canopen_bus.py`):

| Schritt | Objekt | Pflicht lt. CiA 301 | bei Fehlschlag |
|---|---|---|---|
| Name | 0x1008:00 (Manufacturer Device Name, String) | optional | Platzhalter `node NN` |
| FW-Version | 0x100A:00 (Manufacturer Software Version) | optional | `?` |
| Seriennummer | 0x1018:04 (Serial Number, U32) | optional | `?` |
| Vendor-ID | 0x1018:01 (U32) | **Pflicht** | Identity `?` |
| Product-Code | 0x1018:02 (U32) | optional | Identity `?` |

Identity-Signatur: `"<vendor>·<product>"` als Hex. NMT-Zustand aus
`node.nmt.state` (canopen speist das aus empfangenen Heartbeats — ohne
Heartbeat bleibt er `INITIALISING`, siehe Fehlerpfad 4).

### Phase 4 — EDS-Zuordnung + Variante [Ist]

Identity gegen die **aktivierten** EDS-Registry-Einträge matchen
(`_scan_async`/`_apply_scan`, `act_scan` selbst spawnt nur den Task).
Beanspruchen mehrere
aktivierte EDS-Dateien dieselbe Identity, gewinnt deterministisch die
**neueste Datei** (mtime); die EDS-Liste im Setup markiert alle
Beteiligten mit einer Konflikt-Warnung (⚠), der Scan loggt den Konflikt.
Bei Treffer: EDS zuweisen, Log
`SCAN identity 0x1018 node NN → <identity> ⇒ <datei>`, und
Variantenerkennung `_read_variant` (`core.py`): SDO-Read des per EDS
konfigurierten Objekts, Wert → Label über `variant_map`. Ohne Treffer:
EDS `—`, Log `… — no active EDS match`.

### Phase 5 — Ergebnis [Ist]

Geräteliste wird ersetzt; Auswahl-/SuperUser-Status bereits bekannter
Nodes bleibt erhalten (`prev`, `core.py`). Abschluss-Log
`SCAN done — N devices found, EDS auto-assigned`, Snapshot-Push.

## Timing und Timeouts

| Größe | Wert | Quelle |
|---|---|---|
| künstliche Verzögerung | 1,1 s | `SCAN_DELAY_S`, nur noch simulierte Busse (`bus.simulated`) |
| Settle nach Probe | 0,5 s | `_SCAN_SETTLE_S` |
| SDO-Response-Timeout | 0,3 s je Versuch (canopen-Default) | jede fehlende optionale sub kostet also ~0,3 s pro Gerät |
| Gesamtdauer real | ≈ 0,5 s + n_Geräte × (2–5 SDO-Reads) | wächst mit Anzahl fehlender optionaler Objekte |

## Fehlerpfade

| # | Fehler | Ist-Verhalten | Soll-Verhalten |
|---|---|---|---|
| 1 | **Kein einziger Responder** — Ursachen nicht unterscheidbar am Ergebnis: keine Geräte versorgt, falsche Bitrate (kein ACK → TX-Error → Controller error-passive/bus-off), Verkabelung/Terminierung | **[umgesetzt]** `BusInterface.bus_state()` (Default `""`, `CanopenBus` via python-can `bus.state`): bei `passive`/`error` Log `SCAN 0 found — bus errors detected, check bitrate/wiring` (`emcy0`), sonst `SCAN done — 0 devices found` | am IXXAT verifizieren, ob das Backend `bus.state` liefert; sonst bleibt der neutrale Text |
| 2 | SDO-Timeout einzelner Identity-Reads | Feld `?` bzw. Platzhalter, kein Retry (`_try_upload` schluckt Abort *und* Timeout gleich) | 1 Retry nur bei Timeout (Abort = definitive Antwort, kein Retry); bleibt der Pflicht-Read 0x1018:01 leer → Logzeile je Knoten |
| 3 | Knoten im **Stopped**-Zustand | unsichtbar (antwortet nicht auf SDO) | passive Phase 1 erkennt ihn am Heartbeat und listet ihn mit NMT `Stopped`, Identity `?` |
| 4 | Kein Heartbeat konfiguriert (0x1017 = 0) | NMT-Spalte zeigt `INITIALISING` (nie ein Heartbeat gesehen) | als `?` darstellen statt eines falschen Zustands; optional später: Node-Guarding-RTR auf 0x700+Node (nicht jedes Gerät unterstützt RTR — nur als Option) |
| 5 | **Doppelte Node-IDs** (zwei Geräte antworten auf dieselbe Probe) | SDO-Antworten kollidieren/verschachteln sich, Identity zufällig/inkonsistent | Erkennen an widersprüchlichen Antworten (z. B. wechselnde 0x1018-Werte bei Wiederholung) → Warn-Log, Verweis auf LSS-Umadressierung (A-03) |
| 6 | Mehrere aktivierte EDS mit derselben Identity | **[umgesetzt]** neueste Datei (mtime) gewinnt deterministisch (`_eds_by_identity`), Warn-Log beim Scan, ⚠-Badge an allen Beteiligten in der EDS-Liste | — |

### Befund F-1 — Identity-Format inkonsistent (Blocker für echte Hardware) — **BEHOBEN**

Drei Stellen erzeugten die Identity-Signatur in **drei verschiedenen
Formaten**:

- EDS-Upload (`add_eds_file`, `core.py`): `f"0x{vendor:04X}·0x{product:04X}"` → `0x00AF·0x2600`
- Hardware-Scan (`canopen_bus.py` via `_bytes_to_hex` einer 4-Byte-Antwort): immer 8 Hex-Stellen → `0x000000AF·00002600`
- historische Seed-Daten (früher in `data.py`, minimale Breite →
  `0xAF·2600`; `SEED_EDS_FILES` in `data.py` ist mittlerweile leer,
  Identity-Seeds kommen aus Plugin-`seed_eds()`, z. B. `bench-vendor`)

Folge wäre gewesen: **Am echten Adapter matcht nie ein EDS**. Im
Demo-Modus fiel das nicht auf, weil `EdsDemoBus` die Identity direkt aus
der Registry übernimmt.

Umsetzung: **kanonisches Format ist die minimale Hex-Breite ohne
führende Nullen** — `core.normalize_identity()`; EDS-Upload und
`CanopenBus.scan` erzeugen es direkt, jeder Vergleich (u. a. `act_scan`,
`_eds_by_identity`) normalisiert beide Seiten, sodass bereits gespeicherte
Einträge im alten Format ohne Migration weiter matchen (Test:
`test_scan_matches_registry_entries_stored_in_legacy_format`).

### Befund F-2 — Scan blockiert den Event-Loop — **BEHOBEN**

`done()` rief `self.bus.scan()` synchron im asyncio-Loop auf; am echten
Bus stecken darin `time.sleep(0.5)` plus alle SDO-Timeouts — WebSocket,
Trace und alle Aktionen hätten gestanden. Umsetzung: die gesamte
Busarbeit des Scans (Probe + Identity + Variante) läuft in
`asyncio.to_thread`; die Zustandsübernahme (`_apply_scan`) bleibt im
Loop. `CanopenBus` ist dafür geeignet, da canopen die Protokollarbeit
ohnehin im Notifier-Thread erledigt; `Db` ist mit
`check_same_thread=False` + Lock threadtauglich.

## Beobachtbares Ergebnis

- Geräteliste in der Devices-Box (Node, Name, NMT, FW, SN, Variante, EDS).
- Logzeilen: `SCAN node-id 1…127`, je Knoten die Identity-Zeile,
  abschließend `SCAN done — N devices found, EDS auto-assigned`.
- `state.scanBusy` während des Laufs (Button zeigt `…`).

## Abnahme-Checkliste IXXAT (F-1/F-2 sind umgesetzt — Checkliste ist bereit)

- [ ] 1 DUT, korrekte Bitrate: Scan findet das Gerät, Identity entspricht
      dem EDS (`⇒ <datei>` im Log), Variante wird gelesen.
- [ ] 2+ DUTs: alle gefunden, EDS je Gerät korrekt zugeordnet, Auswahl
      bleibt über einen zweiten Scan erhalten.
- [ ] Falsche Bitrate eingestellt: Scan meldet den Busfehler-Hinweis
      (Fehlerpfad 1, sofern das IXXAT-Backend `bus.state` liefert), Tool
      bleibt bedienbar.
- [ ] DUT ohne 0x1008/0x100A: Platzhalter statt Abbruch.
- [ ] DUT in Stopped versetzen (NMT stop), erneut scannen: Gerät bleibt
      dank passiver Phase gelistet (sofern Heartbeat aktiv).
- [ ] UI bleibt während des Scans responsiv (Trace läuft weiter).
