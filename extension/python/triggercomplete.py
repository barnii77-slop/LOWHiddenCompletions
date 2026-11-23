"""TriggerComplete: LibreOffice Writer AI autocomplete behind a trigger phrase.

Flow per Writer view:
  typing the trigger phrase  -> start a streaming request in the background
  next key is Tab            -> insert what has arrived so far, stop the request
  next key is anything else  -> drop the completion, stop the request
A mouse click counts as "anything else".
"""

import os
import threading
import time

import uno
import unohelper
from com.sun.star.awt import XCallback, XKeyHandler, XMouseClickHandler
from com.sun.star.awt.Key import BACKSPACE, TAB
from com.sun.star.awt.KeyModifier import MOD1
from com.sun.star.awt.MessageBoxButtons import BUTTONS_OK
from com.sun.star.document import XDocumentEventListener
from com.sun.star.frame import XTerminateListener
from com.sun.star.task import XJob
from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK

import triggercomplete_core as core
import triggercomplete_server as server

EXTENSION_ID = "org.triggercomplete"
IMPL_NAME = "org.triggercomplete.Job"
LOG = core.LOG

# How long to wait for Writer to flush typed characters into the document.
# Writer buffers keystrokes while more keyboard input is queued.
CONFIRM_DELAYS = (0.03, 0.1, 0.3)

# Grace period before stopping the server after the last Writer document closed,
# so that opening the next document right away does not restart it.
SERVER_STOP_DELAY = 5.0

_sessions = []
_globals = {"config": None, "doc_listener": None, "ctx": None, "server": None, "terminate_listener": None}


def _config_dir():
    env = os.environ.get("TRIGGERCOMPLETE_DIR")
    if env:
        return os.path.expanduser(env)
    ext_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(ext_root, "config_dir.txt"), encoding="utf-8") as f:
            d = f.read().strip()
        if d:
            return os.path.expanduser(d)
    except OSError:
        pass
    return os.path.join(os.path.expanduser("~"), ".config", "triggercomplete")


def _config():
    if _globals["config"] is None:
        _globals["config"] = core.Config(_config_dir())
        _globals["config"].settings  # sets up logging
        LOG.info("TriggerComplete loaded, config dir %s", _globals["config"].dir)
    return _globals["config"]


class _Callback(unohelper.Base, XCallback):
    def __init__(self, fn):
        self.fn = fn

    def notify(self, data):
        try:
            self.fn()
        except Exception:
            LOG.exception("callback failed")


def _run_on_main_thread_later(delay, fn):
    """Run fn on the LibreOffice main thread after delay seconds."""
    ctx = _globals["ctx"]

    def post():
        try:
            cb = ctx.ServiceManager.createInstanceWithContext("com.sun.star.awt.AsyncCallback", ctx)
            cb.addCallback(_Callback(fn), None)
        except Exception:
            LOG.exception("cannot post callback")

    t = threading.Timer(delay, post)
    t.daemon = True
    t.start()


