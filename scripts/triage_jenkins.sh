#!/bin/bash
#
# triage_jenkins.sh — find newly broken builds and explain what broke them.
#
# Triggered by a hook on the Jenkins side when a Stable build fails — not on a
# timer. Each run looks for builds
# whose test failures nobody has accounted for yet, then analyses them: it reads
# the failures out of Testray, works out which commits could have caused them,
# writes the verdicts back so the team can see them in Testray, and leaves a
# Slack message summarising what it found.
#
# Nothing is analysed twice. A failure that already carries a verdict, and a
# build pair that has already been looked at, are both skipped — which is what
# keeps a routine that is red for days from being re-analysed every half hour.
#
# This is the entry point on release-master. The job binds three secrets, sets a few
# paths, and calls this; the ORDER of the two commands, the preflight checks and
# the lock live here rather than in a text field in a job config, so they can be
# reviewed, tested and fixed like the rest of the pipeline.
#
#   Build step:  ./scripts/triage_jenkins.sh --wait-for-import
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
# running — a job that notifies on failure must not be woken by an overlap), 1
# usage or preflight, 2 a step failed. A skip is recoverable rather than free:
# the build that triggered it waits for the NEXT Stable failure to be picked up
# by --catch-up, which is no longer half an hour away.
#
# The hook fires the instant Stable's build fails, which is before Testray has
# finished importing that build's results. --wait-for-import (opt-in — see
# the Build step above) makes tick() poll for that before scanning, so THIS
# trigger can catch the build instead of leaving it for the next one's
# --catch-up.

set -o pipefail

function die {
	local message="$(date '+%Y-%m-%d %H:%M:%S') ! ${*}"

	echo "${message}" >&2

	exit 1
}

function ensure_slack_fallback {
	# The Slack message is only ever meant to exist on a SUCCESSFUL tick — a
	# failure is left with no file on purpose, so the Jenkins Slack Notifier's
	# ${FILE,path=...} template has nothing to read and notifyEveryFailure
	# posts no summary. This only covers the "ran fine but queued nothing"
	# success case, where submit never runs to write the real file itself.
	local slack_file="${_PROJECT_DIR}/slack/testray_analyzer_slack_message.txt"

	if [ -f "${slack_file}" ]
	then
		return
	fi

	mkdir --parents "$(dirname -- "${slack_file}")"
	printf '%s\n' "${1}" > "${slack_file}"
}

function log {
	echo "$(date '+%Y-%m-%d %H:%M:%S') ${*}"
}

