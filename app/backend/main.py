import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, HTTPException
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from bson import ObjectId
import redis

from auth import get_current_user

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


def parse_json(data):
    data["id"] = str(data["_id"])
    del data["_id"]
    return data


# Die beiden Endpunkte für die Probes tragen bewusst kein
# Depends(get_current_user). Das kubelet ruft den Pod direkt auf, an der
# Auth-Kette vorbei, und schickt deshalb keine Gateway-Header. Eine geschützte
# Probe schlüge dauerhaft fehl.
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
        raise HTTPException(
            status_code=503, detail=f"MongoDB nicht erreichbar: {fehler}"
        )
    return {"status": "ok"}


@app.get("/tasks")
def get_tasks(user=Depends(get_current_user)):
    tasks = list(db.tasks.find({}, {"test_cases": 0}))  # Testcases verbergen
    return [parse_json(t) for t in tasks]


@app.get("/tasks/{task_id}")
def get_task(task_id: str, user=Depends(get_current_user)):
    task = db.tasks.find_one({"_id": ObjectId(task_id)}, {"test_cases": 0})
    if not task:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")
    return parse_json(task)


@app.post("/submit")
def submit_code(payload: dict, user=Depends(get_current_user)):
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
        # tatsächlichen Übernahme (worker.py, _uebernehmen), ein Queue-Eintrag,
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

    return {"submission_id": str(sub_id), "status": "PENDING"}


@app.get("/submission/{sub_id}")
def get_submission_status(sub_id: str, user=Depends(get_current_user)):
    sub = db.submissions.find_one({"_id": ObjectId(sub_id)})
    if not sub:
        raise HTTPException(status_code=404, detail="Submission nicht gefunden")
    return parse_json(sub)
