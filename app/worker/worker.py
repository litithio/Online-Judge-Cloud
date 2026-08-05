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
# Zeit und Speicher sind Vorgaben. Eine Aufgabe kann beides über die Felder
# time_limit_seconds und memory_limit_mb selbst festlegen, weil sich der Bedarf
# je Aufgabe stark unterscheidet: Ein Sieb über 15 Millionen Zahlen ist etwas
# anderes als die Summe zweier Zahlen. Ein Limit, das für die aufwendigste
# Aufgabe passt, trennt bei allen anderen kaum noch zwischen einer guten und
# einer schlechten Lösung.
SANDBOX_TIMEOUT = 5  # vergangene Zeit, fängt auch wartende Prozesse
SANDBOX_SPEICHER_MB = 128
SANDBOX_AUSGABE_BYTES = 1024**2  # RLIMIT_FSIZE, begrenzt stdout und stderr
SANDBOX_PROZESSE = 64  # RLIMIT_NPROC gegen Fork-Bomben
MELDUNG_MAX = 2000  # so viel einer Fehlerausgabe steht später am Ergebnis
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

# Der Wechsel des Users braucht root im Worker. Ohne ihn liefe fremder Code
# unter demselben User wie der Worker: Er könnte /app samt worker.py lesen,
# den Worker signalisieren und über prlimit seine eigenen Grenzen wieder anheben.
# Der Judge nimmt seine einzige Trennung deshalb nicht stillschweigend zurück,
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

# Oberhalb der working directories, im Besitz des Workers. Ohne diese Ebene
# läge ein Lauf direkt in /tmp, und weil dort jeder schreiben darf, könnte die
# Einreichung ihr eigenes Verzeichnis umbenennen. Das Aufräumen danach fände
# nur noch den alten Pfad und ließe alles liegen.
SANDBOX_BASIS = pathlib.Path(tempfile.gettempdir()) / "judge"
SANDBOX_BASIS.mkdir(mode=0o755, exist_ok=True)


# Setzt die Grenzen und übergibt dann an die Einreichung. Über -c und nicht über
# preexec_fn, weil dort Python-Code zwischen fork und exec liefe und pymongo im
# Hintergrund eigene Threads betreibt. Diese Kombination gilt laut Dokumentation
# von subprocess als unsicher, sobald Threads im Spiel sind: Erbt der Subprozess
# eine Sperre, die ein anderer Thread hält, hängt er vor dem exec.
# Nach dem exec bleiben die Grenzen bestehen, und senken kann sie der
# eingereichte Code zwar, anheben nicht.
# Der eingereichte Code soll kein Netz haben, so wie zuvor der Sandbox-Container
# über network_mode none. Ein eigener Netz-Namespace verlangt CAP_SYS_ADMIN, die
# der Worker nicht hat und nicht bekommen soll. Über einen User-Namespace geht es
# ohne: Darin hat der Prozess die Rechte, sich einen leeren Netz-Namespace
# anzulegen. Das uid_map hält seine Kennung fest, sonst wäre er darin nobody und
# käme an sein eigenes Verzeichnis nicht mehr heran.
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
    """Prüft einmal beim Start, ob der Sandbox ein leeres Netz zu geben ist."""
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


