import re
from contextlib import asynccontextmanager
from uuid import UUID
from fastapi import Depends, FastAPI, Header, HTTPException, Response, Request
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials
from .accounts import NativeAccounts, Login, Refresh, Registration, Reset
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from .auth import TokenVerifier, org_unit
from .config import Settings
from .delivery import DeliveryWorker
from .models import Encounter
from .messages import TeamMessages, MessageForm
from .queue import SyncQueue
from .quality import ReportingQuality
from .permissions import assigned_role, require_permission, PERMISSIONS
from .workflows import Workflows, CaseForm, CaseAction
from pydantic import BaseModel, ConfigDict, Field
from .accounts import Role
import json


def create_app(settings=None, queue=None, verifier=None, *, start_worker=True, quality=None, accounts=None):
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
                             quality=quality or ReportingQuality(configuration),
                             accounts=accounts or NativeAccounts.from_env())
            if start_worker:
                worker = DeliveryWorker(storage, configuration)
                worker.start()
            yield
        finally:
            if worker:
                await run_in_threadpool(worker.close)
            if resources.get("accounts"):
                await run_in_threadpool(resources["accounts"].close)
            if resources.get("quality"):
                await run_in_threadpool(resources["quality"].close)
            await run_in_threadpool(storage.close)
            resources.clear()

    api = FastAPI(title="CommunityCare AI API", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    # Run the dynamically configured verifier through FastAPI dependency injection.
    from .auth import bearer

    def identity(credentials=Depends(bearer)):
        return resources["verifier"](credentials)

    def claims(actor=Depends(identity)):
        assigned_role(actor)
        return actor

    @api.middleware("http")
    async def no_store(request, call_next):
        if request.url.path.startswith(("/api/v1/auth/", "/api/v1/messages", "/api/v1/workflows", "/api/v1/staff")):
            size = request.headers.get("content-length")
            if size is not None and (not size.isdigit() or int(size) > 16384):
                return JSONResponse({"detail": "Account form is too large"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @api.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        if request.url.path.startswith(("/api/v1/auth/", "/api/v1/messages", "/api/v1/workflows", "/api/v1/staff")):
            return JSONResponse({"detail": "Check the account form fields and try again"}, status_code=422)
        from fastapi.exception_handlers import request_validation_exception_handler
        return await request_validation_exception_handler(request, error)

    def session_result(result):
        actor = resources["verifier"](HTTPAuthorizationCredentials(scheme="Bearer", credentials=result["accessToken"]))
        org_unit(actor)
        return result

    @api.post("/api/v1/auth/login")
    def native_login(form: Login, request: Request):
        service = resources["accounts"]
        service.limit("login", request.client.host if request.client else "unknown", form.username)
        return session_result(service.login(form))

    @api.post("/api/v1/auth/refresh")
    def native_refresh(form: Refresh, request: Request):
        service = resources["accounts"]
        service.limit("refresh", request.client.host if request.client else "unknown", form.refreshToken.get_secret_value())
        return session_result(service.refresh(form))

    @api.post("/api/v1/auth/register", status_code=201)
    def native_register(form: Registration, request: Request):
        service = resources["accounts"]
        service.limit("register", request.client.host if request.client else "unknown", form.email)
        return service.register(form)

    @api.post("/api/v1/auth/password-reset", status_code=202)
    def native_reset(form: Reset, request: Request):
        service = resources["accounts"]
        service.limit("reset", request.client.host if request.client else "unknown", form.username)
        return service.reset(form)

    @api.get("/actuator/health")
    def health():
        try:
            healthy = resources["queue"].healthy()
        except Exception:
            healthy = False
        return JSONResponse({"status": "UP" if healthy else "DOWN"}, status_code=200 if healthy else 503)

    @api.get("/api/v1/me")
    def current_user(actor=Depends(identity)):
        subject = actor["sub"]
        username = actor.get("preferred_username")
        username = username if isinstance(username, str) and username else subject
        name = actor.get("name")
        name = name if isinstance(name, str) and name else username
        role = assigned_role(actor, allow_pending=True)
        return {"subject": subject, "username": username, "name": name, "orgUnit": org_unit(actor), "role": role,
                "permissions": PERMISSIONS.get(role, [])}

    @api.put("/api/v1/encounters/{encounter_id}")
    def submit(encounter_id: UUID, encounter: Encounter, response: Response, actor=Depends(claims),
               expected_org: str | None = Header(default=None, alias="X-Organisation-Unit")):
        assigned_org = org_unit(actor)
        if expected_org is not None and expected_org != assigned_org:
            raise HTTPException(403, "Facility assignment changed; sign in again")
        require_permission(actor, "submit_report")
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

    def message_org(actor, expected_org):
        assigned = org_unit(actor)
        if expected_org is not None and expected_org != assigned:
            raise HTTPException(403, "Facility assignment changed; sign in again")
        return assigned

    @api.get("/api/v1/messages")
    def messages(actor=Depends(claims), expected_org: str | None = Header(default=None, alias="X-Organisation-Unit")):
        return TeamMessages(resources["queue"]).list(message_org(actor, expected_org))

    @api.put("/api/v1/messages/{message_id}")
    def send_message(message_id: UUID, form: MessageForm, actor=Depends(claims),
                     expected_org: str | None = Header(default=None, alias="X-Organisation-Unit")):
        return TeamMessages(resources["queue"]).send(message_org(actor, expected_org), actor, message_id, form)

    def workflows():
        return Workflows(resources["queue"],resources["accounts"])

    @api.get("/api/v1/workflows")
    def case_list(actor=Depends(claims),expected_org: str | None = Header(default=None,alias="X-Organisation-Unit")):
        message_org(actor,expected_org)
        return workflows().list(actor)

    @api.get("/api/v1/workflows/{case_id}")
    def case_detail(case_id: UUID,actor=Depends(claims),expected_org: str | None = Header(default=None,alias="X-Organisation-Unit")):
        message_org(actor,expected_org)
        return workflows().detail(actor,case_id)

    @api.put("/api/v1/workflows/{case_id}")
    def case_create(case_id: UUID,form: CaseForm,actor=Depends(claims),expected_org: str | None = Header(default=None,alias="X-Organisation-Unit")):
        message_org(actor,expected_org)
        return workflows().create(actor,case_id,form)

    @api.post("/api/v1/workflows/{case_id}/actions")
    def case_change(case_id: UUID,form: CaseAction,actor=Depends(claims),expected_org: str | None = Header(default=None,alias="X-Organisation-Unit")):
        message_org(actor,expected_org)
        return workflows().change(actor,case_id,form)

    @api.get("/api/v1/staff")
    def staff_directory(actor=Depends(claims),expected_org: str | None = Header(default=None,alias="X-Organisation-Unit")):
        org=message_org(actor,expected_org)
        return resources["accounts"].staff_list(org,include_pending=assigned_role(actor)=="Supervisor")

    class StaffRoleForm(BaseModel):
        model_config=ConfigDict(extra="forbid")
        role: Role
        reason: str = Field(min_length=3,max_length=500)
        requestId: UUID

    @api.put("/api/v1/staff/{subject}/role")
    def staff_role(subject: UUID,form: StaffRoleForm,actor=Depends(claims),expected_org: str | None = Header(default=None,alias="X-Organisation-Unit")):
        require_permission(actor,"manage_staff_roles")
        org=message_org(actor,expected_org);target=str(subject)
        if len(form.reason.strip())<3:raise HTTPException(422,"Enter a reason")
        with resources["queue"].pool.connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",("staff-roles/"+org,))
            if resources["accounts"].staff_member(org,actor["sub"])["role"]!="Supervisor":
                raise HTTPException(403,"Supervisor approval is required")
            prior=connection.execute("SELECT * FROM staff_role_audit WHERE org_unit=%s AND request_id=%s",(org,form.requestId)).fetchone()
            if prior:
                stored=json.loads(resources["queue"].crypto.decrypt("staff-role/"+org+"/"+str(form.requestId),prior["payload"]))
                if prior["actor_id"]!=actor["sub"] or prior["target_id"]!=target or stored["after"]!=form.role.value or stored["reason"]!=form.reason:
                    raise HTTPException(409,"Role-change reference already used")
                return resources["accounts"].staff_member(org,target)
            old=resources["accounts"].staff_member(org,target)
            result=resources["accounts"].assign_staff_role(org,actor,target,form.role.value)
            payload=json.dumps({"before":old["role"],"after":result["role"],"reason":form.reason}).encode()
            connection.execute("INSERT INTO staff_role_audit(org_unit,request_id,actor_id,target_id,payload) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (org,form.requestId,actor["sub"],target,resources["queue"].crypto.encrypt("staff-role/"+org+"/"+str(form.requestId),payload)))
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
