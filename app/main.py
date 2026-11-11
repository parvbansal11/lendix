"""Single-service synthetic office, durable event feed and asynchronous jobs."""
import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .agents import IntakeOutput
from .config import DEMO_USERS, ROOT, Settings
from .gemini import GeminiClient
from .model import ModelUnavailable
from .nonilion.adapter import LocalAdapter, NonilionAdapter, verify_nonilion_request
from .orchestrator import Orchestrator
from .store import Store, utcnow
from .uploads import MAX_UPLOAD_BYTES, parse_upload


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    role: Literal["credit_officer", "ops", "borrower"] = "borrower"
    conversation_id: str = Field(default="office", min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


def create_app(settings=None, model=None):
    settings = settings or Settings()
    tasks = set()

    @asynccontextmanager
    async def lifespan(app):
        settings.validate()
        store = Store(settings.db_path)
        store.seed(ROOT / "data/seed.json")
        client = model or GeminiClient(settings, store)
        local = LocalAdapter(store)
        orchestrator = Orchestrator(store, settings, client, local)
        local.orchestrator = orchestrator
        app.state.store, app.state.orchestrator, app.state.adapter = store, orchestrator, local
        app.state.nonilion = NonilionAdapter()
        for job in store.all("job"):
            if job["status"] in {"queued", "running"}:
                job["status"] = "interrupted"
                store.put("job", job["id"], job)
                store.emit("error", "Front Desk", "A server restart interrupted this job. Please retry the request; completed loan decisions remain recorded.", conversation_id=job["conversation_id"], data={"job_id": job["id"]})

        async def reminders():
            while True:
                await asyncio.sleep(30)
                try:
                    await orchestrator.deliver_due_reminders()
                except Exception:
                    store.emit("error", "Collections", "A scheduled reminder needs operator attention; it remains queued.")

        clock_task = asyncio.create_task(reminders())
        yield
        clock_task.cancel()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(clock_task, *tasks, return_exceptions=True)
        if model is None:
            await client.close()
        store.engine.dispose()

    app = FastAPI(title="Lendix Finance", lifespan=lifespan,
                  docs_url="/docs" if settings.env == "dev" else None,
                  redoc_url="/redoc" if settings.env == "dev" else None,
                  openapi_url="/openapi.json" if settings.env == "dev" else None)

    @app.middleware("http")
    async def headers(request, call_next):
        body_limit = MAX_UPLOAD_BYTES + 65536 if request.url.path == "/api/upload" else 65536
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > body_limit:
                    return JSONResponse({"detail": "Request exceeds the 5 MB upload limit."}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid content length."}, status_code=400)
        if request.method in {"POST", "PUT", "PATCH"}:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > body_limit:
                    return JSONResponse({"detail": "Request exceeds the allowed size."}, status_code=413)
            # Starlette's request cache replays this bounded body to downstream parsers.
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
        if request.url.path.startswith(("/api", "/files")):
            response.headers["Cache-Control"] = "no-store"
        return response

    def rate(request):
        ip = request.client.host if request.client else "unknown"
        if not app.state.store.allow_request(ip):
            raise HTTPException(429, "Message limit reached: 20 messages per 10 minutes. Please try again later.")

    def submit(work, conversation, label="message"):
        store = app.state.store
        job_id = "JOB-" + secrets.token_hex(6)
        job = {"id": job_id, "status": "queued", "conversation_id": conversation, "created_at": utcnow().isoformat(), "label": label}
        store.put("job", job_id, job)
        store.emit("status", "Front Desk", "Request queued; the office will post results here.", conversation_id=conversation,
                   data={"job_id": job_id, "status": "queued"})

        async def runner():
            job["status"] = "running"
            store.put("job", job_id, job)
            try:
                await work()
                job["status"] = "completed"
            except asyncio.CancelledError:
                job["status"] = "interrupted"
                raise
            except Exception:
                job["status"] = "failed"
                store.emit("error", "Front Desk", "This request could not be completed. Please retry or ask the officer to inspect the recorded state. No approval was inferred.", conversation_id=conversation)
            finally:
                store.put("job", job_id, job)
                store.emit("status", "Front Desk", f"Job {job['status']}.", conversation_id=conversation,
                           data={"job_id": job_id, "status": job["status"]})

        task = asyncio.create_task(runner())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"accepted": True, "job_id": job_id}

    @app.get("/")
    async def home():
        return FileResponse(ROOT / "static/index.html")

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "data": "synthetic", "nonilion": "not_connected"}

    @app.get("/api/state")
    async def state():
        documents = [{k: v for k, v in item.items() if k not in {"path", "token"}}
                     for item in app.state.store.all("file") if datetime.fromisoformat(item["expires_at"]) > utcnow()]
        return {"loans": app.state.store.all("loan"), "documents": documents, "users": DEMO_USERS,
                "demo_role_switching": True, "nonilion_connected": False}

    @app.post("/api/chat", status_code=202)
    async def chat(payload: ChatRequest, request: Request):
        rate(request)
        if not payload.message.strip():
            raise HTTPException(422, "Enter a message.")
        return submit(lambda: app.state.adapter.on_message(payload.model_dump()), payload.conversation_id)

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: str):
        item = app.state.store.get("job", job_id)
        if not item:
            raise HTTPException(404, "Job not found.")
        return item

    @app.post("/api/upload", status_code=202)
    async def upload(request: Request, file: UploadFile = File(...), loan_id: str = Form(""),
                     role: Literal["credit_officer", "ops", "borrower"] = Form("borrower"),
                     conversation_id: str = Form("office")):
        rate(request)
        if len(conversation_id) > 80 or not conversation_id.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(422, "Invalid conversation ID.")
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        await file.close()
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Uploads must be 5 MB or smaller.")
        try:
            parsed = await asyncio.to_thread(parse_upload, file.filename or "", content)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        loan = app.state.store.get("loan", loan_id.upper()) if loan_id else None
        if loan_id and not loan:
            raise HTTPException(404, "Loan not found.")
        if loan and loan.get("status") not in {"new", "pending", "received", "awaiting_documents"}:
            raise HTTPException(409, "Documents cannot change after review. Open a new application for changed information.")

        async def process():
            office = app.state.orchestrator
            async with office.lock:
                office.conversation = conversation_id
                selected = app.state.store.get("loan", loan_id.upper()) if loan_id else None
                if selected and selected.get("status") not in {"new", "pending", "received", "awaiting_documents"}:
                    await office.reply("This application has already been reviewed. Open a new application for changed information.", selected)
                    return
                current_loan = selected
                office.tool("Intake", "parse_upload", lambda: None, current_loan)
                if parsed["applications"]:
                    if loan and len(parsed["applications"]) != 1:
                        await office.reply("Attach one application when a loan ID is selected, or clear the selection for a batch import.", loan)
                        return
                    if len(parsed["applications"]) > 25:
                        await office.reply("Import at most 25 applications per request in this demo.")
                        return
                    for row in parsed["applications"]:
                        try:
                            if parsed["kind"] == "csv" and isinstance(row.get("consent"), str):
                                raw_consent = row["consent"].strip().lower()
                                if raw_consent in {"true", "false"}:
                                    row["consent"] = raw_consent == "true"
                            values = IntakeOutput.model_validate({k: v for k, v in row.items() if k in IntakeOutput.model_fields}).model_dump(exclude_none=True)
                        except ValidationError:
                            await office.reply("Some application fields have invalid types or values. Check the amounts, tenure and consent in the upload.", loan)
                            return
                        await office.application("", role, current_loan, supplied=values, new_application=current_loan is None)
                elif parsed.get("text", "").strip():
                    await office.application(parsed["text"][:30000], role, current_loan)
                else:
                    await office.reply("The image or scan was received, but this demo does not perform image identity verification. Please provide readable text or a structured CSV/JSON application for officer review.", loan)
                office.event("status", "Intake", "Attachment parsed as untrusted data; verification was not inferred.", loan)

        return submit(process, conversation_id, "upload")

    @app.get("/api/events")
    async def events(request: Request, conversation_id: str = "office"):
        try:
            cursor = max(0, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            cursor = 0

        async def stream():
            nonlocal cursor
            heartbeat = 0
            while not await request.is_disconnected():
                rows = app.state.store.events(cursor, limit=250)
                for item in rows:
                    cursor = item["id"]
                    if item["conversation_id"] in {conversation_id, "office"}:
                        yield f"id: {cursor}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                heartbeat += 1
                if heartbeat % 20 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(.5)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/files/{token}")
    async def download(token: str):
        record = app.state.store.get("file", token)
        if not record or datetime.fromisoformat(record["expires_at"]) <= utcnow():
            raise HTTPException(404, "This download does not exist or has expired.")
        path = Path(record["path"]).resolve()
        if not path.is_relative_to(Path(settings.output_dir).resolve()) or not path.is_file():
            raise HTTPException(404, "File unavailable.")
        return FileResponse(path, filename=record["filename"], media_type=record["mime_type"])

    @app.post("/nonilion/webhook")
    async def webhook(request: Request):
        if not verify_nonilion_request(request):
            raise HTTPException(403, "Request verification is not configured.")
        rate(request)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise HTTPException(413, "Webhook body too large.")
        # Opaque bytes until the guide supplies the real event schema.
        async def process():
            try:
                await app.state.nonilion.on_message(bytes(body))
            except NotImplementedError:
                app.state.store.emit("error", "Front Desk", "Nonilion is not connected: fill from the Nonilion Bot API guide.")
        accepted = submit(process, "office", "nonilion webhook")
        return {**accepted, "adapter_status": "not_implemented"}

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app


app = create_app()
