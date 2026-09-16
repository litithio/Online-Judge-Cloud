"""Startet die API auf einem Socket, der IPv4 und IPv6 annimmt (#123).

uvicorn bindet über --host nur eine Adressfamilie. Der Cluster vergibt nur
IPv6, deshalb stand dort ::. Damit erreichte kubectl port-forward den Prozess
nicht, denn der Forward spricht im Pod-Netz 127.0.0.1 an. Mit 0.0.0.0 verlöre
umgekehrt Traefik den Weg zum Pod. Ein Socket auf :: mit IPV6_V6ONLY=0 nimmt
beides an. asyncio setzt IPV6_V6ONLY beim eigenen Bind auf 1
(BaseEventLoop.create_server), deshalb entsteht der Socket hier und geht fertig
gebunden an den Server. Linux bindet :: ohne die Option dual, solange der
sysctl net.ipv6.bindv6only auf 0 steht. Der Wert steht hier ausdrücklich,
damit der Socket nicht an diesem sysctl hängt.

Der Socket geht als Objekt an Server.run und nicht als Deskriptor über --fd.
Aus einem Deskriptor baut uvicorn den Socket mit der Familie AF_UNIX nach, und
asyncio setzt TCP_NODELAY nur für AF_INET und AF_INET6.
"""

import socket

import uvicorn

PORT = 8000


def lausch_socket(port):
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    # Wie uvicorn beim eigenen Bind. Nach einem Neustart des Containers liegen
    # im Pod-Netz noch Verbindungen in TIME_WAIT, ohne die Option scheitert
    # der Bind daran.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind(("::", port))
    return sock


def starten(port):
    # listen ruft asyncio beim Start des Servers selbst auf.
    with lausch_socket(port) as sock:
        uvicorn.Server(uvicorn.Config("main:app")).run(sockets=[sock])


if __name__ == "__main__":
    starten(PORT)
