# Mit Distributionsnamen und nicht nur -slim: der kurze Tag wechselt die
# Debian-Basis von allein, zuletzt von Bookworm auf Trixie, und tauscht damit
# die glibc aus, gegen die die Zeitlimits des Judge gemessen sind.
FROM python:3.12-slim-trixie
# Ohne das puffert Python die Ausgabe blockweise und der Worker schweigt, bis
# der Puffer voll ist. Im Cluster bliebe kubectl logs genauso leer.
ENV PYTHONUNBUFFERED=1

# Kein eigener User für den eingereichten Code. Der Worker vergibt je Lauf eine
# UID aus einem Bereich über 65534, und der Kernel braucht dafür keinen Eintrag
# in /etc/passwd (#87). Ein Vorrat an useradd-Usern wäre nur eine zweite Stelle,
# die zum Bereich in worker.py passen muss.

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# 750 statt der Vorgabe 755: Der eingereichte Code läuft unter einer eigenen UID
# und soll worker.py nicht lesen können. Die Standardbibliothek liegt außerhalb
# von /app und bleibt erreichbar.
RUN chmod 750 /app
CMD ["python", "worker.py"]
