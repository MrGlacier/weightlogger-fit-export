#!/usr/bin/env python3
"""Erzeugt Garmin-FIT-Dateien fuer Gewichtsdaten im Stil von WeightLogger.

Was das Script macht:
- Liest Gewichtsdaten entweder aus einer lokalen CSV, aus Google Drive oder direkt ueber CLI-Parameter.
- Wandelt die Messwerte in ein Garmin-kompatibles FIT-Format um.
- Kann sich den zuletzt exportierten Zeitstempel ueber eine `.last_check`-Datei in Google Drive merken,
  damit bereits exportierte Werte nicht doppelt in die FIT-Datei geschrieben werden.

Voraussetzungen:
- Python 3.11 oder neuer.
- Fuer Google-Drive-Zugriff werden diese Pakete benoetigt:
  `pip install google-api-python-client google-auth google-auth-oauthlib`
- Fuer OAuth wird eine Google-OAuth-Client-JSON-Datei benoetigt.

Typische Aufrufe:
1. Lokale CSV in FIT umwandeln:
   `python weightlogger_fit_export.py --csv gewicht.csv --output gewicht.fit`

2. Google-Drive-CSV verwenden:
   `python weightlogger_fit_export.py --google-oauth-client-secret-file client_secret.json --output gewicht.fit`

3. Erst testen, ohne FIT-Datei zu schreiben und ohne `.last_check` zu aktualisieren:
   `python weightlogger_fit_export.py --google-oauth-client-secret-file client_secret.json --dry-run --verbose`

4. Einzelnen Messwert direkt uebergeben:
   `python weightlogger_fit_export.py --timestamp 2026-04-12T08:00:00+02:00 --weight 63.6 --output gewicht.fit`

Wichtige Hinweise:
- Standardmaessig sucht das Script in Google Drive den Ordner `FitDays-Export` und darin die Datei `Gewicht.csv`.
- Die CSV darf deutsche Spaltennamen enthalten, zum Beispiel `Zeit`, `Gewicht`, `Körperfettanteil` und `Skelettmuskulatur`.
- Werte wie `63,6kg`, `18,4%` oder `1495kcal` werden bereinigt und geparst.
- Leere Werte oder `--` werden als fehlende Werte behandelt.
- Nur vollstaendige Messwerte werden exportiert. Wenn fuer einen Eintrag Pflichtwerte fehlen,
  wird dieser komplett uebersprungen.
- Die CSV-Spalte `Skelettmuskulatur` wird verwendet. Falls sie als Prozentwert vorliegt,
  rechnet das Script den Wert in Kilogramm um, weil Garmin hier Kilogramm erwartet.
- Das FIT-Feld `Körperbau` wird, falls in der CSV nicht vorhanden, heuristisch aus Körperfett und
  Skelettmuskulatur auf eine Bewertung von 1 bis 9 gemappt.
- CSV-Zeitwerte ohne Zeitzone werden als lokale Zeit in `Europe/Berlin` interpretiert.
- BMI bleibt optional und ist weiterhin experimentell.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import logging
import pathlib
import struct
import sys
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from typing import Iterable

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload, MediaInMemoryUpload
except ImportError:  # pragma: no cover - optional dependency
    Request = None
    Credentials = None
    InstalledAppFlow = None
    build = None
    MediaIoBaseDownload = None
    MediaInMemoryUpload = None

FIT_EPOCH = dt.datetime(1989, 12, 31, tzinfo=dt.timezone.utc)
PROFILE_VERSION = 1600
PROTOCOL_VERSION = 0x20
FILE_TYPE_WEIGHT = 9
MESG_NUM_FILE_ID = 0
MESG_NUM_WEIGHT_SCALE = 30
MANUFACTURER_DEVELOPMENT = 255
PRODUCT_ID = 1
SERIAL_NUMBER = 0

BASE_TYPE_ENUM = 0x00
BASE_TYPE_UINT8 = 0x02
BASE_TYPE_UINT16 = 0x84
BASE_TYPE_UINT32 = 0x86
BASE_TYPE_UINT32Z = 0x8C

INVALID_UINT8 = 0xFF
INVALID_UINT16 = 0xFFFF
INVALID_UINT32 = 0xFFFFFFFF

GOOGLE_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
LAST_CHECK_FILE_NAME = ".last_check"
DEFAULT_GOOGLE_TOKEN_FILE = ".google_drive_token.json"
LOCAL_TIMEZONE = ZoneInfo("Europe/Berlin")


REQUIRED_CSV_COLUMNS = {"timestamp", "weight"}

REQUIRED_COMPLETE_MEASUREMENT_FIELDS = (
    "weight",
    "percent_fat",
    "percent_hydration",
    "muscle_mass",
    "daily_calorie_intake",
    "visceral_fat_rating",
    "bone_mass",
    "metabolic_age",
)

CSV_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp": ("timestamp", "Zeit"),
    "weight": ("weight", "Gewicht"),
    "percent_fat": ("percent_fat", "Körperfettanteil"),
    "percent_hydration": ("percent_hydration", "Körperwasser"),
    "skeletal_muscle_rate": ("skeletal_muscle_rate", "Skelettmuskulatur"),
    "physique_rating": ("physique_rating", "Körperbau"),
    "daily_calorie_intake": ("daily_calorie_intake", "BMR"),
    "visceral_fat_rating": ("visceral_fat_rating", "Eingeweidefett"),
    "bone_mass": ("bone_mass", "Knochengewicht"),
    "metabolic_age": ("metabolic_age", "Körperalter"),
    "bmi": ("bmi", "BMI"),
}
LOGGER = logging.getLogger(__name__)


@dataclass
class Measurement:
    timestamp: dt.datetime
    weight: float
    percent_fat: float | None = None
    percent_hydration: float | None = None
    skeletal_muscle_rate: float | None = None
    muscle_mass: float | None = None
    daily_calorie_intake: float | None = None
    physique_rating: int | None = None
    visceral_fat_rating: int | None = None
    bone_mass: float | None = None
    metabolic_age: int | None = None
    user_profile_index: int | None = None
    bmi: float | None = None


# `Measurement` ist das zentrale Datenobjekt des Scripts.
# Hier landen alle Messwerte in normalisierter Form, bevor sie spaeter
# in FIT-Datensaetze umgewandelt werden.


def configure_logging(verbose: bool) -> None:
    """Initialisiert das Logging fuer normale Ausgaben und Debug-Hinweise."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )


