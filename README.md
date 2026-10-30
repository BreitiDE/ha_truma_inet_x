# Truma iNet X (Bluetooth) – Home-Assistant-Integration `ha_truma_inet_x`

Version 1.0.1

Steuert eine Truma-Heizung (z. B. **Combi 6 E**) über das **Truma iNet X Panel** per Bluetooth LE –
direkt aus Home Assistant, ohne Cloud und ohne Smartphone. Die Integration spricht dasselbe Protokoll
wie die offizielle Truma-iNet-X-App (Version 3.5.0 analysiert).

> **Status:** Das Protokoll wurde aus der App rekonstruiert und gegen ein simuliertes Panel getestet
> (automatisierte Tests). Ein Test an einem echten Panel steht noch aus. Bei Problemen bitte die
> Diagnosedaten (Geräteseite → *Diagnose herunterladen*) und ein Debug-Log sichern.

## Voraussetzungen

* Home Assistant 2025.2 oder neuer mit funktionierender Bluetooth-Integration
* Am besten ein **lokaler Bluetooth-Adapter** (USB-Dongle/Onboard) in Reichweite des Panels.
  ESPHome-Bluetooth-Proxys funktionieren für den Betrieb, die PIN-Kopplung geht aber nur über
  einen lokalen Adapter (BlueZ).
* Die Truma-App darf während der Einrichtung **nicht** mit dem Panel verbunden sein.

## Installation

1. Den Ordner `custom_components/ha_truma_inet_x` nach `/config/custom_components/` kopieren
   – oder in HACS dieses GitHub-Repository als *Benutzerdefiniertes Repository* (Typ *Integration*)
   hinzufügen und dort installieren.
2. Home Assistant neu starten.
3. Das Panel wird automatisch per Bluetooth erkannt (*Einstellungen → Geräte & Dienste*), alternativ
   *Integration hinzufügen → Truma iNet X (Bluetooth)*.

## Kopplung

Das iNet X Panel koppelt **ohne Code** („Just Works“) – wie bei der Truma-App.

1. Die Truma-iNet-X-App auf **allen** Handys vollständig schließen (auch im Hintergrund). Das Panel
   akzeptiert nur eine App-Verbindung gleichzeitig.
2. Am Panel im Hauptmenü auf **„App“** tippen und den Anweisungen auf dem Display folgen. Das Panel
   meldet dann per Bluetooth „Kopplung aktiv“ – der Einrichtungsdialog zeigt diesen Status an.
3. Im Dialog direkt **Absenden** klicken (PIN-Feld leer lassen). Home Assistant wartet bis zu 30 s
   auf „Kopplung aktiv“, koppelt über BlueZ und meldet sich am Panel an – mit einer eigenen,
   dauerhaft gespeicherten Identität (Name „Home Assistant“), wie ein weiteres Smartphone.

**Legacy-Pairing (Standard: an):** Das Panel nutzt LE-Legacy-Pairing. Unter Linux klappt die Kopplung
zuverlässig nur, wenn „Secure Connections“ am Adapter währenddessen aus ist. Die Integration schaltet
das – wie `btmgmt sc off` / `btmgmt io-cap 3` – nur für die Dauer der Kopplung um und stellt es danach
wieder zurück.

Ist Home Assistant bereits gekoppelt, *Bereits gekoppelt* anhaken. Manuelle Kopplung auf dem Host:

```text
btmgmt sc off
btmgmt io-cap 3
bluetoothctl
  agent NoInputNoOutput
  default-agent
  pair <MAC>
  trust <MAC>
btmgmt sc on
```

## Handy-App parallel nutzen

Solange Home Assistant verbunden ist, weist das Panel die Handy-App ab („Ein anderes Smartphone ist
mit dem iNet X verbunden“). Mit dem Schalter **„Bluetooth für Handy-App freigeben“** (oder dem Dienst
`ha_truma_inet_x.release_bluetooth`) trennt sich Home Assistant für die eingestellte Zeit
(Standard 10 Minuten, in den Optionen änderbar) und verbindet sich danach automatisch wieder.
Ausschalten beendet die Freigabe vorzeitig. Für Firmware-Updates des Panels wird die Handy-App benötigt.

## Entitäten

Alle Entitäten entstehen automatisch aus den Parametern, die das Panel meldet – was die Anlage nicht
hat, wird nicht angelegt.

| Entität | Funktion |
|---|---|
| `climate` | Aus / Heizen / Lüften (bei passender Anlage auch Automatik, Kühlen), Solltemperatur, Ist-Temperatur, Heizmodus als Preset |
| `water_heater` Warmwasser | Aus / Eco 40 °C / Komfort 60 °C / Heiß 70 °C, Wassertemperatur |
| `switch` | Gas, Warmwasser-Boost, Elektro bzw. Diesel (wenn nur Ein/Aus), Bluetooth für Handy-App freigeben |
| `select` | Elektro-Leistungsstufe (z. B. 900 W/1800 W), Energiequellen-Priorität, Heizmodus |
| `number` | Lüftungsstufe |
| `sensor` | Raum-, Wasser-, Innen-/Außentemperatur, Heiz-/Warmwasserstatus, Fehlercode, Batteriespannungen |
| `binary_sensor` | Verbunden, Raumheizung aktiv, Warmwasser aktiv, Landstrom, Störung |

### Dienste

* `ha_truma_inet_x.write_parameter` – beliebigen Parameter schreiben (Expertenfunktion), z. B.
  `topic: AirHeating`, `parameter: TgtTemp`, `value: 215` (= 21,5 °C)
* `ha_truma_inet_x.dump_parameters` – alle gemeldeten Parameter als Antwort zurückgeben
* `ha_truma_inet_x.rediscover` – Geräte- und Parametererkennung erneut ausführen
* `ha_truma_inet_x.release_bluetooth` – Verbindung für `minutes` Minuten freigeben (0 = Freigabe beenden)

## Optionen

* Name, unter dem Home Assistant am Panel erscheint
* Dauer der Bluetooth-Freigabe für die Handy-App
* Characteristic für Transport-Benachrichtigungen: *Automatisch* (`FC314001`, empfohlen) oder
  `FC314004` (so macht es die Android-App; bei einer Combi D 4 E führte das zu Fehlern)

## Fehlersuche

```yaml
logger:
  logs:
    custom_components.ha_truma_inet_x: debug
```

* **„Kopplung nicht aktiv“:** Am Panel im Hauptmenü auf „App“ tippen und sofort absenden.
* **„Anderes Smartphone verbunden“:** Truma-App auf allen Handys schließen (auch im Hintergrund).
* **Keine Anmeldung / `no_registration`:** Kopplung prüfen; Truma-App schließen; alte Kopplung am
  Panel bzw. in Home Assistant entfernen und neu koppeln.
* **ESPHome-Bluetooth-Proxy:** Die Kopplung inkl. Legacy-Umschaltung geht nur über einen lokalen
  Adapter. Danach kann der Betrieb auch über einen Proxy laufen.
* **„Gerät nicht lokal“ bei der Kopplung:** Bluetooth-Integration auf aktives Scannen stellen oder mit
  `bluetoothctl` koppeln und *Bereits gekoppelt* wählen.
* Der Binärsensor *Verbunden* zeigt in seinen Attributen Adresse, Protokollversion, Busgeräte und
  Transportstatistik.

## Hinweis

Inoffizielles Projekt, nicht mit der Truma Gerätetechnik GmbH & Co. KG verbunden. Nutzung auf eigene Gefahr.
