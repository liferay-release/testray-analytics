#!/bin/bash
#
# triage_jenkins.sh — find newly broken builds and explain what broke them.
#
# Runs on a schedule — every 30 minutes on Jenkins. Each run looks for builds
# whose test failures nobody has accounted for yet, then analyses them: it reads
# the failures out of Testray, works out which commits could have caused them,
# writes the verdicts back so the team can see them in Testray, and leaves a
# Slack message summarising what it found.
#
# Nothing is analysed twice. A failure that already carries a verdict, and a
# build pair that has already been looked at, are both skipped — which is what
# keeps a routine that is red for days from being re-analysed every half hour.
#
# This is the whole Jenkins build step. The job binds three secrets, sets a few
# paths, and calls this; the ORDER of the two commands, the preflight checks and
# the lock live here rather than in a text field in a job config, so they can be
# reviewed, tested and fixed like the rest of the pipeline.
#
#   Build step:  ./scripts/triage_jenkins.sh
#   First run:   ./scripts/triage_jenkins.sh --no-classify   # free; proves the wiring
#   Preflight:   ./scripts/triage_jenkins.sh --check         # read-only, no spend
#
# Two producers feed one drainer (ARCHITECTURE "Two queues, one drainer"):
# `scan` registers work for the routines named in TRIAGE_SCAN_ROUTINES, and
# `Run Triage` in the Testray UI registers it from a click. `watch` drains
# whatever is there. Running only the drainer — which is what the ticket
# originally specified — drains an empty queue forever.
#
# Exit codes: 0 ok (including a tick skipped because another is still
# running — under a 30-minute trigger that is the healthy case, not a failure,
# and a job that notifies on failure must not be woken by it), 1 usage or
# preflight, 2 a step failed.

set -o pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(dirname -- "${SCRIPT_DIR}")"

CLASSIFY="true"
ENGINE="${TRIAGE_ENGINE:-api}"
CHECK_ONLY="false"
REQUIRE_OBJECTS="false"
PYTHON_BIN="${TRIAGE_PYTHON_BIN:-python3.13}"

# The instance is defaulted because this script exists to run against prod, and
# `testray_target()` prints where it actually pointed on every command — so a
# wrong value is visible in the console rather than inferred from a 401.
export TESTRAY_BASE_URL="${TESTRAY_BASE_URL:-https://testray.liferay.com}"
export TESTRAY_UI_URL="${TESTRAY_UI_URL:-https://testray.liferay.com/web/testray}"

# The credential guard in triage_pipeline.sh refuses env-supplied secrets,
# because a stale TESTRAY_CLIENT_ID in a shell silently redirects the read half
# of a run. On CI they are supplied deliberately, which is what this says.
export TRIAGE_ALLOW_ENV_CREDENTIALS=1

function print_help {
	cat <<-END
	Usage: $(basename "${0}") [options]

	  --no-classify      prepare only: no model calls, no spend. Use for the
	                     first run against a new instance.
	  --engine ENGINE    api (default) or claude-code. claude-code needs the
	                     CLI installed and logged in on the agent.
	  --check            run the preflight checks and exit. Read-only: it
	                     mints a token and reads three endpoints, spends
	                     nothing.
	  --require-objects  fail if the triage Objects are absent. Pass this once
	                     the analytics CX is deployed on the target instance,
	                     so a broken deploy is caught instead of silently
	                     degrading to marker files.
	  -h, --help         this.

	Required environment:
	  TESTRAY_CLIENT_ID / TESTRAY_CLIENT_SECRET   Jenkins secret-text bindings
	  ANTHROPIC_API_KEY                           only for --engine api
	  TRIAGE_REPO_PATH                            persistent liferay-portal checkout
	  TRIAGE_SCAN_ROUTINES                        e.g. 79529

	Optional environment:
	  TESTRAY_BASE_URL      default ${TESTRAY_BASE_URL}
	  TESTRAY_UI_URL        default ${TESTRAY_UI_URL}
	  TRIAGE_ROUTINE_REMOTES  e.g. 79529=origin — the remote NAME in the
	                        checkout. Without it every commit link falls back
	                        to liferay/liferay-portal, which does not carry
	                        Stable's commits.
	  TRIAGE_QUEUE          marker directory. Only used where the TriageRun
	                        Object is absent, and then it MUST outlive the
	                        workspace: it is the only record that a pair was
	                        already analysed.
	  TRIAGE_EXPECT_REMOTE  substring the checkout's remote URL must contain
	                        (default brianchandotcom)
	  TRIAGE_PYTHON_BIN     interpreter used to build the venv (default
	                        ${PYTHON_BIN})
	END
}

