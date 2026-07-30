# Mit Distributionsnamen und nicht nur -slim: der kurze Tag wechselt die
# Debian-Basis von allein, zuletzt von Bookworm auf Trixie, und tauscht damit
# die glibc aus, gegen die die Zeitlimits des Judge gemessen sind.
FROM python:3.12-slim-trixie
# Ohne das puffert Python die Ausgabe blockweise und der Worker schweigt, bis
# der Puffer voll ist. Im Cluster bliebe kubectl logs genauso leer.
ENV PYTHONUNBUFFERED=1

# Unter diesem User läuft der eingereichte Code, getrennt von dem des Workers.
# Ohne Heimatverzeichnis und ohne Anmeldeschale, er soll nichts können außer
# rechnen. Der Worker wechselt vor dem Start des Subprozesses auf ihn.
RUN useradd --system --no-create-home --shell /usr/sbin/nologin sandbox

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# 750 statt der Vorgabe 755: Der eingereichte Code läuft als sandbox und soll
# worker.py nicht lesen können. Die Standardbibliothek liegt außerhalb von /app
# und bleibt erreichbar.
RUN chmod 750 /app
CMD ["python", "worker.py"]
