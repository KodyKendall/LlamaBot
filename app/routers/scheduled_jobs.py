"""API routes for scheduled agent jobs."""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.models import User, ScheduledJob, ScheduledJobRun
from app.dependencies import get_db_session, admin_required, engineer_or_admin_required

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scheduled-jobs", tags=["scheduled-jobs"])


# ============== Pydantic Models ==============

class CreateScheduledJobRequest(BaseModel):
    name: str
    description: Optional[str] = None
    agent_name: str
    prompt: str
    llm_model: str = "gemini-3-flash"
    cron_expression: str
    timezone: str = "UTC"
    max_duration_seconds: int = 300
    recursion_limit: int = 100
    is_enabled: bool = True


class UpdateScheduledJobRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    agent_name: Optional[str] = None
    prompt: Optional[str] = None
    llm_model: Optional[str] = None
    cron_expression: Optional[str] = None
    timezone: Optional[str] = None
    max_duration_seconds: Optional[int] = None
    recursion_limit: Optional[int] = None
    is_enabled: Optional[bool] = None


# ============== Helper Functions ==============

def _job_to_dict(job: ScheduledJob) -> dict:
    """Convert ScheduledJob to JSON-serializable dict."""
    return {
        "id": job.id,
        "name": job.name,
        "description": job.description,
        "agent_name": job.agent_name,
        "prompt": job.prompt,
        "llm_model": job.llm_model,
        "cron_expression": job.cron_expression,
        "timezone": job.timezone,
        "max_duration_seconds": job.max_duration_seconds,
        "recursion_limit": job.recursion_limit,
        "is_enabled": job.is_enabled,
        "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
        "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
        "created_by_user_id": job.created_by_user_id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def _run_to_dict(run: ScheduledJobRun) -> dict:
    """Convert ScheduledJobRun to JSON-serializable dict."""
    return {
        "id": run.id,
        "job_id": run.job_id,
        "status": run.status,
        "trigger_type": run.trigger_type,
        "thread_id": run.thread_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "duration_seconds": run.duration_seconds,
        "output_summary": run.output_summary,
        "error_message": run.error_message,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "total_tokens": run.total_tokens,
        "triggered_by_user_id": run.triggered_by_user_id,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def _calculate_next_run(cron_expression: str, tz: str) -> Optional[datetime]:
    """Calculate the next run time based on cron expression."""
    try:
        from croniter import croniter
        from pytz import timezone as pytz_timezone

        tz_obj = pytz_timezone(tz)
        now_local = datetime.now(tz_obj)
        cron = croniter(cron_expression, now_local)
        next_run_local = cron.get_next(datetime)
        return next_run_local.astimezone(timezone.utc)
    except Exception as e:
        logger.warning(f"Failed to calculate next_run: {e}")
        return None


# ============== Scheduler Token Auth ==============

from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic(auto_error=False)


def scheduler_auth(
    x_scheduler_token: Optional[str] = Header(None),
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
    session: Session = Depends(get_db_session)
) -> Optional[User]:
    """
    Allow either scheduler token OR user auth for invoking scheduled jobs.

    For cron invocations: Use X-Scheduler-Token header
    For manual invocations: Use standard HTTP Basic Auth
    """
    # First check scheduler token
    if x_scheduler_token:
        from app.services.token_service import verify_scheduler_token
        if verify_scheduler_token(x_scheduler_token):
            return None  # Valid scheduler token, no user context
        raise HTTPException(status_code=401, detail="Invalid scheduler token")

    # Fall back to user auth
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    from app.services.user_service import authenticate_user
    user = authenticate_user(session, credentials.username, credentials.password)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    # Check engineer or admin role
    if not user.is_admin and getattr(user, 'role', 'user') != "engineer":
        raise HTTPException(
            status_code=403,
            detail="Engineer or admin privileges required"
        )

    return user


# ============== Job CRUD Endpoints ==============

@router.get("", response_class=JSONResponse)
async def list_jobs(
    user: User = Depends(engineer_or_admin_required),
    session: Session = Depends(get_db_session)
):
    """List all scheduled jobs."""
    stmt = select(ScheduledJob).order_by(ScheduledJob.created_at.desc())
    jobs = session.exec(stmt).all()
    return [_job_to_dict(job) for job in jobs]


@router.post("", response_class=JSONResponse)
async def create_job(
    request: CreateScheduledJobRequest,
    user: User = Depends(engineer_or_admin_required),
    session: Session = Depends(get_db_session)
):
    """Create a new scheduled job."""
    # Calculate initial next_run
    next_run = _calculate_next_run(request.cron_expression, request.timezone)

    job = ScheduledJob(
        name=request.name,
        description=request.description,
        agent_name=request.agent_name,
        prompt=request.prompt,
        llm_model=request.llm_model,
        cron_expression=request.cron_expression,
        timezone=request.timezone,
        max_duration_seconds=request.max_duration_seconds,
        recursion_limit=request.recursion_limit,
        is_enabled=request.is_enabled,
        next_run_at=next_run,
        created_by_user_id=user.id,
    )

    session.add(job)
    session.commit()
    session.refresh(job)

    logger.info(f"Created scheduled job: {job.name} (id={job.id})")
    return _job_to_dict(job)


@router.get("/{job_id}", response_class=JSONResponse)
async def get_job(
    job_id: int,
    user: User = Depends(engineer_or_admin_required),
    session: Session = Depends(get_db_session)
):
    """Get a scheduled job by ID."""
    job = session.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_to_dict(job)


@router.patch("/{job_id}", response_class=JSONResponse)
async def update_job(
    job_id: int,
    request: UpdateScheduledJobRequest,
    user: User = Depends(engineer_or_admin_required),
    session: Session = Depends(get_db_session)
):
    """Update a scheduled job."""
    job = session.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Update fields that were provided
    update_data = request.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(job, field, value)

    job.updated_at = datetime.now(timezone.utc)

    # Recalculate next_run if cron_expression or timezone changed
    if "cron_expression" in update_data or "timezone" in update_data:
        job.next_run_at = _calculate_next_run(job.cron_expression, job.timezone)

    session.add(job)
    session.commit()
    session.refresh(job)

    logger.info(f"Updated scheduled job: {job.name} (id={job.id})")
    return _job_to_dict(job)


@router.delete("/{job_id}", response_class=JSONResponse)
async def delete_job(
    job_id: int,
    user: User = Depends(admin_required),
    session: Session = Depends(get_db_session)
):
    """Delete a scheduled job (admin only)."""
    job = session.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job_name = job.name
    session.delete(job)
    session.commit()

    logger.info(f"Deleted scheduled job: {job_name} (id={job_id})")
    return {"success": True, "message": f"Job '{job_name}' deleted"}


# ============== Job Control Endpoints ==============

@router.post("/{job_id}/enable", response_class=JSONResponse)
async def enable_job(
    job_id: int,
    user: User = Depends(engineer_or_admin_required),
    session: Session = Depends(get_db_session)
):
    """Enable a scheduled job."""
    job = session.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.is_enabled = True
    job.updated_at = datetime.now(timezone.utc)
    job.next_run_at = _calculate_next_run(job.cron_expression, job.timezone)

    session.add(job)
    session.commit()
    session.refresh(job)

    logger.info(f"Enabled scheduled job: {job.name} (id={job.id})")
    return _job_to_dict(job)


@router.post("/{job_id}/disable", response_class=JSONResponse)
async def disable_job(
    job_id: int,
    user: User = Depends(engineer_or_admin_required),
    session: Session = Depends(get_db_session)
):
    """Disable a scheduled job."""
    job = session.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.is_enabled = False
    job.updated_at = datetime.now(timezone.utc)

    session.add(job)
    session.commit()
    session.refresh(job)

    logger.info(f"Disabled scheduled job: {job.name} (id={job.id})")
    return _job_to_dict(job)


@router.post("/{job_id}/run", response_class=JSONResponse)
async def run_job_manually(
    job_id: int,
    request: Request,
    user: User = Depends(engineer_or_admin_required),
    session: Session = Depends(get_db_session)
):
    """Manually trigger a scheduled job."""
    job = session.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Execute the job in background
    from app.services.headless_agent_executor import execute_agent_headless

    try:
        run = await execute_agent_headless(
            agent_name=job.agent_name,
            prompt=job.prompt,
            llm_model=job.llm_model,
            job_id=job.id,
            trigger_type="manual",
            max_duration_seconds=job.max_duration_seconds,
            recursion_limit=job.recursion_limit,
            app=request.app,
            triggered_by_user_id=user.id
        )

        # Update job's last_run_at
        job.last_run_at = datetime.now(timezone.utc)
        session.add(job)
        session.commit()

        return _run_to_dict(run) if run else {"success": False, "error": "Run record not created"}
    except Exception as e:
        logger.error(f"Failed to run job {job_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============== Cron Invocation Endpoint ==============

@router.post("/invoke", response_class=JSONResponse)
async def invoke_due_jobs(
    request: Request,
    user: Optional[User] = Depends(scheduler_auth),
    session: Session = Depends(get_db_session)
):
    """
    Cron invocation endpoint - check and run all due jobs.

    Called by host crontab every minute via:
    curl -X POST http://localhost:8000/api/scheduled-jobs/invoke -H "X-Scheduler-Token: $TOKEN"
    """
    now = datetime.now(timezone.utc)

    # Find enabled jobs where next_run_at <= now
    stmt = select(ScheduledJob).where(
        ScheduledJob.is_enabled == True,
        ScheduledJob.next_run_at <= now
    )
    due_jobs = session.exec(stmt).all()

    if not due_jobs:
        return {"jobs_executed": 0, "message": "No jobs due"}

    from app.services.headless_agent_executor import execute_agent_headless

    results = []
    for job in due_jobs:
        try:
            logger.info(f"Cron triggering job: {job.name} (id={job.id})")

            run = await execute_agent_headless(
                agent_name=job.agent_name,
                prompt=job.prompt,
                llm_model=job.llm_model,
                job_id=job.id,
                trigger_type="cron",
                max_duration_seconds=job.max_duration_seconds,
                recursion_limit=job.recursion_limit,
                app=request.app,
                triggered_by_user_id=None  # Cron has no user context
            )

            # Update job timing
            job.last_run_at = datetime.now(timezone.utc)
            job.next_run_at = _calculate_next_run(job.cron_expression, job.timezone)
            session.add(job)

            results.append({
                "job_id": job.id,
                "job_name": job.name,
                "status": run.status if run else "unknown",
                "run_id": run.id if run else None
            })
        except Exception as e:
            logger.error(f"Cron job {job.id} failed: {e}", exc_info=True)
            results.append({
                "job_id": job.id,
                "job_name": job.name,
                "status": "error",
                "error": str(e)[:200]
            })

    session.commit()

    return {
        "jobs_executed": len(results),
        "results": results
    }


# ============== Run History Endpoints ==============

@router.get("/{job_id}/runs", response_class=JSONResponse)
async def get_job_runs(
    job_id: int,
    limit: int = 50,
    user: User = Depends(engineer_or_admin_required),
    session: Session = Depends(get_db_session)
):
    """Get run history for a specific job."""
    job = session.get(ScheduledJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    stmt = (
        select(ScheduledJobRun)
        .where(ScheduledJobRun.job_id == job_id)
        .order_by(ScheduledJobRun.created_at.desc())
        .limit(limit)
    )
    runs = session.exec(stmt).all()
    return [_run_to_dict(run) for run in runs]


@router.get("/runs/recent", response_class=JSONResponse)
async def get_recent_runs(
    limit: int = 50,
    user: User = Depends(engineer_or_admin_required),
    session: Session = Depends(get_db_session)
):
    """Get recent runs across all jobs."""
    stmt = (
        select(ScheduledJobRun)
        .order_by(ScheduledJobRun.created_at.desc())
        .limit(limit)
    )
    runs = session.exec(stmt).all()
    return [_run_to_dict(run) for run in runs]


@router.get("/runs/{run_id}", response_class=JSONResponse)
async def get_run_details(
    run_id: int,
    user: User = Depends(engineer_or_admin_required),
    session: Session = Depends(get_db_session)
):
    """Get details of a specific run."""
    run = session.get(ScheduledJobRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return _run_to_dict(run)
