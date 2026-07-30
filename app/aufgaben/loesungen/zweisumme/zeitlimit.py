import sys

daten = sys.stdin.read().split()
n, k = int(daten[0]), int(daten[1])
zahlen = [int(w) for w in daten[2:2 + n]]
for i in range(n):
    for j in range(i + 1, n):
        if zahlen[i] + zahlen[j] == k:
            print("JA")
            sys.exit()
print("NEIN")
