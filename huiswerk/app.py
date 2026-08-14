import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, g, jsonify, request, send_from_directory, session

# huiswerk.db staat in de lesbord-projectmap (één niveau boven dit bestand),
# naast lesbord.db. Absoluut pad zodat het losstaat van de werkmap.
_HIER = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(_HIER), "huiswerk.db"))
PIN = os.environ.get("APP_PIN", "")
TZ = ZoneInfo("Europe/Amsterdam")

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "wijzig-dit-in-productie")

SCHEMA = """
CREATE TABLE IF NOT EXISTS klas (
    id INTEGER PRIMARY KEY,
    naam TEXT NOT NULL,
    volgorde INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS leerling (
    id INTEGER PRIMARY KEY,
    klas_id INTEGER NOT NULL REFERENCES klas(id) ON DELETE CASCADE,
    voornaam TEXT NOT NULL,
    volgorde INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS opdracht (
    id INTEGER PRIMARY KEY,
    klas_id INTEGER NOT NULL REFERENCES klas(id) ON DELETE CASCADE,
    nummer TEXT NOT NULL,
    datum TEXT,
    periode INTEGER NOT NULL DEFAULT 1,
    hoofdstuk TEXT NOT NULL DEFAULT '',
    volgorde INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS status (
    leerling_id INTEGER NOT NULL REFERENCES leerling(id) ON DELETE CASCADE,
    opdracht_id INTEGER NOT NULL REFERENCES opdracht(id) ON DELETE CASCADE,
    waarde INTEGER NOT NULL,
    gewijzigd TEXT,
    PRIMARY KEY (leerling_id, opdracht_id)
);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    kolommen = {r[1] for r in conn.execute("PRAGMA table_info(opdracht)")}
    if "periode" not in kolommen:
        conn.execute("ALTER TABLE opdracht ADD COLUMN periode INTEGER NOT NULL DEFAULT 1")
    if "hoofdstuk" not in kolommen:
        conn.execute("ALTER TABLE opdracht ADD COLUMN hoofdstuk TEXT NOT NULL DEFAULT ''")
    conn.commit()
    conn.close()


def vandaag():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def ingelogd():
    return not PIN or session.get("ok") is True


@app.before_request
def bewaak():
    if request.path.startswith("/api/") and request.path != "/api/login":
        if not ingelogd():
            return jsonify(error="Niet ingelogd"), 401


@app.route("/")
def index():
    pad = os.path.join(app.root_path, "static", "index.html")
    if not os.path.exists(pad):
        return (
            "static/index.html ontbreekt. Verwachte structuur:\n\n"
            "  app.py\n  static/index.html\n\n"
            f"Gezocht in: {pad}",
            500,
            {"Content-Type": "text/plain; charset=utf-8"},
        )
    return send_from_directory("static", "index.html")


@app.post("/api/login")
def login():
    if not PIN:
        return jsonify(ok=True)
    if (request.json or {}).get("pin") == PIN:
        session["ok"] = True
        session.permanent = True
        return jsonify(ok=True)
    return jsonify(error="Onjuiste pincode"), 403


@app.get("/api/sessie")
def sessie():
    return jsonify(pin_nodig=bool(PIN), ingelogd=ingelogd())


@app.get("/api/klassen")
def klassen():
    rijen = db().execute(
        """
        SELECT k.id, k.naam,
               (SELECT COUNT(*) FROM leerling l WHERE l.klas_id = k.id) AS aantal,
               (SELECT MAX(datum) FROM opdracht o WHERE o.klas_id = k.id) AS laatste
        FROM klas k ORDER BY k.volgorde, k.naam
        """
    ).fetchall()
    return jsonify([dict(r) for r in rijen])


@app.post("/api/klassen")
def klas_toevoegen():
    naam = (request.json or {}).get("naam", "").strip()
    if not naam:
        return jsonify(error="Geef een klasnaam op"), 400
    conn = db()
    cur = conn.execute("INSERT INTO klas (naam) VALUES (?)", (naam,))
    conn.commit()
    return jsonify(id=cur.lastrowid, naam=naam)


@app.delete("/api/klassen/<int:klas_id>")
def klas_verwijderen(klas_id):
    conn = db()
    conn.execute("DELETE FROM klas WHERE id = ?", (klas_id,))
    conn.commit()
    return jsonify(ok=True)


@app.get("/api/klassen/<int:klas_id>")
def klas(klas_id):
    conn = db()
    k = conn.execute("SELECT id, naam FROM klas WHERE id = ?", (klas_id,)).fetchone()
    if k is None:
        return jsonify(error="Klas bestaat niet"), 404
    leerlingen = conn.execute(
        "SELECT id, voornaam FROM leerling WHERE klas_id = ? ORDER BY volgorde, voornaam",
        (klas_id,),
    ).fetchall()
    opdrachten = conn.execute(
        "SELECT id, nummer, datum, periode, hoofdstuk FROM opdracht WHERE klas_id = ? "
        "ORDER BY periode, volgorde, id",
        (klas_id,),
    ).fetchall()
    statussen = conn.execute(
        """
        SELECT s.leerling_id, s.opdracht_id, s.waarde
        FROM status s JOIN leerling l ON l.id = s.leerling_id
        WHERE l.klas_id = ?
        """,
        (klas_id,),
    ).fetchall()
    return jsonify(
        id=k["id"],
        naam=k["naam"],
        leerlingen=[dict(r) for r in leerlingen],
        opdrachten=[dict(r) for r in opdrachten],
        status={f"{r['leerling_id']}-{r['opdracht_id']}": r["waarde"] for r in statussen},
    )


@app.post("/api/klassen/<int:klas_id>/leerlingen")
def leerlingen_toevoegen(klas_id):
    tekst = (request.json or {}).get("namen", "")
    namen = [n.strip() for n in tekst.splitlines() if n.strip()]
    if not namen:
        return jsonify(error="Geef minstens één voornaam op"), 400
    conn = db()
    start = conn.execute(
        "SELECT COALESCE(MAX(volgorde), 0) FROM leerling WHERE klas_id = ?", (klas_id,)
    ).fetchone()[0]
    conn.executemany(
        "INSERT INTO leerling (klas_id, voornaam, volgorde) VALUES (?, ?, ?)",
        [(klas_id, n, start + i + 1) for i, n in enumerate(namen)],
    )
    conn.commit()
    return jsonify(ok=True, toegevoegd=len(namen))


@app.delete("/api/leerlingen/<int:leerling_id>")
def leerling_verwijderen(leerling_id):
    conn = db()
    conn.execute("DELETE FROM leerling WHERE id = ?", (leerling_id,))
    conn.commit()
    return jsonify(ok=True)


@app.post("/api/klassen/<int:klas_id>/opdrachten")
def opdracht_toevoegen(klas_id):
    data = request.json or {}
    periode = data.get("periode", 1)
    if periode not in (1, 2, 3, 4):
        return jsonify(error="Ongeldige periode"), 400
    hoofdstuk = str(data.get("hoofdstuk", "")).strip()
    nummers = data.get("nummers")
    if nummers is None:
        enkel = (data.get("nummer") or "").strip()
        nummers = [enkel] if enkel else []
    nummers = [str(n).strip() for n in nummers if str(n).strip()]
    if not nummers:
        return jsonify(error="Geef minstens één opdrachtnummer op"), 400

    conn = db()
    bestaand = {
        r[0]
        for r in conn.execute(
            "SELECT nummer FROM opdracht WHERE klas_id = ? AND periode = ? AND hoofdstuk = ?",
            (klas_id, periode, hoofdstuk),
        )
    }
    nieuw, gezien = [], set()
    for n in nummers:
        if n in bestaand or n in gezien:
            continue
        gezien.add(n)
        nieuw.append(n)

    start = conn.execute(
        "SELECT COALESCE(MAX(volgorde), 0) FROM opdracht WHERE klas_id = ? AND periode = ?",
        (klas_id, periode),
    ).fetchone()[0]
    conn.executemany(
        "INSERT INTO opdracht (klas_id, nummer, periode, hoofdstuk, volgorde) VALUES (?, ?, ?, ?, ?)",
        [(klas_id, n, periode, hoofdstuk, start + i + 1) for i, n in enumerate(nieuw)],
    )
    conn.commit()
    return jsonify(aangemaakt=len(nieuw), overgeslagen=len(nummers) - len(nieuw))


@app.delete("/api/klassen/<int:klas_id>/opdrachten")
def opdrachten_periode_wissen(klas_id):
    periode = request.args.get("periode", type=int)
    if periode not in (1, 2, 3, 4):
        return jsonify(error="Ongeldige periode"), 400
    hoofdstuk = request.args.get("hoofdstuk")
    conn = db()
    if hoofdstuk is None:
        cur = conn.execute(
            "DELETE FROM opdracht WHERE klas_id = ? AND periode = ?", (klas_id, periode)
        )
    else:
        cur = conn.execute(
            "DELETE FROM opdracht WHERE klas_id = ? AND periode = ? AND hoofdstuk = ?",
            (klas_id, periode, hoofdstuk),
        )
    conn.commit()
    return jsonify(verwijderd=cur.rowcount)


@app.patch("/api/opdrachten/<int:opdracht_id>")
def opdracht_wijzigen(opdracht_id):
    data = request.json or {}
    conn = db()
    if "nummer" in data:
        conn.execute(
            "UPDATE opdracht SET nummer = ? WHERE id = ?", (data["nummer"].strip(), opdracht_id)
        )
    if "datum" in data:
        conn.execute("UPDATE opdracht SET datum = ? WHERE id = ?", (data["datum"], opdracht_id))
    if "periode" in data and data["periode"] in (1, 2, 3, 4):
        conn.execute("UPDATE opdracht SET periode = ? WHERE id = ?", (data["periode"], opdracht_id))
    if "hoofdstuk" in data:
        conn.execute(
            "UPDATE opdracht SET hoofdstuk = ? WHERE id = ?",
            (str(data["hoofdstuk"]).strip(), opdracht_id),
        )
    conn.commit()
    rij = conn.execute(
        "SELECT id, nummer, datum, periode, hoofdstuk FROM opdracht WHERE id = ?", (opdracht_id,)
    ).fetchone()
    return jsonify(dict(rij))


@app.delete("/api/opdrachten/<int:opdracht_id>")
def opdracht_verwijderen(opdracht_id):
    conn = db()
    conn.execute("DELETE FROM opdracht WHERE id = ?", (opdracht_id,))
    conn.commit()
    return jsonify(ok=True)


@app.post("/api/status")
def status_zetten():
    data = request.json or {}
    leerling_id = data.get("leerling_id")
    opdracht_id = data.get("opdracht_id")
    waarde = int(data.get("waarde", 0))
    if leerling_id is None or opdracht_id is None or waarde not in (0, 1, 2):
        return jsonify(error="Ongeldige registratie"), 400
    conn = db()
    nu = datetime.now(TZ).isoformat(timespec="seconds")
    if waarde == 0:
        conn.execute(
            "DELETE FROM status WHERE leerling_id = ? AND opdracht_id = ?",
            (leerling_id, opdracht_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO status (leerling_id, opdracht_id, waarde, gewijzigd)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(leerling_id, opdracht_id)
            DO UPDATE SET waarde = excluded.waarde, gewijzigd = excluded.gewijzigd
            """,
            (leerling_id, opdracht_id, waarde, nu),
        )
    datum = conn.execute("SELECT datum FROM opdracht WHERE id = ?", (opdracht_id,)).fetchone()
    gezet = datum["datum"] if datum else None
    if waarde != 0 and datum is not None and not datum["datum"]:
        gezet = vandaag()
        conn.execute("UPDATE opdracht SET datum = ? WHERE id = ?", (gezet, opdracht_id))
    conn.commit()
    return jsonify(ok=True, datum=gezet)


