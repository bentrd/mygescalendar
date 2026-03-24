#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time as time_module
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from html import unescape
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

try:
    from zoneinfo import ZoneInfo
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Python 3.9+ est requis") from exc


LOGIN_URL = "https://ges-cas.kordis.fr/login?service=https%3A%2F%2Fmyges.fr%2Fj_spring_cas_security_check"
PLANNING_URL = "https://myges.fr/student/planning-calendar"
AJAX_SOURCE = "calendar:myschedule"
FORM_NAME = "calendar"
DEFAULT_TZ = "Europe/Paris"


DETAIL_RENDER = "dlg1"
DETAIL_BEHAVIOR = "eventSelect"
NAV_RENDER = "calendar:myschedule calendar:currentDate calendar:currentWeek calendar:campuses calendar:lastUpdate"

@dataclass
class MyGesConfig:
    username: str
    password: str
    timezone_name: str
    fetch_details: bool = True


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def read_config(env_path: Path) -> MyGesConfig:
    load_env_file(env_path)
    username = os.getenv("MYGES_USERNAME", "").strip()
    password = os.getenv("MYGES_PASSWORD", "").strip()
    timezone_name = os.getenv("MYGES_TIMEZONE", DEFAULT_TZ).strip() or DEFAULT_TZ

    if not username or not password:
        raise SystemExit(
            "MYGES_USERNAME ou MYGES_PASSWORD manquant. Créez un fichier .env à partir de .env.example."
        )

    return MyGesConfig(username=username, password=password, timezone_name=timezone_name)


