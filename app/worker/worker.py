import json
import os
import pathlib
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import redis
from bson import ObjectId
from pymongo import MongoClient

# Grenzen je Lauf. Sie stehen hier und nicht nur am Pod, weil ein Pod-Limit für
# alle Läufe zusammen gilt: Eine speicherhungrige Einreichung würde sonst den
# Worker mit in den OOM-Kill nehmen, samt der Einreichung, die er gerade
# bearbeitet.
#
# Zeit und Speicher sind Vorgaben: Eine Aufgabe kann beides über
# time_limit_seconds und memory_limit_mb überschreiben, denn ein Limit, das für
# die aufwendigste Aufgabe passt, trennt bei allen anderen kaum noch zwischen
# einer guten und einer schlechten Lösung.
SANDBOX_TIMEOUT = 5  # vergangene Zeit, fängt auch wartende Prozesse
SANDBOX_SPEICHER_MB = 128

# Fest je Lauf, von keiner Aufgabe zu ändern: Diese Grenzen fangen
# Fehlverhalten, keinen Bedarf, der mit der Aufgabe wächst.
SANDBOX_AUSGABE_BYTES = 1024**2  # RLIMIT_FSIZE, begrenzt stdout und stderr
SANDBOX_PROZESSE = 64  # RLIMIT_NPROC gegen Fork-Bomben

MELDUNG_MAX = 2000  # so viel Fehlerausgabe übernimmt das Feld result der Einreichung
REST_FRIST = 1.0  # Sekunden, die das Aufräumen auf beendete Prozesse wartet
QUEUE_PAUSE = 1.0  # Sekunden bis zum nächsten Versuch, wenn Valkey nicht antwortet

# Obergrenzen für das, was eine Aufgabe fordern darf. Ohne sie könnte eine
# Aufgabe das Zeitlimit praktisch abschalten oder mehr Speicher erlauben, als der
# Container insgesamt hat, und damit statt der Einreichung den Worker in den
# OOM-Kill treiben. Dieselben Werte stehen in app/aufgaben/laden.py, dort fallen
# sie schon beim Laden auf.
GRENZE_ZEIT_MAX = 60
GRENZE_SPEICHER_MAX_MB = 256

# Unter diesem User läuft der eingereichte Code. Er wird im Dockerfile
# angelegt und hat kein Leserecht auf /app.
SANDBOX_USER = os.getenv("SANDBOX_USER", "sandbox")

# Der Wechsel des Users braucht root im Worker. Ohne ihn würde fremder Code
# unter demselben User laufen wie der Worker: Er könnte /app samt worker.py lesen,
# den Worker per Signal beenden und über prlimit seine eigenen Grenzen wieder
# anheben.
# Der Judge läuft deshalb nicht stillschweigend ohne diese Trennung weiter,
# sondern startet gar nicht erst.
TRENNUNG_ERZWINGEN = os.getenv("SANDBOX_TRENNUNG_ERZWINGEN", "1") != "0"

mongo_client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = mongo_client["coding_platform"]
redis_client = redis.Redis.from_url(os.getenv("REDIS_URI", "redis://localhost:6379"))


def _sandbox_uid():
    """Gibt die UID zurück, unter der der eingereichte Code laufen soll."""
    if os.geteuid() != 0:
        if TRENNUNG_ERZWINGEN:
            raise SystemExit(
                f"Der Worker läuft nicht als root und kann den eingereichten Code "
                f"deshalb nicht unter dem User {SANDBOX_USER} ausführen. "
                f"Ohne diese Trennung liest fremder Code die Zugangsdaten des "
                f"Workers. Zum Übergehen SANDBOX_TRENNUNG_ERZWINGEN=0 setzen, "
                f"aber dann nur mit einer anderen Isolation davor."
            )
        return None
    try:
        uid = pwd.getpwnam(SANDBOX_USER).pw_uid
    except KeyError:
        raise SystemExit(
            f"Den User {SANDBOX_USER} gibt es im Image nicht. Er wird in "
            f"app/worker/Dockerfile angelegt."
        ) from None
    # Eine UID, die auf den Worker selbst zeigt, hebt die Trennung nicht nur
    # auf: Das Aufräumen nach dem Lauf beendet dann alle Prozesse mit dieser UID
    # und damit den Worker.
    if uid in (0, os.geteuid()):
        raise SystemExit(
            f"{SANDBOX_USER} hat die UID {uid} und damit die des Workers. Der "
            f"eingereichte Code braucht eine eigene."
        )
    return uid


