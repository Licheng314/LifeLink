#!/usr/bin/env bash
# Run on the Linux central host. The Windows wrapper supplies a committed SHA.
set -Eeuo pipefail
umask 077

die() { printf 'DEPLOY_ERROR %s\n' "$*" >&2; exit 1; }
usage() {
    echo 'Usage: deploy_server.sh --check|--apply --commit SHA --branch codex/NAME --repo /absolute/path [--pip-index HTTPS_URL]' >&2
    exit 2
}

mode='' commit='' branch='' repo='' pip_index='https://pypi.org/simple'
while (($#)); do
    case "$1" in
        --check|--apply) mode="$1"; shift ;;
        --commit|--branch|--repo|--pip-index)
            (($# >= 2)) || usage
            case "$1" in
                --commit) commit="$2" ;;
                --branch) branch="$2" ;;
                --repo) repo="$2" ;;
                --pip-index) pip_index="$2" ;;
            esac
            shift 2 ;;
        *) usage ;;
    esac
done
[[ -n "$mode" && "$commit" =~ ^[0-9a-f]{40}$ ]] || usage
[[ "$branch" =~ ^codex/[a-z0-9][a-z0-9._/-]*$ ]] || usage
[[ "$repo" =~ ^/[A-Za-z0-9._/-]+$ && "$repo" != / ]] || usage
[[ "$pip_index" =~ ^https://[A-Za-z0-9._/-]+$ ]] || usage
[[ -d "$repo/.git" ]] || die 'The repository directory is missing.'
cd "$repo"
[[ "$(git branch --show-current)" == "$branch" ]] || die 'The server is on another branch.'
[[ -z "$(git status --porcelain=v1)" ]] || die 'The server working tree has uncommitted files.'

remote_sha=$(git ls-remote --exit-code origin "refs/heads/$branch" | cut -f1)
[[ "$remote_sha" == "$commit" ]] || die 'The server Git remote does not contain the requested branch tip.'
current_sha=$(git rev-parse HEAD)

container='central-server-central-1'
[[ "$(docker inspect --format '{{.State.Running}}' "$container")" == true ]] || die 'The current central container is not running.'
previous_image=$(docker inspect --format '{{.Image}}' "$container")
volume=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/lifelink"}}{{.Name}}{{end}}{{end}}' "$container")
[[ "$volume" == central-server_lifelink-central-data ]] || die 'Unexpected central data volume.'
volume_path=$(docker volume inspect --format '{{.Mountpoint}}' "$volume")
[[ -d "$volume_path" && "$volume_path" == */_data && "$volume_path" != / ]] || die 'Unsafe data volume path.'

cd "$repo/central-server"
compose=(-f compose.yaml)
public_domain=''
read_setting() {
    local key="$1" result=''
    [[ -f .env ]] || return 0
    while IFS='=' read -r setting value; do
        if [[ "$setting" == "$key" ]]; then result="${value%$'\r'}"; break; fi
    done < .env
    result="${result#\"}"; result="${result%\"}"
    printf '%s' "$result"
}
if [[ "$(docker inspect --format '{{.State.Running}}' central-server-caddy-1 2>/dev/null || true)" == true ]]; then
    [[ -f .env ]] || die 'Caddy is running but the public .env is missing.'
    compose+=(-f compose.public.yaml)
    public_domain=$(read_setting LIFE_LINK_PUBLIC_DOMAIN)
    [[ "$public_domain" =~ ^[A-Za-z0-9.-]+$ ]] || die 'Invalid public domain setting.'
fi
data_port=$(read_setting LIFE_LINK_DOCKER_DATA_PORT)
management_port=$(read_setting LIFE_LINK_DOCKER_MANAGEMENT_PORT)
data_port=${data_port:-8091}
management_port=${management_port:-8092}
[[ "$data_port" =~ ^[0-9]{2,5}$ && "$management_port" =~ ^[0-9]{2,5}$ ]] || die 'Invalid local port setting.'

if [[ "$mode" == --check ]]; then
    printf 'CHECK_OK current=%s target=%s volume=%s public=%s\n' "$current_sha" "$commit" "$volume" "$([[ -n "$public_domain" ]] && echo yes || echo no)"
    exit 0
fi

backup_dir='/opt/lifelink-backups'
install -d -m 700 "$backup_dir"
exec 9>"$backup_dir/deploy.lock"
flock -n 9 || die 'Another central deployment is running.'
volume_kb=$(du -sk "$volume_path" | cut -f1)
free_kb=$(df -Pk "$backup_dir" | awk 'END {print $4}')
((free_kb > volume_kb * 2 + 262144)) || die 'Not enough free space for the data backup.'

printf 'DEPLOY_STEP fetch %s\n' "$commit"
git -C "$repo" fetch --quiet origin "$branch"
[[ "$(git -C "$repo" rev-parse FETCH_HEAD)" == "$commit" ]] || die 'Fetched commit differs from the requested SHA.'
git -C "$repo" merge-base --is-ancestor "$current_sha" "$commit" || die 'The requested commit is not a fast-forward.'
git -C "$repo" merge --ff-only --quiet "$commit"

candidate="lifelink-central:rev-${commit:0:12}"
rollback="lifelink-central:rollback-$(date -u +%Y%m%dT%H%M%SZ)"
printf 'DEPLOY_STEP build %s\n' "$candidate"
docker build --quiet --pull=false --network=host \
    --build-arg "LIFE_LINK_PIP_INDEX_URL=$pip_index" \
    -t "$candidate" -f "$repo/central-server/Dockerfile" "$repo" >/dev/null
docker run --rm --entrypoint python "$candidate" -c \
    "from zoneinfo import ZoneInfo; from PIL import Image; import central.ai_readers; ZoneInfo('Asia/Shanghai')" \
    || die 'The new image failed its import smoke test.'
docker tag "$previous_image" "$rollback"

backup_file="$backup_dir/pre-${commit:0:12}-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar"
cutover_started=0
on_exit() {
    local result=$?
    trap - EXIT
    if ((result != 0 && cutover_started)); then
        echo 'DEPLOY_STEP restore_previous_image' >&2
        if docker tag "$previous_image" lifelink-central:local \
            && docker compose "${compose[@]}" up -d --no-build --no-deps central; then
            local restored=0
            for ((attempt=1; attempt<=30; attempt++)); do
                if [[ "$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || true)" == healthy ]] \
                    && curl --fail --silent --output /dev/null --max-time 5 "http://127.0.0.1:$data_port/v1/health"; then
                    restored=1
                    break
                fi
                sleep 2
            done
            if ((restored)); then
                printf 'DEPLOY_RECOVERY_OK backup=%s rollback=%s\n' "$backup_file" "$rollback" >&2
            else
                printf 'DEPLOY_RECOVERY_FAILED backup=%s rollback=%s\n' "$backup_file" "$rollback" >&2
            fi
        else
            printf 'DEPLOY_RECOVERY_FAILED backup=%s rollback=%s\n' "$backup_file" "$rollback" >&2
        fi
    fi
    exit "$result"
}
trap on_exit EXIT
trap 'exit 130' INT TERM

printf 'DEPLOY_STEP backup %s\n' "$volume"
cutover_started=1
docker compose "${compose[@]}" stop central
tar -C "$volume_path" -cf "$backup_file" .
tar -tf "$backup_file" >/dev/null
[[ -s "$backup_file" ]] || die 'The data backup is empty.'

printf 'DEPLOY_STEP switch %s\n' "$candidate"
docker tag "$candidate" lifelink-central:local
docker compose "${compose[@]}" up -d --no-build --no-deps central
expected_image=$(docker image inspect --format '{{.Id}}' "$candidate")

healthy=0
for ((attempt=1; attempt<=60; attempt++)); do
    state=$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || true)
    if [[ "$state" == healthy ]] \
        && [[ "$(docker inspect --format '{{.Image}}' "$container")" == "$expected_image" ]] \
        && curl --fail --silent --output /dev/null --max-time 5 "http://127.0.0.1:$data_port/v1/health" \
        && curl --fail --silent --output /dev/null --max-time 5 "http://127.0.0.1:$management_port/"; then
        healthy=1
        break
    fi
    [[ "$state" != unhealthy ]] || break
    sleep 2
done
((healthy)) || die 'New central container did not pass local health checks.'
if [[ -n "$public_domain" ]]; then
    curl --fail --silent --output /dev/null --max-time 15 "https://$public_domain/v1/health" \
        || die 'Public HTTPS health check failed.'
fi
cutover_started=0
printf 'DEPLOY_OK commit=%s image=%s backup=%s rollback=%s\n' "$commit" "$candidate" "$backup_file" "$rollback"
