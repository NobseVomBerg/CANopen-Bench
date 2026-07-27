# Sequenz-Format (YAML) — Testfälle und Abläufe

Das Format beschreibt deklarative Schrittfolgen auf dem Bus. Es wird an
zwei Stellen verwendet:

- **Testfälle**: Dateien `TC<id>_<kurzname>.yaml` im konfigurierten
  TestCases-Ordner (Setup-Seite, `paths["tc"]`).
- **Abläufe** (Format v2): austauschbare Prozedur-Dateien wie die
  Button-Teach-Adressierung (`data/flows/teach_addressing.yaml`, A-05) —
  gleiche Syntax, ohne die `TC<id>_`-Dateinamenspflicht.

Version 2 erweitert das ursprünglich rein lineare Format um **Variablen
(Register), Sprünge zu Labels und einfache Arithmetik** nach dem Vorbild
eines simplen Assembler-Befehlssatzes — bewusste Weiterentwicklung, um
herstellerspezifische Abläufe als Dateien abbilden zu können statt als
Code. Bestehende v1-Dateien sind unverändert gültig.

Ausführungssemantik (Verdikte, Executor, Abbruch): siehe
[`A-04-testlauf.md`](A-04-testlauf.md). Beispiele:
[`../../examples/testcases/`](../../examples/testcases/).

## Kopf

```yaml
id: "4602"                  # Pflicht; Katalog-ID, eindeutig im Ordner
name: "Aux power off handling, under-voltage"   # Pflicht; Anzeige
tools: [PSU]                # optional; benötigte Zusatzgeräte, [] = keine
est: "8.4 s"                # optional; erwartete Dauer, nur Anzeige
dut: selected               # optional; Zielgerät der Bus-Schritte:
                            #   selected (Default) | {code: "D28"}
preconditions: []           # optional; Schritte; Fehlschlag → SKIP
steps: []                   # Pflicht; die eigentliche Sequenz
```

Die Node-ID steht **nie** in der Datei — sie wird zur Laufzeit über die
`dut`-Angabe aufgelöst (Builtin `$node`).

## Register und Werte (v2)

- **10 vordefinierte Register `R0`…`R9`**, je Lauf mit 0 initialisiert;
  Inhalt: ganze Zahlen, Arithmetik mit 32-bit-Wrap (unsigned).
- **Wert-Angaben** — überall, wo ein Wert erwartet wird (`value`,
  `expect`, `mask`, `cob`, `data`-Bytes, Vergleichsoperanden `a`/`b`,
  `node`), sind gleichwertig erlaubt:
  - Hex-String `"0x2050"`, Ganzzahl-Literal `42`,
  - Registername `R3`,
  - Builtin: `$node` (aufgelöste DUT-Node-ID), `$expected`
    (Soll-Geräteanzahl aus dem übernommenen Soll-Zustand (Machine
    Control)).
  - `$session` (die Session-Identität des Tools: Master-SerialNr 4 Byte,
    je Workspace einmalig erzeugt + SessionId 1 Byte, je Adressierungslauf
    inkrementiert) ist **nur** in `can_send`-Daten erlaubt — als ganze
    `data`-Quelle oder als Listeneintrag, der an Ort und Stelle zu seinen
    Bytes expandiert (`data: [$session, "0x02", 0, 0]`).
  - **Symbol** aus den C-Headern des Geräts, `$eObjIdx_LampControl`
    — überall erlaubt, wo ein Wert steht, also auch für `index` und `sub`:

    ```yaml
    - sdo_write: {index: $eObjIdx_LampControl, sub: "00", value: "0x01", size: 4}
    - sdo_read:  {index: $eObjIdx_Process, sub: $eSubProcess_Status, into: R1}
    ```

    Aufgelöst wird beim **Laden** der Datei, nicht zur Laufzeit: ein
    Tippfehler macht den Testfall im Katalog ungültig, statt mitten im Lauf
    an echter Hardware zu scheitern. Woher die Symbole kommen und wie man
    eigene ergänzt: `docs/extending.md`, Abschnitt „Symbol tables". Bei
    gleichnamigen Symbolen aus zwei Plugins qualifiziert
    `$memiro:eObjIdx_LampControl`.

