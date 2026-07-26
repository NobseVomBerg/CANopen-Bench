# A-04 — Testfall-Ausführung

> **Status:** Der Step-Executor ist umgesetzt (`Bench._run_task`/`_exec_case`
> in `core.py`, Katalog-Lader `canopen_bench/testcases.py`) und seit
> Format v2 eine kleine VM: Programmzähler, Register R0–R9, Sprünge und
> Arithmetik, Schrittzähler-Guard (`_run_program`/`_exec_one`). Dieselbe VM
> führt auch die Ablauf-Dateien der Machine Control aus (A-05 Teach).
> Die [Ist]-Markierungen unten beschreiben den implementierten Stand;
> der `data.TESTS`-Fallback läuft weiterhin über die alte Tick-Simulation.

## Zweck und Auslöser

Lebenszyklus eines Testlaufs auf der Tests-Seite: ausgewählte Testfälle
in definierter Reihenfolge ausführen, je Testfall ein Verdikt bilden,
am Ende einen Report erzeugen. Die Ausführung läuft über den
Step-Executor (`_run_step`-Tick-Simulation aus `data.FAILING_TESTS`
bleibt nur als Fallback, wenn kein Testfall-Katalog geladen ist).

- Auslöser: Run-Button → `act_run_start` (`core.py`),
  Stop → `act_run_stop` (`core.py`).

## Vorbedingungen

- Verbunden (A-01) und mindestens ein Gerät gescannt/ausgewählt — der
  Executor braucht ein Ziel für die Bus-Primitive.
- Testfälle ausgewählt (`test_sel`); Wiederholungen (`repeat_case`,
  `repeat_run`) und `stop_on_err` wie gewünscht.

## Akteure

UI → `Bench.act_run_start` → Step-Executor →
`BusInterface` (nmt/sdo_read/sdo_write/poll_frames) → DUT; bei
`manual`-Schritten zusätzlich der Bediener.

## Ablauf

1. **Katalog laden [Ist]:** Die Tests-Seite listet die `.yaml`-Dateien
   aus dem konfigurierten TestCases-Ordner (`paths["tc"]`,
   Format siehe [`testfall-format.md`](testfall-format.md)); Anzeige von
   `id`, `name`, `tools`, `est` wie heute aus `data.TESTS`. Fehlt der
   Ordner oder ist er leer, bleibt `data.TESTS` als Demo-Katalog aktiv.
   Nicht parsebare Dateien erscheinen im Katalog als fehlerhaft markiert
   und sind nicht wählbar (Fehlerpfad 1).
2. **Expansion [Ist]:** `act_run_start` expandiert die Auswahl zu
   `run_order`: für jeden Run-Durchlauf (`repeat_run`) jede gewählte ID
   × `repeat_case`. Log `RUN  started — N test cases`.
3. **Je Testfall — Vorbedingungen [Ist]:** die `preconditions`-Schritte
   der Testdatei ausführen. Schlägt eine fehl, ist das Verdikt **SKIP**
   (nicht FAIL): der Testfall wurde nicht ausgeführt, weil sein Umfeld
   nicht stimmt. SKIP zählt im Report gesondert und löst `stop_on_err`
   nicht aus.
4. **Je Testfall — Schritte [Ist]:** die `steps` sequenziell ausführen
   (Semantik der Primitive in `testfall-format.md`). Der erste
   fehlgeschlagene Erwartungs-Schritt bricht den Testfall mit **FAIL**
   ab; technische Fehler (Ausnahme, Verbindungsverlust, `manual` ohne
   Bestätigung) ergeben **ERROR**. Laufen alle Schritte durch: **PASS**.
5. **Ergebnis [Ist-Format]:** `results[tid]`, Logzeile
   `TEST <tid> passed` / `TEST <tid> FAILED` (Typ `test` bzw. `emcy0`).
6. **stop_on_err [Ist]:** bei FAIL und ERROR mit gesetzter Option: Lauf
   abbrechen, Log `RUN  aborted — stop on error (after k of N)`, Report
   über die gelaufenen Fälle.
