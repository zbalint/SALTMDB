import urllib.request
import json

endpoints = [
    "/",
    "/api/entities",
    "/api/events",
    "/api/tags",
    "/api/relations",
    "/api/locks"
]

for ep in endpoints:
    url = f"http://127.0.0.1:8080{ep}"
    try:
        req = urllib.request.urlopen(url)
        content = req.read()
        print(f"Endpoint {ep} -> Status: {req.status}, Size: {len(content)} bytes")
        if ep.startswith("/api/"):
            # Check JSON validity
            data = json.loads(content.decode("utf-8"))
            if "error" in data:
                print(f"  [ERROR IN JSON]: {data['error']}")
    except Exception as e:
        print(f"Endpoint {ep} -> FAILED: {e}")
