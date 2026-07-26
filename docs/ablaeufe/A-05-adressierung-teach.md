# A-05 — Adressierung im Button-Teach-Verfahren

Rekonstruiert aus einem kommentierten Bus-Mitschnitt der Zielgeräte
(anonymisiert — das Tool ist für eine Open-Source-Veröffentlichung
vorgesehen, Geräte- und Herstellernamen bleiben außen vor). Das Verfahren
ist **kein LSS**, sondern ein proprietäres Broadcast-Protokoll, von dem es
am Markt viele herstellerspezifische Varianten gibt; deshalb ist der
Ablauf eine **austauschbare Datei** und das aktive Verfahren in den
Machine-Control-Optionen **wählbar**. Seit der Plugin-Aufteilung
(docs/extending.md) liefert ein Vendor-Plugin diese Datei über
`flow_dirs()`, dazu die Session-Identität (`AddressingProvider`) und
die Demo-Bus-Simulation (`DemoHook`);
der neutrale Core bringt als Alternative die Standard-Adressierung
nach CiA 305 mit (`flows/lss_standard.yaml` über die Step-Primitive
`lss_assign`, siehe A-03 — die alte Bus-Primitive `lss_readdress`
ist entfallen).

## Zweck und Auslöser

Node-IDs in physischer Montage-Reihenfolge vergeben (ab 01 aufsteigend)
und anschließend allen Geräten eine gemeinsame Session-ID verteilen.
**Der Master initiiert**, der Bediener treibt per Knopfdruck:

1. **Maschinensteuerung aktiv** (`mc.enabled`): Beim Start wird die
   Systemvalidierung (A-03 Scan & Verify) ausgeführt — da das Tool offline
   startet, konkret beim Verbinden des Adapters (Option `scanStart`).
   Schlägt sie fehl (Gerät fehlt oder fremde Session-ID) → **Teach startet
   automatisch** (Option `autoReaddr`, default an) und wartet auf den
   Bediener. Ist die Maschinensteuerung **nicht aktiv**, passiert nie
   etwas automatisch.
