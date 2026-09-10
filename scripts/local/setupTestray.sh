#!/usr/bin/env bash
#
# setupTestray.sh — bring up a local Testray from scratch.
#
# Part 1 of 2. This part follows the "Deploy Testray with docker compose"
# runbook: start the containers, then deploy the REST API and the client
# extensions in order. Part 2 (fetch prod data and load it locally) is a
# separate script that needs OAuth credentials in config.yml first.
#
# Deviations from the runbook, all deliberate:
#   - step 2  (license key)     skipped — the container already has one
#   - step 3                    does not exist; the doc's numbering skips it
#   - step 5  (rest-impl)       deployed, but with a local SQL patch instead of
#                               the runbook's revert-16b8cd7 dance (see below)
#   - step 9b (analytics CX)    added — our TriageResult object (LPD-95843)
#
# The rest-impl patch, and why it exists:
#   TestrayStatusMetricResourceImpl hand-writes SQL that reads cpuUseTime_ and
#   importStatus_ from the Build *extension* table (alias bx). On prod those
#   columns live there because they were added to the Build object after it was
#   created. A site-initializer creates Build with every field at once, so
#   locally they land on the base table and the extension table holds only
#   c_buildid_ — every query using bx.<those columns> then fails with
#   "column bx.cpuusetime_ does not exist", the endpoint 500s, and the Testray
#   UI shows no routines and no builds (the pages are driven by
#   /o/testray-rest/v1.0/testray-status-metrics/...).
#
#   So we rewrite bx.-> b. for those two columns, deploy, and immediately
#   restore the file. The fix lives only in the deployed jar; liferay-portal is
#   left pristine, because this is a local-schema workaround and must never be
#   committed. The real fix belongs upstream in testray-rest-impl, which should
#   not hardcode which physical table an Objects field lives in.
#
# The runbook says to run `java11` / `java17` and `gw`. Those are interactive
# shell aliases and do not exist in a script, so JAVA_HOME is set explicitly
# per deploy and gradlew is called by path.
#
#   ./setupTestray.sh                 # start containers + deploy everything
#   ./setupTestray.sh --fresh         # destroy volumes first (wipes the DB)
#   ./setupTestray.sh --skip-startup  # deploys only, containers already up
#   ./setupTestray.sh --only cron     # one component (see --list)
#   ./setupTestray.sh --quiet         # log to file only, don't stream
#   ./setupTestray.sh --list          # show component names
#
# Docker and gradle output streams to the terminal AND the log by default;
# --quiet sends it to the log only.
#
set -euo pipefail

# --- paths ------------------------------------------------------------------

# Every directory is derived, never hardcoded to one person's home. The
# default assumes the three repos sit side by side — this script lives in
# <testray-analytics>/scripts/local, so ../../.. is that shared parent — and
# each one can be pointed elsewhere with an environment variable.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
WORKSPACE_DIR="${TESTRAY_WORKSPACE_DIR:-$(dirname -- "${REPO_ROOT}")}"

TESTRAY2_DIR="${TESTRAY2_DIR:-${WORKSPACE_DIR}/testray2}"
COMPOSE_DIR="${TESTRAY_COMPOSE_DIR:-${TESTRAY2_DIR}/liferay}"
PORTAL="${TESTRAY_PORTAL_DIR:-${WORKSPACE_DIR}/liferay-portal}"
WORKSPACE="$PORTAL/workspaces/liferay-testray-workspace"
METRIC_SRC="$WORKSPACE/modules/testray-rest-impl/src/main/java/com/liferay/testray/rest/internal/resource/v1_0/TestrayStatusMetricResourceImpl.java"
SI_DIR="$WORKSPACE/client-extensions/liferay-testray-site-initializer/site-initializer"
SUMMARY_DEF="$SI_DIR/object-definitions/testray-build-summary.json"
SUMMARY_REL="$SI_DIR/object-relationships/testray-build-to-buildSummary.json"
BUNDLES="$COMPOSE_DIR/bundles"
CONTAINER="testray-liferay"
DB_CONTAINER="testray-postgres"
BASE_URL="http://localhost:8080"
EXPECTED_BRANCH="${TESTRAY_EXPECT_BRANCH:-master-testray}"

# JDK locations differ per machine; zulu is what the Testray build expects.
JAVA11="${TESTRAY_JAVA11:-/usr/lib/jvm/zulu11}"
JAVA17="${TESTRAY_JAVA17:-/usr/lib/jvm/zulu17}"

# Logs live beside this script, wherever it has been moved to.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/setup-$(date +%Y%m%dT%H%M%S).log"