## Schritt-Primitive

Jeder Schritt ist ein Ein-Schlüssel-Mapping.

### Bus (v1, mit v2-Erweiterungen)

| Primitive | Form | Semantik | Verdikt-Wirkung |
|---|---|---|---|
| `nmt` | `nmt: start` oder `nmt: {cmd, node: all\|<wert>}` | NMT-Kommando; Kurzform zielt auf das DUT, `node: all` = Broadcast | Fehler nur ohne Verbindung → ERROR |
| `sdo_read` | `{index, sub, into?, expect?, expect_abort?, mask?}` | SDO-Upload; **Ergebnis landet immer im Register `into` (Default `R0`)** — numerisch als int, nicht-numerische Antworten (Strings) als 0 | Abort/Timeout ohne `expect_abort` → FAIL; `expect` verfehlt → FAIL |
| `sdo_write` | `{index, sub, value, size?, expect_abort?}` | SDO-Download; `value` darf Register/Builtin sein — dann bestimmt `size` (1/2/4 Bytes, Default 4) die Breite; literale Hex-Strings behalten ihre eigene Breite | Abort/Timeout ohne `expect_abort` → FAIL; mit `expect_abort` (wie bei `sdo_read`) muss **genau dieser** Abort-Code kommen — ein erfolgreicher Write oder ein anderer Code → FAIL |
| `wait` | `wait: 1.5` | Sekunden warten | — |
| `wait_for` | `{heartbeat: <zustand>, timeout, node?, on_timeout?}` **oder** `{cob: <wert> \| [<wert>, …], timeout, data?: "00" \| [...], into?: Rn, on_timeout?}` | auf Heartbeat-Zustand warten oder (v2) auf einen Frame mit dieser COB-ID, optional mit Datenpräfix. `cob`/`data` als **Liste** racen mehrere (COB, Präfix)-Paare **gleichzeitig in einem Wait** (nicht nacheinander) — mit `into: Rn` landet der Index des zuerst getroffenen Paars in diesem Register (ohne `into` wird kein Register beschrieben); damit lässt sich ein Nebensignal (z. B. Addr-End) nicht mehr verpassen, während auf das Hauptsignal gewartet wird — kein blindes Zeitfenster zwischen zwei getrennten Polls mehr nötig. Mit `on_timeout: <label>` wird bei Timeout dorthin **gesprungen** statt FAIL | Timeout → FAIL (ohne `on_timeout`) |
| `can_send` | `{cob: <wert>, data: [<byte-werte>] \| $session}` | (v2) rohen CAN-Frame senden; Listeneinträge liefern je 1 Byte (Register: Low-Byte); `$session` als Eintrag expandiert zu seinen Bytes. Ohne installierten Addressing-Provider (Vendor-Plugin) gibt es keine Session-Identität — ein Schritt mit `$session` schlägt dann fehl | Fehler ohne Verbindung → ERROR; `$session` ohne Provider → FAIL |
| `lss_assign` | `{count: <wert>, into?: Rn}` | (v2) Standard-Adressierung nach CiA 305: unkonfigurierte Slaves (Node-ID 0xFF) einzeln per LSS-Fastscan identifizieren und auf die Node-IDs 1..count konfigurieren/speichern; genau ein bereits konfiguriertes Gerät wird per globalem State-Switching umadressiert. Die Anzahl tatsächlich zugewiesener Nodes landet in `into` (Default `R0`) — auf echter LSS-Hardware bislang **ungetestet** (A-03) | Fehler nur ohne Verbindung → ERROR; Unterzahl per `jump_lt`/`fail` im Ablauf behandeln |
| `manual` | `manual: "Text"` bzw. `{text, timeout?}` | Bedieneranweisung, wartet auf Bestätigung (Default 120 s) | Abbruch/Timeout → ERROR |
| `log` | `log: "Text"` | Annotation im Lauf-Log | — |

