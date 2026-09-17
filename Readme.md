# FitDays+ → Garmin FIT Export

Dieses Projekt wandelt Gewichtsdaten aus einer FitDays+-CSV in eine Garmin-kompatible FIT-Datei um. Die CSV kann lokal gelesen oder aus Google Drive heruntergeladen werden.

Im Google-Drive-Modus merkt sich das Skript den zuletzt exportierten Messzeitpunkt in einer `.last_check`-Datei im Drive-Ordner. Dadurch werden bei späteren Läufen nur neue Messungen exportiert.

## Funktionen

- deutsche FitDays+-Spalten und englische Feldnamen
- Werte mit Einheiten oder Dezimalkomma, etwa `63,6kg`, `18,4%` und `1495kcal`
- lokale CSV-Dateien oder Google Drive als Datenquelle
- Umrechnung der Skelettmuskulatur von Prozent in Kilogramm
- Berechnung des Körperbau-Werts, wenn er in der CSV fehlt
- lokale Zeitzone `Europe/Berlin` für CSV-Zeitstempel ohne Zeitzone
- optionaler BMI-Export
- Dry-Run und ausführliche Protokollierung

## Voraussetzungen

- Python 3.11 oder neuer
- für lokale CSV-Dateien sind keine zusätzlichen Python-Pakete erforderlich
- für Google Drive: ein Google-Konto, die aktivierte Google Drive API und ein OAuth-Client vom Typ „Desktop-Anwendung“

## Installation

```bash
cd /Pfad/zum/Projekt
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

Nur für Google Drive werden zusätzliche Pakete benötigt:

```bash
python -m pip install google-api-python-client google-auth google-auth-oauthlib
```

## Lokale CSV verwenden

CSV prüfen, ohne eine FIT-Datei zu schreiben:

```bash
python weightlogger_fit_export.py \
  --csv Gewicht.csv \
  --dry-run \
  --verbose
```

FIT-Datei erzeugen:

```bash
python weightlogger_fit_export.py \
  --csv Gewicht.csv \
  --output latest_weight.fit
```

Ohne `--output` wird die FIT-Datei neben der CSV mit der Endung `.fit` angelegt. Bei einer lokalen CSV wird `.last_check` nicht verwendet; jeder Lauf verarbeitet alle vollständigen Messungen.

## Google Drive verwenden

1. Google Drive API für dein Google-Cloud-Projekt aktivieren.
2. Einen OAuth-Client vom Typ „Desktop-Anwendung“ erstellen.
3. Die heruntergeladene JSON-Datei lokal ablegen, beispielsweise als `client_secret.json`.
4. In Google Drive einen Ordner `FitDays-Export` erstellen und dort `Gewicht.csv` ablegen.

OAuth-Dateien und Tokens enthalten Zugangsdaten und dürfen nicht in Git eingecheckt werden. Die mitgelieferte `.gitignore` schließt die üblichen Dateinamen aus.

Erster Testlauf:

```bash
python weightlogger_fit_export.py \
  --google-oauth-client-secret-file client_secret.json \
  --dry-run \
  --verbose
```

Beim ersten Start öffnet sich der Browser für die Google-Anmeldung. Danach speichert das Skript den OAuth-Token standardmäßig lokal in `.google_drive_token.json`.

FIT-Datei erzeugen:

```bash
python weightlogger_fit_export.py \
  --google-oauth-client-secret-file client_secret.json \
  --output latest_weight.fit
```

Nach einem erfolgreichen Export wird `.last_check` im Drive-Ordner auf den neuesten exportierten Messzeitpunkt gesetzt. Die Datei wird beim ersten Export automatisch erstellt. Ein Dry-Run verändert weder `.last_check` noch eine FIT-Datei.

Ordner und CSV können alternativ über Namen oder Google-Drive-IDs ausgewählt werden.

## Einzelnen Messwert exportieren

```bash
python weightlogger_fit_export.py \
  --timestamp 2026-04-12T08:00:00+02:00 \
  --weight 63.6 \
  --percent-fat 18.4 \
  --percent-hydration 59.8 \
  --muscle-mass 29.2 \
  --daily-calorie-intake 1495 \
  --visceral-fat-rating 3 \
  --bone-mass 3.5 \
  --metabolic-age 48 \
  --bmi 22.4 \
  --output latest_weight.fit
```

Wenn `--physique-rating` fehlt, versucht das Skript den Wert aus Körperfett und Muskelmasse abzuleiten.

## Letzten Exportzeitpunkt anzeigen

Mit `--check` (oder `-c`) kann der letzte Exportzeitpunkt angezeigt werden. Die Funktion sucht zuerst in Google Drive (wenn `--google-oauth-client-secret-file` angegeben ist), dann lokal und erstellt bei Bedarf eine neue `.last_check`-Datei:

```bash
# Lokal
python weightlogger_fit_export.py --check

# Mit Google Drive
python weightlogger_fit_export.py \
  --google-oauth-client-secret-file client_secret.json \
  --check
