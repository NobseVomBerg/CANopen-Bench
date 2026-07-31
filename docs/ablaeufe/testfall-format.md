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
id: "4602"                  # Pflicht; Katalog-ID, eindeutig im Ordner (s.u.)
name: "Aux power off handling, under-voltage"   # Pflicht; Anzeige
desc: "Prüft, dass ..."     # optional; ein Satz für den Report-Kopf
grade: automated            # optional; automated | semi | manual | production
tools: [PSU]                # optional; benötigte Zusatzgeräte, [] = keine
est: "8.4 s"                # optional; erwartete Dauer, nur Anzeige
dut: selected               # optional; Zielgerät der Bus-Schritte:
                            #   selected (Default) | {code: "D28"}
variants: ["820", "920"]    # optional; HW-Varianten, für die der Fall gilt,
                            #   so wie das Gerät sie meldet. [] = alle
on_fail: continue           # optional; continue (Default) | stop
preconditions: []           # optional; Schritte; Fehlschlag → SKIP
steps: []                   # Pflicht; die eigentliche Sequenz
```

Die Node-ID des Prüflings steht **nie** in der Datei — sie wird zur
Laufzeit über die `dut`-Angabe aufgelöst (Builtin `$node`). Ein einzelner
Bus-Schritt darf per `node:` ein anderes Gerät ansprechen: ein Fall
handelt von einem Prüfling, aber am Bus hängt manchmal mehr — ein zweites
Gerät, das Material verbraucht, ein Gateway. Ohne diese Angabe ließe sich
so ein Fall gar nicht aufschreiben.

### `id` — eindeutig, und zwar nachprüfbar

Der Katalog ist nach `id` geschlüsselt: Laufreihenfolge, Ergebnis und
Report werden alle gegen sie geschrieben. Beanspruchen zwei Dateien im
Ordner dieselbe `id`, ist also nur eine von beiden im Katalog — welche,
entscheidet die Verzeichnisreihenfolge.

Das passiert lautlos, wenn niemand es meldet: 85 Dateien im Ordner, 81
Einträge in der Liste. Deshalb protokolliert der Scan jede Kollision und
hängt sie als Schema-Fehler an den Fall, der übrig geblieben ist — mit
dem Namen der Datei, die verdrängt wurde. Die Liste zeigt ihn dann rot,
und der fehlende Fall ist nicht mehr unsichtbar, sondern benannt.

Zu beheben ist das nur in der Quelle: eine der beiden Dateien bekommt
eine andere `id`.

### EMCY prüfen — welcher Teil des Frames gemeint ist

Ein EMCY-Frame hat nach CiA 301 drei Teile: **Error-Code** (Byte 0–1,
u16 LE), **Error-Register** (Byte 2) und **5 Byte herstellerspezifisch**
(Byte 3–7). Was in den letzten fünf steht, sagt die Norm nicht — und
genau dort legt eine Gerätefamilie ihren eigenen Fehlercode ab, gegen
den ihre Testfälle geschrieben sind. Ein Gerät kann normkonform `0x1000`
(generic error) in den Error-Code schreiben und *welcher* Fehler es ist
in die Herstellerbytes.

Verbreitet, und was `mec` liest: die ersten beiden Herstellerbytes
(3–4) als **u16 little-endian**, gleiche Byte-Reihenfolge wie der
Error-Code. Byte 5–7 werden nicht verglichen; braucht ein Fall etwas
daraus, ist das ein eigenes Feld wert und keine stillschweigende
Umdeutung von `mec`.

Jeder Teil wird einzeln benannt, und **jedes angegebene Feld muss
passen**:

| Feld | Vergleicht | Maske |
|---|---|---|
| `code` | Error-Code, Byte 0–1 (u16 LE) | `mask`, Default `0xFFFF` |
| `mec` | Manufacturer Error Code, Byte 3–4 (u16 LE) | `mec_mask`, Default `0xFFFF` |
| `reg` | Error-Register, Byte 2 (u8) | — |

```yaml
- expect_emcy: {mec: $eErrCode_MotorStalled}      # nur der Herstellercode
- expect_emcy: {mec: "0x6D", reg: "0x01"}         # zusammen mit dem Register
- expect_emcy: {code: "0x8110", mask: "0xFF00"}   # nur die CiA-Klasse
- expect_emcy: {code: "0x00", mask: "0x00"}       # irgendeine, egal welche
```

Mindestens eines von `code`, `mec`, `reg` muss dastehen. Ein Schritt, der
keines nennt, prüft nichts und sieht dabei aus, als täte er es — „gar
keine EMCY" ist `expect_no_emcy`, und das ist eine andere Aussage.

Der Grund für die Trennung: `code` und `mec` sind zwei voneinander
unabhängige Dinge. In einem echten Frame `00 10 01 72 00 00 00 00` ist
der Error-Code `0x1000`, das Register `0x01` und der Herstellercode
`0x0072` — eine einzelne Zahl für beides wäre eine Zahl, die zwei Sachen
bedeutet, und ein `expect_emcy 0x72` verglich dann `0x1000` gegen `0x72`
und meldete „keine EMCY gesehen" über eine, die längst dalag.

Fehlschlägt der Schritt, nennt die Begründung deshalb auch, **was
stattdessen kam** — mit Code, Register und Herstellercode.

### `variants` — für welche Hardware der Fall gilt

Die Varianten, wie das Gerät sie selbst meldet (Scan liest sie aus dem
Objekt, das der EDS-Eintrag dafür benennt). Leer heißt: für alle.

Einmal im Kopf, nicht als Vorbedingung aus Sprüngen: der Katalog kann
danach filtern, ohne irgendetwas auszuführen, und der Executor
überspringt eine Abweichung von selbst (Verdikt **SKIP** mit
Begründung). **Nur eine bekannte Abweichung überspringt** — meldet das
Gerät keine Variante, läuft der Fall. Nicht zu prüfen, weil eine Zahl
nicht gelesen werden konnte, ließe Abdeckung lautlos verschwinden.

### `on_fail` — ob der Fall seinen eigenen Fehlschlag überlebt

`continue` (**Default**) merkt sich den ersten Fehlschlag — er bleibt die
Begründung des Verdikts **FAIL** — und läuft weiter. Zwei Gründe, warum
das die Vorgabe ist: die letzten Schritte eines Falls richten oft den
Prüfstand wieder her, und ein Lauf, der bei 12 V stehen bleibt, lässt das
Gerät bei 12 V stehen. Und ein Fall, der von vier kaputten Dingen
berichten könnte, berichtet von einem — man repariert es, startet neu,
findet das nächste.

`stop` beendet den Fall beim ersten verfehlten Erwartungs-Schritt, für
die Fälle, in denen danach ohnehin nichts mehr aussagekräftig ist.

Ein explizites `fail:` beendet den Fall **immer** — es ist der Abbruch,
den jemand hingeschrieben hat, kein verfehlter Vergleich. Technische
Fehler (ERROR) brechen ebenfalls weiterhin ab: durch einen
Verbindungsverlust lässt sich nicht hindurchlaufen. Für `preconditions`
gilt `on_fail` nicht — dort heißt Fehlschlag SKIP, und es gibt nichts
rückgängig zu machen.

`jump_on_error` ist nur mit `continue` erreichbar; mit `stop` wäre der
Fall am Fehlschlag schon beendet.

### `note` — der Satz daneben

Jeder Schritt mit einer Mapping-Form darf ein `note: "…"` tragen. Es
landet im Report unter dem Schritt, in einer eigenen Zeile:

```yaml
- sdo_write: {index: "0x1F51", sub: "0x02", value: 2, size: 1, note: "Reboot DUT"}
```

```
14   write 0x1F51:0x02 = 2  (Program control)
     Reboot DUT