### Variablen, Arithmetik, Sprünge (v2)

| Primitive | Form | Semantik |
|---|---|---|
| `mov` | `{to: Rn, value: <wert>}` | Rn := value |
| `add` / `sub` / `and` / `or` | `{to: Rn, value: <wert>}` | Rn := Rn OP value (32-bit-Wrap) |
| `label` | `label: <name>` | Sprungmarke (eindeutig je Schrittliste) |
| `jump` | `jump: <name>` | unbedingter Sprung |
| `jump_eq` / `jump_ne` / `jump_gt` / `jump_lt` | `{a: <wert>, b: <wert>, to: <name>}` | Sprung, wenn a == / ≠ / > / < b |
| `fail` | `fail: "Text"` | Fall sofort mit **FAIL** beenden (typisches Sprungziel) |
| `end` | `end:` | Fall sofort mit **PASS** beenden |

Sprungziele müssen in **derselben** Schrittliste liegen (`preconditions`
und `steps` sind getrennte Programme) und werden beim Parsen geprüft.

### `sdo_read` im Detail

- `expect`: Vergleich numerisch — `"0x2A"` matcht einen gelesenen Wert
  `0x0000002A`; nicht-numerische Werte (Strings) literal.
- `mask`: optional zu `expect`; verglichen wird `(wert & mask) == (expect & mask)`.
- `expect_abort`: erwarteter SDO-Abort-Code; ein Wert oder anderer Abort → FAIL.
  `expect` und `expect_abort` schließen sich aus.

## Vollständigkeits- und Schutzregeln

- Pflichtfelder: `id`, `name`, `steps` (nicht leer).
- Unbekannte Schlüssel (Kopf oder Schritt) sind Schemafehler — die Datei
  erscheint rot im Katalog und ist nicht ausführbar.
- Testfälle: `id` muss zum Dateinamen-Präfix `TC<id>_` passen
  (Ablauf-Dateien sind davon ausgenommen).
- **Schleifen-Schutz:** maximal 10 000 Schrittausführungen je Fall;
  Überschreitung → ERROR („step limit exceeded").

## Plugin-Schritte

Erweiterungspakete können eigene Schritt-Primitive registrieren
(`BenchPlugin.step_types()`, siehe `docs/extending.md`). Sie
werden in YAML als `<plugin>.<key>` referenziert (z. B.
`- acme.block_download: {...}`), zur Parse-Zeit vom Plugin validiert und
in derselben VM ausgeführt. Eine Datei, die Plugin-Schritte nutzt, ist
ohne das installierte Plugin ein Schema-Fehler im Katalog („unknown step
primitive") — gewollt, damit Vendor-Abläufe nicht stumm falsch laufen.

## Abgrenzung

- **Keine PSU-/Prüfmittel-Primitive:** Aktionen an Zusatzgeräten laufen
  als `manual`-Schritt, bis ein Gerät fernsteuerbar ist.
- **Keine PDO-Primitive** in dieser Ausbaustufe.

## Anforderungen an die Engine — umgesetzt

- Parser/Katalog: `canopen_bench/testcases.py` (`parse_testcase`,
  `load_catalog`); Label-Prüfung beim Parsen; `pyyaml` Basis-Abhängigkeit.
- Ausführung: `Bench`-Executor (asyncio-Task) mit Programmzähler,
  Registerbank je Lauf und Schrittzähler-Guard; Raw-CAN über die
  Bus-Primitiven `send_raw`/`wait_frame`, Standard-Adressierung über
  `lss_assign` (`BusInterface`).
- YAML-Eigenheit: unquotierte `0x…`-Literale parst YAML als int — die
  Engine normalisiert beides; in Dateien Hex-Werte bevorzugt quoten.
  Achtung: unquotiertes `no`/`yes`/`on`/`off` parst YAML als bool.
