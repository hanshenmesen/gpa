import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import demo_web.server as server_module


class WebSecurityHeaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_port = server_module.PORT
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), server_module.Handler)
        server_module.PORT = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{server_module.PORT}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        server_module.PORT = cls.original_port

    def assert_security_headers(self, headers):
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_html_and_json_responses_include_security_and_length_headers(self):
        for path in ("/", "/api/status"):
            with self.subTest(path=path):
                with urlopen(self.base_url + path, timeout=5) as response:
                    body = response.read()
                    self.assert_security_headers(response.headers)
                    self.assertEqual(int(response.headers["Content-Length"]), len(body))

    def test_local_cors_is_exact_and_foreign_write_is_rejected(self):
        local_request = Request(
            self.base_url + "/api/status",
            headers={"Origin": self.base_url},
        )
        with urlopen(local_request, timeout=5) as response:
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], self.base_url)

        foreign_request = Request(
            self.base_url + "/api/client/heartbeat",
            data=json.dumps({"client_id": "foreign"}).encode(),
            headers={"Content-Type": "application/json", "Origin": "https://example.com"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(foreign_request, timeout=5)
        self.assertEqual(raised.exception.code, 403)
        self.assertIsNone(raised.exception.headers.get("Access-Control-Allow-Origin"))
        self.assert_security_headers(raised.exception.headers)

    def test_not_found_response_is_also_protected(self):
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.base_url + "/not-found", timeout=5)
        self.assertEqual(raised.exception.code, 404)
        self.assert_security_headers(raised.exception.headers)

    def test_malformed_and_non_object_json_return_stable_validation_errors(self):
        for body in (b"{broken", b"[]"):
            with self.subTest(body=body):
                request = Request(
                    self.base_url + "/api/client/heartbeat",
                    data=body,
                    headers={"Content-Type": "application/json", "Origin": self.base_url},
                    method="POST",
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=5)
                self.assertEqual(raised.exception.code, 422)
                response = json.load(raised.exception)
                self.assertFalse(response["ok"])

    def test_invalid_or_oversized_content_length_is_rejected_without_reading_body(self):
        for content_length, expected_status in (("invalid", 422), (str(2 * 1024 * 1024), 413)):
            with self.subTest(content_length=content_length):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server_module.PORT, timeout=5
                )
                connection.putrequest("POST", "/api/client/heartbeat")
                connection.putheader("Origin", self.base_url)
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", content_length)
                connection.endheaders()
                response = connection.getresponse()
                body = json.loads(response.read())
                connection.close()

                self.assertEqual(response.status, expected_status)
                self.assertFalse(body["ok"])

    def test_workflow_api_distinguishes_invalid_and_missing_resources(self):
        with self.assertRaises(HTTPError) as invalid:
            urlopen(self.base_url + "/api/workflows/%2E%2E", timeout=5)
        self.assertEqual(invalid.exception.code, 400)

        request = Request(
            self.base_url + "/api/workflows/definitely-missing/delete",
            data=b"{}",
            headers={"Content-Type": "application/json", "Origin": self.base_url},
            method="POST",
        )
        with self.assertRaises(HTTPError) as missing:
            urlopen(request, timeout=5)
        self.assertEqual(missing.exception.code, 404)
        payload = json.load(missing.exception)
        self.assertEqual(payload["error"], "Workflow not found: definitely-missing")


if __name__ == "__main__":
    unittest.main()