def csv_row_is_empty(row: dict[str, str | None]) -> bool:
    """Prueft, ob eine CSV-Zeile komplett leer ist und ignoriert werden kann."""
    return all((value or "").strip() == "" for value in row.values())


def ensure_required_csv_columns(fieldnames: list[str] | None) -> None:
    """Stellt sicher, dass die benoetigten logischen CSV-Spalten vorhanden sind."""
    if fieldnames is None:
        raise ValueError("CSV-Datei ist leer")

    missing_columns = [
        logical_name for logical_name in REQUIRED_CSV_COLUMNS if find_csv_column_name(fieldnames, logical_name) is None
    ]
    if missing_columns:
        missing_columns_text = ", ".join(sorted(missing_columns))
        raise ValueError(f"CSV-Datei hat nicht alle Pflichtspalten: {missing_columns_text}")


def find_csv_column_name(fieldnames: list[str], logical_name: str) -> str | None:
    """Sucht zu einem internen Feldnamen den passenden echten CSV-Spaltennamen."""
    aliases = CSV_HEADER_ALIASES.get(logical_name, (logical_name,))
    for alias in aliases:
        if alias in fieldnames:
            return alias
    return None


def normalize_csv_row(row: dict[str, str | None], fieldnames: list[str]) -> dict[str, str]:
    """Mappt eine CSV-Zeile ueber Aliasnamen auf die internen Feldnamen des Scripts."""
    normalized_row: dict[str, str] = {}
    for logical_name in CSV_HEADER_ALIASES:
        column_name = find_csv_column_name(fieldnames, logical_name)
        if column_name is None:
            continue
        normalized_row[logical_name] = (row.get(column_name) or "").strip()
    return normalized_row


# Diese Hilfsfunktion prueft, ob ein Messwert vollstaendig genug fuer den Export ist.
def measurement_is_complete(measurement: Measurement) -> tuple[bool, list[str]]:
    """Prueft, ob ein Messwert alle fuer den Export benoetigten Felder enthaelt."""
    missing_fields: list[str] = []
    for field_name in REQUIRED_COMPLETE_MEASUREMENT_FIELDS:
        if getattr(measurement, field_name) is None:
            missing_fields.append(field_name)

    if measurement.physique_rating is None:
        missing_fields.append("physique_rating")
    return len(missing_fields) == 0, missing_fields


def parse_measurement_float(raw_value: str) -> float | None:
    """Parst numerische Messwerte und entfernt dabei Einheiten wie kg, %, oder kcal."""
    normalized = raw_value.strip()
    if normalized == "" or normalized == "--":
        return None
    normalized = normalized.replace("kg", "").replace("%", "").replace("kcal", "").strip()
    normalized = normalized.replace(",", ".")
    return float(normalized)


def parse_measurement_int(raw_value: str) -> int | None:
    """Parst einen Messwert ganzzahlig und rundet bei Bedarf sauber."""
    parsed_value = parse_measurement_float(raw_value)
    return None if parsed_value is None else int(round(parsed_value))


# Diese Hilfsfunktion verarbeitet Muskelmasse sowohl in kg als auch in Prozent.
# Prozentwerte werden anhand des aktuellen Koerpergewichts in kg umgerechnet.
def parse_muscle_mass(raw_value: str, weight_kg: float) -> float | None:
    """Parst Muskelmasse entweder direkt in kg oder rechnet Prozentwerte in kg um."""
    normalized = raw_value.strip()
    if normalized == "" or normalized == "--":
        return None
    if "%" in normalized:
        percentage_value = parse_measurement_float(normalized)
        if percentage_value is None:
            return None
        return round(weight_kg * (percentage_value / 100), 1)
    return parse_measurement_float(normalized)


def calculate_physique_rating(
    percent_fat: float | None,
    skeletal_muscle_rate: float | None,
) -> int | None:
    """Berechnet den Koerperbau heuristisch aus Koerperfett und Skelettmuskulatur."""
    if percent_fat is None or skeletal_muscle_rate is None:
        return None

    if percent_fat < 15.0:
        body_fat_bucket = 0
    elif percent_fat <= 25.0:
        body_fat_bucket = 1
    else:
        body_fat_bucket = 2

    if skeletal_muscle_rate < 40.0:
        muscle_bucket = 0
    elif skeletal_muscle_rate < 46.0:
        muscle_bucket = 1
    else:
        muscle_bucket = 2

    return (body_fat_bucket * 3) + muscle_bucket + 1


def parse_csv_timestamp(raw: str) -> dt.datetime:
    """Parst Zeitstempel aus ISO-8601 oder aus dem CSV-Format der Waagen-App."""
    normalized = raw.strip()
    try:
        return parse_timestamp(normalized)
    except argparse.ArgumentTypeError:
        pass

    for pattern in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            parsed_value = dt.datetime.strptime(normalized, pattern)
            return parsed_value.replace(tzinfo=LOCAL_TIMEZONE)
        except ValueError:
            continue

    raise argparse.ArgumentTypeError(f"Ungueltiger Zeitstempel: {raw}")


