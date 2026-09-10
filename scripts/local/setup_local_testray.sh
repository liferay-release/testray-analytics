#!/bin/bash
#
# setup_local_testray.sh — stand up a local Testray with real data and the
# triage screens, in one command.
#
# It runs the pieces that were already scripted (setupTestray.sh,
# loadTestrayData.py, deployCx.sh) in the right order, checks the things that
# otherwise fail twenty minutes in, and stops at the one step nobody can
# automate: creating the OAuth application. Run it again afterwards and it
# picks up where it left off.
#
#   ./scripts/local/setup_local_testray.sh            # start; stops at the OAuth step
#   ./scripts/local/setup_local_testray.sh            # run again after doing it
#   ./scripts/local/setup_local_testray.sh --check    # prerequisites only, changes nothing
#
# Every step is skipped when it is already done, so re-running is cheap and
# safe. Nothing here spends money — the pipeline is not run, only installed.
#
# Exit codes: 0 ok (including "stopped for the OAuth step"), 1 a prerequisite
# or a step failed.

set -o pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

# The three repos sit side by side by default; each is overridable, and nothing
# is hardcoded to a home directory.
WORKSPACE_DIR="${TESTRAY_WORKSPACE_DIR:-$(dirname -- "${REPO_ROOT}")}"
PORTAL_DIR="${TESTRAY_PORTAL_DIR:-${WORKSPACE_DIR}/liferay-portal}"
TESTRAY2_DIR="${TESTRAY2_DIR:-${WORKSPACE_DIR}/testray2}"
PORTAL_BRANCH="${TESTRAY_EXPECT_BRANCH:-master-testray}"
TESTRAY_URL="${TESTRAY_URL:-http://localhost:8080}"
# release-master runs 3.13, but a laptop may not have it and the tool only
# needs 3.11. Take the newest available rather than demanding one version.
PYTHON_BIN="${TRIAGE_PYTHON_BIN:-}"

CHECK_ONLY="false"
FRESH="false"
SKIP_DATA="false"
SKIP_CX="false"

function log  { echo "$(date '+%H:%M:%S') ${*}"; }
function step { echo; echo "$(date '+%H:%M:%S') ── ${*}"; }
function die  { echo; echo "!! ${*}" >&2; exit 1; }

function print_help {
	cat <<-END
	Usage: $(basename "${0}") [options]

	  --fresh       destroy the database first. Wipes the OAuth application and
	                every loaded row — only for starting over.
	  --skip-data   do not copy builds down from prod.
	  --skip-cx     do not rebuild the triage client extension.
	  --check       run the prerequisite checks and exit.
	  -h, --help    this.

	Directories (all overridable, all defaulted from this script's location):
	  TESTRAY_WORKSPACE_DIR   ${WORKSPACE_DIR}
	  TESTRAY_PORTAL_DIR      ${PORTAL_DIR}
	  TESTRAY2_DIR            ${TESTRAY2_DIR}
	  TESTRAY_EXPECT_BRANCH   ${PORTAL_BRANCH}
	  TRIAGE_PYTHON_BIN       ${PYTHON_BIN:-auto: newest of python3.13/3.12/3.11}
	END
}

while [ ${#} -gt 0 ]; do
	case "${1}" in
		--fresh)     FRESH="true"; shift ;;
		--skip-data) SKIP_DATA="true"; shift ;;
		--skip-cx)   SKIP_CX="true"; shift ;;
		--check)     CHECK_ONLY="true"; shift ;;
		-h|--help)   print_help; exit 0 ;;
		*) echo "Unknown option: ${1}" >&2; print_help; exit 1 ;;
	esac
done

cd "${REPO_ROOT}" || die "cannot cd to ${REPO_ROOT}"

# --- step 0: the things that fail late if you do not check them early -------

step "step 0: prerequisites"

command -v docker >/dev/null || die "docker is not installed."
docker info >/dev/null 2>&1 || die "docker is installed but not running. Start it and re-run."

