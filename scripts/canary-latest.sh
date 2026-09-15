#!/usr/bin/env bash
# canary-latest.sh — ADVISORY floating-version canary for the Hermes plugin (hermes-rine).
#
# WHY THIS EXISTS. `compose.hermes.yml`'s pinned lane clones Hermes at one exact commit, and
# every assertion the suite makes about the host — the adapter ABC we subclass, the
# post-delivery callback the whole exactly-once guarantee rests on, `build_session_key`'s
# `profile` keyword, `PlatformEntry`'s field set — is an assertion against that FROZEN
# commit, which by construction cannot catch drift away from it. `README.md` floors users at
# an OLDER release still (see the Dockerfile header: the pin and the floor are deliberately
# different numbers), so the window between "what we test" and "what a user
# runs" is real and only ever grows. This lane closes it from the other side: it builds the
# same rig against whatever Hermes ships on `main` today and re-runs the plugin's suite,
# `tests/test_upstream_contract.py` included, against that.
#
# ADVISORY, NOT BLOCKING. A red canary is upstream news, not our regression: it means
# published Hermes changed under a plugin whose source did not. It is deliberately NOT in
# `scripts/ci.sh`, the publish gate, or the pre-push hook. Run it on demand, and when it
# fails read the upstream commit it names before touching plugin code.
#
# ALL Hermes execution happens inside the contained rig (compose.hermes.yml +
# compose.hermes.canary.yml), on its OWN image tag and its OWN venv volume so the pinned
# lane keeps testing the pinned commit. Nothing Hermes-related is installed on the host.
#
# Run:  bash rine-hermes/scripts/canary-latest.sh              # hermes-agent main
#       bash rine-hermes/scripts/canary-latest.sh v2026.9.7    # any ref: branch, tag or SHA
#       CANARY_FRESH=false bash rine-hermes/scripts/canary-latest.sh   # reuse the last venv
#
# 🔴 CANARY_FRESH defaults to TRUE and should stay that way. Hermes is installed EDITABLE
# from /opt/hermes, so a reused venv follows the new source but keeps the OLD dependency
# tree — a new upstream dependency then surfaces as an ImportError attributed to drift in
# our bindings. Reuse only when re-running the same ref twice.
#
# The rig runs as root and mounts this tree read-write, so run ../scripts/janitor-cred-hygiene.sh
# after a run, as with every other lane. It shares no volume with the pinned rig, but it is
# still a rig: serialise it against the other compose.hermes.yml lanes.
#
# Exit: 0 = plugin survives that ref · 1 = drift detected (advisory) · 2 = rig/setup error.

set -uo pipefail

REF="${1:-main}"
CANARY_FRESH="${CANARY_FRESH:-true}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$PKG_DIR/.." && pwd)"
CANARY_VOLUME="rine-hermes_hermes_canary_venv"

# The pinned lane's commit, read from the Dockerfile rather than restated here — when the
# two are equal a green canary is a tautology, and the report has to be able to say so.
PINNED=$(sed -n 's/^ARG HERMES_REF=//p' "$PKG_DIR/Dockerfile" | head -1)
# The advertised floor is stated in README.md and NOWHERE machine-readable: `pyproject.toml`
# declares no hermes-agent dependency at all, because the host supplies it on PYTHONPATH.
FLOOR=$(sed -n 's/.*Hermes Agent \*\*\(.*\)\*\*.*/\1/p' "$PKG_DIR/README.md" | head -1)

FAILURES=()
ok()    { echo "  PASS: $1"; }
drift() { echo "  DRIFT: $1"; [[ -n "${2:-}" ]] && echo "         $2"; FAILURES+=("$1"); }

# `env_file: .env.e2e` is declared by the base compose file, so its absence is a hard
# Compose error long before any test runs — name it here instead of leaking that as a stack.
if [[ ! -f "$PROJECT_ROOT/.env.e2e" ]]; then
    echo "FATAL: $PROJECT_ROOT/.env.e2e missing — compose.hermes.yml declares it as env_file."
    echo "       It holds the shared rig credentials: gitignored, chmod 600, written by hand."
    exit 2
fi
if ! compgen -G "$PROJECT_ROOT/rine-mls/dist-wheels/*.whl" >/dev/null; then
    echo "FATAL: rine-mls/dist-wheels is empty — the rig installs rine-mls from there, never"
    echo "       from source. Populate it first:  ./scripts/build-mls-artifacts.sh --py"
    exit 2
fi

COMPOSE=(docker compose -f "$PROJECT_ROOT/compose.hermes.yml"
         -f "$PROJECT_ROOT/compose.hermes.canary.yml")
export CANARY_HERMES_REF="$REF"

echo "============================================"
echo "  Hermes floating-version CANARY (advisory) — $(date -Iseconds)"
echo "  requested ref  : $REF"
echo "  rig pins       : $PINNED   (what every other lane tests)"
echo "  users may run  : Hermes Agent ${FLOOR:-<unparsed>} or later"
echo "============================================"