SANDBOX_UID = _sandbox_uid()

# Basisverzeichnis, unter dem der Worker für jeden Lauf ein eigenes working
# directory anlegt. Es gehört dem Worker und die Läufe liegen so nicht direkt
# in /tmp: Dort darf jeder schreiben, die Einreichung könnte ihr eigenes
# Verzeichnis also umbenennen, und das Aufräumen danach würde nur noch den
# alten Pfad finden und alles liegen lassen.
SANDBOX_BASIS = pathlib.Path(tempfile.gettempdir()) / "judge"
SANDBOX_BASIS.mkdir(mode=0o755, exist_ok=True)


# Kappt dem eingereichten Code das Netz, so wie es zuvor der eigene
# Sandbox-Container über network_mode none tat. Der direkte Weg wäre ein
# leerer Netz-Namespace, doch den darf nur anlegen, wer CAP_SYS_ADMIN hat,
# und die soll der Worker nicht bekommen. Der Umweg: Erst legt sich der
# Prozess einen User-Namespace an, das darf jeder, und innerhalb davon hat
# er die vollen Rechte, also auch die, sich den leeren Netz-Namespace
# anzulegen. Das uid_map trägt seine UID wieder ein. Für den Dateizugriff
# wäre das nicht nötig, den prüft der Kernel weiter gegen die alte UID. Aber
# ohne den Eintrag hätte der Prozess im neuen Namespace keine darstellbare
# UID mehr: Wer die eigene UID abfragt, würde den Platzhalter-User nobody
# bekommen.
#
# Dockers seccomp-Profil blockiert CLONE_NEWUSER, im Compose-Stand geht das
# deshalb nicht. containerd im Cluster setzt kein solches Profil, dort greift es.
NETZ_BLOCK = (
    "u, g = os.getuid(), os.getgid()\n"
    "os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET)\n"
    "open('/proc/self/setgroups', 'w').write('deny')\n"
    "open('/proc/self/uid_map', 'w').write(f'{u} {u} 1')\n"
    "open('/proc/self/gid_map', 'w').write(f'{g} {g} 1')\n"
)

# Im Cluster gehört SANDBOX_NETZ_ERZWINGEN=1 ins Deployment. Dort funktioniert
# die Trennung, und sie soll nicht unbemerkt wegfallen, wenn jemand später ein
# seccomp-Profil setzt.
NETZ_ERZWINGEN = os.getenv("SANDBOX_NETZ_ERZWINGEN", "0") != "0"


def _netz_trennung_moeglich():
    """Prüft einmal beim Start, ob es möglich ist, der Sandbox ein leeres Netz zu geben."""
    if SANDBOX_UID is None:
        return False
    fertig = subprocess.run(
        [sys.executable, "-c", "import os\n" + NETZ_BLOCK],
        capture_output=True,
        user=SANDBOX_UID,
        group=SANDBOX_USER,
        extra_groups=[],
    )
    return fertig.returncode == 0


NETZ_TRENNUNG = _netz_trennung_moeglich()
if NETZ_ERZWINGEN and not NETZ_TRENNUNG:
    raise SystemExit(
        "SANDBOX_NETZ_ERZWINGEN ist gesetzt, aber der eingereichte Code bekommt "
        "kein eigenes Netz. Meist blockiert ein seccomp-Profil den Aufruf von "
        "unshare mit CLONE_NEWUSER."
    )


