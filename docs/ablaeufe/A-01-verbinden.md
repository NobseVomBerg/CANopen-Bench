# A-01 — Verbinden / Trennen

## Zweck und Auslöser

Aufbau und Abbau der Verbindung zum CAN-Adapter. Erst danach sind Scan,
SDO/NMT und Trace möglich — das Tool startet grundsätzlich offline.

- Auslöser: Setup-Seite „connect"-Button → `act_connect_toggle`
  (`core.py`), außerdem `act_set_bitrate` (Bitrate-Änderung bei laufender
  Verbindung wendet sich automatisch an: Reconnect mit der neuen Bitrate,
  Log `BUS  bitrate applied — reconnected @ <rate> kbit/s`, bei Fehler
  `BUS  reconnect failed — …` und Trennen) und `Bench.shutdown`
  (`core.py`, Trennen bei Server-Stopp). Ein Adapterwechsel trennt
  stattdessen automatisch (kein Auto-Reconnect).
- Adapterwahl (`cpc` / `ixxat` / `pcan` / `demo`) und Bitrate kommen aus dem
  Setup-Zustand; `demo` geht am Hardware-Pfad vorbei zum `EdsDemoBus`
  (`Bench.bus`-Property, `core.py`).

## Vorbedingungen

- Hardware-Adapter (CPC/IXXAT/PCAN) auf der Setup-Seite gewählt — jeder
  Hardware-Adapter geht immer auf den echten `CanopenBus`; ohne Hardware
  ist „Demo mode" der Weg (ein separater Simulator existiert nicht mehr).
- IXXAT: Windows-PC, **VCI4-Treiber installiert**, Adapter am USB, Kanal
  nicht von einem anderen Programm (canAnalyser o. ä.) belegt.
- Bitrate passend zum Bus konfiguriert (Standard 500 kbit/s). Achtung:
  eine falsche Bitrate fällt beim Verbinden **nicht** auf — siehe
  Fehlerpfade.

## Akteure

UI → `Bench.act_connect_toggle` → `CanopenBus.connect`
(`canopen_bench/bus/canopen_bus.py`) → python-can-Backend
(`ixxat` = IXXAT-VCI-Backend von python-can) → Adapter.

## Ablauf: Verbinden

1. **[Ist]** `act_connect_toggle` setzt `connected = True` und ruft
   `bus.connect(adapter, bitrate)`.
2. **[Ist]** `CanopenBus.connect` mappt den Adapter-Key auf das
   python-can-Backend: eingebaut sind `ixxat → ("ixxat", 0)` und
   `pcan → ("pcan", "PCAN_USBBUS1")` (`_ADAPTER_BACKENDS`,
   `canopen_bus.py`); Plugin-Adapter steuern ihr Mapping über
   `BenchPlugin.adapter_backends()` bei (z. B. `cpc → ("cpcusb", None)`
   aus dem Paket `cob-cpcusb`, das Karte und Treiber gemeinsam
   trägt). Besteht bereits eine Verbindung, wird sie zuerst getrennt.
3. **[Ist]** `canopen.Network()` wird erzeugt, der `_TraceListener`
   **vor** `connect()` registriert (sonst verpasst der Notifier-Thread ihn),
   dann `network.connect(interface=…, channel=…, bitrate=bitrate·1000)`
   (App rechnet in kbit/s, python-can in bit/s).
4. **[Ist]** python-can öffnet das Gerät, initialisiert den Controller mit
   der Bitrate und startet den Notifier-Thread; ab jetzt landen alle
   empfangenen Frames im Trace-Puffer.
5. **[Ist]** Log `BUS  connected — <Adapter> @ <Bitrate> kbit/s`; die
   Tick-Loop (`core.py`) beginnt, `poll_frames()` in den Trace zu
   schieben.

## Ablauf: Trennen

