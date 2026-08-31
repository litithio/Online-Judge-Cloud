import sys

n = int(sys.stdin.readline())
anzahl = 0
for k in range(2, n + 1):
    teiler = 2
    while teiler * teiler <= k:
        if k % teiler == 0:
            break
        teiler += 1
    else:
        anzahl += 1
print(anzahl)