# Baut den Starter: ein kleines Python-Programm, das zuerst die eigenen
# Grenzen setzt und sich dann per exec durch die Lösung ersetzt. subprocess
# bietet für solche Vorarbeit eigentlich preexec_fn an, das scheidet hier aber
# aus: preexec_fn läuft zwischen fork und exec, und weil pymongo im
# Hintergrund Threads betreibt, kann der Kindprozess dabei eine Sperre erben,
# die nie mehr freigegeben wird. Er würde dann für immer vor dem exec hängen. Die
# subprocess-Dokumentation warnt genau vor dieser Kombination.
# Nach dem exec bleiben die Grenzen bestehen. Anheben kann der eingereichte
# Code höchstens das CPU-Soft-Limit, um die eine Sekunde bis zum Hard-Limit,
# alle anderen Grenzen liegen fest.
def _starter(zeit, speicher_bytes):
    # RLIMIT_CPU zählt nur Sekunden, in denen der Prozess rechnet. Wartezeit
    # zählt das timeout beim communicate, beide bekommen dieselbe Zahl:
    # RLIMIT_CPU fängt Programme, die durchgehend rechnen, das timeout
    # zusätzlich solche, die warten.
    #
    # Das Netz zuerst, danach die Grenzen: Der Namespace braucht selbst Speicher,
    # und ein enges RLIMIT_AS würde ihn scheitern lassen.
    return (
        "import os, resource, sys\n"
        + (NETZ_BLOCK if NETZ_TRENNUNG else "")
        + f"resource.setrlimit(resource.RLIMIT_CPU, ({zeit}, {zeit + 1}))\n"
        + f"resource.setrlimit(resource.RLIMIT_AS, ({speicher_bytes},"
        + f" {speicher_bytes}))\n"
        + f"resource.setrlimit(resource.RLIMIT_FSIZE, ({SANDBOX_AUSGABE_BYTES},"
        + f" {SANDBOX_AUSGABE_BYTES}))\n"
        + f"resource.setrlimit(resource.RLIMIT_NPROC, ({SANDBOX_PROZESSE},"
        + f" {SANDBOX_PROZESSE}))\n"
        + "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
        + 'os.execv(sys.executable, [sys.executable, "loesung.py"])\n'
    )


def grenzen_der_aufgabe(task):
    """Liest Zeit und Speicher aus der Aufgabe, mit den Vorgaben als Fallback.

    Fehlerhafte Werte werden nicht stillschweigend übergangen: Ein Limit, das
    versehentlich als Zeichenkette oder als Null in der Aufgabe steht, würde den
    Judge sonst für alle Einreichungen dieser Aufgabe falsch urteilen lassen.
    """
    zeit = task.get("time_limit_seconds", SANDBOX_TIMEOUT)
    speicher = task.get("memory_limit_mb", SANDBOX_SPEICHER_MB)
    for name, wert, hoechstens in (
        ("time_limit_seconds", zeit, GRENZE_ZEIT_MAX),
        ("memory_limit_mb", speicher, GRENZE_SPEICHER_MAX_MB),
    ):
        if not isinstance(wert, int) or isinstance(wert, bool) or wert <= 0:
            raise ValueError(f"{name} muss eine positive ganze Zahl sein, ist {wert!r}")
        if wert > hoechstens:
            raise ValueError(f"{name} darf höchstens {hoechstens} sein, ist {wert}")
    return zeit, speicher * 1024**2


def _sandbox_pids():
    """Alle Prozesse mit der UID der Sandbox, Zombies eingeschlossen."""
    treffer = []
    for eintrag in pathlib.Path("/proc").glob("[0-9]*"):
        try:
            zeilen = (eintrag / "status").read_text().splitlines()
            uid = next(z for z in zeilen if z.startswith("Uid:")).split()[1]
            if int(uid) == SANDBOX_UID:
                treffer.append(int(eintrag.name))
        except (OSError, ValueError, StopIteration):
            continue
    return treffer


def _reste_beenden():
    """Beendet, was ein Lauf hinterlassen hat.

    Startet die Einreichung selbst einen Prozess, kann der per setsid die
    Prozessgruppe verlassen und überlebt dann den Kill auf die Gruppe. Er läuft
    unter der UID der Sandbox weiter, verbraucht die CPU des Pods und belegt
    Plätze im NPROC-Kontingent. Der Worker arbeitet die Queue nacheinander ab,
    nach einem Lauf darf es also keine Prozesse mit dieser UID mehr geben.
    """
    if SANDBOX_UID is None:
        return

    # Die Schleife läuft, bis nichts mehr übrig ist. Ein einzelner Durchgang
    # würde die Prozesse verpassen, die zwischen dem Blick in /proc und dem
    # Signal noch starten. Ein abgesetzter Prozess wird nach dem Kill von PID 1
    # adoptiert, im Container also vom Worker selbst, und muss von ihm
    # eingesammelt werden. Sonst bleibt er als Zombie stehen und belegt einen
    # Platz im NPROC-Kontingent.
    frist = time.monotonic() + REST_FRIST
    while True:
        for pid in _sandbox_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                continue

        # Auch das Einsammeln ist gedeckelt. Ein übersehener Prozess, der
        # fortlaufend neue Prozesse erzeugt und sterben lässt, würde die Schleife
        # sonst endlos mit Arbeit versorgen und der Worker würde nie zur Frist
        # kommen.
        for _ in range(SANDBOX_PROZESSE * 2):
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break

        offen = _sandbox_pids()
        if not offen:
            return
        if time.monotonic() > frist:
            print(f"Prozesse der Sandbox blieben stehen: {offen}")
            return
        time.sleep(0.02)


