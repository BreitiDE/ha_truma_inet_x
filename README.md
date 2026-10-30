# Truma iNet X (Bluetooth) – Home-Assistant-Integration `ha_truma_inet_x`

Version 1.0.0

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

1. Panel in den Kopplungsmodus versetzen (Panel-Menü: Einstellungen → Bluetooth/Verbindungen →
   neues Gerät koppeln). Der Einrichtungsdialog zeigt an, ob das Panel gerade koppelbereit ist.
2. Die am Panel angezeigte **6-stellige PIN** eingeben und absenden.
3. Die Integration koppelt über BlueZ, meldet sich am Panel an und stellt sich mit einer eigenen,
   dauerhaft gespeicherten Identität (`Muid`/`Uuid`, Name „Home Assistant“) vor – wie ein weiteres Smartphone.

Wurde Home Assistant schon anders gekoppelt (z. B. `bluetoothctl` → `pair <MAC>`), *Bereits gekoppelt*
anhaken.

## Entitäten

Alle Entitäten entstehen automatisch aus den Parametern, die das Panel meldet – was die Anlage nicht
hat, wird nicht angelegt.

| Entität | Funktion |
|---|---|
| `climate` | Aus / Heizen / Lüften (bei passender Anlage auch Automatik, Kühlen), Solltemperatur, Ist-Temperatur, Heizmodus als Preset |
| `water_heater` Warmwasser | Aus / Eco 40 °C / Komfort 60 °C / Heiß 70 °C, Wassertemperatur |
| `switch` | Gas, Warmwasser-Boost, Elektro bzw. Diesel (wenn nur Ein/Aus) |
| `select` | Elektro-Leistungsstufe (z. B. 900 W/1800 W), Energiequellen-Priorität, Heizmodus |
| `number` | Lüftungsstufe |
| `sensor` | Raum-, Wasser-, Innen-/Außentemperatur, Heiz-/Warmwasserstatus, Fehlercode, Batteriespannungen |
| `binary_sensor` | Verbunden, Raumheizung aktiv, Warmwasser aktiv, Landstrom, Störung |

### Dienste

* `ha_truma_inet_x.write_parameter` – beliebigen Parameter schreiben (Expertenfunktion), z. B.
  `topic: AirHeating`, `parameter: TgtTemp`, `value: 215` (= 21,5 °C)
* `ha_truma_inet_x.dump_parameters` – alle gemeldeten Parameter als Antwort zurückgeben
* `ha_truma_inet_x.rediscover` – Geräte- und Parametererkennung erneut ausführen

## Optionen

* Name, unter dem Home Assistant am Panel erscheint
* Characteristic für Transport-Benachrichtigungen: *Automatisch* (`FC314001`, empfohlen) oder
  `FC314004` (so macht es die Android-App; bei einer Combi D 4 E führte das zu Fehlern)

## Fehlersuche

```yaml
logger:
  logs:
    custom_components.ha_truma_inet_x: debug
```

* **Keine Anmeldung / `no_registration`:** Kopplung prüfen; Truma-App schließen; Panel neu koppeln.
* **„Gerät nicht lokal“ bei der Kopplung:** Bluetooth-Integration auf aktives Scannen stellen oder mit
  `bluetoothctl` koppeln und *Bereits gekoppelt* wählen.
* Der Binärsensor *Verbunden* zeigt in seinen Attributen Adresse, Protokollversion, Busgeräte und
  Transportstatistik.

## Hinweis

Inoffizielles Projekt, nicht mit der Truma Gerätetechnik GmbH & Co. KG verbunden. Nutzung auf eigene Gefahr.
