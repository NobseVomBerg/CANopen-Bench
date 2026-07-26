# Funktions-Roadmap (Vergleich mit kommerziellen CANopen-Tools)

Ergebnis der Lücken-Analyse gegen CANalyzer.CANopen (Vector),
canAnalyser 3 + CANopen-Modul (IXXAT/HMS), CANopen Magic (ESAcademy)
und PCAN-Explorer (PEAK), Stand 2026-07. Bei SDO-Interpretation,
EDS-Handling, Test-Executor und Workspaces sind wir konkurrenzfähig;
die echten Lücken liegen im Trace (PDO-Inhalte, EMCY-Klartext,
Statistik, Replay/Formate) und beim aktiven Senden (SYNC, zyklische
Sendelisten).

Empfohlene Reihenfolge (Aufwand/Nutzen):
EMCY-Klartext → Trace-Statistik → PDO-Dekodierung → SYNC + Sendelisten
→ CSV/candump → Signal-Plot → CiA-301-Smoke-Suite.
Kategorie A ist damit abgearbeitet. Kategorie B bleibt zurückgestellt,
bis klar ist, ob die Nutzer eher testen (→ Plot/Replay) oder in Betrieb
nehmen (→ Mapping/DCF).

## Kategorie A — fehlt wirklich, passt exakt zum Werkzeugcharakter

- [x] **EMCY-Klartext** — Error-Code (CiA-301-Tabelle, z. B. 0x8130 =
  Heartbeat consumer timeout) und Error-Register-Bits dekodieren;
  herstellerspezifische Codes über Plugin-Hook nachpflegbar
  (`BenchPlugin.emcy_codes`). Später: Fehlerhistorie 0x1003 auslesen.
- [x] **Trace-Statistik** — Stats-Ansicht im Trace: Frames gesamt und
  Frames/s je COB-ID (5-s-Fenster), Anteils-Balken, Klassen-Summen,
  Buslast-Verlauf (60 s Sparkline), Fehlerzähler. Zähler laufen seit
  Connect bzw. Trace-Clear.
- [x] **PDO-Payload-Dekodierung über das EDS-Mapping** — PDO-Frames
  werden über das Default-Mapping (0x1600/0x1A00) des zugewiesenen EDS
  in benannte Signale zerlegt (bitgenau, LSB-first, INTEGER-Typen
  vorzeichenbehaftet); Demo-Geräte senden TPDO1 gemäß ihrem EDS.
  Annahme: Predefined Connection Set + EDS-Default-Mapping — das
  Live-Mapping vom Gerät lesen gehört zum Mapping-Editor (Kategorie B).