def use_google_drive_input(args: argparse.Namespace) -> bool:
    """Erkennt, ob das Script im Google-Drive-Modus statt mit lokaler CSV laufen soll."""
    return any(
        [
            args.google_drive_csv_file_id is not None,
            args.google_drive_folder_id is not None,
            args.google_oauth_client_secret_file is not None,
            args.google_drive_csv_file_name != "Gewicht.csv",
            args.google_drive_folder_name != "FitDays-Export",
        ]
    )


def require_google_drive_dependencies() -> None:
    """Prueft, ob die optionalen Google-Drive-Abhaengigkeiten installiert sind."""
    if (
        Request is None
        or Credentials is None
        or InstalledAppFlow is None
        or build is None
        or MediaIoBaseDownload is None
        or MediaInMemoryUpload is None
    ):
        raise RuntimeError(
            "Google-Drive-Unterstuetzung benoetigt 'google-api-python-client', 'google-auth' und 'google-auth-oauthlib'. "
            "Installation: pip install google-api-python-client google-auth google-auth-oauthlib"
        )


def create_google_drive_service(
    oauth_client_secret_file: str,
    token_file: str,
):
    """Erstellt einen authentifizierten Google-Drive-Service ueber OAuth."""
    require_google_drive_dependencies()

    credentials = None
    token_path = pathlib.Path(token_file)
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(
            str(token_path),
            GOOGLE_DRIVE_SCOPES,
        )

    if credentials is not None and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    elif credentials is None or not credentials.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            oauth_client_secret_file,
            GOOGLE_DRIVE_SCOPES,
        )
        credentials = flow.run_local_server(port=0)

    if credentials is None:
        raise RuntimeError("Google-OAuth-Zugangsdaten konnten nicht erstellt werden")

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=credentials)


def download_drive_file_content(drive_service, file_id: str) -> str:
    """Laedt den kompletten Inhalt einer Google-Drive-Datei als Text herunter."""
    request = drive_service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue().decode("utf-8")


def load_measurements_from_csv_content(content: str) -> list[Measurement]:
    """Liest CSV-Inhalt ein, normalisiert ihn und baut daraus Measurement-Objekte."""
    measurements: list[Measurement] = []
    reader = csv.DictReader(io.StringIO(content))
    ensure_required_csv_columns(reader.fieldnames)
    fieldnames = reader.fieldnames or []
    for row in reader:
        if csv_row_is_empty(row):
            continue
        normalized_row = normalize_csv_row(row, fieldnames)
        weight_kg = float(parse_measurement_float(normalized_row["weight"]))
        percent_fat = parse_measurement_float(normalized_row.get("percent_fat", ""))
        percent_hydration = parse_measurement_float(normalized_row.get("percent_hydration", ""))
        skeletal_muscle_rate = parse_measurement_float(normalized_row.get("skeletal_muscle_rate", ""))
        muscle_mass = parse_muscle_mass(normalized_row.get("skeletal_muscle_rate", ""), weight_kg)
        daily_calorie_intake = parse_measurement_float(normalized_row.get("daily_calorie_intake", ""))
        visceral_fat_rating = parse_measurement_int(normalized_row.get("visceral_fat_rating", ""))
        bone_mass = parse_measurement_float(normalized_row.get("bone_mass", ""))
        metabolic_age = parse_measurement_int(normalized_row.get("metabolic_age", ""))
        physique_rating = parse_measurement_int(normalized_row.get("physique_rating", ""))
        if physique_rating is None:
            physique_rating = calculate_physique_rating(percent_fat, skeletal_muscle_rate)
        LOGGER.info(
            "CSV-Messwert geladen: Zeit=%s, Gewicht=%s kg, Koerperfett=%s %%, Skelettmuskulatur=%s %%, Muskelmasse=%s kg, Koerperbau=%s",
            normalized_row["timestamp"],
            weight_kg,
            percent_fat,
            skeletal_muscle_rate,
            muscle_mass,
            physique_rating,
        )

        measurements.append(
            Measurement(
                timestamp=parse_csv_timestamp(normalized_row["timestamp"]),
                weight=weight_kg,
                percent_fat=percent_fat,
                percent_hydration=percent_hydration,
                skeletal_muscle_rate=skeletal_muscle_rate,
                muscle_mass=muscle_mass,
                daily_calorie_intake=daily_calorie_intake,
                physique_rating=physique_rating,
                visceral_fat_rating=visceral_fat_rating,
                bone_mass=bone_mass,
                metabolic_age=metabolic_age,
                user_profile_index=parse_measurement_int(normalized_row.get("user_profile_index", "")),
                bmi=parse_measurement_float(normalized_row.get("bmi", "")),
            )
        )
    return measurements


def find_drive_file_in_folder(drive_service, folder_id: str, file_name: str) -> dict | None:
    """Sucht eine Datei mit einem bestimmten Namen innerhalb eines Google-Drive-Ordners."""
    escaped_file_name = file_name.replace("'", "\\'")
    query = f"'{folder_id}' in parents and name = '{escaped_file_name}' and trashed = false"
    response = (
        drive_service.files()
        .list(
            q=query,
            spaces="drive",
            fields="files(id, name)",
            pageSize=1,
        )
        .execute()
    )
    files = response.get("files", [])
    return files[0] if files else None


def find_drive_folder_by_name(drive_service, folder_name: str) -> dict | None:
    """Sucht einen Google-Drive-Ordner ueber seinen Namen."""
    escaped_folder_name = folder_name.replace("'", "\\'")
    query = "mimeType = 'application/vnd.google-apps.folder' " f"and name = '{escaped_folder_name}' and trashed = false"
    response = (
        drive_service.files()
        .list(
            q=query,
            spaces="drive",
            fields="files(id, name, createdTime)",
            orderBy="createdTime desc",
            pageSize=1,
        )
        .execute()
    )
    files = response.get("files", [])
    return files[0] if files else None


