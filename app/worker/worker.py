import os
import pathlib
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone

import redis
from bson import ObjectId
from pymongo import MongoClient, ReturnDocument

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
# Fehlverhalten, keinen Bedarf, der mit der Aufgabe wächst. Sie schützen den
# Worker und den Node, nicht die Bewertung, und bleiben deshalb Sache des
# Workers statt eines Felds an der Aufgabe. Braucht eine Aufgabe später mehr
# als 1 MiB Ausgabe, wird die Grenze hier nachgezogen, für alle Aufgaben.
SANDBOX_AUSGABE_BYTES = 1024**2  # RLIMIT_FSIZE, begrenzt stdout und stderr
# RLIMIT_NPROC gegen Fork-Bomben, Threads zählen mit. Wie viel die Zahl
# zulässt, hängt an der Laufzeit, gemessen im Cluster gegen beide. Unter runc
# zählt der Prozess der Einreichung selbst mit, mit 1 startet sie also gar
# nichts mehr. Unter runsc zählt er nicht mit, dort bleibt ihr ein weiterer
# Prozess. Der Judge läuft im Cluster unter runsc, im Compose unter runc. In
# beiden Fällen ist die Fork-Bombe damit erledigt, und die Zahl deckelt
# zugleich den Speicher, denn RLIMIT_AS gilt je Prozess.
SANDBOX_PROZESSE = 1

MELDUNG_MAX = 2000  # so viel Fehlerausgabe übernimmt das Feld result der Einreichung
REST_FRIST = 1.0  # Sekunden, die das Aufräumen auf beendete Prozesse wartet
QUEUE_PAUSE = 1.0  # Sekunden bis zum nächsten Versuch, wenn Valkey nicht antwortet

# Zeitlimit des blpop unten. Der Wert bestimmt nur, wie oft die Schleife im
# Leerlauf umläuft, aus den Grenzen der Aufgaben folgt er nicht. Er muss unter
# dem socket_timeout des Clients liegen, sonst bricht der Socket das Warten ab,
# bevor Valkey selbst antwortet. Siehe den Aufbau des Clients weiter unten.
QUEUE_WARTEN = 15

# Wohin der Worker seinen Heartbeat schreibt, siehe _heartbeat. /run und
# nicht /tmp: /tmp ist im Cluster ein emptyDir mit Deckel, und dorthin schreibt
# auch die Einreichung. /run gehört im Image root mit 0755, der Worker läuft als
# root und der User sandbox liest dort, ohne schreiben zu können. Die Frist, ab
# der die Datei als zu alt gilt, steht nicht hier, sondern im Chart. Nur die
# Probe braucht sie, der Worker selbst liest die Datei nie.
HEARTBEAT_PFAD = "/run/heartbeat"

# Wall-Clock-Frist über RLIMIT_CPU: Die Aufgabe begrenzt die Rechenzeit über
# RLIMIT_CPU (_starter), diese Frist fängt zusätzlich, was wartet statt zu
# rechnen, denn RLIMIT_CPU zählt nur CPU-Zeit. Der Puffer liegt auf dem harten
# RLIMIT_CPU-Limit (zeit + 1), nicht auf zeit selbst: Nur so bleibt er ein
# kleiner Abstand zum tatsächlichen Konkurrenten und bläht nicht nebenbei die
# Frist für schlafende oder blockierende Einreichungen auf, die RLIMIT_CPU nie
# sieht. Muss über null liegen, sonst konkurrieren beide Mechanismen um
# dieselbe Grenze, und welcher zuerst greift, entscheidet der Zufall statt der
# Zeit.
ZEITFRIST_PUFFER = 0.5

# Dieser Worker bedient nur die Liste seiner eigenen Sprache (#82). Mehrere
# Sprachen heißt mehrere Worker-Deployments, je mit eigenem WORKER_SPRACHE und
# eigenem Image, nicht ein Worker, der mehrere Listen abfragt.
WORKER_SPRACHE = os.getenv("WORKER_SPRACHE", "python")
QUEUE_KEY = f"judge:{WORKER_SPRACHE}"

# Name des Worker-Pods an der Einreichung (#52, #53), damit nachvollziehbar
# bleibt, welcher Worker eine Übernahme hatte. In Kubernetes ist der Hostname
# eines Pods per Voreinstellung sein Name, ohne eigenen Eintrag über die
# Downward API.
WORKER_ID = f"worker-{socket.gethostname()}"