class Session(unohelper.Base, XKeyHandler, XMouseClickHandler):
    """Key and mouse interception for one Writer view."""

    def __init__(self, model, controller):
        self.model = model
        self.controller = controller
        self.pending = None      # (CompletionJob, trigger text as found in the document)
        self.typed = ""          # characters typed since the last non-character key
        self.seq = 0             # bumps on every key press or click
        controller.addKeyHandler(self)
        controller.addMouseClickHandler(self)

    def detach(self):
        self._discard("view closed")
        for remove in (self.controller.removeKeyHandler, self.controller.removeMouseClickHandler):
            try:
                remove(self)
            except Exception:
                pass

    # ---- XKeyHandler

    def keyPressed(self, ev):
        try:
            self.seq += 1
            if self.pending is not None:
                if ev.KeyCode == TAB and ev.Modifiers == 0:
                    return self._accept()
                self._discard("key")
            self._track_typing(ev)
        except Exception:
            LOG.exception("keyPressed failed")
        return False

    def keyReleased(self, ev):
        try:
            if self.pending is None and self.typed:
                s = _config().settings
                if core.matches_trigger(self.typed, s["trigger_phrase"], s["trigger_case_sensitive"]):
                    self.typed = ""
                    self._confirm_trigger(self.seq, 0)
        except Exception:
            LOG.exception("keyReleased failed")
        return False

    # ---- XMouseClickHandler

    def mousePressed(self, ev):
        self.seq += 1
        self.typed = ""
        if self.pending is not None:
            self._discard("mouse click")
        return False

    def mouseReleased(self, ev):
        return False

    # ---- XEventListener

    def disposing(self, source):
        self._discard("disposed")
        _remove_session(self)

    # ---- internals

    def _track_typing(self, ev):
        ch = getattr(ev.KeyChar, "value", ev.KeyChar)  # pyuno may wrap chars in uno.Char
        if ch and ch >= " " and ch != "\x7f" and not (ev.Modifiers & MOD1):
            self.typed = (self.typed + ch)[-200:]
        elif ev.KeyCode == BACKSPACE:
            self.typed = self.typed[:-1]
        else:
            self.typed = ""

    def _confirm_trigger(self, seq, attempt):
        """Start a completion once the document really ends with the trigger at the cursor."""
        if seq != self.seq or self.pending is not None:
            return  # another key or click came first
        s = _config().settings
        trigger = s["trigger_phrase"]
        ctx = self._context(len(trigger) + int(s["context_chars_before"]), int(s["context_chars_after"]))
        if ctx is None:
            return
        before, after = ctx
        if not core.matches_trigger(before, trigger, s["trigger_case_sensitive"]):
            if attempt < len(CONFIRM_DELAYS):
                LOG.debug("trigger typed but not yet in the document, re-checking (attempt %d)", attempt + 1)
                _run_on_main_thread_later(CONFIRM_DELAYS[attempt],
                                          lambda: self._confirm_trigger(seq, attempt + 1))
            return
        found = before[-len(trigger):]
        if s["remove_trigger_on_accept"]:
            before = before[:-len(trigger)]
        before = before[-int(s["context_chars_before"]):] if int(s["context_chars_before"]) > 0 else ""
        job = core.CompletionJob(s, _config().system_prompt, before, after).start()
        self.pending = (job, found)
        LOG.info("trigger detected, request started (%d chars before, %d after)", len(before), len(after))

    def _context(self, n_before, n_after):
        vc = self.controller.getViewCursor()
        if not vc.isCollapsed():
            return None
        text = vc.getText()
        pos = vc.getStart()
        cur = text.createTextCursorByRange(pos)
        cur.goLeft(min(n_before, 32767), True)
        before = cur.getString()
        after = ""
        if n_after > 0:
            cur = text.createTextCursorByRange(pos)
            cur.goRight(min(n_after, 32767), True)
            after = cur.getString()
        return before[-n_before:], after[:n_after]

    def _discard(self, why):
        if self.pending is None:
            return
        job, _ = self.pending
        self.pending = None
        job.cancel()
        LOG.info("completion discarded (%s) after %.2fs", why, _elapsed(job))

    def _accept(self):
        """Handle Tab while a completion is pending. Returns True to swallow the key."""
        job, found = self.pending
        self.pending = None
        completion = job.text().replace("\r\n", "\n").replace("\r", "\n")
        job.cancel()
        s = _config().settings
        vc = self.controller.getViewCursor()
        text = vc.getText()
        cur = text.createTextCursorByRange(vc.getStart())
        cur.goLeft(len(found), True)
        if not vc.isCollapsed() or cur.getString() != found:
            LOG.info("cursor moved away from the trigger, Tab passed through")
            return False
        if job.error is not None and not completion:
            LOG.info("Tab ignored: request failed (%s)", job.error)
            return True
        if not completion:
            LOG.info("Tab ignored: nothing generated yet after %.2fs", _elapsed(job))
            return True

        undo = self.model.getUndoManager()
        undo.enterUndoContext("AI completion")
        try:
            if s["remove_trigger_on_accept"]:
                cur.setString("")
            cur.collapseToEnd()
            probe = text.createTextCursorByRange(cur)
            probe.goLeft(1, True)
            if probe.getString() in (" ", "\t", " "):
                completion = completion.lstrip(" \t")
            for i, part in enumerate(completion.split("\n")):
                if i:
                    text.insertControlCharacter(cur, PARAGRAPH_BREAK, False)
                if part:
                    text.insertString(cur, part, False)
            vc.gotoRange(cur.getEnd(), False)
        finally:
            undo.leaveUndoContext()
        LOG.info("completion accepted after %.2fs, %d chars%s", _elapsed(job), len(completion),
                 "" if job.done.is_set() else " (generation stopped)")
        return True


def _elapsed(job):
    return time.monotonic() - job.started


def _attach(model):
    try:
        if model is None or not model.supportsService("com.sun.star.text.TextDocument"):
            return
        controllers = model.getControllers()
        while controllers.hasMoreElements():
            c = controllers.nextElement()
            if any(sess.controller == c for sess in _sessions):
                continue
            _sessions.append(Session(model, c))
            LOG.info("attached to %s", model.getURL() or "new document")
            _server_acquire()
    except Exception:
        LOG.exception("attach failed")


