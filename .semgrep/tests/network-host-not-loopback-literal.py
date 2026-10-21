import socket
from socket import create_connection as open_socket


class ServerHTTPServer:
    def __init__(self, address: tuple[str, int], handler: object) -> None:
        self.address = address
        self.handler = handler


class Handler:
    pass


class SALTMDBTCPServer(ServerHTTPServer):
    pass


class _ThreadingRpcServer(ServerHTTPServer):
    pass


def network_calls(host: str, port: int, server: socket.socket) -> None:
    # ruleid: network-host-not-loopback-literal
    open_socket((host, port), timeout=1)
    # ruleid: network-host-not-loopback-literal
    server.bind((host, port))
    # ruleid: network-host-not-loopback-literal
    ServerHTTPServer((host, port), object())
    # ruleid: network-host-not-loopback-literal
    SALTMDBTCPServer((host, 8080), Handler)
    # ruleid: network-host-not-loopback-literal
    _ThreadingRpcServer(("0.0.0.0", 0), Handler)


def loopback_calls(server: socket.socket, port: int) -> None:
    # ok: network-host-not-loopback-literal
    socket.create_connection(("127.0.0.1", port), timeout=1)
    # ok: network-host-not-loopback-literal
    server.bind(("127.0.0.1", port))
    # ok: network-host-not-loopback-literal
    ServerHTTPServer(("127.0.0.1", port), object())
    # ok: network-host-not-loopback-literal
    SALTMDBTCPServer(("127.0.0.1", 8080), Handler)
    # ok: network-host-not-loopback-literal
    _ThreadingRpcServer(("127.0.0.1", 0), Handler)
