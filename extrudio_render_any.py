# /// script
# requires-python = ">=3.12"
#
# [tool.orcaslicer.plugin]
# name = "Render in Extrudio"
# description = "After a slice, see your print in your filament, in real scenes. You approve each send on Extrudio's page, inside OrcaSlicer."
# author = "Extrudio"
# version = "1.0.0"
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
- It ignores every message from the page.

The flow and its security review: the Extrudio app's docs/spikes/orca-plugin.md and its DECISIONS,
2026-11-20.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request

import orca

VERSION = "1.0.0"
# Extrudio, and its storage (the engine's signed upload URLs, on the app's own Supabase project).
APP = "https://app.extrudio.site"
STORAGE = "https://cpavinhltwobpnblhuym.supabase.co"
# A development build points both at a local app instead (the plugin's README).
_DEV = os.environ.get("EXTRUDIO_APP_URL", "").rstrip("/")
if _DEV.startswith(("http://localhost:", "http://127.0.0.1:")):
    APP = STORAGE = _DEV

HANDOFFS = APP + "/api/slicer/handoffs"
USER_AGENT = "ExtrudioOrcaPlugin/" + VERSION
MAX_BYTES = 200 * 1024 * 1024
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


def slices_root():
    # Orca 2.5.1 writes each slice's G-code under
    # $TMPDIR/orcaslicer_<uid>/orcaslicer_model/<day>/<time>#<pid>#<n>/Metadata/.<pid>.<plate>.gcode
    tmp = os.environ.get("TMPDIR", "/tmp").rstrip("/")
    return "%s/orcaslicer_%d/orcaslicer_model" % (tmp, os.getuid())


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
        if file["bytes"] > MAX_BYTES:
            notify(TEXT["too_large"] % (MAX_BYTES // (1024 * 1024)))
            return True
        name = (self.project_name() or "print")[:200] + ".gcode"
        try:
            status, handoff = call("", {"fileName": name, "bytes": file["bytes"]})
        except (urllib.error.URLError, OSError, ValueError):
            status, handoff = 0, {}
        if status != 201:
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
                if status != 200 or answer.get("state") == "refused":
                    return
                if answer.get("upload"):
                    upload = answer["upload"]
                    break
                time.sleep(BRIDGE_EVERY if time.time() - started < BRIDGE_SECONDS else POLL_SECONDS)
            if upload is None:
                return
            if not upload["url"].startswith((STORAGE + "/", APP + "/")):
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
                except (urllib.error.URLError, OSError):
                    if attempt == 1:
                        return
                    time.sleep(2)
            call("/sent", {"code": code})
        except Exception:
            pass
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
            fs_read=[slices_root() + "/*"],
            network=sorted({APP, STORAGE}),
            threading=True,
        )
        orca.register_capability(RenderInExtrudio)
        orca.register_capability(Notifications)
