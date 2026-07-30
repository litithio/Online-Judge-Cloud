import sys

daten = sys.stdin.read().split()
n, k = int(daten[0]), int(daten[1])
gesehen = set()
for wort in daten[2:2 + n]:
    z = int(wort)
    if k - z in gesehen:
        print("JA")
        break
    gesehen.add(z)
else:
    print("NEIN")
