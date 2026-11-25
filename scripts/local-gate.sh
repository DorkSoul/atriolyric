#!/usr/bin/env bash
# Runs the whole test gate for HEAD on a workstation: the same checks the
# pull-request workflow runs, in containers. See docs/releasing.md.
#
#   scripts/local-gate.sh            run the gate for HEAD
#   scripts/local-gate.sh --full     run every part, whatever changed
#   scripts/local-gate.sh --all-browsers   also run Firefox and WebKit
#                                    (the gate needs Chromium only)
#   scripts/local-gate.sh --plan     say which parts would run, and stop
#
# The gate runs in containers that match what ships, not on the workstation's
# own tools: the backend as root in the Dockerfile's runtime base image with
# that image's ffmpeg, the browser tests in the Playwright image CI uses.
#
# Only the parts a change can affect are run. The changes are the files that
# differ from the last commit this script passed (the local ref
# refs/gate/verified), when that commit is an ancestor of HEAD:
#   server/                      backend lint, types, tests, audit
#   web/ (code, tests, config)   web lint, build, audit, the browser suite
#   web/public/ (not scripts)    web lint, build, audit, Chromium only
#   docs, *.md                   nothing
#   anything else (Dockerfile, workflows, scripts, ...)   every part
# The version number in web/package.json and web/package-lock.json does not
# count as a web change. The API-types check and the real two-device suite
# always run. With no usable verified commit, or with --full, every part runs.
#
# Environment:
#   GATE_WRAP        command prefix for every heavy step, e.g. a wrapper that
#                    limits CPU and memory (default: none)
#   GATE_ENGINE      podman or docker (default: podman if present)
#   GATE_CACHE       directory for caches and test output, on disk
#                    (default: ~/.cache/atriolyric-gate)
#   GATE_BROWSERS    space-separated Playwright projects (default: chromium;
#                    --all-browsers makes it chromium firefox webkit)
#   GATE_PYTEST_WORKERS   parallel backend test processes (default: 6)
set -euo pipefail

full=0
plan=0
all_browsers=0
for arg in "$@"; do
  case $arg in
    --all-browsers) all_browsers=1 ;;
    --full) full=1 ;;
    --plan) plan=1 ;;
    *) echo "Unknown option: $arg (see the top of this script)" >&2; exit 2 ;;
  esac
done

root=$(git rev-parse --show-toplevel)
cd "$root"
if [ -n "$(git status --porcelain)" ]; then
  echo "The working tree has uncommitted changes; the gate checks a commit." >&2
  exit 1
fi
sha=$(git rev-parse HEAD)

engine=${GATE_ENGINE:-$(command -v podman >/dev/null 2>&1 && echo podman || echo docker)}
cache=${GATE_CACHE:-$HOME/.cache/atriolyric-gate}
# Chromium is the gate; Firefox and WebKit are optional extra checks.
if [ "$all_browsers" = 1 ]; then
  default_browsers="chromium firefox webkit"
else
  default_browsers="chromium"
fi
browsers=${GATE_BROWSERS:-$default_browsers}
pytest_workers=${GATE_PYTEST_WORKERS:-6}
# shellcheck disable=SC2206  # GATE_WRAP is a command prefix, split on purpose
wrap=(${GATE_WRAP:-})
mkdir -p "$cache/uv" "$cache/npm" "$cache/out" "$cache/xdg"

# --- What does this commit need? ---------------------------------------------

# A package file whose only difference from the verified commit is the app's
# own version number.
version_only() { # path
  node -e '
    const { execFileSync } = require("child_process")
    const [base, path] = process.argv.slice(1)
    const load = (rev) => {
      const json = JSON.parse(execFileSync("git", ["show", `${rev}:${path}`], { maxBuffer: 1 << 28 }))
      delete json.version
      if (json.packages && json.packages[""]) delete json.packages[""].version
      return JSON.stringify(json)
    }
    process.exit(load(base) === load("HEAD") ? 0 : 1)
  ' "$base" "$1" 2>/dev/null
}

