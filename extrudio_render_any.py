# /// script
# requires-python = ">=3.12"
#
# [tool.orcaslicer.plugin]
# name = "Render in Extrudio"
# description = "After a slice, see your print in your filament, in studio and room scenes. You approve each send on Extrudio's page, inside OrcaSlicer."
# author = "Extrudio"
# version = "1.2.0"
# ///
"""Render in Extrudio, an OrcaSlicer 2.5 plugin.

After a slice, a notification offers "Render in Extrudio". A click asks Extrudio for a hand-off
and opens Extrudio's page in a panel beside the 3D view. There the person signs in and approves
the send, with a code this plugin hands the page through Orca's own bridge (so a link opened
anywhere else can't approve it). Only then does the plugin get one upload URL, for that person's
account, and send the sliced G-code to it.

- Standard library only: nothing is fetched from PyPI.
- No key, no settings but the notification switch, no telemetry.
- It reads only the G-code Orca writes after a slice, and talks only to Extrudio and its storage.
- It ignores every message from the page, and opens no page but Extrudio's own.
- When a send fails, it says why in one line on stderr, which Orca keeps in its log folder
  (python_<date>.log): the step and a status or an error's kind, never a code, a secret, a link or
  a path.

The flow and its security review: the Extrudio app's docs/spikes/orca-plugin.md and its DECISIONS,
2026-11-20.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import orca

VERSION = "1.2.0"
# Extrudio, and its storage (the engine's signed upload URLs, on the app's own Supabase project).
APP = "https://app.extrudio.site"
STORAGE = "https://cpavinhltwobpnblhuym.supabase.co"
# A development build points both at a local app instead (the plugin's README).
_DEV = os.environ.get("EXTRUDIO_APP_URL", "").rstrip("/")
if _DEV.startswith(("http://localhost:", "http://127.0.0.1:")):
    APP = STORAGE = _DEV

HANDOFFS = APP + "/api/slicer/handoffs"
# The only page the panel opens: Extrudio's own, for the hand-off the plugin asked for.
PAGE = APP + "/from-orca"
USER_AGENT = "ExtrudioOrcaPlugin/" + VERSION
POLL_SECONDS = 1.0
LIFETIME_SECONDS = 10 * 60
# How long, and how often, the plugin hands the page its bridge secret as the page loads.
BRIDGE_SECONDS = 15.0
BRIDGE_EVERY = 0.5

TEXT = {
    "sliced": "Sliced. See it in your filament, in a real scene.",
    "render": "Render in Extrudio",
    "approve": "Approve the code %s on Extrudio's page to send %s.",
    "busy": "A send to Extrudio is already under way.",
    "no_answer": "Extrudio didn't answer. Try again in a moment.",
    "binary": "Binary G-code isn't supported. Turn it off in the printer settings, then slice again.",
    "too_large": "This file is over %d MB.",
    "slice_first": "Slice first, then Render in Extrudio.",
    "panel_title": "Extrudio",
    "opening": "Opening Extrudio…",
    "notifications_on": "Render in Extrudio: notifications on",
    "notifications_off": "Render in Extrudio: notifications off",
}


def temp_dir():
    """The temporary folder as Orca finds it: wxWidgets' GetTempDir, which reads TMPDIR, then TMP,
    then TEMP, and falls back to the system's (Windows) or /tmp. Python's own tempfile reads TEMP
    before TMP, so the plugin follows wx instead."""
    for name in ("TMPDIR", "TMP", "TEMP"):
        value = os.environ.get(name, "")
        if value:
            return value.rstrip("/\\")
    if os.name == "nt":
        import tempfile

        return tempfile.gettempdir().rstrip("/\\")
    return "/tmp"


def slices_root():
    """Where Orca 2.5 writes each slice's G-code, under
    <temp>/orcaslicer_<uid>/orcaslicer_model/<day>/<time>#<pid>#<id>/Metadata/.<pid>.<n>.gcode
    on macOS and Linux, and with no <uid> level on Windows (Orca's per_user_temp_dir)."""
    if os.name == "nt":
        return os.path.join(temp_dir(), "orcaslicer_model")
    return "%s/orcaslicer_%d/orcaslicer_model" % (temp_dir(), os.getuid())


def log(step, what):
    """One line on stderr, which Orca keeps in its log folder: the step that failed and a status or
    an error's kind. Never a code, a secret, a link or a path."""
    try:
        sys.stderr.write("[Render in Extrudio %s] %s: %s\n" % (VERSION, step, what))
        sys.stderr.flush()
    except Exception:
        pass


def kind(error):
    """An error's kind, without its message, which can carry a link or a path."""
    if isinstance(error, urllib.error.HTTPError):
        return "HTTP %d" % error.code
    if isinstance(error, urllib.error.URLError) and isinstance(error.reason, BaseException):
        return "%s (%s)" % (type(error).__name__, type(error.reason).__name__)
    return type(error).__name__


def is_our_page(url):
    """Extrudio's own page for a hand-off, as the app gives it: /from-orca, then ? or # or nothing."""
    return isinstance(url, str) and (
        url == PAGE or url.startswith(PAGE + "?") or url.startswith(PAGE + "#")
    )


def call(path, body):
    """A JSON POST to Extrudio's hand-off routes: the answer's status and body."""
    request = urllib.request.Request(
        HANDOFFS + path,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read().decode("utf-8") or "{}")
        except ValueError:
            return error.code, {}


def settings_path():
    # The plugin's own folder in Orca's data folder: the one place it writes, the switch only.
    return os.path.join(str(orca.host.plugin.storage()), "settings.json")


def notifications_on():
    try:
        with open(settings_path(), encoding="utf-8") as file:
            return json.load(file).get("notify", True) is not False
    except (OSError, ValueError, AttributeError):
        return True


def set_notifications(on):
    with open(settings_path(), "w", encoding="utf-8") as file:
        json.dump({"notify": bool(on)}, file)


def is_binary(path):
    with open(path, "rb") as file:
        return file.read(4) == b"GCDE"


def notify(text, link=None, on_click=None, level=None):
    ui = orca.host.ui
    orca.host.ui.push_notification(
        level or ui.NotificationLevel.RegularNotificationLevel,
        text,
        hyper_text=link or "",
        on_click=on_click,
    )


PANEL = """
<style>body { margin: 0; padding: 16px; font: 13px system-ui; }</style>
<p>%s</p>
<script>location.replace(%s);</script>
"""


class RenderInExtrudio(orca.script.ScriptPluginCapabilityBase):
    last = None  # the latest slice: {"path", "bytes"}
    offered = None  # the project the notification was last offered for
    panel = None
    busy = False

    def get_name(self):
        return TEXT["render"]

    # Speed Dial and the Plugins dialog: the same as the notification's link.
    def execute(self):
        if self.last is None:
            return orca.ExecutionResult.success(TEXT["slice_first"])
        self.start()
        return orca.ExecutionResult.success("")

    def on_lifecycle_event(self, event, ctx):
        if event == orca.LifecycleEvent.GCodeExportFinished:
            # The slicing worker: note the file only. No UI from here, which can deadlock.
            if ctx.code != orca.LifecycleEvtCode.Ok or not ctx.msg:
                return
            try:
                self.last = {"path": ctx.msg, "bytes": os.path.getsize(ctx.msg)}
            except OSError:
                self.last = None
        elif event == orca.LifecycleEvent.SlicingJobComplete:
            # The UI thread: offer the render once a project, unless it's switched off.
            if self.last is None or not notifications_on():
                return
            project = self.project_name()
            if project == self.offered:
                return
            self.offered = project
            notify(TEXT["sliced"], TEXT["render"], self.start)

    def project_name(self):
        """The project's name, else its first object's, else "print" (UI thread only)."""
        try:
            path = orca.host.plater().project_path()
            if path:
                name = os.path.basename(path)
                return name[: -len(".3mf")] if name.lower().endswith(".3mf") else name
        except Exception:
            pass
        try:
            for item in orca.host.model().objects():
                if item.name:
                    return os.path.splitext(item.name)[0]
        except Exception:
            pass
        return "print"

    # The notification's link, on the UI thread.
    def start(self):
        if self.busy:
            notify(TEXT["busy"])
            return True
        if self.last is None:
            notify(TEXT["slice_first"])
            return True
        file = dict(self.last)
        try:
            if is_binary(file["path"]):
                notify(TEXT["binary"])
                return True
        except OSError:
            notify(TEXT["slice_first"])
            return True
        name = (self.project_name() or "print")[:200] + ".gcode"
        try:
            status, handoff = call("", {"fileName": name, "bytes": file["bytes"]})
        except (urllib.error.URLError, OSError, ValueError) as error:
            log("start", kind(error))
            status, handoff = 0, {}
        if status == 413:
            # Over Extrudio's limit, which the server states: the plugin keeps no number of its own.
            cap = ((handoff.get("error") or {}).get("params") or {}).get("maxBytes")
            if isinstance(cap, int) and cap > 0:
                notify(TEXT["too_large"] % (cap // (1024 * 1024)))
                return True
        if status != 201:
            if status:
                log("start", "answered %d" % status)
            notify(TEXT["no_answer"])
            return True
        if not all(isinstance(handoff.get(key), str) for key in ("code", "bridge", "shortCode")):
            log("start", "an answer without its codes")
            notify(TEXT["no_answer"])
            return True
        if not is_our_page(handoff.get("url")):
            # The panel opens Extrudio's own page only, whatever an answer says.
            log("start", "an answer with another page")
            notify(TEXT["no_answer"])
            return True
        self.busy = True
        notify(TEXT["approve"] % (handoff["shortCode"], name))
        self.open_page(handoff["url"])
        threading.Thread(
            target=self.send, args=(handoff["code"], handoff["bridge"], file), daemon=True
        ).start()
        return True

    def open_page(self, url):
        if self.panel is not None and self.panel.is_open():
            self.panel.close()
        self.panel = orca.host.ui.create_dock_panel(
            html=PANEL % (TEXT["opening"], json.dumps(url)),
            title=TEXT["panel_title"],
            width=440,
            height=760,
            # The page never instructs the plugin: every message from it is ignored.
            on_message=lambda message: None,
            on_close=self.on_close,
            dock="right",
        )

    def on_close(self):
        self.panel = None

    # A worker thread: hand the page its bridge secret, wait for the approval, then send the file.
    def send(self, code, bridge, file):
        try:
            started = time.time()
            deadline = started + LIFETIME_SECONDS
            upload = None
            while time.time() < deadline:
                if self.panel is not None and time.time() - started < BRIDGE_SECONDS:
                    # Orca marshals post() onto its UI thread; the page keeps the first one.
                    self.panel.post({"bridge": bridge})
                status, answer = call("/status", {"code": code})
                if status != 200:
                    log("status", "answered %d" % status)
                    return
                if answer.get("state") == "refused":
                    log("status", "refused on Extrudio's page")
                    return
                if answer.get("upload"):
                    upload = answer["upload"]
                    break
                time.sleep(BRIDGE_EVERY if time.time() - started < BRIDGE_SECONDS else POLL_SECONDS)
            if upload is None:
                log("approval", "none within %d minutes" % (LIFETIME_SECONDS // 60))
                return
            if not str(upload.get("url", "")).startswith((STORAGE + "/", APP + "/")):
                log("upload", "an upload link to another host")
                return
            for attempt in range(2):
                try:
                    with open(file["path"], "rb") as body:
                        request = urllib.request.Request(
                            upload["url"],
                            data=body,
                            method=upload.get("method", "PUT"),
                            headers={
                                **upload.get("headers", {}),
                                "Content-Length": str(file["bytes"]),
                                "User-Agent": USER_AGENT,
                            },
                        )
                        with urllib.request.urlopen(request, timeout=600):
                            pass
                    break
                except (urllib.error.URLError, OSError) as error:
                    log("upload", "try %d of 2: %s" % (attempt + 1, kind(error)))
                    if attempt == 1:
                        return
                    time.sleep(2)
            status, _ = call("/sent", {"code": code})
            if status != 200:
                log("sent", "answered %d" % status)
        except Exception as error:
            log("send", kind(error))
        finally:
            self.busy = False


class Notifications(orca.script.ScriptPluginCapabilityBase):
    """The switch: the notification after a slice, on or off. The Speed Dial action stays."""

    def get_name(self):
        return "Render in Extrudio: notifications on or off"

    def execute(self):
        now_on = not notifications_on()
        set_notifications(now_on)
        return orca.ExecutionResult.success(
            TEXT["notifications_on"] if now_on else TEXT["notifications_off"]
        )


@orca.plugin
class ExtrudioPlugin(orca.base):
    def register_capabilities(self):
        orca.request_permissions(
            fs_read=[os.path.join(slices_root(), "*")],
            network=sorted({APP, STORAGE}),
            threading=True,
        )
        orca.register_capability(RenderInExtrudio)
        orca.register_capability(Notifications)
