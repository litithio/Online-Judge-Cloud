import os
import pathlib
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from bson import ObjectId
from bson.errors import InvalidId
import redis

from auth import get_current_user, herkunft_pruefen

BASIS_VERZEICHNIS = pathlib.Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASIS_VERZEICHNIS / "templates")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

# DB Connections
#
# connectTimeoutMS/socketTimeoutMS wie am Worker (app/worker/worker.py):
# serverSelectionTimeoutMS bleibt bei der Vorgabe von 30 Sekunden, das deckt
# eine Neuwahl des Primary im ReplicaSet ab. Ohne die beiden anderen Werte
# wartet ein Aufruf unbegrenzt, sobald MongoDB die Verbindung annimmt und
# danach nicht mehr antwortet, und blockiert damit einen Thread im
# Threadpool auf Dauer statt für einige Sekunden.
mongo_client = MongoClient(
    MONGO_URI,
    connectTimeoutMS=5000,
    socketTimeoutMS=10000,
)
db = mongo_client["coding_platform"]
# Kein blpop hier, nur rpush in /submit, deshalb reichen fünf Sekunden für
# beide Werte, anders als am Worker.
redis_client = redis.Redis.from_url(
    os.getenv("REDIS_URI", "redis://localhost:6379"),
    socket_connect_timeout=5,
    socket_timeout=5,
)

# Eigene Verbindung nur für /readyz, mit kurzen Zeitlimits.
#
# Der Client oben behält die Vorgabe von 30 Sekunden. MongoDB läuft als
# ReplicaSet, und während einer Neuwahl des Primary ist für einige Sekunden kein
# Server wählbar. Mit 2 Sekunden würden die fachlichen Routen in dieser Zeit
# scheitern, statt die Wahl abzuwarten.
#
# socketTimeoutMS und connectTimeoutMS zusätzlich zu serverSelectionTimeoutMS:
# Letzteres begrenzt nur die Suche nach einem Server. Antwortet ein gewählter
# Server danach nicht mehr, wartet der Aufruf ohne die beiden anderen Werte
# unbegrenzt, und die Aufrufe von /readyz liefen sich im Threadpool auf.
health_client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=2000,
    connectTimeoutMS=2000,
    socketTimeoutMS=2000,
)


