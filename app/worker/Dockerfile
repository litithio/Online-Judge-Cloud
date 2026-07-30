# Mit Distributionsnamen und nicht nur -slim: der kurze Tag wechselt die
# Debian-Basis von allein, zuletzt von Bookworm auf Trixie, und tauscht damit
# die glibc aus, gegen die die Zeitlimits des Judge gemessen sind.
#
# Über ein ARG, weil derselbe Wert zweimal gebraucht wird: als Laufzeit des
# Workers und als Image der Sandbox, in der der eingereichte Code läuft. Zwei
# getrennte Angaben würden bei der nächsten Aktualisierung auseinanderlaufen,
# ohne dass es auffällt. Das zweite ARG ist nötig, weil eine Deklaration vor
# FROM im Bauabschnitt nicht mehr sichtbar ist.
ARG PYTHON_IMAGE=python:3.12-slim-trixie
FROM ${PYTHON_IMAGE}
ARG PYTHON_IMAGE
ENV SANDBOX_IMAGE=${PYTHON_IMAGE}
# Ohne das puffert Python die Ausgabe blockweise und der Worker schweigt, bis
# der Puffer voll ist. Im Cluster bliebe kubectl logs genauso leer.
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "worker.py"]