"""Lesbord — digitaal lesbord voor wiskunde.

FastAPI backend: klassen, lessen, afbeeldingen-upload, static frontend.
Draait achter nginx onder een subpad (bijv. /lesbord/) omdat de
frontend uitsluitend relatieve URLs gebruikt.
"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from typing import Optional

from a2wsgi import WSGIMiddleware
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from huiswerk.app import app as huiswerk_wsgi_app

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "lesbord.db")
HUISWERK_DB = os.path.join(BASE, "huiswerk.db")
UPLOAD_DIR = os.path.join(BASE, "uploads")
STATIC_DIR = os.path.join(BASE, "static")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_UPLOAD = 10 * 1024 * 1024  # 10 MB

# --- Wachtwoordbeveiliging -------------------------------------------------
# Eén gedeeld wachtwoord vóór het lesbord (geen accounts). Zet in productie
# een eigen wachtwoord via de omgevingsvariabele LESBORD_WACHTWOORD.
WACHTWOORD = os.environ.get("LESBORD_WACHTWOORD", "lesbord")
COOKIE_NAAM = "lesbord_auth"
COOKIE_MAXAGE = 60 * 60 * 24 * 30  # 30 dagen
# Publieke paden die zónder inloggen bereikbaar zijn.
PUBLIEKE_PADEN = {"/login", "/api/login", "/logout"}


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db() -> None:
    con = db()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS klassen (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            naam   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lessen (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            klas_id    INTEGER NOT NULL REFERENCES klassen(id) ON DELETE CASCADE,
            titel      TEXT NOT NULL,
            datum      TEXT NOT NULL,
            content    TEXT NOT NULL DEFAULT '{}',
            updated_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_lessen_klas ON lessen(klas_id, datum);
        CREATE TABLE IF NOT EXISTS instellingen (
            sleutel TEXT PRIMARY KEY,
            waarde  TEXT NOT NULL
        );
        """
    )
    con.commit()
    # migratie: kolom leerlingen (JSON-lijst met namen) toevoegen indien nodig
    cols = [r[1] for r in con.execute("PRAGMA table_info(klassen)")]
    if "leerlingen" not in cols:
        con.execute("ALTER TABLE klassen ADD COLUMN leerlingen TEXT NOT NULL DEFAULT '[]'")
        con.commit()
    con.close()


init_db()
app = FastAPI(title="Lesbord")


# --- Auth-helpers ----------------------------------------------------------
def _auth_secret() -> bytes:
    """Per-deploy geheim om het login-cookie te ondertekenen; blijft stabiel
    over herstarts zodat sessies behouden blijven."""
    con = db()
    row = con.execute(
        "SELECT waarde FROM instellingen WHERE sleutel='_auth_secret'"
    ).fetchone()
    if row:
        s = row["waarde"]
    else:
        s = secrets.token_hex(32)
        con.execute(
            "INSERT INTO instellingen(sleutel, waarde) VALUES ('_auth_secret', ?)",
            (s,),
        )
        con.commit()
    con.close()
    return s.encode()


def _verwacht_token() -> str:
    """Onvervalsbaar token: HMAC van een constante met het serverdgeheim."""
    return hmac.new(_auth_secret(), b"lesbord-auth-v1", hashlib.sha256).hexdigest()


def _is_ingelogd(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAAM, "")
    return bool(token) and hmac.compare_digest(token, _verwacht_token())


def _naar_login(path: str) -> RedirectResponse:
    """Redirect naar de wachtwoordpagina, prefix-onafhankelijk.

    Berekent hoeveel mappen diep het verzoek zit en klimt met ``../`` terug
    naar de app-root. Werkt zo zowel op de root, onder een nginx-subpad
    (/lesbord/…) als vanuit de gemounte huiswerk-app (/huiswerk/…).
    """
    dirpad = path if path.endswith("/") else path.rsplit("/", 1)[0] + "/"
    omhoog = len([s for s in dirpad.split("/") if s])
    return RedirectResponse("../" * omhoog + "login", status_code=303)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if path in PUBLIEKE_PADEN or _is_ingelogd(request):
        return await call_next(request)
    # API-verzoeken (ook /huiswerk/api/…) krijgen 401 i.p.v. een redirect.
    if "/api/" in path:
        return JSONResponse({"detail": "Niet ingelogd"}, status_code=401)
    return _naar_login(path)