1. **[Ist]** `act_connect_toggle` setzt `connected = False`, ruft
   `bus.disconnect()` (→ `network.disconnect()`, Notifier-Thread stoppt,
   Gerät wird freigegeben), leert die Geräteliste, Log `BUS  disconnected`.
2. **[Ist]** Beim Server-Shutdown identisch über `Bench.shutdown`
   (Log `BUS  disconnected — server shutdown`).
3. **[Ist]** `CanopenBus.disconnect` ist gegen ein totes Interface
   gehärtet: schlägt `network.disconnect()` fehl (jeder Teilschritt kann
   am abgezogenen Adapter werfen; `Network.check()` wirft am Ende sogar
   die gespeicherte Notifier-Exception erneut), werden Notifier und Bus
   einzeln best-effort gestoppt (`_shutdown_network`,
   `canopen_bus.py`). Ein manuelles Trennen nach Adapterverlust bleibt
   damit immer möglich.

## Ablauf: Verbindungsverlust (Auto-Disconnect)

Wird der Adapter im laufenden Betrieb abgezogen (oder der Treiber-Port
geschlossen), wirft `bus.recv()` im Notifier-RX-Thread — beim IXXAT z. B.
`VCIError: function canControlGetStatus failed (Attempt to send a message
to a disconnected communication port.)`. Früher starb dieser Thread mit
Traceback auf stderr, die App blieb scheinbar „verbunden", und weil
`canopen.Network.send_message` nach jedem Senden `check()` aufruft, warf
anschließend **jede** SDO/NMT-Operation dieselbe Exception (bis zur API
als HTTP 500). Jetzt:

1. **[umgesetzt]** Ein `_ErrorListener` am Notifier (registriert in
   `CanopenBus._install_listeners`, vor `network.connect()`) fängt den
   RX-Fehler ab → `CanopenBus._connection_lost(exc)`.
2. **[umgesetzt]** `_connection_lost` ist idempotent (nur der erste
   Melder wirkt), hängt das tote `network` sofort aus und räumt auf
   einem eigenen Thread auf — `Notifier.stop()` joint den RX-Thread und
   darf deshalb nie auf diesem selbst laufen. Zuerst wird
   `BusInterface.on_lost(reason)` gerufen, dann das Netz abgebaut.
