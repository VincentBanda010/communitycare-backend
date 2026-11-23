import re
from contextlib import asynccontextmanager
from uuid import UUID
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from .auth import TokenVerifier, org_unit
from .config import Settings
from .delivery import DeliveryWorker
from .models import Encounter
from .queue import SyncQueue
from .quality import ReportingQuality


def create_app(settings=None, queue=None, verifier=None, *, start_worker=True, quality=None):
    resources = {}

    @asynccontextmanager
    async def lifespan(app):
        configuration = settings or Settings.from_env()
        storage = queue or SyncQueue(configuration)
        worker = None
        try:
            await run_in_threadpool(storage.open)
            resources.update(settings=configuration, queue=storage,
                             verifier=verifier or await run_in_threadpool(TokenVerifier, configuration),
                             quality=quality or ReportingQuality(configuration))
            if start_worker:
                worker = DeliveryWorker(storage, configuration)
                worker.start()
            yield
        finally:
            if worker:
                await run_in_threadpool(worker.close)
            if resources.get("quality"):
                await run_in_threadpool(resources["quality"].close)
            await run_in_threadpool(storage.close)
            resources.clear()

    api = FastAPI(title="CommunityCare AI API", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    # Run the dynamically configured verifier through FastAPI dependency injection.
    from .auth import bearer

    def claims(credentials=Depends(bearer)):
        return resources["verifier"](credentials)

    @api.middleware("http")
    async def no_store(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @api.get("/actuator/health")
    def health():
        try:
            healthy = resources["queue"].healthy()
        except Exception:
            healthy = False
        return JSONResponse({"status": "UP" if healthy else "DOWN"}, status_code=200 if healthy else 503)

    @api.get("/api/v1/me")
    def current_user(actor=Depends(claims)):
        subject = actor["sub"]
        username = actor.get("preferred_username")
        username = username if isinstance(username, str) and username else subject
        name = actor.get("name")
        name = name if isinstance(name, str) and name else username
        return {"subject": subject, "username": username, "name": name, "orgUnit": org_unit(actor)}

    @api.put("/api/v1/encounters/{encounter_id}")
    def submit(encounter_id: UUID, encounter: Encounter, response: Response, actor=Depends(claims),
               expected_org: str | None = Header(default=None, alias="X-Organisation-Unit")):
        assigned_org = org_unit(actor)
        if expected_org is not None and expected_org != assigned_org:
            raise HTTPException(403, "Facility assignment changed; sign in again")
        receipt = resources["queue"].enqueue(assigned_org, encounter_id, encounter)
        response.status_code = 202 if receipt["state"] == "QUEUED" else 200
        return receipt

    @api.get("/api/v1/reporting/quality")
    def reporting_quality(actor=Depends(claims),
                          expected_org: str | None = Header(default=None, alias="X-Organisation-Unit")):
        assigned_org = org_unit(actor)
        if expected_org is not None and expected_org != assigned_org:
            raise HTTPException(403, "Facility assignment changed; sign in again")
        return resources["quality"].snapshot(assigned_org)

    @api.get("/api/v1/encounters/{encounter_id}")
    def receipt(encounter_id: UUID, actor=Depends(claims),
                expected_org: str | None = Header(default=None, alias="X-Organisation-Unit")):
        assigned_org = org_unit(actor)
        if expected_org is not None and expected_org != assigned_org:
            raise HTTPException(403, "Facility assignment changed; sign in again")
        result = resources["queue"].find(assigned_org, encounter_id)
        if result is None:
            raise HTTPException(404, "Encounter not found")
        return result

    @api.get("/api/v1/updates/{filename}")
    def download(filename: str, actor=Depends(claims)):
        if filename != "manifest.jws" and not re.fullmatch(r"[a-f0-9]{64}\.zip", filename):
            raise HTTPException(404, "Update not found")
        directory = resources["settings"].update_directory.resolve()
        file = directory / filename
        if file.is_symlink() or not file.is_file() or file.resolve().parent != directory:
            raise HTTPException(404, "Update not found")
        return FileResponse(file, media_type="application/jose" if filename == "manifest.jws" else "application/zip")

    return api


app = create_app()
