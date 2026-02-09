"""
Log Review Session
==================

Manages AI-powered analysis of agent session logs.
Queries SQLite agent_sessions.db for errors, tool usage, and patterns,
reads project config files, then streams a structured analysis via Claude.
"""

import asyncio
import json
import logging
import shutil
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Optional

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from dotenv import load_dotenv

from .chat_constants import ROOT_DIR

# Load environment variables from .env file if present
load_dotenv()

logger = logging.getLogger(__name__)

# Read-only feature MCP tools for context
LOG_REVIEW_FEATURE_TOOLS = [
    "mcp__features__feature_get_stats",
    "mcp__features__feature_get_summary",
]

# Truncation limits for project config files (characters)
CONFIG_TRUNCATION_LIMIT = 8000
PROMPT_TRUNCATION_LIMIT = 4000


class LogReviewSession:
    """
    Manages a log review analysis session.

    Gathers agent session logs and project configuration, builds a rich
    system prompt, and streams Claude's structured analysis to the client.
    """

    def __init__(self, project_name: str, project_dir: Path):
        self.project_name = project_name
        self.project_dir = project_dir
        self.client: Optional[ClaudeSDKClient] = None
        self.messages: list[dict] = []
        self.complete: bool = False
        self.created_at = datetime.now()
        self._conversation_id: Optional[str] = None
        self._client_entered: bool = False
        self._settings_file: Optional[Path] = None
        self._query_lock = asyncio.Lock()

    async def close(self) -> None:
        """Clean up resources and close the Claude client."""
        if self.client and self._client_entered:
            try:
                await self.client.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"Error closing Claude client: {e}")
            finally:
                self._client_entered = False
                self.client = None

        if self._settings_file and self._settings_file.exists():
            try:
                self._settings_file.unlink()
            except Exception as e:
                logger.warning(f"Error removing settings file: {e}")

    def _gather_log_context(self) -> str:
        """Query agent_sessions.db for recent sessions, errors, and tool usage."""
        from .agent_session_database import get_session_logs, get_sessions

        sections: list[str] = []

        # Get last 10 sessions metadata
        sessions = get_sessions(self.project_dir, self.project_name, limit=10)
        if not sessions:
            return "<no_sessions>\nNo agent sessions found. The agent has not been run yet for this project.\n</no_sessions>"

        # Format session metadata
        session_lines = []
        for s in sessions:
            duration = ""
            if s.get("started_at") and s.get("ended_at"):
                try:
                    start = datetime.fromisoformat(s["started_at"])
                    end = datetime.fromisoformat(s["ended_at"])
                    mins = (end - start).total_seconds() / 60
                    duration = f" ({mins:.0f}min)"
                except (ValueError, TypeError):
                    pass
            session_lines.append(
                f"  Session #{s['id']}: status={s['status']}, yolo={s['yolo_mode']}, "
                f"model={s.get('model', 'unknown')}, concurrency={s.get('max_concurrency', 'N/A')}, "
                f"started={s.get('started_at', '?')}{duration}"
            )
        sections.append(f"<sessions>\n{chr(10).join(session_lines)}\n</sessions>")

        # Get error logs from last 5 sessions (up to 50 per session)
        error_lines = []
        session_ids = [s["id"] for s in sessions[:5]]
        for sid in session_ids:
            errors = get_session_logs(self.project_dir, sid, line_type_filter="error", limit=50)
            for err in errors:
                content = err["content"][:500]  # Truncate long errors
                error_lines.append(
                    f"  [Session #{sid}] [{err.get('timestamp', '?')}] "
                    f"agent={err.get('agent_name', '?')}: {content}"
                )
        if error_lines:
            sections.append(f"<errors>\n{chr(10).join(error_lines[:200])}\n</errors>")
        else:
            sections.append("<errors>\nNo errors found in recent sessions.\n</errors>")

        # Get tool_use logs from last 3 sessions (up to 200 per session)
        tool_lines = []
        for sid in session_ids[:3]:
            tools = get_session_logs(self.project_dir, sid, line_type_filter="tool_use", limit=200)
            for t in tools:
                content = t["content"][:300]
                tool_lines.append(f"  [Session #{sid}] {content}")
        if tool_lines:
            sections.append(f"<tool_usage>\n{chr(10).join(tool_lines[:500])}\n</tool_usage>")

        # Get tool_result logs that contain error/failed/denied keywords
        result_lines = []
        for sid in session_ids[:5]:
            results = get_session_logs(self.project_dir, sid, line_type_filter="tool_result", limit=200)
            for r in results:
                content_lower = r["content"].lower()
                if any(kw in content_lower for kw in ("error", "failed", "denied", "permission", "not allowed")):
                    content = r["content"][:500]
                    result_lines.append(f"  [Session #{sid}] {content}")
        if result_lines:
            sections.append(f"<failed_tool_results>\n{chr(10).join(result_lines[:200])}\n</failed_tool_results>")

        return "\n\n".join(sections)

    def _read_project_config(self) -> str:
        """Read project configuration files with truncation."""
        from autoforge_paths import get_prompts_dir

        sections: list[str] = []
        prompts_dir = get_prompts_dir(self.project_dir)

        # allowed_commands.yaml
        ac_path = self.project_dir / ".autoforge" / "allowed_commands.yaml"
        if ac_path.exists():
            try:
                content = ac_path.read_text(encoding="utf-8")[:CONFIG_TRUNCATION_LIMIT]
                sections.append(f"<allowed_commands_yaml>\n{content}\n</allowed_commands_yaml>")
            except Exception:
                pass
        else:
            sections.append("<allowed_commands_yaml>\nNo allowed_commands.yaml found (using defaults only).\n</allowed_commands_yaml>")

        # coding_prompt.md
        coding_prompt = prompts_dir / "coding_prompt.md"
        if coding_prompt.exists():
            try:
                content = coding_prompt.read_text(encoding="utf-8")[:PROMPT_TRUNCATION_LIMIT]
                sections.append(f"<coding_prompt>\n{content}\n</coding_prompt>")
            except Exception:
                pass

        # initializer_prompt.md
        init_prompt = prompts_dir / "initializer_prompt.md"
        if init_prompt.exists():
            try:
                content = init_prompt.read_text(encoding="utf-8")[:PROMPT_TRUNCATION_LIMIT]
                sections.append(f"<initializer_prompt>\n{content}\n</initializer_prompt>")
            except Exception:
                pass

        # CLAUDE.md (at project root)
        claude_md = self.project_dir / "CLAUDE.md"
        if claude_md.exists():
            try:
                content = claude_md.read_text(encoding="utf-8")[:CONFIG_TRUNCATION_LIMIT]
                sections.append(f"<claude_md>\n{content}\n</claude_md>")
            except Exception:
                pass

        return "\n\n".join(sections) if sections else "No project configuration files found."

    def _build_system_prompt(self, log_context: str, config_context: str) -> str:
        """Assemble the analysis system prompt."""
        return f"""You are an expert DevOps analyst reviewing agent session logs for the AutoForge autonomous coding system.

Your task is to analyze the provided agent session logs and project configuration, then produce a structured report with actionable recommendations for improving autonomous agent execution.

## Project: {self.project_name}

## Agent Session Logs

{log_context}

## Project Configuration

{config_context}

## Report Format

Produce your analysis using the following sections. Use markdown formatting.

### 1. Executive Summary
2-3 sentences summarizing overall agent health and effectiveness. Mention session count, success rate, and the single biggest issue.

### 2. Command Analysis
Identify bash commands the agent used frequently (from tool_use logs) that may not be in the project's `allowed_commands.yaml`. For each:
- Command name and frequency
- Whether it was denied/failed due to permissions
- Exact YAML snippet to add to `allowed_commands.yaml`

If no command issues are found, say so explicitly.

### 3. Error Patterns
Group recurring errors by type (build errors, test failures, permission denials, crashes, timeouts). For each group:
- Error pattern description
- Frequency and affected sessions
- Root cause analysis
- Specific preventive measure

### 4. Efficiency Analysis
Analyze wasted effort patterns:
- Agent crashes and their causes
- Repeated failed attempts at the same task
- Time spent on tasks that could be avoided with better configuration
- YOLO vs standard mode effectiveness (if both were used)

### 5. Prompt Improvement Suggestions
Specific text to add to project prompts to prevent recurring issues. Provide suggestions as fenced code blocks that can be directly added to:
- `coding_prompt.md` — instructions for the coding agent
- `initializer_prompt.md` — instructions for feature creation
- `CLAUDE.md` — project-level agent instructions

### 6. Configuration Recommendations
Based on observed patterns, recommend changes to:
- Batch size (current behavior vs optimal)
- Concurrency level
- YOLO mode suitability for this project
- Any other AutoForge settings

### 7. Action Items
Top 5 prioritized changes, ordered by impact. Each should be a concrete, actionable step.

## Guidelines
- Only cite evidence from the actual logs provided. Do not speculate beyond what the data shows.
- If there are very few sessions or no errors, acknowledge this and provide what analysis you can.
- Format all configuration suggestions as code blocks so users can copy-paste them.
- Be specific: instead of "improve error handling", say exactly what prompt text to add and where.
- Use the Read, Glob, and Grep tools to look at relevant project files if you need more context."""

    async def start(self) -> AsyncGenerator[dict, None]:
        """Initialize session, gather logs, and stream initial analysis."""
        # Find and validate Claude CLI
        system_cli = shutil.which("claude")
        if not system_cli:
            yield {
                "type": "error",
                "content": "Claude CLI not found. Please install it: npm install -g @anthropic-ai/claude-code"
            }
            return

        # Gather log context
        yield {"type": "text", "content": "Analyzing agent session logs...\n\n"}
        log_context = self._gather_log_context()

        # Check for no sessions
        if "<no_sessions>" in log_context:
            yield {
                "type": "text",
                "content": "No agent sessions found for this project. Run the agent at least once to generate logs for analysis."
            }
            yield {"type": "analysis_complete"}
            self.complete = True
            return

        config_context = self._read_project_config()
        system_prompt = self._build_system_prompt(log_context, config_context)

        # Create temporary security settings file
        security_settings = {
            "sandbox": {"enabled": True},
            "permissions": {
                "defaultMode": "bypassPermissions",
                "allow": [
                    "Read(./**)",
                    "Glob(./**)",
                    "Grep(./**)",
                    *LOG_REVIEW_FEATURE_TOOLS,
                ],
            },
        }
        from autoforge_paths import get_expand_settings_path
        settings_file = get_expand_settings_path(self.project_dir, uuid.uuid4().hex)
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        self._settings_file = settings_file
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump(security_settings, f, indent=2)

        # Build environment overrides
        from registry import get_effective_sdk_env, get_model_for_role
        sdk_env = get_effective_sdk_env()
        model = get_model_for_role("log_review")

        # Build MCP servers config for feature read tools
        mcp_servers = {
            "features": {
                "command": sys.executable,
                "args": ["-m", "mcp_server.feature_mcp"],
                "env": {
                    "PROJECT_DIR": str(self.project_dir.resolve()),
                    "PYTHONPATH": str(ROOT_DIR.resolve()),
                },
            },
        }

        # Create Claude SDK client
        try:
            self.client = ClaudeSDKClient(
                options=ClaudeAgentOptions(
                    model=model,
                    cli_path=system_cli,
                    system_prompt=system_prompt,
                    allowed_tools=[
                        "Read",
                        "Glob",
                        "Grep",
                        *LOG_REVIEW_FEATURE_TOOLS,
                    ],
                    mcp_servers=mcp_servers,  # type: ignore[arg-type]
                    permission_mode="bypassPermissions",
                    max_turns=50,
                    cwd=str(self.project_dir.resolve()),
                    settings=str(settings_file.resolve()),
                    env=sdk_env,
                )
            )
            await self.client.__aenter__()
            self._client_entered = True
        except Exception:
            logger.exception("Failed to create Claude client for log review")
            yield {
                "type": "error",
                "content": "Failed to initialize Claude for log analysis"
            }
            return

        # Start the analysis
        try:
            async with self._query_lock:
                async for chunk in self._query_claude(
                    "Analyze the agent session logs provided in your system prompt and produce the structured report."
                ):
                    yield chunk

            yield {"type": "analysis_complete"}
            self.complete = True
            yield {"type": "response_done"}
        except Exception:
            logger.exception("Failed to start log review analysis")
            yield {
                "type": "error",
                "content": "Failed to start analysis"
            }

    async def send_message(self, user_message: str) -> AsyncGenerator[dict, None]:
        """Send a follow-up question and stream Claude's response."""
        if not self.client:
            yield {
                "type": "error",
                "content": "Session not initialized. Call start() first."
            }
            return

        self.messages.append({
            "role": "user",
            "content": user_message,
            "timestamp": datetime.now().isoformat()
        })

        try:
            async with self._query_lock:
                async for chunk in self._query_claude(user_message):
                    yield chunk
            yield {"type": "response_done"}
        except Exception:
            logger.exception("Error during log review follow-up")
            yield {
                "type": "error",
                "content": "Error while processing follow-up question"
            }

    async def _query_claude(self, message: str) -> AsyncGenerator[dict, None]:
        """Internal method to query Claude and stream responses."""
        if not self.client:
            return

        await self.client.query(message)

        async for msg in self.client.receive_response():
            msg_type = type(msg).__name__

            if msg_type == "AssistantMessage" and hasattr(msg, "content"):
                for block in msg.content:
                    block_type = type(block).__name__

                    if block_type == "TextBlock" and hasattr(block, "text"):
                        text = block.text
                        if text:
                            yield {"type": "text", "content": text}

                            self.messages.append({
                                "role": "assistant",
                                "content": text,
                                "timestamp": datetime.now().isoformat()
                            })

    def is_complete(self) -> bool:
        """Check if analysis is complete."""
        return self.complete

    def get_messages(self) -> list[dict]:
        """Get all messages in the conversation."""
        return self.messages.copy()