# Marge, wie lange eine Einreichung über ihre eigentliche Obergrenze hinaus
# als RUNNING gilt, bevor der Durchlauf aus #82 sie für hängengeblieben hält
# und erneut einreiht. Gilt in zwei Rollen: als Frist ab der Übernahme
# (_uebernehmen), solange die Aufgabe noch nicht gelesen ist, und danach als
# Marge über der tatsächlichen Obergrenze der Aufgabe (_urteil), sobald die
# feststeht. Eine feste Zahl als Frist selbst könnte über der Summe aller
# Testfälle einer Aufgabe nie zuverlässig liegen, die Zahl der Testfälle ist
# nicht gedeckelt. Als Marge auf die aus der Aufgabe berechnete Summe
# angewendet, wächst die Frist dagegen mit der Aufgabe mit.
#
# Abzudecken ist, was ein Lauf über die reine Rechenzeit hinaus braucht. Je
# Testfall kommen der Start des Prozesses, das Schreiben der Eingabe, das
# Aufräumen und ein Schreibzugriff auf MongoDB dazu, und die Frist je Lauf
# liegt mit zeit + 1 + ZEITFRIST_PUFFER über der zeit, mit der die Summe
# rechnet. 90 ist keine berechnete Zahl, sondern eine runde Wahl mit Abstand.
# Bei sehr vielen Testfällen reicht ein fester Wert dafür nicht, die Aufgaben
# im Repo haben zwei bis drei.
#
# Mit dem Takt des Durchlaufs hat die Marge nichts zu tun. Ein selteneres
# Laufen holt eine hängende Einreichung später zurück, und ein Worker, der über
# seine Frist hinaus arbeitet, bekommt dadurch eher noch die Gelegenheit, sein
# Ergebnis zu schreiben.
CLAIM_FRIST_PUFFER_SEKUNDEN = int(os.getenv("CLAIM_FRIST_PUFFER_SEKUNDEN", "90"))

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

# Zeitlimits an beiden Clients. Ohne sie wartet ein abgesetzter Aufruf
# unbegrenzt, sobald der Server die Verbindung hält und nicht mehr antwortet.
# Der Worker steht dann still, ohne dass es an ihm auffällt, und die Einreichung
# bleibt auf RUNNING stehen, bis der Durchlauf ihre Frist reißen sieht.
#
# serverSelectionTimeoutMS bleibt bei der Vorgabe von 30 Sekunden, wie am
# Hauptclient der API (app/backend/main.py). MongoDB läuft als ReplicaSet, und
# während einer Neuwahl des Primary ist für einige Sekunden kein Server wählbar.
# Diese Wartezeit ist gewollt. socketTimeoutMS deckt den anderen Fall ab, eine
# stehende Verbindung ohne Antwort, und zehn Sekunden liegen weit über allem,
# was ein Schreibzugriff im selben Cluster braucht. Gegen ein pausiertes mongodb
# im lokalen Compose bricht ein find_one_and_update damit nach 10,4 Sekunden mit
# NetworkTimeout ab, ohne die Werte wartet es endlos.
#
# Damit stirbt der Worker, wo er vorher hing. _uebernehmen steht ohne eigenes
# try in process_queue, ein NetworkTimeout dort beendet den Prozess. Der Tausch
# ist gewollt und nicht umsonst. Ein hängender Worker zieht einen Eintrag und
# steht danach still, der Pod zählt für KEDA weiter als Kapazität und die Queue
# wächst, ohne dass jemand arbeitet. Ein sterbender Worker macht den Ausfall
# sichtbar, zieht dafür aber je Neustart einen weiteren Eintrag, gebremst nur
# vom Backoff des Containers. Die so gezogenen Einreichungen holt durchlauf.py
# zurück (#113), jede davon kostet einen Versuch an requeue_versuche oder
# versuche.
mongo_client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    connectTimeoutMS=5000,
    socketTimeoutMS=10000,
)
db = mongo_client["coding_platform"]

# socket_timeout gilt auch für ein blockierendes blpop und bricht es ab, sobald
# es länger wartet. Deshalb liegt es über QUEUE_WARTEN und nicht darunter. Die
# fünf Sekunden Abstand decken Netzlatenz und die Antwort von Valkey selbst.
#
# Eine Garantie ist der Abstand nicht. Läuft der Socket in sein Limit, nachdem
# Valkey den Eintrag schon aus der Liste genommen hat, ist der Eintrag weg und
# der Worker erfährt es nie. BLPOP ohne Processing-Liste kennt diesen Fall
# ohnehin, er tritt genauso bei einem Verbindungsabbruch auf, und durchlauf.py
# fängt ihn über #113 ab.
redis_client = redis.Redis.from_url(
    os.getenv("REDIS_URI", "redis://localhost:6379"),
    socket_connect_timeout=5,
    socket_timeout=QUEUE_WARTEN + 5,
)


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
# Ein gesetztes seccomp-Profil wie das Standardprofil von Docker blockiert
# CLONE_NEWUSER; containerd im Cluster setzt keins, dort greift die Trennung.
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
# alle anderen Grenzen liegen fest. Gebaut wird er je Lauf neu, weil Zeit und
# Speicher von der Aufgabe kommen, nicht einmal beim Start des Workers.
def _starter(zeit, speicher_bytes):
    # RLIMIT_CPU zählt nur Sekunden, in denen der Prozess rechnet. Das ist die
    # eigentliche Grenze der Aufgabe; ZEITFRIST_PUFFER oben legt zusätzlich
    # eine Wall-Clock-Frist über den Lauf, die auch wartende Prozesse fängt,
    # die RLIMIT_CPU nicht sieht.
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