class MyGesClient:
    def __init__(self, config: MyGesConfig):
        self.config = config
        self.cookie_jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookie_jar))

    def _request(self, url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None, retries: int = 3) -> tuple[str, str]:
        request = Request(url, data=data, headers=headers or {})
        for attempt in range(retries):
            try:
                with self.opener.open(request) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    body = response.read().decode(charset, errors="replace")
                    return response.geturl(), body
            except Exception as exc:
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    print(f"Tentative {attempt + 1} échouée ({exc}), nouvelle tentative dans {wait}s...")
                    time_module.sleep(wait)
                else:
                    raise

    def login(self) -> None:
        _, login_page = self._request(LOGIN_URL)
        form_action = extract_form_action(login_page)
        fields = extract_login_fields(login_page)
        fields["username"] = self.config.username
        fields["password"] = self.config.password

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": LOGIN_URL,
            "Origin": "https://ges-cas.kordis.fr",
            "User-Agent": build_user_agent(),
        }
        final_url, body = self._request(form_action, data=urlencode(fields).encode("utf-8"), headers=headers)
        if "Identifiant ou mot de passe invalide" in body or "/login" in urlparse(final_url).path:
            raise RuntimeError("Échec de la connexion. Vérifiez vos identifiants MYGES.")

    def fetch_planning_page(self) -> str:
        final_url, body = self._request(PLANNING_URL, headers={"User-Agent": build_user_agent()})
        if "/login" in final_url:
            raise RuntimeError("La session n'est pas authentifiée.")
        return body

    def _ajax_headers(self) -> dict[str, str]:
        return {
            "faces-request": "partial/ajax",
            "x-requested-with": "XMLHttpRequest",
            "accept": "application/xml, text/xml, */*; q=0.01",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "origin": "https://myges.fr",
            "referer": PLANNING_URL,
            "user-agent": build_user_agent(),
        }

    def _navigate_week(self, direction: str, view_state: str) -> str:
        """
        Envoie un appel de navigation previousMonth / nextMonth.
        Retourne le ViewState (potentiellement mis à jour).
        direction doit être 'previousMonth' ou 'nextMonth'.
        """
        source = f"{FORM_NAME}:{direction}"
        payload = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": source,
            "javax.faces.partial.execute": "@all",
            "javax.faces.partial.render": NAV_RENDER,
            source: source,
            FORM_NAME: FORM_NAME,
            f"{AJAX_SOURCE}_view": "agendaWeek",
            "javax.faces.ViewState": view_state,
        }
        _, body = self._request(PLANNING_URL, data=urlencode(payload).encode("utf-8"), headers=self._ajax_headers())
        return extract_view_state_from_partial(body) or view_state

    def fetch_schedule(self, week_of: date) -> dict:
        planning_page = self.fetch_planning_page()
        view_state = extract_view_state(planning_page)
        campus_map = extract_campus_map(planning_page)

        # Le serveur maintient sa propre "semaine courante" à partir d'aujourd'hui.
        # Il faut la faire avancer jusqu'à la semaine cible avant de récupérer les données.
        today_monday = date.today() - timedelta(days=date.today().weekday())
        target_monday = week_of - timedelta(days=week_of.weekday())
        weeks_offset = (target_monday - today_monday).days // 7

        if weeks_offset < 0:
            for _ in range(abs(weeks_offset)):
                view_state = self._navigate_week("previousMonth", view_state)
        elif weeks_offset > 0:
            for _ in range(weeks_offset):
                view_state = self._navigate_week("nextMonth", view_state)

        week_start_ms, week_end_ms = week_bounds_ms(week_of, self.config.timezone_name)

        payload = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": AJAX_SOURCE,
            "javax.faces.partial.execute": AJAX_SOURCE,
            "javax.faces.partial.render": AJAX_SOURCE,
            AJAX_SOURCE: AJAX_SOURCE,
            f"{AJAX_SOURCE}_start": str(week_start_ms),
            f"{AJAX_SOURCE}_end": str(week_end_ms),
            FORM_NAME: FORM_NAME,
            f"{AJAX_SOURCE}_view": "agendaWeek",
            "javax.faces.ViewState": view_state,
        }
        _, body = self._request(PLANNING_URL, data=urlencode(payload).encode("utf-8"), headers=self._ajax_headers())
        events_payload = extract_events_payload(body)
        view_state = extract_view_state_from_partial(body) or view_state
        raw_events = events_payload.get("events", [])

        enriched: list[dict] = []
        for raw in raw_events:
            event = normalize_event(raw, campus_map, self.config.timezone_name)
            if self.config.fetch_details and raw.get("id"):
                detail = self.fetch_event_detail(raw["id"], view_state)
                event.update(detail)
                time_module.sleep(0.15)  # respecter le serveur
            enriched.append(event)

        return {
            "week_of": week_of.isoformat(),
            "timezone": self.config.timezone_name,
            "campuses": campus_map,
            "events": enriched,
        }

    def fetch_event_detail(self, event_id: str, view_state: str) -> dict:
        """Envoie l'appel AJAX eventSelect et retourne les champs de détail parsés."""
        payload = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": AJAX_SOURCE,
            "javax.faces.partial.execute": AJAX_SOURCE,
            "javax.faces.partial.render": DETAIL_RENDER,
            "javax.faces.behavior.event": DETAIL_BEHAVIOR,
            "javax.faces.partial.event": DETAIL_BEHAVIOR,
            f"{AJAX_SOURCE}_selectedEventId": event_id,
            FORM_NAME: FORM_NAME,
            f"{AJAX_SOURCE}_view": "agendaWeek",
            "javax.faces.ViewState": view_state,
        }
        _, body = self._request(PLANNING_URL, data=urlencode(payload).encode("utf-8"), headers=self._ajax_headers())
        return extract_event_detail(body)


def build_user_agent() -> str:
    return "Mozilla/5.0 (compatible; myges-calendar/1.0; +https://myges.fr)"


def extract_form_action(html: str) -> str:
    match = re.search(r'<form[^>]+action="([^"]+)"', html)
    if not match:
        raise RuntimeError("Impossible de trouver l'action du formulaire de connexion CAS.")
    return urljoin(LOGIN_URL, unescape(match.group(1)))


