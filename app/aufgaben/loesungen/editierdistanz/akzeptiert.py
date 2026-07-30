import sys

a = sys.stdin.readline().strip()
b = sys.stdin.readline().strip()
vorher = list(range(len(b) + 1))
for i, ca in enumerate(a, 1):
    jetzt = [i]
    for j, cb in enumerate(b, 1):
        jetzt.append(min(vorher[j] + 1, jetzt[j - 1] + 1,
                         vorher[j - 1] + (ca != cb)))
    vorher = jetzt
print(vorher[-1])
