"""Fake-Datenbank für die Backend-Tests.

Die Filter der geprüften Routen bestehen aus Gleichheiten je Feld und, seit
#240, $in für die Sammel-Nachschlage der Aufgabentitel in
main._einreichungen_liste. Mehr wertet FakeCollection nicht aus. Ein Test
gegen eine echte MongoDB bräuchte einen laufenden Dienst im Test-Container,
den es dort bewusst nicht gibt.
"""

from urllib.parse import urlencode

from bson import ObjectId
from starlette.requests import Request


def _treffer(dokument, filter):
    for feld, bedingung in filter.items():
        wert = dokument.get(feld)
        if isinstance(bedingung, dict) and "$in" in bedingung:
            if wert not in bedingung["$in"]:
                return False
        elif wert != bedingung:
            return False
    return True


class FakeCursor(list):
    """Trägt nur .sort() zusätzlich zu list, dem einzigen Cursor-Aufruf, den
    main.py nach find() verkettet (_einreichungen_liste). Eigene Signatur
    (Feld, Richtung) wie pymongo, nicht die von list.sort(key=, reverse=).
    """

    def sort(self, feld, richtung=1):
        super().sort(key=lambda d: d.get(feld), reverse=richtung < 0)
        return self


class FakeCollection:
    def __init__(self, dokumente):
        self.dokumente = dokumente

    def find_one(self, filter, projektion=None):
        for dokument in self.dokumente:
            if _treffer(dokument, filter):
                return dict(dokument)
        return None

    def find(self, filter=None, projektion=None):
        if not filter:
            return FakeCursor(dict(d) for d in self.dokumente)
        return FakeCursor(dict(d) for d in self.dokumente if _treffer(d, filter))

    def insert_one(self, dokument):
        neu = dict(dokument)
        neu.setdefault("_id", ObjectId())
        self.dokumente.append(neu)
        return _EingefuegtesDokument(neu["_id"])


class _EingefuegtesDokument:
    def __init__(self, inserted_id):
        self.inserted_id = inserted_id


class FakeDb:
    def __init__(self, einreichungen=(), aufgaben=()):
        self.submissions = FakeCollection(list(einreichungen))
        self.tasks = FakeCollection(list(aufgaben))


def anfrage():
    # Nur für die Signatur der HTML-Routen. Die Templates greifen auf kein
    # Feld der Anfrage zu, ein leerer Scope reicht.
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
        }
    )


def formular_anfrage(daten):
    """POST-Anfrage mit einem application/x-www-form-urlencoded-Körper, für
    Routen wie verwaltung_aufgabe_anlegen (#240), die await request.form()
    aufrufen. receive liefert den Körper in einem Stück, request.form() holt
    ihn dort ab.
    """
    koerper = urlencode(daten, doseq=True).encode()

    async def receive():
        return {"type": "http.request", "body": koerper, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"content-length", str(len(koerper)).encode()),
            ],
            "query_string": b"",
        },
        receive,
    )


class FakeRedis:
    """llen für main.verwaltung_seite und rpush für main.submit_code, mehr
    rufen die Routen nicht auf. Beide arbeiten auf denselben Listen, laengen
    belegt eine Queue vorab mit so vielen Einträgen, wie llen melden soll.
    """

    def __init__(self, laengen=None):
        self.listen = {
            name: [None] * anzahl for name, anzahl in (laengen or {}).items()
        }

    def llen(self, schluessel):
        return len(self.listen.get(schluessel, []))

    def rpush(self, name, wert):
        self.listen.setdefault(name, []).append(wert)
