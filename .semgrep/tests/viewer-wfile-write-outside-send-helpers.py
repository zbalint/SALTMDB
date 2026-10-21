class Writer:
    def write(self, data: bytes) -> None:
        _ = data

    def flush(self) -> None:
        return None


class Handler:
    wfile: Writer

    def __init__(self) -> None:
        self.wfile = Writer()

    def render(self) -> None:
        # ruleid: viewer-wfile-write-outside-send-helpers
        self.wfile.write(b"body")

    def send_body(self) -> None:
        # ok: viewer-wfile-write-outside-send-helpers
        self.wfile.write(b"body")

    def send_error(self) -> None:
        # ok: viewer-wfile-write-outside-send-helpers
        self.wfile.write(b"error")


def helper(handler: Handler) -> None:
    # ok: viewer-wfile-write-outside-send-helpers
    handler.wfile.flush()
