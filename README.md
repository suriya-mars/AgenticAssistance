# AgentBuild — local multi-agent assistant (Ollama)

Mail triage, reply drafting, PDF question answering, the BUG quality/sign-off process, and pre-release quality checks. Everything runs locally: the LLMs run on Ollama, and the only outbound connection is read-only IMAP.

## Setup
```bash
sudo pacman -S ollama-cuda && sudo systemctl enable --now ollama
ollama pull qwen3:4b && ollama pull qwen3.5:9b   # model roles set in config/settings.yaml
./venv/bin/pip install -e '.[dev]'
./venv/bin/agent doctor
```
For real mail, set `mail.source: imap` and the `mail.imap.*` settings in `config/settings.yaml`, then `export AGENT_IMAP_PASSWORD=<app password>`.

## Docker (runs anywhere, models included)
```bash
scripts/build.sh 1.0                       # builds agentbuild-app + agentbuild-ollama (qwen3:4b + qwen3.5:9b baked in)
scripts/export.sh 1.0 /path/with/15GB      # folder with images.tar, compose files, config, ./agent, ./install.sh
# on the target machine (any OS with Docker, x86-64):
./install.sh && ./agent doctor             # then ./agent <any command below>
```
### On a new machine, from GitHub (e.g. a work laptop)
Needs only Docker (Compose 2.24+), git and internet; no Python or Ollama:
```bash
git clone <this repo> && cd AgentBuild
scripts/build.sh 1.0          # downloads the models during the build (~9 GB, one time), builds both images
cp .env.example .env
./agent-docker doctor         # then ./agent-docker <any command>, e.g. ./agent-docker pdf chat files/x.pdf
```
Put PDFs in `files/` and repositories in `repos/`. Your data stays in `data/`, which is not in git.
On Windows, run these in a WSL2 terminal with Docker Desktop.

Here, `./agent-docker <command>` runs the same containers against this repo's `config/` and `data/`.
The GPU is used automatically when Docker has the NVIDIA runtime; otherwise the CPU is used. Details: `docker/bundle/README.md`.

## Commands
```
agent mail triage [--limit 10]        # summarise, prioritise, extract tasks; offers QC for release mails
agent mail draft <uid> --say "..."    # draft saved to data/drafts/*.eml, never sent
agent tasks                           # tasks pulled from mail
agent qc run <repo> [--ref main]      # QC pipeline -> GO / NO_GO / NEEDS_REVIEW (exit code 0 only on GO)
agent qc tools                        # configured QC plug-ins
agent pdf add <file.pdf>              # extract + index (OCR for scanned pages)
agent pdf ask <pdf|name> "question"   # answer with page citations, each quote verified
agent pdf chat <pdf|name>             # interactive Q&A with follow-ups
agent pdf list | agent pdf remove <doc>
agent runs list | agent runs show <id> # audit traces (data/runs/*.jsonl)
agent ask "run QC on sample_repo"     # free text -> supervisor routes it
```

## BUG quality process
One isolated context per bug id (`BUG-12345`, or just `12345`), run in **rounds**:
```
agent bug new 12345 -t "Payments API order lookup fix" [--tester a@x.com --pm b@x.com --keyleader c@x.com]
agent bug scan BUG-12345 report.pdf     # 1. ASoC scan -> counts, issues, gate. FAIL => BLOCKED
agent bug draft BUG-12345 security      # 2. security team mail (ASoC PDF attached)
agent bug sync BUG-12345                # 3. security team must APPROVE (even on a PASS). Rejection => BLOCKED
agent bug draft BUG-12345 tester        # 4. tester mail
agent bug sync BUG-12345                # 5+6. tester approval / sign-off replies, you confirm y/N
agent bug draft BUG-12345 signoff       # 7. PM + key leader sign-off request mails
agent bug sync BUG-12345                # 8+9. PM / key leader sign-offs -> COMPLETED

# when BLOCKED (ASoC gate failed, or security team rejected): fix it, then start again with the same id
agent bug restart BUG-12345 --fix "disabled TLS 1.0/1.1"   # new round; history kept
agent bug scan BUG-12345 new-report.pdf                    # ...and continue from step 1

agent bug status BUG-12345 | agent bug list | agent bug ask BUG-12345 "why was it blocked?" | agent bug chat BUG-12345
agent bug mark BUG-12345 security_approval --note "approved in meeting"   # manual override; `bug reopen` to undo
```
- **Gate** (`bug.security_gate`): a severity count above its limit fails the scan. The default fails on any critical or high.
  Set every limit to `0` to fail on any finding at all.