# Deploy order matters: the REST API supplies the object definitions the client
# extensions build on, and the site initializers run last so the objects they
# reference already exist.
#   name | relative path under $WORKSPACE | JAVA_HOME | artifact in osgi/*
COMPONENTS=(
  "rest-api|modules/testray-rest-api|$JAVA11|modules"
  "rest-impl|modules/testray-rest-impl|$JAVA11|modules"
  "cron|client-extensions/liferay-testray-etc-cron|$JAVA17|client-extensions"
  "jira|client-extensions/liferay-testray-etc-jira|$JAVA17|client-extensions"
  "custom-element|client-extensions/liferay-testray-custom-element|$JAVA17|client-extensions"
  "site-initializer|client-extensions/liferay-testray-site-initializer|$JAVA17|client-extensions"
  "analytics|client-extensions/liferay-testray-analytics-site-initializer|$JAVA17|client-extensions"
)

# --- options ----------------------------------------------------------------

FRESH=0; SKIP_STARTUP=0; ONLY=""; VERBOSE=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --fresh)        FRESH=1 ;;
    --skip-startup) SKIP_STARTUP=1 ;;
    --quiet)        VERBOSE=0 ;;
    --only)         ONLY="${2:-}"; shift ;;
    --list)         for c in "${COMPONENTS[@]}"; do echo "  ${c%%|*}"; done; exit 0 ;;
    -h|--help)      sed -n '2,30p' "$0"; exit 0 ;;
    *)              echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# --- helpers ----------------------------------------------------------------

mkdir -p "$LOG_DIR"
log()  { printf '\n\033[1m==> %s\033[0m\n' "$*" | tee -a "$LOG_FILE"; }
info() { printf '    %s\n' "$*" | tee -a "$LOG_FILE"; }
die()  { printf '\n\033[31mFAILED: %s\033[0m\n' "$*" | tee -a "$LOG_FILE" >&2; exit 1; }

# Where a long-running command's output goes. Streaming by default: a 15-minute
# gradle deploy with no output is indistinguishable from a hang. `set -o
# pipefail` is on, so piping through here still surfaces the command's failure.
sink() { if (( VERBOSE )); then tee -a "$LOG_FILE"; else cat >>"$LOG_FILE"; fi; }

trap 'die "aborted at line $LINENO — see $LOG_FILE"' ERR

# Sources are patched in place just long enough to build them, then restored on
# any exit — success, failure or Ctrl-C — so a patch can never be left behind
# for someone to commit by accident.
PATCHED_FILES=()
restore_patch() {
  local f
  for f in "${PATCHED_FILES[@]:-}"; do
    [[ -n "$f" ]] || continue
    git -C "$PORTAL" checkout -- "$f" 2>/dev/null && info "restored $(basename "$f")"
  done
  PATCHED_FILES=()
}
trap restore_patch EXIT

# patch_file <path> <sed-script> <description>
# Skips (loudly) if the file is already modified — someone else's edits win.
patch_file() {
  local f="$1" script="$2" desc="$3"
  if [[ ! -f "$f" ]]; then
    info "WARNING: $(basename "$f") not found — skipping patch: $desc"
    return 1
  fi
  if ! git -C "$PORTAL" diff --quiet -- "$f"; then
    info "WARNING: $(basename "$f") already modified — leaving it alone"
    return 1
  fi
  sed -i "$script" "$f"
  if git -C "$PORTAL" diff --quiet -- "$f"; then
    info "no change needed: $desc"
    return 0
  fi
  PATCHED_FILES+=("$f")
  info "patched: $desc"
}

# A patch that matched nothing looks identical to one that was already applied.
# These make the difference visible: they check the file is in the state the
# deploy needs, whatever route it took to get there. Upstream fixing the bug
# themselves is a pass; upstream moving the code is a loud failure.
assert_present() {
  grep -q "$2" "$1" 2>/dev/null || info "WARNING: $3  [$(basename "$1")]"
}
assert_absent() {
  grep -q "$2" "$1" 2>/dev/null && info "WARNING: $3  [$(basename "$1")]" || true
}

# patch_with <path> <function> <description>
# Same guard/record semantics as patch_file, but the edit is done by a shell
# function (for changes sed should not be trusted with, like JSON).
patch_with() {
  local f="$1" fn="$2" desc="$3"
  if [[ ! -f "$f" ]]; then
    info "WARNING: $(basename "$f") not found — skipping patch: $desc"
    return 1
  fi
  if ! git -C "$PORTAL" diff --quiet -- "$f"; then
    info "WARNING: $(basename "$f") already modified — leaving it alone"
    return 1
  fi
  "$fn" "$f"
  if git -C "$PORTAL" diff --quiet -- "$f"; then
    info "no change needed: $desc"
    return 0
  fi
  PATCHED_FILES+=("$f")
  info "patched: $desc"
}

