import os
import pathlib
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
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


def _testfaelle_ansicht(test_results):
    """Ansicht je Testfall für ergebnis.html und ergebnis-laeuft.html.

    NOT_RUN heißt bei einer fertigen Einreichung "wegen Abbruch nie
    gelaufen" und bei einer laufenden "noch nicht dran" (worker.py, _urteil:
    Platzhalter vor dem ersten Fall, Abbruch beim ersten Fehlschlag). Beides
    zeigt dieselbe Zeile "steht aus", welcher der beiden Fälle vorliegt, sagt
    schon status_klasse der Einreichung.
    """
    ansicht = []
    for t in test_results or []:
        eintrag = {
            "nummer": t["test_id"],
            "offen": t.get("verdict") == "NOT_RUN",
            "zeit_ms": t.get("zeit_ms"),
            "speicher_mb": (
                t["speicher_kb"] / 1024 if t.get("speicher_kb") is not None else None
            ),
        }
        if not eintrag["offen"]:
            eintrag["klasse"] = "ok" if t["verdict"] == "AC" else "fehler"
            eintrag["text"] = VERDICT_TEXT.get(t["verdict"], t["verdict"])
            eintrag["zusatz"] = None if t["verdict"] == "AC" else t.get("detail")
        ansicht.append(eintrag)
    return ansicht


# Klartext für fehler.html, siehe README ("Herkunftsprüfung"/#15): MongoDB
# oder Valkey nicht erreichbar, oder eine Aufgabe/Einreichung ohne Treffer.
# Kein eigener Dienst, keine Traefik-Middleware, siehe #15 - das hier deckt
# nur ab, was die Anwendung selbst an ihren eigenen Routen bemerkt.
HTTP_STATUS_TEXT = {503: "Service Unavailable", 404: "Not Found"}


def _fehlerseite(
    request,
    status_code,
    titel,
    meldung,
    zusicherung=None,
    knopf_text="Erneut versuchen",
    knopf_href="/aufgaben",
):
    return templates.TemplateResponse(
        "fehler.html",
        {
            "request": request,
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
@app.get("/healthz")
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
    tasks = list(db.tasks.find({}, {"test_cases": 0}))  # Testcases verbergen
    return [parse_json(t) for t in tasks]


@app.get("/aufgaben", response_class=HTMLResponse)
def aufgaben_seite(request: Request, user=Depends(get_current_user)):
    try:
        tasks = list(
            db.tasks.find({}, {"test_cases": 0})
        )  # dieselbe Abfrage wie /tasks
    except PyMongoError as fehler:
        return _dienst_nicht_erreichbar(request, fehler, "/aufgaben")
    ansicht = []
    for t in tasks:
        t = parse_json(t)
        # Dieselbe Vorgabe wie auf der Detailseite (aufgabe_seite): ohne
        # eigenes Limit setzt der Worker durch, nicht ab, "–" wäre falsch.
        t["zeit_s"] = t.get("time_limit_seconds") or WORKER_STANDARD_ZEIT_S
        ansicht.append(t)
    return templates.TemplateResponse(
        "aufgaben.html",
        {"request": request, "tasks": ansicht, "user": user},
    )


@app.get("/tasks/{task_id}")
def get_task(task_id: str, user=Depends(get_current_user)):
    task = db.tasks.find_one({"_id": ObjectId(task_id)}, {"test_cases": 0})
    if not task:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")
    return parse_json(task)


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
    anzahl_testfaelle = len(
        task.pop("test_cases", [])
    )  # Inhalt bleibt verborgen, wie in /tasks
    return templates.TemplateResponse(
        "aufgabe.html",
        {
            "request": request,
            "task": parse_json(task),
            "anzahl_testfaelle": anzahl_testfaelle,
            # Was der Worker tatsächlich durchsetzt, wenn die Aufgabe selbst
            # kein Limit trägt (grenzen_der_aufgabe) - "–" wäre hier falsch,
            # betrifft derzeit summe.json.
            "zeit_s": task.get("time_limit_seconds") or WORKER_STANDARD_ZEIT_S,
            "speicher_mb": task.get("memory_limit_mb") or WORKER_STANDARD_SPEICHER_MB,
            "user": user,
        },
    )


@app.post("/submit")
def submit_code(
    payload: dict, request: Request, response: Response, user=Depends(get_current_user)
):
    task_id = payload.get("task_id")
    code = payload.get("code")
    sprache = payload.get("sprache", STANDARD_SPRACHE)
    if sprache not in AKTIVE_SPRACHEN:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Sprache nicht aktiv: {sprache}. "
                f"Aktive Sprachen: {', '.join(AKTIVE_SPRACHEN)}"
            ),
        )

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
    sub = db.submissions.find_one({"_id": ObjectId(sub_id)})
    if not sub:
        raise HTTPException(status_code=404, detail="Submission nicht gefunden")
    return parse_json(sub)


@app.get("/einreichungen", response_class=HTMLResponse)
def einreichungen_seite(request: Request, user=Depends(get_current_user)):
    try:
        submissions = list(
            db.submissions.find(
                {"user_id": user.get("sub")}, {"code": 0, "test_results": 0}
            ).sort("created_at", -1)
        )
        # /submit prüft task_id nicht gegen die Aufgaben (main.py, submit_code),
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
    except PyMongoError as fehler:
        return _dienst_nicht_erreichbar(request, fehler, "/einreichungen")
    return templates.TemplateResponse(
        "einreichungen.html",
        {
            "request": request,
            "submissions": [
                _einreichung_ansicht(s, aufgaben_titel) for s in submissions
            ],
            "user": user,
        },
    )


@app.get("/einreichung/{sub_id}", response_class=HTMLResponse)
def einreichung_seite(sub_id: str, request: Request, user=Depends(get_current_user)):
    """Ergebnis einer einzelnen Einreichung, laufend oder fertig.

    Eine Route für beide Zustände statt zweier, wie schon /einreichungen die
    leere Liste mitträgt: dieselbe Einreichung, nur zu unterschiedlichem
    Zeitpunkt abgefragt, keine zwei verschiedenen Ressourcen.

    Dieselbe Abfrage wie /submission/{sub_id}, aber anders als der JSON-
    Endpunkt fängt diese Seite eine kaputte ObjectId (aus einem verstümmelten
    Link) ab und zeigt die 404-Seite statt eines 500 - eine Seite für Menschen
    darf das, der JSON-Endpunkt bleibt unverändert. Ebenso ohne Prüfung auf
    user_id, wie /submission/{sub_id} - diese Seite zeigt nur an, was der
    bestehende JSON-Endpunkt ohnehin preisgibt.
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
        submission = db.submissions.find_one({"_id": ObjectId(sub_id)})
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

    kontext = {
        "request": request,
        "user": user,
        "sub_id": str(submission["_id"]),
        "task_titel": aufgabe["title"] if aufgabe else "Aufgabe gelöscht",
        "sprache": submission["sprache"],
        "testfaelle": _testfaelle_ansicht(submission.get("test_results")),
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
        return templates.TemplateResponse("ergebnis-laeuft.html", kontext)

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
    return templates.TemplateResponse("ergebnis.html", kontext)
