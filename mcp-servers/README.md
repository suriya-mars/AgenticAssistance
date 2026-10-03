# AgentBuild MCP servers

AgentBuild's tools as **MCP servers**: documents, mail, the BUG quality process, and release QC, usable from any
[Model Context Protocol](https://modelcontextprotocol.io) client (Claude Desktop, VS Code, Claude Code, other agents).

This is a **separate project**. It imports the `agentbuild` package from the parent folder and never changes it:
the same config, data and local Ollama models are used, so the CLI and the MCP tools always see the same bugs and
documents.

```
AgentBuild/                 ← the existing project (unchanged)
└── mcp-servers/            ← this project
    ├── src/agentbuild_mcp/
    │   ├── server.py       entry point, tool groups, stdio / HTTP
    │   ├── runtime.py      builds AgentBuild per call, runs it off the event loop, progress heartbeats
    │   ├── approvals.py    human confirmation over MCP (previews, needs_decision)
    │   ├── guards.py       allowed folders, untrusted-text wrapping, mcp.yaml
    │   └── tools/          docs.py · mail.py · bug.py · qc.py · system.py
    ├── tests/              19 tests through a real MCP client (in memory, stdio subprocess, HTTP)
    ├── examples/           client configs
    └── mcp.yaml            MCP-only settings
```

## The tools

| Group | Tools |
|---|---|
| **docs** | `docs_add`, `docs_list`, `docs_search`, `docs_ask` (local model, quotes checked against the page), `docs_remove` |
| **mail** | `mail_list_unread`, `mail_read`, `mail_search`, `mail_triage`, `mail_draft_reply` (never sent), `tasks_list` |
| **bug** | `bug_process`, `bug_create`, `bug_update`, `bug_list`, `bug_status`, `bug_scan`, `bug_draft`, `bug_sync`, `bug_ask`, `bug_mark_step`, `bug_reopen_step`, `bug_restart`, `bug_delete` |
| **qc** | `qc_list_checks`, `qc_run_check`, `qc_run`, `repo_read_file` |
| **system** | `system_health` (in every server), `runs_list`, `run_trace` |

Every tool is annotated as read-only, changing or destructive, so clients can ask before running anything that
isn't read-only. `agentbuild-mcp --list-tools` prints the list.

### How the user stays in control

The CLI asks `y/N`. Over MCP the person is behind the client, so changes work in two steps:

- **Preview, then confirm.** `bug_draft`, `bug_restart`, `bug_delete`, `bug_mark_step`, `bug_reopen_step`,
  `qc_run`, `qc_run_check` and `docs_remove` first run with `confirm=false`: nothing changes and you get the exact
  draft, command or effect. The client calls again with `confirm=true` only after the user agrees.
- **Questions from replies.** `bug_sync` returns `status: "needs_decision"` with a question, for example *"Mark
  'Security team approved' for BUG-12345?"* with the sender, date and subject. The client asks the user and calls
  `bug_sync` again with `decisions={"<id>": "approve" | "reject"}`, repeating until `status: "done"`. Steps are
  recorded only after an answer.

The server tells clients these rules in its MCP instructions.

## Setup

Requires AgentBuild's own setup first (Python 3.11+, Ollama with the models; see `../README.md`).

```bash
cd mcp-servers
./setup.sh                 # Linux / macOS
setup-windows.cmd          # Windows (cmd or PowerShell, no admin rights)
```
This creates `mcp-servers/venv` with both packages, prints the tools, and shows the command that clients
should launch, e.g. `…/mcp-servers/venv/bin/agentbuild-mcp` (on Windows `…\venv\Scripts\agentbuild-mcp.exe`).

## Connect a client

Clients start the server themselves over **stdio**. Use the full path to `agentbuild-mcp`:

| Client | How |
|---|---|
| **Claude Desktop** | Settings → Developer → Edit Config; add the entry from [`examples/claude_desktop_config.json`](examples/claude_desktop_config.json) |
| **VS Code** | Copy [`examples/vscode-mcp.json`](examples/vscode-mcp.json) to `.vscode/mcp.json` |
| **Claude Code** | `claude mcp add agentbuild -- /full/path/to/mcp-servers/venv/bin/agentbuild-mcp` |
| **Any other MCP client** | command `agentbuild-mcp`, no arguments (or `--tools docs,bug` for fewer tools) |

**HTTP** (for clients that connect by URL, or another machine):
```bash
export AGENT_MCP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
venv/bin/agentbuild-mcp --transport http                 # http://127.0.0.1:8765/mcp
venv/bin/agentbuild-mcp --transport http --host 0.0.0.0  # other machines; refuses to start without the token
```
Clients send `Authorization: Bearer <token>`. Without a token the server only listens on localhost.

> **Privacy: read this before connecting a cloud client.** Tool results are read by the client's model. With
> Claude Desktop, Claude Code or any cloud-hosted model, email text, ASoC findings and document passages leave your
> machine. AgentBuild itself stays local, but what the client sees does not. For confidential work data, use a
> client that runs a local model, or keep to tools that return little raw text (`docs_ask` and `bug_status` return
> answers and summaries; `mail_read`, `docs_search`, `repo_read_file` and `run_trace` return raw text).
> Check your company's policy.

## Configuration

| Where | What |
|---|---|
| `mcp.yaml` | `agentbuild_home` (default `..`), `allowed_doc_roots` (default `files`, `samples`), `max_text_chars`, HTTP host and port |
| `../config/*.yaml` | Everything else, shared with the CLI: models, mail, recipients, QC checks, `qc.allowed_repo_roots` |
| `../.env` | Read at start (e.g. `AGENT_IMAP_PASSWORD`, model choices); real environment variables win |
| Environment | `AGENTBUILD_HOME`, `AGENT_MCP_CONFIG`, `AGENT_MCP_DOC_ROOTS` (extra folders, `:`-separated, `;` on Windows), `AGENT_MCP_TOKEN` |

Paths in tool arguments are relative to the AgentBuild folder, e.g. `files/report.pdf` or `repos/my-service`.
Config edits apply to the next tool call; no restart is needed.

## Safety

- **Files:** documents only from `allowed_doc_roots` (symlinks resolved first), and repositories only under
  `qc.allowed_repo_roots`. Anything else is refused, so a client can't read `~/.ssh`. `repo_read_file` can't
  leave its repository.
- **Outside text:** email, document and repository text is cleaned and wrapped in `<untrusted_…>` tags. Mail
  results (`mail_read`, `mail_triage`) and ASoC scan results also list phrases that look like prompt injection in
  `injection_flags`.
- **Errors:** messages for problems you can act on are passed back to the client. Unexpected errors return only
  "Error executing tool", with details in the server's stderr log.
- **stdio:** nothing but protocol is written to stdout (logs and PyMuPDF messages go to stderr).
- **AgentBuild's own guardrails still apply:** sender allowlists, step order, the security gate, sandboxed
  checks, and audit traces in `../data/runs/`.

## Tests

```bash
venv/bin/pytest           # 19 tests, ~8 s, no Ollama needed (scripted model, throw-away data folder)
```
They call the tools through a real MCP client: in memory, as a stdio subprocess, and over HTTP.

## Troubleshooting

| Problem | Fix |
|---|---|
| Tools fail | Call `system_health`: it reports Ollama, missing models, mail login, QC checks and OCR |
| Client times out on `bug_scan` or `docs_ask` | Models on a CPU are slow. The server sends progress every 10 s; raise the client's tool timeout if it ignores progress |
| `outside the folders this server may read` | Copy the file into `files/`, or add its folder to `allowed_doc_roots` in `mcp.yaml` |
| `No AgentBuild project at …` | Set `AGENTBUILD_HOME` (or `agentbuild_home` in `mcp.yaml`) to the AgentBuild folder |