def find_latest_csv_in_folder(drive_service, folder_id: str) -> dict | None:
    """Sucht die neueste CSV-Datei im Google-Drive-Ordner nach createdTime sortiert.

    Diese Funktion durchsucht den angegebenen Google-Drive-Ordner nach allen CSV-Dateien
    und gibt diejenige zurueck, die am spaetesten erstellt wurde. Leere Ergebnisse
    oder Fehler werden als None zurueckgegeben.
    """
    query = f"'{folder_id}' in parents and name contains '.csv' and trashed = false"
    response = (
        drive_service.files()
        .list(
            q=query,
            spaces="drive",
            fields="files(id, name, createdTime)",
            orderBy="createdTime desc",
            pageSize=10,
        )
        .execute()
    )
    files = response.get("files", [])
    if files:
        LOGGER.info("Neueste CSV-Datei in Drive gefunden: %s (%s)", files[0]["name"], files[0]["createdTime"])
    else:
        LOGGER.info("Keine CSV-Dateien im Google-Drive-Ordner %s gefunden", folder_id)
    return files[0] if files else None


def resolve_google_drive_targets(
    drive_service,
    csv_file_id: str | None,
    folder_id: str | None,
    folder_name: str | None,
    csv_file_name: str | None,
    use_latest_csv: bool = False,
) -> tuple[str, str]:
    """Loest CSV-Datei und Zielordner auf, egal ob ueber Namen, ID oder neueste Datei."""
    resolved_folder_id = folder_id
    if resolved_folder_id is None:
        if not folder_name:
            raise ValueError("Entweder --google-drive-folder-id oder --google-drive-folder-name muss gesetzt sein.")
        folder = find_drive_folder_by_name(drive_service, folder_name)
        if folder is None:
            raise ValueError(f"Google-Drive-Ordner nicht gefunden: {folder_name}")
        resolved_folder_id = folder["id"]

    resolved_csv_file_id = csv_file_id
    if resolved_csv_file_id is None and use_latest_csv:
        # Neueste CSV-Datei automatisch suchen
        latest_csv = find_latest_csv_in_folder(drive_service, resolved_folder_id)
        if latest_csv is None:
            raise ValueError("Keine CSV-Dateien im Google-Drive-Ordner gefunden")
        resolved_csv_file_id = latest_csv["id"]
        LOGGER.info("Automatisch neueste CSV gewaehlt: %s", latest_csv["name"])
    elif resolved_csv_file_id is None:
        if not csv_file_name:
            raise ValueError("Entweder --google-drive-csv-file-id oder --google-drive-csv-file-name muss gesetzt sein.")
        csv_file = find_drive_file_in_folder(
            drive_service,
            resolved_folder_id,
            csv_file_name,
        )
        if csv_file is None:
            raise ValueError(f"Google-Drive-CSV-Datei im Ordner '{resolved_folder_id}' nicht gefunden: {csv_file_name}")
        resolved_csv_file_id = csv_file["id"]

    LOGGER.info("Google-Drive-Ordner aufgeloest: %s", resolved_folder_id)
    LOGGER.info("Google-Drive-CSV-Datei aufgeloest: %s", resolved_csv_file_id)
    return resolved_csv_file_id, resolved_folder_id


def resolve_google_drive_folder_id(
    drive_service,
    folder_id: str | None,
    folder_name: str | None,
) -> str:
    """Loest die Google-Drive-Ordner-ID auf, egal ob ueber direkte ID oder Namen."""
    resolved_folder_id = folder_id
    if resolved_folder_id is None:
        if not folder_name:
            raise ValueError("Entweder --google-drive-folder-id oder --google-drive-folder-name muss gesetzt sein.")
        folder = find_drive_folder_by_name(drive_service, folder_name)
        if folder is None:
            raise ValueError(f"Google-Drive-Ordner nicht gefunden: {folder_name}")
        resolved_folder_id = folder["id"]

    LOGGER.info("Google-Drive-Ordner aufgeloest: %s", resolved_folder_id)
    return resolved_folder_id


def read_last_check_from_drive(drive_service, folder_id: str) -> dt.datetime | None:
    """Liest den zuletzt exportierten Zeitstempel aus `.last_check` in Google Drive."""
    existing_file = find_drive_file_in_folder(drive_service, folder_id, LAST_CHECK_FILE_NAME)
    if existing_file is None:
        LOGGER.info("Keine Datei %s im Google-Drive-Ordner %s gefunden", LAST_CHECK_FILE_NAME, folder_id)
        return None
    raw_value = download_drive_file_content(drive_service, existing_file["id"]).strip()
    if raw_value == "":
        LOGGER.info("Datei %s im Google-Drive-Ordner %s ist leer", LAST_CHECK_FILE_NAME, folder_id)
        return None
    parsed_timestamp = parse_timestamp(raw_value)
    LOGGER.info("Letzter Exportzeitpunkt geladen: %s", parsed_timestamp.isoformat())
    return parsed_timestamp


def write_last_check_to_drive(drive_service, folder_id: str, timestamp: dt.datetime) -> None:
    """Schreibt den neuesten exportierten Zeitstempel in `.last_check` nach Google Drive."""
    content = timestamp.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z") + "\n"
    media = MediaInMemoryUpload(
        content.encode("utf-8"),
        mimetype="text/plain",
        resumable=False,
    )
    existing_file = find_drive_file_in_folder(drive_service, folder_id, LAST_CHECK_FILE_NAME)
    LOGGER.info("Aktualisiere %s im Google-Drive-Ordner %s", LAST_CHECK_FILE_NAME, folder_id)
    if existing_file is None:
        drive_service.files().create(
            body={
                "name": LAST_CHECK_FILE_NAME,
                "parents": [folder_id],
                "mimeType": "text/plain",
            },
            media_body=media,
            fields="id",
        ).execute()
        return

    drive_service.files().update(
        fileId=existing_file["id"],
        media_body=media,
    ).execute()


