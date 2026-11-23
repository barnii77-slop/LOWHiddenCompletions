"""Unit tests for triggercomplete_core against the mock server.  python3 -m unittest discover tests"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "extension", "python", "pythonpath"))
import triggercomplete_core as core  # noqa: E402

TEXT = "jumps over the lazy dog and runs into the forest."


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockServer:
    def __init__(self, *extra):
        self.port = free_port()
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "mock_server.py"),
                                      "--port", str(self.port), "--text", TEXT, *extra],
                                     stdout=subprocess.PIPE)
        self.proc.stdout.readline()  # ready line
        self.base = "http://127.0.0.1:%d" % self.port

    def stats(self):
        with urllib.request.urlopen(self.base + "/stats") as r:
            return json.load(r)["requests"]

    def stop(self):
        self.proc.terminate()
        self.proc.wait()
        self.proc.stdout.close()


def settings(**kw):
    s = dict(core.DEFAULT_SETTINGS)
    s.update(kw)
    return s


def wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class PureTests(unittest.TestCase):
    def test_matches_trigger(self):
        self.assertTrue(core.matches_trigger("so the trigger phrase", "trigger phrase", False))
        self.assertTrue(core.matches_trigger("Trigger Phrase", "trigger phrase", False))
        self.assertFalse(core.matches_trigger("Trigger phrase", "trigger phrase", True))
        self.assertFalse(core.matches_trigger("trigger phrases", "trigger phrase", False))
        self.assertFalse(core.matches_trigger("phrase", "trigger phrase", False))

    def test_build_chat_request(self):
        url, body = core.build_request(settings(api_base_url="http://h:1/v1/", extra_body={"top_k": 5},
                                                user_prompt_template="{before}<CURSOR>{after}"),
                                       "SYS", "before {x}", "after")
        self.assertEqual(url, "http://h:1/v1/chat/completions")
        self.assertEqual(body["messages"], [{"role": "system", "content": "SYS"},
                                            {"role": "user", "content": "before {x}<CURSOR>after"}])
        self.assertTrue(body["stream"])
        self.assertEqual(body["top_k"], 5)

    def test_thinking_switch(self):
        _, body = core.build_request(settings(thinking=False), "", "a", "")
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        _, body = core.build_request(settings(thinking=True), "", "a", "")
        self.assertNotIn("chat_template_kwargs", body)
        # extra_body merges into nested dicts instead of replacing them
        _, body = core.build_request(settings(thinking=False, extra_body={"chat_template_kwargs": {"x": 1}}),
                                     "", "a", "")
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False, "x": 1})
        self.assertEqual(core.DEFAULT_SETTINGS["thinking_off_body"]["chat_template_kwargs"],
                         {"enable_thinking": False})  # defaults not mutated
        _, body = core.build_request(settings(thinking=False, api_mode="completions"), "", "a", "")
        self.assertNotIn("chat_template_kwargs", body)

    def test_build_completions_request(self):
        url, body = core.build_request(
            settings(api_mode="completions", completion_prompt_template="{system}\n{before}"), "SYS", "abc", "")
        self.assertEqual(url, "http://127.0.0.1:8080/v1/completions")
        self.assertEqual(body["prompt"], "SYS\nabc")
        self.assertNotIn("messages", body)

    def test_strip_think(self):
        self.assertEqual(core.strip_think("<think>hmm</think>\n\nHello"), "Hello")
        self.assertEqual(core.strip_think("<think>still thinking"), "")
        self.assertEqual(core.strip_think("<thi"), "")
        self.assertEqual(core.strip_think(" world"), " world")

    def test_settings_example_matches_defaults(self):
        with open(os.path.join(ROOT, "settings.example.json"), encoding="utf-8") as f:
            example = json.load(f)
        self.assertEqual(sorted(example), sorted(core.DEFAULT_SETTINGS))

    def test_config_reload(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = core.Config(d)
            self.assertEqual(cfg.settings["trigger_phrase"], "trigger phrase")
            self.assertEqual(cfg.system_prompt, "")
            with open(os.path.join(d, "settings.json"), "w") as f:
                json.dump({"trigger_phrase": "xyz", "log_file": ""}, f)
            with open(os.path.join(d, "SYSTEM.md"), "w") as f:
                f.write("be brief\n")
            self.assertEqual(cfg.settings["trigger_phrase"], "xyz")
            self.assertEqual(cfg.settings["model"], "local-model")
            self.assertEqual(cfg.system_prompt, "be brief")
            time.sleep(0.01)
            with open(os.path.join(d, "settings.json"), "w") as f:
                f.write("{broken")
            self.assertEqual(cfg.settings["trigger_phrase"], "xyz")  # keeps last good settings


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.srv = MockServer("--delay", "0.05")

    def tearDown(self):
        self.srv.stop()

    def job(self, **kw):
        return core.CompletionJob(settings(api_base_url=self.srv.base + "/v1", **kw), "SYS", "The fox ", "").start()

    def test_full_stream(self):
        job = self.job(api_key="secret")
        self.assertTrue(job.done.wait(5))
        self.assertIsNone(job.error)
        self.assertEqual(job.text(), TEXT)
        req = self.srv.stats()[0]
        self.assertTrue(req["finished"])
        self.assertEqual(req["auth"], "Bearer secret")
        self.assertEqual(req["body"]["messages"][1]["content"],
                         "<document>\nThe fox <CURSOR>\n</document>\n\nWrite the text to insert at <CURSOR>.")

    def test_completions_mode(self):
        job = self.job(api_mode="completions")
        self.assertTrue(job.done.wait(5))
        self.assertEqual(job.text(), TEXT)

    def test_partial_text_then_cancel_aborts_server(self):
        job = self.job()
        self.assertTrue(wait_for(lambda: len(job.text().split()) >= 3))
        partial = job.text()
        job.cancel()
        self.assertTrue(job.done.wait(2))
        self.assertTrue(TEXT.startswith(partial))
        self.assertLess(len(partial), len(TEXT))
        self.assertIsNone(job.error)
        self.assertTrue(wait_for(lambda: self.srv.stats()[0]["aborted"]), self.srv.stats())
        self.assertFalse(self.srv.stats()[0]["finished"])

    def test_cancel_before_first_token(self):
        job = self.job()
        job.cancel()
        self.assertTrue(job.done.wait(2))
        self.assertEqual(job.text(), "")

    def test_connection_refused(self):
        job = core.CompletionJob(settings(api_base_url="http://127.0.0.1:%d/v1" % free_port()), "", "x", "").start()
        self.assertTrue(job.done.wait(5))
        self.assertIsNotNone(job.error)
        self.assertEqual(job.text(), "")

    def test_http_error(self):
        job = core.CompletionJob(settings(api_base_url=self.srv.base + "/nope"), "", "x", "").start()
        self.assertTrue(job.done.wait(5))
        self.assertIn("HTTP 404", str(job.error))

    def test_non_streaming_response(self):
        job = self.job(extra_body={"stream": False})
        self.assertTrue(job.done.wait(5))
        self.assertEqual(job.text(), TEXT)


class ThinkTests(unittest.TestCase):
    def test_think_block_hidden(self):
        srv = MockServer("--delay", "0.01", "--think")
        try:
            job = core.CompletionJob(settings(api_base_url=srv.base + "/v1"), "", "x", "").start()
            self.assertTrue(job.done.wait(5))
            self.assertEqual(job.text(), TEXT)
            self.assertIn("<think>", job.raw_text())
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
