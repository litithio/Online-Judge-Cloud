import sys

n = int(sys.stdin.readline())
if n < 2:
    print(0)
else:
    sieb = bytearray([1]) * (n + 1)
    sieb[0] = sieb[1] = 0
    for i in range(2, int(n**0.5) + 1):
        if sieb[i]:
            sieb[i * i :: i] = bytearray(len(range(i * i, n + 1, i)))
    print(sum(sieb))