function main {
	local script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

	# _CLASSIFY, _ENGINE, _WAIT_FOR_IMPORT, _PYTHON_BIN and _PROJECT_DIR are
	# read by die, print_help and tick below, so they stay shared globals
	# rather than locals of main.
	_PROJECT_DIR=$(dirname -- "${script_dir}")

	trap 'unset TESTRAY_CLIENT_ID TESTRAY_CLIENT_SECRET ANTHROPIC_API_KEY' EXIT

	_CLASSIFY="true"
	_ENGINE=${TRIAGE_ENGINE:-api}
	# Off by default: only the failure-triggered hook run needs this, and
	# a manual `--no-classify`/`--check` call to prove the wiring should not
	# be made to sit through up to TRIAGE_IMPORT_WAIT_TIMEOUT for nothing.
	_WAIT_FOR_IMPORT="false"
	local check_only="false"
	local require_objects="false"
	_PYTHON_BIN=${TRIAGE_PYTHON_BIN:-python3.13}

	# The instance is defaulted because this script exists to run against prod, and
	# `testray_target()` prints where it actually pointed on every command — so a
	# wrong value is visible in the console rather than inferred from a 401.
	export TESTRAY_BASE_URL=${TESTRAY_BASE_URL:-https://testray.liferay.com}
	export TESTRAY_UI_URL=${TESTRAY_UI_URL:-https://testray.liferay.com/web/testray}

	# The credential guard in triage_pipeline.sh refuses env-supplied secrets,
	# because a stale TESTRAY_CLIENT_ID in a shell silently redirects the read half
	# of a run. On release-master they are supplied deliberately, which is what
	# this says.
	export TRIAGE_ALLOW_ENV_CREDENTIALS=1

	while [[ "${#}" -gt 0 ]]
	do
		case "${1}" in
			--no-classify)
				_CLASSIFY="false"
				shift
				;;
			--engine)
				_ENGINE=${2}
				shift 2
				;;
			--check)
				check_only="true"
				shift
				;;
			--require-objects)
				require_objects="true"
				shift
				;;
			--wait-for-import)
				_WAIT_FOR_IMPORT="true"
				shift
				;;
			-h|--help)
				print_help
				exit 0
				;;
			*)
				print_help
				die "Unable to recognize option: ${1}"
				;;
		esac
	done

	if ! cd "${_PROJECT_DIR}"
	then
		die "Unable to cd to ${_PROJECT_DIR}"
	fi

	# --- preflight --------------------------------------------------------------
	#
	# Every check here is something that has failed, or would fail, silently: a
	# missing secret reads as a broken instance, a wrong remote produces links to a
	# repo that does not have the commits, and a missing API key surfaces minutes
	# into a run rather than at the start.

	for var in TRIAGE_REPO_PATH TRIAGE_SCAN_ROUTINES
	do
		if [ -z "${!var}" ]
		then
			die "Unable to find ${var} in the environment. See --help."
		fi
	done

	if [[ "$(hostname)" =~ ^release-slave-[1-4]$ ]]
	then
		export TESTRAY_CLIENT_ID=$("${_PROJECT_DIR}/scripts/get_credential.sh" "Testray OAuth - Liferay Release User" "username")
		export TESTRAY_CLIENT_SECRET=$("${_PROJECT_DIR}/scripts/get_credential.sh" "Testray OAuth - Liferay Release User" "password")

		if [ -z "${TESTRAY_CLIENT_ID}" ] || [ -z "${TESTRAY_CLIENT_SECRET}" ]
		then
			die "Unable to fetch Testray OAuth credentials from 1Password Connect."
		fi
	else
		if [ -z "${TESTRAY_CLIENT_ID}" ]
		then
			die "Unable to find TESTRAY_CLIENT_ID in the environment. See --help."
		fi

		if [ -z "${TESTRAY_CLIENT_SECRET}" ]
		then
			die "Unable to find TESTRAY_CLIENT_SECRET in the environment. See --help."
		fi
	fi

	if [ ! -d "${TRIAGE_REPO_PATH}" ]
	then
		die "Unable to use TRIAGE_REPO_PATH=${TRIAGE_REPO_PATH}: not a directory. prepare needs a persistent liferay-portal checkout to diff against."
	fi

	if [ "${_CLASSIFY}" == "true" ] && [ "${_ENGINE}" == "api" ]
	then
		if [[ "$(hostname)" =~ ^release-slave-[1-4]$ ]]
		then
			export ANTHROPIC_API_KEY=$("${_PROJECT_DIR}/scripts/get_credential.sh" "Release Team Claude API Token" "credential")

			if [ -z "${ANTHROPIC_API_KEY}" ]
			then
				die "Unable to fetch ANTHROPIC_API_KEY (item 'Release Team Claude API Token', field 'credential') from 1Password Connect, and --engine api needs it."
			fi
		else
			if [ -z "${ANTHROPIC_API_KEY}" ]
			then
				die "Unable to find ANTHROPIC_API_KEY in the environment, and --engine api needs it. (Check the binding name: ANTHROPIC, not ANTROPIC.)"
			fi
		fi
	fi

	# A wrong remote is the quiet one: github_slug() falls back to
	# liferay/liferay-portal, so every commit and compare link in the report and in
	# the Slack message points at a repo where Stable's commits do not exist yet.
	local expect_remote=${TRIAGE_EXPECT_REMOTE:-brianchandotcom}
	local remote_name=${TRIAGE_ROUTINE_REMOTES#*=}
	remote_name=${remote_name:-origin}
	local remote_url=$(git -C "${TRIAGE_REPO_PATH}" remote get-url "${remote_name}" 2> /dev/null)

	if [ -z "${remote_url}" ]
	then
		die "Unable to find git remote '${remote_name}' in ${TRIAGE_REPO_PATH}. TRIAGE_ROUTINE_REMOTES names a REMOTE, not a URL."
	fi

	case "${remote_url}" in
		*"${expect_remote}"*) ;;
		*)
			die "Unable to confirm remote '${remote_name}' (${remote_url}) points at '${expect_remote}'. Stable's commits live on brianchandotcom; the wrong remote yields links to commits that are not there. Override with TRIAGE_EXPECT_REMOTE."
			;;
	esac

	# Editable install, so config/ and slack/ resolve inside this checkout rather
	# than next to a copy in site-packages.
	if [ ! -x .venv/bin/testray-analysis ]
	then
		log "> building venv with ${_PYTHON_BIN}"

		if ! command -v "${_PYTHON_BIN}" > /dev/null
		then
			die "Unable to find ${_PYTHON_BIN}. Set TRIAGE_PYTHON_BIN."
		fi

		if ! "${_PYTHON_BIN}" -m venv .venv
		then
			die "Unable to create the venv"
		fi

		if ! .venv/bin/pip install --quiet --upgrade pip
		then
			die "Unable to upgrade pip"
		fi

		if ! .venv/bin/pip install --quiet --editable .
		then
			die "Unable to run pip install -e ."
		fi
	fi

	export TRIAGE_LOG_DIR=${TRIAGE_LOG_DIR:-${_PROJECT_DIR}/logs}

	if ! mkdir --parents "${TRIAGE_LOG_DIR}"
	then
		die "Unable to create ${TRIAGE_LOG_DIR}"
	fi

	# Step 0 is the whole preamble: everything a reader needs to find this run's
	# output before any of it exists. On a failure twenty minutes in, the console
	# has already said where to look.
	log "step 0: preflight ok"
	log "  routines: ${TRIAGE_SCAN_ROUTINES}   engine: ${_ENGINE}   classify: ${_CLASSIFY}   wait_for_import: ${_WAIT_FOR_IMPORT}"
	log "  portal:   ${TRIAGE_REPO_PATH}  (${remote_name} -> ${remote_url})"
	log "  logs:     ${TRIAGE_LOG_DIR}  (one file per pipeline step)"
	log "  bundles:  ${_PROJECT_DIR}/runs"
	log "  slack:    ${_PROJECT_DIR}/slack/testray_analyzer_slack_message.txt"

	log "step 0: checking credentials, scopes and the triage Objects"

	local preflight_args=()

	if [ "${require_objects}" == "true" ]
	then
		preflight_args+=(--require-objects)
	fi

	if ! .venv/bin/testray-analysis preflight "${preflight_args[@]}"
	then
		die "Unable to complete preflight — fix the above before running the pipeline. A 403 on an /o/c/ endpoint is a missing OAuth scope, NOT a missing deploy: the queue cannot tell them apart, so it would silently fall back to marker files and every write would be refused."
	fi

	if [ "${check_only}" == "true" ]
	then
		log "--check: nothing was run"
		exit 0
	fi

	# --- one tick, under a lock -------------------------------------------------
	#
	# The lock is here rather than left to the job's "do not allow concurrent
	# builds" checkbox because the cost of getting it wrong is money: two Stable
	# builds can fail inside one classify run, firing the hook twice, and nothing
	# in the queue stops two drainers claiming the same work. A second tick exits
	# 3 immediately, which reads as "skipped", not as a failure.
	local lock_file=${TRIAGE_LOCK_FILE:-${TMPDIR:-/tmp}/triage_jenkins.lock}

	local rc

	if command -v flock > /dev/null
	then
		if ! exec 9> "${lock_file}"
		then
			die "Unable to open lock ${lock_file}"
		fi
		if ! flock --nonblock 9
		then
			# Success on purpose: the previous tick is still working, which is
			# exactly what the lock is for. Failing here would turn a normal
			# overlap into a red build and a Slack alert about nothing.
			log "skipped: another tick still holds ${lock_file}"
			exit 0
		fi
		tick
		rc=${?}
	else
		log "WARNING: flock not available, running without a lock. Make sure the"
		log "         job forbids concurrent builds, or two ticks can pay twice"
		log "         for one analysis."
		tick
		rc=${?}
	fi

	if [[ "${rc}" -ne 0 ]]
	then
		log "! failed (exit ${rc}) — the failing step's own log is in ${TRIAGE_LOG_DIR}"
		exit "${rc}"
	fi

	# The Slack message is written by submit on every run, including one that
	# concluded nothing. Naming it here means the job's console says where the
	# post-build step should read from.
	local slack_file="${_PROJECT_DIR}/slack/testray_analyzer_slack_message.txt"

	if [ ! -f "${slack_file}" ]
	then
		# submit only runs when a job was actually claimed — a tick that finds
		# nothing new to queue (everything already registered, or no failures
		# in the window) never calls it. Still a success, so the Jenkins Slack
		# Notifier's ${FILE,path=...} template (notifySuccess) needs something
		# to read even though nothing was analysed this time.
		log "note: no Slack message was written — submit did not run"
		ensure_slack_fallback "ℹ️ Triage tick completed — nothing was submitted this run (no new work queued)."
	fi

	log "done"
}

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
	  --wait-for-import  before scanning, wait for a build newer than the one
	                     on file to finish importing (TRIAGE_IMPORT_POLL_INTERVAL
	                     / TRIAGE_IMPORT_WAIT_TIMEOUT below). Pass this on the
	                     failure-triggered hook, where the tick can fire before
	                     Testray has even created the build's row. Off by
	                     default so a manual run to prove the wiring does not
	                     sit through the wait for nothing.
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
	                        ${_PYTHON_BIN})
	  TRIAGE_IMPORT_POLL_INTERVAL  seconds between import-status checks
	                        (default 60) while scan --wait-for-import waits
	                        for a just-failed build to finish importing
	  TRIAGE_IMPORT_WAIT_TIMEOUT   seconds to wait before giving up on that
	                        build for this tick (default 3600 = 1 hour — measured
	                        import lag on a real trigger has been ~24 minutes)
	  TRIAGE_IMPORT_SETTLE_WINDOW  heuristic fallback only (no PORTAL_GIT_COMMIT):
	                        seconds to watch for a newer build before accepting
	                        an already-DONE one as the answer (default 600 =
	                        10 min). Without this, a tick that starts late
	                        finds the build it was triggered for already done
	                        and sits out the full wait timeout for a build id
	                        that will never appear.
	  PORTAL_GIT_COMMIT     the commit Stable was testing when the hook fired
	                        (LPD-105603, set by the Jenkins trigger itself).
	                        When present, scan --wait-for-import waits for
	                        THIS exact build instead of guessing from
	                        whatever is newest. Optional: unset falls back
	                        to the guess.
	END
}