- **Order is enforced**: e.g. no tester mail before the security team approves. A reply that arrives early is deferred.
- **Rounds**: after `restart`, replies dated before the restart never count, so an old approval can't complete a new round.
  `{resubmission_note}` and `{fix_note}` put the fix into the next mails.
- **Same thread**: security re-submissions are sent as a reply on top of the existing security thread (`Re:` subject,
  `In-Reply-To`/`References`, their last mail quoted). This also happens for any role that asks for it in a reply.
  Override with `--thread` / `--new-thread`.
- **Mail formats** live in `config/bug_templates/*.md`. **Drafts** go to `data/drafts/bugs/` or your IMAP Drafts folder.
  Nothing is ever sent by the agent.
- **Only listed senders count**: `bug.recipients` in settings, or per bug with `--tester/--pm/--keyleader/--security`.
- **ASoC numbers are checked by code**: counts, header and the Issue Types table are parsed from the report text. The model's
  reading is only a fallback, and any mismatch is shown as a `check:` warning.

## PDF Q&A
- Text comes from PyMuPDF. Pages with no text layer are OCR'd with local Tesseract; install your language
  packs (`sudo pacman -S tesseract-data-eng`) and set `docs.ocr_lang` (e.g. `eng+tam`).
- PDFs under `docs.context_chars` (12k chars) are sent whole; bigger ones get the top BM25 passages
  (page-bounded chunks, so each passage has one page number). Follow-up questions reuse the previous question's terms.
- The model must cite `{page, exact quote}`. Code then checks each quote against the real page text:
  **verified**, **page corrected**, **close match** (all numbers must match), or **NOT FOUND** (flagged to you).
- The PDF text is untrusted: it is sanitised, wrapped as `<untrusted_document>`, and the PDF agent has no tools.
- Sample: `python samples/make_sample_pdf.py && agent pdf chat samples/docs/release_policy.pdf`
  (PyMuPDF is AGPL-licensed, which is fine for personal/internal use.)

## Plugging in your organisation's QC tools
- **CLI tools**: add an entry to `config/qc_tools.yaml` (argv, severity, required, parser). No code changes are needed.
- **Custom logic** (APIs, SonarQube, internal services): implement the `QCTool` protocol in `src/agentbuild/tools/qc/base.py`, then add it to the list returned by `load_qc_tools`.

Each tool becomes an LLM tool named `qc_<name>` with risk level EXEC. The **verdict comes from rules**: a failing or skipped blocker gives NO_GO, and a failing major check gives NEEDS_REVIEW. The LLM only writes the analysis.

## Guardrails at a glance
| Layer | Where |
|---|---|
| Email is sanitised, flagged for injection and wrapped as `<untrusted_email>`; the mail agents have **no tools** | `guardrails/input.py`, `config/agents.yaml` |
| Tool allowlist per agent; READ/WRITE/EXEC risk tiers; EXEC hard-blocked once the context is tainted | `guardrails/policy.py` |
| Mail → QC needs the sender on the allowlist **and** human approval; the QC agent gets a clean context | `core/supervisor.py`, `config/policies.yaml` |
| No shell: argv templates, cwd limited to allowed repo roots, scrubbed env, timeouts, temp git worktree for refs | `guardrails/sandbox.py`, `tools/qc/` |
| Pydantic/JSON-schema outputs with retry; secret redaction; step/tool/time budgets; JSONL audit | `core/agent.py`, `guardrails/output.py` |

## Tests
`./venv/bin/pytest`: 52 tests using a scripted FakeLLM. Ollama is not needed. The same tests run inside the Docker image:
`docker run --rm --network none --entrypoint python agentbuild-app:1.0 -m pytest -q -p no:cacheprovider /app/tests`