def get_last_check_date(
    drive_service: object | None,
    folder_id: str | None,
    local_path: pathlib.Path,
) -> dt.datetime:
    """Ermittelt den letzten Exportzeitpunkt aus Google Drive, lokal oder einer neu erstellten Datei.

    Diese Funktion sucht zuerst in Google Drive nach der `.last_check`-Datei im angegebenen Ordner.
    Falls diese nicht existiert oder kein Drive-Service vorhanden ist, wird lokal im Dateisystem
    nach einer `.last_check`-Datei gesucht. Wenn dort ebenfalls keine gefunden wird, wird eine
    neue lokale Datei mit dem aktuellen Zeitstempel erstellt. Der ermittelte Zeitstempel wird
    als datetime-Objekt zurückgegeben.
    """
    # 1. Versuch: Google Drive lesen
    if drive_service is not None and folder_id is not None:
        drive_timestamp = read_last_check_from_drive(drive_service, folder_id)
        if drive_timestamp is not None:
            return drive_timestamp

    # 2. Versuch: Lokal lesen
    if local_path.exists():
        raw_value = local_path.read_text(encoding="utf-8").strip()
        if raw_value:
            timestamp = parse_timestamp(raw_value)
            LOGGER.info("Letzter Exportzeitpunkt (lokal) geladen: %s", timestamp.isoformat())
            return timestamp

    # 3. Fallback: Lokale Datei neu erstellen
    now = dt.datetime.now(dt.timezone.utc)
    content = now.isoformat().replace("+00:00", "Z") + "\n"
    local_path.write_text(content, encoding="utf-8")
    LOGGER.info("Neue lokale %s erstellt mit aktuellem Zeitstempel: %s", local_path.name, now.isoformat())
    return now


def filter_new_measurements(
    measurements: list[Measurement],
    last_check_timestamp: dt.datetime | None,
) -> list[Measurement]:
    """Filtert nur Messwerte heraus, die neuer sind als der letzte bekannte Export."""
    if last_check_timestamp is None:
        filtered_measurements = sorted(measurements, key=lambda measurement: measurement.timestamp)
        LOGGER.info("Kein letzter Exportzeitpunkt gefunden. Exportiere alle %s Messwerte.", len(filtered_measurements))
        return filtered_measurements

    last_check_utc = last_check_timestamp.astimezone(dt.timezone.utc)
    filtered_measurements = sorted(
        [
            measurement
            for measurement in measurements
            if measurement.timestamp.astimezone(dt.timezone.utc) > last_check_utc
        ],
        key=lambda measurement: measurement.timestamp,
    )
    LOGGER.info(
        "Gefilterte Messwerte: %s neu von insgesamt %s.",
        len(filtered_measurements),
        len(measurements),
    )
    return filtered_measurements


# Diese Hilfsfunktion laesst nur vollstaendige Messwerte durch und protokolliert uebersprungene Eintraege.
def filter_complete_measurements(measurements: list[Measurement]) -> list[Measurement]:
    """Filtert nur vollstaendige Messwerte fuer den Export und protokolliert uebersprungene Eintraege."""
    complete_measurements: list[Measurement] = []
    skipped_measurements = 0

    for measurement in measurements:
        is_complete, missing_fields = measurement_is_complete(measurement)
        if is_complete:
            complete_measurements.append(measurement)
            continue

        skipped_measurements += 1
        LOGGER.info(
            "Messwert vom %s wird uebersprungen, weil Felder fehlen: %s. Gewicht=%s kg, Koerperfett=%s %%, Skelettmuskulatur=%s %%, Muskelmasse=%s kg, Koerperbau=%s",
            measurement.timestamp.isoformat(),
            ", ".join(missing_fields),
            measurement.weight,
            measurement.percent_fat,
            measurement.skeletal_muscle_rate,
            measurement.muscle_mass,
            measurement.physique_rating,
        )

    LOGGER.info(
        "Vollstaendige Messwerte: %s von %s. Uebersprungen: %s.",
        len(complete_measurements),
        len(measurements),
        skipped_measurements,
    )
    return complete_measurements


def fit_crc(data: bytes) -> int:
    """Berechnet die CRC-Pruefsumme fuer FIT-Header und FIT-Dateiinhalt."""
    crc_table = (
        0x0000,
        0xCC01,
        0xD801,
        0x1400,
        0xF001,
        0x3C00,
        0x2800,
        0xE401,
        0xA001,
        0x6C00,
        0x7800,
        0xB401,
        0x5000,
        0x9C01,
        0x8801,
        0x4400,
    )
    crc = 0
    for byte in data:
        tmp = crc_table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc ^= tmp ^ crc_table[byte & 0xF]

        tmp = crc_table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc ^= tmp ^ crc_table[(byte >> 4) & 0xF]
    return crc & 0xFFFF


def build_header(data_size: int) -> bytes:
    """Erzeugt den FIT-Dateiheader inklusive Header-CRC."""
    header_without_crc = struct.pack(
        "<BBHI4s",
        14,
        PROTOCOL_VERSION,
        PROFILE_VERSION,
        data_size,
        b".FIT",
    )
    header_crc = fit_crc(header_without_crc)
    return header_without_crc + struct.pack("<H", header_crc)


def definition_record(
    local_message_number: int, global_message_number: int, fields: list[tuple[int, int, int]]
) -> bytes:
    """Erzeugt einen FIT-Definitionsdatensatz fuer die nachfolgenden Datensaetze."""
    payload = bytearray()
    payload.append(0x40 | (local_message_number & 0x0F))
    payload.append(0)  # Reserviertes Byte laut FIT-Format.
    payload.append(0)  # Daten werden im Little-Endian-Format geschrieben.
    payload.extend(struct.pack("<H", global_message_number))
    payload.append(len(fields))
    for field_number, size, base_type in fields:
        payload.extend((field_number, size, base_type))
    return bytes(payload)


