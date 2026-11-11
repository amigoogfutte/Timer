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


    # --- kalenderabonnement: et hemmeligt link pr. bruger ---
    def _tokens(self) -> dict[str, str]:
        f = self.root / "kalender_tokens.json"
        try:
            d = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def token_for(self, user_id: str) -> str:
        import secrets

        with self._laas:
            d = self._tokens()
            for tok, uid in d.items():
                if uid == user_id:
                    return tok
            tok = secrets.token_urlsafe(24)
            d[tok] = user_id
            f = self.root / "kalender_tokens.json"
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d), encoding="utf-8")
            os.replace(tmp, f)
            return tok

    def user_for(self, token: str) -> str | None:
        return self._tokens().get(token)

    def laes(self, user_id: str) -> dict[str, dict]:
        with self._laas:
            return self._laes(user_id)


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


def _minutter(t: str) -> int | None:
    m = re.match(r"^(\d{1,2}):(\d{2})", t or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


class KalenderLinkView(HomeAssistantView):
    """Giver den indloggede bruger sit hemmelige abonnementslink."""

    url = "/api/timeregistrering/kalenderlink"
    name = "api:timeregistrering:kalenderlink"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store

    async def get(self, request: web.Request) -> web.Response:
        tok = await self.hass.async_add_executor_job(self.store.token_for, request["hass_user"].id)
        return self.json({"sti": f"/api/timeregistrering/kalender/{tok}.ics"})


class KalenderFeedView(HomeAssistantView):
    """Kalenderabonnement (webcal) med alle brugerens aftaler. Adgang via det hemmelige link."""

    url = "/api/timeregistrering/kalender/{token}.ics"
    name = "api:timeregistrering:kalender"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store

    async def get(self, request: web.Request, token: str) -> web.Response:
        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo

        uid = await self.hass.async_add_executor_job(self.store.user_for, token)
        if not uid:
            return web.Response(status=404, text="Ukendt kalender")
        data = await self.hass.async_add_executor_job(self.store.laes, uid)
        tz = ZoneInfo(self.hass.config.time_zone or "Europe/Copenhagen")
        fmt = "%Y%m%dT%H%M%SZ"
        nu = datetime.now(timezone.utc).strftime(fmt)
        linjer = [
            "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Timer og materialer//DA", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
            "X-WR-CALNAME:Aftaler (Timer)", "REFRESH-INTERVAL;VALUE=DURATION:PT15M", "X-PUBLISHED-TTL:PT15M",
        ]
        for r in data.values():
            if r.get("deleted") or r.get("kind") != "sag":
                continue
            a = r.get("aftale")
            if not isinstance(a, dict) or not a.get("dato"):
                continue
            try:
                dag = datetime.strptime(a["dato"], "%Y-%m-%d")
            except (ValueError, TypeError):
                continue
            t0 = _minutter(a.get("tid", "")) 
            t0 = 7 * 60 if t0 is None else t0
            t1 = _minutter(a.get("slut", ""))
            varighed = t1 - t0 if t1 is not None and t1 > t0 else 60
            start = (dag + timedelta(minutes=t0)).replace(tzinfo=tz).astimezone(timezone.utc)
            slut = start + timedelta(minutes=varighed)
            titel = ((r.get("bf") + " ") if r.get("bf") else "") + "Sag " + str(r.get("nr", "")) + " – " + str(r.get("adresse", ""))
            linjer += [
                "BEGIN:VEVENT",
                f"UID:{r.get('id')}@timeregistrering",
                f"DTSTAMP:{nu}",
                f"LAST-MODIFIED:{datetime.fromtimestamp((r.get('updated') or 0) / 1000, timezone.utc).strftime(fmt)}",
                f"DTSTART:{start.strftime(fmt)}",
                f"DTEND:{slut.strftime(fmt)}",
                f"SUMMARY:{_ics_tekst(titel)}",
            ]
            if r.get("adresse"):
                linjer.append(f"LOCATION:{_ics_tekst(str(r['adresse']))}")
            if r.get("besk"):
                linjer.append(f"DESCRIPTION:{_ics_tekst(str(r['besk']))}")
            if r.get("arkiveret"):
                linjer.append("STATUS:CONFIRMED")
            linjer += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Aftale", "TRIGGER:-PT30M", "END:VALARM", "END:VEVENT"]
        linjer.append("END:VCALENDAR")
        body = "\r\n".join(_ics_fold(l) for l in linjer) + "\r\n"
        return web.Response(body=body.encode("utf-8"), content_type="text/calendar", charset="utf-8",
                            headers={"Cache-Control": "no-store"})


# --- Siri / Genveje (bruges fra CarPlay) ---

_TAL_ORD = {
    "nul": 0, "en": 1, "et": 1, "én": 1, "ét": 1, "to": 2, "tre": 3, "fire": 4, "fem": 5, "seks": 6,
    "syv": 7, "otte": 8, "ni": 9, "ti": 10, "elleve": 11, "tolv": 12,
}


def _tal(v) -> float | None:
    """Tolk et antal fra Siri: "2,5", "2.5", "to en halv", "halvanden", "en halv", "3 timer"."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v or "").strip().lower().replace("½", " og en halv")
    if not s:
        return None
    if "halvanden" in s:
        return 1.5
    m = re.search(r"\d+(?:[.,]\d+)?", s)
    tal = float(m.group(0).replace(",", ".")) if m else None
    if tal is None:
        for ordet in re.findall(r"[a-zæøåé]+", s):
            if ordet in _TAL_ORD:
                tal = float(_TAL_ORD[ordet])
                break
    halv = bool(re.search(r"\bhalv\b", s))
    kvart = bool(re.search(r"\bkvart\b", s))
    if tal is None:
        return 0.5 if halv else (0.25 if kvart else None)
    # "en halv" alene = 0,5; "to en halv"/"to og en halv" = 2,5
    if halv and not m:
        rest = re.sub(r"\b(en|et|og|halv|time|timer)\b", " ", s).strip()
        if not rest:
            return 0.5
        return tal + 0.5
    if halv and m and "," not in m.group(0) and "." not in m.group(0):
        return tal + 0.5
    if kvart and not m:
        return tal + 0.25
    return tal


def _fmt(t: float) -> str:
    return (f"{t:.2f}".rstrip("0").rstrip(".")).replace(".", ",")


def _aktive_sager(data: dict[str, dict]) -> list[dict]:
    """Ikke-afsluttede sager, den senest brugte først."""
    brugt: dict[str, float] = {}
    for r in data.values():
        if r.get("kind") == "post" and not r.get("deleted"):
            sid = r.get("sagId")
            brugt[sid] = max(brugt.get(sid, 0), r.get("updated") or 0)
    sager = [r for r in data.values() if r.get("kind") == "sag" and not r.get("deleted") and not r.get("arkiveret")]
    sager.sort(key=lambda r: (brugt.get(r.get("id"), 0), r.get("updated") or 0), reverse=True)
    return sager


def _sag_navn(r: dict) -> str:
    return f"{r.get('nr', '')} {r.get('adresse', '')}".strip()


class StemmeSagerView(HomeAssistantView):
    """GET: liste over åbne sager som tekst, til "Vælg fra liste" i en Genvej."""

    url = "/api/timeregistrering/stemme/sager"
    name = "api:timeregistrering:stemme:sager"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store

    async def get(self, request: web.Request) -> web.Response:
        data = await self.hass.async_add_executor_job(self.store.laes, request["hass_user"].id)
        return self.json({"sager": [_sag_navn(r) for r in _aktive_sager(data)]})


class StemmeView(HomeAssistantView):
    """POST fra en Genvej: {"sag": "1234 Vejnavn 5", "type": "timer|materiale|note",
    "timer": "2,5", "tekst": "...", "vare": "...", "antal": "3", "enhed": "stk"}.

    Svarer med {"svar": "..."}, som Genvejen kan læse højt.
    """

    url = "/api/timeregistrering/stemme"
    name = "api:timeregistrering:stemme"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, store: Store) -> None:
        self.hass = hass
        self.store = store

    async def post(self, request: web.Request) -> web.Response:
        import secrets
        import time
        from datetime import datetime
        from zoneinfo import ZoneInfo

        body = await request.read()
        if len(body) > MAX_RECORD_BYTES:
            return self.json({"svar": "Det var for meget tekst."}, 413)
        try:
            obj = json.loads(body or b"{}")
        except ValueError:
            return self.json({"svar": "Genvejen sendte noget, jeg ikke forstod."}, 400)
        if not isinstance(obj, dict):
            return self.json({"svar": "Genvejen sendte noget, jeg ikke forstod."}, 400)

        user_id = request["hass_user"].id
        data = await self.hass.async_add_executor_job(self.store.laes, user_id)

        # Find sagen: tag sagsnummeret (første ord) fra det valgte, eller brug den senest brugte.
        aktive = _aktive_sager(data)
        valgt = str(obj.get("sag") or "").strip()
        sag = None
        if valgt:
            nr = valgt.split()[0].lower()
            alle = aktive + [r for r in data.values() if r.get("kind") == "sag" and not r.get("deleted") and r not in aktive]
            sag = next((r for r in alle if str(r.get("nr", "")).strip().lower() == nr), None)
            if sag is None:
                return self.json({"svar": f"Jeg kunne ikke finde sag {valgt}."}, 404)
        elif aktive:
            sag = aktive[0]
        else:
            return self.json({"svar": "Du har ingen åbne sager i appen."}, 404)

        typ = str(obj.get("type") or "timer").strip().lower()
        tekst = str(obj.get("tekst") or "").strip()[:2000]
        tz = ZoneInfo(self.hass.config.time_zone or "Europe/Copenhagen")
        nu = int(time.time() * 1000)
        post = {
            "id": f"p-siri-{nu:x}-{secrets.token_hex(3)}",
            "kind": "post",
            "sagId": sag.get("id"),
            "dato": datetime.now(tz).strftime("%Y-%m-%d"),
            "oprettet": nu,
            "updated": nu,
        }
        if typ.startswith("tim"):
            t = _tal(obj.get("timer"))
            if not t or t <= 0 or t > 24:
                return self.json({"svar": "Jeg forstod ikke antallet af timer. Prøv igen."}, 400)
            post.update(type="timer", timer=t, tekst=tekst)
            svar = f"Gemt: {_fmt(t)} {'time' if t == 1 else 'timer'} på sag {sag.get('nr')}."
        elif typ.startswith("mat"):
            vare = str(obj.get("vare") or "").strip()[:200]
            antal = _tal(obj.get("antal")) or 1
            enhed = str(obj.get("enhed") or "stk").strip()[:10] or "stk"
            if not vare:
                return self.json({"svar": "Jeg fik ikke hvilken vare. Prøv igen."}, 400)
            post.update(type="materiale", vare=vare, antal=antal, enhed=enhed, varenr="")
            svar = f"Gemt: {_fmt(antal)} {enhed} {vare} på sag {sag.get('nr')}."
        else:
            if not tekst:
                return self.json({"svar": "Noten var tom. Prøv igen."}, 400)
            post.update(type="note", tekst=tekst)
            svar = f"Note gemt på sag {sag.get('nr')}."

        await self.hass.async_add_executor_job(self.store.sync, user_id, [post])
        return self.json({"svar": svar, "sag": _sag_navn(sag)})


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
        for view in (SyncView(hass, store), MigView(), IcsView(hass), KalenderLinkView(hass, store),
                     KalenderFeedView(hass, store), StemmeSagerView(hass, store), StemmeView(hass, store),
                     AppRedirectView()):
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
