"""Tests for triggercomplete_server.  python3 -m unittest discover -s tests"""

import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "extension", "python", "pythonpath"))
import triggercomplete_server as server  # noqa: E402

MOCK = os.path.join(ROOT, "tools", "mock_server.py")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class ServerManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.port = free_port()
        self.url = "http://127.0.0.1:%d/v1" % self.port
        # env prefix like the real use case; "; echo" keeps sh alive as group leader with python as child
        self.cmd = "MOCK_FLAG=1 %s %s --port %d; echo server ended" % (
            shlex.quote(sys.executable), shlex.quote(MOCK), self.port)
        self.log = os.path.join(self.tmp.name, "server.log")
        self.extra = []

    def tearDown(self):
        for p in self.extra:
            p.kill()
            p.wait()
        subprocess.run(["pkill", "-f", "mock_server.py --port %d" % self.port])
        self.tmp.cleanup()

    def manager(self, client=None, command=None):
        return server.ServerManager(command or self.cmd, self.tmp.name, self.log, self.url, client)

    def fake_client(self):
        """A live process standing in for a second LibreOffice instance."""
        p = subprocess.Popen(["sleep", "60"])
        self.extra.append(p)
        return p, server.identity(p.pid)

    def up(self):
        return server.port_in_use(self.url)

    def state(self):
        with open(os.path.join(self.tmp.name, "server.json")) as f:
            return json.load(f)

    def test_start_and_stop(self):
        m = self.manager()
        m.acquire()
        self.assertTrue(m.registered)
        self.assertTrue(wait_for(self.up), "server did not come up")
        pgid = m.proc.pid
        self.assertEqual(self.state()["server"]["ident"][0], pgid)
        m.acquire()  # idempotent
        m.release()
        self.assertFalse(self.up())
        self.assertFalse(server._group_alive(pgid), "process group survived")
        self.assertIsNone(self.state()["server"])
        with open(self.log) as f:
            self.assertIn("starting: MOCK_FLAG=1", f.read())

    def test_second_instance_shares_server(self):
        a = self.manager()
        a.acquire()
        self.assertTrue(wait_for(self.up))
        _, ident = self.fake_client()
        b = self.manager(client=ident)
        b.acquire()
        self.assertIsNone(b.proc, "second instance started its own server")
        self.assertEqual(len(self.state()["clients"]), 2)
        a.release()
        self.assertTrue(self.up(), "server stopped while still in use")
        b.release()
        self.assertFalse(self.up())

    def test_crashed_instance_does_not_keep_server(self):
        a = self.manager()
        a.acquire()
        self.assertTrue(wait_for(self.up))
        p, ident = self.fake_client()
        self.manager(client=ident).acquire()
        p.kill()
        p.wait()
        a.release()
        self.assertFalse(self.up())

    def test_adopts_server_after_crash(self):
        _, ident = self.fake_client()
        crashed = self.manager(client=ident)
        crashed.acquire()  # stands in for an instance that died without releasing
        self.assertTrue(wait_for(self.up))
        self.extra[0].kill()
        self.extra[0].wait()
        b = self.manager()
        b.acquire()
        self.assertIsNone(b.proc)
        self.assertEqual(self.state()["clients"], [b.client])
        b.release()
        self.assertFalse(self.up())

    def test_foreign_server_left_alone(self):
        own = subprocess.Popen([sys.executable, MOCK, "--port", str(self.port)], stdout=subprocess.PIPE)
        self.extra.append(own)
        own.stdout.readline()
        m = self.manager()
        m.acquire()
        self.assertFalse(m.registered)
        self.assertIsNone(m.proc)
        m.release()
        self.assertIsNone(own.poll(), "foreign server was killed")
        own.stdout.close()

    def test_stale_state_file(self):
        with open(os.path.join(self.tmp.name, "server.json"), "w") as f:
            json.dump({"server": {"ident": [999999, "1"], "command": "x"}, "clients": [[999998, "1"]]}, f)
        m = self.manager()
        m.acquire()
        self.assertIsNotNone(m.proc)
        self.assertTrue(wait_for(self.up))
        m.release()
        self.assertFalse(self.up())

    def test_sigterm_ignored_then_killed(self):
        old = server.STOP_TIMEOUT
        server.STOP_TIMEOUT = 0.5
        try:
            m = self.manager(command="trap '' TERM; sleep 60; true")
            m.acquire()
            pgid = m.proc.pid
            time.sleep(0.2)
            m.release()
            self.assertTrue(wait_for(lambda: not server._group_alive(pgid), 2))
        finally:
            server.STOP_TIMEOUT = old


if __name__ == "__main__":
    unittest.main()