function tick {
	local rc=0

	# Producer. Cheap, and never spends: REST reads plus a git diff.
	#
	# --wait-for-import (opt-in via the script's own --wait-for-import flag):
	# for the case where this tick was fired by the Stable build failing,
	# which is BEFORE Testray finishes importing that build's results.
	# Without it, scan queries `importStatus eq 'DONE'`, the just-failed build
	# fails that filter, and the tick reads exactly like "nothing red" — the
	# build then waits for the NEXT Stable failure's --catch-up, which on a
	# quiet week is days away. Polling here (still free: REST reads only)
	# lets THIS trigger catch it instead. Left off by default so a manual
	# `--no-classify`/`--check` run to prove the wiring does not sit through
	# up to TRIAGE_IMPORT_WAIT_TIMEOUT for a build that was never coming.
	local scan_args=(--once)

	if [ "${_WAIT_FOR_IMPORT}" == "true" ]
	then
		scan_args+=(--wait-for-import)

		# LPD-105603: the Stable job's own trigger now passes the commit it
		# was testing (PORTAL_GIT_COMMIT), which lets scan wait for that EXACT
		# build instead of guessing from whatever is newest. Passed only when
		# non-empty: an unset PORTAL_GIT_COMMIT must fall back to the
		# heuristic, not hand scan a blank filter value.
		if [ -n "${PORTAL_GIT_COMMIT:-}" ]
		then
			scan_args+=(--trigger-git-commit "${PORTAL_GIT_COMMIT}")
		fi
	fi

	# The TICK owns the Slack file, not any one step. scan writes a "nothing
	# new" message when it queues nothing, and watch's submits each append
	# theirs, so clearing it here — once, before either runs — is what lets a
	# tick that both recognised old failures and analysed new ones report both.
	# Without the reset the post would carry the previous tick's text too.
	rm --force "${_PROJECT_DIR}/slack/testray_analyzer_slack_message.txt"

	export TRIAGE_SLACK_APPEND=1

	log "step 1: scan ${scan_args[*]}"

	if ! .venv/bin/testray-analysis scan "${scan_args[@]}"
	then
		return 2
	fi

	log "step 1: scan ok"

	# Consumer. This is the step that costs money when --classify is on.
	local watch_args=(--once --engine "${_ENGINE}")

	if [ "${_CLASSIFY}" == "true" ]
	then
		watch_args+=(--classify)
	fi

	log "step 2: watch ${watch_args[*]}"

	if ! .venv/bin/testray-analysis watch "${watch_args[@]}"
	then
		rc=2
	fi

	if [[ "${rc}" -eq 0 ]]
	then
		log "step 2: watch ok"
	fi

	return "${rc}"
}

main "${@}"
