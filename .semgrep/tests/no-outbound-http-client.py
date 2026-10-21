import http.client
import httpx
import requests
from urllib import request
from urllib.request import urlopen as fetch_url


def outbound_calls(url: str) -> None:
    # ruleid: no-outbound-http-client
    fetch_url(url)
    # ruleid: no-outbound-http-client
    request.urlopen(url)
    # ruleid: no-outbound-http-client
    http.client.HTTPConnection("example.test")
    # ruleid: no-outbound-http-client
    http.client.HTTPSConnection("example.test")
    # ruleid: no-outbound-http-client
    requests.get(url)
    # ruleid: no-outbound-http-client
    httpx.get(url)


def local_calls() -> None:
    # ok: no-outbound-http-client
    request.Request("http://127.0.0.1")
    # ok: no-outbound-http-client
    http.client.HTTPMessage()
