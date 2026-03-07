"""
Claude Code SDK Handler

Manages Claude Code sessions and processes messages using the ClaudeSDKClient.
Each conversation thread gets its own persistent client with automatic context retention.
Slack conversation history is the source of truth — no local database needed.
"""

import asyncio
import time
import tempfile
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass
from slack_sdk import WebClient
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ProcessError,
    CLINotFoundError,
    tool,
    create_sdk_mcp_server,
)


@dataclass
class ClaudeResponse:
    """Response from Claude Code processing."""
    text: str
    created_files: list[Path]


def create_slack_tools(slack_user_token: str):
    """Create Slack tool functions using the provided user token."""
    client = WebClient(token=slack_user_token)

    @tool(
        "search_slack",
        "Search Slack messages across all public channels. "
        "Supports Slack search modifiers: in:#channel, from:@user, before:date, after:date, has:link, etc.",
        {"query": str, "count": int},
    )
    async def search_slack(args: dict[str, Any]) -> dict[str, Any]:
        query = args["query"]
        count = min(args.get("count", 10), 20)
        result = client.search_messages(query=query, count=count, sort="timestamp")
        matches = result.get("messages", {}).get("matches", [])
        if not matches:
            return {"content": [{"type": "text", "text": f"No results for: {query}"}]}
        lines = []
        for msg in matches:
            channel_name = msg.get("channel", {}).get("name", "unknown")
            user = msg.get("username", "unknown")
            text = msg.get("text", "")[:300]
            ts = msg.get("ts", "")
            lines.append(f"#{channel_name} | {user} | {ts}\n{text}")
        return {"content": [{"type": "text", "text": "\n---\n".join(lines)}]}

    @tool(
        "read_channel_messages",
        "Read recent messages from a Slack channel by its ID (e.g. C0AHV43APR8). "
        "Use search_slack or list_channels first to find channel IDs.",
        {"channel_id": str, "limit": int},
    )
    async def read_channel_messages(args: dict[str, Any]) -> dict[str, Any]:
        channel_id = args["channel_id"]
        limit = min(args.get("limit", 20), 50)
        result = client.conversations_history(channel=channel_id, limit=limit)
        messages = result.get("messages", [])
        if not messages:
            return {"content": [{"type": "text", "text": "No messages found."}]}
        lines = []
        for msg in reversed(messages):
            user = msg.get("user", "unknown")
            text = msg.get("text", "")[:500]
            lines.append(f"<@{user}>: {text}")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    @tool(
        "list_channels",
        "List public Slack channels. Use to discover channel names and IDs.",
        {"limit": int},
    )
    async def list_channels(args: dict[str, Any]) -> dict[str, Any]:
        limit = min(args.get("limit", 100), 200)
        result = client.conversations_list(
            types="public_channel", limit=limit, exclude_archived=True,
        )
        channels = result.get("channels", [])
        if not channels:
            return {"content": [{"type": "text", "text": "No channels found."}]}
        lines = [
            f"#{ch['name']} (ID: {ch['id']}) - {ch.get('purpose', {}).get('value', '')[:80]}"
            for ch in channels
        ]
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    return create_sdk_mcp_server(
        name="slack",
        tools=[search_slack, read_channel_messages, list_channels],
    )