```

Der Report zeigt je Schritt bis zu drei Zeilen: **was lief** (mit dem
Objektnamen aus dem EDS, wenn er bekannt ist), **warum** (`note`) und
**was zurückkam** — auch im Gutfall, samt Enum-Bedeutung, wo ein Plugin
das Objekt beschreibt. Eine Zeile pro Schritt ist kompakt und eine Woche
später nicht mehr nachvollziehbar.

Ein **geglückter `sdo_write` hat keine dritte Zeile**: das Gerät antwortet
darauf mit nichts, und der geschriebene Wert steht schon in der ersten.
Kommt er aus einem Register oder einem Builtin, steht er dort *auch* —
aufgelöst, hinter dem Namen:

```
20   write 0x220B:0x02 = R12 = 0x00007211  (Menu.IdWithValue)
     Write the calculated Value
```

Sonst stünde die Zahl, die auf den Bus ging, nirgends. Anzeige und
Businhalt kommen aus derselben Funktion, damit ein Report nicht einen
Wert nennen kann, den das Gerät nie gesehen hat. Scheitert der Schreib­vorgang,
steht der Abort wie gehabt darunter.

In `note` und `log` sind einfache Formatierungs-Tags erlaubt (`<b>`,
`<i>`, `<u>`, `<em>`, `<strong>`, `<code>`, `<small>`, `<sub>`, `<sup>`,
`<br>`, `<hr>`) — alles andere wird escaped.

## Register und Werte (v2)

- **16 vordefinierte Register `R0`…`R15`**, je Lauf mit 0 initialisiert;
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
    `$acme:eObjIdx_LampControl`.

## Schritt-Primitive

Jeder Schritt ist ein Ein-Schlüssel-Mapping.

### Bus (v1, mit v2-Erweiterungen)

| Primitive | Form | Semantik | Verdikt-Wirkung |
|---|---|---|---|
| `nmt` | `nmt: start` oder `nmt: {cmd, node: all\|<wert>}` | NMT-Kommando; Kurzform zielt auf das DUT, `node: all` = Broadcast | Fehler nur ohne Verbindung → ERROR |
| `sdo_read` | `{index, sub, into?, expect?, expect_abort?, mask?, node?}` | SDO-Upload; **Ergebnis landet immer im Register `into` (Default `R0`)** — numerisch als int, nicht-numerische Antworten (Strings) als 0 | Abort/Timeout ohne `expect_abort` → FAIL; `expect` verfehlt → FAIL |
| `sdo_write` | `{index, sub, value, size?, expect_abort?, node?}` | SDO-Download; `value` darf Register/Builtin sein — dann bestimmt `size` (1/2/4 Bytes, Default 4) die Breite; literale Hex-Strings behalten ihre eigene Breite | Abort/Timeout ohne `expect_abort` → FAIL; mit `expect_abort` (wie bei `sdo_read`) muss **genau dieser** Abort-Code kommen — ein erfolgreicher Write oder ein anderer Code → FAIL |
| `wait` | `wait: 1.5` oder `{s, note?}` | Sekunden warten. Die Mapping-Form gibt es nur, damit ein Warten seine `note` tragen kann: „wait 4 s" sagt im Report nichts, „warten auf Reset und Lifter-Zyklus" sagt, warum vier | — |
| `wait_for` | `{heartbeat: <zustand>, timeout, node?, on_timeout?}` **oder** `{cob: <wert> \| [<wert>, …], timeout, data?: "00" \| [...], into?: Rn, on_timeout?}` | auf Heartbeat-Zustand warten oder (v2) auf einen Frame mit dieser COB-ID, optional mit Datenpräfix. `cob`/`data` als **Liste** racen mehrere (COB, Präfix)-Paare **gleichzeitig in einem Wait** (nicht nacheinander) — mit `into: Rn` landet der Index des zuerst getroffenen Paars in diesem Register (ohne `into` wird kein Register beschrieben); damit lässt sich ein Nebensignal (z. B. Addr-End) nicht mehr verpassen, während auf das Hauptsignal gewartet wird — kein blindes Zeitfenster zwischen zwei getrennten Polls mehr nötig. Mit `on_timeout: <label>` wird bei Timeout dorthin **gesprungen** statt FAIL | Timeout → FAIL (ohne `on_timeout`) |
| `can_send` | `{cob: <wert>, data: [<byte-werte>] \| $session}` | (v2) rohen CAN-Frame senden; Listeneinträge liefern je 1 Byte (Register: Low-Byte); `$session` als Eintrag expandiert zu seinen Bytes. `data: []` sendet einen Frame **ohne Daten** — ein CiA-301-SYNC trägt genau dann keine, wenn kein Zähler konfiguriert ist. Ohne installierten Addressing-Provider (Vendor-Plugin) gibt es keine Session-Identität — ein Schritt mit `$session` schlägt dann fehl | Fehler ohne Verbindung → ERROR; `$session` ohne Provider → FAIL |
| `lss_assign` | `{count: <wert>, into?: Rn}` | (v2) Standard-Adressierung nach CiA 305: unkonfigurierte Slaves (Node-ID 0xFF) einzeln per LSS-Fastscan identifizieren und auf die Node-IDs 1..count konfigurieren/speichern; genau ein bereits konfiguriertes Gerät wird per globalem State-Switching umadressiert. Die Anzahl tatsächlich zugewiesener Nodes landet in `into` (Default `R0`) — auf echter LSS-Hardware bislang **ungetestet** (A-03) | Fehler nur ohne Verbindung → ERROR; Unterzahl per `jump_lt`/`fail` im Ablauf behandeln |
| `manual` | `manual: "Text"` bzw. `{text, timeout?}` | Bedieneranweisung, wartet auf Bestätigung (Default 120 s) | Abbruch/Timeout → ERROR |
| `ask` | `ask: "Frage?"` bzw. `{text, title?, timeout?}` | Frage an den Bediener mit **drei** Antworten. Ja läuft weiter, Nein ist eine Aussage über das Gerät (kein Abbruch), Abbrechen heißt „gilt hier nicht" | Nein → FAIL mit der Frage als Begründung; Abbrechen → SKIP; Timeout → ERROR |
| `adjust` | `{index, sub, text?, size?, node?}` | Objekt lesen, dem Bediener zum Ändern anbieten, den eingetippten Wert zurückschreiben (Ersatz für eine Mini-Form im Altwerkzeug). **Die Eingabe ist dezimal, außer sie beginnt mit `0x`** — anders als überall sonst im Format, weil hier ein Mensch neben einem Messgerät tippt. Der geschriebene Wert landet in `R0` | Lese-/Schreib-Abort → FAIL; Abbrechen → SKIP; Timeout oder keine Zahl → ERROR |
| `psu` | `{ch?, volt?, curr?, output?}` | Labornetzteil stellen (`canopen_bench/instruments/`): Spannung/Strom eines Kanals (`ch`, Default 1) und/oder Ausgang `on`/`off`. Mindestens eines von `volt`/`curr`/`output`. Volt/Ampere dürfen Kommazahlen sein | kein Netzteil verbunden oder Fehler am Gerät → ERROR (Prüfmittel fehlt, das ist kein Fehlverhalten des DUT) |
| `log` | `log: "Text"` | Annotation im Lauf-Log | — |
| `emcy_clear` | `emcy_clear:` | verwirft die bis hier aufgezeichneten EMCYs | — |
| `expect_no_emcy` | `{code?, mask?, mec?, mec_mask?, reg?, node?}` | prüft, dass **keine** passende EMCY kam — ohne Feld: gar keine. Wartet nicht: kein Warten beweist, dass nichts mehr kommt; geprüft wird dasselbe Fenster wie bei `expect_emcy`, also alles seit dem letzten `emcy_clear` | passende EMCY vorhanden → FAIL, mit ihren Feldern in der Begründung |
| `expect_emcy` | `{code?, mask?, mec?, mec_mask?, reg?, node?, timeout?}` | prüft, ob seit dem letzten `emcy_clear` eine passende EMCY kam — **auch eine, die vor diesem Schritt eintraf**; sonst wird bis `timeout` (Default 1 s) darauf gewartet. Mindestens eines von `code`, `mec`, `reg` ist Pflicht (s.u.) | keine passende EMCY → FAIL, mit dem, was stattdessen kam |

### Variablen, Arithmetik, Sprünge (v2)

| Primitive | Form | Semantik |
|---|---|---|
| `mov` | `{to: Rn, value: <wert>}` | Rn := value |
| `add` / `sub` / `mul` / `div` / `and` / `or` / `xor` | `{to: Rn, value: <wert>}` | Rn := Rn OP value (32-bit-Wrap); `div` ist ganzzahlig, Division durch 0 → ERROR |
| `label` | `label: <name>` | Sprungmarke (eindeutig je Schrittliste) |
| `jump` | `jump: <name>` | unbedingter Sprung |
| `jump_on_error` | `jump_on_error: <name>` | springt, wenn der Fall bereits fehlgeschlagen ist. Nur mit `on_fail: continue` überhaupt erreichbar — sonst wäre der Lauf am Fehlschlag schon beendet |
| `rand` | `{to: Rn, min?, max?}` | Zufallszahl im Bereich (inklusive), Default 0…2³²−1 |
| `jump_eq` / `jump_ne` / `jump_gt` / `jump_lt` / `jump_ge` / `jump_le` | `{a: <wert>, b: <wert>, to: <name>}` | Sprung, wenn a == / ≠ / > / < / ≥ / ≤ b |
| `fail` | `fail: "Text"` | Fall sofort mit **FAIL** beenden (typisches Sprungziel) |
| `skip` | `skip: "Text"` | Fall sofort mit **SKIP** beenden — „gilt hier nicht", etwa für eine Gerätevariante, die der Fall nicht abdeckt. Kein Fehler, färbt einen Lauf nicht rot |
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

- **Prüfmittel nur, soweit fernsteuerbar:** für Labornetzteile gibt es
  `psu` (siehe oben). Alles andere läuft weiter als `manual`-Schritt.
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
