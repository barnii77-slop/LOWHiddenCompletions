"""Start and stop the LLM server command, shared by all LibreOffice processes of this user.

A JSON state file (guarded by flock) records the server process and the LibreOffice
processes ("clients") using it. The first client starts the server, the last one to
release it stops it. Dead clients are pruned, so a crashed LibreOffice does not keep
the server alive forever: the next one adopts it and stops it later.
"""

import json
import logging
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.parse

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

LOG = logging.getLogger("triggercomplete")
HAVE_PROC = os.path.isdir("/proc/self")
STOP_TIMEOUT = 5.0


def _stat(pid):
    """(state, starttime) from /proc, or None."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            data = f.read()
    except OSError:
        return None
    fields = data[data.rindex(")") + 2:].split()
    return fields[0], fields[19]


def identity(pid):
    """[pid, start time]: the start time guards against reused PIDs."""
    st = _stat(pid)
    return [pid, st[1] if st else None]


def is_alive(ident):
    pid, start = ident
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    st = _stat(pid)
    if st is None:
        return not HAVE_PROC
    state, starttime = st
    if state == "Z":
        return False
    return start is None or starttime == start


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def port_in_use(url):
    u = urllib.parse.urlsplit(url)
    if not u.hostname:
        return False
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        with socket.create_connection((u.hostname, port), timeout=0.5):
            return True
    except OSError:
        return False


class _FileLock:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.f = open(self.path, "a+")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


class ServerManager:
    def __init__(self, command, state_dir, log_path=None, probe_url=None, client=None):
        self.command = command
        os.makedirs(state_dir, exist_ok=True)
        self.state_path = os.path.join(state_dir, "server.json")
        self.lock_path = self.state_path + ".lock"
        self.log_path = log_path
        self.probe_url = probe_url
        self.client = client or identity(os.getpid())
        self.registered = False
        self.proc = None
        self._lock = threading.Lock()

    # ---- public

    def acquire(self):
        """Register this process as a user of the server, starting it if needed."""
        if fcntl is None:
            LOG.warning("server_command is only supported on Linux and macOS")
            return
        with self._lock:
            if self.registered:
                return
            with _FileLock(self.lock_path):
                state = self._read()
                srv = state["server"]
                if srv is not None and not is_alive(srv["ident"]):
                    srv = None
                if srv is None:
                    if self.probe_url and port_in_use(self.probe_url):
                        LOG.info("something already listens at %s, not starting server_command", self.probe_url)
                        self._write(None, state["clients"])
                        return
                    srv = self._spawn()
                else:
                    LOG.info("using the server started earlier (pid %d)", srv["ident"][0])
                self._write(srv, state["clients"] + [self.client])
                self.registered = True

    def release(self):
        """Unregister this process; stop the server if no other process uses it."""
        with self._lock:
            if not self.registered:
                return
            self.registered = False
            with _FileLock(self.lock_path):
                state = self._read()
                clients = [c for c in state["clients"] if c != self.client]
                srv = state["server"]
                if srv is not None and not clients:
                    self._stop(srv["ident"][0])
                    srv = None
                elif srv is not None:
                    LOG.info("server still used by %d other LibreOffice process(es)", len(clients))
                self._write(srv, clients)

    # ---- internals

    def _read(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        clients = [c for c in state.get("clients", []) if is_alive(c)]
        return {"server": state.get("server"), "clients": clients}

    def _write(self, server, clients):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"server": server, "clients": clients}, f)
        os.replace(tmp, self.state_path)

    def _spawn(self):
        # LibreOffice builds with a bundled Python set these; they would break Python tools in the command
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}
        out = subprocess.DEVNULL
        if self.log_path:
            out = open(self.log_path, "ab")
            out.write(("\n=== %s starting: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), self.command)).encode())
            out.flush()
        try:
            proc = subprocess.Popen(self.command, shell=True, env=env, cwd=os.path.expanduser("~"),
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        finally:
            if out is not subprocess.DEVNULL:
                out.close()
        self.proc = proc
        threading.Thread(target=self._watch, args=(proc,), daemon=True).start()
        LOG.info("started server (pid %d): %s", proc.pid, self.command)
        return {"ident": identity(proc.pid), "command": self.command}

    def _watch(self, proc):
        code = proc.wait()  # also reaps the child
        LOG.info("server process %d exited with code %s", proc.pid, code)

    def _stop(self, pgid):
        LOG.info("stopping server (pid %d)", pgid)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + STOP_TIMEOUT
        while time.monotonic() < deadline:
            if not _group_alive(pgid):
                return
            time.sleep(0.05)
        LOG.warning("server did not stop within %gs, killing it", STOP_TIMEOUT)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
