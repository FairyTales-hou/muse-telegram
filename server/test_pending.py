"""Regression tests for fetch-and-claim persistence."""

import importlib.util
import json
from pathlib import Path
import random
import tempfile
import threading
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("bridge", Path(__file__).with_name("bridge.py"))
bridge = importlib.util.module_from_spec(spec)
# The published snapshot replaces these production constants with placeholders.
bridge.OWNER_CHANNEL_ID = -1
bridge.CHANNEL_BOT_SHARED_ID = -2
spec.loader.exec_module(bridge)


class FakeHandler:
    def __init__(self, path):
        self.path = path
        self.response = None

    def _route(self):
        return "pending", None

    def _send_json(self, status, body):
        self.response = status, body


class PendingPersistenceTests(unittest.TestCase):
    def request(self, queue, path="/token/pending?chat_id=1"):
        state = {"queue": queue}
        handler = FakeHandler(path)
        with patch.object(bridge, "load_state", return_value=state), \
             patch.object(bridge, "save_state") as save, \
             patch.object(bridge.time, "time", return_value=2000):
            bridge.Handler.do_GET(handler)
        return handler.response, save.call_count

    def test_empty_poll_does_not_write(self):
        response, writes = self.request([])
        self.assertEqual(response, (200, []))
        self.assertEqual(writes, 0)

    def test_claim_is_persisted(self):
        record = {"id": 1, "chat_id": 1, "status": "pending"}
        response, writes = self.request([record])
        self.assertEqual(response[1], [record])
        self.assertEqual(record["status"], "processing")
        self.assertEqual(record["claimed_at"], 2000)
        self.assertEqual(writes, 1)

    def test_reclaim_for_another_chat_is_persisted(self):
        record = {"id": 1, "chat_id": 2, "status": "processing", "claimed_at": 1}
        response, writes = self.request([record])
        self.assertEqual(response, (200, []))
        self.assertEqual(record["status"], "pending")
        self.assertNotIn("claimed_at", record)
        self.assertEqual(writes, 1)

    def test_expired_record_is_reclaimed_and_claimed_once(self):
        record = {"id": 1, "chat_id": 1, "status": "processing", "claimed_at": 1}
        response, writes = self.request([record])
        self.assertEqual(response[1], [record])
        self.assertEqual(record["status"], "processing")
        self.assertEqual(record["claimed_at"], 2000)
        self.assertEqual(writes, 1)

    def test_unexpired_claim_is_not_reissued(self):
        record = {"id": 1, "chat_id": 1, "status": "processing", "claimed_at": 1100}
        response, writes = self.request([record])
        self.assertEqual(response, (200, []))
        self.assertEqual(writes, 0)

    def test_idle_claim_respects_other_processing_and_limit(self):
        queue = [{"id": i, "chat_id": 1, "status": "pending"} for i in range(4)]
        queue.append({"id": 9, "chat_id": 2, "status": "processing", "claimed_at": 1900})
        response, writes = self.request(queue, "/token/pending?chat_id=1&idle_only=1&limit=2")
        self.assertEqual([r["id"] for r in response[1]], [0, 1])
        self.assertEqual(queue[2]["status"], "pending")
        self.assertEqual(writes, 1)

    def test_busy_chat_does_not_claim(self):
        queue = [{"id": 1, "chat_id": 1, "status": "processing", "claimed_at": 1900},
                 {"id": 2, "chat_id": 1, "status": "pending"}]
        response, writes = self.request(queue, "/token/pending?chat_id=1&idle_only=1&limit=2")
        self.assertEqual(response, (200, []))
        self.assertEqual(queue[1]["status"], "pending")
        self.assertEqual(writes, 0)

    def test_invalid_options_do_not_claim(self):
        for query in ("idle_only=1", "chat_id=no&idle_only=1", "chat_id=1&limit=0",
                      "chat_id=1&limit=1001", "chat_id=1&limit=", "chat_id=1&idle_only=x"):
            queue = [{"id": 1, "chat_id": 1, "status": "pending"}]
            response, writes = self.request(queue, "/token/pending?" + query)
            self.assertEqual(response[0], 400, query)
            self.assertEqual(queue[0]["status"], "pending")
            self.assertEqual(writes, 0)


class PendingConcurrencyTests(unittest.TestCase):
    def test_concurrent_hooks_claim_only_one_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.json")
            config_path = str(Path(directory) / "config.json")
            Path(config_path).write_text(json.dumps({"bridge_token": "test"}), encoding="utf-8")
            Path(state_path).write_text(json.dumps({"queue": [
                {"id": i, "chat_id": 1, "status": "pending"} for i in range(30)
            ]}), encoding="utf-8")
            with patch.object(bridge, "STATE_PATH", state_path), patch.object(bridge, "CONFIG_PATH", config_path):
                server = bridge.ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = "http://127.0.0.1:%s/test/pending?chat_id=1&idle_only=1&limit=15" % server.server_port
                    def request(_):
                        with urllib.request.urlopen(url, timeout=5) as response:
                            return json.load(response)
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        results = list(pool.map(request, range(2)))
                    self.assertEqual(sorted(map(len, results)), [0, 15])
                    saved = json.loads(Path(state_path).read_text(encoding="utf-8"))
                    self.assertEqual(sum(r["status"] == "processing" for r in saved["queue"]), 15)
                finally:
                    server.shutdown()
                    thread.join()
                    server.server_close()


class DuplicationTests(unittest.TestCase):
    def test_matches_original_algorithm(self):
        def original(texts):
            norms = [n for n in (bridge._normalize_text(t) for t in texts) if n]
            if len(norms) < 2:
                return 0.0
            similar = sum(any(i != j and (a == b or
                (len(a) >= 4 and len(b) >= 4 and (a in b or b in a)))
                for j, b in enumerate(norms)) for i, a in enumerate(norms))
            return round(similar / len(norms), 3)

        rng = random.Random(42)
        samples = [None, "", "a", "AA", " ABCD ", "abcde", "abc", "bcde", "hello", "HELLO"]
        for _ in range(500):
            texts = rng.choices(samples, k=rng.randrange(50))
            self.assertEqual(bridge._duplication_ratio(texts), original(texts))


if __name__ == "__main__":
    unittest.main()
