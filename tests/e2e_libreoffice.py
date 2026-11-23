#!/usr/bin/env python3
"""End-to-end test: real LibreOffice Writer on Xvfb, keys typed with xdotool, mock LLM server.

Needs: soffice, unopkg, Xvfb, xdotool, python3 with the uno module. Run as a normal user
(unopkg refuses to run as root without --shared):

  python3 tests/e2e_libreoffice.py
"""

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import build  # noqa: E402

import uno  # noqa: E402

FULL = "jumps over the lazy dog and runs into the forest."
SYSTEM = "TEST SYSTEM PROMPT\nsecond line"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="tc-e2e-")
        self.procs = []
        self.display = ":%d" % (90 + os.getpid() % 100)
        self.env = dict(os.environ, DISPLAY=self.display, SAL_USE_VCLPLUGIN="gen")
        self.env.pop("TRIGGERCOMPLETE_DIR", None)
        self.env["XDG_RUNTIME_DIR"] = os.path.join(self.tmp, "run")  # isolates the server state file

    def start(self):
        self.mock_port = free_port()
        self.mock = "http://127.0.0.1:%d" % self.mock_port
        self.mock_pattern = "mock_server.py --port %d" % self.mock_port
        # LibreOffice starts the mock server itself through server_command
        server_command = "MOCK_FLAG=1 %s %s --port %d --text %s; echo server ended" % (
            shlex.quote(sys.executable), shlex.quote(os.path.join(ROOT, "tools", "mock_server.py")),
            self.mock_port, shlex.quote(FULL))
        os.makedirs(os.path.join(self.tmp, "run"))

        cfg = os.path.join(self.tmp, "cfg")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "settings.json"), "w") as f:
            json.dump({"api_base_url": self.mock + "/v1", "log_level": "debug",
                       "context_chars_after": 100, "server_command": server_command}, f)
        with open(os.path.join(cfg, "SYSTEM.md"), "w") as f:
            f.write(SYSTEM + "\n")
        self.log_path = os.path.join(cfg, "triggercomplete.log")

        oxt = build.build(cfg, os.path.join(self.tmp, "TriggerComplete.oxt"))
        profile = "file://" + os.path.join(self.tmp, "profile")
        subprocess.run(["unopkg", "add", "-env:UserInstallation=" + profile, oxt],
                       check=True, env=self.env, stdout=subprocess.DEVNULL)

        self.spawn(["Xvfb", self.display, "-screen", "0", "1280x900x24", "-nolisten", "tcp"])
        time.sleep(1)
        uno_port = free_port()
        self.spawn(["soffice", "-env:UserInstallation=" + profile, "--writer", "--norestore",
                    "--nologo", "--nofirststartwizard",
                    "--accept=socket,host=127.0.0.1,port=%d;urp;" % uno_port])

        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", local)
        for _ in range(120):
            try:
                ctx = resolver.resolve(
                    "uno:socket,host=127.0.0.1,port=%d;urp;StarOffice.ComponentContext" % uno_port)
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("soffice did not come up")
        desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        self.desktop = desktop
        for _ in range(60):
            doc = desktop.getCurrentComponent()
            if doc is not None and doc.supportsService("com.sun.star.text.TextDocument"):
                break
            time.sleep(0.5)
        self.doc = doc
        win = self.xdo("search", "--sync", "--onlyvisible", "--name", "Writer").split()[0]
        self.xdo("windowsize", win, "1200", "850")
        self.xdo("windowmove", win, "0", "0")
        self.xdo("windowfocus", "--sync", win)
        time.sleep(1)
        self.xdo("mousemove", "600", "450", "click", "1")  # put focus into the text area
        if not wait_until(self.server_up, 20):
            raise RuntimeError("LibreOffice did not start the server")

    def spawn(self, cmd, wait_line=False):
        p = subprocess.Popen(cmd, env=self.env, stdout=subprocess.PIPE if wait_line else subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        self.procs.append(p)
        if wait_line:
            p.stdout.readline()
        return p

    def server_up(self):
        try:
            with socket.create_connection(("127.0.0.1", self.mock_port), timeout=0.5):
                return True
        except OSError:
            return False

    def server_procs(self):
        out = subprocess.run(["pgrep", "-f", self.mock_pattern], capture_output=True, text=True).stdout
        return out.split()

    def plugin_log(self):
        with open(self.log_path) as f:
            return f.read()

    def xdo(self, *args):
        return subprocess.run(["xdotool", *args], env=self.env, check=True,
                              capture_output=True, text=True).stdout

    def type(self, s, delay=40):
        self.xdo("type", "--delay", str(delay), s)

    def key(self, *keys):
        self.xdo("key", *keys)

    def text(self):
        return self.doc.getText().getString()

    def reset(self, text="", delay=0.05):
        self.doc.getText().setString("")
        self.post("/reset", {})
        self.post("/config", {"text": text or FULL, "delay": delay})
        time.sleep(0.3)

    def post(self, path, obj):
        req = urllib.request.Request(self.mock + path, data=json.dumps(obj).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req).read()

    def requests(self):
        with urllib.request.urlopen(self.mock + "/stats") as r:
            return json.load(r)["requests"]

    def stop(self):
        try:
            self.doc.setModified(False)
        except Exception:
            pass
        subprocess.run(["pkill", "-f", getattr(self, "mock_pattern", "-nothing-")])
        for p in reversed(self.procs):
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
            if p.stdout:
                p.stdout.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def wait_until(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class Runner:
    def __init__(self, env):
        self.env = env
        self.failures = []

    def check(self, name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "   " + str(detail)))
        if not cond:
            self.failures.append(name)


def scenarios(e, r):
    print("accept full completion with Tab")
    e.reset()
    e.type("The quick brown trigger phrase")
    time.sleep(1.5)
    e.key("Tab")
    time.sleep(0.5)
    r.check("text", e.text() == "The quick brown " + FULL, repr(e.text()))
    reqs = e.requests()
    r.check("one request", len(reqs) == 1, reqs)
    if reqs:
        msgs = reqs[0]["body"]["messages"]
        r.check("system prompt from SYSTEM.md", msgs[0] == {"role": "system", "content": SYSTEM}, msgs)
        r.check("trigger removed from context",
                msgs[1]["content"].startswith("<document>\nThe quick brown <CURSOR>\n</document>"), msgs)
        r.check("request finished", reqs[0]["finished"], reqs[0])

    print("one Ctrl+Z undoes the whole completion")
    e.key("ctrl+z")
    time.sleep(0.5)
    r.check("text after undo", e.text() == "The quick brown trigger phrase", repr(e.text()))

    print("other key discards and aborts")
    e.reset(delay=0.2)
    e.type("Hello trigger phrase")
    time.sleep(0.6)
    e.type("x")
    time.sleep(0.3)
    r.check("text untouched", e.text() == "Hello trigger phrasex", repr(e.text()))
    r.check("server saw abort", wait_until(lambda: e.requests() and e.requests()[0]["aborted"]), e.requests())

    print("Tab mid-generation accepts the partial text and stops generation")
    e.reset(delay=0.2)
    e.type("A trigger phrase")
    time.sleep(1.0)
    e.key("Tab")
    time.sleep(0.3)
    t = e.text()
    r.check("partial prefix inserted", t.startswith("A jumps") and len(t) < len("A " + FULL)
            and FULL.startswith(t[2:]), repr(t))
    r.check("server saw abort", wait_until(lambda: e.requests() and e.requests()[0]["aborted"]), e.requests())

    print("Tab without trigger is a normal Tab")
    e.reset()
    e.type("abc")
    e.key("Tab")
    time.sleep(0.3)
    r.check("tab inserted", e.text() == "Abc\t", repr(e.text()))  # autocorrect capitalises
    r.check("no request", e.requests() == [], e.requests())

    print("Tab before anything arrived is swallowed, nothing changes")
    e.reset(delay=2.0)
    e.type("Slow trigger phrase")
    time.sleep(0.3)
    e.key("Tab")
    time.sleep(0.3)
    r.check("text untouched", e.text() == "Slow trigger phrase", repr(e.text()))

    print("mouse click discards")
    e.reset(delay=0.2)
    e.type("Click trigger phrase")
    time.sleep(0.5)
    e.xdo("click", "1")
    time.sleep(0.3)
    r.check("server saw abort", wait_until(lambda: e.requests() and e.requests()[0]["aborted"]), e.requests())

    print("capitalised trigger, multi-line completion, text after the cursor")
    e.reset(text="First line.\nSecond line.")
    e.doc.getText().setString("\nTAIL")
    e.key("ctrl+Home")
    e.type("Trigger phrase")
    time.sleep(1.0)
    e.key("Tab")
    time.sleep(0.3)
    r.check("paragraphs", e.text() == "First line.\nSecond line.\nTAIL", repr(e.text()))
    reqs = e.requests()
    if reqs:
        r.check("after-context sent", "\n<CURSOR>\nTAIL\n</document>" in reqs[0]["body"]["messages"][1]["content"],
                reqs[0]["body"]["messages"][1])

    print("fast typing (5 ms between keys)")
    e.reset()
    e.type("Fast trigger phrase", delay=5)
    time.sleep(1.5)
    e.key("Tab")
    time.sleep(0.3)
    r.check("accepted", e.text() == "Fast " + FULL, repr(e.text()))

    print("arrow keys back onto a trigger do not fire")
    e.reset()
    e.type("Nav trigger phrase.")  # fires once while typing, "." aborts it
    e.key("Left")
    time.sleep(1.0)
    reqs = e.requests()
    r.check("only the typed trigger fired", len(reqs) == 1 and reqs[0]["aborted"], reqs)

    print("settings.json is re-read live (new trigger phrase)")
    e.reset()
    cfg = os.path.join(os.path.dirname(e.log_path), "settings.json")
    with open(cfg) as f:
        s = json.load(f)
    s["trigger_phrase"] = ";;"
    with open(cfg, "w") as f:
        json.dump(s, f)
    e.type("Semi;;")
    time.sleep(1.0)
    e.key("Tab")
    time.sleep(0.3)
    r.check("new trigger works", e.text() == "Semi" + FULL, repr(e.text()))


def server_lifecycle(e, r):
    print("server_command: started once, stopped with the last Writer document and on exit")
    r.check("started by the plugin", e.plugin_log().count("started server") == 1, e.plugin_log())
    doc2 = e.desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, ())
    time.sleep(1)
    r.check("second document reuses it", e.plugin_log().count("started server") == 1)
    doc2.close(True)
    time.sleep(7)  # longer than the grace period
    r.check("still running while a document is open", e.server_up())

    e.doc.setModified(False)
    e.doc.close(True)
    r.check("stopped after the last document closed",
            wait_until(lambda: not e.server_up() and not e.server_procs(), 15), e.server_procs())

    e.doc = e.desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, ())
    r.check("started again for a new document", wait_until(e.server_up, 15))
    try:
        e.desktop.terminate()
    except Exception:
        pass  # the bridge goes away with LibreOffice
    r.check("stopped when LibreOffice exits",
            wait_until(lambda: not e.server_up() and not e.server_procs(), 15), e.server_procs())


def main():
    e = Env()
    r = Runner(e)
    try:
        e.start()
        scenarios(e, r)
        server_lifecycle(e, r)
    finally:
        log = ""
        if os.path.exists(getattr(e, "log_path", "")):
            with open(e.log_path) as f:
                log = f.read()
        e.stop()
    if "--show-log" in sys.argv or r.failures:
        print("\nplugin log:\n" + log)
    if r.failures:
        print("FAILED: %s" % ", ".join(r.failures))
        return 1
    print("\nall end-to-end checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