def _urteil_nach_signal(signalnummer, zeit):
    if signalnummer == signal.SIGXCPU:
        return f"TIMEOUT: Rechenzeit von {zeit} Sekunden überschritten"
    if signalnummer == signal.SIGXFSZ:
        return f"OUTPUT_LIMIT: mehr als {SANDBOX_AUSGABE_BYTES // 1024} KiB ausgegeben"
    # Nicht pauschal als Zeitüberschreitung: Eine Einreichung kann sich selbst
    # per SIGKILL beenden, und der OOM-Killer trifft ebenso. Dass ein Kill vom
    # Zeitlimit kam, weiß nur der Pfad, der ihn selbst gesendet hat.
    # signal.Signals kennt die Echtzeitsignale oberhalb von SIGRTMIN nicht und
    # wirft dafür ValueError. Ungefangen würde der als Umgebungsfehler
    # herauskommen,
    # und eine Einreichung könnte ihr eigenes Ende damit zu einem Fehler des
    # Judges umdeuten.
    try:
        name = signal.Signals(signalnummer).name
    except ValueError:
        name = f"Signal {signalnummer}"
    return f"EXECUTION_ERROR: durch {name} beendet"


def _gelesen(fd, grenze):
    """Liest eine Ausgabedatei über ihren file descriptor, nicht über den Pfad.

    Das working directory gehört dem User der Sandbox, die Einreichung kann eine
    Ausgabedatei also löschen und durch einen Symlink oder ein FIFO ersetzen. Ein
    Zugriff über den Pfad würde danach als Worker in eine fremde Datei laufen
    oder dauerhaft blockieren. Der file descriptor zeigt weiter auf die Datei, die
    beim Start geöffnet wurde.
    """
    os.lseek(fd, 0, os.SEEK_SET)
    roh = os.read(fd, grenze)
    return roh.decode("utf-8", errors="replace")


class Umgebungsfehler(Exception):
    """Der Lauf ist an der Umgebung gescheitert, nicht am eingereichten Code.

    Ohne eigene Exception kommen diese Fälle als Zeichenkette zurück und laufen
    in denselben Vergleich mit der erwarteten Ausgabe wie ein echtes Ergebnis.
    Die Einreichung würde dann FAILED für Code bekommen, der nie gelaufen ist.
    """


