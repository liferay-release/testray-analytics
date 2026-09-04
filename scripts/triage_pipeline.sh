#!/bin/bash
#
# triage_pipeline.sh — prepare, classify and submit one triage run in one shot.
#
# This is the single definition of the pipeline sequence. Jenkins calls it,
# `testray-analysis watch` calls it, and a human can call it by hand; none of
# them re-implement the order of steps or where the logs land.
#
# Modelled on liferay-docker's release scripts (`lc_time_run`): every step's
# output goes to its own numbered .txt file and the console gets one line per
# step with its timing, so a failure names both the step and the file to read.
# That is what makes it legible in a Jenkins console, where interleaved output
# from three long-running Python processes is not.
#
#   ./scripts/triage_pipeline.sh --baseline-build-id 377678 --target-build-id 377676
#   ./scripts/triage_pipeline.sh -b 377678 -t 377676 --no-classify
#
# Exit codes: 0 ok, 1 usage, 2 a step failed (its log path is printed).

set -o pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(dirname -- "${SCRIPT_DIR}")"

# The venv python, not whatever `python3` resolves to: pandas and pyyaml live
# there, and a bare python3 fails several steps in with an ImportError rather
# than up front.
PYTHON="${TRIAGE_PYTHON:-${PROJECT_DIR}/.venv/bin/python}"

TRIAGE_LOG_DIR="${TRIAGE_LOG_DIR:-${PROJECT_DIR}/logs}"
START_TIME="$(date +%Y%m%dT%H%M%SZ)"
STEP=0

BASELINE_BUILD_ID=""
TARGET_BUILD_ID=""
CLASSIFY="true"
SUBMIT_DRY_RUN="false"
SUBMIT_NO_WRITE="false"
ENGINE="claude-code"
MODE="by-cluster"
OUT_DIR="${PROJECT_DIR}/runs"
EXTRA_ARGS=()

function print_help {
	cat <<-END
	Usage: $(basename "${0}") -b <baseline build id> -t <target build id> [options]

	  -b, --baseline-build-id ID   baseline build (required)
	  -t, --target-build-id ID     target build (required)
	      --no-classify            stop after prepare; print what would finish it
	      --dry-run                submit builds the batch file but upserts nothing
	      --no-write               submit validates and renders, builds no batch
	      --engine ENGINE          classify engine: claude-code (default) or api
	      --mode MODE              by-cluster (default), per-test, by-subtask
	      --out DIR                where bundles are written (default ./runs)
	      --                       remaining args are passed through to prepare

	Environment:
	  TRIAGE_LOG_DIR   where per-step logs go (default ./logs)
	  TRIAGE_PYTHON    interpreter to use (default ./.venv/bin/python)
	END
}

function log {
	echo "$(date '+%Y-%m-%d %H:%M:%S') ${*}"
}

# Run one pipeline step into its own log file, echoing a single console line.
# Returns the step's exit code so the caller decides whether to continue.
function run_step {
	local name="${1}"
	shift

	STEP=$((STEP + 1))

	local log_file="${TRIAGE_LOG_DIR}/log_${START_TIME}_step_${STEP}_${name}.txt"
	local start=$(date +%s)

	log "> ${name}"

	"${@}" > "${log_file}" 2>&1
	local exit_code=${?}

	local seconds=$(( $(date +%s) - start ))

	if [ "${exit_code}" -ne 0 ]
	then
		log "! ${name} failed after ${seconds}s (exit ${exit_code})"
		echo
		# The tail is almost always the actual cause, and a Jenkins operator
		# should not have to go fetch a file to see it.
		echo "--- last 20 lines of ${log_file} ---"
		tail -20 "${log_file}"
		echo "--- full log: ${log_file} ---"
		return "${exit_code}"
	fi

	log "< ${name} ok in ${seconds}s  (${log_file})"

	return 0
}

# Kept so the credential guard below can print a command that actually
# reproduces this invocation — by the time it runs, parsing has consumed "$@".
readonly ORIGINAL_ARGS=("${@}")

while [ ${#} -gt 0 ]
do
	case "${1}" in
		-b|--baseline-build-id)
			BASELINE_BUILD_ID="${2}"
			shift 2
			;;
		-t|--target-build-id)
			TARGET_BUILD_ID="${2}"
			shift 2
			;;
		--no-classify)
			CLASSIFY="false"
			shift
			;;
		--dry-run)
			SUBMIT_DRY_RUN="true"
			shift
			;;
		--no-write)
			SUBMIT_NO_WRITE="true"
			shift
			;;
		--engine)
			ENGINE="${2}"
			shift 2
			;;
		--mode)
			MODE="${2}"
			shift 2
			;;
		--out)
			OUT_DIR="${2}"
			shift 2
			;;
		-h|--help)
			print_help
			exit 0
			;;
		--)
			shift
			EXTRA_ARGS=("${@}")
			break
			;;
		*)
			echo "Unknown option: ${1}" >&2
			print_help
			exit 1
			;;
	esac
done

if [ -z "${BASELINE_BUILD_ID}" ] || [ -z "${TARGET_BUILD_ID}" ]
then
	echo "Both --baseline-build-id and --target-build-id are required." >&2
	print_help
	exit 1
fi

if [ ! -x "${PYTHON}" ]
then
	echo "No interpreter at ${PYTHON}. Set TRIAGE_PYTHON." >&2
	exit 1
fi