def _indizes_anlegen():
    # Beide Aufrufe in einem Block: Ist MongoDB nicht erreichbar, scheitert
    # schon der erste nach dem Zeitlimit von 30 Sekunden, und der zweite
    # entfällt damit. Finge jeder Aufruf sein Scheitern einzeln ab, fiele
    # das Zeitlimit doppelt an.
    #
    # Breit gefangen, denn woran die Erstellung auch scheitert, der Prozess
    # soll weiterlaufen. create_index ist idempotent, beim nächsten Start
    # mit erreichbarer MongoDB entsteht der Index also doch noch.
    try:
        # Trägt sowohl den Durchlauf aus #82 (RUNNING mit abgelaufener Frist
        # finden) als auch dessen Requeue-Vergleich. Ohne ihn liefe die Suche
        # über alle Einreichungen, nicht nur über die wartenden.
        db.submissions.create_index([("status", 1), ("frist", 1)])

        # Trägt die zweite Suche des Durchlaufs aus #113: PENDING ohne
        # frischen Queue-Eintrag (last_enqueued_at älter als
        # REENQUEUE_AFTER_SECONDS). Eigener Index, weil sich diese Suche
        # nicht über frist filtern lässt, die bei PENDING immer None ist.
        db.submissions.create_index([("status", 1), ("last_enqueued_at", 1)])
    except Exception as fehler:
        # flush wie an den anderen Log-Stellen, stdout puffert ohne Terminal
        # blockweise.
        print(
            f"Indizes nicht angelegt, {type(fehler).__name__}: {fehler}",
            flush=True,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Die Indizes entstehen in einem eigenen Thread, nicht hier im Hook: Auf
    # den lifespan-Hook wartet FastAPI, bevor es Anfragen annimmt. Liefe die
    # Erstellung hier, hielte eine nicht erreichbare MongoDB den Start um ihr
    # Zeitlimit auf, und /healthz bliebe so lange stumm, obwohl die Probe
    # bewusst an keinem anderen Dienst hängt (#102, #108).
    #
    # daemon, damit ein Thread, der noch auf MongoDB wartet, das Beenden des
    # Prozesses nicht aufhält.
    threading.Thread(target=_indizes_anlegen, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)

# Die Herkunftsprüfung aus #79 hängt als Middleware am ganzen Prozess und
# nicht als Depends an den Routen. FastAPI bedient /openapi.json, /docs und
# /redoc selbst, und die kennen keine Abhängigkeit. Ohne die Middleware
# beantwortet der Pod sie jedem im Cluster, gemessen mit 200 aus einem Pod im
# Namespace. Die beiden Probes nimmt auth.OFFENE_PFADE aus.
app.middleware("http")(herkunft_pruefen)


def _utf8_tauglich(wert):
    # errors="replace" setzt beim Kodieren ein Fragezeichen, kein U+FFFD.
    if isinstance(wert, str):
        return wert.encode("utf-8", "replace").decode("utf-8")
    if isinstance(wert, list):
        return [_utf8_tauglich(eintrag) for eintrag in wert]
    if isinstance(wert, dict):
        return {_utf8_tauglich(k): _utf8_tauglich(v) for k, v in wert.items()}
    return wert


@app.exception_handler(RequestValidationError)
async def validierungsfehler_antwort(request, exc):
    """Wie der Standard-Handler von FastAPI, nur UTF-8-fest (#80).

    Der Standard-Handler schreibt die abgelehnte Eingabe in die 422-Antwort
    zurück. Trägt sie ein einzelnes Surrogat, etwa in code, wirft die
    UTF-8-Kodierung der Antwort selbst UnicodeEncodeError, und aus der
    Ablehnung wird eine 500. Ersetzt wird über die ganze Fehlerstruktur,
    nicht nur über input, denn auch loc trägt bei einem unbekannten Feld
    dessen Namen und damit beliebige Zeichen des Aufrufers.
    """
    return JSONResponse(
        status_code=422,
        content={"detail": _utf8_tauglich(jsonable_encoder(exc.errors()))},
    )


app.mount("/static", StaticFiles(directory=BASIS_VERZEICHNIS / "static"), name="static")

# Bauplan der Plattform: alle Sprachen, die einmal einen Worker bekommen
# sollen (#107). /submit prüft nicht gegen diese Liste, denn eine Einreichung
# in einer Sprache ohne Worker landete in einer Queue, die niemand bedient,
# und bliebe dauerhaft PENDING.
SPRACHEN = ("python", "java", "cpp", "rust")

# Sprachen, zu denen tatsächlich ein Worker läuft (judge.sprachen in
# app/chart/values.yaml, je Eintrag ein Deployment samt ScaledObject). Nur
# gegen diese Liste prüft /submit. Eine Sprache aus dem Bauplan wandert
# hierher, sobald ihr Worker-Image existiert und das Chart sie ausrollt.
AKTIVE_SPRACHEN = ("python",)
STANDARD_SPRACHE = "python"

# Vorgaben des Workers, wenn eine Aufgabe kein eigenes Limit trägt
# (app/worker/worker.py, grenzen_der_aufgabe: SANDBOX_TIMEOUT/
# SANDBOX_SPEICHER_MB). Hier dupliziert, weil der Worker in einem anderen
# Image liegt und aufgabe_seite nur zur Anzeige braucht, was tatsächlich
# gilt - "–" wäre falsch, der Worker setzt in diesem Fall durch, nicht ab.
WORKER_STANDARD_ZEIT_S = 5
WORKER_STANDARD_SPEICHER_MB = 128

# Verwaltung (#240): sichtbar für Konten mit der Keycloak-Rolle dozent, die
# das OIDC-Plugin als X-Auth-Request-Roles weiterreicht (auth.py,
# traefik-auth.yaml). Ersetzt die frühere ADMIN_USERS-Liste aus #56, die nur
# Benutzernamen kannte statt einer echten Rolle.
DOZENT_ROLLE = "dozent"


def _ist_admin(user):
    return DOZENT_ROLLE in user.get("roles", [])


# Blendet die Testfälle aus wie bisher die Projektion {"test_cases": 0},
# liefert aber ihre Anzahl mit (#71). Eingaben und erwartete Ausgaben bleiben
# damit verborgen. Pipeline statt Projektion mit Ausdruck, weil die jedes
# auszugebende Feld aufzählen müsste und ein neues Feld dann stumm fehlte.
# $ifNull, damit ein von Hand angelegtes Dokument ohne test_cases die Abfrage
# nicht abbricht, $size allein wirft dann einen Fehler.
TESTFAELLE_GEZAEHLT = [
    {"$set": {"test_case_count": {"$size": {"$ifNull": ["$test_cases", []]}}}},
    {"$unset": "test_cases"},
]


def parse_json(data):
    data["id"] = str(data["_id"])
    del data["_id"]
    return data


# Für die absolute Zeitangabe auf der fertigen Ergebnisseite: created_at
# steht als UTC in MongoDB (submit_code), eine deutsche Uhrzeit ohne
# Umrechnung läge zur Sommerzeit zwei Stunden daneben. Die relative Angabe
# in _relative_zeit unten braucht das nicht, eine Zeitdifferenz ist in jeder
# Zeitzone gleich lang.
BERLIN_TZ = ZoneInfo("Europe/Berlin")

# Anzeigetext je Status, sofern die Einreichung noch kein result trägt (siehe
# worker.py: PENDING/RUNNING haben nie eines, SUCCESS/FAILED/UNRESOLVED
# schreibt _ergebnis_schreiben immer mit dem Urteil hinein).
STATUS_TEXT = {
    "PENDING": "in der Warteschlange",
    "RUNNING": "wird ausgeführt",
    "UNRESOLVED": "kein Urteil möglich",
}

# CSS-Klasse aus dhbw.css je Status (.status.wartet/.laeuft/.ok/.fehler).
STATUS_KLASSE = {
    "PENDING": "wartet",
    "RUNNING": "laeuft",
    "SUCCESS": "ok",
    "FAILED": "fehler",
    "UNRESOLVED": "fehler",
}


def _relative_zeit(zeitpunkt):
    """ "vor X s/min/h/d", einmalig zum Zeitpunkt der Anfrage berechnet.

    Ohne HTMX aktualisiert sich die Seite nicht von selbst, der Wert bleibt
    also bis zum nächsten Laden stehen. Das entspricht dem heutigen Stand
    ohne Polling, siehe #56.

    zeitpunkt kommt naiv aus MongoDB zurück (pymongo ohne tz_aware=True),
    geschrieben wurde es als UTC (main.py, submit_code). replace statt
    astimezone, denn ein naiver Wert trägt keine Zeitzone, die sich
    umrechnen ließe.
    """
    zeitpunkt = zeitpunkt.replace(tzinfo=timezone.utc)
    sekunden = max(0, int((datetime.now(timezone.utc) - zeitpunkt).total_seconds()))
    if sekunden < 60:
        return f"vor {sekunden} s"
    minuten = sekunden // 60
    if minuten < 60:
        return f"vor {minuten} min"
    stunden = minuten // 60
    if stunden < 24:
        return f"vor {stunden} h"
    return f"vor {stunden // 24} d"


def _einreichung_ansicht(submission, aufgaben_titel):
    return {
        "id": str(submission["_id"]),
        "user_id": submission.get("user_id", ""),
        "username": submission.get("username", "?"),
        "task_id": submission["task_id"],
        "task_titel": aufgaben_titel.get(submission["task_id"], "Aufgabe gelöscht"),
        "sprache": submission["sprache"],
        "eingereicht": _relative_zeit(submission["created_at"]),
        "status_klasse": STATUS_KLASSE.get(submission["status"], "fehler"),
        # result trägt bei SUCCESS/FAILED/UNRESOLVED bereits den fertigen
        # deutschen Satz (worker.py, _urteil). Nur bei PENDING/RUNNING fehlt
        # es, dafür greift STATUS_TEXT.
        "status_text": submission.get("result")
        or STATUS_TEXT.get(submission["status"], submission["status"]),
    }


# Klartext je Testfall-Urteil (worker.py: run_code_in_sandbox/​_urteil). AC ist
# der einzige positive Fall, alles andere zeigt dieselbe Fehlerfarbe, nur mit
# passendem Text.
VERDICT_TEXT = {
    "AC": "bestanden",
    "WA": "falsche Ausgabe",
    "TLE": "Zeitlimit überschritten",
    "MLE": "Speicherlimit überschritten",
    "RE": "Laufzeitfehler",
    "OLE": "zu viel Ausgabe",
}


def _testfaelle_ansicht(test_results, namen=None):
    """Ansicht je Testfall für ergebnis.html und ergebnis-laeuft.html.

    NOT_RUN heißt bei einer fertigen Einreichung "wegen Abbruch nie
    gelaufen" und bei einer laufenden "noch nicht dran" (worker.py, _urteil:
    Platzhalter vor dem ersten Fall, Abbruch beim ersten Fehlschlag). Beides
    zeigt dieselbe Zeile "steht aus", welcher der beiden Fälle vorliegt, sagt
    schon status_klasse der Einreichung.

    namen ordnet test_id einen Namen aus der Aufgabe zu (#71). Die Zuordnung
    läuft über die Position, denn test_results trägt keine Namen, der Worker
    schreibt test_id als Nummer des Falls (worker.py, _urteil). Ohne Treffer
    zeigt das Template "Testfall N", etwa wenn die Aufgabe gelöscht ist, ihre
    Testfälle vor dem nächsten Seed noch keine Namen tragen oder der Fall
    kein Beispiel ist (einreichung_seite, #208).

    Die Zuordnung zeigt immer den aktuellen Stand der Aufgabe. Sortiert ein
    Seed die Testfälle um, steht an einer alten Einreichung der Name aus der
    neuen Revision. Stabil würde das erst, wenn der Worker den Namen beim
    Lauf in test_results mitschriebe, wie er es mit test_id tut.
    """
    namen = namen or {}
    ansicht = []
    for t in test_results or []:
        eintrag = {
            "nummer": t["test_id"],
            "name": namen.get(t["test_id"]),
            "offen": t.get("verdict") == "NOT_RUN",
            "zeit_ms": t.get("zeit_ms"),
            "speicher_mb": (
                t["speicher_kb"] / 1024 if t.get("speicher_kb") is not None else None
            ),
        }
        if not eintrag["offen"]:
            eintrag["klasse"] = "ok" if t["verdict"] == "AC" else "fehler"
            eintrag["text"] = VERDICT_TEXT.get(t["verdict"], t["verdict"])
            # eingabe/erwartet/erhalten stehen nur bei WA und erst seit #252
            # (worker.py, _urteil) - eine Einreichung von davor trägt bei WA
            # nur detail als fertigen Satz, diff bleibt dann None und zusatz
            # zeigt wie bisher den Satz.
            eintrag["diff"] = (
                {
                    "eingabe": t["eingabe"],
                    "erwartet": t["erwartet"],
                    "erhalten": t["erhalten"],
                }
                if t["verdict"] == "WA" and "erwartet" in t
                else None
            )
            eintrag["zusatz"] = (
                None if t["verdict"] == "AC" or eintrag["diff"] else t.get("detail")
            )
        ansicht.append(eintrag)
    return ansicht


# Statusse, die einen ausgewerteten Punktstand tragen (test_results
# vollständig, nicht nur ein Platzhalter). PENDING/RUNNING/UNRESOLVED zählen
# nicht mit, siehe _stand_je_aufgabe.
STAND_ZAEHLT_STATUS = ("SUCCESS", "FAILED")


def _stand_je_aufgabe(user_id, task_id=None):
    """Letzte und beste eigene Einreichung je Aufgabe (#252, "Ihr Stand").

    In Python gruppiert statt über eine Aggregation: db.tasks.aggregate wird
    von FakeCollection (tests/backend/fakes.py) bewusst nicht nachgebildet,
    aufgaben_seite und aufgabe_seite laufen aber direkt gegen FakeDb
    (test_seiten_rendern.py, test_aufgabe_rubriken.py). find()+sort() kennt
    der Fake dagegen, wie schon _einreichungen_liste zeigt - derselbe
    Aufbau hier hält die Routen ohne echte MongoDB testbar.

    "Beste" vergleicht die Quote bestandener Testfälle (bestanden/gesamt),
    nicht die reine Anzahl: gesamt kann sich zwischen Revisionen einer
    Aufgabe unterscheiden (neue Testfälle im Seed), eine reine Anzahl wäre
    dann nicht vergleichbar. Einreichungen ohne ausgewerteten Punktstand
    (PENDING vor dem ersten Fall, UNRESOLVED ohne test_results) tragen
    keine Quote und fallen damit aus dem Vergleich heraus.

    task_id schränkt auf eine einzelne Aufgabe ein (aufgabe_seite), ohne
    liefert die Gruppierung für jede Aufgabe mit eigenen Einreichungen einen
    Eintrag (aufgaben_seite).
    """
    filter_query = {"user_id": user_id}
    if task_id is not None:
        filter_query["task_id"] = task_id
    # Neueste zuerst, damit das erste Vorkommen je Aufgabe unten "letzte"
    # ist, wie schon _einreichungen_liste sortiert.
    submissions = db.submissions.find(filter_query).sort("created_at", -1)

    gruppiert = {}
    for s in submissions:
        eintrag = gruppiert.setdefault(s["task_id"], {"letzte": None, "beste": None})
        test_results = s.get("test_results") or []
        bestanden = sum(1 for t in test_results if t.get("verdict") == "AC")
        gesamt = len(test_results)
        zaehlt = s["status"] in STAND_ZAEHLT_STATUS

        if eintrag["letzte"] is None:
            eintrag["letzte"] = {
                "id": str(s["_id"]),
                "status": s["status"],
                "zaehlt": zaehlt,
                "bestanden": bestanden,
                "gesamt": gesamt,
            }
        if zaehlt and gesamt > 0:
            quote = bestanden / gesamt
            bisher = eintrag["beste"]
            if bisher is None or quote > bisher["quote"]:
                eintrag["beste"] = {
                    "bestanden": bestanden,
                    "gesamt": gesamt,
                    "quote": quote,
                }

    ergebnis = {}
    for tid, eintrag in gruppiert.items():
        letzte = eintrag["letzte"]
        beste = eintrag["beste"]
        # Nur zeigen, wenn "beste" tatsächlich von "letzte" abweicht - sonst
        # stünde dieselbe Zahl zweimal da (Entscheidung zu #252).
        beste_weicht_ab = beste is not None and (
            not letzte["zaehlt"]
            or (beste["bestanden"], beste["gesamt"])
            != (letzte["bestanden"], letzte["gesamt"])
        )
        ergebnis[tid] = {
            "id": letzte["id"],
            "status": letzte["status"],
            "klasse": STATUS_KLASSE.get(letzte["status"], "fehler"),
            "zaehlt": letzte["zaehlt"],
            "bestanden": letzte["bestanden"],
            "gesamt": letzte["gesamt"],
            "beste_bestanden": beste["bestanden"] if beste_weicht_ab else None,
            "beste_gesamt": beste["gesamt"] if beste_weicht_ab else None,
        }
    return ergebnis


def _stand_text(eintrag):
    """Text für die Spalte/den Hinweis aus _stand_je_aufgabe.

    eintrag ist None, wenn die Aufgabe noch keine eigene Einreichung hat -
    _stand_je_aufgabe trägt dafür gar keinen Schlüssel ein.
    """
    if eintrag is None:
        return "noch nicht abgegeben"
    if eintrag["zaehlt"]:
        return f"{eintrag['bestanden']} von {eintrag['gesamt']}"
    return STATUS_TEXT.get(eintrag["status"], eintrag["status"])


def _stand_beste_text(eintrag):
    if eintrag is None or eintrag["beste_bestanden"] is None:
        return None
    return f"{eintrag['beste_bestanden']} von {eintrag['beste_gesamt']}"


# Klartext für fehler.html, siehe README ("Herkunftsprüfung"/#15): MongoDB
# oder Valkey nicht erreichbar, oder eine Aufgabe/Einreichung ohne Treffer.
# Kein eigener Dienst, keine Traefik-Middleware, siehe #15 - das hier deckt
# nur ab, was die Anwendung selbst an ihren eigenen Routen bemerkt.
HTTP_STATUS_TEXT = {503: "Service Unavailable", 404: "Not Found", 403: "Forbidden"}


def _fehlerseite(
    request,
    status_code,
    titel,
    meldung,
    zusicherung=None,
    knopf_text="Erneut versuchen",
    knopf_href="/aufgaben",
):
    # Request als erstes Argument, wie an allen TemplateResponse-Aufrufen.
    # starlette hat die ältere Form ohne Request entfernt, und fastapi pinnt
    # starlette nur nach unten, ein frischer Image-Build zieht also immer die
    # neueste Version.
    return templates.TemplateResponse(
        request,
        "fehler.html",
        {
            "titel": titel,
            "meldung": meldung,
            "zusicherung": zusicherung,
            "knopf_text": knopf_text,
            "knopf_href": knopf_href,
            "status_code": status_code,
            "status_text": HTTP_STATUS_TEXT.get(status_code, ""),
        },
        status_code=status_code,
    )


def _dienst_nicht_erreichbar(request, fehler, ort):
    """MongoDB nicht erreichbar an einer HTML-Route (#15).

    Nur für die HTML-Seiten: Die JSON-Endpunkte (/tasks, /submission, ...)
    bleiben unverändert und werfen bei einem PyMongoError weiterhin einen
    unbehandelten 500, wie bisher.
    """
    print(
        f"{ort}: MongoDB nicht erreichbar, {type(fehler).__name__}: {fehler}",
        flush=True,
    )
    return _fehlerseite(
        request,
        503,
        "Der Online Judge ist gerade nicht erreichbar",
        "Die Anwendung wird im Moment nicht bedient. Das passiert bei einem "
        "Neustart und dauert meist nur wenige Sekunden.",
        zusicherung=(
            "Bereits abgegebene Einreichungen sind davon nicht betroffen. Sie "
            "bleiben in der Warteschlange und werden ausgeführt, sobald wieder "
            "ein Worker bereitsteht."
        ),
        knopf_text="Erneut versuchen",
        knopf_href=request.url.path,
    )


# Die beiden Endpunkte für die Probes tragen bewusst kein
# Depends(get_current_user) und stehen in auth.OFFENE_PFADE. Das kubelet ruft
# den Pod direkt auf, an der Auth-Kette vorbei, und schickt weder die
# Identitäts-Header noch den Wert aus #79. Eine geschützte Probe schlüge
# dauerhaft fehl.
#
# @app.head zusätzlich zu @app.get, weil @app.get nur GET registriert und
# HEAD sonst mit 405 antwortet (#127). Externe Prüfungen wie curl -I sprechen
# Health-Pfade mit HEAD an, die Probes im Chart schicken weiter GET.
# Nicht @app.api_route mit beiden Methoden, dort teilen sich GET und HEAD
# eine operationId und FastAPI warnt bei jedem Abruf von /openapi.json.
# HEAD bleibt aus dem Schema, der Eintrag wäre eine Dublette von GET.
@app.get("/healthz")
@app.head("/healthz", include_in_schema=False)
async def healthz():
    """Läuft der Prozess noch? Hängt an keinem anderen Dienst.

    Die livenessProbe startet den Pod neu, wenn hier nichts mehr kommt. Eine
    Prüfung von MongoDB gehört deshalb nicht hierher: ein Ausfall der Datenbank
    würde sonst reihum alle Pods neu starten, ohne dass ein Neustart hilft.

    async, damit die Antwort im Eventloop entsteht. Die übrigen Endpunkte sind
    synchron und laufen im Threadpool. Wäre /healthz auch dort, könnten
    blockierte Aufrufe es mit ausbremsen und einen Neustart auslösen.
    """
    return {"status": "ok"}


@app.get("/readyz")
@app.head("/readyz", include_in_schema=False)
def readyz():
    """Kann der Pod Anfragen beantworten?

    Geprüft wird MongoDB, weil jede Route sie braucht. Valkey bleibt außen vor:
    ohne die Queue scheitert /submit, während /tasks und /submission weiter
    antworten. Ein Pod ohne Valkey ist also mehr wert als gar kein Pod.
    """
    try:
        health_client.admin.command("ping")
    except PyMongoError as fehler:
        # Der Grund geht ins Log und nicht in die Antwort. Die Meldung von
        # PyMongo nennt die Hostnamen der drei Mitglieder und den Zustand des
        # ReplicaSets, und /readyz antwortet als Probe-Pfad ohne
        # Herkunftsprüfung jedem im Cluster.
        print(
            f"readyz: MongoDB nicht erreichbar, {type(fehler).__name__}: {fehler}",
            flush=True,
        )
        raise HTTPException(status_code=503, detail="Nicht bereit")
    return {"status": "ok"}


@app.get("/")
def index():
    """https://app.<zone> landet hier nach der Anmeldung (traefik-auth.yaml),
    /aufgaben ist die eigentliche Einstiegsseite. Ohne diese Route endete der
    dokumentierte Aufruf in einem JSON-404.
    """
    return RedirectResponse(url="/aufgaben")


@app.get("/tasks")
def get_tasks(user=Depends(get_current_user)):
    tasks = list(db.tasks.aggregate(TESTFAELLE_GEZAEHLT))
    return [parse_json(t) for t in tasks]


@app.get("/aufgaben", response_class=HTMLResponse)
def aufgaben_seite(request: Request, user=Depends(get_current_user)):
    try:
        # Wie /tasks, nur ohne die Anzahl der Testfälle. Die Liste zeigt sie
        # nicht, erst die Detailseite (aufgabe_seite).
        tasks = list(db.tasks.find({}, {"test_cases": 0}))
    except PyMongoError as fehler:
        return _dienst_nicht_erreichbar(request, fehler, "/aufgaben")
    try:
        stand = _stand_je_aufgabe(user.get("sub"))
    except PyMongoError as fehler:
        return _dienst_nicht_erreichbar(request, fehler, "/aufgaben")
    ansicht = []
    for t in tasks:
        t = parse_json(t)
        # Dieselbe Vorgabe wie auf der Detailseite (aufgabe_seite): ohne
        # eigenes Limit setzt der Worker durch, nicht ab, "–" wäre falsch.
        t["zeit_s"] = t.get("time_limit_seconds") or WORKER_STANDARD_ZEIT_S
        eintrag = stand.get(t["id"])
        t["stand_klasse"] = eintrag["klasse"] if eintrag else "wartet"
        t["stand_vorhanden"] = eintrag is not None
        t["stand_text"] = _stand_text(eintrag)
        t["stand_beste"] = _stand_beste_text(eintrag)
        ansicht.append(t)
    return templates.TemplateResponse(
        request,
        "aufgaben.html",
        {"tasks": ansicht, "user": user, "ist_admin": _ist_admin(user)},
    )


@app.get("/tasks/{task_id}")
def get_task(task_id: str, user=Depends(get_current_user)):
    # Dieselbe 404 mit derselben Meldung wie bei einer fehlenden Aufgabe
    # (#259). Eine ID ohne ObjectId-Format warf sonst InvalidId und damit
    # eine 500, und eine eigene Meldung würde verraten, dass die ID schon
    # am Format scheitert und nicht erst an der Suche.
    if not ObjectId.is_valid(task_id):
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")
    treffer = list(
        db.tasks.aggregate(
            [{"$match": {"_id": ObjectId(task_id)}}] + TESTFAELLE_GEZAEHLT
        )
    )
    if not treffer:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")
    return parse_json(treffer[0])


@app.get("/aufgabe/{task_id}", response_class=HTMLResponse)
def aufgabe_seite(task_id: str, request: Request, user=Depends(get_current_user)):
    def aufgabe_404():
        return _fehlerseite(
            request,
            404,
            "Diese Aufgabe gibt es nicht",
            "Die Aufgabe wurde nicht gefunden. Möglicherweise wurde sie "
            "entfernt oder der Link ist nicht mehr gültig.",
            knopf_text="Zu den Aufgaben",
            knopf_href="/aufgaben",
        )

    try:
        task = db.tasks.find_one(
            {"_id": ObjectId(task_id)}
        )  # dieselbe Abfrage wie /tasks/{task_id}
    except InvalidId:
        # Anders als /tasks/{task_id}, das hier ebenfalls ungefangen ließe:
        # eine kaputte ID aus einem verstümmelten Link ist für einen
        # Seitenaufruf kein Serverfehler, sondern derselbe Fall wie unten.
        return aufgabe_404()
    except PyMongoError as fehler:
        return _dienst_nicht_erreichbar(request, fehler, f"/aufgabe/{task_id}")
    if not task:
        # Anders als /tasks/{task_id}: eine HTML-Seite statt eines nackten
        # JSON-404, siehe #56. Der JSON-Endpunkt selbst bleibt unverändert.
        return aufgabe_404()
    testfaelle = task.pop("test_cases", [])  # Inhalt bleibt verborgen, wie in /tasks
    # Nur die markierten Fälle erreichen das Template, nicht die ganze Liste
    # mit einem Filter dort. Was das Template nie bekommt, kann keine
    # Template-Änderung versehentlich zeigen. Der Vergleich mit True wie in
    # laden.py, das jede andere Belegung von sample ablehnt.
    # removesuffix am Eingabeende. Die Leerzeile zwischen Eingabe und Ausgabe
    # im Beispielblock kommt aus dem Template (wie im Entwurf), fast jede
    # Eingabe endet aber selbst mit einem Zeilenumbruch, und .beispiel zeigt
    # mit white-space pre beide, also eine Leerzeile zu viel.
    beispiele = [
        {
            "input": fall["input"].removesuffix("\n"),
            "expected_output": fall["expected_output"],
        }
        for fall in testfaelle
        if isinstance(fall, dict) and fall.get("sample") is True
    ]
    try:
        letzte = _stand_je_aufgabe(user.get("sub"), task_id=task_id).get(task_id)
    except PyMongoError as fehler:
        return _dienst_nicht_erreichbar(request, fehler, f"/aufgabe/{task_id}")
    return templates.TemplateResponse(
        request,
        "aufgabe.html",
        {
            "task": parse_json(task),
            "anzahl_testfaelle": len(testfaelle),
            "beispiele": beispiele,
            # Was der Worker tatsächlich durchsetzt, wenn die Aufgabe selbst
            # kein Limit trägt (grenzen_der_aufgabe) - "–" wäre hier falsch,
            # betrifft derzeit summe.json.
            "zeit_s": task.get("time_limit_seconds") or WORKER_STANDARD_ZEIT_S,
            "speicher_mb": task.get("memory_limit_mb") or WORKER_STANDARD_SPEICHER_MB,
            "user": user,
            "ist_admin": _ist_admin(user),
            # None ohne eigene Einreichung - editor-fuss zeigt den Hinweis
            # dann gar nicht (#252).
            "letzte_einreichung_id": letzte["id"] if letzte else None,
            "letzte_einreichung_text": _stand_text(letzte),
            "letzte_einreichung_zaehlt": letzte["zaehlt"] if letzte else False,
            "letzte_einreichung_beste": _stand_beste_text(letzte),
        },
    )


# Höchstlänge für code in /submit. Dieselbe Grenze, die die Sandbox der
# Ausgabe einer Einreichung setzt (SANDBOX_AUSGABE_BYTES in
# app/worker/worker.py). Die größte Musterlösung unter app/aufgaben/loesungen
# misst unter 2 KiB, die Grenze hält also keine echte Lösung auf. Sie hält
# das Dokument der Einreichung zugleich weit unter den 16 MB, die MongoDB je
# Dokument zulässt, auch wenn jedes Zeichen in UTF-8 bis zu vier Bytes belegt.
CODE_MAX_ZEICHEN = 1024 * 1024


class SubmitBody(BaseModel):
    """Body von /submit (#80).

    Beide Werte gehen unverändert in submissions und von dort in den Worker.
    Was die Prüfung hier durchlässt, muss der Worker verarbeiten können,
    sonst endet die Einreichung nach erschöpften Versuchen als UNRESOLVED und
    ein Nutzer kann eine falsche Lösung als Störung des Judges erscheinen
    lassen.

    extra="forbid" lehnt unbekannte Felder ab. Sie landeten sonst zwar nicht
    im Dokument, ein Tippfehler wie "sprach" fiele aber stumm auf den
    Standardwert zurück statt auf einen Fehler.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    # Ein einzelnes Surrogat, das am Worker UnicodeEncodeError warf, lehnt
    # die str-Prüfung von pydantic-core selbst ab, dafür braucht es keinen
    # eigenen Validator. Ein Test in tests/backend hält das für die
    # gepinnte pydantic-Version fest.
    code: str = Field(max_length=CODE_MAX_ZEICHEN)
    sprache: str = STANDARD_SPRACHE

    @field_validator("task_id")
    @classmethod
    def _task_id_im_objectid_format(cls, wert):
        # Der Typ str allein hält "abc" nicht auf. Der Worker gibt den Wert
        # an ObjectId() weiter, und dort warf "abc" bisher InvalidId.
        if not ObjectId.is_valid(wert):
            raise ValueError("keine gültige ObjectId")
        return wert


@app.post("/submit")
def submit_code(
    payload: SubmitBody,
    request: Request,
    response: Response,
    user=Depends(get_current_user),
):
    task_id = payload.task_id
    code = payload.code
    sprache = payload.sprache
    if sprache not in AKTIVE_SPRACHEN:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Sprache nicht aktiv: {sprache}. "
                f"Aktive Sprachen: {', '.join(AKTIVE_SPRACHEN)}"
            ),
        )

    # Nachschlagen vor dem Insert (#80). Zu einer Aufgabe, die es nicht
    # gibt, bliebe die Einreichung sonst dauerhaft PENDING liegen, denn der
    # Worker trifft beim find_one nichts und nimmt mit continue den nächsten
    # Job. Eine Aufgabe, die zwischen dieser Prüfung und der Ausführung
    # gelöscht wird, bleibt davon unberührt, das behandelt ein eigenes Issue.
    if db.tasks.find_one({"_id": ObjectId(task_id)}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")

    # 1. Submission in MongoDB erstellen, mit den Feldern aus #82: sprache für
    # die Queue-Auswahl des Durchlaufs, versuche/run_token/frist für die
    # bedingte Übernahme im Worker. Die API selbst besetzt nur versuche mit 0
    # und die anderen beiden mit None, der Worker füllt sie beim Claim.
    jetzt = datetime.now(timezone.utc)
    submission = {
        "user_id": user.get("sub"),
        "username": user.get("preferred_username", "Unknown"),
        "task_id": task_id,
        "code": code,
        "sprache": sprache,
        "status": "PENDING",
        "result": None,
        "test_results": None,
        "versuche": 0,
        # Eigener Zähler statt versuche: versuche steigt nur bei einer
        # tatsächlichen Übernahme (worker.py, _übernehmen), ein Queue-Eintrag,
        # der verlorenging, bevor ihn ein Worker zog, rührt ihn nie an. Ohne
        # requeue_versuche griffe MAX_VERSUCHE in durchlauf.py für #113 also
        # nie, und eine dauerhaft verlorene Einreichung würde unbegrenzt oft
        # erneut eingereiht.
        "requeue_versuche": 0,
        "run_token": None,
        "frist": None,
        "worker_id": None,
        # Zeitpunkt des letzten RPUSH (#113). Gesetzt hier bei der Anlage und
        # erneut vom Durchlauf bei jedem Requeue, siehe durchlauf.py. Trägt
        # dessen Suche nach PENDING-Einreichungen, deren Queue-Eintrag
        # verlorenging, etwa weil Valkey seinen Inhalt verliert oder ein
        # Worker den Eintrag per BLPOP schon zog, aber vor der Übernahme in
        # MongoDB starb. Scheitert der RPUSH unten, steht das Feld danach
        # wieder auf None. Das heißt Ausgang unbekannt und nicht sicher nicht
        # eingereiht, ob die ID in der Liste steht, klärt erst der LPOS-Check
        # im Durchlauf.
        "last_enqueued_at": jetzt,
        # Einzige Stelle, die die Anlage selbst festhält; der Worker schreibt
        # danach nur noch updated_at. Ohne created_at ließe sich nicht sagen,
        # wie lange eine Einreichung schon in der Warteschlange steht.
        "created_at": jetzt,
    }
    sub_id = db.submissions.insert_one(submission).inserted_id

    # 2. Nur die ID in die Queue der Sprache schreiben. Code und Aufgabe liest
    # der Worker erst nach der Übernahme aus MongoDB, siehe worker.py.
    try:
        redis_client.rpush(f"judge:{sprache}", str(sub_id))
    except Exception as fehler:
        # Kein 500 nach einem geglückten Insert. Der Aufrufer läse ihn als
        # nicht angekommen und legte beim Wiederholen eine zweite Einreichung
        # an, die genauso bewertet würde. Die Einreichung bleibt deshalb
        # stehen und die Antwort trägt weiter ihre ID.
        #
        # Die Ausnahme sagt nicht, dass Valkey den RPUSH nicht ausgeführt hat.
        # Bricht die Verbindung nach der Ausführung und vor der Antwort ab,
        # steht die ID bereits in der Liste. Der Marker unten heißt deshalb
        # Ausgang unbekannt, und der LPOS-Check im Durchlauf entscheidet.
        #
        # Breit gefangen, hier und im inneren Block. Nach dem Insert soll
        # nichts mehr den Endpunkt verlassen, und eine eng gefasste Ausnahme
        # ließe genau den Fall durch, an den niemand gedacht hat. Dasselbe
        # tut worker.py beim BLPOP. flush, weil das Backend-Image kein
        # PYTHONUNBUFFERED setzt und stdout ohne Terminal blockweise puffert.
        # Ungeflusht stünde die Meldung erst in docker logs, wenn der Puffer
        # voll ist.
        print(
            f"Einreichung {sub_id}: RPUSH ohne Bestätigung, "
            f"{type(fehler).__name__}: {fehler}",
            flush=True,
        )
        try:
            # last_enqueued_at auf None statt auf jetzt. Der Durchlauf
            # trennt daran den unbestätigten RPUSH von einer Einreichung, die
            # nur wartet (durchlauf.py, _ohne_frischen_eintrag), und prüft
            # diese hier beim nächsten Lauf, statt REENQUEUE_AFTER_SECONDS
            # abzuwarten.
            db.submissions.update_one(
                {"_id": sub_id}, {"$set": {"last_enqueued_at": None}}
            )
        except Exception as marke_fehler:
            # Auch dieser Schreibzugriff darf die Antwort nicht mehr kippen,
            # sonst führt eine Neuwahl des Primary genau zu dem 500 nach
            # geglücktem Insert, den der äußere Block verhindert. Bleibt der
            # Zeitpunkt der Anlage stehen, greift der Durchlauf über seinen
            # $lt-Zweig, nur eben erst nach REENQUEUE_AFTER_SECONDS. Das ist
            # der Weg, den #113 ohnehin vorsieht.
            print(
                f"Einreichung {sub_id}: last_enqueued_at nicht auf None, "
                f"{type(marke_fehler).__name__}: {marke_fehler}",
                flush=True,
            )

    # Nur für den Aufruf per HTMX von aufgabe.html (#56): der Editor postet
    # per hx-post auf diese Route und folgt dem Redirect auf die Ergebnisseite
    # selbst. Jeder andere Aufrufer - lastgenerator.py, curl, künftige
    # Clients - bekommt weiterhin genau die JSON-Antwort von vorher, ohne
    # diesen Header.
    if request.headers.get("HX-Request") == "true":
        response.headers["HX-Redirect"] = f"/einreichung/{sub_id}"

    return {"submission_id": str(sub_id), "status": "PENDING"}


@app.get("/submission/{sub_id}")
def get_submission_status(sub_id: str, user=Depends(get_current_user)):
    # Dieselbe 404 wie unten bei der fehlenden Einreichung, aus demselben
    # Grund wie an /tasks/{task_id} (#259).
    if not ObjectId.is_valid(sub_id):
        raise HTTPException(status_code=404, detail="Submission nicht gefunden")
    # user_id im Filter statt einer eigenen Prüfung nach dem Lesen. Fremde
    # und fehlende Einreichungen antworten so mit derselben 404, ein 403
    # würde die Existenz einer fremden Einreichung bestätigen (#76).
    sub = db.submissions.find_one({"_id": ObjectId(sub_id), "user_id": user.get("sub")})
    if not sub:
        raise HTTPException(status_code=404, detail="Submission nicht gefunden")
    return parse_json(sub)


def _einreichungen_liste(filter_query):
    """Gemeinsame Abfrage für /einreichungen und /admin/einreichungen: nur
    der Filter unterscheidet eigene von allen Einreichungen, Nachschlage der
    Aufgabentitel und Ansicht sind identisch. PyMongoError wird nicht hier,
    sondern von den Routen gefangen, die je ihren eigenen Aufrufort kennen.
    """
    submissions = list(
        db.submissions.find(filter_query, {"code": 0, "test_results": 0}).sort(
            "created_at", -1
        )
    )
    # Seit #80 lässt /submit nur noch task_id im ObjectId-Format durch,
    # Einreichungen aus der Zeit davor können aber jeden Wert tragen.
    # is_valid statt ObjectId(...) im Try, damit eine kaputte ID hier nicht
    # die ganze Liste mit 500 abbricht, sondern nur als "Aufgabe gelöscht"
    # erscheint.
    aufgaben_titel = {
        str(t["_id"]): t["title"]
        for t in db.tasks.find(
            {
                "_id": {
                    "$in": [
                        ObjectId(s["task_id"])
                        for s in submissions
                        if ObjectId.is_valid(s["task_id"])
                    ]
                }
            },
            {"title": 1},
        )
    }
    return [_einreichung_ansicht(s, aufgaben_titel) for s in submissions]


@app.get("/einreichungen", response_class=HTMLResponse)
def einreichungen_seite(request: Request, user=Depends(get_current_user)):
    try:
        submissions = _einreichungen_liste({"user_id": user.get("sub")})
    except PyMongoError as fehler:
        return _dienst_nicht_erreichbar(request, fehler, "/einreichungen")
    return templates.TemplateResponse(
        request,
        "einreichungen.html",
        {
            "submissions": submissions,
            "user": user,
            "ist_admin": _ist_admin(user),
            "admin_ansicht": False,
        },
    )


def _kein_zugriff(
    request, knopf_href="/einreichungen", knopf_text="Zu meinen Einreichungen"
):
    """403 für die drei Verwaltung-Routen (#240), dieselbe Fehlerseite wie
    überall sonst. Keine eigene Depends-Abhängigkeit dafür: es gibt heute nur
    diese drei Routen, eine Abstraktion für einen einzigen Aufrufer lohnt
    sich erst, wenn eine vierte dazukommt.
    """
    return _fehlerseite(
        request,
        403,
        "Kein Zugriff",
        "Diese Seite ist nur für Konten mit der Rolle dozent sichtbar.",
        knopf_text=knopf_text,
        knopf_href=knopf_href,
    )


def _heute_utc_start():
    jetzt = datetime.now(timezone.utc)
    return jetzt.replace(hour=0, minute=0, second=0, microsecond=0)


@app.get("/verwaltung", response_class=HTMLResponse)
def verwaltung_seite(request: Request, user=Depends(get_current_user)):
    if not _ist_admin(user):
        return _kein_zugriff(request)
    try:
        warteschlange = sum(redis_client.llen(f"judge:{s}") for s in AKTIVE_SPRACHEN)
    except redis.RedisError as fehler:
        return _dienst_nicht_erreichbar(request, fehler, "/verwaltung")
    try:
        aufgaben = list(db.tasks.find({}))
        laeuft = len(list(db.submissions.find({"status": "RUNNING"})))
        # Nur die drei Felder, die unten gebraucht werden - eine Aufgabe kann
        # hunderte Einreichungen mit Code und Testergebnissen haben, die hier
        # nicht mitreisen müssen.
        alle_einreichungen = list(
            db.submissions.find({}, {"task_id": 1, "status": 1, "created_at": 1})
        )
    except PyMongoError as fehler:
        return _dienst_nicht_erreichbar(request, fehler, "/verwaltung")

    heute_start = _heute_utc_start()
    heute = sum(
        1
        for s in alle_einreichungen
        if s["created_at"].replace(tzinfo=timezone.utc) >= heute_start
    )

    # Gruppierung in Python statt einer $group-Aggregation: die Zahlen je
    # Aufgabe kommen aus alle_einreichungen, das schon vollständig geladen
    # ist. Eine zweite Mongo-Abfrage je Aufgabe wäre bei wenigen Aufgaben
    # und mäßig vielen Einreichungen kein Gewinn, nur mehr Rundreisen.
    aufgaben_ansicht = []
    for t in aufgaben:
        tid = str(t["_id"])
        eigene = [s for s in alle_einreichungen if s["task_id"] == tid]
        bestanden = sum(1 for s in eigene if s["status"] == "SUCCESS")
        letzte = max((s["created_at"] for s in eigene), default=None)
        aufgaben_ansicht.append(
            {
                "id": tid,
                "title": t["title"],
                "difficulty": t.get("difficulty"),
                "test_case_count": len(t.get("test_cases", [])),
                "anzahl": len(eigene),
                "bestanden": bestanden,
                "bestanden_prozent": (
                    round(100 * bestanden / len(eigene)) if eigene else None
                ),
                "letzte_einreichung": _relative_zeit(letzte) if letzte else None,
            }
        )

    return templates.TemplateResponse(
        request,
        "verwaltung.html",
        {
            "user": user,
            "ist_admin": True,
            "warteschlange": warteschlange,
            "laeuft": laeuft,
            "heute": heute,
            "aufgaben": aufgaben_ansicht,
        },
    )


# Formularfelder von verwaltung-aufgabe-neu.html, dieselben Regeln wie
# app/aufgaben/laden.py (gelesen): eigene Konstanten hier statt eines
# Imports, laden.py liegt im Worker-Image, main.py im Backend-Image, ein
# Import über die Image-Grenze böte nur eine Kopplung zur Build-Zeit.
SCHWIERIGKEITEN = ("leicht", "mittel", "schwer")
AUFGABE_GRENZEN = {"time_limit_seconds": 60, "memory_limit_mb": 256}

# Feste Zahl an Testfall-Blöcken im Formular statt eines dynamischen
# "+ Weiterer Testfall" ohne JavaScript (kein HTMX hier, siehe #240-Vorgabe
# "nur die vereinbarte Kernfunktion"). Eine leere Zeile wird beim Speichern
# übersprungen, keine Fehlermeldung. Mehr als fünf Testfälle sind ein
# Folgeschritt, kein Blocker für die Kernfunktion.
TESTFALL_BLOECKE = 5


class AufgabeUngueltig(Exception):
    """Ein Formularfehler beim Anlegen einer Aufgabe, keine Ausnahme aus der
    Datenbank. verwaltung_aufgabe_neu_seite fängt genau diese und zeigt die
    Meldung im Formular, alles andere bleibt ein unbehandelter 500.
    """


def _aufgabe_aus_formular(form):
    titel = (form.get("titel") or "").strip()
    if not titel:
        raise AufgabeUngueltig("Titel darf nicht leer sein.")

    schwierigkeit = form.get("schwierigkeit")
    if schwierigkeit not in SCHWIERIGKEITEN:
        raise AufgabeUngueltig(f"Schwierigkeit muss {', '.join(SCHWIERIGKEITEN)} sein.")

    beschreibung = (form.get("beschreibung") or "").strip()
    if not beschreibung:
        raise AufgabeUngueltig("Beschreibung darf nicht leer sein.")

    aufgabe = {"title": titel, "description": beschreibung, "difficulty": schwierigkeit}

    for feld, hoechstens in AUFGABE_GRENZEN.items():
        roh = (form.get(feld) or "").strip()
        if not roh:
            continue
        try:
            wert = int(roh)
        except ValueError:
            raise AufgabeUngueltig(f"{feld} muss eine ganze Zahl sein.") from None
        if wert <= 0 or wert > hoechstens:
            raise AufgabeUngueltig(f"{feld} muss zwischen 1 und {hoechstens} liegen.")
        aufgabe[feld] = wert

    test_cases = []
    for i in range(1, TESTFALL_BLOECKE + 1):
        name = (form.get(f"tc{i}_name") or "").strip()
        eingabe = form.get(f"tc{i}_eingabe") or ""
        erwartet = (form.get(f"tc{i}_erwartet") or "").strip()
        # Ganz leerer Block: nicht benutzt, kein Fehler. Teilweise gefüllt:
        # vermutlich ein vergessenes Feld, das soll aussagen, nicht als
        # stiller Fall zwei Testfälle weiter auftauchen.
        if not name and not eingabe.strip() and not erwartet:
            continue
        if not name or not erwartet:
            raise AufgabeUngueltig(
                f"Testfall {i}: Name und erwartete Ausgabe dürfen nicht leer sein."
            )
        test_cases.append({"name": name, "input": eingabe, "expected_output": erwartet})

    if not test_cases:
        raise AufgabeUngueltig("Mindestens ein Testfall wird gebraucht.")
    aufgabe["test_cases"] = test_cases
    return aufgabe


@app.get("/verwaltung/aufgabe-neu", response_class=HTMLResponse)
def verwaltung_aufgabe_neu_seite(request: Request, user=Depends(get_current_user)):
    if not _ist_admin(user):
        return _kein_zugriff(
            request, knopf_href="/verwaltung", knopf_text="Zur Verwaltung"
        )
    return templates.TemplateResponse(
        request,
        "verwaltung-aufgabe-neu.html",
        {
            "user": user,
            "ist_admin": True,
            "schwierigkeiten": SCHWIERIGKEITEN,
            "testfall_bloecke": TESTFALL_BLOECKE,
            "fehler": None,
            "werte": {},
        },
    )


@app.post("/verwaltung/aufgabe-neu", response_class=HTMLResponse)
async def verwaltung_aufgabe_anlegen(request: Request, user=Depends(get_current_user)):
    if not _ist_admin(user):
        return _kein_zugriff(
            request, knopf_href="/verwaltung", knopf_text="Zur Verwaltung"
        )
    form = await request.form()
    try:
        aufgabe = _aufgabe_aus_formular(form)
    except AufgabeUngueltig as fehler:
        # Werte aus dem Formular zurückgeben, damit ein Tippfehler nicht die
        # ganze Eingabe kostet - dieselbe Vorlage, nur mit fehler gesetzt.
        return templates.TemplateResponse(
            request,
            "verwaltung-aufgabe-neu.html",
            {
                "user": user,
                "ist_admin": True,
                "schwierigkeiten": SCHWIERIGKEITEN,
                "testfall_bloecke": TESTFALL_BLOECKE,
                "fehler": str(fehler),
                "werte": form,
            },
            status_code=400,
        )
    try:
        # insert_one statt des upsert aus laden.py: Ein zweiter Seed-Lauf mit
        # demselben Titel überschreibt diese Aufgabe später trotzdem (siehe
        # der Hinweis dazu im Entwurf, verwaltung-aufgabe-neu.html) - das ist
        # ein offener Punkt aus #71 und kein neues Verhalten dieser Route.
        eingefuegt = db.tasks.insert_one(aufgabe)
    except PyMongoError as fehler:
        return _dienst_nicht_erreichbar(request, fehler, "/verwaltung/aufgabe-neu")
    return RedirectResponse(url=f"/aufgabe/{eingefuegt.inserted_id}", status_code=303)


# Bildet die Ergebnis-Auswahl aus verwaltung-einreichungen.html auf
# status_klasse ab (main.py, _einreichung_ansicht/STATUS_KLASSE). Die Maske
# zeigt Klartext, keine internen Statuswerte wie PENDING oder UNRESOLVED.
ERGEBNIS_FILTER = {
    "bestanden": "ok",
    "fehlgeschlagen": "fehler",
    "wird ausgeführt": "laeuft",
    "in der Warteschlange": "wartet",
}


@app.get("/verwaltung/einreichungen", response_class=HTMLResponse)
def verwaltung_einreichungen_seite(
    request: Request,
    user=Depends(get_current_user),
    aufgabe: str = "",
    ergebnis: str = "",
    person: str = "",
):
    """Alle Einreichungen aller Nutzer, mit Filtern (#240).

    Aufgabe und Ergebnis filtert dieselbe Abfrage/Ansicht wie /einreichungen
    (_einreichungen_liste), nur ohne den user_id-Filter. Ergebnis und Person
    filtern danach in Python: Ergebnis ist eine abgeleitete Kategorie
    (status_klasse), keine gespeicherte, und Person soll Kennung wie
    Anzeigename treffen, beides mit einer Teilzeichenkette wie im Entwurf -
    beides bräuchte sonst eigene Mongo-Operatoren für zwei Felder gleichzeitig.
    """
    if not _ist_admin(user):
        return _kein_zugriff(request)
    try:
        aufgaben = list(db.tasks.find({}, {"title": 1}))
        filter_query = {"task_id": aufgabe} if aufgabe else {}
        submissions = _einreichungen_liste(filter_query)
    except PyMongoError as fehler:
        return _dienst_nicht_erreichbar(request, fehler, "/verwaltung/einreichungen")

    if ergebnis in ERGEBNIS_FILTER:
        klasse = ERGEBNIS_FILTER[ergebnis]
        submissions = [s for s in submissions if s["status_klasse"] == klasse]
    if person.strip():
        gesucht = person.strip().lower()
        submissions = [
            s
            for s in submissions
            if gesucht in s["username"].lower() or gesucht in s["user_id"].lower()
        ]

    return templates.TemplateResponse(
        request,
        "verwaltung-einreichungen.html",
        {
            "user": user,
            "ist_admin": True,
            "submissions": submissions,
            "aufgaben": [parse_json(t) for t in aufgaben],
            "filter_aufgabe": aufgabe,
            "filter_ergebnis": ergebnis,
            "filter_person": person,
        },
    )


@app.get("/einreichung/{sub_id}", response_class=HTMLResponse)
def einreichung_seite(sub_id: str, request: Request, user=Depends(get_current_user)):
    """Ergebnis einer einzelnen Einreichung, laufend oder fertig.

    Eine Route für beide Zustände statt zweier, wie schon /einreichungen die
    leere Liste mitträgt: dieselbe Einreichung, nur zu unterschiedlichem
    Zeitpunkt abgefragt, keine zwei verschiedenen Ressourcen.

    Dieselbe Abfrage wie /submission/{sub_id}, samt user_id im Filter (#76),
    aber anders als der JSON-Endpunkt fängt diese Seite eine kaputte ObjectId
    (aus einem verstümmelten Link) ab und zeigt die 404-Seite statt eines
    500. Eine Seite für Menschen darf das, der JSON-Endpunkt bleibt
    unverändert.

    Für die Rolle dozent (#240) fällt der user_id-Filter weg: die Verwaltung
    verlinkt hierher aus verwaltung-einreichungen.html auf fremde
    Einreichungen, dieselbe 404-Prüfung wie #76 träfe dort jeden Klick. Der
    JSON-Endpunkt /submission/{sub_id} bleibt unverändert, dafür gibt es
    noch keine Verwaltungsansicht.
    """

    def einreichung_404():
        return _fehlerseite(
            request,
            404,
            "Diese Einreichung gibt es nicht",
            "Die Einreichung wurde nicht gefunden. Möglicherweise wurde sie "
            "entfernt oder der Link ist nicht mehr gültig.",
            knopf_text="Zu meinen Einreichungen",
            knopf_href="/einreichungen",
        )

    try:
        filter_query = {"_id": ObjectId(sub_id)}
        if not _ist_admin(user):
            filter_query["user_id"] = user.get("sub")
        submission = db.submissions.find_one(filter_query)
    except InvalidId:
        return einreichung_404()
    except PyMongoError as fehler:
        return _dienst_nicht_erreichbar(request, fehler, f"/einreichung/{sub_id}")
    if not submission:
        return einreichung_404()

    try:
        aufgabe = (
            db.tasks.find_one({"_id": ObjectId(submission["task_id"])})
            if ObjectId.is_valid(submission["task_id"])
            else None
        )
    except PyMongoError as fehler:
        return _dienst_nicht_erreichbar(request, fehler, f"/einreichung/{sub_id}")

    # Namen nur für Beispiele, die die Aufgabenseite ohnehin zeigt. Ein
    # verborgener Fall heißt auf der Seite "Testfall N": Namen wie "Nur der
    # Startwert 1" nennen sonst die Eingabe, die das Urteil seit #208 nicht
    # mehr verrät. Der Vergleich mit True wie an den Beispielen in
    # aufgabe_seite.
    testfall_namen = {
        nummer: fall["name"]
        for nummer, fall in enumerate((aufgabe or {}).get("test_cases", []), 1)
        if isinstance(fall, dict) and fall.get("name") and fall.get("sample") is True
    }
    kontext = {
        "user": user,
        "ist_admin": _ist_admin(user),
        "sub_id": str(submission["_id"]),
        "task_titel": aufgabe["title"] if aufgabe else "Aufgabe gelöscht",
        "sprache": submission["sprache"],
        "testfaelle": _testfaelle_ansicht(
            submission.get("test_results"), testfall_namen
        ),
    }

    if submission["status"] in ("PENDING", "RUNNING"):
        kontext.update(
            {
                "status": submission["status"],
                "eingereicht": _relative_zeit(submission["created_at"]),
                "worker_id": submission.get("worker_id"),
                "erledigt": sum(1 for t in kontext["testfaelle"] if not t["offen"]),
                "gesamt": len(kontext["testfaelle"]),
                # versuche > 1: ein früherer Versuch hat die Einreichung nicht
                # zu Ende gebracht (Frist gerissen, durchlauf.py hat sie
                # requeued), ein Worker übernahm sie erneut. Kein Valkey-
                # Stream mehr seit #82, die Queue trägt nur die ID, versuche
                # zählt die Übernahmen (worker.py, _uebernehmen).
                "wiederaufnahme": submission.get("versuche", 0) > 1,
            }
        )
        return templates.TemplateResponse(request, "ergebnis-laeuft.html", kontext)

    test_results = submission.get("test_results") or []
    gesamtlaufzeit_ms = sum(
        t["zeit_ms"] for t in test_results if t.get("zeit_ms") is not None
    )
    kontext.update(
        {
            "status": submission["status"],
            # durchlauf.py schreibt bei UNRESOLVED (Versuche ausgeschöpft)
            # kein result, sonst stünde hier wörtlich "None" auf der Seite.
            # Derselbe Rückgriff auf STATUS_TEXT wie in _einreichung_ansicht.
            "result": submission.get("result")
            or STATUS_TEXT.get(submission["status"], submission["status"]),
            "bestanden": sum(1 for t in test_results if t.get("verdict") == "AC"),
            "gesamt": len(test_results),
            "eingereicht": submission["created_at"]
            .replace(tzinfo=timezone.utc)
            .astimezone(BERLIN_TZ)
            .strftime("%d.%m.%Y, %H:%M"),
            "gesamtlaufzeit_s": gesamtlaufzeit_ms / 1000 if gesamtlaufzeit_ms else None,
        }
    )
    return templates.TemplateResponse(request, "ergebnis.html", kontext)
