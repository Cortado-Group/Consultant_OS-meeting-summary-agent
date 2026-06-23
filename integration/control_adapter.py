"""Control adapter for meeting-summary-agent."""
from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import datetime, timezone

from agent_sdk.contract.enums import EventType

import integration.state as state
from integration.event_store import emit
from integration.slack_notifier import SlackNotifier

logger = logging.getLogger(__name__)

_slack = SlackNotifier("meeting-summary-agent")

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_LOOKBACK_HOURS = int(os.environ.get("SUMMARY_LOOKBACK_HOURS", "48"))
_MAX_MEETINGS_PER_RUN = int(os.environ.get("SUMMARY_MAX_PER_RUN", "10"))


def _run_summary_pass() -> dict:
    """Single pass: fetch meetings without summaries, generate, write back."""
    from summary_analyzer import SummaryAnalyzer
    from integration.django_api_client import DjangoApiClient

    client = DjangoApiClient()
    analyzer = SummaryAnalyzer()

    meetings = client.get_recent_meetings(_LOOKBACK_HOURS)
    logger.info("summary_pass.meetings_fetched count=%d", len(meetings))

    created = skipped = processed = 0
    created_meetings: list[str] = []

    for meeting in meetings:
        if processed >= _MAX_MEETINGS_PER_RUN:
            break

        guid = meeting.get("guid", "")

        if not meeting.get("has_transcript"):
            skipped += 1
            continue

        # Skip if already has a summary (check from list response first, then detail if needed)
        if meeting.get("summary"):
            logger.debug("summary_pass.skip_has_summary guid=%s", guid)
            skipped += 1
            continue

        if client.has_topics(guid):
            logger.debug("summary_pass.skip_has_topics guid=%s", guid)
            skipped += 1
            continue

        detail = client.get_meeting_detail(guid)
        transcript = (detail.get("transcript") or "").strip()
        if not transcript:
            skipped += 1
            continue

        # Double-check summary from detail (list may omit it)
        if detail.get("summary"):
            logger.debug("summary_pass.skip_has_summary_detail guid=%s", guid)
            skipped += 1
            continue

        processed += 1
        meeting_name = meeting.get("name") or guid
        logger.info("summary_pass.analyzing guid=%s name=%r", guid, meeting_name)

        result = analyzer.analyze(transcript)
        if not result["summary"]:
            logger.info("summary_pass.no_summary guid=%s", guid)
            continue

        try:
            client.update_meeting_summary(guid, result["summary"])
            logger.info("summary_pass.summary_updated guid=%s", guid)
            created += 1
            created_meetings.append(meeting_name)
        except Exception as exc:
            logger.error("summary_pass.summary_update_failed guid=%s error=%s", guid, exc)
            continue

        topics_created = 0
        for topic in result.get("topics", []):
            try:
                client.create_topic(guid, topic)
                topics_created += 1
            except Exception as exc:
                logger.error("summary_pass.topic_create_failed guid=%s error=%s", guid, exc)

        logger.info("summary_pass.done guid=%s topics_created=%d", guid, topics_created)

    return {"created": created, "skipped": skipped, "processed": processed, "meetings": created_meetings}


def start(job_name: str, **kwargs) -> dict:
    correlation_id = kwargs.get("correlation_id")
    emit(EventType.STARTED, job_name, detail="start: clearing pause flag",
         data={"correlation_id": correlation_id} if correlation_id else None)
    with state.lock:
        state.paused = False
    emit(EventType.COMPLETED, job_name, detail="start: pause flag cleared",
         data={"correlation_id": correlation_id} if correlation_id else None)
    return {"job_name": job_name, "action": "start", "ok": True,
            "paused": False, "message": "Pause cleared. Runs will proceed."}


def soft_stop(job_name: str, **kwargs) -> dict:
    correlation_id = kwargs.get("correlation_id")
    emit(EventType.STARTED, job_name, detail="soft_stop: setting pause flag",
         data={"correlation_id": correlation_id} if correlation_id else None)
    with state.lock:
        state.paused = True
    emit(EventType.COMPLETED, job_name, detail="soft_stop: pause flag set",
         data={"correlation_id": correlation_id} if correlation_id else None)
    return {"job_name": job_name, "action": "soft_stop", "ok": True,
            "paused": True, "message": "Paused. Active work drains naturally."}


