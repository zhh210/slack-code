"""
Claude Code SDK Handler

Manages Claude Code sessions and processes messages using the SDK.
"""

import asyncio
import tempfile
from pathlib import Path
from typing import Optional
from dataclasses import dataclass
from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    UserMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ProcessError,
    CLINotFoundError,
)
from conversation_db import ConversationDB


@dataclass
class ClaudeResponse:
    """Response from Claude Code processing."""
    text: str
    created_files: list[Path]


class ClaudeCodeHandler:
    """Handles Claude Code SDK interactions for Slack bot."""

    def __init__(
        self,
        working_dir: str = ".",
        allowed_tools: Optional[list[str]] = None,
        max_turns: int = 10,
        db_path: str = "conversations.db",
    ):
        """
        Initialize the Claude Code handler.

        Args:
            working_dir: Directory where Claude Code will operate
            allowed_tools: List of allowed tools (default: safe read-only tools)
            max_turns: Maximum conversation turns per request
            db_path: Path to the SQLite database for conversation history
        """
        self.working_dir = Path(working_dir).resolve()
        self.max_turns = max_turns

        # Tools available to Claude
        # Includes Write for creating files that can be sent back to users
        self.allowed_tools = allowed_tools or [
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "WebSearch",
            "WebFetch",
        ]

        # Track files created during this session
        self.created_files: dict[str, list[Path]] = {}

        # Persistent conversation storage
        self.db = ConversationDB(db_path)

    async def process_message(
        self,
        prompt: str,
        conversation_key: str,
        extra_context: Optional[str] = None,
        image_paths: Optional[list[Path]] = None,
        output_dir: Optional[Path] = None,
    ) -> ClaudeResponse:
        """
        Process a message using Claude Code SDK.

        Uses session resumption for token efficiency - only injects history
        when a session cannot be resumed (e.g., after bot restart).

        Args:
            prompt: The user's message/prompt
            conversation_key: Unique key for this conversation thread
            extra_context: Optional additional context to prepend
            image_paths: Optional list of image file paths to include
            output_dir: Directory where Claude can write output files

        Returns:
            ClaudeResponse with text and list of created files
        """
        # Create output directory for files Claude creates
        if output_dir is None:
            output_dir = Path(tempfile.mkdtemp(prefix="claude_output_"))
        output_dir.mkdir(parents=True, exist_ok=True)

        # Try to get existing session ID for resumption
        session_id = self.db.get_session_id(conversation_key)

        # Build the prompt
        full_prompt = ""

        # Include Slack conversation context if provided
        if extra_context:
            full_prompt += extra_context + "\n"

        # Only inject history if we can't resume a session
        # This saves tokens on subsequent messages in a conversation
        if not session_id:
            history = self.db.get_history(conversation_key, limit=10)
            if history:
                full_prompt += "Previous conversation with you:\n"
                for msg in history[-10:]:
                    role = "User" if msg["role"] == "user" else "Assistant"
                    content = msg["content"][:500] + "..." if len(msg["content"]) > 500 else msg["content"]
                    full_prompt += f"{role}: {content}\n\n"
                full_prompt += "---\n\n"

        full_prompt += prompt

        # If images are provided, instruct Claude to read them
        if image_paths:
            paths_str = ", ".join(str(p) for p in image_paths)
            full_prompt = f"{full_prompt}\n\nIMPORTANT: First read and analyze the image file(s) at: {paths_str}"

        # Configure Claude Code options - use output_dir as working directory
        options = ClaudeAgentOptions(
            cwd=str(output_dir),
            allowed_tools=self.allowed_tools,
            max_turns=self.max_turns,
            system_prompt=self._get_system_prompt(output_dir),
            resume=session_id,  # Resume existing session if available
        )

        created_files: list[Path] = []

        try:
            response_parts = []
            tool_uses = []
            new_session_id = None

            async for message in query(prompt=full_prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            response_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            tool_uses.append({
                                "tool": block.name,
                                "input": block.input,
                            })
                            # Track files created with Write tool
                            if block.name == "Write":
                                file_path = block.input.get("file_path", "")
                                if file_path:
                                    created_files.append(Path(file_path))
                elif isinstance(message, ResultMessage):
                    # Capture session ID for future resumption
                    new_session_id = message.session_id

            # Build response
            response = "\n".join(response_parts)

            # Add tool usage summary if any tools were used
            if tool_uses:
                tool_summary = "\n".join(
                    f"  - `{t['tool']}`: {str(t['input'])[:100]}..."
                    for t in tool_uses[:5]  # Show max 5 tool uses
                )
                if len(tool_uses) > 5:
                    tool_summary += f"\n  - ... and {len(tool_uses) - 5} more"
                response = f"{response}\n\n_Tools used:_\n{tool_summary}"

            # Save session ID for future resumption (token efficient)
            if new_session_id:
                self.db.set_session_id(conversation_key, new_session_id)

            # Save to conversation history database (for recovery after restart)
            self.db.add_message(conversation_key, "user", prompt)
            self.db.add_message(conversation_key, "assistant", response)

            # Truncate response for Slack (max ~4000 chars to be safe)
            if len(response) > 3900:
                response = response[:3900] + "\n\n... _(response truncated)_"

            return ClaudeResponse(
                text=response or "I processed your request but have no text response.",
                created_files=created_files,
            )

        except CLINotFoundError:
            return ClaudeResponse(
                text=":warning: Claude Code CLI is not installed on this server. "
                     "Please install it with: `curl -fsSL https://claude.ai/install.sh | bash`",
                created_files=[],
            )
        except ProcessError as e:
            return ClaudeResponse(
                text=f":x: Claude Code process error (exit code {e.exit_code}): {str(e)}",
                created_files=[],
            )
        except Exception as e:
            return ClaudeResponse(
                text=f":x: Unexpected error: {str(e)}",
                created_files=[],
            )

    def reset_conversation(self, conversation_key: str) -> None:
        """Reset the conversation history and session for a given key."""
        self.db.clear_conversation(conversation_key)
        self.db.clear_session(conversation_key)

    def _get_system_prompt(self, output_dir: Path) -> str:
        """Get the system prompt for Claude Code."""
        return f"""You are a helpful coding assistant integrated with Slack.

Your responses will be displayed in Slack, so:
- Keep responses concise and well-formatted
- Use Slack-compatible markdown (single backticks for inline code, triple backticks for code blocks)
- Be direct and helpful
- When showing code, use appropriate syntax highlighting

You have access to tools for reading files, writing files, searching codebases, and web search.

IMPORTANT - File Creation:
- When asked to create files, use the Write tool to save them to: {output_dir}
- Files you create will be automatically uploaded and sent to the user in Slack
- Always use absolute paths starting with {output_dir}/ when writing files
- Example: {output_dir}/output.py or {output_dir}/report.txt"""


class ClaudeCodeHandlerWithEdits(ClaudeCodeHandler):
    """
    Extended handler that allows file editing operations.

    WARNING: Only use this if you trust users with write access to the working directory.
    """

    def __init__(self, working_dir: str = ".", max_turns: int = 10):
        super().__init__(
            working_dir=working_dir,
            allowed_tools=[
                "Read",
                "Write",
                "Edit",
                "Glob",
                "Grep",
                "Bash",  # Be careful with this!
                "WebSearch",
                "WebFetch",
            ],
            max_turns=max_turns,
        )
