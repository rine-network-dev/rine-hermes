# Contained rig for the Hermes Agent plugin (hermes-rine).
#
# Nous Research Hermes Agent + its dependency tree install INSIDE the container ONLY
# (into a venv on a named volume; see ../compose.hermes.yml). NOTHING Hermes-related is
# ever installed on the host — same hard rule as compose.openclaw.yml / compose.crewai.yml.
#
# Hermes itself is cloned at BUILD time to /opt/hermes (pinned tag, reproducible) and
# installed EDITABLE into the named-volume venv on first run (entrypoint), alongside the
# bind-mounted ./rine-sdk and ./rine-hermes[dev] — so the rig consumes the UNRELEASED SDK
# fixes directly and the plugin under test is live-editable.
#
# Typical use (from repo root):
#   docker compose -f compose.hermes.yml build
#   docker compose -f compose.hermes.yml run --rm hermes hermes --version
#   docker compose -f compose.hermes.yml run --rm hermes pytest -q
#   docker compose -f compose.hermes.yml run --rm hermes ruff check src tests
#   docker compose -f compose.hermes.yml run --rm hermes python e2e/e2e_hermes.py
#   # reset the venv: docker volume rm rine-hermes_hermes_venv
FROM ubuntu:24.04

# Headless install prerequisites for a bare ubuntu:24.04 (Hermes' setup-hermes.sh is
# interactive — we replicate its non-interactive core instead). ripgrep = Hermes' fast
# search backend; git for the clone + any VCS deps; build-essential for native wheels.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git python3 python3-venv python3-dev build-essential ripgrep jq \
 && rm -rf /var/lib/apt/lists/*

# uv: fast resolver + Python toolchain manager (installs the pinned 3.11 interpreter).
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"
RUN uv python install 3.11

# Hermes source, pinned to an exact commit (the repo tags by CalVer — this commit is the
# tag v2026.9.7, pyproject version 0.21.1). Fetch-by-SHA (GitHub allows it) keeps the image
# reproducible and identical to the reference source the build read; the tag is annotated, so
# `git rev-parse v2026.9.7` yields the TAG object, not this commit — always pin the commit.
# Editable install into the named-volume venv happens in the entrypoint (first run).
#
# 🔴 The rig pin and the advertised floor are DIFFERENT NUMBERS, deliberately. `pyproject.toml`
# floors users at v2026.8.31 / 0.21.0 — the oldest release whose behaviour we make claims about —
# while this rig tests against the newest, so upstream drift surfaces here before it reaches a
# user. Raising the pin does not raise the floor; that is a separate, stated decision.
# Previous pin: 29112bef099274229cadff79cdff7bf7b99c4b77 (v2026.8.31, 0.21.0 — the floor).
ARG HERMES_REF=2237be355906fbe6065ce1815711eee52b2d646e
RUN git init /opt/hermes \
 && git -C /opt/hermes remote add origin https://github.com/NousResearch/hermes-agent \
 && git -C /opt/hermes fetch --depth 1 origin "${HERMES_REF}" \
 && git -C /opt/hermes checkout --detach FETCH_HEAD

ENV VENV=/opt/venv \
    PATH="/opt/venv/bin:/root/.local/bin:${PATH}" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HERMES_HOME=/root/.hermes \
    CI=true

WORKDIR /work/rine-hermes

COPY entrypoint.sh /usr/local/bin/rig-entrypoint
RUN chmod +x /usr/local/bin/rig-entrypoint

ENTRYPOINT ["rig-entrypoint"]
CMD ["python", "-c", "import hermes_rine, rine; print('hermes_rine', hermes_rine.__version__, '/ rine', rine.__version__)"]
