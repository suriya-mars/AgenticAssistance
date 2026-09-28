# AgentBuild application: CLI, agents, guardrails, OCR and QC tools. Talks to the `ollama` service.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 # QC runs git worktrees on mounted repos owned by the host user
 && git config --system safe.directory '*'

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# ruff + pytest are the example QC tools in config/qc_tools.yaml
RUN pip install --no-cache-dir . ruff pytest

COPY config ./config
COPY prompts ./prompts
COPY samples ./samples
# Sample PDFs are generated, not stored in git
RUN cd samples && python make_sample_pdf.py >/dev/null && python make_asoc_pdf.py >/dev/null
COPY tests ./tests

ENV AGENTBUILD_HOME=/app \
    AGENT_OLLAMA_URL=http://ollama:11434 \
    HOME=/tmp \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["agent"]
CMD ["--help"]