def data_record(local_message_number: int, values: Iterable[bytes]) -> bytes:
    """Erzeugt einen FIT-Datensatz mit den bereits vorbereiteten Byte-Werten."""
    payload = bytearray()
    payload.append(local_message_number & 0x0F)
    for value in values:
        payload.extend(value)
    return bytes(payload)


def encode_u8(value: int | None) -> bytes:
    """Kodiert einen optionalen 8-Bit-Wert oder den FIT-Invalid-Wert."""
    return struct.pack("<B", INVALID_UINT8 if value is None else value)


def encode_u16_scaled(value: float | None, scale: int = 100) -> bytes:
    """Kodiert einen skalierten 16-Bit-Wert fuer FIT-Felder mit Faktor."""
    if value is None:
        return struct.pack("<H", INVALID_UINT16)
    return struct.pack("<H", int(round(value * scale)))


def encode_u16(value: int | None) -> bytes:
    """Kodiert einen optionalen 16-Bit-Ganzzahlwert fuer FIT."""
    return struct.pack("<H", INVALID_UINT16 if value is None else value)


def encode_u32(value: int | None) -> bytes:
    """Kodiert einen optionalen 32-Bit-Ganzzahlwert fuer FIT."""
    return struct.pack("<I", INVALID_UINT32 if value is None else value)