function log {
	echo "$(date '+%Y-%m-%d %H:%M:%S') ${*}"
}

function die {
	echo "$(date '+%Y-%m-%d %H:%M:%S') ! ${*}" >&2
	exit 1
}

while [ ${#} -gt 0 ]
do
	case "${1}" in
		--no-classify)
			CLASSIFY="false"
			shift
			;;
		--engine)
			ENGINE="${2}"
			shift 2
			;;
		--check)
			CHECK_ONLY="true"
			shift
			;;
		--require-objects)
			REQUIRE_OBJECTS="true"
			shift
			;;
		-h|--help)
			print_help
			exit 0
			;;
		*)
			echo "Unknown option: ${1}" >&2
			print_help
			exit 1
			;;
	esac
done

cd "${PROJECT_DIR}" || die "cannot cd to ${PROJECT_DIR}"

# --- preflight --------------------------------------------------------------
#
# Every check here is something that has failed, or would fail, silently: a
# missing secret reads as a broken instance, a wrong remote produces links to a
# repo that does not have the commits, and a missing API key surfaces minutes
# into a run rather than at the start.

for var in TESTRAY_CLIENT_ID TESTRAY_CLIENT_SECRET TRIAGE_REPO_PATH \
	TRIAGE_SCAN_ROUTINES
do
	[ -n "${!var}" ] || die "${var} is not set. See --help."
done

[ -d "${TRIAGE_REPO_PATH}" ] || die "TRIAGE_REPO_PATH=${TRIAGE_REPO_PATH} is not a directory. prepare needs a persistent liferay-portal checkout to diff against."

if [ "${CLASSIFY}" == "true" ] && [ "${ENGINE}" == "api" ] && [ -z "${ANTHROPIC_API_KEY}" ]
then
	# Named explicitly because the variable is easy to misspell, and the
	# claude-code engine scrubs it deliberately — so "it worked before" is not
	# evidence that it is set.
	die "ANTHROPIC_API_KEY is not set, and --engine api needs it. (Check the binding name: ANTHROPIC, not ANTROPIC.)"
fi

# A wrong remote is the quiet one: github_slug() falls back to
# liferay/liferay-portal, so every commit and compare link in the report and in
# the Slack message points at a repo where Stable's commits do not exist yet.
readonly EXPECT_REMOTE="${TRIAGE_EXPECT_REMOTE:-brianchandotcom}"
REMOTE_NAME="${TRIAGE_ROUTINE_REMOTES#*=}"
REMOTE_NAME="${REMOTE_NAME:-origin}"
REMOTE_URL="$(git -C "${TRIAGE_REPO_PATH}" remote get-url "${REMOTE_NAME}" 2>/dev/null)"

[ -n "${REMOTE_URL}" ] || die "no git remote '${REMOTE_NAME}' in ${TRIAGE_REPO_PATH}. TRIAGE_ROUTINE_REMOTES names a REMOTE, not a URL."

case "${REMOTE_URL}" in
	*"${EXPECT_REMOTE}"*) ;;
	*)
		die "remote '${REMOTE_NAME}' is ${REMOTE_URL}, which does not contain '${EXPECT_REMOTE}'. Stable's commits live on brianchandotcom; the wrong remote yields links to commits that are not there. Override with TRIAGE_EXPECT_REMOTE."
		;;
esac

# Editable install, so config/ and slack/ resolve inside this checkout rather
# than next to a copy in site-packages.
if [ ! -x .venv/bin/testray-analysis ]
then
	log "> building venv with ${PYTHON_BIN}"
	command -v "${PYTHON_BIN}" >/dev/null || die "${PYTHON_BIN} not found. Set TRIAGE_PYTHON_BIN."
	"${PYTHON_BIN}" -m venv .venv || die "venv creation failed"
	.venv/bin/pip install --quiet --upgrade pip || die "pip upgrade failed"
	.venv/bin/pip install --quiet -e . || die "pip install -e . failed"
