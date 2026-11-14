"""Export and import of a workspace, typed on a page the node serves (agentos.node.forms).

A workspace's file is sealed with a passphrase, so the passphrase is typed here, on this computer, and the
web app never sees it. Export answers with the sealed file as a download. Import takes the file the person
chooses and the passphrase it was made with, plans it, and applies it only when the plan has no blockers.
Nothing here logs or traces a value; the file's own contents are never echoed.
"""

from __future__ import annotations

import asyncio
import html
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from agentos.core.errors import AgentosError
from agentos.node.forms import Kind, Trip, Trips, field_input, note, page


class WorkspaceExport(Kind):
    name = "workspace_export"
    takes = ("workspace",)
    needs = ("workspace",)

    def title(self, trip: Trip, api: Any) -> str:
        return f"Export {trip.fields.get('workspace', 'a workspace')}"

    def body(self, trip: Trip, api: Any, error: str | None = None) -> str:
        name = html.escape(trip.fields.get("workspace", ""))
        return (f"<h1>Export {name}</h1><p class=soft>The file is sealed with the passphrase you type here. Keep the "
                "passphrase: without it nobody can open the file, agentos included. The file downloads from this page "
                "and is not kept on this computer.</p>" + (note(error) if error else "")
                + '<form method="post" autocomplete="off">'
                + field_input("Passphrase for the file", "passphrase", kind="password")
                + '<label style="display:flex;gap:9px;font-weight:500;font-size:14px;margin:0 0 16px">'
                  '<input type="checkbox" name="with_proxies" value="yes" style="width:auto;margin-top:4px">'
                  "Include its proxies, sealed in the same file.</label>"
                + '<button type="submit">Export and download</button></form>')

    async def submit(self, trip: Trip, api: Any, form: Any, trips: Trips) -> Response:
        from agentos.ops.wsexport import export_name, export_workspace, runtime_sources, staging_dir

        passphrase = str(form.get("passphrase") or "")
        if len(passphrase) < 8:
            return self.again(trip, api, "Use a passphrase of at least 8 characters. Nothing was exported.")
        src = runtime_sources(api.runtime)
        folder = staging_dir(src.home, "export")
        folder.mkdir(mode=0o700)
        dest = folder / export_name(trip.fields["workspace"], datetime.now(UTC))
        try:
            result = await export_workspace(src, trip.fields["workspace"], dest, passphrase=passphrase,
                                            with_proxies=bool(form.get("with_proxies")))
        except AgentosError as exc:
            shutil.rmtree(folder, ignore_errors=True)
            return self.again(trip, api, exc.message or "The workspace could not be exported. Nothing was saved.")
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        trips.close(trip, "done")
        headers = {"content-disposition": f'attachment; filename="{dest.name}"', "content-length": str(result.size),
                   "content-encoding": "identity", "cache-control": "no-store"}
        return StreamingResponse(_chunks(dest), media_type="application/octet-stream", headers=headers,
                                 background=BackgroundTask(shutil.rmtree, folder, ignore_errors=True))


class WorkspaceImport(Kind):
    name = "workspace_import"
    takes = ("name",)  # the new name the app may send: the import may need one (a name already here)
    needs = ()
    multipart = True

    def title(self, trip: Trip, api: Any) -> str:
        return "Import a workspace"

    def body(self, trip: Trip, api: Any, error: str | None = None) -> str:
        return ("<h1>Import a workspace</h1><p class=soft>Choose the file a workspace was exported to, and the "
                "passphrase it was sealed with. agentos checks the file first. Nothing is imported unless the check "
                "passes.</p>" + (note(error) if error else "")
                + '<form method="post" enctype="multipart/form-data" autocomplete="off">'
                + '<label for="file">Export file</label><input id="file" name="file" type="file" required '
                  'style="margin-bottom:16px">'
                + field_input("New name (optional)", "name", value=trip.fields.get("name", ""), required=False)
                + '<p class=soft style="margin:-8px 0 16px">The name the workspace shows on this computer. Its id is '
                  'made from it.</p>'
                + field_input("Passphrase it was sealed with", "passphrase", kind="password")
                + '<button type="submit">Check and import</button></form>')

    async def submit(self, trip: Trip, api: Any, form: Any, trips: Trips) -> Response:
        from agentos.ops.wsexport import read_export, runtime_sources, staging_dir
        from agentos.ops.wsimport import ImportOptions, apply_import, plan_import

        passphrase = str(form.get("passphrase") or "")
        upload = form.get("file")
        # The name the person typed here, kept for the page when it has to be asked again.
        typed = str(form.get("name") or "").strip()
        trip.fields["name"] = typed
        if not passphrase:
            return self.again(trip, api, "Type the passphrase the file was sealed with. Nothing was imported.")
        if upload is None or not getattr(upload, "filename", None):
            return self.again(trip, api, "Choose the export file. Nothing was imported.")
        src = runtime_sources(api.runtime)
        staged = staging_dir(src.home, "upload")
        try:
            await _receive(upload, staged)
            try:
                contents = await asyncio.to_thread(read_export, staged, staging_dir(src.home, "import"),
                                                   passphrase=passphrase, identities=None)
            except (AgentosError, ValueError):
                # One line for both causes: a wrong passphrase and a damaged file look the same from outside.
                return self.again(trip, api, "The passphrase did not open this file, or the file is damaged. "
                                             "Nothing was imported.")
        finally:
            staged.unlink(missing_ok=True)
        try:
            plan = await plan_import(contents, src, ImportOptions(display_name=typed or None, by="owner", on_page=True))
            if not plan.ok:
                reasons = " ".join(str(item) for item in plan.blockers[:3]) or "The file needs a change first."
                return self.again(trip, api, f"Nothing was imported. {reasons}")
            result = await apply_import(plan, contents, src)
        except AgentosError as exc:
            return self.again(trip, api, exc.message or "The import did not finish. Nothing else was changed.")
        finally:
            contents.discard()
        trips.close(trip, "done")
        name = html.escape(typed or str(getattr(result, "workspace", "") or "The workspace"))
        return page("Imported", f"<h1 class=good>Imported</h1><p>{name} is back on this computer. You can close this "
                                "tab.</p>")


async def _receive(upload: Any, path: Path) -> None:
    """Stream the uploaded file to disk, never into memory."""
    await upload.seek(0)
    with path.open("wb") as out:
        await asyncio.to_thread(shutil.copyfileobj, upload.file, out)


async def _chunks(path: Path):
    with path.open("rb") as handle:
        while chunk := await asyncio.to_thread(handle.read, 1 << 20):
            yield chunk