7. **Abschluss [Ist]:** `RUN  finished — report created`, Report via
   `_push_report` (`core.py`) mit Score `passed/total` in die
   Report-Historie.

### Step-Executor **[Ist]**

Die frühere Tick-getriebene Simulation ist durch einen **eigenen
asyncio-Task pro Lauf** ersetzt (nicht durch mehr Tick-Logik):

- Schritt-Timing (`wait`, `wait_for`-Timeouts, SDO-Dauer) ist vom
  0,8-s-Raster unabhängig; ein Task mit `await` bildet das natürlich ab.
- Blockierende Bus-Aufrufe laufen über `asyncio.to_thread` (dasselbe
  Muster wie für den Scan, A-02/F-2).
- Der Task prüft zwischen den Schritten ein Abbruch-Flag
  (`act_run_stop` setzt es; Verdikt des angebrochenen Falls: ERROR mit
  Log `stopped by user`).
- Fortschritt wandert in den Snapshot: `{tid, step, of, text}` — die UI
  zeigt daraus die Zeile `TEST 4602 step 3/9  <text>` (wie im
  Design-Mockup) und den Balken.
- `manual`-Schritte setzen den Zustand `waiting_operator` mit dem
  Anweisungstext; die UI zeigt einen Bestätigen/Abbrechen-Dialog, der
  über neue Aktionen `act_manual_confirm` / `act_manual_abort` auflöst.
- Nur die Tests-Seite ist auf den Executor umgestellt; SWDL- und
  Trace-Simulation in der Tick-Loop bleiben unberührt.

## Timing und Timeouts

- Schrittdauer bestimmen die Primitive (SDO ~0,3 s Timeout je Versuch,
  `wait`/`wait_for` explizit).
- `manual` ohne Reaktion: Timeout konfigurierbar je Schritt, Default
  120 s → ERROR (verhindert unbemerkt hängende Läufe).

## Verdikte

| Verdikt | Bedeutung | löst `stop_on_err` aus |
|---|---|---|
| PASS | alle Schritte erfolgreich | nein |
| FAIL | eine Erwartung (`expect`/`expect_abort`/`wait_for`) nicht erfüllt | ja |
| ERROR | technischer Fehler: Ausnahme, Verbindungsverlust, `manual`-Timeout/-Abbruch, Datei fehlerhaft | ja |
| SKIP | Vorbedingung nicht erfüllt — Testfall nicht ausgeführt | nein |

## Fehlerpfade

| # | Fehler | Soll-Verhalten |
|---|---|---|
| 1 | Testdatei nicht parsebar (YAML-/Schemafehler) | beim Katalog-Laden markieren, nicht wählbar; gerät sie dennoch in einen Lauf (Datei zwischenzeitlich geändert): Verdikt ERROR |
| 2 | Kein Gerät ausgewählt / `dut`-Rolle nicht auflösbar | Lauf startet nicht, Log `RUN  no target device` (`emcy0`) |
| 3 | Verbindungsverlust während des Laufs | laufender Testfall ERROR, Lauf abbrechen (unabhängig von `stop_on_err`), Log |
| 4 | Server-Shutdown während des Laufs | Executor-Task wird gecancelt; kein Report-Torso, Log wie `act_run_stop` |

## Beobachtbares Ergebnis

- Ergebnis-Badges je Testfall, Fortschrittszeile `step k/n`, Lauf-Log,
  Report-Eintrag `run_MMDD_HHMM.html` mit Score in der Historie.

## Verifikation ohne Hardware

Der Executor spricht ausschließlich `BusInterface` — damit ist er komplett
ohne Adapter testbar:

- Unit-/Service-Tests gegen `EdsDemoBus` (virtuelle DUTs aus echten
  EDS-Dateien) analog `tests/test_demo.py`.
- Protokoll-Ende-zu-Ende gegen `CanopenBus` + python-can-`virtual`-Bus
  mit `canopen.LocalNode` als Gegenstelle, analog
  `tests/test_canopen_bus.py` — so lässt sich z. B. ein kompletter
  YAML-Testfall inklusive `wait_for`-Heartbeat real durchspielen.