# Append caseResultTotal to BuildSummary's objectFields, matching the shape of
# the counters already there. Idempotent.
add_total_field() {
  python3 - "$1" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    d = json.load(f)
fields = d.get("objectFields", [])
if not any(x.get("name") == "caseResultTotal" for x in fields):
    fields.append({
        "DBType": "Integer",
        "businessType": "Integer",
        "externalReferenceCode": "CASE-RESULTS-TOTAL",
        "indexed": False,
        "indexedAsKeyword": False,
        "label": {"en_US": "Case Results Total"},
        "name": "caseResultTotal",
        "required": False,
        "state": False,
        "system": False,
    })
    with open(path, "w") as f:
        json.dump(d, f, indent="\t")
        f.write("\n")
PY
}

# Wait for the portal to actually answer, not just for the port to open.
wait_for_portal() {
  local deadline=$((SECONDS + 600)) code
  log "Waiting for $BASE_URL (up to 10 min)"
  # This is the longest silent stretch in the whole setup, and a first-time
  # reader has no way to tell "starting normally" from "wedged". Name the one
  # command that shows what is actually happening.
  log "  Watch it in another terminal: docker logs -f --tail 100 testray-liferay"
  while (( SECONDS < deadline )); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE_URL/" || true)
    if [[ "$code" == "200" || "$code" == "302" ]]; then
      info "portal responding (HTTP $code) after ${SECONDS}s"
      # Reported for information, not gated on. On a fresh database this is
      # 404: the Routine object does not exist until testray-rest-api is
      # deployed below. On a re-run against an existing database it is 401
      # (object present, credentials wanted). Either is fine here.
      local api
      api=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE_URL/o/c/routines" || true)
      info "/o/c/routines -> HTTP ${api:-none}"
      return 0
    fi
    sleep 5
  done
  die "portal did not respond within 10 minutes"
}

# gradle exiting 0 only means the artifact was handed to the container; the
# deploy is asynchronous. Compare the artifact's mtime to catch a silent no-op.
artifact_mtime() {
  local kind="$1" pattern="$2" f
  f=$(ls -t "$BUNDLES/osgi/$kind/"*"$pattern"* 2>/dev/null | head -1) || true
  [[ -n "${f:-}" ]] && stat -c %Y "$f" || echo 0
}

# Client-extension zips keep the directory name (liferay-testray-etc-cron.zip),
# but module jars are named from the bundle symbolic name with dots
# (testray-rest-api -> com.liferay.testray.rest.api-1.0.0.jar).
artifact_pattern() {
  local kind="$1" dirname="$2"
  if [[ "$kind" == "modules" ]]; then echo "${dirname//-/.}"; else echo "$dirname"; fi
}

deploy_component() {
  local name="$1" relpath="$2" javahome="$3" kind="$4"
  local dir="$WORKSPACE/$relpath"
  [[ -d "$dir" ]] || die "missing component directory: $dir"
  [[ -d "$javahome" ]] || die "missing JDK: $javahome"

  local artifact before after
  artifact=$(artifact_pattern "$kind" "${relpath##*/}")
  before=$(artifact_mtime "$kind" "$artifact")

  log "Deploying $name  (JDK $(basename "$javahome"))"
  info "$dir"
  (
    cd "$dir"
    JAVA_HOME="$javahome" PATH="$javahome/bin:$PATH" \
      "$WORKSPACE/gradlew" deploy -Ddeploy.docker.container.id="$CONTAINER"
  ) 2>&1 | sink || die "$name deploy failed — tail $LOG_FILE"

  # Liferay's client-extension watcher keys off the artifact's mtime, and
  # gradle PRESERVES the source timestamp when it copies. So an unchanged build
  # drops in a file that still looks old, the container never installs it, and
  # the failure is silent — a site initializer simply never runs and its
  # objects never appear. (Deleting the file first does not help: the copy
  # restores the old timestamp.) Touching it guarantees a pickup.
  local f
  f=$(ls -t "$BUNDLES/osgi/$kind/"*"$artifact"* 2>/dev/null | head -1) || true
  if [[ -z "${f:-}" ]]; then
    info "WARNING: no artifact matching '$artifact' in bundles/osgi/$kind"
    return
  fi
  touch "$f"
  after=$(artifact_mtime "$kind" "$artifact")
  if [[ "$before" == "0" ]]; then
    info "artifact installed: $(basename "$f")"
  else
    info "artifact refreshed: $(basename "$f") @ $(date -d "@$after" '+%H:%M:%S')"
  fi
}

