# A-03 — Scan & Verify und LSS-Umadressierung (Machine Control)

## Zweck und Auslöser

Prüfen, ob der Bus dem **übernommenen Soll-Zustand des Workspace**
entspricht (richtige Anzahl Geräte, richtige Zuordnung), und bei
Abweichung die Knoten per LSS neu adressieren. Das ist der
„Maschinensteuerungs"-Modus: das Tool stellt sicher, dass der Prüfplatz
so bestückt ist wie erwartet.

- Auslöser: „⌕ Scan & verify"-Button (`send('mc_verify')`,
  `static/app.js`) → `act_mc_verify` (`canopen_bench/core.py`);
  Umadressierung `act_mc_readdress` (`core.py`) manuell oder
  automatisch bei Mismatch (Option `autoReaddr`).
- Beim Server-Start: `startup()` stellt den gemerkten Ein/Aus-Zustand
  der Machine Control wieder her (persistiert in `mc_opts`; Option
  `autoStart` aus = immer deaktiviert starten) und stößt bei aktiver MC
  und Option `scanStart` den Verify an — da das Tool offline startet,
  läuft heute immer der Skip-Pfad (`MC   startup scan skipped — connect
  the interface, then scan & verify`).

## Vorbedingungen

- Verbunden (A-01); sonst Skip-Log (`emcy0`).
- Ein Soll-Zustand ist im Workspace übernommen (kv `mc_ref`): er enthält
  `expected` (Geräteanzahl beim Übernehmen), `assignments`
  (node → EDS-Datei) und `session` — bewusst per „Adopt current state as
  expected"-Button in der MC-Karte übernommen (Action `mc_adopt`), nicht
  automatisch gespeichert.
- Für die Umadressierung: DUTs mit LSS-Slave-Unterstützung (CiA 305).

## Akteure

UI → `Bench.act_mc_verify` / `act_mc_readdress` →
`BusInterface.scan`; Umadressierung über das gewählte Ablauf-File
(Standard: `lss_standard.yaml` mit der Step-Primitive `lss_assign`,
`BusInterface.lss_assign`; Vendor-Verfahren per Plugin, A-05) → DUTs.

## Ablauf: Scan & Verify

1. **[Ist]** Guard `mc.busy` / `connected`; Log
   `MC   scan & verify against the expected state`.
2. **[Ist]** Vollständigen Scan nach A-02 ausführen
   (`_mc_verify_task` → `_scan_async`).
   **Befund F-4 — BEHOBEN:** früher wurde nicht gescannt, sondern das
   Ergebnis des letzten manuellen Scans übernommen.
3. **[Ist]** Erwartungswerte aus dem **Soll-Zustand des Workspace**
   (kv `mc_ref`: erwartete Anzahl, Session-ID, Node→EDS-Zuordnung) —
   bewusst per „Adopt current state" in der MC-Karte übernommen, nie
   automatisch bei jedem Scan (sonst wäre die Verifikation zirkulär).
   `expected`/`session` fließen beim Übernehmen und beim Start in
   `self.mc` (`_adopt_ref`); ein Teach schreibt die neue Session in den
   Soll-Zustand zurück. Mehrere parallele Konfigurationen = mehrere
   Workspaces.
   **Befund F-5 — BEHOBEN.** Ist kein Soll-Zustand übernommen, wird der
   Verify mit `MC   scan & verify skipped — no expected state adopted`
   verweigert.
4. **[Ist]** Vergleich Anzahl **und Zuordnung**: je gefundenem Gerät muss
   die per Scan ermittelte EDS-Zuordnung dem `assignments`-Eintrag des
   übernommenen Soll-Zustands entsprechen (Node-ID → EDS-Datei);
   Alt-Zustände ohne `assignments`-Feld werden nur über die Anzahl
   geprüft. Ergebnis `mc.result` `ok`/`mismatch`, Logzeile
   `MC   n/m devices · session-ID <session> ✓ — expected state valid`
   bzw. `MC   n/m devices — mismatch · <erste Abweichung>` (`emcy0`).
