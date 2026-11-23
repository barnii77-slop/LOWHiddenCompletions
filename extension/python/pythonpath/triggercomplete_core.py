"""Core of TriggerComplete: config, prompt building, streaming HTTP client.

No UNO imports here so it can be unit tested with a plain python3.
"""

import http.client
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse

LOG = logging.getLogger("triggercomplete")

DEFAULT_SETTINGS = {
    "trigger_phrase": "trigger phrase",
    "trigger_case_sensitive": False,
    "remove_trigger_on_accept": True,
    "api_base_url": "http://127.0.0.1:8080/v1",
    "api_key": "",
    "api_mode": "chat",
    "model": "local-model",
    "max_tokens": 200,
    "temperature": 0.7,
    "stop": [],
    "thinking": False,
    "thinking_off_body": {"chat_template_kwargs": {"enable_thinking": False}},
    "extra_body": {},
    "request_timeout_seconds": 120,
    "context_chars_before": 3000,
    "context_chars_after": 500,
    "user_prompt_template": "<document>\n{before}<CURSOR>{after}\n</document>\n\nWrite the text to insert at <CURSOR>.",
    "completion_prompt_template": "{before}",
    "strip_think_tags": True,
    "log_file": "triggercomplete.log",
    "log_level": "info",
    "server_command": "",
    "server_log_file": "server.log",
}


class Config:
    """settings.json and SYSTEM.md from one directory, reloaded when they change."""

    def __init__(self, config_dir):
        self.dir = config_dir
        self.settings_path = os.path.join(config_dir, "settings.json")
        self.system_path = os.path.join(config_dir, "SYSTEM.md")
        self._settings = dict(DEFAULT_SETTINGS)
        self._settings_mtime = -1
        self._system = ""
        self._system_mtime = -1
        self._log_target = None

    @staticmethod
    def _mtime(path):
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    @property
    def settings(self):
        mtime = self._mtime(self.settings_path)
        if mtime != self._settings_mtime:
            self._settings_mtime = mtime
            self._settings = self._load_settings(mtime is not None)
            self._setup_logging()
        return self._settings

    @property
    def system_prompt(self):
        mtime = self._mtime(self.system_path)
        if mtime != self._system_mtime:
            self._system_mtime = mtime
            try:
                with open(self.system_path, encoding="utf-8") as f:
                    self._system = f.read().strip()
            except OSError:
                self._system = ""
        return self._system

    def _load_settings(self, exists):
        merged = dict(DEFAULT_SETTINGS)
        if not exists:
            return merged
        try:
            with open(self.settings_path, encoding="utf-8") as f:
                user = json.load(f)
            if not isinstance(user, dict):
                raise ValueError("top level must be an object")
        except (OSError, ValueError) as e:
            LOG.error("cannot read %s, keeping previous settings: %s", self.settings_path, e)
            return self._settings
        unknown = sorted(k for k in user if k not in DEFAULT_SETTINGS and not k.startswith("_"))
        if unknown:
            LOG.warning("unknown keys in settings.json: %s", ", ".join(unknown))
        merged.update(user)
        return merged

    def _setup_logging(self):
        s = self._settings
        level = getattr(logging, str(s.get("log_level", "info")).upper(), logging.INFO)
        LOG.setLevel(level)
        name = s.get("log_file") or ""
        target = os.path.join(self.dir, name) if name else None
        if target == self._log_target:
            return
        for h in list(LOG.handlers):
            if getattr(h, "_triggercomplete", False):
                LOG.removeHandler(h)
                h.close()
        self._log_target = target
        if target:
            try:
                h = logging.FileHandler(target, encoding="utf-8")
            except OSError:
                return
            h._triggercomplete = True
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            LOG.addHandler(h)


def matches_trigger(text, trigger, case_sensitive):
    if not trigger or len(text) < len(trigger):
        return False
    tail = text[-len(trigger):]
    if case_sensitive:
        return tail == trigger
    return tail.casefold() == trigger.casefold()


def _fill(template, **values):
    # str.replace instead of str.format so braces in templates stay literal
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def _merge(dst, src):
    """Merge src into dst, recursing into nested dicts."""
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _merge(dst[key], value)
        else:
            dst[key] = json.loads(json.dumps(value))  # copy
    return dst


