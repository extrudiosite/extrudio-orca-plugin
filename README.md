# Render in Extrudio

An OrcaSlicer 2.5 plugin. After a slice, see your print in your filament, in studio and room scenes,
on [Extrudio](https://extrudio.site).

## How it works

1. Slice. A notification offers **Render in Extrudio**.
2. Click it. Extrudio's page opens in a panel beside the 3D view, with the same code Orca shows.
3. Click **Send**, and sign in if asked. The sliced G-code goes to your Extrudio account, which
   opens it with a first preview.

You approve every send on Extrudio's page. A link to that page opened anywhere else can't approve
it.

## Install

Tested on macOS (OrcaSlicer 2.5.1) and Windows 10 (OrcaSlicer 2.5.2).

1. Download `extrudio_render_any.py` from the latest release, and check its SHA-256 against the one
   on extrudio.site.
2. Put it in a folder of its own, `orca_plugins/extrudio_render/`, inside OrcaSlicer's
   configuration folder (Help → Show Configuration Folder).
3. Restart OrcaSlicer, then tick **Render in Extrudio** in File → Plugins.
4. Allow what it asks for.

## What it asks for

- **Read:** the G-code Orca writes after a slice, in its temporary folder.
- **Network:** `app.extrudio.site`, and the storage the file uploads to
  (`cpavinhltwobpnblhuym.supabase.co`).
- **A background thread,** to send the file without holding Orca up.

## What it doesn't do

- No key, no account details, no telemetry. It keeps one setting: whether to show the notification.
- Standard library only: nothing is downloaded from PyPI.
- It ignores every message from the page.
- When a send fails, it writes one line to OrcaSlicer's log: the step and the error, never a code,
  a link or a file path.
- Binary G-code (`.bgcode`) isn't supported: turn it off in the printer settings. Files up to
  100 MB.

To turn the notification off or on: Speed Dial → **Render in Extrudio: notifications on or off**.

What Extrudio keeps for each send: [extrudio.site/privacy](https://extrudio.site/privacy).

## Development

`EXTRUDIO_APP_URL=http://localhost:<port>` points the plugin at a local Extrudio app. It takes
localhost only.

## Licence

MIT, in `LICENSE`.