def run_code_in_sandbox(code: str, test_input: str, zeit: int, speicher: int) -> str:
    """Führt den eingereichten Code als Subprozess mit den Grenzen der Aufgabe aus."""
    verzeichnis = None
    deskriptoren = []
    try:
        verzeichnis = tempfile.mkdtemp(prefix="work-", dir=SANDBOX_BASIS)
        pfad = pathlib.Path(verzeichnis)
        (pfad / "loesung.py").write_text(code, encoding="utf-8")

        # stdout und stderr in Dateien statt in Pipes. Nur so greift RLIMIT_FSIZE
        # gegen endlose Ausgabe. Eine Pipe würde stattdessen den Speicher des
        # Workers füllen, der sie ausliest, und zwar noch vor dem Zeitlimit.
        # Die Dateien gehören dem Worker und sind für die Sandbox nicht zu
        # öffnen, geschrieben wird ausschließlich über den vererbten
        # file descriptor.
        for name in ("ausgabe.txt", "fehler.txt"):
            deskriptoren.append(
                os.open(str(pfad / name), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            )
        aus_fd, fehler_fd = deskriptoren

        if SANDBOX_UID is not None:
            # Nur das working directory und die Lösung wechseln den Besitzer, die
            # Ebene darüber bleibt beim Worker.
            for ziel in (pfad, pfad / "loesung.py"):
                os.chown(ziel, SANDBOX_UID, -1)
            pfad.chmod(0o700)

        prozess = subprocess.Popen(
            [sys.executable, "-c", _starter(zeit, speicher)],
            cwd=verzeichnis,
            stdin=subprocess.PIPE,
            stdout=aus_fd,
            stderr=fehler_fd,
            # env ohne MONGO_URI und REDIS_URI: Der Parameter ersetzt die
            # geerbte Umgebung des Workers, die Einreichung sieht nur die vier
            # Variablen hier. Den Zugriff verhindert das nicht, der Subprozess
            # teilt das Netz des Workers, aber es gibt die Adressen nicht auch
            # noch her.
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": verzeichnis,
                "PYTHONDONTWRITEBYTECODE": "1",  # sonst legt jeder Import eigener Module __pycache__ an
                "PYTHONUNBUFFERED": "1",  # sonst fehlt nach einem Kill die gepufferte Ausgabe
            },
            # group und extra_groups müssen mit. Der Parameter user setzt für
            # sich genommen nur die UID, die Gruppen würden dann die des
            # Workers, und über die Gruppe root wäre /app trotz 750 lesbar.
            user=SANDBOX_UID,
            group=SANDBOX_USER if SANDBOX_UID is not None else None,
            extra_groups=[] if SANDBOX_UID is not None else None,
            # start_new_session startet die Einreichung in einer eigenen
            # Session und damit in einer eigenen Prozessgruppe. Das killpg
            # beim Zeitlimit trifft dann auch die Prozesse, die die
            # Einreichung selbst gestartet hat.
            start_new_session=True,
        )
        try:
            prozess.communicate(input=test_input.encode("utf-8"), timeout=zeit)
        except subprocess.TimeoutExpired:
            os.killpg(prozess.pid, signal.SIGKILL)
            prozess.wait()
            return f"TIMEOUT: Zeitlimit von {zeit} Sekunden überschritten"

        if prozess.returncode < 0:
            return _urteil_nach_signal(-prozess.returncode, zeit)

        # Die Grenze meldet der Kernel je nach Zeitpunkt als Signal oder als
        # EFBIG an den schreibenden Aufruf. Im zweiten Fall endet die Einreichung
        # mit einem gewöhnlichen Traceback, und ohne diese Prüfung würde als
        # Urteil EXECUTION_ERROR statt des wahren Grundes stehen. Für stdout und stderr
        # getrennt geprüft, denn die Grenze gilt jeder Datei einzeln.
        for fd in (aus_fd, fehler_fd):
            if os.fstat(fd).st_size >= SANDBOX_AUSGABE_BYTES:
                grenze = SANDBOX_AUSGABE_BYTES // 1024
                return f"OUTPUT_LIMIT: mehr als {grenze} KiB ausgegeben"

        ausgabe = _gelesen(aus_fd, SANDBOX_AUSGABE_BYTES)
        if prozess.returncode != 0:
            # Gekürzt, weil die Meldung als Urteil in der Datenbank landet und
            # dort neben jeder Einreichung steht. Ein Traceback passt hinein.
            meldung = _gelesen(fehler_fd, MELDUNG_MAX).strip()
            return f"EXECUTION_ERROR: {meldung or ausgabe.strip()}"
        return ausgabe.strip()
    except Exception as e:
        # Der Typ bleibt in der Meldung: Umgebungsfehler sagt, dass es nicht am
        # eingereichten Code lag, nicht was gescheitert ist.
        raise Umgebungsfehler(f"{type(e).__name__}: {e}") from e
    finally:
        # Jeder Schritt einzeln abgefangen. Eine Exception aus dem Aufräumen
        # würde sonst die eigentliche Exception verdrängen und die Schritte
        # danach auslassen. Seit process_queue Fehler je Job abfängt, überlebt
        # der Worker das, und file descriptors und Verzeichnisse würden sich
        # über die Läufe hinweg ansammeln.
        try:
            _reste_beenden()
        except Exception as e:
            print(f"Reste der Sandbox nicht beendet: {type(e).__name__}: {e}")
        for fd in deskriptoren:
            try:
                os.close(fd)
            except OSError:
                pass
        if verzeichnis is not None:
            try:
                shutil.rmtree(verzeichnis)
            except Exception as e:
                print(f"{verzeichnis} nicht entfernt: {e}")


def _job_lesen(item):
    """Liest den Auftrag aus der Queue und gibt die ID der Einreichung dazu.

    Eigene Funktion, weil hier eine Grenze verläuft: Scheitert etwas in ihr,
    gibt es noch keine Einreichung, an die ein Fehler zu schreiben wäre. Alles
    danach lässt sich beschriften.
    """
    job = json.loads(item)
    sub_id = job["submission_id"]
    # Ohne diese Prüfung erzeugt ObjectId(None) eine frische ID, statt zu
    # scheitern. Das Ergebnis würde dann an ein Document gehen, das es nicht gibt.
    if not isinstance(sub_id, str):
        raise TypeError(
            f"submission_id ist {type(sub_id).__name__}, keine Zeichenkette"
        )
    return ObjectId(sub_id), job