5. **[Ist]** Bei Mismatch und aktivem `autoReaddr`: `_start_teach()`
   direkt (`act_mc_readdress` ist nur der Button-Wrapper für den
   manuellen Anstoss) — höchstens ein Versuch pro Verify.

## Ablauf: Heartbeat-Ausfallüberwachung

Ergänzt Scan & Verify um eine **laufende** Überwachung zwischen zwei
Verify-Läufen — ein Scan findet einen Ausfall erst beim nächsten
manuellen/automatischen Anstoss, ein Prüflauf über Minuten würde einen
zwischenzeitlich verstummten Antrieb sonst gar nicht bemerken.
Bewusst **nur** Teil der Machine Control, nicht der allgemeinen
Devices-Box: beobachtet werden ausschließlich die im übernommenen
Soll-Zustand zugeordneten Nodes (`mc_ref.assignments`), nicht irgendein
gescanntes Gerät.

1. **[Ist]** Jeder Heartbeat-Frame (`cls == "HB"`), der die Trace-Pipeline
   durchläuft, aktualisiert einen Zeitstempel je Node
   (`Bench._hb_seen`, `core._tick_loop_body`) — unabhängig vom
   MC-Zustand, reine Buchführung.
2. **[Ist]** `Bench._check_heartbeats()` läuft einmal pro Tick (0,8 s),
   solange verbunden, Trace nicht pausiert, MC aktiv **und** ein
   Soll-Zustand übernommen ist; sonst kein Effekt, gemeldete Ausfälle
   werden sofort verworfen (`_hb_lost.clear()`).
3. **[Ist]** Für jeden Node im Soll-Zustand: Ausfall, wenn der letzte
   Heartbeat länger als `mc.hbTimeoutMs` zurückliegt (Default 3000 ms,
   MC-Karte einstellbar, persistiert in `mc_opts`). Ein **Gnadenfenster**
   von einer Timeout-Länge nach Start/Reconnect/Adopt/Aktivierung
   (`_reset_hb_monitor`) verhindert einen Fehlalarm, bevor der erste
   Heartbeat überhaupt eintreffen konnte.
4. **[Ist]** Zustandswechsel werden geloggt, nicht jeder Tick:
   `MC   node NN — heartbeat lost (no HB for >Ts)` (`emcy0`, roter
   Badge) beim ersten Ausbleiben, `MC   node NN — heartbeat resumed`
   (`info`) sobald wieder ein Heartbeat ankommt — z. B. nach einem
   erneuten Scan, der das Gerät wiederfindet.
5. **[Ist]** Löst **keine** Aktion aus (kein automatischer Re-Address,
   kein erzwungener Re-Scan) — reine Beobachtung. Die Reaktion (Scan &
   verify, Teach, Eingriff vor Ort) bleibt beim Bediener bzw. beim
   bestehenden Auto-Re-Address-Pfad nach einem expliziten Verify.

## Ablauf: Standard-Adressierung (LSS, CiA 305)

Ist-Implementierung (`CanopenBus.lss_assign`, aufgerufen über die
Step-Primitive `lss_assign` aus dem Core-Ablauf `flows/lss_standard.yaml`;
die alte Bus-Primitive `lss_readdress` ist entfernt):

1. Je Ziel-Node 1..count: **Fastscan** (`lss.fast_scan()`) identifiziert
   genau einen **unkonfigurierten** Slave (Factory-Zustand, Node-ID 0xFF —
   konfigurierte Geräte nehmen am Fastscan nicht teil) und lässt ihn im
   Configuration-Zustand zurück → `configure_node_id(node)` +
   `store_configuration()` → `send_switch_state_global(WAITING_STATE)`.
2. Antwortet niemand auf den Fastscan und ist **genau ein** Gerät
   erwartet, wird dieses eine Gerät per globalem State-Switching
   umadressiert (`CONFIGURATION_STATE` → `configure_node_id(1)` +
   `store_configuration()` → `WAITING_STATE`) — der globale Pfad ist nur
   mit einem LSS-Slave am Bus eindeutig.