def _urteil_nach_signal(signalnummer, zeit, rusage):
    """Bildet ein Signal, an dem der Prozess gestorben ist, auf ein
    Verdict-Kürzel und einen Meldungstext ab."""
    if signalnummer == signal.SIGXCPU:
        return "TLE", f"Rechenzeit von {zeit} Sekunden überschritten"
    if signalnummer == signal.SIGXFSZ:
        return "OLE", f"mehr als {SANDBOX_AUSGABE_BYTES // 1024} KiB ausgegeben"
    # SIGXCPU trifft nur beim Soft-Limit, und die Einreichung kann sich dafür
    # per signal.signal einen eigenen Handler setzen: Die Warnung verpufft
    # dann, der Prozess rechnet weiter, bis ihn das Hard-Limit (zeit + 1)
    # stattdessen per SIGKILL beendet. Ob ein SIGKILL vom Zeitlimit kam oder
    # von woanders (Selbst-Kill, OOM-Killer), zeigt die rusage aus dem
    # eigenen wait4 oben: Steht dem Prozess bis zum Kill schon mindestens so
    # viel CPU-Zeit wie das Limit zu Buche, war das Limit die Ursache, ganz
    # gleich, wer das Signal geschickt hat. Ein Selbst-Kill oder der
    # OOM-Killer träfen früher, bei weniger CPU-Zeit als dem Limit.
    if signalnummer == signal.SIGKILL and rusage.ru_utime + rusage.ru_stime >= zeit:
        return "TLE", f"Rechenzeit von {zeit} Sekunden überschritten"
    # signal.Signals kennt die Echtzeitsignale oberhalb von SIGRTMIN nicht und
    # wirft dafür ValueError. Ungefangen würde der als Umgebungsfehler
    # herauskommen,
    # und eine Einreichung könnte ihr eigenes Ende damit zu einem Fehler des
    # Judges umdeuten.
    try:
        name = signal.Signals(signalnummer).name
    except ValueError:
        name = f"Signal {signalnummer}"
    return "RE", f"durch {name} beendet"


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


def _letzte_zeile(fd, grenze):
    """Die letzte Zeile einer Ausgabedatei, gelesen von ihrem Ende her.

    _gelesen liefert den Anfang der Datei, und der Anfang bleibt auch die
    Meldung an der Einreichung. Für die Einordnung zählt dagegen das Ende. Der
    Name der Exception steht in der letzten Zeile eines Tracebacks, und eine
    Einreichung, die vorher viel nach stderr schreibt, schöbe ihn sonst über
    die Grenze hinaus. Der Zugriff läuft wie in _gelesen über den file
    descriptor, nicht über den Pfad.
    """
    groesse = os.fstat(fd).st_size
    os.lseek(fd, max(0, groesse - grenze), os.SEEK_SET)
    text = os.read(fd, grenze).decode("utf-8", errors="replace").strip()
    return text.splitlines()[-1] if text else ""


class Umgebungsfehler(Exception):
    """Der Lauf ist an der Umgebung gescheitert, nicht am eingereichten Code.

    Ohne eigene Exception kommen diese Fälle als Zeichenkette zurück und laufen
    in denselben Vergleich mit der erwarteten Ausgabe wie ein echtes Ergebnis.
    Die Einreichung würde dann FAILED für Code bekommen, der nie gelaufen ist.
    """