```

## Neueste CSV-Datei verwenden

Mit `--last-csv` (oder `-l`) wird automatisch die neueste CSV-Datei verwendet. In Google Drive nach `createdTime`, lokal nach Änderungsdatum (`st_mtime`):

```bash
# Neueste lokale CSV
python weightlogger_fit_export.py -l --output latest.fit

# Neueste CSV aus Google Drive
python weightlogger_fit_export.py \
  --google-oauth-client-secret-file client_secret.json \
  -l --output latest.fit
```

## Unterstützte CSV-Spalten

| FitDays+-Spalte | Englischer Feldname | Verwendung |
|---|---|---|
| `Zeit` | `timestamp` | Messzeitpunkt |
| `Gewicht` | `weight` | Gewicht in kg |
| `Körperfettanteil` | `percent_fat` | Körperfett in Prozent |
| `Körperwasser` | `percent_hydration` | Körperwasser in Prozent |
| `Skelettmuskulatur` | `skeletal_muscle_rate` | Prozentwert; wird in kg umgerechnet |
| `BMR` | `daily_calorie_intake` | Grundumsatz in kcal |
| `Eingeweidefett` | `visceral_fat_rating` | Viszeralfett-Bewertung |
| `Knochengewicht` | `bone_mass` | Knochenmasse in kg |
| `Körperalter` | `metabolic_age` | Metabolisches Alter |
| `Körperbau` | `physique_rating` | Optional; wird andernfalls berechnet |
| `BMI` | `bmi` | Optionales, experimentelles FIT-Feld |

Leere Werte und `--` gelten als fehlende Werte. Exportiert werden nur Messungen mit Zeitstempel, Gewicht, Körperfett, Körperwasser, Muskelmasse, BMR, Eingeweidefett, Knochengewicht, Körperalter und Körperbau. Der Körperbau kann automatisch berechnet werden. BMI und Benutzerprofil-Index sind optional.

Mit `--verbose` zeigt das Skript, welche Messungen wegen fehlender Werte übersprungen werden.

## Skelettmuskulatur und Körperbau

FitDays+ liefert die Skelettmuskulatur häufig als Prozentwert, Garmin erwartet in diesem FIT-Feld jedoch Kilogramm. Das Skript rechnet deshalb beispielsweise so um:

```text
Gewicht:            63,6 kg
Skelettmuskulatur:  46,1 %
Muskelmasse:        29,3 kg
```

Falls `Körperbau` fehlt, berechnet das Skript heuristisch einen Wert von 1 bis 9 aus Körperfett und Skelettmuskulatur. Das ist eine Annäherung und keine offizielle FitDays+-Berechnung.

## Parameter

| Parameter | Beschreibung |
|---|---|
| `--csv` | lokale CSV-Eingabedatei |
| `--output` | Pfad der erzeugten FIT-Datei |
| `--google-oauth-client-secret-file` | lokale OAuth-Client-JSON-Datei |
| `--google-oauth-token-file` | lokaler OAuth-Token; Standard: `.google_drive_token.json` |
| `--google-drive-folder-name` | Drive-Ordnername; Standard: `FitDays-Export` |
| `--google-drive-folder-id` | alternative direkte Drive-Ordner-ID |
| `--google-drive-csv-file-name` | CSV-Dateiname; Standard: `Gewicht.csv` |
| `--google-drive-csv-file-id` | alternative direkte Drive-Datei-ID |
| `--timestamp`, `--weight` | Pflichtwerte für einen einzelnen CLI-Messwert |
| `--percent-fat`, `--percent-hydration`, `--muscle-mass` | Körperzusammensetzung |
| `--daily-calorie-intake` | BMR/Grundumsatz |
| `--physique-rating` | Körperbau-Wert von 1 bis 9 |
| `--visceral-fat-rating`, `--bone-mass`, `--metabolic-age` | weitere Messwerte |
| `--user-profile-index` | optionaler FIT-Benutzerprofil-Index |
| `--bmi` | optionales, experimentelles BMI-Feld |
| `--check`, `-c` | zeigt den letzten Exportzeitpunkt aus `.last_check` an (Google Drive, lokal oder neu erstellt) |
| `--dry-run` | prüfen, ohne FIT-Datei oder `.last_check` zu verändern |
| `--last-csv`, `-l` | verwendet die neueste CSV-Datei (Drive: nach createdTime, lokal: nach Änderungsdatum) |
| `--verbose` | zusätzliche Protokollausgaben |

Die vollständige Parameterliste zeigt:

```bash
python weightlogger_fit_export.py --help
```

## Datenschutz und Git

FitDays+-CSV-Dateien enthalten persönliche Gesundheitsdaten. Reale CSV-Exporte, OAuth-Dateien, Tokens, `.last_check` und erzeugte FIT-Dateien werden deshalb nicht versioniert. Eine bewusst anonymisierte Beispieldatei kann unter einem Namen wie `example/Gewicht.example.csv` eingecheckt werden. Prüfe vor dem ersten Push trotzdem immer:

```bash
git status --short
```

Es findet kein automatischer Upload zu Garmin Connect statt. Die FIT-Datei wird ausschließlich lokal erzeugt.