3. Rückgabe = Anzahl tatsächlich zugewiesener Nodes; der Ablauf
   `lss_standard.yaml` prüft sie gegen `$expected` (`jump_lt` → FAIL)
   und schaltet danach `nmt start (all)`.

**Befund F-6 — durch Neuzuschnitt erledigt:** Die frühere Implementierung
nutzte globales State-Switching für *mehrere* Geräte (alle erhielten am
Ende dieselbe Node-ID). Der globale Pfad wird jetzt nur noch bei genau
einem Gerät verwendet; Mehrgeräte-Kommissionierung läuft über Fastscan.

**Status: auf echter LSS-Hardware ungetestet** — implementiert nach
CiA 305 und canopen-Bibliotheks-API, verifiziert bislang nur gegen den
Demo-Bus (dessen `lss_assign` die virtuellen DUTs auf 1..count
nummeriert). Offene Punkte für den ersten Hardware-Kontakt: Verhalten
bereits konfigurierter Geräte beim Fastscan, Timeout-Tuning, und die
selektive Umadressierung konfigurierter Geräte per
`switch_state_selective` (0x1018-Identität aus dem Scan) als weitere
Ausbaustufe.

## Timing und Timeouts

- Verify-Dauer = Dauer des echten Scans (siehe A-02); keine Kunstpause
  mehr.
- LSS-Antwort-Timeout: canopen-Default (`lss.responses`-Timeout ~1 s);
  je Gerät Fastscan (bitweise Suche, mehrere Telegramme) + 3 Dialoge
  (configure, store, waiting).

## Fehlerpfade

| # | Fehler | Soll-Verhalten |
|---|---|---|
| 1 | Kein Soll-Zustand übernommen | **[umgesetzt]** Verify verweigert, Log `MC   scan & verify skipped — no expected state adopted` |
| 2 | Gerät ohne LSS-Support (keine Antwort auf 0x7E5-Kommandos) | **[Soll]** Timeout je LSS-Dialog, Gerät überspringen, Log je Node (`emcy0`), Ergebnis `mismatch` belassen — heute nicht als eigener Log je Node umgesetzt |
| 3 | `store_configuration` nicht unterstützt (LSS-Fehlercode ≠ 0) | **[Soll]** Warn-Log: neue Node-ID gilt bis zum Power-Cycle, nicht persistent — heute nicht umgesetzt |
| 4 | Verify direkt nach Umadressierung schlägt fehl | keine Endlosschleife: `autoReaddr` löst pro Verify höchstens **einen** Umadressierungsversuch aus, danach Mismatch stehen lassen |

## Beobachtbares Ergebnis

- MC-Panel: `found/expected`, `result` (ok/mismatch), `session`, `last`.
- Logzeilen wie oben; Mismatch als `emcy0` (roter Badge).
- MC-Panel, solange aktiv und Soll-Zustand übernommen: laufende
  Heartbeat-Zeile — neutral „♥ Heartbeat monitoring — watching n
  device(s)" oder rot „⚠ Heartbeat lost — node NN" bei Ausfall.

## Abnahme-Checkliste IXXAT

- [ ] Soll-Zustand mit n Geräten übernehmen (MC-Karte, „Adopt current
      state as expected"); Verify meldet `n/n ✓` (setzt F-4/F-5-Behebung
      voraus).
- [ ] Ein Gerät abklemmen → Verify meldet Mismatch.
- [ ] Umadressierung mit **einem** Gerät: neue Node-ID wird übernommen
      und überlebt einen Power-Cycle (`store_configuration`).
- [ ] Mehrgeräte-Umadressierung erst nach Umsetzung des Soll-Ablaufs
      (F-6) testen — mit der Ist-Implementierung ausdrücklich nicht bei
      mehr als einem Gerät auslösen.