def to_fit_timestamp(value: dt.datetime) -> int:
    """Wandelt einen Python-Zeitstempel in die Garmin-FIT-Epoch um."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    value_utc = value.astimezone(dt.timezone.utc)
    delta = value_utc - FIT_EPOCH
    return int(delta.total_seconds())


def parse_timestamp(raw: str) -> dt.datetime:
    """Parst einen allgemeinen ISO-Zeitstempel aus CLI oder Hilfsdateien."""
    normalized = raw.strip().replace("Z", "+00:00")
    try:
        value = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Ungueltiger Zeitstempel: {raw}") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value


def optional_float(row: dict[str, str], key: str) -> float | None:
    """Liest einen optionalen Float-Wert aus einem Dictionary."""
    value = row.get(key, "").strip()
    return None if value == "" else float(value)


def optional_int(row: dict[str, str], key: str) -> int | None:
    """Liest einen optionalen Integer-Wert aus einem Dictionary."""
    value = row.get(key, "").strip()
    return None if value == "" else int(value)


def load_measurements_from_csv(path: pathlib.Path) -> list[Measurement]:
    """Laedt eine lokale CSV-Datei vom Dateisystem und parst ihren Inhalt."""
    with path.open("r", newline="", encoding="utf-8") as handle:
        return load_measurements_from_csv_content(handle.read())


# Ab hier wird das eigentliche FIT-Dateiformat aufgebaut.
# Ab hier wird das eigentliche FIT-Dateiformat aufgebaut.
# Zuerst werden Definitionen fuer die enthaltenen Nachrichtentypen geschrieben,
# danach folgen die eigentlichen Messdaten als Datensaetze.
def build_fit_file(measurements: list[Measurement], include_bmi: bool) -> bytes:
    """Baut aus allen Measurements die finale FIT-Datei inklusive Header und CRC."""
    if not measurements:
        raise ValueError("Mindestens ein Messwert ist erforderlich")

    file_id_definition = definition_record(
        local_message_number=0,
        global_message_number=MESG_NUM_FILE_ID,
        fields=[
            (0, 1, BASE_TYPE_ENUM),
            (1, 2, BASE_TYPE_UINT16),
            (2, 2, BASE_TYPE_UINT16),
            (3, 4, BASE_TYPE_UINT32Z),
            (4, 4, BASE_TYPE_UINT32),
        ],
    )

    weight_fields: list[tuple[int, int, int]] = [
        (253, 4, BASE_TYPE_UINT32),
        (0, 2, BASE_TYPE_UINT16),
        (1, 2, BASE_TYPE_UINT16),
        (2, 2, BASE_TYPE_UINT16),
        (5, 2, BASE_TYPE_UINT16),
        (9, 2, BASE_TYPE_UINT16),
        (8, 1, BASE_TYPE_UINT8),
        (11, 1, BASE_TYPE_UINT8),
        (4, 2, BASE_TYPE_UINT16),
        (10, 1, BASE_TYPE_UINT8),
        (12, 2, BASE_TYPE_UINT16),
    ]
    if include_bmi:
        weight_fields.append((13, 2, BASE_TYPE_UINT16))

    weight_definition = definition_record(
        local_message_number=1,
        global_message_number=MESG_NUM_WEIGHT_SCALE,
        fields=weight_fields,
    )

    created_at = max(m.timestamp for m in measurements)
    file_id_data = data_record(
        local_message_number=0,
        values=[
            struct.pack("<B", FILE_TYPE_WEIGHT),
            struct.pack("<H", MANUFACTURER_DEVELOPMENT),
            struct.pack("<H", PRODUCT_ID),
            struct.pack("<I", SERIAL_NUMBER),
            struct.pack("<I", to_fit_timestamp(created_at)),
        ],
    )

    weight_data_records: list[bytes] = []
    for measurement in measurements:
        values: list[bytes] = [
            encode_u32(to_fit_timestamp(measurement.timestamp)),
            encode_u16_scaled(measurement.weight, 100),
            encode_u16_scaled(measurement.percent_fat, 100),
            encode_u16_scaled(measurement.percent_hydration, 100),
            encode_u16_scaled(measurement.muscle_mass, 100),
            encode_u16_scaled(measurement.daily_calorie_intake, 4),
            encode_u8(measurement.physique_rating),
            encode_u8(measurement.visceral_fat_rating),
            encode_u16_scaled(measurement.bone_mass, 100),
            encode_u8(measurement.metabolic_age),
            encode_u16(measurement.user_profile_index),
        ]
        if include_bmi:
            values.append(encode_u16_scaled(measurement.bmi, 10))
        weight_data_records.append(data_record(1, values))

    data = b"".join([file_id_definition, weight_definition, file_id_data, *weight_data_records])
    file_crc = fit_crc(data)
    return build_header(len(data)) + data + struct.pack("<H", file_crc)


def build_output_path(output: str | None, csv_path: pathlib.Path | None) -> pathlib.Path:
    """Bestimmt den Zielpfad der FIT-Datei anhand von CLI-Optionen oder Defaults."""
    if output:
        return pathlib.Path(output)
    if csv_path is not None:
        return csv_path.with_suffix(".fit")
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    return pathlib.Path(f"ws_{timestamp}.fit")


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Definiert und parst alle Kommandozeilenparameter des Scripts."""
    parser = argparse.ArgumentParser(description="Erzeugt eine Garmin-FIT-Datei fuer Gewichtsdaten.")
    parser.add_argument("--csv", dest="csv_path", help="Lokale CSV-Eingabedatei")
    parser.add_argument(
        "--google-drive-csv-file-id",
        help="Google-Drive-Datei-ID der CSV-Eingabedatei",
    )
    parser.add_argument(
        "--google-drive-csv-file-name",
        "-f",
        default="Gewicht.csv",
        help="Dateiname der CSV-Eingabedatei innerhalb des Zielordners in Google Drive",
    )
    parser.add_argument(
        "--google-drive-folder-id",
        help="Google-Drive-Ordner-ID, in dem `.last_check` gespeichert wird",
    )
    parser.add_argument(
        "--google-drive-folder-name",
        "-d",
        default="FitDays-Export",
        help="Google-Drive-Ordnername, in dem CSV-Datei und `.last_check` liegen",
    )
    parser.add_argument(
        "--google-oauth-client-secret-file",
        help="Pfad zur Google-OAuth-Client-Secret-JSON-Datei",
    )
    parser.add_argument(
        "--google-oauth-token-file",
        default=DEFAULT_GOOGLE_TOKEN_FILE,
        help="Pfad zur lokalen Google-OAuth-Token-Cache-Datei",
    )
    parser.add_argument("--output", help="Zielpfad der erzeugten FIT-Datei")
    parser.add_argument("--timestamp", type=parse_timestamp, help="Zeitstempel eines einzelnen Messwerts")
    parser.add_argument("--weight", type=float, help="Gewicht eines einzelnen Messwerts in kg")
    parser.add_argument("--percent-fat", type=float, help="Koerperfettanteil eines einzelnen Messwerts in Prozent")
    parser.add_argument("--percent-hydration", type=float, help="Koerperwasser eines einzelnen Messwerts in Prozent")
    parser.add_argument("--muscle-mass", type=float, help="Muskelmasse eines einzelnen Messwerts in kg")
    parser.add_argument("--daily-calorie-intake", type=float, help="Grundumsatz eines einzelnen Messwerts in kcal")
    parser.add_argument(
        "--physique-rating", type=int, help="Koerperbau-Bewertung eines einzelnen Messwerts von 1 bis 9"
    )
    parser.add_argument("--visceral-fat-rating", type=int, help="Eingeweidefett-Bewertung eines einzelnen Messwerts")
    parser.add_argument("--bone-mass", type=float, help="Knochengewicht eines einzelnen Messwerts in kg")
    parser.add_argument("--metabolic-age", type=int, help="Koerperalter eines einzelnen Messwerts")
    parser.add_argument("--user-profile-index", type=int, help="Optionaler Benutzerprofil-Index fuer FIT")
    parser.add_argument("--bmi", type=float, help="Optionales experimentelles BMI-Feld")
    parser.add_argument(
        "--check",
        "-c",
        action="store_true",
        help="Zeigt den letzten Exportzeitpunkt aus .last_check an (Google Drive, lokal oder neu erstellt)",
    )
    parser.add_argument(
        "--last-csv",
        "-l",
        action="store_true",
        help="Verwendet die neueste CSV-Datei im Drive-Ordner (nach createdTime)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Eingaben pruefen und anzeigen, was exportiert wuerde, ohne FIT-Datei zu schreiben oder `.last_check` zu aktualisieren",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Zusaetzliche Logging-Ausgaben aktivieren",
    )
    return parser.parse_args(argv)