fi

export TRIAGE_LOG_DIR="${TRIAGE_LOG_DIR:-${PROJECT_DIR}/logs}"
mkdir -p "${TRIAGE_LOG_DIR}" || die "cannot create ${TRIAGE_LOG_DIR}"

# Step 0 is the whole preamble: everything a reader needs to find this run's
# output before any of it exists. On a failure twenty minutes in, the console
# has already said where to look.
log "step 0: preflight ok"
log "  routines: ${TRIAGE_SCAN_ROUTINES}   engine: ${ENGINE}   classify: ${CLASSIFY}"
log "  portal:   ${TRIAGE_REPO_PATH}  (${REMOTE_NAME} -> ${REMOTE_URL})"
log "  logs:     ${TRIAGE_LOG_DIR}  (one file per pipeline step)"
log "  bundles:  ${PROJECT_DIR}/runs"
log "  slack:    ${PROJECT_DIR}/slack/testray_analyzer_slack_message.txt"

log "step 0: checking credentials, scopes and the triage Objects"

preflight_args=()
[ "${REQUIRE_OBJECTS}" == "true" ] && preflight_args+=(--require-objects)

.venv/bin/testray-analysis preflight "${preflight_args[@]}" \
	|| die "preflight failed — fix the above before running the pipeline. A 403 on an /o/c/ endpoint is a missing OAuth scope, NOT a missing deploy: the queue cannot tell them apart, so it would silently fall back to marker files and every write would be refused."

if [ "${CHECK_ONLY}" == "true" ]
then
	log "--check: nothing was run"
	exit 0
fi

# --- one tick, under a lock -------------------------------------------------
#
# The lock is here rather than left to the job's "do not allow concurrent
# builds" checkbox because the cost of getting it wrong is money: a classify
# run can outlast the 30-minute trigger, and nothing in the queue stops two
# drainers claiming the same work. A second tick exits 3 immediately, which
# reads as "skipped", not as a failure.
readonly LOCK_FILE="${TRIAGE_LOCK_FILE:-${TMPDIR:-/tmp}/triage_jenkins.lock}"

function tick {
	local rc=0

	# Producer. Cheap, and never spends: REST reads plus a git diff.
	log "step 1: scan"
	.venv/bin/testray-analysis scan --once || return 2
	log "step 1: scan ok"

	# Consumer. This is the step that costs money when --classify is on.
	local watch_args=(--once --engine "${ENGINE}")
	[ "${CLASSIFY}" == "true" ] && watch_args+=(--classify)

	log "step 2: watch ${watch_args[*]}"
	.venv/bin/testray-analysis watch "${watch_args[@]}" || rc=2
	[ "${rc}" -eq 0 ] && log "step 2: watch ok"

	return "${rc}"
}

if command -v flock >/dev/null
then
	exec 9>"${LOCK_FILE}" || die "cannot open lock ${LOCK_FILE}"
	if ! flock -n 9
	then
		# Success on purpose: the previous tick is still working, which is
		# exactly what the lock is for. Failing here would turn a normal
		# overlap into a red build and a Slack alert about nothing.
		log "skipped: another tick still holds ${LOCK_FILE}"
		exit 0
	fi
	tick
	RC=${?}
else
	log "WARNING: flock not available, running without a lock. Make sure the"
	log "         job forbids concurrent builds, or two ticks can pay twice"
	log "         for one analysis."
	tick
	RC=${?}
fi

if [ "${RC}" -ne 0 ]
then
	log "! failed (exit ${RC}) — the failing step's own log is in ${TRIAGE_LOG_DIR}"
	exit "${RC}"
fi

# The Slack message is written by submit on every run, including one that
# concluded nothing. Naming it here means the job's console says where the
# post-build step should read from.
readonly SLACK_FILE="${PROJECT_DIR}/slack/testray_analyzer_slack_message.txt"
[ -f "${SLACK_FILE}" ] || log "note: no Slack message was written — submit did not run"

log "done"
