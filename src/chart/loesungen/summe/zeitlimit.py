import sys

a, b = map(int, sys.stdin.read().split())
# Für die Summe gibt es keinen natürlich langsamen Algorithmus wie bei den
# anderen Aufgaben. Die Schleife verbrennt deshalb absichtlich Zeit, bis das
# Zeitlimit des Workers greift.
z = 0
while True:
    z += 1
print(a + b)