if [ -z "${PYTHON_BIN}" ]; then
	for candidate in python3.13 python3.12 python3.11 python3; do
		command -v "${candidate}" >/dev/null || continue
		if "${candidate}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
			PYTHON_BIN="${candidate}"
			break
		fi
	done
fi

[ -n "${PYTHON_BIN}" ] || die "no python 3.11+ found (looked for python3.13, python3.12, python3.11, python3).
   Install one, or point at it: TRIAGE_PYTHON_BIN=/path/to/python $(basename "${0}")"

for d in "${PORTAL_DIR}" "${TESTRAY2_DIR}"; do
	[ -d "${d}" ] || die "missing ${d}
   Expected the repos side by side. Clone what is missing:
       git clone git@github.com:liferay-release/liferay-portal.git ${PORTAL_DIR}
       git clone https://github.com/dxpcloud/testray2 ${TESTRAY2_DIR}
   Or point at them: TESTRAY_PORTAL_DIR=… TESTRAY2_DIR=… $(basename "${0}")"
done

# The build patches three Testray source files and restores them afterwards. It
# refuses to touch a file you have already modified — and then the patch does
# not apply and the failure surfaces later, as a 500 on a page.
DIRTY="$(git -C "${PORTAL_DIR}" status --porcelain -- workspaces/ 2>/dev/null)"
[ -z "${DIRTY}" ] || die "${PORTAL_DIR}/workspaces has local modifications:
${DIRTY}
   The setup patches three files there and needs a clean tree. Discard them:
       git -C ${PORTAL_DIR} checkout -- workspaces/"

ON_BRANCH="$(git -C "${PORTAL_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null)"
if [ "${ON_BRANCH}" != "${PORTAL_BRANCH}" ]; then
	die "${PORTAL_DIR} is on '${ON_BRANCH}', not '${PORTAL_BRANCH}'.
   That branch carries the triage client extensions. Building from another one
   deploys a bundle with the Triage column missing — and nothing tells you:
       git -C ${PORTAL_DIR} checkout ${PORTAL_BRANCH}"
fi

# ~40 GB: the portal build plus the container images.
AVAIL_GB="$(df -BG --output=avail "${REPO_ROOT}" 2>/dev/null | tail -1 | tr -dc '0-9')"
if [ -n "${AVAIL_GB}" ] && [ "${AVAIL_GB}" -lt 40 ]; then
	log "   WARNING: ${AVAIL_GB} GB free. The build and images want about 40."
fi

log "   docker      running"
log "   portal      ${PORTAL_DIR} (${ON_BRANCH}, clean)"
log "   testray2    ${TESTRAY2_DIR}"
log "   python      $(${PYTHON_BIN} --version 2>&1)"

if [ "${CHECK_ONLY}" == "true" ]; then
	log "--check: prerequisites ok, nothing was run."
	exit 0
fi

# --- step 1: the containers -------------------------------------------------

function testray_is_up {
	[ "$(curl -s -o /dev/null -m 5 -w '%{http_code}' "${TESTRAY_URL}/o/c/routines")" == "200" ]
}

step "step 1: Testray"

if [ "${FRESH}" == "false" ] && testray_is_up; then
	log "   already answering at ${TESTRAY_URL} — skipping (pass --fresh to rebuild)"
else
	args=()
	[ "${FRESH}" == "true" ] && args+=(--fresh)
	log "   running setupTestray.sh ${args[*]} — about 20 minutes"
	"${SCRIPT_DIR}/setupTestray.sh" "${args[@]}" || die "setupTestray.sh failed. Its output is above, and ${SCRIPT_DIR}/logs/ has the detail."
	testray_is_up || die "setupTestray.sh finished but ${TESTRAY_URL}/o/c/routines does not answer 200."
fi

# --- step 2: this tool ------------------------------------------------------

step "step 2: testray-analytics"

if [ -x .venv/bin/testray-analysis ]; then
	log "   .venv already built — skipping"
else
	log "   building .venv with ${PYTHON_BIN}"
	"${PYTHON_BIN}" -m venv .venv || die "venv creation failed"
	.venv/bin/pip install --quiet --upgrade pip || die "pip upgrade failed"
	.venv/bin/pip install --quiet -e . || die "pip install -e . failed"