# Session registry with thread safety
_log_review_sessions: dict[str, LogReviewSession] = {}
_log_review_sessions_lock = threading.Lock()


def get_log_review_session(project_name: str) -> Optional[LogReviewSession]:
    """Get an existing log review session for a project."""
    with _log_review_sessions_lock:
        return _log_review_sessions.get(project_name)


async def create_log_review_session(project_name: str, project_dir: Path) -> LogReviewSession:
    """Create a new log review session, closing any existing one."""
    old_session: Optional[LogReviewSession] = None

    with _log_review_sessions_lock:
        old_session = _log_review_sessions.pop(project_name, None)
        session = LogReviewSession(project_name, project_dir)
        _log_review_sessions[project_name] = session

    if old_session:
        try:
            await old_session.close()
        except Exception as e:
            logger.warning(f"Error closing old log review session for {project_name}: {e}")

    return session


async def remove_log_review_session(project_name: str) -> None:
    """Remove and close a log review session."""
    session: Optional[LogReviewSession] = None

    with _log_review_sessions_lock:
        session = _log_review_sessions.pop(project_name, None)

    if session:
        try:
            await session.close()
        except Exception as e:
            logger.warning(f"Error closing log review session for {project_name}: {e}")


async def cleanup_all_log_review_sessions() -> None:
    """Close all active log review sessions. Called on server shutdown."""
    sessions_to_close: list[LogReviewSession] = []

    with _log_review_sessions_lock:
        sessions_to_close = list(_log_review_sessions.values())
        _log_review_sessions.clear()

    for session in sessions_to_close:
        try:
            await session.close()
        except Exception as e:
            logger.warning(f"Error closing log review session {session.project_name}: {e}")
