# A-04 — Testfall-Ausführung

> **Status:** Der Step-Executor ist umgesetzt (`Bench._run_task`/`_exec_case`
> in `core.py`, Katalog-Lader `canopen_bench/testcases.py`) und seit
> Format v2 eine kleine VM: Programmzähler, Register R0–R15, Sprünge und
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
   (Semantik der Primitive in `testfall-format.md`). Ein fehlgeschlagener
   Erwartungs-Schritt macht das Verdikt **FAIL**; der Fall läuft
   standardmäßig weiter (`on_fail`, Default `continue`), damit seine
   letzten Schritte den Prüfstand wieder herrichten können und
   `jump_on_error` dorthin springen kann. Ein explizites `fail:` beendet
   den Fall immer. Technische Fehler (Ausnahme, Verbindungsverlust,
   `manual` ohne Bestätigung) ergeben **ERROR** und brechen ab. Laufen
   alle Schritte ohne Fehlschlag durch: **PASS**.
5. **Ergebnis [Ist-Format]:** `results[tid]`, Logzeile
   `TEST <tid> passed` / `TEST <tid> FAILED` (Typ `test` bzw. `emcy0`).
6. **stop_on_err [Ist]:** bei FAIL und ERROR mit gesetzter Option: Lauf
   abbrechen, Log `RUN  aborted — stop on error (after k of N)`, Report
   über die gelaufenen Fälle. **Standardmäßig aus**: ein
   fehlgeschlagener Fall ist ein Ergebnis, kein Grund, über die
   restlichen nichts mehr zu erfahren.
7. **Abschluss [Ist]:** `RUN  finished — report created`, Report via
   `_push_report` (`core.py`) mit Score `passed/total` in die
   Report-Historie. Der Eintrag verlinkt die Datei — `GET
   /api/report/<name>` reicht sie aus dem Ergebnisordner heraus, unter
   einem gemeinsamen Präfix, damit die relativen Links *im* Report (die
   Fallseiten, das Stylesheet) weiter auflösen. Nur ein einfacher
   Dateiname aus genau diesem Ordner wird bedient, und nur `.html`,
   `.json`, `.css`. Die Beispielreports der Demo tragen keine Datei und
   werden deshalb als Text statt als Link gezeigt.

### Zustand auf den Schirm **[Ist]**

Der Executor meldet nach jedem Schritt eine Zustandsänderung, und eine
Meldung ist ein vollständiger Snapshot. Ungebremst waren das an drei
kurzen Fällen 3627 Pushes und 41 MB in fünf Sekunden: die Seite kam
nicht mehr zum Zeichnen, das Panel nannte bis zum Schluss den ersten
Fall, und der Lauf selbst dauerte siebzehnmal so lange.

`_changed()` fasst deshalb zusammen — einer unterwegs, und was während
dessen anfragt, wird zu *einem* nachlaufenden Push. Das Nachlaufen ist
der Punkt: die letzte Anfrage trägt „der Lauf ist fertig", und genau
die würde reines Wegwerfen verlieren. Der Tick-Loop geht durch dieselbe
Schleuse, damit Tick und laufender Fall nicht gleichzeitig auf denselben
Socket schreiben.

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
  Report-Eintrag mit Score in der Historie.
- Kasten „Overview by variant“ auf der Tests-Seite: Zeitraum wählen,
  erzeugen, danach je Variante Erfolgsquote und letztes Verdikt — die
  Datei selbst liegt bei den Reports.

## Reports **[Ist]**

Ein Lauf schreibt in den Results-Ordner (`paths["res"]`, sonst
`<workspace>/results`):

| Datei | Inhalt |
|---|---|
| `<stamp>__<tid>__<name>.html` | ein Testfall: Kopfdaten, dann je Schritt bis zu drei Zeilen — was lief (mit EDS-Objektnamen), die `note` des Autors, und was zurückkam (auch im Gutfall, mit Enum-Bedeutung wo bekannt). Buchhaltung des Falls (Label, Sprung, Registerrechnung) ist hellbraun abgesetzt, damit eine Schleife als Schleife lesbar bleibt |
| `<stamp>__summary.html` | der Lauf: eine Zeile je Testfall, verlinkt auf dessen Datei |
| `<stamp>__summary.json` | derselbe Lauf als Daten — Grundlage der Übersicht unten |
| `testReportStyle.css` | einmal geschrieben, von allen Reports verlinkt, **nie überschrieben** |

Schlägt das Schreiben fehl (volle Platte, ungültiger Pfad), ist das eine
Logzeile `RUN  report not written — …` und **kein** fehlgeschlagener
Lauf: die Verdikte stehen bereits im Log und auf dem Schirm.

### Übersicht nach HW-Varianten **[Ist]**

Aktion `report_overview` mit `{days: N}` (1…90, Vorgabe 7) faltet die
Läufe der letzten N Tage zu `__overview.html` zusammen — je
Hardware-Variante ein aufklappbarer Abschnitt mit Läufen, Erfolgen und
letztem Status je Testfall, verlinkt auf dessen jüngsten Einzelreport.
Sie beantwortet die Frage, die die Zusammenfassung eines einzelnen Laufs
nicht beantworten kann: *ist nur die 920 kaputt oder alle?*

- Gelesen wird `*__summary.json`, nicht das HTML, das verlinkt werden
  soll.
- Gefiltert wird über das `started` des Laufs, **nicht** über die
  Dateizeit: ein Results-Ordner wird kopiert und synchronisiert, mtime
  überlebt das nicht.
- Ein Gerät ohne gemeldete Variante wird unter seinem Gerätenamen
  gruppiert statt weggelassen — sonst wäre die Übersicht unbemerkt
  unvollständig.
- Das Verdikt einer Variante ist das des **jüngsten** Laufs, kein
  Mittelwert: „12 von 14 bestanden“ sagt nichts darüber, ob es heute
  funktioniert.
- Erzeugt wird sie auf Anforderung, nicht nach jedem Lauf: sie liest den
  ganzen Ordner, und die meisten Läufe sind ein weiterer Datenpunkt in
  einem Bild, das gerade niemand ansieht.

## Verifikation ohne Hardware

Der Executor spricht ausschließlich `BusInterface` — damit ist er komplett
ohne Adapter testbar:

- Unit-/Service-Tests gegen `EdsDemoBus` (virtuelle DUTs aus echten
  EDS-Dateien) analog `tests/test_demo.py`.
- Protokoll-Ende-zu-Ende gegen `CanopenBus` + python-can-`virtual`-Bus
  mit `canopen.LocalNode` als Gegenstelle, analog
  `tests/test_canopen_bus.py` — so lässt sich z. B. ein kompletter
  YAML-Testfall inklusive `wait_for`-Heartbeat real durchspielen.
