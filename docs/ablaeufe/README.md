# Abläufe — Spezifikation der Bedien- und Bus-Sequenzen

Jede nicht-triviale Funktion des Bench-Tools ist ein **Ablauf**: eine feste
Sequenz aus UI-Aktion, Service-Logik (`Bench`), Bus-Primitiven
(`BusInterface`) und Geräteverhalten (DUT). Dieses Verzeichnis spezifiziert
diese Abläufe, damit

1. die Implementierung gegen ein definiertes Soll geprüft werden kann
   (insbesondere die erste Inbetriebnahme am echten IXXAT-Adapter),
2. Fehlerpfade *vor* dem Hardware-Test durchdacht sind, nicht erst danach,
3. der Bereich Tests auf demselben Fundament steht: Testfälle sind selbst
   Abläufe und werden im maschinenlesbaren Format aus
   [`testfall-format.md`](testfall-format.md) definiert.

## Dokumente

| Datei | Ablauf |
|---|---|
| [`A-01-verbinden.md`](A-01-verbinden.md) | Verbinden / Trennen (Adapter-Bring-up, speziell IXXAT/VCI4) |
| [`A-02-scan.md`](A-02-scan.md) | Scan: Knoten finden, identifizieren, EDS zuordnen |
| [`A-03-scan-verify.md`](A-03-scan-verify.md) | Machine Control: Scan & Verify, LSS-Umadressierung |
| [`A-04-testlauf.md`](A-04-testlauf.md) | Testfall-Ausführung (Lauf-Lebenszyklus, Step-Executor) |
| [`A-05-adressierung-teach.md`](A-05-adressierung-teach.md) | Adressierung im Button-Teach-Verfahren |
| [`testfall-format.md`](testfall-format.md) | YAML-Format für Testfall-Sequenzen |

Beispiel-Testfälle: [`../../examples/testcases/`](../../examples/testcases/).

## Namensschema

`A-NN-<kurzname>.md`, fortlaufend nummeriert. Neue Abläufe (z. B. SWDL,
EDS-Verwaltung) bekommen die nächste freie Nummer.

## Template

Jeder Ablauf wird nach diesem Schema beschrieben:

```markdown
# A-NN — <Name>

## Zweck und Auslöser
Was der Ablauf leistet; welche UI-Aktion / welcher `act_*`-Handler ihn startet.

## Vorbedingungen
Zustand von Tool, Adapter und Bus, der erfüllt sein muss.

## Akteure
UI → Bench (core.py) → BusInterface-Implementierung → Adapter → DUT(s).

## Ablauf
Nummerierte Schritte mit den konkreten CANopen-Diensten (SDO/NMT/LSS/
Heartbeat), COB-IDs und Objekten. Jeder Schritt ist markiert:
  [Ist]  bereits so implementiert (mit Code-Referenz)
  [Soll] spezifizierte Erweiterung, noch nicht implementiert

## Timing und Timeouts
Wartezeiten, Timeouts, Retry-Politik.

## Fehlerpfade
Pro Schritt: was schiefgehen kann, erwartetes Verhalten, Logzeile.
Bekannte Lücken der Ist-Implementierung werden als Befund F-n geführt.

## Beobachtbares Ergebnis
Logzeilen, State-Snapshot-Felder, UI-Effekte.

## Abnahme-Checkliste (falls Hardware-relevant)
Abzuhakende Prüfschritte für den Test am echten Adapter.
```

Die Ist-Referenzen zeigen auf den Stand bei Erstellung der Spezifikation;
bei Abweichung gilt: Code-Verhalten prüfen, Spezifikation nachziehen oder
Code korrigieren — nicht stillschweigend auseinanderlaufen lassen.

## Roadmap (Umsetzung der [Soll]-Teile)

Die Spezifikationen beschreiben Soll-Verhalten, das in dieser Reihenfolge
umgesetzt werden soll:

1. **Scan-Härtung nach A-02** — ✅ Code-Teil umgesetzt: F-1
   (Identity-Format), F-2 (Event-Loop-Blockade), F-3 (Connect-Fehler)
   behoben, Busstatus-Hinweis bei 0 Treffern ergänzt, IXXAT-Kanal als
   int. Der IXXAT (Windows, VCI4) ist seither im regulären Einsatz am
   Bench-PC und das Tool ist daran breit erprobt. Die Abnahme-Checklisten
   in A-01 und A-02 sind damit inhaltlich abgedeckt, aber nie Punkt für
   Punkt als Protokoll abgehakt worden — sie stehen dort weiter als
   Vorlage für eine formale Abnahme oder einen neuen Adapter.
2. **Step-Executor nach A-04 / testfall-format.md** — ✅ umgesetzt
   (`Bench._run_task`, `canopen_bench/testcases.py`); ersetzt die
   simulierte `_run_step`-Logik für echte Testfall-Dateien und ist ohne
   Hardware gegen den `EdsDemoBus` getestet (`tests/test_executor.py`).
3. **Testkatalog aus dem TestCases-Ordner** — ✅ umgesetzt: die
   Tests-Seite listet die `TC*.yaml` aus dem konfigurierten Ordner
   (Fallback `data.TESTS`, wenn leer). Suiten (benannte Auswahl +
   Laufkonfiguration) sind ✅ umgesetzt (speichern/laden/löschen,
   persistiert im Workspace).
4. **Scan & Verify nach A-03** — ✅ F-4/F-5 behoben: Verify führt einen
   echten Scan aus und prüft Anzahl **und** Zuordnung gegen den
   übernommenen Soll-Zustand (`mc_ref`).
5. **Button-Teach-Adressierung nach A-05** — ✅ umgesetzt: Sequenz-Format
   v2 (Register, Sprünge, Arithmetik, Raw-CAN) trägt den Ablauf als
   austauschbare Datei (`data/flows/teach_addressing.yaml`); Machine
   Control startet ihn manuell oder automatisch (nur bei aktivem
   MC-Modus). F-6/LSS ist damit für die Machine Control gegenstandslos.
   **Offen:** Abnahme am echten Gerät (A-05, offene Punkte: u. a.
   Session-Byte-Layout bestätigen).
6. **Weitere offene Punkte:** PDO-Primitive für Testfälle, SWDL-Ablauf,
   Trace-Interpretation für PDO-Inhalte (SDO ist umgesetzt).
