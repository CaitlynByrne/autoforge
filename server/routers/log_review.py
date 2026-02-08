"""
Log Review Router
=================

WebSocket and REST endpoints for AI-powered agent log analysis.
Streams a structured analysis of agent session logs and project configuration.
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ..services.log_review_session import (
    LogReviewSession,
    create_log_review_session,
    get_log_review_session,
    remove_log_review_session,
)
from ..utils.project_helpers import get_project_path as _get_project_path
from ..utils.validation import validate_project_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/log-review", tags=["log-review"])


# ============================================================================
# REST Endpoints
# ============================================================================

class LogReviewSessionStatus(BaseModel):
    """Status of a log review session."""
    project_name: str
    is_active: bool
    is_complete: bool
    message_count: int


@router.get("/sessions/{project_name}", response_model=LogReviewSessionStatus)
async def get_log_review_session_status(project_name: str):
    """Get status of a log review session."""
    project_name = validate_project_name(project_name)

    session = get_log_review_session(project_name)
    if not session:
        raise HTTPException(status_code=404, detail="No active log review session for this project")

    return LogReviewSessionStatus(
        project_name=project_name,
        is_active=True,
        is_complete=session.is_complete(),
        message_count=len(session.get_messages()),
    )


@router.delete("/sessions/{project_name}")
async def cancel_log_review_session(project_name: str):
    """Cancel and remove a log review session."""
    project_name = validate_project_name(project_name)

    session = get_log_review_session(project_name)
    if not session:
        raise HTTPException(status_code=404, detail="No active log review session for this project")

    await remove_log_review_session(project_name)
    return {"success": True, "message": "Log review session cancelled"}


# ============================================================================
# WebSocket Endpoint
# ============================================================================

@router.websocket("/ws/{project_name}")
async def log_review_websocket(websocket: WebSocket, project_name: str):
    """
    WebSocket endpoint for AI-powered agent log analysis.

    Message protocol:

    Client -> Server:
    - {"type": "start"} - Start the analysis
    - {"type": "message", "content": "..."} - Follow-up question
    - {"type": "ping"} - Keep-alive ping

    Server -> Client:
    - {"type": "text", "content": "..."} - Text chunk from Claude
    - {"type": "analysis_complete"} - Analysis finished (follow-ups now allowed)
    - {"type": "response_done"} - Response complete
    - {"type": "error", "content": "..."} - Error message
    - {"type": "pong"} - Keep-alive pong
    """
    await websocket.accept()

    try:
        project_name = validate_project_name(project_name)
    except HTTPException:
        await websocket.send_json({"type": "error", "content": "Invalid project name"})
        await websocket.close(code=4000, reason="Invalid project name")
        return

    # Look up project directory from registry
    project_dir = _get_project_path(project_name)
    if not project_dir:
        await websocket.send_json({"type": "error", "content": "Project not found in registry"})
        await websocket.close(code=4004, reason="Project not found in registry")
        return

    if not project_dir.exists():
        await websocket.send_json({"type": "error", "content": "Project directory not found"})
        await websocket.close(code=4004, reason="Project directory not found")
        return

    session: Optional[LogReviewSession] = None

    try:
        while True:
            try:
                data = await websocket.receive_text()
                message = json.loads(data)
                msg_type = message.get("type")

                if msg_type == "ping":
                    await websocket.send_json({"type": "pong"})
                    continue

                elif msg_type == "start":
                    # Check if session already exists (idempotent start)
                    existing_session = get_log_review_session(project_name)
                    if existing_session:
                        session = existing_session
                        await websocket.send_json({
                            "type": "text",
                            "content": "Resuming existing log review session. Ask a follow-up question or close and reopen for a fresh analysis."
                        })
                        await websocket.send_json({"type": "analysis_complete"})
                        await websocket.send_json({"type": "response_done"})
                    else:
                        # Create and start a new log review session
                        session = await create_log_review_session(project_name, project_dir)

                        # Stream the analysis
                        async for chunk in session.start():
                            await websocket.send_json(chunk)

                elif msg_type == "message":
                    if not session:
                        session = get_log_review_session(project_name)
                        if not session:
                            await websocket.send_json({
                                "type": "error",
                                "content": "No active session. Send 'start' first."
                            })
                            continue

                    user_content = message.get("content", "").strip()
                    if not user_content:
                        await websocket.send_json({
                            "type": "error",
                            "content": "Empty message"
                        })
                        continue

                    # Stream Claude's response to follow-up
                    async for chunk in session.send_message(user_content):
                        await websocket.send_json(chunk)

                else:
                    await websocket.send_json({
                        "type": "error",
                        "content": f"Unknown message type: {msg_type}"
                    })

            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error",
                    "content": "Invalid JSON"
                })

    except WebSocketDisconnect:
        logger.info(f"Log review WebSocket disconnected for {project_name}")

    except Exception:
        logger.exception(f"Log review WebSocket error for {project_name}")
        try:
            await websocket.send_json({
                "type": "error",
                "content": "Internal server error"
            })
        except Exception:
            pass

    finally:
        # Don't remove the session on disconnect - allow resume
        pass