3. **[umgesetzt]** Auch die Sendepfade melden den Verlust: `sdo_read`/
   `sdo_write` fangen `(can.CanError, OSError, RuntimeError)` und liefern
   `abort = "connection lost"`, `nmt`/`send_raw` schlucken den Fehler nach
   der Meldung, `scan`/`lss_readdress` werfen `ConnectionError` („SCAN
   failed — CAN interface lost" im Log).
4. **[umgesetzt]** `Bench._on_bus_lost` (Callback, threadsicher über
   `loop.call_soon_threadsafe`) setzt `connected = False`, leert die
   Geräteliste, loggt `BUS  connection lost — <Fehlertext> —
   auto-disconnected` (`emcy0`) und pusht den Snapshot an die Browser.
   Ein laufender Testlauf bricht über die bestehende
   `connected`-Prüfung im Step-Executor mit „connection lost" ab.

## Timing und Timeouts

- `connect()`/`disconnect()` laufen synchron im Event-Loop; am echten
  Adapter typischerweise < 1 s. Länger blockierende Treiber wären ein
  Grund, auf `asyncio.to_thread` auszuweichen (wie für den Scan gefordert,
  siehe A-02 / F-2) — erst am realen Gerät bewerten.

## Fehlerpfade

| # | Fehler | Symptom | Soll-Verhalten |
|---|---|---|---|
| 1 | VCI4-Treiber fehlt | python-can wirft beim Backend-Laden (`CanInterfaceNotImplementedError`/ImportError) | **[umgesetzt]** Fehler wird gefangen, Log `BUS  connect failed — <Fehlertext>` (`emcy0`), Zustand bleibt getrennt |
| 2 | Adapter nicht gesteckt | VCI-Fehler „device not found" beim Öffnen | wie 1 (gleicher Fangpfad) |
| 3 | Kanal belegt (anderes Tool) | VCI-Fehler beim Öffnen des Kanals | wie 1 (gleicher Fangpfad) |
| 4 | Falsche Bitrate | **Verbinden gelingt fehlerfrei.** Probleme erst beim ersten Sendeversuch: kein ACK → TX-Error-Counter steigt → Controller error-passive/bus-off | Erkennung gehört in den Scan (A-02, Fehlerpfad 1), nicht ins Verbinden |
| 5 | Adapter im laufenden Betrieb abgezogen | `bus.recv()` im Notifier-RX-Thread wirft (IXXAT: `VCIError … disconnected communication port`); danach würfe jede Bus-Operation dieselbe Exception | **[umgesetzt]** Fehler wird gefangen und löst den Auto-Disconnect aus — siehe „Ablauf: Verbindungsverlust"; Log `BUS  connection lost — <Fehlertext> — auto-disconnected` (`emcy0`) |

**Befund F-3 — BEHOBEN:** `act_connect_toggle` setzte `connected = True`,
*bevor* `bus.connect()` lief; ein Treiberfehler ließ die UI fälschlich
auf „verbunden" stehen (und schlug als HTTP 500 bis zur API durch).
Umsetzung: `try/except` um `bus.connect()` in `act_connect_toggle` und um
den Reconnect-Pfad in `act_set_bitrate` — bei Fehler bleibt/wird der
Zustand getrennt, Log `BUS  connect failed — <exc>` bzw.
`BUS  reconnect failed — <exc>` (`emcy0`), keine Exception zur
API-Schicht (Test: `test_connect_failure_leaves_tool_disconnected`).

**Offener Punkt (am Gerät bestätigen):** `_ADAPTER_BACKENDS` übergibt
den IXXAT-Kanal jetzt als Integer `0` (python-can-Doku nutzt int) —
beim ersten Verbindungsversuch bestätigen. Außerdem klären, wie bei
**mehreren** gesteckten IXXAT-Adaptern der richtige gewählt wird
(python-can-Parameter `unique_hardware_id`) — bis dahin gilt: genau ein
Adapter am PC.

## Beobachtbares Ergebnis

- `state.connected` im Snapshot, Button-Zustand in der UI.
- Logzeilen wie oben; bei aktivem Bus füllt sich die Trace-Seite.

## Abnahme-Checkliste IXXAT

Der IXXAT ist am Bench-PC im regulären Einsatz; diese Liste ist nie
förmlich abgehakt worden und dient als Vorlage für eine dokumentierte
Abnahme oder die Inbetriebnahme eines weiteren Adapters.

- [ ] VCI4 installiert, Adapter im Windows-Geräte-Manager sichtbar.
- [ ] `python -m canopen_bench`, Adapter „IXXAT" wählen, 500 kbit/s,
      verbinden → Log `BUS  connected`, keine Exception im Server-Log.
      (Kanal wird als int `0` übergeben — siehe offener Punkt.)
- [ ] Mit sendendem Gerät am Bus: Trace zeigt Frames mit plausibler
      COB-ID-Dekodierung.
- [ ] Trennen und erneut Verbinden funktioniert (Kanal wird sauber
      freigegeben).
- [ ] Negativtest: Adapter abziehen, verbinden → definierte Fehlermeldung
      (`BUS  connect failed — …`) statt hängender „verbunden"-Anzeige.
- [ ] Negativtest: im **verbundenen** Zustand Adapter abziehen → Tool
      trennt automatisch (Log `BUS  connection lost — … —
      auto-disconnected`, UI zeigt getrennt, Geräteliste leer), kein
      Traceback `Exception in thread Notifier` im Server-Log; erneutes
      Verbinden nach Wiederanstecken funktioniert.