# `main` ist der Einstiegspunkt fuer den kompletten Ablauf.
# Hier wird entschieden, ob aus lokaler CSV, Google Drive oder aus direkter CLI-Eingabe gelesen wird.
# Danach werden neue Werte gefiltert, optional ein Dry-Run ausgefuehrt und am Ende die FIT-Datei geschrieben.
def main(argv: list[str]) -> int:
    """Steuert den gesamten Ablauf: Eingabe laden, filtern, FIT bauen und speichern."""
    args = parse_args(argv)
    configure_logging(args.verbose)

    # --check Modus: Nur letzten Exportzeitpunkt anzeigen
    if args.check:
        local_last_check_path = pathlib.Path(".last_check")
        drive_service: object | None = None
        folder_id: str | None = None

        if args.google_oauth_client_secret_file:
            try:
                drive_service = create_google_drive_service(
                    args.google_oauth_client_secret_file,
                    args.google_oauth_token_file,
                )
                if args.google_drive_folder_id:
                    folder_id = args.google_drive_folder_id
                else:
                    # Ordner-ID aus dem Namen auflösen (benoetigt Drive-Verbindung)
                    try:
                        folder_id = resolve_google_drive_folder_id(
                            drive_service,
                            args.google_drive_folder_id,
                            args.google_drive_folder_name,
                        )
                    except Exception:
                        folder_id = None
            except Exception as exc:
                print(f"Google-Drive-Verbindung fehlgeschlagen: {exc}", file=sys.stderr)
                return 1

        last_check_date = get_last_check_date(
            drive_service=drive_service,
            folder_id=folder_id,
            local_path=local_last_check_path,
        )
        berlin_time = last_check_date.astimezone(LOCAL_TIMEZONE)
        formatted_date = berlin_time.strftime("%d.%m.%Y - %H:%M Uhr")
        print(f"Letzter Exportzeitpunkt: \033[92m{formatted_date}\033[0m")
        return 0

    measurements: list[Measurement]
    csv_path: pathlib.Path | None = None
    drive_service = None
    last_check_folder_id: str | None = None

    if use_google_drive_input(args):
        if not args.google_oauth_client_secret_file:
            print(
                "--google-oauth-client-secret-file ist bei Google-Drive-Eingaben erforderlich.",
                file=sys.stderr,
            )
            return 2

        try:
            drive_service = create_google_drive_service(
                args.google_oauth_client_secret_file,
                args.google_oauth_token_file,
            )
            resolved_csv_file_id, resolved_folder_id = resolve_google_drive_targets(
                drive_service,
                args.google_drive_csv_file_id,
                args.google_drive_folder_id,
                args.google_drive_folder_name,
                args.google_drive_csv_file_name,
                use_latest_csv=args.last_csv,
            )
            csv_content = download_drive_file_content(drive_service, resolved_csv_file_id)
            measurements = load_measurements_from_csv_content(csv_content)
            last_check_folder_id = resolved_folder_id
            last_check_timestamp = read_last_check_from_drive(drive_service, last_check_folder_id)
            measurements = filter_new_measurements(measurements, last_check_timestamp)
            measurements = filter_complete_measurements(measurements)
        except Exception as exc:
            print(f"Google-Drive-Fehler: {exc}", file=sys.stderr)
            return 1
    elif args.csv_path:
        csv_path = pathlib.Path(args.csv_path)
        try:
            measurements = load_measurements_from_csv(csv_path)
            measurements = filter_complete_measurements(measurements)
        except Exception as exc:
            print(f"CSV-Fehler: {exc}", file=sys.stderr)
            return 1
    elif args.last_csv:
        # Lokale neueste CSV-Datei suchen
        csv_directory = pathlib.Path.cwd()
        csv_files = sorted(
            csv_directory.glob("*.csv"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not csv_files:
            print("Keine CSV-Dateien im aktuellen Verzeichnis gefunden.", file=sys.stderr)
            return 1
        csv_path = csv_files[0]
        LOGGER.info("Neueste lokale CSV gewaehlt: %s", csv_path.name)
        try:
            measurements = load_measurements_from_csv(csv_path)
            measurements = filter_complete_measurements(measurements)
        except Exception as exc:
            print(f"CSV-Fehler: {exc}", file=sys.stderr)
            return 1
    else:
        if args.timestamp is None or args.weight is None:
            print(
                "Entweder --csv, eine Google-Drive-Eingabe oder beide Parameter --timestamp und --weight sind erforderlich.",
                file=sys.stderr,
            )
            return 2
        calculated_physique_rating = args.physique_rating
        if calculated_physique_rating is None:
            skeletal_muscle_rate = (
                None
                if args.muscle_mass is None or args.weight is None
                else round((args.muscle_mass / args.weight) * 100, 1)
            )
            calculated_physique_rating = calculate_physique_rating(args.percent_fat, skeletal_muscle_rate)
        measurements = [
            Measurement(
                timestamp=args.timestamp,
                weight=args.weight,
                skeletal_muscle_rate=(
                    None
                    if args.muscle_mass is None or args.weight is None
                    else round((args.muscle_mass / args.weight) * 100, 1)
                ),
                percent_fat=args.percent_fat,
                percent_hydration=args.percent_hydration,
                muscle_mass=args.muscle_mass,
                daily_calorie_intake=args.daily_calorie_intake,
                physique_rating=calculated_physique_rating,
                visceral_fat_rating=args.visceral_fat_rating,
                bone_mass=args.bone_mass,
                metabolic_age=args.metabolic_age,
                user_profile_index=args.user_profile_index,
                bmi=args.bmi,
            )
        ]
        measurements = filter_complete_measurements(measurements)

    if not measurements:
        print("Keine neuen Messwerte gefunden. Nichts zu exportieren.")
        return 0

    measurements = sorted(measurements, key=lambda measurement: measurement.timestamp)
    include_bmi = any(measurement.bmi is not None for measurement in measurements)
    newest_measurement_timestamp = max(measurement.timestamp for measurement in measurements)

    if args.dry_run:
        print(
            "Dry run: Es wuerden "
            f"{len(measurements)} Messwert(e) von "
            f"{measurements[0].timestamp.isoformat()} bis {newest_measurement_timestamp.isoformat()} exportiert"
        )
        return 0

    output_path = build_output_path(args.output, csv_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fit_bytes = build_fit_file(measurements, include_bmi=include_bmi)
    output_path.write_bytes(fit_bytes)
    LOGGER.info("FIT-Datei geschrieben nach %s", output_path)

    if drive_service is not None and last_check_folder_id is not None:
        try:
            write_last_check_to_drive(
                drive_service,
                last_check_folder_id,
                newest_measurement_timestamp,
            )
        except Exception as exc:
            print(f"{LAST_CHECK_FILE_NAME} in Google Drive konnte nicht aktualisiert werden: {exc}", file=sys.stderr)
            return 1

    print(f"Geschrieben: {output_path} ({len(measurements)} Messwert(e), {len(fit_bytes)} Bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