2. Bediener fordert die Adressierung manuell an
   („Re-address (teach)" im Machine-Control-Panel → `act_mc_readdress`) —
   unabhängig vom Maschinensteuerungs-Modus.

Geräte sind standardmäßig unadressiert; bereits adressierte Geräte werden
durch den Ablauf neu vergeben (sie heartbeaten vorher unter ihrer alten
ID weiter, siehe Mitschnitt: 701/702 aktiv vor dem Teach).

## Akteure

Bediener (Buttons) → UI → Teach-Ablauf (austauschbare YAML-Sequenz im
erweiterten Testfall-Format, siehe „Umsetzung") → generische
Bus-Primitiven (Raw-Frame senden / auf Frame warten) → DUTs.

## Protokoll (aus dem kommentierten Mitschnitt)

Das 0x781/0x783-Telegramm ist 8 Byte:
**`[SerialNr:4][SessionId:1][AddrControl:1][00 00]`** — SerialNr ist die
Kennung des Masters (das Tool erzeugt sie einmalig je Workspace),
SessionId ein je Adressierungslauf inkrementiertes Byte. AddrControl:

| Code | Sender | Bedeutung |
|---|---|---|
| 2 | Master (0x781) | Adressierung starten — Slaves verwerfen ihre Node-IDs |
| 1 | Gerät (0x783) | **Addr-End**: ein bereits adressiertes Gerät signalisiert das Ende (Button) |
| 3 | Master (0x781) | Adressierung beenden — alle Slaves speichern SerialNr + SessionId + Node-ID |
| 0 | Master (0x781) | Systemvalidierung anfordern — Slaves quittieren „ok" mit Boot-up |

Weitere Telegramme:

| COB-ID | Richtung | Inhalt | Bedeutung |
|---|---|---|---|
| 0x000 | Master | NMT Reset Communication (alle) | Initialisierung vor der Adressierung |
| **0x780** | Master → alle | 1 Byte: nächste Node-ID | „Angebot": das nächste Gerät, dessen Confirm-Button gedrückt wird, übernimmt diese ID |
| 0x700+ID | Gerät → alle | Boot-up (`00`) | Bestätigung: Gerät hat die angebotene ID übernommen (bzw. „SysVal ok" nach AddrControl 0) |
| 0x000 | Master | NMT Preop/Start (alle) | Abschluss-Sequenz |

Beobachtung im Mitschnitt: Das 0x783 des Geräts trägt SerialNr teilweise
genullt (`dd 00 00 00 …`) — Ursache unklar, das Tool wartet daher nur auf
die COB-ID 0x783, ohne die Datenbytes zu prüfen.

## Geräteseitiges Verhalten (Nutzer-Angabe)

Ein Gerät, das seine Node-ID bestätigt hat, wartet ausschließlich auf das
Signal **„Adressierungs-Ende"** — auslösbar per Button an diesem **oder
einem beliebigen anderen** bereits adressierten Gerät („Addr-End").
Jegliche anderen Eingaben werden im Node ausgeschlossen (andere Buttons
als Addr-End ignoriert); einen „erneuten Knopfdruck übernimmt eine neue
ID"-Fall gibt es damit nicht. Der Master beendet die Vergabephase, sobald
die Soll-Anzahl (`$expected`) erreicht ist **oder** ein Gerät Addr-End
auf 0x783 signalisiert — die Ablauf-Datei prüft **beides gleichzeitig in
einem einzigen `wait_for`** (Listenform von `cob`/`data`, siehe
`testfall-format.md`), nicht mehr nacheinander in getrennten Wartefenstern:
ein Addr-End-Telegramm kann so nicht mehr in einer Lücke zwischen zwei
Polls verpasst werden (siehe `PROTOCOL.md` im Vendor-Verzeichnis für den
konkreten Fehlerfall, der das ausgelöst hat).

## Ablauf

1. **Initialisierung**: NMT Reset Communication (alle); dann 0x781 mit
   AddrControl 2 — die Slaves verwerfen ihre Node-IDs. Neue Session
   (SerialNr bleibt, SessionId inkrementiert). Guard: verbunden, kein
   Scan/Teach aktiv. Obergrenze n: Beim **Bediener-Teach** das obere
   Ende der Address-Range (Bus-Interface) — der Teach läuft offen und
   endet über Addr-End bzw. wenn keine Bestätigung mehr kommt; so kann
   eine gewachsene Maschine ohne Vorwissen adressiert werden. Beim
   **Auto-Re-Address** (nach gescheiterter Verifikation) dagegen
   `expected` des übernommenen Soll-Zustands (`mc_ref`).
2. **Je Gerät k = 1…n**:
   a. Master sendet 0x780 mit `k` (Angebot).
   b. UI-Prompt: „Confirm-Button am nächsten Gerät drücken" (physische
      Montage-Reihenfolge = ID-Reihenfolge).
   c. Bediener drückt den Button → Gerät übernimmt ID k, sendet Boot-up
      auf 0x700+k, heartbeatet Pre-Operational.
   d. Master erkennt das Boot-up → weiter mit k+1 (im Mitschnitt folgt
      das nächste Angebot < 1 ms nach dem Boot-up). Parallel prüft der
      Master auf **0x783 (Addr-End)** — dann endet die Vergabe sofort.
3. **Ende**: 0x781 mit AddrControl 3 — alle Slaves speichern SerialNr,
   SessionId und ihre Node-ID persistent (Quittung u. a. per EMCY
   „Error reset or no error", gerätetypabhängig).
4. **Systemvalidierung**: 0x781 mit AddrControl 0 + NMT Enter
   Pre-Operational (alle) — jeder Slave quittiert „SysVal ok" mit einem
   Boot-up.
5. **Start**: NMT Start (alle) → Heartbeats Operational, EMCYs
   („Error reset"/gerätespezifisch).
6. **Abschluss des Tools**: Nach einem **Bediener-Teach** wird
   gescannt und der frisch adressierte Bus als Soll-Zustand übernommen
   (Adopt) — die Knopfdrücke an den physischen Geräten sind die
   Bestätigung des Bedieners, ein Verify gegen die alte Referenz wäre
   sinnlos. Nach einem **Auto-Re-Address** dagegen Verify-Scan nach
   A-03 gegen den unveränderten Soll-Zustand (`mc_ref`) — er stellt
   den Soll-Zustand wieder her und darf einen geschrumpften Bus nie
   stillschweigend übernehmen, sonst verschwände ein totes Gerät aus
   der Verifikation. Die neue Session schreibt jeder Teach in die
   Referenz zurück.

## Timing und Timeouts

- Warten auf Confirm: ~50 s je Gerät (100 Prüfrunden à 0,5 s, je Runde
  ein gemeinsamer Wait auf Boot-up **und** Addr-End in der Ablauf-Datei),
  danach Abbruch mit Fehlerlog.
- Kurze Pausen zwischen Ende-, SysVal- und NMT-Schritten (0,5–1,5 s),
  angelehnt an den Mitschnitt.

## Fehlerpfade

| # | Fehler | Verhalten |
|---|---|---|
| 1 | Kein Knopfdruck innerhalb des Timeouts | Teach-Abbruch, Log (`emcy0`); bereits vergebene IDs bleiben gültig |
| 2 | Bediener bricht ab (UI „Abort") | wie 1, mit „aborted by operator" |
| 3 | Boot-up einer **anderen** als der angebotenen ID | wird ignoriert (Master wartet gezielt auf 0x700+k) |
| 4 | Verbindungsverlust | Teach-Abbruch |
| 5 | Verify nach Teach schlägt fehl | Ergebnis Mismatch stehen lassen — **kein** automatischer zweiter Teach (keine Schleife) |

## Beobachtbares Ergebnis

- Machine-Control-Panel: Teach-Karte mit Fortschritt aus dem
  `teach`-Snapshot-Feld (`{step, of, text}`, z. B. „press the button on
  device k of n"), Abort; im Demo-Modus zusätzlich „Simulate button
  press". Neue Session-ID in der Status-Karte.
- Logzeilen kommen aus dem Ablauf selbst (`log`-Schritte und die
  Bus-Primitive, z. B. „offering node-ID …"), nicht aus einer festen
  `MC   teach k/n — node kk assigned`-Zeile; abschließend die
  A-03-Verify-Zeilen.

## Offene Punkte (am Gerät bestätigen)

- [ ] Warum das 0x783 (Addr-End) SerialNr/SessionId teilweise genullt
      trägt; ob die Datenbytes geprüft werden sollten.
- [ ] Wie die Geräte die Session-ID beim Verify zurückmelden (eigenes
      Objekt? Vergleich derzeit nur Tool-seitig über Anzahl+Zuordnung).
- [ ] Erwartet ein Gerätetyp nach AddrControl 3 eine Wiederholung des
      Broadcasts (ältere Mitschnitte zeigten das Ende-Telegramm doppelt)?

## Umsetzung

Der Ablauf ist **nicht fest verdrahtet**, sondern liegt als austauschbare
YAML-Datei im Sequenz-Format vor (`data/flows/teach_addressing.yaml` im
Workspace, beim ersten Start aus der mitgelieferten Vorlage kopiert) —
herstellerspezifische Varianten sind damit reine Datei-Tausche. Möglich
macht das die Format-v2-Erweiterung (Register, Sprünge, Arithmetik,
`can_send`/`wait_for` auf COB-ID — siehe `testfall-format.md`).

`EdsDemoBus` simuliert die Geräteseite: „Simulate button press" lässt das
nächste virtuelle Gerät die auf 0x780 angebotene ID übernehmen und das
Boot-up senden; der Session-Broadcast wird gespeichert. Damit ist der
komplette Ablauf ohne Hardware übbar (`tests/test_teach.py`).