@app.get("/api/klassen/<int:klas_id>/export")
def export(klas_id):
    conn = db()
    k = conn.execute("SELECT naam FROM klas WHERE id = ?", (klas_id,)).fetchone()
    if k is None:
        return jsonify(error="Klas bestaat niet"), 404
    leerlingen = conn.execute(
        "SELECT id, voornaam FROM leerling WHERE klas_id = ? ORDER BY volgorde, voornaam",
        (klas_id,),
    ).fetchall()
    opdrachten = conn.execute(
        "SELECT id, nummer, datum, periode, hoofdstuk FROM opdracht WHERE klas_id = ? "
        "ORDER BY periode, volgorde, id",
        (klas_id,),
    ).fetchall()
    stat = {
        (r["leerling_id"], r["opdracht_id"]): r["waarde"]
        for r in conn.execute(
            "SELECT s.leerling_id, s.opdracht_id, s.waarde FROM status s "
            "JOIN leerling l ON l.id = s.leerling_id WHERE l.klas_id = ?",
            (klas_id,),
        )
    }
    label = {0: "", 1: "gemaakt", 2: "niet gemaakt"}
    periodes = sorted({o["periode"] for o in opdrachten})

    def pct(leerling_id, rijen):
        groen = sum(1 for o in rijen if stat.get((leerling_id, o["id"])) == 1)
        rood = sum(1 for o in rijen if stat.get((leerling_id, o["id"])) == 2)
        return f"{round(groen / (groen + rood) * 100)}%" if groen + rood else ""

    kop = ["Voornaam"]
    kop += [
        "P{} {}{} ({})".format(
            o["periode"], (o["hoofdstuk"] + " ") if o["hoofdstuk"] else "", o["nummer"], o["datum"] or "-"
        )
        for o in opdrachten
    ]
    kop += [f"% periode {p}" for p in periodes] + ["% totaal"]
    regels = [";".join(kop)]

    for l in leerlingen:
        rij = [l["voornaam"]]
        rij += [label[stat.get((l["id"], o["id"]), 0)] for o in opdrachten]
        rij += [pct(l["id"], [o for o in opdrachten if o["periode"] == p]) for p in periodes]
        rij.append(pct(l["id"], opdrachten))
        regels.append(";".join(rij))

    csv = "\n".join(regels)
    naam = k["naam"].replace(" ", "_")
    return csv, 200, {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": f'attachment; filename="huiswerk_{naam}.csv"',
    }


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