def _remove_session(sess):
    if sess in _sessions:
        _sessions.remove(sess)
        if not _sessions:
            _run_on_main_thread_later(SERVER_STOP_DELAY, _server_release_if_unused)


def _in_background(fn):
    def run():
        try:
            fn()
        except Exception:
            LOG.exception("server management failed")
    threading.Thread(target=run, daemon=True).start()


def _server_acquire():
    """Start server_command (or join the running one) for this LibreOffice process."""
    cfg = _config()
    s = cfg.settings
    cmd = (s.get("server_command") or "").strip()
    if not cmd:
        return
    mgr = _globals["server"]
    if mgr is None or (not mgr.registered and mgr.command != cmd):
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        state_dir = os.path.join(runtime, "triggercomplete") if runtime else cfg.dir
        log_name = s.get("server_log_file") or ""
        mgr = server.ServerManager(cmd, state_dir, os.path.join(cfg.dir, log_name) if log_name else None,
                                   probe_url=s["api_base_url"])
        _globals["server"] = mgr
    if not mgr.registered:
        _in_background(mgr.acquire)


def _server_release_if_unused():
    mgr = _globals["server"]
    if not _sessions and mgr is not None and mgr.registered:
        LOG.info("no Writer document open any more")
        _in_background(mgr.release)


class _TerminateListener(unohelper.Base, XTerminateListener):
    def queryTermination(self, ev):
        pass

    def notifyTermination(self, ev):
        mgr = _globals["server"]
        if mgr is not None:
            try:
                mgr.release()  # synchronous: the process is about to exit
            except Exception:
                LOG.exception("stopping the server failed")

    def disposing(self, ev):
        pass


class _DocListener(unohelper.Base, XDocumentEventListener):
    def documentEventOccured(self, ev):
        try:
            name = ev.EventName
            if name in ("OnViewCreated", "OnNew", "OnLoad"):
                _attach(ev.Source)
            elif name in ("OnViewClosed", "OnUnload"):
                # OnViewClosed: drop sessions whose controller is gone
                for sess in list(_sessions):
                    if sess.model == ev.Source and (name == "OnUnload" or not _alive(sess)):
                        sess.detach()
                        _remove_session(sess)
        except Exception:
            LOG.exception("document event handling failed")

    def disposing(self, source):
        pass


def _alive(sess):
    try:
        return sess.controller.getFrame() is not None
    except Exception:
        return False


def _warn_missing_config_dir(ctx):
    """Tell the user once at startup when the config directory does not exist."""
    cfg_dir = _config().dir
    if os.path.isdir(cfg_dir):
        return
    desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    frame = desktop.getCurrentFrame()
    if frame is None and desktop.getFrames().getCount() > 0:
        frame = desktop.getFrames().getByIndex(0)
    if frame is None:
        LOG.warning("config directory %s does not exist", cfg_dir)
        return
    win = frame.getContainerWindow()
    box = win.getToolkit().createMessageBox(
        win, uno.Enum("com.sun.star.awt.MessageBoxType", "WARNINGBOX"), BUTTONS_OK, "TriggerComplete",
        "TriggerComplete cannot find its config directory:\n%s\n\n"
        "It should contain settings.json and SYSTEM.md. Rebuild the extension in the repository "
        "directory with \"make install\", or set TRIGGERCOMPLETE_DIR." % cfg_dir)
    box.execute()


class TriggerCompleteJob(unohelper.Base, XJob):
    """Started by LibreOffice on startup (Jobs.xcu); hooks every Writer view."""

    def __init__(self, ctx):
        self.ctx = ctx

    def execute(self, args):
        _globals["ctx"] = self.ctx
        _config()
        if _globals["doc_listener"] is None:
            listener = _DocListener()
            broadcaster = self.ctx.getValueByName("/singletons/com.sun.star.frame.theGlobalEventBroadcaster")
            broadcaster.addDocumentEventListener(listener)
            _globals["doc_listener"] = listener
            desktop = self.ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)
            _globals["terminate_listener"] = _TerminateListener()
            desktop.addTerminateListener(_globals["terminate_listener"])
            comps = desktop.getComponents().createEnumeration()
            while comps.hasMoreElements():
                _attach(comps.nextElement())
            # deferred: during onFirstVisibleTask no frame is active yet
            _run_on_main_thread_later(0.5, lambda: _warn_missing_config_dir(self.ctx))
        return None


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(TriggerCompleteJob, IMPL_NAME, ("com.sun.star.task.Job",))