- [x] **SYNC-Producer + zyklische Sendelisten** — PDO-RAW-Zeilen haben
  eine Zykluszeit (ms) + ⟳-Schalter (Server sendet im Hintergrund,
  Ausgangszustand nach Neustart immer „aus"); SYNC-Producer mit
  Intervall im Setup. Demo-Geräte wenden empfangene RPDOs per
  EDS-Mapping an (0x60FF → 0x606C folgt, einfaches Antriebsmodell).
  Hinweis: asyncio-Timing — füttert Geräte und erzeugt Last, ist aber
  kein hardware-getimter Generator für Jitter-Messungen.
- [x] **CSV-/candump-Export + candump-Import** — Trace-Ansicht: „⤓ CSV"/
  „⤓ candump" exportieren den gefilterten Trace (voller Puffer, nicht
  nur die Scrollback-Ansicht), „⤒ Import…" liest ein `candump -l`-Logfile
  ein, dekodiert es über dieselbe Annotate-Pipeline wie Live-Traffic
  (SDO/PDO/EMCY — ohne deren Live-Nebenwirkungen wie Statuslog/
  Signal-Plot, siehe `_annotate_*(..., live=False)`) und legt es als
  normale Capture-Datei ab (taucht in „Load capture…" auf). Bewusst
  nicht weiterverfolgt: `.trc` (PCAN, mehrere Versions-/Spaltenlayouts)
  und weitere Fremdformate — eigene Mitschnitte laufen ohnehin über das
  native JSON-Format (Save/Load, verlustfrei), fremde Feldaufzeichnungen
  öffnet man selten genug, dass ein zweiter Importparser den Aufwand
  nicht lohnt. Replay (Demo-Bus wie auch echte Hardware) ebenfalls
  ausgeklammert — siehe Kategorie C.
- [x] **Signal-Plot über Zeit** — dritte Trace-Ansicht neben Trace/Stats:
  bis zu 4 Signale gleichzeitig, ausgewählt per ∿-Icon in Objects/
  Favoriten (wie Favoriten persistiert), gespeist aus derselben
  SDO-/PDO-Dekodierung, die der Trace ohnehin schon berechnet (kein
  separates Polling). Je Signal 600 Punkte, unabhängig Y-skaliert.

## Kategorie B — Commissioning (zurückgestellt, s. o.)

- [ ] **PDO-Mapping-Editor** — 0x1400–0x1BFF lesen, Transmission-Type /
  COB-ID / Mapping ändern.
- [ ] **Store/Restore-Komfort** — 0x1010/0x1011 als Knöpfe
  („save"/„load"-Signatur schreiben) statt Hand-SDO.
- [ ] **DCF** — Gerätekonfiguration als DCF exportieren/importieren und
  aufs Gerät spielen; passt zu „der Workspace ist die Konfiguration".
- [ ] **LSS-Ausbau** — Fastscan wird intern schon fürs Assign genutzt;
  es fehlen Identify/Blink und Bitrate-Umschaltung per LSS.
- [ ] **`txlist`-Testprimitive** — Testfälle steuern die zyklischen
  Sender (`txlist_start`/`txlist_stop` als Format-v2-Schritte), statt
  Zyklen in YAML nachzubauen — Test orchestriert die Umgebung
  (CAPL-+-IG-Block-Muster).

## Kategorie C — interessant, größer, strategisch

- [ ] **Knotensimulation am echten Bus** — Demo-Bus-Simulation
  invertiert: fehlendes Gerät am realen Bus simulieren, damit die
  Maschine ohne das Gerät läuft (CANopen-Magic-Alleinstellung). Das ist
  der sinnvolle Nachfolger der ursprünglich angedachten Idee „Replay auf
  echte Hardware": blindes Abspielen aufgezeichneter Telegramme bringt
  nichts, weil kein DUT darauf reagiert (fehlende Protokoll-/Zustands-
  logik) — hier reagiert wenigstens die vorhandene Demo-Logik auf echte
  SDO-/PDO-Anfragen der Maschine.
- [ ] **Replay auf den Demo-Bus** — aufgezeichneten Trace zeitlich
  nachspielen. Aktuell kein klarer Mehrwert: Timings und Ablauf sind
  beim Reinladen eines Mitschnitts (Save/Load, CSV/candump-Import)
  schon vollständig nachvollziehbar. Lohnt sich erst, sobald geräte-
  spezifische Visualisierung (Display/LEDs/Buttons) je nach Bedarf
  implementiert ist — dann zeigt das Abspielen live, was auf diesen
  Elementen passiert wäre.
- [ ] **KI-gestützte TestCase-Extraktion aus Mitschnitten** — relevante
  Abläufe aus einer Feldaufzeichnung automatisiert erkennen und als
  Testfall-YAML (Format v2) vorschlagen, statt sie von Hand
  nachzubauen.
- [x] **CiA-301-Smoke-Suite** — vier mitgelieferte, generische Testfälle
  unter `examples/testcases/` (TC0001–TC0004): Identity-Pflichtobjekte
  (0x1018, alle fünf Sub-Indizes), Pflicht-Kommunikationsobjekte
  (0x1000/0x1001), NMT-Zustandsautomat (start/preop/stop/resetcomm,
  Heartbeat-Zustand nach jedem Übergang geprüft — Heartbeat *ist* der
  Test, kein eigener Fall), SDO-Abort-Verhalten (unbekannter Index,
  unbekannter Sub-Index eines bekannten Objekts, Schreiben auf ein
  Pflicht-ro-Objekt). Bewusst ohne herstellerspezifische Objekte, läuft
  daher gegen jedes CiA-301-konforme Gerät inkl. Demo-Modus. Dafür zwei
  kleine, generisch nützliche Korrekturen nötig: die Demo-Bus-Simulation
  unterschied vorher nicht zwischen „Index unbekannt" (0x06020000) und
  „Sub-Index eines bekannten Objekts unbekannt" (0x06090011) — beides kam
  als Erstgenanntes zurück; und `sdo_write` kannte kein `expect_abort`
  (nur `sdo_read` konnte bisher „dieser Abort-Code ist das erwartete
  Ergebnis" ausdrücken) — jetzt symmetrisch zu `sdo_read` ergänzt.
- [x] **Heartbeat-Ausfallüberwachung** — bewusst nur Teil der Machine
  Control (nicht der allgemeinen Devices-Box): laufende Überwachung der
  im Soll-Zustand zugeordneten Nodes, konfigurierbarer Timeout,
  Gnadenfenster nach Start/Adopt/Reconnect, Alarm + Erholung im MC-Log
  und in der MC-Karte. Löst keine Aktion aus — reine Beobachtung.

## Bewusst nicht übernehmen

CAPL-artiges Scripting (YAML-Format + Plugin-StepTypes decken das ab),
Multi-Channel/Gateway-Analyse, CAN-FD/USDO (bis Bedarf da ist),
proprietäre Formate wie BLF/MDF, J1939/DeviceNet-Breite — das ist das
Revier der Universal-Analyzer; unsere Identität ist der fokussierte
CANopen-Prüfstand.

## Quellen

- <https://www.vector.com/int/en/products/products-a-z/software/canalyzer/option-canopen/>
- <https://www.tecnologix.it/en/ixxat-canopen-module.html>
- <https://twincomm.nl/en/product/can/cananalyser-3-suite/cananalyser-3/>
- <https://phytools.com/products/canopen-magic-ultimate>
- <https://blog.esacademy.com/tag/canopen-magic/>
- <https://www.peak-system.com/products/software/analysis-software/pcan-explorer-6/>