# ---------------------------------------------------------------------------
echo ""; echo "=== 1 · build the rig against hermes-agent@$REF ==="
# ---------------------------------------------------------------------------
if [[ "$CANARY_FRESH" == "true" ]]; then
    # Drop the venv so the entrypoint reinstalls the whole dependency tree against this
    # ref. See the CANARY_FRESH note in the header — this is the default for a reason.
    docker volume rm "$CANARY_VOLUME" >/dev/null 2>&1 && echo "  (dropped stale canary venv)"
fi
if ! BUILD=$("${COMPOSE[@]}" build --pull hermes 2>&1); then
    echo "FATAL: could not build the canary image for ref '$REF'"
    tail -20 <<<"$BUILD"
    echo ""
    echo "  A ref that does not exist upstream fails here, at 'git fetch --depth 1'."
    exit 2
fi
ok "built rine-hermes-canary:latest from hermes-agent@$REF"

# ---------------------------------------------------------------------------
echo ""; echo "=== 2 · identify what that actually resolved to ==="
# ---------------------------------------------------------------------------
# A branch name is a moving target: the report has to name the commit, or a red run cannot
# be reproduced and a green one cannot be trusted a week later.
RESOLVED=$("${COMPOSE[@]}" run --rm --entrypoint git hermes -C /opt/hermes rev-parse HEAD 2>/dev/null |
           tr -d '\r' | grep -oE '^[0-9a-f]{40}$' | head -1)
if [[ -z "$RESOLVED" ]]; then
    echo "FATAL: the built image has no Hermes checkout at /opt/hermes"
    exit 2
fi
ok "hermes-agent@$REF → $RESOLVED"
if [[ "$RESOLVED" == "$PINNED" ]]; then
    echo "  NOTE: identical to the pinned commit — this run is a tautology and proves"
    echo "        nothing new. It earns its keep on the first upstream commit after the pin."
fi

# ---------------------------------------------------------------------------
echo ""; echo "=== 3 · the plugin's own suite against that Hermes ==="
# ---------------------------------------------------------------------------
# The whole suite, not a subset: the upstream seams are spread across it (the contract
# canary, the session-key tests that call the real build_session_key, the register tests
# that read PlatformEntry's real field set, and every SendResult/MessageEvent assertion).
# First run also bootstraps the venv, which is the slow part (2-6 min).
SUITE=$("${COMPOSE[@]}" run --rm hermes pytest --tb=short -q 2>&1)
SUITE_RC=$?
SUMMARY=$(grep -E '^[0-9]+ (passed|failed)|[0-9]+ (passed|failed|error)' <<<"$SUITE" | tail -1)
if [[ $SUITE_RC -eq 0 ]]; then
    ok "suite green against $RESOLVED — ${SUMMARY:-no summary line}"
else
    drift "the plugin suite fails against hermes-agent@$RESOLVED" "${SUMMARY:-no summary line}"
    echo ""
    echo "  --- failing tests (contract canary first) ---"
    # One vanished export fails every test in the module that imports it, so a plain
    # head -25 of the failure list is 25 lines of collateral with the diagnosis cut off the
    # end. test_upstream_contract.py is the test that names the BINDING rather than a
    # symptom, so it is printed first and never truncated away.
    FAILED_LINES=$(grep -E '^(FAILED|ERROR) tests/' <<<"$SUITE")
    grep    -- 'test_upstream_contract' <<<"$FAILED_LINES" | sed 's/^/    /'
    grep -v -- 'test_upstream_contract' <<<"$FAILED_LINES" | head -15 | sed 's/^/    /'
    TOTAL_FAILED=$(grep -c . <<<"$FAILED_LINES")
    echo "    ($TOTAL_FAILED failing in total)"
    echo ""
    echo "  tests/test_upstream_contract.py names the binding when the break is a signature"
    echo "  or a vanished export; anything else is a behaviour change to read upstream first."
fi

# ---------------------------------------------------------------------------
echo ""
echo "============================================"
if [[ ${#FAILURES[@]} -eq 0 ]]; then
    echo "  CANARY GREEN — hermes-rine survives hermes-agent@$REF ($RESOLVED)."
    echo "  Rig pin unchanged at $PINNED; raising it is a separate, stated decision."
    echo "============================================"
    exit 0
fi
echo "  CANARY RED (ADVISORY) — hermes-agent@$REF resolved to $RESOLVED"
echo ""
echo "  Upstream Hermes changed under an unchanged plugin. This is NOT a build break: the"
echo "  pinned lane ($PINNED) is unaffected and still gates every release. Triage before"
echo "  changing plugin code — users on Hermes Agent ${FLOOR:-?} or later see this, we do not yet."
echo ""
for f in "${FAILURES[@]}"; do echo "    - $f"; done
echo "============================================"
exit 1