# Our TriageResult definition declares objectFolderExternalReferenceCode:
# TESTRAY, and that folder is created by the *Testray* site initializer. Deploy
# the analytics extension a second later and it aborts with
#   "No ObjectFolder exists with the key {externalReferenceCode=TESTRAY, ...}"
# leaving no Analytics site and no /o/c/triageresults — silently, because the
# gradle deploy itself succeeded. Testray's initializer takes ~15s, so wait for
# the folder to actually exist rather than guessing at a sleep.
wait_for_object_folder() {
  local deadline=$((SECONDS + 300)) n
  log "Waiting for the TESTRAY object folder (Testray initializer must finish first)"
  while (( SECONDS < deadline )); do
    n=$(docker exec "$DB_CONTAINER" psql -U root -d lportal -t -c \
          "select count(*) from objectfolder where externalreferencecode='TESTRAY';" \
        2>/dev/null | tr -d ' \n')
    if [[ "${n:-0}" -gt 0 ]]; then
      info "TESTRAY object folder present after ${SECONDS}s"
      return 0
    fi
    sleep 5
  done
  info "WARNING: TESTRAY object folder never appeared — the analytics"
  info "         initializer will abort and /o/c/triageresults will 404"
}

deploy_analytics() {
  wait_for_object_folder
  deploy_component "analytics" "$1" "$2" "$3"
}

# Deploy rest-impl with the bx.-> b. column fix, then put the source back.
deploy_rest_impl() {
  local relpath="$1" javahome="$2" kind="$3"

  patch_file "$METRIC_SRC" \
    's/bx\.cpuUseTime_/b.cpuUseTime_/g; s/bx\.importStatus_/b.importStatus_/g' \
    "bx.cpuUseTime_ / bx.importStatus_ -> b. (Build fields are on the base table here)"

  # These patches are text matches against upstream source. If upstream moves
  # the code the match silently does nothing, so assert the intent instead of
  # trusting that sed ran.
  assert_absent "$METRIC_SRC" 'bx\.cpuUseTime_\|bx\.importStatus_' \
    "Build fields still read from the bx extension table — the routines page will 500"

  deploy_component "rest-impl" "$relpath" "$javahome" "$kind"
  restore_patch
}

# The stock site-initializer ships a BuildSummary that the builds-metrics query
# cannot use. Two defects, both fixed here for the duration of the build:
#
#   1. testray-build-summary.json defines eight counters but not
#      caseResultTotal, which the SQL selects as bs.caseresulttotal_.
#   2. testray-build-to-buildSummary.json writes the Build placeholder as
#      "[$OBJECT_DEFINITION_ID:Build]" — missing the closing $ — so the token
#      never resolves, the relationship is silently never created, and
#      BuildSummary ends up with no link to Build at all. (Upstream bug: any
#      fresh Testray hits this.)
#
# Both must be present when the site is FIRST initialized: fields and
# relationships declared at creation land on the base table, whereas anything
# added later lands in the _x extension table where the hardcoded SQL cannot
# see it.
deploy_site_initializer() {
  local relpath="$1" javahome="$2" kind="$3"

  patch_with "$SUMMARY_DEF" add_total_field \
    "add caseResultTotal to the BuildSummary definition"

  patch_file "$SUMMARY_REL" \
    's/\[\$OBJECT_DEFINITION_ID:Build\]/[$OBJECT_DEFINITION_ID:Build$]/' \
    "fix missing \$ in the buildToBuildSummary Build placeholder"

  assert_present "$SUMMARY_DEF" 'caseResultTotal' \
    "BuildSummary has no caseResultTotal — the build list will 500"
  assert_present "$SUMMARY_REL" 'OBJECT_DEFINITION_ID:Build\$\]' \
    "buildToBuildSummary placeholder unresolved — Build/BuildSummary will not be linked"

  deploy_component "site-initializer" "$relpath" "$javahome" "$kind"
  restore_patch
}

# --- preflight --------------------------------------------------------------

log "Preflight"
[[ -f "$COMPOSE_DIR/docker-compose.yaml" ]] || die "no docker-compose.yaml in $COMPOSE_DIR"
[[ -x "$WORKSPACE/gradlew" ]] || die "no gradlew in $WORKSPACE"
command -v docker >/dev/null || die "docker not on PATH"