def run_code_in_sandbox(code: str, test_input: str, zeit: int, speicher: int):
    """Führt den eingereichten Code als Subprozess mit den Grenzen der Aufgabe aus.

    Gibt ein Kürzel zurück (OK bei einem sauberen Lauf, sonst TLE, OLE, MLE
    oder RE), dazu Ausgabe oder Meldung, Laufzeit in ms und Spitzenspeicher in
    KiB. Ob OK zu AC oder WA wird, entscheidet der Aufrufer selbst durch den
    Vergleich mit der erwarteten Ausgabe, das kennt diese Funktion nicht.
    """
    verzeichnis = None
    deskriptoren = []
    eingabe_pfad = None
    try:
        # Reste eines früheren Laufs zuerst. Sie laufen unter derselben UID und
        # belegen deshalb das Kontingent aus SANDBOX_PROZESSE. Der nächste Lauf
        # bekäme dafür ein RE mit der Meldung, er habe seine eigene Grenze
        # ausgeschöpft, obwohl er nichts gestartet hat. Das Aufräumen läuft
        # schon nach jedem Lauf, hier bekommt es eine zweite Frist, denn ein
        # Prozess kann inzwischen von selbst geendet haben. Bleibt danach etwas
        # stehen, ist der Lauf an der Umgebung gescheitert und nicht am
        # eingereichten Code.
        _reste_beenden()
        reste = _sandbox_pids()
        if reste:
            raise Umgebungsfehler(
                f"Prozesse eines früheren Laufs stehen noch unter der UID der "
                f"Sandbox und belegen ihr Kontingent, PIDs {reste}"
            )

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

        # Eingabe ebenfalls über eine Datei statt über eine Pipe: der größte
        # Testfall (zweisumme) hat 372 KB, ein Pipe-Puffer nur 64 KiB. Mit dem
        # eigenen wait4 unten statt communicate liest niemand mehr nebenbei aus
        # der Pipe, ein stdin.write() bliebe ab dem vollen Puffer stehen. Die
        # Datei liegt eine Ebene über dem working directory und bleibt beim
        # Worker, damit die Einreichung sie nicht kürzen kann.
        eingabe_fd, eingabe_pfad = tempfile.mkstemp(
            prefix="eingabe-", dir=SANDBOX_BASIS
        )
        deskriptoren.append(eingabe_fd)
        # os.write ist der rohe Systemaufruf: Er schreibt bis zu so viele
        # Bytes wie übergeben und meldet im Rückgabewert, wie viele es
        # tatsächlich wurden. Läuft das Volume mitten im Schreiben voll,
        # bliebe eine gekürzte Eingabedatei sonst unbemerkt, die Lösung liest
        # dann eine andere als die erwartete Eingabe und die Einreichung
        # bekäme dafür ein WA, ein Endurteil für einen Fehler der
        # Infrastruktur. Läuft das Volume tatsächlich voll, wirft der nächste
        # Durchgang OSError, das except unten macht daraus den
        # Umgebungsfehler, und die Einreichung bleibt auf RUNNING.
        eingabe_bytes = test_input.encode("utf-8")
        geschrieben = 0
        while geschrieben < len(eingabe_bytes):
            geschrieben += os.write(eingabe_fd, eingabe_bytes[geschrieben:])
        os.lseek(eingabe_fd, 0, os.SEEK_SET)

        if SANDBOX_UID is not None:
            # Nur die Lösung und das working directory wechseln den Besitzer, die
            # Ebene darüber bleibt beim Worker.
            #
            # Die loesung.py zuerst, solange das Verzeichnis noch dem Worker
            # gehört und mode 0700 trägt. Erst danach das Verzeichnis. Sonst
            # gehörte es nach dem ersten chown der Sandbox, und ein Prozess aus
            # einem früheren Lauf unter derselben UID könnte die loesung.py bis
            # zum zweiten chown durch einen Sym- oder Hardlink auf eine fremde
            # Datei wie /etc/passwd ersetzen. Der Worker chownte sie dann als
            # root an die Sandbox. follow_symlinks=False sichert zusätzlich gegen
            # einen Symlink. Siehe #185.
            for ziel in (pfad / "loesung.py", pfad):
                os.chown(ziel, SANDBOX_UID, -1, follow_symlinks=False)
            pfad.chmod(0o700)

        start = time.monotonic()
        prozess = subprocess.Popen(
            [sys.executable, "-c", _starter(zeit, speicher)],
            cwd=verzeichnis,
            stdin=eingabe_fd,
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

        # Eigenes wait4 statt communicate/wait: nur wait4 liefert ru_maxrss,
        # den Spitzenspeicher des Kindes, auch für einen Lauf, der per SIGKILL
        # gestorben ist. WNOHANG bis zur Frist, damit die Schleife Zeit und
        # Speicher weiter zusammenhält, ohne blockierend auf das Kind zu warten.
        frist = start + zeit + 1 + ZEITFRIST_PUFFER
        while True:
            pid, status, rusage = os.wait4(prozess.pid, os.WNOHANG)
            if pid != 0:
                break
            if time.monotonic() > frist:
                os.killpg(prozess.pid, signal.SIGKILL)
                _, _, rusage = os.wait4(prozess.pid, 0)
                dauer_ms = round((time.monotonic() - start) * 1000)
                return (
                    "TLE",
                    f"Zeitlimit von {zeit} Sekunden überschritten",
                    dauer_ms,
                    rusage.ru_maxrss,
                )
            time.sleep(0.02)

        dauer_ms = round((time.monotonic() - start) * 1000)
        speicher_kb = rusage.ru_maxrss

        # returncode von Hand gesetzt: Das eigene wait4 oben umgeht Popens
        # Reaping, das returncode sonst selbst füllt. Dieselbe Umrechnung, die
        # subprocess intern vornimmt: negativ bei einem Signal.
        if os.WIFSIGNALED(status):
            prozess.returncode = -os.WTERMSIG(status)
        else:
            prozess.returncode = os.WEXITSTATUS(status)

        if prozess.returncode < 0:
            verdict, meldung = _urteil_nach_signal(-prozess.returncode, zeit, rusage)
            return verdict, meldung, dauer_ms, speicher_kb

        # Die Grenze meldet der Kernel je nach Zeitpunkt als Signal (oben, über
        # SIGXFSZ) oder als EFBIG an den schreibenden Aufruf. Im zweiten Fall
        # endet die Einreichung mit einem gewöhnlichen Traceback, und ohne
        # diese Prüfung stünde als Urteil RE statt des wahren Grundes. Für
        # stdout und stderr getrennt geprüft, denn die Grenze gilt jeder Datei
        # einzeln.
        for fd in (aus_fd, fehler_fd):
            if os.fstat(fd).st_size >= SANDBOX_AUSGABE_BYTES:
                grenze = SANDBOX_AUSGABE_BYTES // 1024
                return "OLE", f"mehr als {grenze} KiB ausgegeben", dauer_ms, speicher_kb

        ausgabe = _gelesen(aus_fd, SANDBOX_AUSGABE_BYTES)
        if prozess.returncode != 0:
            # Gekürzt, weil die Meldung als Urteil in der Datenbank landet und
            # dort neben jeder Einreichung steht. Ein Traceback passt hinein.
            meldung = _gelesen(fehler_fd, MELDUNG_MAX).strip()
            letzte_zeile = _letzte_zeile(fehler_fd, MELDUNG_MAX)

            # Eigene Meldung, wenn der Start eines weiteren Prozesses oder
            # Threads gescheitert ist. Ohne sie stünde als Urteil nur ein
            # Traceback, aus dem die Grenze nicht hervorgeht. Erkannt an der
            # letzten Zeile, wie beim MemoryError unten. fork meldet EAGAIN als
            # BlockingIOError, das Anlegen eines Threads als RuntimeError mit
            # festem Text, beide Male ohne eigene Fehlerklasse. Die Meldung
            # nennt deshalb beide Grenzen, die den Start verhindern können.
            # Dieselbe Zeile steht auch dann in stderr, wenn pthread_create am
            # Speicher der Aufgabe scheitert und nicht an SANDBOX_PROZESSE.
            # Eine Einreichung, die selbst einen BlockingIOError bis nach oben
            # durchreicht, bekommt diese Meldung fälschlich.
            if letzte_zeile.startswith(
                "BlockingIOError: [Errno 11]"
            ) or letzte_zeile.startswith("RuntimeError: can't start new thread"):
                return (
                    "RE",
                    "Start eines weiteren Prozesses oder Threads gescheitert."
                    " Der Judge begrenzt die Zahl der Prozesse je Einreichung,"
                    " Threads zählen mit. Auch zu wenig Speicher verhindert den"
                    f" Start. {letzte_zeile}",
                    dauer_ms,
                    speicher_kb,
                )

            # MLE statt des allgemeinen RE bei einem MemoryError. Der ist das
            # verlässliche Signal, nicht der gemessene Speicher: RLIMIT_AS
            # begrenzt den virtuellen Adressraum, eine einzelne große
            # Zuweisung (z. B. ein 200-MiB-bytearray in einem Zug) scheitert
            # dort schon beim mmap, bevor auch nur eine Seite eingelagert
            # wird. ru_maxrss zählt aber nur eingelagerte Seiten und bleibt
            # dann nahe der Grundlast des Interpreters, weit unter der
            # Grenze der Aufgabe. Die Zeilenprüfung greift, weil Tracebacks
            # mit dem Namen der Exception enden. Die Nähe zur Grenze bleibt
            # als zweites Signal für den Fall, dass Speicher schrittweise
            # wächst und der Absturz woanders auftritt als bei der Zuweisung
            # selbst.
            ist_memory_error = letzte_zeile == "MemoryError" or letzte_zeile.startswith(
                "MemoryError:"
            )
            if ist_memory_error or speicher_kb * 1024 >= speicher * 0.95:
                return (
                    "MLE",
                    meldung or "Speichergrenze überschritten",
                    dauer_ms,
                    speicher_kb,
                )
            return "RE", meldung or ausgabe.strip(), dauer_ms, speicher_kb
        return "OK", ausgabe.strip(), dauer_ms, speicher_kb
    except Umgebungsfehler:
        # Schon eingeordnet, der Zweig darunter würde die Meldung ein zweites
        # Mal in einen Umgebungsfehler packen.
        raise
    except Exception as e:
        # Der Typ bleibt in der Meldung: Umgebungsfehler sagt, dass es nicht am
        # eingereichten Code lag, nicht was gescheitert ist.
        raise Umgebungsfehler(f"{type(e).__name__}: {e}") from e
    finally:
        # Jeder Schritt einzeln abgefangen. Eine Exception aus dem Aufräumen
        # würde sonst die eigentliche Exception verdrängen und die Schritte
        # danach auslassen. Seit process_queue Fehler je Job abfängt, überlebt
        # der Worker das, und file descriptors und Verzeichnisse würden sich
        # über die Läufe hinweg ansammeln. Das Kind ist an dieser Stelle immer
        # schon über wait4 eingesammelt, _reste_beenden holt nur noch Prozesse,
        # die die Einreichung selbst gestartet hat.
        try:
            _reste_beenden()
        except Exception as e:
            print(f"Reste der Sandbox nicht beendet: {type(e).__name__}: {e}")
        for fd in deskriptoren:
            try:
                os.close(fd)
            except OSError:
                pass
        if eingabe_pfad is not None:
            try:
                os.unlink(eingabe_pfad)
            except OSError:
                pass
        if verzeichnis is not None:
            # Vor dem rmtree und nicht erst danach. Das rmtree hat keine Frist,
            # und eine Einreichung darf im Rahmen ihres emptyDir-Deckels sehr
            # viele Dateien anlegen. Ohne diesen Aufruf liefe das Aufräumen mit
            # dem Rest der Frist, die der Lauf selbst schon fast aufgebraucht
            # hat. Es bleibt eine Grenze: Braucht das rmtree selbst länger als
            # die Frist der Probe, stirbt der Worker trotzdem, siehe README
            # ## Grenzen.
            _heartbeat()
            try:
                shutil.rmtree(verzeichnis)
            except Exception as e:
                print(f"{verzeichnis} nicht entfernt: {e}")


def _heartbeat():
    """Setzt die mtime von HEARTBEAT_PFAD auf jetzt.

    Die Liveness-Probe im Chart prüft nur das Alter dieser Datei, deshalb steht
    nichts in ihr. Eine mtime kann nicht halb geschrieben sein, ein Zeitstempel
    im Inhalt schon.

    Der Aufruf steht an jeder Stelle, die einen Schritt abschließt, nicht nur je
    Schleifenrunde. Ein Lauf ist nach oben offen, GRENZE_ZEIT_MAX begrenzt 60
    Sekunden je Testfall und die Zahl der Testfälle nichts. Über einen ganzen
    Lauf ließe sich keine Frist herleiten, über einen einzelnen Schritt schon.

    Ein Fehler beim Schreiben beendet den Worker nicht. Er meldet sich von
    selbst, denn eine Datei, die nicht mehr jünger wird, lässt die Probe
    greifen.
    """
    try:
        pathlib.Path(HEARTBEAT_PFAD).touch()
    except OSError as e:
        print(f"Heartbeat nicht geschrieben: {type(e).__name__}: {e}")


def _sub_id_lesen(item):
    """Liest die ID der Einreichung aus der Queue.

    Seit #82 trägt die Liste nur noch die ID, keinen Auftrag mehr. Eigene
    Funktion, weil hier eine Grenze verläuft: Scheitert etwas in ihr, gibt es
    noch keine Einreichung, an die ein Fehler zu schreiben wäre. Alles danach
    lässt sich beschriften.
    """
    roh = item.decode("utf-8") if isinstance(item, bytes) else item
    return ObjectId(roh)


def _uebernehmen(sub_id):
    """Übernimmt eine Einreichung mit einem bedingten Update von PENDING auf
    RUNNING (#82): eigenes Token für diesen Versuch, Versuchszähler hoch,
    Frist neu gesetzt, Name dieses Workers vermerkt.

    Gibt das aktualisierte Document zurück, oder None, wenn die Übernahme
    nicht griff. Das ist kein Fehler: Ein anderer Worker oder der Durchlauf
    kann schneller gewesen sein, oder dieselbe ID steht kurzzeitig doppelt in
    der Queue. Erst nach der Übernahme werden Code und Aufgabe gelesen, aus
    dem Document selbst, nicht mehr aus der Queue.

    Die Frist hier ist nur der Platzhalter für die Zeit bis dahin: Die Aufgabe
    ist an dieser Stelle noch nicht gelesen, ihre echte Obergrenze also noch
    nicht bekannt. _urteil ersetzt sie, sobald die Aufgabe vorliegt.
    """
    token = uuid.uuid4().hex
    frist = datetime.now(timezone.utc) + timedelta(seconds=CLAIM_FRIST_PUFFER_SEKUNDEN)
    return db.submissions.find_one_and_update(
        {"_id": sub_id, "status": "PENDING"},
        {
            "$set": {
                "status": "RUNNING",
                "run_token": token,
                "frist": frist,
                "worker_id": WORKER_ID,
            },
            "$inc": {"versuche": 1},
        },
        return_document=ReturnDocument.AFTER,
    )


def _urteil(sub_id, token, submission, task):
    """Führt die Testfälle nacheinander aus und schreibt jeden einzeln in
    test_results, sobald er fertig ist. Bricht beim ersten nicht bestandenen
    Fall ab, die Fälle danach bleiben auf NOT_RUN stehen: Der Punktstand
    bedeutet damit "so viele bestanden, dann abgebrochen", nicht "das ist der
    einzige Fehler". Wie die Ergebnisseite das benennt, gehört zu #56, hier
    entsteht nur die Datengrundlage dafür.
    """
    test_cases = task.get("test_cases", [])

    # Ohne Testfälle liefe die Schleife unten leer durch und jede Einreichung
    # bestünde, auch ungültiger Code (#53). Das ist ein Fehler der Aufgabe,
    # kein Urteil über den Code. laden.py lehnt solche Aufgaben beim
    # Einspielen ab, hier geht es um Aufgaben, die daran vorbei in die
    # Datenbank kamen. UNRESOLVED wie ENDZUSTAND_ERSCHOEPFT in durchlauf.py:
    # nur "kein Urteil zustande gekommen", welchen Namen nicht beurteilbare
    # Einreichungen endgültig tragen, entscheidet #81. Die Prüfung steht vor
    # grenzen_der_aufgabe, damit eine Aufgabe mit beiden Mängeln zugleich
    # hier mit dem Grund endet statt als Umgebungsfehler in den Requeues.
    if not test_cases:
        return "UNRESOLVED", "Aufgabe ohne Testfälle, kein Urteil möglich"

    zeit, speicher = grenzen_der_aufgabe(task)

    # Löst die Frist von der Übernahme (_uebernehmen) ab, sobald die Aufgabe
    # feststeht: zeit gilt je Testfall, die Summe über alle Testfälle ist die
    # tatsächliche Obergrenze dieses Laufs, unabhängig von ihrer Zahl. Der
    # bedingte Update lässt einen inzwischen erschöpften run_token in Ruhe,
    # wie jeder andere Schreibzugriff hier.
    db.submissions.update_one(
        {"_id": sub_id, "status": "RUNNING", "run_token": token},
        {
            "$set": {
                "frist": datetime.now(timezone.utc)
                + timedelta(
                    seconds=zeit * len(test_cases) + CLAIM_FRIST_PUFFER_SEKUNDEN
                )
            }
        },
    )
    _heartbeat()

    # Platzhalter für alle Fälle, bevor der erste läuft. Damit trägt der
    # Fortschritt "2 von 5 erledigt" schon während des Laufs, nicht erst am
    # Ende.
    platzhalter = [
        {
            "test_id": i,
            "verdict": "NOT_RUN",
            "detail": None,
            "zeit_ms": None,
            "speicher_kb": None,
        }
        for i in range(1, len(test_cases) + 1)
    ]
    db.submissions.update_one(
        {"_id": sub_id, "status": "RUNNING", "run_token": token},
        {"$set": {"test_results": platzhalter}},
    )
    _heartbeat()

    bestanden = 0
    for i, case in enumerate(test_cases, 1):
        verdict, text, dauer_ms, speicher_kb = run_code_in_sandbox(
            submission["code"], case["input"], zeit, speicher
        )
        _heartbeat()
        if verdict == "OK":
            erwartet = str(case["expected_output"]).strip()
            if text == erwartet:
                verdict, detail = "AC", "bestanden"
            else:
                verdict, detail = "WA", f"Erwartet '{erwartet}', bekommen '{text}'"
        else:
            detail = text

        db.submissions.update_one(
            {"_id": sub_id, "status": "RUNNING", "run_token": token},
            {
                "$set": {
                    f"test_results.{i - 1}": {
                        "test_id": i,
                        "verdict": verdict,
                        "detail": detail,
                        "zeit_ms": dauer_ms,
                        "speicher_kb": speicher_kb,
                    }
                }
            },
        )
        _heartbeat()

        if verdict != "AC":
            return "FAILED", (
                f"{bestanden} von {len(test_cases)} bestanden, "
                f"abgebrochen bei Testfall {i}: {verdict}"
            )
        bestanden += 1

    return "SUCCESS", f"Alle {len(test_cases)} Tests bestanden"


def _ergebnis_schreiben(sub_id, token, status, text):
    """Schreibt das Urteil, aber nur mit noch gültigem Token (#82).

    Der Filter auf status RUNNING und das eigene run_token verhindert, dass
    ein Worker, dessen Frist bereits ablief und dessen Einreichung der
    Durchlauf schon an einen neuen Versuch vergeben hat, das frischere
    Ergebnis nachträglich mit seinem eigenen, veralteten überschreibt.
    Eigenes try/except, weil einer der Gründe für einen scheiternden Schreib-
    zugriff gerade die nicht erreichbare Datenbank ist. Ohne dieses
    try/except würde die Exception den Fehlerpfad verlassen und den Worker
    doch beenden. Die Einreichung bleibt dann auf RUNNING stehen, und genau
    die nimmt der Durchlauf nach Ablauf ihrer Frist wieder auf.
    """
    try:
        ergebnis = db.submissions.update_one(
            {"_id": sub_id, "status": "RUNNING", "run_token": token},
            {
                "$set": {
                    "status": status,
                    "result": text,
                    "run_token": None,
                    "frist": None,
                    "updated_at": time.time(),
                }
            },
        )
        if ergebnis.matched_count == 0:
            print(f"Einreichung {sub_id}: Token nicht mehr gültig, Ergebnis verworfen")
    except Exception as e:
        print(f"Einreichung {sub_id}: nicht geschrieben, {type(e).__name__}: {e}")


def process_queue():
    if not NETZ_TRENNUNG:
        print(
            "Hinweis: der eingereichte Code teilt das Netz des Workers und "
            "erreicht MongoDB und die Queue direkt. Im Cluster greift die Trennung, "
            "lokal blockiert das seccomp-Profil von Docker den nötigen Aufruf."
        )
    print(f"Worker gestartet ({WORKER_SPRACHE}), warte auf {QUEUE_KEY}...")
    while True:
        # Am Anfang der Runde und nicht erst nach dem blpop. Ein Valkey, das
        # nicht antwortet, ist kein Fehler dieses Workers, und ohne den Aufruf
        # hier töte die Probe bei einem Ausfall der Queue jeden Worker, ohne
        # dass ein Neustart etwas daran ändert.
        _heartbeat()
        try:
            eintrag = redis_client.blpop(QUEUE_KEY, timeout=QUEUE_WARTEN)
        except Exception as e:
            # Hier gibt es keine Einreichung, der etwas anzulasten wäre. Sich zu
            # beenden würde nichts bringen: Der Neustart landet an derselben Stelle,
            # solange Valkey weg ist, und der Container geht in den Backoff.
            print(f"Queue nicht erreichbar: {type(e).__name__}: {e}")
            time.sleep(QUEUE_PAUSE)
            continue

        # None heißt, in QUEUE_WARTEN Sekunden kam kein Eintrag. Der Normalfall
        # im Leerlauf, kein Fehler und nichts zu protokollieren.
        if eintrag is None:
            continue
        _, item = eintrag

        try:
            sub_id = _sub_id_lesen(item)
        except Exception as e:
            # Ohne brauchbare ID gibt es kein Document, an das ein Status zu
            # schreiben wäre. Bleibt das Protokoll, mit dem rohen Inhalt, weil
            # sonst niemand nachvollziehen kann, was ankam. Bytes, solange
            # decode_responses nicht gesetzt ist. Über REDIS_URI lässt sich
            # das umschalten, und ein AttributeError ausgerechnet in diesem
            # except-Zweig würde den Worker beenden.
            roh = item[:MELDUNG_MAX]
            inhalt = (
                roh.decode("utf-8", errors="replace") if isinstance(roh, bytes) else roh
            )
            print(f"Eintrag übersprungen: {type(e).__name__}: {e}, Inhalt: {inhalt}")
            continue

        submission = _uebernehmen(sub_id)
        _heartbeat()
        if submission is None:
            print(f"Einreichung {sub_id}: nicht übernommen, übersprungen")
            continue
        token = submission["run_token"]

        try:
            task = db.tasks.find_one({"_id": ObjectId(submission["task_id"])})
            _heartbeat()
            if task is None:
                raise Umgebungsfehler(
                    f"Aufgabe {submission['task_id']} steht nicht in tasks"
                )
            status, text = _urteil(sub_id, token, submission, task)
        except Exception as e:
            # Was hier ankommt, ist kein Urteil über den eingereichten Code:
            # ein Fehler der Umgebung, eine unbrauchbare Aufgabe oder ein
            # Fehler im Worker selbst. Der Worker wiederholt so etwas nicht von
            # sich aus (#78, #52): er schreibt hier absichtlich kein Ergebnis,
            # die Einreichung bleibt auf RUNNING stehen. Erst wenn ihre Frist
            # abläuft, nimmt der Durchlauf sie zurück auf PENDING und reiht
            # sie erneut ein, oder legt sie nach genug Versuchen als nicht
            # beurteilt ab (#81). Gekürzt, weil pymongo bei einem
            # Verbindungsfehler die ganze Topologie in die Meldung schreibt.
            text = f"{type(e).__name__}: {e}"[:MELDUNG_MAX]
            print(
                f"Einreichung {sub_id}: Umgebungsfehler, wartet auf den Durchlauf: {text}"
            )
            continue

        _ergebnis_schreiben(sub_id, token, status, text)
        _heartbeat()
        print(f"Einreichung {sub_id} verarbeitet: {status}")


if __name__ == "__main__":
    process_queue()
