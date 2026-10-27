from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from aiops import analysis, catalog, db, ingest, jobs, quality, triage
from aiops.config import Settings, load


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="alert-sleuth")
    base_dir = Path(__file__).resolve().parent
    templates_dir = base_dir / "templates"
    static_dir = base_dir / "static"

    templates = Jinja2Templates(directory=str(templates_dir))
    templates.env.autoescape = True

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    conn = db.connect(settings.db_path)

    @app.middleware("http")
    async def check_origin(request: Request, call_next) -> Response:
        # ponytail: origin check instead of CSRF tokens, enough for localhost/SSO proxy
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin:
                origin_host = urlsplit(origin).netloc.lower()
                request_host = request.headers.get("host", "").lower()
                if origin_host and origin_host != request_host:
                    return JSONResponse({"detail": "Origin non autorisée"}, status_code=403)
        return await call_next(request)

    def render(request: Request, name: str, context: dict) -> Response:
        return templates.TemplateResponse(name=name, context=context, request=request)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
        if exc.status_code == 404:
            return HTMLResponse(
                "<!doctype html><html lang='fr'><head><meta charset='utf-8'>"
                "<title>404 — alert-sleuth</title></head>"
                "<body><h1>404</h1><p>Page introuvable</p>"
                "<a href='/alerts'>Retour aux alertes</a></body></html>",
                status_code=404,
            )
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.get("/", include_in_schema=False)
    def root() -> Response:
        return RedirectResponse("/alerts", status_code=303)

    @app.get("/alerts")
    def alerts(
        request: Request,
        service: str | None = Query(None),
        decision: str | None = Query(None),
    ) -> Response:
        service = service or None
        decision = decision or None
        groups = db.list_groups(conn, service=service, decision=decision)
        clusters = triage.common_causes(groups)
        return render(
            request,
            "alerts.html",
            {
                "active": "alerts",
                "groups": groups,
                "clusters": clusters,
                "service": service,
                "decision": decision,
            },
        )

    @app.post("/fetch")
    def fetch_alerts() -> Response:
        def fetch_job(job_conn, on_step):
            count = ingest.fetch(settings, job_conn)
            on_step("Récupération des alertes", "ok", f"{count} alertes")
            return count

        try:
            job_id = jobs.start(settings, "fetch", "fetch", fetch_job)
        except RuntimeError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/triage")
    def run_triage() -> Response:
        def triage_job(job_conn, on_step):
            groups = triage.run(settings, job_conn, use_llm=True)
            on_step("Triage", "ok", f"{len(groups)} groupes")
            return len(groups)

        try:
            job_id = jobs.start(settings, "triage", "triage", triage_job)
        except RuntimeError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/groups/{group_id}/analyze")
    def analyze_group(group_id: str) -> Response:
        if db.get_group(conn, group_id) is None:
            raise HTTPException(status_code=404, detail="Groupe introuvable")

        def analyze_job(job_conn, on_step):
            return analysis.analyze(settings, job_conn, group_id, on_step=on_step)

        try:
            job_id = jobs.start(settings, "analyze", group_id, analyze_job)
        except RuntimeError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs/{job_id}")
    def job_page(request: Request, job_id: str) -> Response:
        job = db.get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job introuvable")
        return render(request, "job.html", {"active": "alerts", "job": job})

    @app.get("/jobs/{job_id}/status")
    def job_status(request: Request, job_id: str) -> Response:
        job = db.get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job introuvable")
        return render(request, "_job_status.html", {"job": job})

    @app.get("/reports")
    def reports_list(request: Request, q: str | None = Query(None)) -> Response:
        q = q or None
        reports = db.list_reports(conn, q=q)
        processed: list[dict] = []
        for report in reports:
            r = report.get("report") if isinstance(report.get("report"), dict) else {}
            pc = r.get("probable_cause") if isinstance(r.get("probable_cause"), dict) else {}
            confidence = None
            raw_confidence = pc.get("confidence")
            if raw_confidence is not None:
                try:
                    confidence = float(raw_confidence)
                except (TypeError, ValueError):
                    confidence = None
            processed.append(
                {
                    **report,
                    "confidence": confidence,
                    "severity": pc.get("severity"),
                }
            )
        return render(
            request,
            "reports.html",
            {"active": "reports", "reports": processed, "q": q},
        )

    @app.get("/reports/{report_id}")
    def report_detail(
        request: Request,
        report_id: int,
        notice: str | None = Query(None),
    ) -> Response:
        data = db.get_report(conn, report_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Rapport introuvable")

        group = None
        group_id = data.get("group_id")
        if group_id:
            group = db.get_group(conn, group_id)

        r = data.get("report") if isinstance(data.get("report"), dict) else {}
        pc = r.get("probable_cause") if isinstance(r.get("probable_cause"), dict) else {}
        service_info = r.get("service") if isinstance(r.get("service"), dict) else {}

        confidence = None
        raw_confidence = pc.get("confidence")
        if raw_confidence is not None:
            try:
                confidence = float(raw_confidence)
            except (TypeError, ValueError):
                confidence = None

        severity = pc.get("severity")
        if not severity and group:
            severity = group.get("priority")

        reason = group.get("reason") if group else None

        def as_list(value: object) -> list:
            return value if isinstance(value, list) else []

        return render(
            request,
            "report.html",
            {
                "active": "reports",
                "report": data,
                "group": group,
                "r": r,
                "pc": pc,
                "service_info": service_info,
                "confidence": confidence,
                "severity": severity,
                "reason": reason,
                "missing": as_list(data.get("missing")),
                "evidence": as_list(data.get("evidence")),
                "feedback": as_list(data.get("feedback")),
                "unverified": as_list(r.get("unverified_ids")),
                "timeline": as_list(r.get("timeline")),
                "hypotheses": as_list(r.get("hypotheses")),
                "actions": as_list(r.get("immediate_actions")),
                "improvements": as_list(r.get("improvements")),
                "notice": notice,
            },
        )

    @app.post("/reports/{report_id}/regenerate")
    def regenerate_report(report_id: int) -> Response:
        data = db.get_report(conn, report_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Rapport introuvable")

        group_id = data.get("group_id")
        if not group_id or db.get_group(conn, group_id) is None:
            raise HTTPException(status_code=404, detail="Groupe introuvable")

        def analyze_job(job_conn, on_step):
            return analysis.analyze(settings, job_conn, group_id, on_step=on_step)

        try:
            job_id = jobs.start(settings, "analyze", group_id, analyze_job)
        except RuntimeError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/reports/{report_id}/feedback")
    def submit_feedback(
        report_id: int,
        verdict: str = Form(...),
        root_cause: str = Form(""),
        comment: str = Form(""),
    ) -> Response:
        data = db.get_report(conn, report_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Rapport introuvable")
        if verdict not in ("useful", "wrong"):
            return JSONResponse({"detail": "Verdict invalide"}, status_code=400)

        db.add_feedback(conn, report_id, verdict, root_cause, comment)
        return RedirectResponse(f"/reports/{report_id}?notice=feedback", status_code=303)

    @app.get("/quality")
    def quality_page(request: Request) -> Response:
        entries = catalog.load(settings.catalog_path)
        rows = quality.scorecard(conn, entries)
        return render(request, "quality.html", {"active": "quality", "rows": rows})

    return app


def factory() -> FastAPI:
    return create_app(load())
