"""
Slack Bot powered by Claude Code SDK

This bot allows you to interact with Claude Code through Slack.
Mention the bot or DM it to start a conversation.
"""

import os
import asyncio
import tempfile
import requests
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from claude_handler import ClaudeCodeHandler, ClaudeResponse


def download_slack_file(file_info: dict, bot_token: str, dest_dir: Path) -> Optional[Path]:
    """Download a file from Slack and return the local path."""
    url = file_info.get("url_private_download") or file_info.get("url_private")
    if not url:
        return None

    filename = file_info.get("name", "file")
    dest_path = dest_dir / filename

    headers = {"Authorization": f"Bearer {bot_token}"}
    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        dest_path.write_bytes(response.content)
        return dest_path
    return None


def get_slack_context(client, channel: str, thread_ts: Optional[str], current_ts: str, limit: int = 20) -> str:
    """
    Fetch Slack conversation context.

    - If in a thread: fetch all thread messages
    - If in channel: fetch recent channel messages

    Returns formatted context string.
    """
    messages = []
    bot_user_id = client.auth_test().get("user_id")

    try:
        if thread_ts and thread_ts != current_ts:
            # In a thread - fetch thread replies
            result = client.conversations_replies(
                channel=channel,
                ts=thread_ts,
                limit=50
            )
            messages = result.get("messages", [])
        else:
            # In channel - fetch recent messages
            result = client.conversations_history(
                channel=channel,
                limit=limit
            )
            messages = result.get("messages", [])
            # Reverse to get chronological order
            messages = list(reversed(messages))
    except Exception as e:
        print(f"Failed to fetch Slack context: {e}")
        return ""

    if not messages:
        return ""

    # Format messages for context
    context_lines = []
    for msg in messages:
        # Skip the current message
        if msg.get("ts") == current_ts:
            continue

        user = msg.get("user", "unknown")
        text = msg.get("text", "")

        # Replace user mentions with readable names
        if f"<@{bot_user_id}>" in text:
            text = text.replace(f"<@{bot_user_id}>", "@Claude")

        # Determine if it's the bot's message
        if user == bot_user_id or msg.get("bot_id"):
            speaker = "Claude"
        else:
            speaker = f"User <@{user}>"

        # Truncate very long messages
        if len(text) > 500:
            text = text[:500] + "..."

        context_lines.append(f"{speaker}: {text}")

    if not context_lines:
        return ""

    return "Recent conversation in this channel/thread:\n" + "\n".join(context_lines) + "\n---\n"

load_dotenv()

# Initialize Slack app
app = App(token=os.environ["SLACK_BOT_TOKEN"])

# Get database path from env or use default
default_db_dir = Path.home() / ".slack_connect"
default_db_path = default_db_dir / "conversations.db"
db_path = os.environ.get("SLACK_CONV_DB", str(default_db_path))

# Ensure database directory exists
Path(db_path).parent.mkdir(parents=True, exist_ok=True)

# Initialize Claude Code handler
claude_handler = ClaudeCodeHandler(
    working_dir=os.environ.get("CLAUDE_WORKING_DIR", os.getcwd()),
    db_path=db_path,
)

# Store conversation contexts per channel/thread
conversations: dict[str, list[dict]] = {}


def get_conversation_key(channel: str, thread_ts: Optional[str] = None) -> str:
    """Generate a unique key for each conversation thread."""
    return f"{channel}:{thread_ts or 'main'}"


@app.event("app_mention")
def handle_mention(event, say, client):
    """Handle when the bot is @mentioned in a channel."""
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    current_ts = event["ts"]
    text = event["text"]

    # Remove the bot mention from the text
    bot_user_id = client.auth_test()["user_id"]
    prompt = text.replace(f"<@{bot_user_id}>", "").strip()

    if not prompt:
        say(
            text="Hi! How can I help you? Ask me anything about code!",
            thread_ts=thread_ts
        )
        return

    # Send initial "thinking" message
    response = say(
        text=":thinking_face: Processing your request...",
        thread_ts=thread_ts
    )

    # Fetch Slack conversation context
    slack_context = get_slack_context(client, channel, event.get("thread_ts"), current_ts)

    # Create output directory for files Claude creates
    output_dir = Path(tempfile.mkdtemp(prefix="claude_output_"))

    # Process with Claude Code
    try:
        result = asyncio.run(claude_handler.process_message(
            prompt=prompt,
            conversation_key=get_conversation_key(channel, thread_ts),
            output_dir=output_dir,
            extra_context=slack_context if slack_context else None,
        ))

        # Update the message with the response text
        client.chat_update(
            channel=channel,
            ts=response["ts"],
            text=result.text
        )

        # Upload any files Claude created
        for file_path in result.created_files:
            if file_path.exists():
                try:
                    client.files_upload_v2(
                        channel=channel,
                        file=str(file_path),
                        filename=file_path.name,
                        thread_ts=thread_ts,
                        initial_comment=f":page_facing_up: Created file: `{file_path.name}`"
                    )
                except Exception as upload_err:
                    say(
                        text=f":warning: Failed to upload {file_path.name}: {upload_err}",
                        thread_ts=thread_ts
                    )

    except Exception as e:
        client.chat_update(
            channel=channel,
            ts=response["ts"],
            text=f":x: Error processing request: {str(e)}"
        )
    finally:
        # Clean up output files
        for p in output_dir.glob("*"):
            try:
                p.unlink()
            except:
                pass
        try:
            output_dir.rmdir()
        except:
            pass