def _urteil(job, task):
    """Führt die Testfälle der Aufgabe aus und gibt Status und Ergebnistext."""
    zeit, speicher = grenzen_der_aufgabe(task)
    for case in task.get("test_cases", []):
        ausgabe = run_code_in_sandbox(job["code"], case["input"], zeit, speicher)
        erwartet = str(case["expected_output"]).strip()
        if ausgabe != erwartet:
            return "FAILED", (
                f"Fehler bei Input '{case['input']}': "
                f"Erwartet '{erwartet}', Bekommen '{ausgabe}'"
            )
    return "SUCCESS", "Alle Tests bestanden!"


def _ergebnis_schreiben(sub_id, status, text):
    """Schreibt Status und Ergebnistext an die Einreichung.

    Eigenes try/except, weil einer der Gründe für SYSTEM_ERROR gerade die
    nicht erreichbare Datenbank ist. Ohne dieses try/except würde die
    Exception den Fehlerpfad verlassen und den Worker doch beenden. Die Einreichung
    bleibt dann auf PENDING stehen, und genau die nimmt der Durchlauf aus #82
    später wieder auf.
    """
    try:
        ergebnis = db.submissions.update_one(
            {"_id": sub_id},
            {"$set": {"status": status, "result": text, "updated_at": time.time()}},
        )
        # update_one scheitert nicht, wenn kein Document zur ID passt. Ohne
        # diese Prüfung würde process_queue die Einreichung als verarbeitet
        # melden, obwohl das Ergebnis nirgends steht.
        if ergebnis.matched_count == 0:
            print(f"Einreichung {sub_id}: kein Document getroffen")
    except Exception as e:
        print(f"Einreichung {sub_id}: nicht geschrieben, {type(e).__name__}: {e}")


def process_queue():
    if not NETZ_TRENNUNG:
        print(
            "Hinweis: der eingereichte Code teilt das Netz des Workers und "
            "erreicht MongoDB und die Queue direkt. Im Cluster greift die Trennung, "
            "lokal blockiert das seccomp-Profil von Docker den nötigen Aufruf."
        )
    print("Worker gestartet, warte auf Tasks...")
    while True:
        try:
            _, item = redis_client.blpop("code_queue")
        except Exception as e:
            # Hier gibt es keinen Job, dem etwas anzulasten wäre. Sich zu
            # beenden würde nichts bringen: Der Neustart landet an derselben Stelle,
            # solange Valkey weg ist, und der Container geht in den Backoff.
            print(f"Queue nicht erreichbar: {type(e).__name__}: {e}")
            time.sleep(QUEUE_PAUSE)
            continue

        try:
            sub_id, job = _job_lesen(item)
        except Exception as e:
            # Ohne brauchbare submission_id gibt es kein Document, an das ein
            # Status zu schreiben wäre. Bleibt das Protokoll, mit dem rohen
            # Inhalt, weil sonst niemand nachvollziehen kann, was ankam.
            # Bytes, solange decode_responses nicht gesetzt ist. Über REDIS_URI
            # lässt sich das umschalten, und ein AttributeError ausgerechnet in
            # diesem except-Zweig würde den Worker beenden.
            roh = item[:MELDUNG_MAX]
            inhalt = (
                roh.decode("utf-8", errors="replace") if isinstance(roh, bytes) else roh
            )
            print(f"Job übersprungen: {type(e).__name__}: {e}, Inhalt: {inhalt}")
            continue

        try:
            task = db.tasks.find_one({"_id": ObjectId(job["task_id"])})
            if task is None:
                # Ohne diesen Zweig würde die Schleife still weiterspringen und
                # die Einreichung dauerhaft auf PENDING stehen bleiben.
                raise Umgebungsfehler(f"Aufgabe {job['task_id']} steht nicht in tasks")
            status, text = _urteil(job, task)
        except Exception as e:
            # Was hier ankommt, ist kein Urteil über den eingereichten Code:
            # ein Fehler der Umgebung, eine unbrauchbare Aufgabe oder ein
            # Fehler im Worker selbst. Gekürzt, weil pymongo bei einem
            # Verbindungsfehler die ganze Topologie in die Meldung schreibt.
            status = "SYSTEM_ERROR"
            text = f"{type(e).__name__}: {e}"[:MELDUNG_MAX]
            print(f"Einreichung {sub_id}: {text}")

        _ergebnis_schreiben(sub_id, status, text)
        print(f"Einreichung {sub_id} verarbeitet: {status}")


if __name__ == "__main__":
    process_queue()