fi

[ -f config/config.yml ] || {
	cp config/config.yml.example config/config.yml
	log "   wrote config/config.yml from the example (gitignored)"
}

# --- step 3: the one manual step -------------------------------------------
#
# The credentials do not exist until a person makes them, so this is where the
# script stops. It checks rather than assumes: an empty client_id is the normal
# state on a first run, not an error.

function has_credentials {
	.venv/bin/python - <<-'PY'
	import sys, yaml
	try:
	    tr = (yaml.safe_load(open("config/config.yml")) or {}).get("testray") or {}
	except Exception:
	    sys.exit(1)
	sys.exit(0 if (tr.get("client_id") and tr.get("client_secret")) else 1)
	PY
}

step "step 3: OAuth application"

if ! has_credentials; then
	cat <<-END

	   This is the one step that cannot be scripted: the credentials do not
	   exist until you create them.

	   1. Open ${TESTRAY_URL} → Control Panel → Security → OAuth2 Administration
	   2. New application, "Client Credentials" grant
	   3. Grant ALL TWELVE of these scopes, in the plain \`.everything\` form —
	      the .read / .write variants are accepted when you save and then
	      silently dropped, so you find out at call time with a 403:

	          c_project    c_team     c_component      c_casetype   c_productversion
	          c_routine    c_build    c_buildsummary   c_run        c_case
	          c_caseresult c_triageresult

	   4. Put the client id and secret into config/config.yml, under \`testray:\`
	      (that file is gitignored — nothing is committed)

	   Then run this script again. It will verify the scopes really landed and
	   carry on from here.

	END
	log "stopped at step 3. Nothing is broken — this is the hand-off point."
	exit 0
fi

log "   credentials present — verifying what the token actually carries"
.venv/bin/testray-analysis preflight || die "preflight failed. The output above says which scope or object is missing."

# --- step 4: real data ------------------------------------------------------

step "step 4: build data"

if [ "${SKIP_DATA}" == "true" ]; then
	log "   --skip-data — skipping"
else
	BUILD_COUNT="$(curl -s -m 10 "${TESTRAY_URL}/o/c/builds?pageSize=1" 2>/dev/null \
		| sed -n 's/.*"totalCount"[: ]*\([0-9]*\).*/\1/p')"
	if [ -n "${BUILD_COUNT}" ] && [ "${BUILD_COUNT}" -gt 0 ]; then
		log "   ${BUILD_COUNT} build(s) already loaded — skipping (safe to re-run by hand)"
	else
		log "   copying builds down from prod — about 10 minutes"
		.venv/bin/python "${SCRIPT_DIR}/loadTestrayData.py" \
			|| die "loadTestrayData.py failed. Prod credentials go in config/config.yml as prod_client_id / prod_client_secret."
	fi
fi

# --- step 5: the triage screens --------------------------------------------

step "step 5: triage client extension"

if [ "${SKIP_CX}" == "true" ]; then
	log "   --skip-cx — skipping"
else
	"${SCRIPT_DIR}/deployCx.sh" liferay-testray-analytics-custom-element \
		|| die "deployCx.sh failed. If it says 'tsc: not found', node_modules is gone:
       cd ${PORTAL_DIR}/workspaces/liferay-testray-workspace && yarn install --frozen-lockfile"
fi

# --- done -------------------------------------------------------------------

cat <<-END

	$(date '+%H:%M:%S') ── ready

	   Testray:   ${TESTRAY_URL}
	   Triage:    ${TESTRAY_URL}/web/liferay-testray/triage

	   Try one analysis. This part is free:

	       .venv/bin/testray-analysis scan --once --routine <id> --dry-run

	   That prints the build pairs it would analyse, and writes nothing. Then
	   pick a pair and gather the evidence:

	       ./scripts/triage_pipeline.sh -b <older> -t <newer> --no-classify

	   Getting verdicts calls a model and costs money; QUICKSTART-LOCAL-TESTRAY.md
	   step 6 covers it, and \`classify --dry-run\` prices a run before you commit
	   to it.

END