@app.event("message")
def handle_dm(event, say, client):
    """Handle direct messages to the bot."""
    # Ignore messages from bots (including ourselves)
    if event.get("bot_id"):
        return

    # Allow file_share subtype (messages with images/files), ignore others
    subtype = event.get("subtype")
    if subtype and subtype not in ("file_share",):
        return

    # Only handle DMs (channel type "im")
    channel_type = event.get("channel_type")
    if channel_type != "im":
        return

    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    current_ts = event["ts"]
    prompt = event.get("text", "").strip()

    # Handle files/images attached to the message
    files = event.get("files", [])
    image_paths: list[Path] = []

    if files:
        # Create temp directory for downloaded files
        temp_dir = Path(tempfile.mkdtemp(prefix="slack_files_"))
        bot_token = os.environ["SLACK_BOT_TOKEN"]

        for file_info in files:
            file_path = download_slack_file(file_info, bot_token, temp_dir)
            if file_path:
                image_paths.append(file_path)

    # Need either text or files to process
    if not prompt and not image_paths:
        return

    # Build prompt with image context
    if image_paths:
        if not prompt:
            prompt = "Please analyze the attached image(s)."
        # Tell Claude about the images so it can read them
        image_list = "\n".join(f"  - {p}" for p in image_paths)
        prompt = f"{prompt}\n\n[Attached files - use Read tool to view them]:\n{image_list}"

    # Send initial "thinking" message
    response = say(
        text=":thinking_face: Processing your request...",
        thread_ts=thread_ts
    )

    # Fetch Slack conversation context (for threads in DMs)
    slack_context = get_slack_context(client, channel, event.get("thread_ts"), current_ts)

    # Create output directory for files Claude creates
    output_dir = Path(tempfile.mkdtemp(prefix="claude_output_"))

    # Process with Claude Code
    try:
        result = asyncio.run(claude_handler.process_message(
            prompt=prompt,
            conversation_key=get_conversation_key(channel, thread_ts),
            image_paths=image_paths,
            output_dir=output_dir,
            extra_context=slack_context if slack_context else None,
        ))

        # Update the message with the response text
        client.chat_update(
            channel=channel,
            ts=response["ts"],
            text=result.text
        )

        # Upload any files Claude created
        for file_path in result.created_files:
            if file_path.exists():
                try:
                    client.files_upload_v2(
                        channel=channel,
                        file=str(file_path),
                        filename=file_path.name,
                        thread_ts=thread_ts,
                        initial_comment=f":page_facing_up: Created file: `{file_path.name}`"
                    )
                except Exception as upload_err:
                    say(
                        text=f":warning: Failed to upload {file_path.name}: {upload_err}",
                        thread_ts=thread_ts
                    )

    except Exception as e:
        client.chat_update(
            channel=channel,
            ts=response["ts"],
            text=f":x: Error processing request: {str(e)}"
        )
    finally:
        # Clean up temp files
        for p in image_paths:
            try:
                p.unlink()
            except:
                pass
        # Clean up output files
        for p in output_dir.glob("*"):
            try:
                p.unlink()
            except:
                pass
        try:
            output_dir.rmdir()
        except:
            pass


@app.event("file_shared")
def handle_file_shared(event, logger):
    """Handle file_shared events (no-op to suppress warnings)."""
    # Files shared in DMs trigger this event
    # The actual file content can be processed via the message event if needed
    logger.debug(f"File shared: {event.get('file_id')}")


@app.event("reaction_added")
def handle_reaction_delete(event, client):
    """Handle reaction to delete bot messages. React with :x: to delete."""
    # Only handle :x: reaction
    if event.get("reaction") != "x":
        return

    try:
        item = event.get("item", {})
        channel = item.get("channel")
        ts = item.get("ts")

        if not channel or not ts:
            return

        # Get the message to check if it's from the bot
        result = client.conversations_history(
            channel=channel,
            latest=ts,
            inclusive=True,
            limit=1
        )

        messages = result.get("messages", [])
        if not messages:
            return

        message = messages[0]

        # Get bot's user ID
        auth_result = client.auth_test()
        bot_user_id = auth_result.get("user_id")

        # Only delete if it's a bot message
        if message.get("user") == bot_user_id or message.get("bot_id"):
            client.chat_delete(channel=channel, ts=ts)

    except Exception as e:
        print(f"Failed to delete message: {e}")


@app.command("/claude")
def handle_slash_command(ack, respond, command):
    """Handle /claude slash command."""
    ack()

    prompt = command.get("text", "").strip()
    channel = command["channel_id"]

    if not prompt:
        respond("Usage: `/claude <your question or task>`")
        return

    respond(":thinking_face: Processing your request...")

    try:
        result = asyncio.run(claude_handler.process_message(
            prompt=prompt,
            conversation_key=get_conversation_key(channel, "slash")
        ))
        respond(result)
    except Exception as e:
        respond(f":x: Error: {str(e)}")


@app.command("/claude-reset")
def handle_reset_command(ack, respond, command):
    """Reset conversation context for the current channel."""
    ack()
    channel = command["channel_id"]

    # Clear all conversations for this channel
    keys_to_remove = [k for k in conversations if k.startswith(f"{channel}:")]
    for key in keys_to_remove:
        del conversations[key]

    claude_handler.reset_conversation(get_conversation_key(channel, "slash"))
    respond(":white_check_mark: Conversation context has been reset.")


def main():
    """Start the Slack bot."""
    print("Starting Claude Code Slack Bot...")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()


if __name__ == "__main__":
    main()