def extract_login_fields(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for name, value in re.findall(r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', html):
        fields[unescape(name)] = unescape(value)
    for required in ("lt", "execution", "_eventId"):
        if required not in fields:
            raise RuntimeError(f"Impossible de trouver le champ CAS : {required}")
    return fields


def extract_view_state(html: str) -> str:
    match = re.search(r'name="javax\.faces\.ViewState"[^>]+value="([^"]+)"', html)
    if not match:
        raise RuntimeError("Impossible de trouver javax.faces.ViewState sur la page de planning.")
    return unescape(match.group(1))


def extract_campus_map(html: str) -> dict[str, str]:
    campus_map: dict[str, str] = {}
    for match in re.finditer(r"<b>\s*([A-Z0-9]+)\s*:\s*([^<]+?)\s*</b>", html, flags=re.IGNORECASE):
        campus = match.group(1).strip().upper()
        address = re.sub(r"\s+", " ", match.group(2)).strip()
        if campus and address:
            campus_map[campus] = address
    return campus_map


def week_bounds_ms(week_of: date, timezone_name: str) -> tuple[int, int]:
    tz = ZoneInfo(timezone_name)
    monday = week_of - timedelta(days=week_of.weekday())
    start_local = datetime.combine(monday, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=7)
    return int(start_local.timestamp() * 1000), int(end_local.timestamp() * 1000)


def extract_events_payload(xml_body: str) -> dict:
    root = ET.fromstring(xml_body)
    update = root.find(".//update[@id='calendar:myschedule']")
    if update is None or not update.text:
        raise RuntimeError("Impossible de trouver les données du planning dans la réponse JSF.")
    return json.loads(update.text)


def extract_view_state_from_partial(xml_body: str) -> str | None:
    """Extrait le ViewState mis à jour d'une réponse JSF partielle, si présent."""
    match = re.search(r'<update id="javax\.faces\.ViewState"><!\[CDATA\[([^\]]+)\]\]>', xml_body)
    return unescape(match.group(1)) if match else None


def extract_event_detail(xml_body: str) -> dict:
    """Parse le fragment JSF dlg1 retourné par un appel AJAX eventSelect."""
    root = ET.fromstring(xml_body)
    update = root.find(f".//*[@id='{DETAIL_RENDER}']")
    if update is None or not update.text:
        return {}
    html = update.text
    detail: dict[str, str | None] = {}
    for field in ("matiere", "intervenant", "salle", "type", "modality", "commentaire"):
        m = re.search(rf'<span\s+id="{re.escape(field)}">([^<]*)</span>', html)
        detail[field] = unescape(m.group(1)).strip() if m else None
    # Normalise les chaînes vides en None
    return {k: v or None for k, v in detail.items()}


def normalize_event(event: dict, campus_map: dict[str, str], timezone_name: str) -> dict:
    title = event.get("title", "")
    title_lines = [line.strip() for line in title.splitlines() if line.strip()]
    summary = title_lines[0] if title_lines else "Événement sans titre"
    room = title_lines[1] if len(title_lines) > 1 else None
    campus_code = extract_campus_code(event.get("className", ""))
    location_parts = []
    if room:
        location_parts.append(room)
    if campus_code and campus_map.get(campus_code):
        location_parts.append(campus_map[campus_code])

    start = parse_myges_datetime(event["start"], timezone_name)
    end = parse_myges_datetime(event["end"], timezone_name)

    return {
        "id": event.get("id"),
        # Ces champs peuvent être écrasés plus tard par les détails (matiere remplace summary)
        "summary": summary,
        "room": room,
        "campus_code": campus_code,
        "location": " - ".join(location_parts) if location_parts else None,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "all_day": bool(event.get("allDay")),
        "class_name": event.get("className"),
        "raw_title": title,
        # Champs de détail – renseignés ultérieurement par fetch_event_detail()
        "matiere": None,
        "intervenant": None,
        "salle": None,
        "type": None,
        "modality": None,
        "commentaire": None,
    }


def extract_campus_code(class_name: str) -> str | None:
    match = re.search(r"reservation-([A-Z0-9]+)", class_name)
    if not match:
        return None
    code = match.group(1)
    return None if code == "null" else code


def parse_myges_datetime(value: str, timezone_name: str) -> datetime:
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    return parsed.astimezone(ZoneInfo(timezone_name))


def fold_ical_line(line: str) -> str:
    chunks = []
    current = line
    while len(current.encode("utf-8")) > 75:
        index = 0
        byte_length = 0
        for index, char in enumerate(current):
            byte_length += len(char.encode("utf-8"))
            if byte_length > 73:
                break
        chunks.append(current[:index])
        current = " " + current[index:]
    chunks.append(current)
    return "\r\n".join(chunks)


def escape_ical_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\n", r"\n")
    )


def build_ics(events: list[dict], calendar_name: str) -> str:
    now_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//OpenCode//MyGES Calendar//EN",
        "CALSCALE:GREGORIAN",
        fold_ical_line(f"X-WR-CALNAME:{escape_ical_text(calendar_name)}"),
    ]

    for event in events:
        uid = f"{event['id'] or uuid.uuid4()}@myges.fr"
        start = datetime.fromisoformat(event["start"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        end = datetime.fromisoformat(event["end"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # Utilise le nom complet de la matière récupéré via les détails si disponible
        summary = event.get("matiere") or event["summary"]

        description_parts = []
        if event.get("intervenant"):
            description_parts.append(f"Intervenant: {event['intervenant']}")
        if event.get("salle"):
            description_parts.append(f"Salle: {event['salle']}")
        elif event.get("room"):
            description_parts.append(f"Salle: {event['room']}")
        if event.get("type"):
            description_parts.append(f"Type: {event['type']}")
        if event.get("modality"):
            description_parts.append(f"Modalité: {event['modality']}")
        if event.get("commentaire"):
            description_parts.append(f"Note: {event['commentaire']}")
        if event.get("campus_code"):
            description_parts.append(f"Campus: {event['campus_code']}")
        description = "\n".join(description_parts)

        # Lieu : préférer la salle complète des détails, sinon utiliser salle + campus
        location = event.get("salle") or event.get("location")

        lines.extend(
            [
                "BEGIN:VEVENT",
                fold_ical_line(f"UID:{escape_ical_text(uid)}"),
                f"DTSTAMP:{now_utc}",
                f"DTSTART:{start}",
                f"DTEND:{end}",
                fold_ical_line(f"SUMMARY:{escape_ical_text(summary)}"),
            ]
        )
        if location:
            lines.append(fold_ical_line(f"LOCATION:{escape_ical_text(location)}"))
        if description:
            lines.append(fold_ical_line(f"DESCRIPTION:{escape_ical_text(description)}"))
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def ensure_parent_dir(path: Path) -> None:
    if path.parent != Path(""):
        path.parent.mkdir(parents=True, exist_ok=True)


def write_text_file(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Récupère les données de planning MyGES et les exporte en JSON ou ICS.")
    parser.add_argument("--env-file", default=".env", help="Chemin vers le fichier env contenant les identifiants MYGES.")
    parser.add_argument(
        "--week-of",
        default=date.today().isoformat(),
        help="N'importe quelle date dans la première semaine à récupérer, au format YYYY-MM-DD. Par défaut : aujourd'hui.",
    )
    parser.add_argument("--weeks", type=int, default=1, help="Nombre de semaines consécutives à récupérer.")
    parser.add_argument("--json-out", default=None, help="Où écrire l'export JSON.")
    parser.add_argument("--ics-out", default=None, help="Où écrire l'export ICS.")
    parser.add_argument("--calendar-name", default="MyGES Planning", help="Nom du calendrier pour l'export ICS.")
    parser.add_argument(
        "--no-details",
        action="store_true",
        default=False,
        help="Ne pas récupérer les détails par événement (plus rapide, mais les titres restent tronqués).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env_path = Path(args.env_file)
    config = read_config(env_path)
    config.fetch_details = not args.no_details
    client = MyGesClient(config)
    client.login()

    first_date = date.fromisoformat(args.week_of)
    if args.weeks < 1:
        raise SystemExit("--weeks doit être au moins 1")
    monday = first_date - timedelta(days=first_date.weekday())
    week_stamp = monday.strftime("semaine-du-%d-%m")  # e.g. semaine-du-23-02

    all_events: list[dict] = []
    schedules: list[dict] = []
    seen_ids: set[str] = set()

    for offset in range(args.weeks):
        current_date = first_date + timedelta(days=offset * 7)
        schedule = client.fetch_schedule(current_date)
        schedules.append(schedule)
        for event in schedule["events"]:
            event_id = event.get("id") or f"{event['start']}-{event['summary']}"
            if event_id in seen_ids:
                continue
            seen_ids.add(event_id)
            all_events.append(event)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timezone": config.timezone_name,
        "weeks": schedules,
        "events": sorted(all_events, key=lambda item: item["start"]),
    }

    json_path = Path(args.json_out or f"output/myges-{week_stamp}.json")
    ensure_parent_dir(json_path)
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    ics_path = Path(args.ics_out or f"output/myges-{week_stamp}.ics")
    ensure_parent_dir(ics_path)
    write_text_file(ics_path, build_ics(payload["events"], args.calendar_name))

    print(f"{len(payload['events'])} événement(s) écrits dans {json_path}")
    print(f"Calendrier ICS écrit dans {ics_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        raise SystemExit(1)
