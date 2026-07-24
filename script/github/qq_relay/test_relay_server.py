#!/usr/bin/env python3

import json
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import relay_server


class RateLimiterTest(unittest.TestCase):
    def test_rejects_requests_over_window_limit(self):
        limiter = relay_server.RateLimiter(limit=2, window_seconds=60)

        self.assertTrue(limiter.acquire(now=100))
        self.assertTrue(limiter.acquire(now=101))
        self.assertFalse(limiter.acquire(now=102))
        self.assertTrue(limiter.acquire(now=161))


class OneBotClientTest(unittest.TestCase):
    @patch("relay_server.urlopen")
    def test_group_number_is_fixed_by_server_configuration(self, urlopen_mock):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"status": "ok", "retcode": 0, "data": {"message_id": 99}}
        ).encode()
        urlopen_mock.return_value = response
        client = relay_server.OneBotClient("http://napcat:3000", "onebot-secret", 1080856850)

        message_id = client.send_group_message("hello")

        self.assertEqual("99", message_id)
        request = urlopen_mock.call_args.args[0]
        self.assertEqual("http://napcat:3000/send_group_msg", request.full_url)
        self.assertEqual(
            {"group_id": 1080856850, "message": "hello"},
            json.loads(request.data.decode()),
        )
        self.assertEqual("Bearer onebot-secret", request.get_header("Authorization"))


class RelayHTTPTest(unittest.TestCase):
    def setUp(self):
        config = relay_server.RelayConfig(
            relay_token="r" * 48,
            onebot_token="o" * 48,
            onebot_url="http://napcat:3000",
            repository="OtterMind/Chat2DB",
            group_id=1080856850,
            rate_limit=2,
        )
        self.state = relay_server.RelayState(config)
        self.state.onebot.send_group_message = MagicMock(return_value="message-42")
        self.server = relay_server.ThreadingHTTPServer(
            ("127.0.0.1", 0), relay_server.create_handler(self.state)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _post(self, payload, token=None):
        request = Request(
            self.base_url + "/v1/qq/github",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {token or 'r' * 48}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())

    def _payload(self, delivery_id="run-1", message="hello"):
        return {
            "repository": "OtterMind/Chat2DB",
            "delivery_id": delivery_id,
            "message": message,
        }

    def test_health_check_does_not_require_authentication(self):
        with urlopen(self.base_url + "/healthz", timeout=2) as response:
            self.assertEqual(200, response.status)
            self.assertEqual({"ok": True}, json.loads(response.read()))

    def test_authenticated_message_is_forwarded(self):
        status, body = self._post(self._payload())

        self.assertEqual(200, status)
        self.assertFalse(body["duplicate"])
        self.assertEqual("message-42", body["message_id"])
        self.state.onebot.send_group_message.assert_called_once_with("hello")

    def test_duplicate_delivery_is_not_sent_twice(self):
        self._post(self._payload())
        _, body = self._post(self._payload())

        self.assertTrue(body["duplicate"])
        self.state.onebot.send_group_message.assert_called_once()

    def test_invalid_token_is_rejected(self):
        with self.assertRaises(HTTPError) as context:
            self._post(self._payload(), token="wrong")

        self.assertEqual(401, context.exception.code)
        self.state.onebot.send_group_message.assert_not_called()

    def test_other_repository_is_rejected(self):
        payload = self._payload()
        payload["repository"] = "someone/else"

        with self.assertRaises(HTTPError) as context:
            self._post(payload)

        self.assertEqual(403, context.exception.code)

    def test_message_length_is_bounded(self):
        with self.assertRaises(HTTPError) as context:
            self._post(self._payload(message="x" * 901))

        self.assertEqual(400, context.exception.code)

    def test_rate_limit_releases_rejected_delivery(self):
        self._post(self._payload("run-1"))
        self._post(self._payload("run-2"))

        with self.assertRaises(HTTPError) as context:
            self._post(self._payload("run-3"))

        self.assertEqual(429, context.exception.code)
        self.assertIsNone(self.state.deliveries.reserve("run-3"))


if __name__ == "__main__":
    unittest.main()