class ClaudeCodeHandler:
    """Handles Claude Code SDK interactions for Slack bot using persistent clients."""

    # Idle timeout for client cleanup (30 minutes)
    CLIENT_IDLE_TIMEOUT = 1800

    def __init__(
        self,
        working_dir: str = ".",
        allowed_tools: Optional[list[str]] = None,
        max_turns: int = 10,
        slack_user_token: Optional[str] = None,
    ):
        """
        Initialize the Claude Code handler.

        Args:
            working_dir: Directory where Claude Code will operate
            allowed_tools: List of allowed tools (default: safe read-only tools)
            max_turns: Maximum conversation turns per request
            slack_user_token: Slack user token (xoxp-) for cross-channel search
        """
        self.working_dir = Path(working_dir).resolve()
        self.max_turns = max_turns

        # Slack tools via user token
        self._slack_server = None
        if slack_user_token:
            self._slack_server = create_slack_tools(slack_user_token)

        self.allowed_tools = allowed_tools or [
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "WebSearch",
            "WebFetch",
        ]
        # Add Slack tool permissions if user token is available
        if self._slack_server:
            self.allowed_tools.extend([
                "mcp__slack__search_slack",
                "mcp__slack__read_channel_messages",
                "mcp__slack__list_channels",
            ])

        # Per-conversation client management
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_activity: dict[str, float] = {}

    def _get_lock(self, conversation_key: str) -> asyncio.Lock:
        """Get or create a per-conversation lock to serialize requests."""
        if conversation_key not in self._locks:
            self._locks[conversation_key] = asyncio.Lock()
        return self._locks[conversation_key]

    async def _get_or_create_client(
        self, conversation_key: str
    ) -> ClaudeSDKClient:
        """Get existing client or create and connect a new one."""
        if conversation_key in self._clients:
            self._last_activity[conversation_key] = time.monotonic()
            return self._clients[conversation_key]

        options = ClaudeAgentOptions(
            cwd=str(self.working_dir),
            allowed_tools=self.allowed_tools,
            max_turns=self.max_turns,
            system_prompt=self._get_system_prompt(),
            mcp_servers={"slack": self._slack_server} if self._slack_server else None,
        )

        client = ClaudeSDKClient(options=options)
        await client.connect()

        self._clients[conversation_key] = client
        self._last_activity[conversation_key] = time.monotonic()
        return client

    async def _remove_client(self, conversation_key: str) -> None:
        """Remove and disconnect a client."""
        client = self._clients.pop(conversation_key, None)
        self._last_activity.pop(conversation_key, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def process_message(
        self,
        prompt: str,
        conversation_key: str,
        extra_context: Optional[str] = None,
        image_paths: Optional[list[Path]] = None,
        output_dir: Optional[Path] = None,
    ) -> ClaudeResponse:
        """
        Process a message using a persistent ClaudeSDKClient per conversation.

        The client automatically maintains conversation context across calls.
        A per-conversation lock ensures messages are processed sequentially.

        Args:
            prompt: The user's message/prompt
            conversation_key: Unique key for this conversation thread
            extra_context: Optional additional context to prepend
            image_paths: Optional list of image file paths to include
            output_dir: Directory where Claude can write output files

        Returns:
            ClaudeResponse with text and list of created files
        """
        lock = self._get_lock(conversation_key)
        async with lock:
            return await self._process_message_impl(
                prompt, conversation_key, extra_context, image_paths, output_dir
            )

    async def _process_message_impl(
        self,
        prompt: str,
        conversation_key: str,
        extra_context: Optional[str] = None,
        image_paths: Optional[list[Path]] = None,
        output_dir: Optional[Path] = None,
    ) -> ClaudeResponse:
        # Create output directory for files Claude creates
        if output_dir is None:
            output_dir = Path(tempfile.mkdtemp(prefix="claude_output_"))
        output_dir.mkdir(parents=True, exist_ok=True)

        client = await self._get_or_create_client(conversation_key)

        # Build the prompt
        # Slack conversation history is passed via extra_context by the caller,
        # which handles recovery after restart (no local DB needed).
        full_prompt = ""

        if extra_context:
            full_prompt += extra_context + "\n"

        full_prompt += prompt

        # Per-message output directory instruction
        full_prompt += (
            f"\n\nIMPORTANT - File Creation: "
            f"When asked to create files, save them to: {output_dir}\n"
            f"Always use absolute paths starting with {output_dir}/"
        )

        if image_paths:
            paths_str = ", ".join(str(p) for p in image_paths)
            full_prompt += f"\n\nIMPORTANT: First read and analyze the image file(s) at: {paths_str}"

        created_files: list[Path] = []

        try:
            response_parts = []
            tool_uses = []

            await client.query(full_prompt)

            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            response_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            tool_uses.append({
                                "tool": block.name,
                                "input": block.input,
                            })
                            if block.name == "Write":
                                file_path = block.input.get("file_path", "")
                                if file_path:
                                    created_files.append(Path(file_path))

            # Build response
            response = "\n".join(response_parts)

            if tool_uses:
                tool_summary = "\n".join(
                    f"  - `{t['tool']}`: {str(t['input'])[:100]}..."
                    for t in tool_uses[:5]
                )
                if len(tool_uses) > 5:
                    tool_summary += f"\n  - ... and {len(tool_uses) - 5} more"
                response = f"{response}\n\n_Tools used:_\n{tool_summary}"

            # Truncate response for Slack (max ~4000 chars to be safe)
            if len(response) > 3900:
                response = response[:3900] + "\n\n... _(response truncated)_"

            return ClaudeResponse(
                text=response or "I processed your request but have no text response.",
                created_files=created_files,
            )

        except CLINotFoundError:
            await self._remove_client(conversation_key)
            return ClaudeResponse(
                text=":warning: Claude Code CLI is not installed on this server. "
                     "Please install it with: `curl -fsSL https://claude.ai/install.sh | bash`",
                created_files=[],
            )
        except ProcessError as e:
            await self._remove_client(conversation_key)
            return ClaudeResponse(
                text=f":x: Claude Code process error (exit code {e.exit_code}): {str(e)}",
                created_files=[],
            )
        except Exception as e:
            await self._remove_client(conversation_key)
            return ClaudeResponse(
                text=f":x: Unexpected error: {str(e)}",
                created_files=[],
            )

    async def reset_conversation(self, conversation_key: str) -> None:
        """Reset the conversation by disconnecting the client."""
        await self._remove_client(conversation_key)

    async def cleanup_idle_clients(self) -> None:
        """Remove clients that have been idle beyond CLIENT_IDLE_TIMEOUT."""
        now = time.monotonic()
        idle_keys = [
            key for key, last in self._last_activity.items()
            if now - last > self.CLIENT_IDLE_TIMEOUT
        ]
        for key in idle_keys:
            await self._remove_client(key)

    async def close(self) -> None:
        """Disconnect all clients. Call on bot shutdown."""
        keys = list(self._clients.keys())
        for key in keys:
            await self._remove_client(key)

    def _get_system_prompt(self) -> str:
        """Get the system prompt for Claude Code."""
        return """You are a helpful coding assistant integrated with Slack.

Your responses will be displayed in Slack, so:
- Keep responses concise and well-formatted
- Use Slack-compatible markdown (single backticks for inline code, triple backticks for code blocks)
- Be direct and helpful
- When showing code, use appropriate syntax highlighting

You have access to tools for reading files, writing files, searching codebases, and web search.

IMPORTANT - File Creation:
- When asked to create files, use the Write tool to save them to the directory specified in the user message
- Files you create will be automatically uploaded and sent to the user in Slack
- Always use absolute paths when writing files"""


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
