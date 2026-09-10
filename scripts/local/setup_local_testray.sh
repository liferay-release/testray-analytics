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

# "Answering" is not the same as "200". Once credentials are wanted,
# /o/c/routines returns 401 on a perfectly healthy instance — gating on 200
# would call it down and re-run a 20-minute setup. The portal root is the right
# liveness probe (setupTestray.sh uses the same one); the object endpoint is
# checked separately, where 404 means the extension did not install.
function testray_is_up {
	local code
	code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "${TESTRAY_URL}/" 2>/dev/null || true)"
	[ "${code}" == "200" ] || [ "${code}" == "302" ]
}

function testray_objects_ready {
	local code
	code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "${TESTRAY_URL}/o/c/routines" 2>/dev/null || true)"
	[ -n "${code}" ] && [ "${code}" != "000" ] && [ "${code}" != "404" ]
}

step "step 1: Testray"

if [ "${FRESH}" == "false" ] && testray_is_up; then
	log "   already answering at ${TESTRAY_URL} — skipping (pass --fresh to rebuild)"
else
	args=()
	[ "${FRESH}" == "true" ] && args+=(--fresh)
	log "   running setupTestray.sh ${args[*]} — about 20 minutes"
	log "   most of that is one silent wait for the portal to answer."
	log "   Watch it in another terminal:"
	log "       docker logs -f --tail 100 testray-liferay"
	"${SCRIPT_DIR}/setupTestray.sh" "${args[@]}" || die "setupTestray.sh failed. Its output is above, and ${SCRIPT_DIR}/logs/ has the detail."
	testray_is_up || die "setupTestray.sh finished but ${TESTRAY_URL} does not answer."
	testray_objects_ready || die "${TESTRAY_URL} is up but /o/c/routines 404s — the Testray client extension did not install. ${SCRIPT_DIR}/TESTRAY-SETUP.md has the recovery steps."
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
	# Pass the branch explicitly: deployCx.sh only checks when the variable is
	# set, and an unset one turns off the guard that stops a bundle built from
	# the wrong branch — which deploys cleanly and leaves the Triage column
	# blank with nothing to tell you why.
	TESTRAY_EXPECT_BRANCH="${PORTAL_BRANCH}" \
	"${SCRIPT_DIR}/deployCx.sh" liferay-testray-analytics-custom-element \
		|| die "deployCx.sh failed. If it says 'tsc: not found', node_modules is gone:
       cd ${PORTAL_DIR}/workspaces/liferay-testray-workspace && yarn install --frozen-lockfile"
fi

# --- done -------------------------------------------------------------------
#
# The closing message walks all three moving parts, because installing them
# proves nothing about whether they are wired together: two things can queue
# work (the scanner, and the Run Triage button) and one thing drains it.
# Testing only the button leaves the scheduled path — the one release-master
# actually runs — unexercised.

# Name the routines that exist, so the commands below can be copy-pasted
# instead of guessed at. Local ids are not prod ids: whatever the loader
# created here is what these need.
ROUTINES="$(.venv/bin/python - <<-'PY' 2>/dev/null || true
	import urllib.error
	from testray_analytics.analysis.prepare import load_config, fetch_paginated, _testray_oauth_token
	try:
	    tr = load_config()["testray"]
	    items = fetch_paginated("/o/c/routines", {"pageSize": "20"},
	                            token=_testray_oauth_token(tr),
	                            base_url=tr["base_url"])
	    for r in items[:10]:
	        print(f"          {r.get('id')}  {r.get('name')}")
	except Exception:
	    pass
	PY
)"

cat <<-END

	$(date '+%H:%M:%S') ── ready

	   Testray:   ${TESTRAY_URL}
	   Triage:    ${TESTRAY_URL}/web/liferay-testray/triage

	   Two things queue work and one thing drains it. Exercise all three: the
	   scanner is what release-master runs on a schedule, the button is what a
	   person uses, and the drainer serves both.

	   1. GIVE YOURSELF THE ROLE — needed for step 3, and its absence is
	      invisible: the Triage options simply do not appear.

	          ${TESTRAY_URL}
	          Control Panel -> Users and Organizations -> Test Test
	          -> Roles -> assign "Testray Administrator"

	   2. TEST THE SCANNER. Free, and writes nothing:

	          .venv/bin/testray-analysis scan --once --routine <id> --dry-run
	${ROUTINES:+
	      Routines on this instance:
	${ROUTINES}}
	      It prints the build pairs it would queue and why. Drop \\`--dry-run\\`
	      to actually queue them.

	   3. TEST THE BUTTON. In a routine's build list, right-click the older
	      build -> Select Triage Baseline, right-click the newer one ->
	      Select Triage Target, then Run Triage on the target. Pick two builds
	      that ran the same tests, or the comparison means nothing.

	      That writes a queued run and nothing more.

	   4. DRAIN whatever is queued, from either source:

	          .venv/bin/testray-analysis watch --once --classify

	      That runs the whole loop for each queued pair — reads the builds,
	      diffs the commits, asks a model for verdicts, writes them back to
	      Testray, renders the report and leaves a Slack message — then
	      exits. Refresh the Triage page and the verdicts are there.

	      \\`--classify\\` is what makes it a real run. Without it the drainer
	      stops after the free half and prints two commands to finish by hand.

	      It defaults to the \\`claude-code\\` engine, which uses your Claude
	      Code subscription and needs the \\`claude\\` CLI installed and signed
	      in. Add \\`--engine api\\` to bill the Anthropic API instead — that
	      path is capped at \$15 per run, and a Stable-sized run is about
	      \$0.27.

	      To watch it happen live instead, drop \\`--once\\`: it polls every ten
	      seconds until you stop it, and the build list shows each run as a
	      coloured diamond while it works.

	   If a queued run is never picked up, the drainer is pointed at a
	   different instance or the row was never written:
	   \\`.venv/bin/testray-analysis preflight\\` says which.

END
