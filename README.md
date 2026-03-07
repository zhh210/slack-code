# Claude Code Slack Bot (Python)

A Slack bot powered by the [Claude Agent SDK](https://github.com/anthropics/claude-code-sdk-python), bringing Claude's coding capabilities directly into your Slack workspace.

## Features

- **@mention in channels**: Mention the bot to ask coding questions
- **Direct messages**: DM the bot for private conversations
- **Slash commands**: Use `/claude` for quick queries
- **Image support**: Share images for Claude to analyze
- **File creation**: Claude can create and send files back to you
- **Cross-channel search**: Search and read messages from any public channel (with user token)
- **Persistent sessions**: Conversation context maintained via `ClaudeSDKClient` — no database needed
- **Delete messages**: React with :x: to delete bot messages

## Prerequisites

- Python 3.10+
- Claude Code CLI installed (`curl -fsSL https://claude.ai/install.sh | bash`)
- A Slack workspace where you can create apps

## Project Structure

```
├── bot.py              # Main Slack bot (event handlers, async loop)
├── claude_handler.py   # ClaudeSDKClient integration + Slack tools
├── requirements.txt    # Python dependencies
├── .env.example        # Environment template
└── README.md
```

## Setup

### 1. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click **Create New App** > **From scratch**
3. Name your app (e.g., "Claude Code") and select your workspace

### 2. Configure Bot Permissions

Go to **OAuth & Permissions** and add these **Bot Token Scopes**:

```
app_mentions:read    - React to @mentions
channels:history     - Read channel history (for message deletion)
chat:write           - Send messages
files:read           - Download files users share
files:write          - Upload files to users
im:history           - Read DM history
im:read              - Access DM info
im:write             - Send DMs
commands             - Use slash commands
reactions:read       - Read reactions (for message deletion)
```

#### Optional: User Token Scopes (for cross-channel search)

To enable Claude to search and read messages across public channels, add these **User Token Scopes**:

```
search:read          - Search messages across workspace
channels:history     - Read public channel messages
channels:read        - List public channels
```

### 3. Enable Socket Mode

1. Go to **Socket Mode** in the sidebar
2. Enable Socket Mode
3. Create an **App-Level Token** with `connections:write` scope
4. Save this token as `SLACK_APP_TOKEN`

### 4. Subscribe to Events

Go to **Event Subscriptions**:

1. Enable Events
2. Subscribe to these **bot events**:
   - `app_mention`
   - `message.im`
   - `reaction_added`

### 5. Create Slash Commands (Optional)

Go to **Slash Commands** and create:

| Command | Description |
|---------|-------------|
| `/claude` | Ask Claude Code a question |
| `/claude-reset` | Reset conversation context |

### 6. Install the App

1. Go to **Install App**
2. Click **Install to Workspace**
3. Copy the **Bot User OAuth Token** as `SLACK_BOT_TOKEN`
4. If you added user token scopes, also copy the **User OAuth Token** as `SLACK_USER_TOKEN`

### 7. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your tokens:

```bash
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token
CLAUDE_WORKING_DIR=/path/to/your/project

# Optional: enable cross-channel Slack search
# SLACK_USER_TOKEN=xoxp-your-user-token
```

### 8. Install Dependencies & Run

```bash
pip install -r requirements.txt
python bot.py
```

## Usage

### In Channels
```
@Claude Code what does the main() function in app.py do?
```

### Direct Messages
Just send a message to the bot directly.

### With Images
Attach an image and ask Claude to analyze it.

### Create Files
Ask Claude to create a file and it will be uploaded to the chat.

### Cross-Channel Search
Ask Claude to find or read messages from other channels:
```
@Claude Code search for discussions about authentication in #engineering
@Claude Code what was the last message in #general?
```
Requires `SLACK_USER_TOKEN` to be configured.

### Slash Command
```
/claude explain this error: TypeError: 'NoneType' object is not subscriptable
```

### Reset Context
```
/claude-reset
```

### Delete Bot Messages
React with :x: on any bot message to delete it.

## How Sessions Work

Each conversation thread gets its own `ClaudeSDKClient` instance:

- **While running** — the client maintains full conversation context natively across messages
- **After restart** — a new client is created and Slack thread history is fetched via the API to restore context
- **Idle cleanup** — clients idle for 30+ minutes are automatically disconnected
- **No database** — Slack is the single source of truth for conversation history

Use `/claude-reset` to disconnect the client and start fresh.

## Security Considerations

By default, the bot enables these tools:
- `Read` - Read file contents
- `Write` - Create files (uploaded to Slack)
- `Edit` - Edit files
- `Glob` - Find files by pattern
- `Grep` - Search file contents
- `WebSearch` - Search the web
- `WebFetch` - Fetch web pages

When `SLACK_USER_TOKEN` is set, these additional tools are available:
- `search_slack` - Search messages across all public channels
- `read_channel_messages` - Read recent messages from a channel
- `list_channels` - List public channels

To use read-only mode, modify `claude_handler.py`:

```python
self.allowed_tools = [
    "Read",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
]
```

## Customization

### Change Working Directory

Set `CLAUDE_WORKING_DIR` in `.env` to the directory you want Claude to operate in.

### Adjust Response Length

Modify the truncation limit in `claude_handler.py`:

```python
if len(response) > 3900:  # Change this value
    response = response[:3900] + "\n\n... _(response truncated)_"
```

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────────┐
│   Slack     │────▶│   bot.py    │────▶│ claude_handler  │
│  Workspace  │◀────│ (Bolt App)  │◀────│ (SDKClient)     │
└─────────────┘     └──────┬──────┘     └────────┬────────┘
                           │                     │
                    ┌──────┴──────┐       ┌──────┴────────┐
                    │  Persistent │       │  Slack Tools  │
                    │  Event Loop │       │ (user token)  │
                    │  (threading)│       │  search/read  │
                    └─────────────┘       └───────────────┘
```

- **bot.py** — Slack Bolt event handlers run synchronously; a persistent background event loop bridges them to async `ClaudeSDKClient` calls
- **claude_handler.py** — manages one `ClaudeSDKClient` per conversation thread, with per-conversation locks, idle cleanup, and optional Slack search tools
- **Slack Tools** — registered as in-process function tools via the SDK; Claude invokes them autonomously when users ask about other channels

## Troubleshooting

### "Claude Code CLI not found"
Install the CLI: `curl -fsSL https://claude.ai/install.sh | bash`

### "Invalid token" errors
- Verify `SLACK_BOT_TOKEN` starts with `xoxb-`
- Verify `SLACK_APP_TOKEN` starts with `xapp-`
- Verify `SLACK_USER_TOKEN` starts with `xoxp-` (if using cross-channel search)
- Reinstall the app if tokens were regenerated

### Bot doesn't respond to DMs
- Check that `message.im` event is subscribed
- Verify `im:history` scope is added

### Bot doesn't respond to mentions
- Check that `app_mention` event is subscribed
- Verify `app_mentions:read` scope is added

### Can't delete bot messages
- Check that `reaction_added` event is subscribed
- Verify `reactions:read` and `channels:history` scopes are added

### Images not being processed
- Verify `files:read` scope is added
- Check that the bot can download files from Slack

### Cross-channel search not working
- Verify `SLACK_USER_TOKEN` is set in `.env`
- Verify `search:read` user token scope is added
- Reinstall the app after adding new scopes
