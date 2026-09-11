#!/bin/bash
#
# deployCx.sh — rebuild a Testray client extension and hot-deploy it to the
# local DXP, then prove the container is actually serving the new build.
#
# Local-environment tooling, which is why it lives here beside setupTestray.sh
# rather than in either source repo: it reaches across both the liferay-portal
# workspace (source) and the testray2 docker tree (the running instance).
#
#   ./deployCx.sh liferay-testray-analytics-custom-element
#   ./deployCx.sh liferay-testray-custom-element
#
# It exists because two things about this loop are non-obvious and both fail
# quietly:
#
#   1. The zip's client-extension-config.json names the stylesheet by CONTENT
#      HASH (`cssURLs=index.<hash>.css`). Any style change re-hashes the file,
#      so copying static/ alone leaves Liferay asking for a stylesheet that no
#      longer exists and the view renders unstyled. This regenerates the config
#      from the currently deployed zip, rewriting that entry.
#
#   2. `bundles/osgi/client-extensions/<name>.zip` keeps its OLD mtime after a
#      redeploy, so it tells you nothing about whether the deploy landed. The
#      only reliable check is diffing the SERVED asset against the build, which
#      is what this polls for.
#
# Exit codes: 0 serving the new build, 1 usage/build failure, 2 deployed but the
# served asset never matched.

set -euo pipefail