def _starter(zeit, speicher_bytes):
    # Die CPU-Zeit bekommt dieselbe Zahl wie die Wanduhr. Sie fängt Programme,
    # die durchgehend rechnen, die Wanduhr zusätzlich solche, die warten.
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
    """Liest Zeit und Speicher aus der Aufgabe, mit den Vorgaben als Rückfall.

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
        # sonst endlos mit Arbeit versorgen und der Worker käme nie zur Frist.
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
    # wirft dafür ValueError. Ungefangen käme der als Umgebungsfehler heraus,
    # und eine Einreichung könnte ihr eigenes Ende damit zu einem Fehler des
    # Judges umdeuten.
    try:
        name = signal.Signals(signalnummer).name
    except ValueError:
        name = f"Signal {signalnummer}"
    return f"EXECUTION_ERROR: durch {name} beendet"


def _gelesen(fd, grenze):
    """Liest eine Ausgabedatei über ihren Deskriptor, nicht über den Pfad.

    Das working directory gehört dem User der Sandbox, die Einreichung kann eine
    Ausgabedatei also löschen und durch einen Symlink oder ein FIFO ersetzen. Ein
    Zugriff über den Pfad liefe danach als Worker in eine fremde Datei oder
    blockierte dauerhaft. Der Deskriptor zeigt weiter auf die Datei, die beim
    Start geöffnet wurde.
    """
    os.lseek(fd, 0, os.SEEK_SET)
    roh = os.read(fd, grenze)
    return roh.decode("utf-8", errors="replace")


class Umgebungsfehler(Exception):
    """Der Lauf ist an der Umgebung gescheitert, nicht am eingereichten Code.

    Ohne eigene Ausnahme kommen diese Fälle als Zeichenkette zurück und laufen
    in denselben Vergleich mit der erwarteten Ausgabe wie ein echtes Ergebnis.
    Die Einreichung bekäme dann FAILED für Code, der nie gelaufen ist.
    """


def run_code_in_sandbox(code: str, test_input: str, zeit: int, speicher: int) -> str:
    """Führt den eingereichten Code als Subprozess mit den Grenzen der Aufgabe aus."""
    verzeichnis = None
    deskriptoren = []
    try:
        verzeichnis = tempfile.mkdtemp(prefix="work-", dir=SANDBOX_BASIS)
        pfad = pathlib.Path(verzeichnis)
        (pfad / "loesung.py").write_text(code, encoding="utf-8")

        # Beide Ströme in Dateien statt in Pipes. Nur so greift RLIMIT_FSIZE
        # gegen endlose Ausgabe. Eine Pipe würde stattdessen den Speicher des
        # Workers füllen, der sie ausliest, und zwar noch vor dem Zeitlimit.
        # Die Dateien gehören dem Worker und sind für die Sandbox nicht zu
        # öffnen, geschrieben wird ausschließlich über den vererbten Deskriptor.
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
            # Ohne MONGO_URI und REDIS_URI. Das verhindert den Zugriff nicht,
            # der Subprozess teilt das Netz des Workers, aber es gibt die
            # Adressen nicht auch noch her.
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": verzeichnis,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
            },
            # Gruppe und Zusatzgruppen müssen mit. Der Parameter user setzt für
            # sich genommen nur die UID, die Gruppen blieben dann die des
            # Workers, und über die Gruppe root wäre /app trotz 750 lesbar.
            user=SANDBOX_UID,
            group=SANDBOX_USER if SANDBOX_UID is not None else None,
            extra_groups=[] if SANDBOX_UID is not None else None,
            # Eigene Prozessgruppe, damit beim Zeitlimit auch die Prozesse
            # fallen, die die Einreichung selbst gestartet hat.
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
        # mit einem gewöhnlichen Traceback, und ohne diese Prüfung stünde als
        # Urteil EXECUTION_ERROR statt des wahren Grundes. Für beide Ströme, denn
        # die Grenze gilt jeder Datei einzeln.
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
        # Jeder Schritt einzeln gefangen. Eine Ausnahme aus dem Aufräumen
        # verdrängte sonst die eigentliche Ausnahme und ließe die Schritte
        # danach aus. Seit process_queue Fehler je Job fängt, überlebt der
        # Worker das, und Deskriptoren und Verzeichnisse sammelten sich über
        # die Läufe hinweg an.
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
    # scheitern. Das Ergebnis ginge dann an ein Document, das es nicht gibt.
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

    Eigener Fehlerfang, weil ein Teil der Gründe für SYSTEM_ERROR gerade die
    nicht erreichbare Datenbank ist. Ohne ihn verließe die Ausnahme den
    Fehlerpfad und beendete den Worker doch. Die Einreichung bleibt dann auf
    PENDING stehen, und genau die nimmt der Durchlauf aus #82 später wieder auf.
    """
    try:
        ergebnis = db.submissions.update_one(
            {"_id": sub_id},
            {"$set": {"status": status, "result": text, "updated_at": time.time()}},
        )
        # Sonst meldet die Zeile danach "verarbeitet", obwohl das Ergebnis
        # nirgends steht.
        if ergebnis.matched_count == 0:
            print(f"Einreichung {sub_id}: kein Document getroffen")
    except Exception as e:
        print(f"Einreichung {sub_id}: nicht geschrieben, {type(e).__name__}: {e}")


def process_queue():
    if not NETZ_TRENNUNG:
        print(
            "Hinweis: der eingereichte Code teilt das Netz des Workers und "
            "erreicht MongoDB und Redis direkt. Im Cluster greift die Trennung, "
            "lokal blockiert das seccomp-Profil von Docker den nötigen Aufruf."
        )
    print("Worker gestartet, warte auf Tasks...")
    while True:
        try:
            _, item = redis_client.blpop("code_queue")
        except Exception as e:
            # Hier gibt es keinen Job, dem etwas anzulasten wäre. Sich zu
            # beenden brächte nichts: Der Neustart landet an derselben Stelle,
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
            # lässt sich das umschalten, und ein AttributeError ausgerechnet im
            # Fehlerfang beendete den Worker.
            roh = item[:MELDUNG_MAX]
            inhalt = (
                roh.decode("utf-8", errors="replace") if isinstance(roh, bytes) else roh
            )
            print(f"Job übersprungen: {type(e).__name__}: {e}, Inhalt: {inhalt}")
            continue

        try:
            task = db.tasks.find_one({"_id": ObjectId(job["task_id"])})
            if task is None:
                # Ohne diesen Zweig sprang die Schleife still weiter und die
                # Einreichung blieb dauerhaft auf PENDING stehen.
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
