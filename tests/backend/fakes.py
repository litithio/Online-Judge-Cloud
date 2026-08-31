"""Fake-Datenbank für die Backend-Tests.

Die Filter der geprüften Routen bestehen aus Gleichheiten je Feld, mehr
wertet FakeCollection nicht aus. Ein Test gegen eine echte MongoDB bräuchte
einen laufenden Dienst im Test-Container, den es dort bewusst nicht gibt.
"""

from starlette.requests import Request


class FakeCollection:
    def __init__(self, dokumente):
        self.dokumente = dokumente

    def find_one(self, filter, projektion=None):
        for dokument in self.dokumente:
            if all(dokument.get(feld) == wert for feld, wert in filter.items()):
                return dict(dokument)
        return None

    def find(self, filter=None, projektion=None):
        if not filter:
            return [dict(d) for d in self.dokumente]
        return [
            dict(d)
            for d in self.dokumente
            if all(d.get(feld) == wert for feld, wert in filter.items())
        ]


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