def build_request(settings, system_prompt, before, after):
    """Return (url, body) for the configured OpenAI-compatible endpoint."""
    base = str(settings["api_base_url"]).rstrip("/")
    body = {
        "model": settings["model"],
        "max_tokens": settings["max_tokens"],
        "temperature": settings["temperature"],
        "stream": True,
    }
    if settings.get("stop"):
        body["stop"] = settings["stop"]
    if settings.get("api_mode") == "completions":
        url = base + "/completions"
        body["prompt"] = _fill(settings["completion_prompt_template"],
                               system=system_prompt, before=before, after=after)
    else:
        url = base + "/chat/completions"
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": _fill(
            settings["user_prompt_template"], before=before, after=after)})
        body["messages"] = messages
        if not settings.get("thinking", True):
            _merge(body, settings.get("thinking_off_body") or {})
    _merge(body, settings.get("extra_body") or {})
    return url, body


_THINK_RE = re.compile(r"\A\s*<think>.*?</think>\s*", re.S)


def strip_think(text):
    """Drop a leading <think>...</think> block, or everything if it is still open."""
    stripped = text.lstrip()
    if not stripped:
        return text
    if "<think>".startswith(stripped[:7]) and len(stripped) < 7:
        return ""  # could be the start of "<think>"
    if not stripped.startswith("<think>"):
        return text
    m = _THINK_RE.match(text)
    return text[m.end():] if m else ""


def _extract_piece(obj, mode):
    if "error" in obj:
        err = obj["error"]
        raise RuntimeError(err.get("message", err) if isinstance(err, dict) else err)
    choices = obj.get("choices") or []
    if not choices:
        return ""
    c = choices[0]
    if mode == "completions":
        return c.get("text") or ""
    delta = c.get("delta") or c.get("message") or {}
    return delta.get("content") or ""


class CompletionJob:
    """One streaming request running in a daemon thread.

    text() returns what has arrived so far; cancel() closes the socket so the
    server notices the disconnect and stops generating.
    """

    def __init__(self, settings, system_prompt, before, after):
        self.mode = "completions" if settings.get("api_mode") == "completions" else "chat"
        self.url, self.body = build_request(settings, system_prompt, before, after)
        self.api_key = settings.get("api_key") or ""
        self.timeout = float(settings.get("request_timeout_seconds") or 120)
        self.strip_think = bool(settings.get("strip_think_tags"))
        self.error = None
        self.done = threading.Event()
        self.started = time.monotonic()
        self._chunks = []
        self._lock = threading.Lock()
        self._cancelled = False
        self._conn = None
        self._thread = threading.Thread(target=self._run, name="triggercomplete", daemon=True)

    def start(self):
        self._thread.start()
        return self

    @property
    def cancelled(self):
        return self._cancelled

    def raw_text(self):
        with self._lock:
            return "".join(self._chunks)

    def text(self):
        t = self.raw_text()
        return strip_think(t) if self.strip_think else t

    def cancel(self):
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            conn = self._conn
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _run(self):
        try:
            self._request()
        except Exception as e:
            if not self._cancelled:
                self.error = e
                LOG.warning("request failed after %.2fs: %s", time.monotonic() - self.started, e)
        finally:
            conn, self._conn = self._conn, None
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self.done.set()

    def _request(self):
        u = urllib.parse.urlsplit(self.url)
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=self.timeout,
                                               context=ssl.create_default_context())
        elif u.scheme == "http":
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=self.timeout)
        else:
            raise ValueError("unsupported URL scheme in api_base_url: %r" % self.url)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key

        with self._lock:
            if self._cancelled:
                return
            self._conn = conn
        conn.connect()
        if self._cancelled:  # cancel() may have run while connecting
            return
        conn.request("POST", path, body=json.dumps(self.body).encode("utf-8"), headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            detail = resp.read(2000).decode("utf-8", "replace")
            raise RuntimeError("HTTP %d from %s: %s" % (resp.status, self.url, detail))

        if "text/event-stream" not in (resp.getheader("Content-Type") or ""):
            # server ignored stream=true
            self._append(_extract_piece(json.loads(resp.read().decode("utf-8")), self.mode))
            return

        while not self._cancelled:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            self._append(_extract_piece(json.loads(data.decode("utf-8")), self.mode))
        if not self._cancelled:
            LOG.info("completion finished in %.2fs, %d chars",
                     time.monotonic() - self.started, len(self.raw_text()))

    def _append(self, piece):
        if piece:
            with self._lock:
                if not self._cancelled:
                    self._chunks.append(piece)
