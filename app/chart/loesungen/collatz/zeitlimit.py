import sys

n = int(sys.stdin.readline())
beste = 0
for start in range(1, n + 1):
    x = start
    schritte = 1
    while x != 1:
        x = 3 * x + 1 if x % 2 else x // 2
        schritte += 1
    if schritte > beste:
        beste = schritte
print(beste)