need_backend=1
web=full # none, assets (one browser) or full (every browser)
base=""
if [ "$full" = 0 ] \
  && git rev-parse --quiet --verify refs/gate/verified >/dev/null \
  && git merge-base --is-ancestor refs/gate/verified HEAD; then
  base=$(git rev-parse refs/gate/verified)
  need_backend=0
  web=none
  while IFS= read -r path; do
    case $path in
      server/*) need_backend=1 ;;
      web/package.json | web/package-lock.json) version_only "$path" || web=full ;;
      web/public/*.js) web=full ;;
      web/public/*) [ "$web" = full ] || web=assets ;;
      web/*) web=full ;;
      docs/* | *.md | LICENSE) ;;
      *) need_backend=1; web=full ;;
    esac
  done < <(git diff --name-only --no-renames "$base" HEAD)
fi
[ "$web" = assets ] && browsers=${browsers%% *}

ran=()
[ "$need_backend" = 1 ] && ran+=("backend")
[ "$web" != none ] && ran+=("web in $browsers")
ran+=("API types" "real two-device suite")
summary=$(IFS=,; echo "${ran[*]}" | sed 's/,/, /g')
if [ -n "$base" ]; then
  echo "Changes since the verified commit ${base:0:7} need: $summary."
  scope="changes since ${base:0:7}"
else
  echo "No verified ancestor to compare with (or --full): running every part."
  scope="every part"
fi
[ "$plan" = 0 ] || exit 0

# --- Running it ----------------------------------------------------------------

# The images come from the files that define the release.
base_image=$(awk '/^FROM .* AS runtime$/ {print $2}' Dockerfile)
uv_image=$(grep -o 'ghcr.io/astral-sh/uv:[^ ]*' Dockerfile | head -1)
pw_version=$(node -p "require('./web/package-lock.json').packages['node_modules/playwright'].version")
pw_image="mcr.microsoft.com/playwright:v${pw_version}-noble"

step_name=""
step_done() {
  [ -z "$step_name" ] || printf '    (%dm%02ds)\n' $(((SECONDS - step_start) / 60)) $(((SECONDS - step_start) % 60))
  step_name=""
}
step() {
  step_done
  step_name=$*
  step_start=$SECONDS
  printf '\n==> %s\n' "$*"
}
# A step that needs the network gets three tries, so one reset connection
# does not cost the whole run.
retry_fn='retry() { local n; for n in 1 2 3; do "$@" && return 0; echo "Attempt $n failed: $*" >&2; sleep $((n * 5)); done; return 1; }'
# The script's last act in the container is to leave a marker, and the step
# counts only if the marker is there afterwards. Exit status alone is not
# proof: a GATE_WRAP that does not pass stdin on leaves the container with an
# empty script, which "passes" in a second with no tests run.
in_container() { # step name, image, VAR=value..., then a shell script on stdin
  local name=$1 image=$2 env=() marker
  shift 2
  for pair in "$@"; do env+=(-e "$pair"); done
  marker=".ran-$name-$sha"
  rm -f "$cache/out/$marker"
  { cat; printf '\ntouch "/cache/out/%s"\n' "$marker"; } \
    | "${wrap[@]}" "$engine" run --rm -i --ipc=host "${env[@]}" -e "RETRY_FN=$retry_fn" \
      -v "$root:/repo:ro" -v "$cache:/cache" "$image" bash -euo pipefail -s
  if [ ! -f "$cache/out/$marker" ]; then
    echo "The $name step exited without running to its end: its script did not reach" >&2
    echo "the container, or stopped early. (Does GATE_WRAP pass stdin through?)" >&2
    exit 1
  fi
  rm -f "$cache/out/$marker"
}

# The gate tests the files in this checkout, so it only passes if they are
# still the commit it read at the start.
same_commit() {
  if [ "$(git rev-parse HEAD)" != "$sha" ] || [ -n "$(git status --porcelain)" ]; then
    echo "The checkout changed while the gate was running (it started at $sha)." >&2
    echo "Run the gate again." >&2
    exit 1
  fi
}

if [ "$need_backend" = 1 ]; then
  step "Backend: lint, types, dependency audit and tests in $base_image (as root)"
  uv_bin=$("$engine" create "$uv_image")
  "$engine" cp "$uv_bin:/uv" "$cache/uv-bin"
  "$engine" cp "$uv_bin:/uvx" "$cache/uvx-bin"
  "$engine" rm "$uv_bin" >/dev/null
  in_container backend "$base_image" "PYTEST_WORKERS=$pytest_workers" <<'EOF'
eval "$RETRY_FN"
# XDG_CACHE_HOME: pyright keeps the Node it downloads for itself there, so
# only the first run on a workstation fetches it from nodejs.org.
export DEBIAN_FRONTEND=noninteractive UV_CACHE_DIR=/cache/uv UV_LINK_MODE=copy XDG_CACHE_HOME=/cache/xdg
# APT::Sandbox::User=root: apt's own sandbox user cannot be mapped in a
# rootless nested container; harmless elsewhere.
retry apt-get -o APT::Sandbox::User=root update -qq
# libatomic1: the Node that pyright downloads for itself needs it.
retry apt-get -o APT::Sandbox::User=root install -y -qq --no-install-recommends ffmpeg libatomic1 >/dev/null
install -m 755 /cache/uv-bin /usr/local/bin/uv
install -m 755 /cache/uvx-bin /usr/local/bin/uvx
ffmpeg -version | head -1
mkdir /work && tar -C /repo/server --exclude=.venv --exclude=data --exclude=htmlcov --exclude='.*_cache' -cf - . | tar -C /work -xf -
cd /work
retry uv sync --frozen
# Cheapest first: a lint or type error stops the run before the tests start.
uv run --frozen ruff check .
uv run --frozen pyright
uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /work/requirements.txt
retry uvx pip-audit@2.10.1 -r /work/requirements.txt --require-hashes --disable-pip
uv run --frozen pytest -m "not network" --cov=app -q -p no:cacheprovider -n "$PYTEST_WORKERS"
EOF
fi

if [ "$web" != none ]; then
  step "Web: lint, build, dependency audit and browser tests ($browsers) in $pw_image"
  in_container web "$pw_image" "BROWSERS=$browsers" <<'EOF'
eval "$RETRY_FN"
export CI=true HOME=/root npm_config_cache=/cache/npm
mkdir -p /work/web && tar -C /repo/web --exclude=node_modules --exclude=dist --exclude=test-results --exclude=playwright-report -cf - . | tar -C /work/web -xf -
cd /work/web
retry npm ci --no-audit --no-fund
# Cheapest first: a lint or type error stops the run before the tests start.
npm run lint
npm run build
retry npm run audit:deps
for project in $BROWSERS; do
  npx playwright test --project="$project" --workers=3 --output="/cache/out/$project"
done
EOF
fi

# The last two steps run on the workstation itself and need its dependencies.
# The app's own version number is left out of the hash, so a release that only
# bumps it does not reinstall.
lock_hash=$(node -e '
  const lock = require("./web/package-lock.json")
  delete lock.version
  delete lock.packages[""].version
  process.stdout.write(JSON.stringify(lock))' | sha256sum | cut -d' ' -f1)
if [ "$(cat web/node_modules/.gate-lock-hash 2>/dev/null)" != "$lock_hash" ]; then
  ( cd web && npm ci --no-audit --no-fund )
  echo "$lock_hash" > web/node_modules/.gate-lock-hash
fi
( cd server && uv sync --frozen --quiet )

step "API types are current"
( cd web && npm run gen:api-types >/dev/null && git diff --exit-code -- src/api-types.ts ) || {
  echo "web/src/api-types.ts is stale. Commit the regenerated file." >&2
  exit 1
}

step "Real two-device suite (real backend, nothing mocked)"
( cd web && "${wrap[@]}" npx playwright test -c playwright.real.config.ts --output="$cache/out/real" )
step_done

same_commit
echo
echo "Gate passed for $sha ($scope: $summary)."
git update-ref refs/gate/verified "$sha"