branch=$(git -C "$PORTAL" rev-parse --abbrev-ref HEAD)
info "liferay-portal branch: $branch"
# Fatal, not a warning. On other branches the testray workspace wants Java 17
# where the runbook (and this script) build rest-api with Java 11, so the very
# first deploy dies with an opaque Gradle variant-incompatibility dump — and
# the analytics CX does not exist there at all. Continuing only buys a
# confusing failure ten minutes later.
if [[ "$branch" != "$EXPECTED_BRANCH" && "${ALLOW_ANY_BRANCH:-0}" != "1" ]]; then
  die "liferay-portal is on '$branch', expected '$EXPECTED_BRANCH'.

    git -C $PORTAL switch $EXPECTED_BRANCH

  That branch carries the analytics client extension and the dependency set
  the Java 11 / Java 17 split in this script assumes. Set ALLOW_ANY_BRANCH=1
  to override, but expect the rest-api deploy to fail on a Gradle variant
  mismatch."
fi
info "log: $LOG_FILE"

# --- part 1a: containers ----------------------------------------------------

if (( SKIP_STARTUP )); then
  log "Skipping startup (--skip-startup)"
else
  if (( FRESH )); then
    log "Tearing down containers and volumes (--fresh)"
    info "this wipes the database: OAuth apps, projects and mirrored data all go"
    (cd "$COMPOSE_DIR" && docker compose down -v) 2>&1 | sink
  fi
  log "Starting containers"
  (cd "$COMPOSE_DIR" && docker compose up -d) 2>&1 | sink
  docker ps --format '  {{.Names}}\t{{.Status}}' | tee -a "$LOG_FILE"
  wait_for_portal
fi

# --- part 1b: deploys -------------------------------------------------------

matched=0
for entry in "${COMPONENTS[@]}"; do
  IFS='|' read -r name relpath javahome kind <<<"$entry"
  [[ -n "$ONLY" && "$ONLY" != "$name" ]] && continue
  matched=1
  case "$name" in
    rest-impl)        deploy_rest_impl "$relpath" "$javahome" "$kind" ;;
    site-initializer) deploy_site_initializer "$relpath" "$javahome" "$kind" ;;
    analytics)        deploy_analytics "$relpath" "$javahome" "$kind" ;;
    *)                deploy_component "$name" "$relpath" "$javahome" "$kind" ;;
  esac
done
[[ -n "$ONLY" && $matched -eq 0 ]] && die "unknown component '$ONLY' (see --list)"

# --- done -------------------------------------------------------------------

# --- schema check -------------------------------------------------------
# The two columns the builds-metrics query needs. Both must be on the BASE
# buildsummary table: fields and relationships declared when the site is first
# initialized land there, anything added later lands in the _x extension table
# where the hardcoded SQL cannot see it. If either is missing or in _x, the
# Testray build list renders empty and the frontend throws
# "Cannot destructure property 'items' of 'undefined'".
log "Object check"
for obj in routines buildsummaries productversions triageresults; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$BASE_URL/o/c/$obj" || true)
  if [[ "$code" == "404" ]]; then
    info "WARNING: /o/c/$obj is 404 — the extension defining it did not initialize"
  else
    info "/o/c/$obj -> HTTP $code"
  fi
done

log "Schema check (Testray build list prerequisites)"
if docker exec testray-postgres psql -U root -d lportal -t -c \
     "select table_name||'.'||column_name from information_schema.columns
      where column_name in ('caseresulttotal_','r_buildtobuildsummary_c_buildid');" \
     2>/dev/null | sed 's/^ *//' | grep -v '^$' | tee -a "$LOG_FILE" | grep -q '_x\.'; then
  info "WARNING: a required column is in the _x extension table — the build"
  info "         list will be empty. It needs a --fresh run so the site is"
  info "         initialized with both already declared."
fi

log "Startup complete"
cat <<EOF | tee -a "$LOG_FILE"

    Testray:  $BASE_URL
    Log:      $LOG_FILE

    Client extensions are picked up asynchronously — give the container a
    minute, then confirm the objects are live:

      curl -s -o /dev/null -w '%{http_code}\\n' $BASE_URL/o/c/triageresults

    Next, before the data load (part 2):
      1. Create the OAuth2 client-credentials app in
         Control Panel > Security > OAuth2 Administration
      2. Grant it the object scopes it needs. Only the plain
         c_<object>.everything form resolves — .read / .write leaves are
         accepted and then silently dropped, surfacing as a 403 at call time.
      3. Put the credentials in testray-analytics/config/config.yml as
         local_client_id / local_client_secret, alongside
         prod_client_id / prod_client_secret.
EOF