class LoginIn(BaseModel):
    wachtwoord: str


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


@app.post("/api/login")
def login(body: LoginIn):
    if not secrets.compare_digest(body.wachtwoord, WACHTWOORD):
        raise HTTPException(401, "Onjuist wachtwoord")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        COOKIE_NAAM,
        _verwacht_token(),
        max_age=COOKIE_MAXAGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("login", status_code=303)
    resp.delete_cookie(COOKIE_NAAM, path="/")
    return resp


class KlasIn(BaseModel):
    naam: str


class LesIn(BaseModel):
    titel: str
    datum: str


class LesUpdate(BaseModel):
    titel: Optional[str] = None
    datum: Optional[str] = None
    content: Optional[dict] = None


class KopieIn(BaseModel):
    klas_id: int
    datum: Optional[str] = None


@app.get("/api/klassen")
def list_klassen():
    con = db()
    rows = con.execute(
        """SELECT k.id, k.naam, k.leerlingen, COUNT(l.id) AS aantal
           FROM klassen k LEFT JOIN lessen l ON l.klas_id = k.id
           GROUP BY k.id ORDER BY k.naam"""
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        d["leerlingen"] = json.loads(d["leerlingen"] or "[]")
        out.append(d)
    return out


class LeerlingenIn(BaseModel):
    namen: list[str]


@app.put("/api/klassen/{klas_id}/leerlingen")
def zet_leerlingen(klas_id: int, body: LeerlingenIn):
    namen = [n.strip() for n in body.namen if n.strip()]
    con = db()
    con.execute("UPDATE klassen SET leerlingen=? WHERE id=?", (json.dumps(namen), klas_id))
    con.commit()
    con.close()
    return {"ok": True, "aantal": len(namen)}


def _huiswerk_klas_stats(naam: str) -> Optional[dict]:
    """Zoek de bijbehorende huiswerkcontrole-klas (match op klasnaam) en bereken
    per leerling het percentage gemaakt huiswerk. Read-only op huiswerk.db.

    Percentage = groen / (groen + rood), net als in de huiswerkcontrole zelf:
    niet-gecontroleerde vakjes tellen niet mee.
    """
    if not os.path.exists(HUISWERK_DB):
        return None
    con = sqlite3.connect(f"file:{HUISWERK_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        k = con.execute(
            "SELECT id, naam FROM klas WHERE lower(trim(naam)) = lower(trim(?))",
            (naam,),
        ).fetchone()
        if not k:
            return None
        leerlingen = con.execute(
            "SELECT id, voornaam FROM leerling WHERE klas_id=? ORDER BY volgorde, voornaam",
            (k["id"],),
        ).fetchall()
        opdrachten = con.execute(
            "SELECT id, nummer, datum, periode, hoofdstuk FROM opdracht WHERE klas_id=? "
            "ORDER BY periode, volgorde, id",
            (k["id"],),
        ).fetchall()
        statussen = con.execute(
            """SELECT s.leerling_id AS lid, s.opdracht_id AS oid, s.waarde AS w
               FROM status s JOIN leerling l ON l.id = s.leerling_id
               WHERE l.klas_id = ?""",
            (k["id"],),
        ).fetchall()
    finally:
        con.close()

    n = len(leerlingen)
    stat = {(r["lid"], r["oid"]): r["w"] for r in statussen}

    # Per leerling (voor namen-overname en het gemiddelde).
    uit, procenten = [], []
    for l in leerlingen:
        g = sum(1 for o in opdrachten if stat.get((l["id"], o["id"])) == 1)
        rd = sum(1 for o in opdrachten if stat.get((l["id"], o["id"])) == 2)
        gecontroleerd = g + rd
        pct = round(g / gecontroleerd * 100) if gecontroleerd else None
        if pct is not None:
            procenten.append(pct)
        uit.append({"voornaam": l["voornaam"], "procent": pct, "gemaakt": g, "nietgemaakt": rd})

    # Per controlemoment (elke opdracht) de klas-verdeling groen/rood/grijs.
    momenten, tot_g, tot_r = [], 0, 0
    for o in opdrachten:
        g = sum(1 for l in leerlingen if stat.get((l["id"], o["id"])) == 1)
        rd = sum(1 for l in leerlingen if stat.get((l["id"], o["id"])) == 2)
        grijs = n - g - rd
        label = ((o["hoofdstuk"] + " ") if o["hoofdstuk"] else "") + o["nummer"]
        momenten.append(
            {
                "label": label,
                "datum": o["datum"],
                "periode": o["periode"],
                "groen": g,
                "rood": rd,
                "grijs": grijs,
                "procent": round(g / (g + rd) * 100) if g + rd else None,
            }
        )
        tot_g += g
        tot_r += rd

    totaal_cellen = n * len(opdrachten)
    totaal = {
        "groen": tot_g,
        "rood": tot_r,
        "grijs": totaal_cellen - tot_g - tot_r,
        "procent": round(tot_g / (tot_g + tot_r) * 100) if tot_g + tot_r else None,
    }
    gemiddelde = round(sum(procenten) / len(procenten)) if procenten else None
    return {
        "gekoppeld": True,
        "huiswerk_klas_id": k["id"],
        "naam": k["naam"],
        "aantal_leerlingen": n,
        "leerlingen": uit,
        "gemiddelde": gemiddelde,
        "totaal": totaal,
        "momenten": momenten,
    }


@app.get("/api/klassen/{klas_id}/huiswerk")
def klas_huiswerk(klas_id: int):
    """Huiswerkcontrole-cijfers voor deze klas (gematcht op klasnaam)."""
    con = db()
    row = con.execute("SELECT naam FROM klassen WHERE id=?", (klas_id,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "Klas niet gevonden")
    stats = _huiswerk_klas_stats(row["naam"])
    if stats is None:
        return {"gekoppeld": False, "naam": row["naam"]}
    return stats


@app.post("/api/klassen")
def create_klas(body: KlasIn):
    con = db()
    cur = con.execute("INSERT INTO klassen(naam) VALUES (?)", (body.naam.strip(),))
    con.commit()
    kid = cur.lastrowid
    con.close()
    return {"id": kid, "naam": body.naam.strip(), "aantal": 0}


@app.put("/api/klassen/{klas_id}")
def rename_klas(klas_id: int, body: KlasIn):
    con = db()
    con.execute("UPDATE klassen SET naam=? WHERE id=?", (body.naam.strip(), klas_id))
    con.commit()
    con.close()
    return {"ok": True}


@app.delete("/api/klassen/{klas_id}")
def delete_klas(klas_id: int):
    con = db()
    con.execute("DELETE FROM klassen WHERE id=?", (klas_id,))
    con.commit()
    con.close()
    return {"ok": True}


@app.get("/api/klassen/{klas_id}/lessen")
def list_lessen(klas_id: int):
    con = db()
    rows = con.execute(
        "SELECT id, titel, datum, updated_at FROM lessen WHERE klas_id=? ORDER BY datum DESC, id DESC",
        (klas_id,),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


@app.post("/api/klassen/{klas_id}/lessen")
def create_les(klas_id: int, body: LesIn):
    con = db()
    cur = con.execute(
        "INSERT INTO lessen(klas_id, titel, datum, content, updated_at) VALUES (?,?,?,?,?)",
        (klas_id, body.titel.strip(), body.datum, "{}", time.time()),
    )
    con.commit()
    lid = cur.lastrowid
    con.close()
    return {"id": lid}


@app.get("/api/lessen/{les_id}")
def get_les(les_id: int):
    con = db()
    row = con.execute(
        "SELECT l.*, k.naam AS klas_naam, k.leerlingen AS leerlingen FROM lessen l JOIN klassen k ON k.id=l.klas_id WHERE l.id=?",
        (les_id,),
    ).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "Les niet gevonden")
    d = dict(row)
    d["content"] = json.loads(d["content"] or "{}")
    d["leerlingen"] = json.loads(d["leerlingen"] or "[]")
    return d


@app.put("/api/lessen/{les_id}")
def update_les(les_id: int, body: LesUpdate):
    con = db()
    row = con.execute("SELECT id FROM lessen WHERE id=?", (les_id,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404, "Les niet gevonden")
    if body.titel is not None:
        con.execute("UPDATE lessen SET titel=? WHERE id=?", (body.titel.strip(), les_id))
    if body.datum is not None:
        con.execute("UPDATE lessen SET datum=? WHERE id=?", (body.datum, les_id))
    if body.content is not None:
        con.execute(
            "UPDATE lessen SET content=?, updated_at=? WHERE id=?",
            (json.dumps(body.content), time.time(), les_id),
        )
    con.commit()
    con.close()
    return {"ok": True}


@app.post("/api/lessen/{les_id}/kopieer")
def kopieer_les(les_id: int, body: KopieIn):
    """Kopieer een les naar een (andere) klas — handig bij 4 parallelle klassen."""
    con = db()
    row = con.execute("SELECT * FROM lessen WHERE id=?", (les_id,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404, "Les niet gevonden")
    cur = con.execute(
        "INSERT INTO lessen(klas_id, titel, datum, content, updated_at) VALUES (?,?,?,?,?)",
        (body.klas_id, row["titel"], body.datum or row["datum"], row["content"], time.time()),
    )
    con.commit()
    nid = cur.lastrowid
    con.close()
    return {"id": nid}


@app.delete("/api/lessen/{les_id}")
def delete_les(les_id: int):
    con = db()
    con.execute("DELETE FROM lessen WHERE id=?", (les_id,))
    con.commit()
    con.close()
    return {"ok": True}


class InstellingIn(BaseModel):
    waarde: str


@app.get("/api/instellingen")
def get_instellingen():
    con = db()
    rows = con.execute("SELECT sleutel, waarde FROM instellingen").fetchall()
    con.close()
    # Interne sleutels (bijv. _auth_secret) niet naar de client lekken.
    return {r["sleutel"]: r["waarde"] for r in rows if not r["sleutel"].startswith("_")}


@app.put("/api/instellingen/{sleutel}")
def zet_instelling(sleutel: str, body: InstellingIn):
    if sleutel.startswith("_"):
        raise HTTPException(400, "Ongeldige sleutel")
    con = db()
    con.execute(
        "INSERT INTO instellingen(sleutel, waarde) VALUES (?,?) "
        "ON CONFLICT(sleutel) DO UPDATE SET waarde=excluded.waarde",
        (sleutel, body.waarde),
    )
    con.commit()
    con.close()
    return {"ok": True}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or "plak.png")[1].lower() or ".png"
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, "Alleen afbeeldingen toegestaan")
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Afbeelding te groot (max 10 MB)")
    name = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
        f.write(data)
    return {"url": f"uploads/{name}"}


app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


@app.get("/huiswerk")
def huiswerk_slash():
    # Zorg voor de afsluitende slash zodat de relatieve URLs in de huiswerk-app
    # kloppen (ook onder een subpad).
    return RedirectResponse("huiswerk/", status_code=307)


# Huiswerkcontrole-app (Flask) gemount onder /huiswerk/. Valt onder dezelfde
# wachtwoordbeveiliging als het lesbord, dus één keer inloggen volstaat.
app.mount("/huiswerk", WSGIMiddleware(huiswerk_wsgi_app))


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8600)
