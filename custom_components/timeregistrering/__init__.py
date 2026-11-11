"""Timer og materialer: gemmer noteringer fra mobil-appen i Home Assistant og viser appen."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

from aiohttp import web

from homeassistant.components import frontend
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import APP_URL, DOMAIN, MAX_BODY_BYTES, MAX_RECORD_BYTES, PANEL_URL_PATH, STORAGE_DIR

_LOGGER = logging.getLogger(__name__)
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{3,120}$")
_USER_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
WWW = Path(__file__).parent / "www"


class Store:
    """Ét lager pr. Home Assistant-bruger: /config/timeregistrering/<bruger-id>.json.

    Indholdet er en samling af poster {id: {...,"updated": ms}}. Ved synkronisering vinder
    den udgave af en post, der senest er ændret. Slettede poster gemmes som {"deleted": true}.
    """

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self._laas = threading.Lock()

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _fil(self, user_id: str) -> Path:
        if not _USER_RE.match(user_id):
            raise ValueError("ugyldig bruger")
        return self.root / f"{user_id}.json"

    def _laes(self, user_id: str) -> dict[str, dict]:
        f = self._fil(user_id)
        if not f.exists():
            return {}
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            _LOGGER.warning("Kunne ikke læse %s", f)
            return {}

    def _skriv(self, user_id: str, data: dict) -> None:
        f = self._fil(user_id)
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)

    def sync(self, user_id: str, indkomne: list) -> dict[str, dict]:
        """Flet poster ind og returnér alle brugerens poster."""
        with self._laas:
            data = self._laes(user_id)
            aendret = False
            for r in indkomne:
                if not isinstance(r, dict):
                    continue
                rid = r.get("id")
                upd = r.get("updated")
                if not isinstance(rid, str) or not _ID_RE.match(rid) or not isinstance(upd, (int, float)):
                    continue
                if len(json.dumps(r, ensure_ascii=False)) > MAX_RECORD_BYTES:
                    continue
                cur = data.get(rid)
                if cur is None or upd > cur.get("updated", 0):
                    data[rid] = r
                    aendret = True
            if aendret:
                self._skriv(user_id, data)
            return data


class SyncView(HomeAssistantView):
    """GET: alle poster. POST {"poster":[...]}: flet ind og få alle poster tilbage."""

    url = "/api/timeregistrering/sync"
    name = "api:timeregistrering:sync"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store

    async def get(self, request: web.Request) -> web.Response:
        user = request["hass_user"]
        data = await self.hass.async_add_executor_job(self.store.sync, user.id, [])
        return self.json({"poster": list(data.values())})

    async def post(self, request: web.Request) -> web.Response:
        user = request["hass_user"]
        body = await request.read()
        if len(body) > MAX_BODY_BYTES:
            return self.json_message("For stor", 413)
        try:
            obj = json.loads(body)
        except ValueError:
            return self.json_message("Ugyldig JSON", 400)
        poster = obj.get("poster") if isinstance(obj, dict) else None
        if not isinstance(poster, list):
            return self.json_message("Forkert indhold", 400)
        data = await self.hass.async_add_executor_job(self.store.sync, user.id, poster)
        return self.json({"poster": list(data.values())})


class MigView(HomeAssistantView):
    """Hvem er logget ind."""

    url = "/api/timeregistrering/mig"
    name = "api:timeregistrering:mig"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        u = request["hass_user"]
        return self.json({"id": u.id, "navn": u.name})


def _ics_tekst(v: str) -> str:
    """Escape tekst til iCalendar."""
    return (v or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _ics_fold(linje: str) -> str:
    """Fold lange linjer (75 bytes) som iCalendar kræver."""
    b = linje.encode("utf-8")
    if len(b) <= 75:
        return linje
    dele, cur = [], b""
    for ch in linje:
        e = ch.encode("utf-8")
        if len(cur) + len(e) > (75 if not dele else 74):
            dele.append(cur.decode("utf-8"))
            cur = b""
        cur += e
    dele.append(cur.decode("utf-8"))
    return "\r\n ".join(dele)


class IcsView(HomeAssistantView):
    """Laver en kalenderfil (.ics) for én aftale ud fra adressens parametre.

    Gemmer ingenting. Bruges af knappen "Læg i kalender", som iPhone åbner som en ny kalenderbegivenhed.
    """

    url = "/api/timeregistrering/ics"
    name = "api:timeregistrering:ics"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo

        # Alt kommer som base64url-kodet JSON i ?d=..., fordi Home Assistant afviser linjeskift i adresser.
        import base64

        q = dict(request.query)
        if "d" in q:
            try:
                raw = q["d"] + "=" * (-len(q["d"]) % 4)
                obj = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
                if isinstance(obj, dict):
                    q = {k: str(v) for k, v in obj.items()}
            except (ValueError, UnicodeDecodeError):
                return web.Response(status=400, text="Ugyldige data")
        titel = q.get("t", "Aftale")[:200]
        sted = q.get("l", "")[:300]
        note = q.get("n", "")[:2000]
        uid = re.sub(r"[^A-Za-z0-9_.:-]", "", q.get("u", ""))[:120] or "aftale"
        try:
            start = datetime.strptime(q.get("s", ""), "%Y%m%dT%H%M%S")
            minutter = max(15, min(int(q.get("d", "60")), 24 * 60))
        except ValueError:
            return web.Response(status=400, text="Ugyldig dato")
        tz = ZoneInfo(self.hass.config.time_zone or "Europe/Copenhagen")
        start_utc = start.replace(tzinfo=tz).astimezone(timezone.utc)
        slut_utc = start_utc + timedelta(minutes=minutter)
        fmt = "%Y%m%dT%H%M%SZ"
        linjer = [
            "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Timer og materialer//DA", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
            "BEGIN:VEVENT",
            f"UID:{uid}@timeregistrering",
            f"DTSTAMP:{datetime.now(timezone.utc).strftime(fmt)}",
            f"DTSTART:{start_utc.strftime(fmt)}",
            f"DTEND:{slut_utc.strftime(fmt)}",
            f"SUMMARY:{_ics_tekst(titel)}",
        ]
        if sted:
            linjer.append(f"LOCATION:{_ics_tekst(sted)}")
        if note:
            linjer.append(f"DESCRIPTION:{_ics_tekst(note)}")
        linjer += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Aftale", "TRIGGER:-PT30M", "END:VALARM", "END:VEVENT", "END:VCALENDAR"]
        body = "\r\n".join(_ics_fold(l) for l in linjer) + "\r\n"
        return web.Response(
            body=body.encode("utf-8"),
            content_type="text/calendar",
            charset="utf-8",
            headers={"Content-Disposition": 'inline; filename="aftale.ics"', "Cache-Control": "no-store"},
        )


class AppRedirectView(HomeAssistantView):
    """Kort adresse: /timer-start → appen."""

    url = "/timer-start"
    name = "timeregistrering:start"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        raise web.HTTPFound(f"{APP_URL}/index.html")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Sæt integrationen op."""
    store = Store(hass.config.path(STORAGE_DIR))
    await hass.async_add_executor_job(store.ensure)

    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("http_registered"):
        # HTTP-ruter kan ikke fjernes igen, så de registreres kun én gang pr. opstart.
        await hass.http.async_register_static_paths([StaticPathConfig(APP_URL, str(WWW), False)])
        for view in (SyncView(hass, store), MigView(), IcsView(hass), AppRedirectView()):
            hass.http.register_view(view)
        data["http_registered"] = True

    frontend.async_register_built_in_panel(
        hass,
        component_name="iframe",
        sidebar_title="Timer",
        sidebar_icon="mdi:clipboard-clock-outline",
        frontend_url_path=PANEL_URL_PATH,
        config={"url": f"{APP_URL}/index.html"},
        require_admin=False,
        update=True,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Fjern sidepanelet."""
    frontend.async_remove_panel(hass, PANEL_URL_PATH)
    return True