function main {
	# Derived, not hardcoded to one person's home: this script lives in
	# <testray-analytics>/scripts/local, so the directory two levels above the
	# repo root is the shared parent holding liferay-portal and testray2 beside
	# it. Every level can be overridden.
	#
	# Not sorted alphabetically: each depends on the one before it, so
	# reordering would use a variable before it is set.
	local script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
	local repo_root=$(cd -- "${script_dir}/../.." && pwd)
	local workspace_dir=${TESTRAY_WORKSPACE_DIR:-$(dirname -- "${repo_root}")}

	local bundles=${TESTRAY_BUNDLES:-${workspace_dir}/testray2/liferay/bundles}
	local portal_dir=${TESTRAY_PORTAL_DIR:-${workspace_dir}/liferay-portal}
	local portal_url=${TESTRAY_URL:-http://localhost:8080}
	local workspace=${TESTRAY_WORKSPACE:-${portal_dir}/workspaces/liferay-testray-workspace/client-extensions}

	local name=${1:-}

	if [ -z "${name}" ] || [ "${name}" == "-h" ] || [ "${name}" == "--help" ]
	then
		cat <<-END
		Usage: $(basename "${0}") <client-extension-name>

		Available in ${workspace}:
		$(ls -1 "${workspace}" 2> /dev/null | sed --expression "s/^/	  /")

		Environment:
		  TESTRAY_WORKSPACE  client-extensions dir (default the liferay-portal workspace)
		  TESTRAY_BUNDLES    docker bundles dir   (default <workspace>/testray2/liferay/bundles,
		                     where <workspace> is the directory holding this repo)
		  TESTRAY_PORTAL_DIR liferay-portal checkout (default <workspace>/liferay-portal)
		  TESTRAY_WORKSPACE_DIR  override <workspace> itself
		  TESTRAY_URL        portal base url      (default http://localhost:8080)
		END
		exit 1
	fi

	local ce_dir="${workspace}/${name}"
	local deployed="${bundles}/osgi/client-extensions/${name}.zip"

	if [ ! -d "${ce_dir}" ]
	then
		echo "Unable to find client extension: ${ce_dir}" >&2
		exit 1
	fi

	if [ ! -f "${deployed}" ]
	then
		echo "Unable to find a deployed config to reuse (not currently deployed): ${deployed}" >&2
		exit 1
	fi

	local work=$(mktemp --directory)
	trap 'rm --force --recursive "${work}"' EXIT

	# Which branch the source is on. Not cosmetic: liferay-testray-custom-element
	# exists on every branch, so building it from a branch without the triage
	# work deploys a bundle with the hook MISSING and silently removes the
	# feature from the running instance. The build succeeds, the deploy
	# succeeds, and the column just goes blank. Print it, and refuse when
	# TESTRAY_EXPECT_BRANCH says otherwise.
	local branch=$(git -C "${workspace}" rev-parse --abbrev-ref HEAD 2> /dev/null || echo '?')

	echo "== build ${name}"
	echo "   source branch: ${branch}"

	if [ -n "${TESTRAY_EXPECT_BRANCH:-}" ] && [ "${branch}" != "${TESTRAY_EXPECT_BRANCH}" ]
	then
		echo "   !! Unable to confirm branch: expected ${TESTRAY_EXPECT_BRANCH}, found ${branch}." >&2
		echo "      Refusing: building from the wrong branch can deploy a bundle" >&2
		echo "      that silently drops features present on the other one." >&2
		exit 1
	fi

	# `npm run build` and not `npx tsc`: there is no local tsc on the package, so
	# npx installs an unrelated `tsc` package from npm, prints "This is not the tsc
	# command you are looking for", and exits 0 WITHOUT typechecking. If this step
	# fails with "tsc: not found", node_modules is missing — it is a yarn workspace,
	# so run `yarn install --frozen-lockfile` at the workspace ROOT, not here.
	if ! (cd "${ce_dir}" && npm run build > "${work}/build.log" 2>&1)
	then
		tail --lines=25 "${work}/build.log"
		exit 1
	fi

	python3 - "${name}" "${ce_dir}" "${deployed}" "${work}" <<'PY'
import json, os, sys, zipfile

name, ce_dir, deployed, work = sys.argv[1:5]
build = f'{ce_dir}/build/static'
statics = sorted(os.listdir(build))
css = [f for f in statics if f.endswith('.css')]

src = zipfile.ZipFile(deployed)
cfg_name = [n for n in src.namelist() if n.endswith('client-extension-config.json')][0]
cfg = json.loads(src.read(cfg_name))
key = next(iter(cfg))

# Bumped so the deploy is unambiguously newer than what is registered.
cfg[key]['buildTimestamp'] += 1

if css:
    cfg[key]['typeSettings'] = [
        f'cssURLs={css[0]}' if s.startswith('cssURLs=') else s
        for s in cfg[key]['typeSettings']
    ]

with zipfile.ZipFile(f'{work}/{name}.zip', 'w', zipfile.ZIP_DEFLATED) as dst:
    for item in src.infolist():
        if item.filename.startswith('static/'):
            continue                      # replaced below
        if item.filename == cfg_name:
            dst.writestr(item.filename, json.dumps(cfg, indent=1))
        else:
            dst.writestr(item, src.read(item.filename))
    for f in statics:
        dst.write(f'{build}/{f}', f'static/{f}')

print('   statics:', ', '.join(statics))
PY

	echo "== deploy"
	cp "${work}/${name}.zip" "${bundles}/deploy/"

	# AutoDeploy normally consumes it in a few seconds.
	for _ in $(seq 1 30)
	do
		if [ ! -f "${bundles}/deploy/${name}.zip" ]
		then
			break
		fi

		sleep 2
	done

	if [ -f "${bundles}/deploy/${name}.zip" ]
	then
		echo "   !! Unable to confirm the deploy landed — still sitting in deploy/ after 60s. Is the container up?" >&2
		exit 2
	fi

	# The bundle restart is asynchronous, so poll the served asset rather than
	# sleeping a fixed amount and hoping.
	local built_sum=$(md5sum "${ce_dir}/build/static/index.js" | cut --delimiter=' ' --fields=1)

	for _ in $(seq 1 30)
	do
		local served_sum=$( \
			curl --silent "${portal_url}/o/${name}/index.js" | \
			md5sum | \
			cut --delimiter=' ' --fields=1)

		if [ "${served_sum}" == "${built_sum}" ]
		then
			echo "   serving the new build"
			exit 0
		fi

		sleep 2
	done

	echo "   !! Unable to confirm the new build is being served — still differs after 60s" >&2
	echo "      check ${bundles}/logs/ for a STOPPED/STARTED pair for this bundle" >&2
	exit 2
}

main "${@}"
