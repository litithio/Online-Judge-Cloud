import sys

n = int(sys.stdin.readline())
laenge = [0] * (n + 1)
laenge[1] = 1
for start in range(2, n + 1):
    kette = []
    x = start
    while x > n or laenge[x] == 0:
        kette.append(x)
        x = 3 * x + 1 if x % 2 else x // 2
    basis = laenge[x]
    for i, y in enumerate(reversed(kette), 1):
        if y <= n:
            laenge[y] = basis + i
print(max(laenge))
