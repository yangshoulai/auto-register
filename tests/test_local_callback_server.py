from __future__ import annotations

import unittest
from urllib.request import urlopen

from register.local_callback_server import LocalCallbackServer


class LocalCallbackServerTest(unittest.TestCase):
    def test_server_returns_ok_for_callback_request(self) -> None:
        server = LocalCallbackServer(port=0)

        try:
            server.start()
            with urlopen(f"{server.url}/auth/callback?code=test", timeout=2) as response:
                body = response.read().decode("utf-8")
                status = response.status
        finally:
            server.stop()

        self.assertEqual(status, 200)
        self.assertEqual(body, "ok")

    def test_start_and_stop_are_idempotent(self) -> None:
        server = LocalCallbackServer(port=0)

        server.start()
        started_url = server.url
        server.start()
        server.stop()
        server.stop()

        self.assertIn("http://localhost:", started_url)


if __name__ == "__main__":
    unittest.main()