def hard_stop(job_name: str, **kwargs) -> dict:
    correlation_id = kwargs.get("correlation_id")
    emit(EventType.STARTED, job_name, detail="hard_stop: setting pause flag",
         data={"correlation_id": correlation_id} if correlation_id else None)
    with state.lock:
        state.paused = True
    emit(EventType.COMPLETED, job_name, detail="hard_stop: paused",
         data={"correlation_id": correlation_id} if correlation_id else None)
    return {"job_name": job_name, "action": "hard_stop", "ok": True,
            "paused": True, "revoked": 0, "message": "Hard-stopped."}


def manual_ping(job_name: str, **kwargs) -> dict:
    correlation_id = kwargs.get("correlation_id")
    emit(EventType.STARTED, job_name, detail="manual_ping: probing",
         data={"correlation_id": correlation_id} if correlation_id else None)
    with state.lock:
        paused = state.paused
    try:
        from integration.django_api_client import DjangoApiClient
        DjangoApiClient()._get("/api/v1/meetings/", occurred_after="2099-01-01")
        probe_msg = "pong — Django API reachable"
    except Exception as err:
        probe_msg = f"pong (Django API unreachable: {err})"
    emit(EventType.COMPLETED, job_name, detail=f"manual_ping: {probe_msg}",
         data={"correlation_id": correlation_id} if correlation_id else None)
    return {"job_name": job_name, "action": "manual_ping", "ok": True,
            "paused": paused, "message": probe_msg}


def run(job_name: str, **kwargs) -> dict:
    """Controller timer target — returns immediately, runs in background."""
    correlation_id = kwargs.get("correlation_id")
    emit(EventType.STARTED, job_name, detail="run: starting summary generation pass (background)",
         data={"correlation_id": correlation_id} if correlation_id else None)

    def _worker():
        try:
            result = _run_summary_pass()
            with state.lock:
                state.run_count += 1
                state.last_run_at = datetime.now(timezone.utc)
                count = state.run_count
            msg = (f"run #{count}: processed {result['processed']} meeting(s), "
                   f"summarized {result['created']}, skipped {result['skipped']}")
            logger.info("control.run.completed %s", msg)
            if result["created"] > 0:
                meetings_list = "\n".join(f"• {m}" for m in result.get("meetings", []))
                _slack.notify(f"summarized {result['created']} meeting(s):\n{meetings_list}")
            emit(EventType.COMPLETED, job_name, detail=msg,
                 data={"correlation_id": correlation_id} if correlation_id else None)
        except Exception as err:
            logger.error("control.run.failed error=%s", err, exc_info=True)
            _slack.notify(f"Run FAILED: {err}")
            emit(EventType.FAILED, job_name, detail=f"run failed: {err}",
                 data={"correlation_id": correlation_id} if correlation_id else None)

    threading.Thread(target=_worker, daemon=True, name=f"run-{correlation_id or 'noid'}").start()
    return {"job_name": job_name, "action": "run", "ok": True,
            "message": "summary generation started (background)"}


def manual_run(job_name: str, **kwargs) -> dict:
    """Single-pass run, only permitted while paused."""
    correlation_id = kwargs.get("correlation_id")
    with state.lock:
        paused = state.paused

    if not paused:
        emit(EventType.FAILED, job_name, detail="manual_run rejected: job is not paused",
             data={"correlation_id": correlation_id} if correlation_id else None)
        return {"job_name": job_name, "action": "manual_run", "ok": False,
                "paused": False, "message": "manual_run is only allowed while job is paused."}

    emit(EventType.STARTED, job_name, detail="manual_run: starting summary generation pass",
         data={"correlation_id": correlation_id} if correlation_id else None)
    try:
        result = _run_summary_pass()
        with state.lock:
            state.run_count += 1
            state.last_run_at = datetime.now(timezone.utc)
            count = state.run_count
        msg = (f"manual_run #{count}: processed {result['processed']} meeting(s), "
               f"summarized {result['created']}, skipped {result['skipped']}")
        if result["created"] > 0:
            meetings_list = "\n".join(f"• {m}" for m in result.get("meetings", []))
            _slack.notify(f"summarized {result['created']} meeting(s):\n{meetings_list}")
        emit(EventType.COMPLETED, job_name, detail=msg,
             data={"correlation_id": correlation_id} if correlation_id else None)
        return {"job_name": job_name, "action": "manual_run", "ok": True,
                "paused": True, "message": msg, **result}
    except Exception as err:
        logger.error("control.manual_run.failed error=%s", err, exc_info=True)
        _slack.notify(f"Manual run FAILED: {err}")
        emit(EventType.FAILED, job_name, detail=f"manual_run failed: {err}",
             data={"correlation_id": correlation_id} if correlation_id else None)
        return {"job_name": job_name, "action": "manual_run", "ok": False,
                "paused": True, "message": str(err)}
