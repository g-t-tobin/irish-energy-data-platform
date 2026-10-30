"""Container health probe: exit 0 if the local API answers GET /health with 200.

Uses only the standard library so the image needs no curl; /health needs no token.
"""

import http.client
import os
import sys


def check(port: int, path: str = "/health", timeout: float = 3.0) -> bool:
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            connection.request("GET", path)
            return connection.getresponse().status == 200
        finally:
            connection.close()
    except (OSError, http.client.HTTPException):
        return False


def main() -> int:
    port = int(os.environ.get("PORT", "8000"))
    path = os.environ.get("HEALTH_CHECK_PATH", "/health")
    return 0 if check(port, path) else 1


if __name__ == "__main__":
    sys.exit(main())