# TESTRAY_CLIENT_ID / _SECRET silently override config.yml. When they hold prod
# values — which is how they are usually set — every call 401s against
# localhost, and the failure looks like a broken local instance rather than a
# shell variable. This has cost time twice, so refuse rather than warn.
if [ -n "${TESTRAY_CLIENT_ID}" ] || [ -n "${TESTRAY_CLIENT_SECRET}" ]
then
	cat <<-END >&2
	TESTRAY_CLIENT_ID / TESTRAY_CLIENT_SECRET are set in this shell and override
	config.yml. If they hold prod values every request will 401 against
	${TRIAGE_TARGET_HINT:-the configured instance}.

	Re-run without them:
	  env -u TESTRAY_CLIENT_ID -u TESTRAY_CLIENT_SECRET ${0} ${ORIGINAL_ARGS[*]}

	Set TRIAGE_ALLOW_ENV_CREDENTIALS=1 to proceed anyway (e.g. on Jenkins, where
	they are set deliberately).
	END

	[ -z "${TRIAGE_ALLOW_ENV_CREDENTIALS}" ] && exit 1
fi

mkdir -p "${TRIAGE_LOG_DIR}"

log "triage pipeline ${BASELINE_BUILD_ID} -> ${TARGET_BUILD_ID}"
log "  mode=${MODE} classify=${CLASSIFY} engine=${ENGINE}"
log "  logs=${TRIAGE_LOG_DIR}"

run_step "prepare" \
	"${PYTHON}" -m testray_analytics.analysis.prepare \
	--baseline-build-id "${BASELINE_BUILD_ID}" \
	--target-build-id "${TARGET_BUILD_ID}" \
	--mode "${MODE}" \
	--out "${OUT_DIR}" \
	"${EXTRA_ARGS[@]}" || exit 2

# prepare prints this line when the bundle is complete; it is the handoff to
# every later step, so it is re-emitted on stdout as a parseable contract for
# callers (the queue runner reads it to know what it just built).
PREPARE_LOG="${TRIAGE_LOG_DIR}/log_${START_TIME}_step_1_prepare.txt"
BUNDLE="$(sed -n 's|^Run bundle ready:[[:space:]]*\(.*\)$|\1|p' "${PREPARE_LOG}" | tail -1)"

if [ -z "${BUNDLE}" ]
then
	log "! prepare succeeded but reported no bundle path — see ${PREPARE_LOG}"
	exit 2
fi

# Relative in prepare's output; callers need something they can use from anywhere.
case "${BUNDLE}" in
	/*) ;;
	*) BUNDLE="${PROJECT_DIR}/${BUNDLE}" ;;
esac

echo "BUNDLE=${BUNDLE}"

if [ "${CLASSIFY}" != "true" ]
then
	log "stopping before classify (--no-classify). To finish:"
	echo "  testray-analysis classify ${BUNDLE}"
	echo "  testray-analysis submit   ${BUNDLE}"
	exit 0
fi

# A dry run first: it makes no model calls, takes seconds, and its log records
# the batch plan and token estimate for the run that is about to happen. That is
# the standing rule for anything that spends model usage — never fire blind.
run_step "classify_dry_run" \
	"${PYTHON}" -m testray_analytics.analysis.classify \
	"${BUNDLE}" --engine "${ENGINE}" --dry-run || exit 2

# Nothing classifiable means nothing to ask. Skipping is not an optimisation
# detail: a run with zero classifiable clusters used to send its auto-only
# sections anyway, the model correctly answered with nothing, and it cost real
# usage to be told so. Reading the count from the bundle rather than from
# prepare's stdout keeps this working when a bundle is re-run by hand.
CLASSIFIABLE="$("${PYTHON}" - "${BUNDLE}" <<-'PYEOF'
	import csv, pathlib, sys
	p = pathlib.Path(sys.argv[1]) / "diff_list_subtasks.csv"
	n = 0
	if p.exists():
	    with p.open(newline="") as fh:
	        n = sum(1 for r in csv.DictReader(fh)
	                if (r.get("bucket") or "").strip() == "classifiable")
	print(n)
	PYEOF
)"

if [ "${CLASSIFIABLE:-0}" -eq 0 ]
then
	log "! no classifiable clusters — skipping classify (nothing to ask)"
	log "  every failure in this pair was auto-classified or excluded upstream."
	log "  submit will still run so the report and coverage are written."
	SKIPPED_CLASSIFY=true
else
	run_step "classify" \
		"${PYTHON}" -m testray_analytics.analysis.classify \
		"${BUNDLE}" --engine "${ENGINE}" || exit 2
fi

SUBMIT_ARGS=()
[ "${SUBMIT_DRY_RUN}" == "true" ] && SUBMIT_ARGS+=("--dry-run")
[ "${SUBMIT_NO_WRITE}" == "true" ] && SUBMIT_ARGS+=("--no-write")

run_step "submit" \
	"${PYTHON}" -m testray_analytics.analysis.submit "${BUNDLE}" \
	"${SUBMIT_ARGS[@]}" || exit 2

# Exit 3, distinct from both success and a step failure: the pipeline behaved
# correctly but explained nothing about a build that is red. Under unattended
# operation that has to be visible — silence is indistinguishable from success,
# which is exactly how a real defect (PortalLogAssertorTest, build 512102) went
# out under a green diamond. The queue runner turns this into its own status.
if [ "${SKIPPED_CLASSIFY:-false}" == "true" ]
then
	log "pipeline complete WITHOUT VERDICTS: ${BUNDLE}"
	exit 3
fi

log "pipeline complete: ${BUNDLE}"
