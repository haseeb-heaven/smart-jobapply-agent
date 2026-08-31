#!/usr/bin/env bash
# lib/sandbox.sh — disposable boundaries for agents and validation commands
set -euo pipefail

SANDBOX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

copy_allowed_env() {
  local names="$1" name value
  for name in $names; do
    if [[ "$name" =~ ^[A-Z][A-Z0-9_]*$ && -n "${!name+x}" ]]; then
      value="${!name}"
      printf '%s=%s\0' "$name" "$value"
    fi
  done
}

agent_provider_env() {
  # Provider secrets must never enter an agent-controlled subprocess.
  printf '%s' ""
}

agent_provider_hosts() {
  case "$1" in
    pi) printf '%s' "${PI_PROVIDER_HOSTS:-api.anthropic.com}" ;;
    claude) printf '%s' "${CLAUDE_PROVIDER_HOSTS:-api.anthropic.com}" ;;
    codex) printf '%s' "${CODEX_PROVIDER_HOSTS:-api.openai.com}" ;;
    opencode) printf '%s' "${OPENCODE_PROVIDER_HOSTS:-api.openai.com}" ;;
    *) return 1 ;;
  esac
}

sandbox_exec_works() {
  command -v sandbox-exec >/dev/null 2>&1 || return 1
  sandbox-exec -p '(version 1) (deny default) (allow process*)' true >/dev/null 2>&1
}

write_macos_profile() {
  local profile="$1" worktree="$2" agent_home="$3" temp_dir="$4" allow_network="$5" provider_hosts="$6" executable="${7:-}"
  cat > "$profile" <<PROFILE
(version 1)
(deny default)
(allow process*)
(allow file-read* (subpath "/usr") (subpath "/bin") (subpath "/sbin") (subpath "/System") (subpath "/Library"))
(allow file-read* (subpath "$worktree"))
(allow file-read* (subpath "$agent_home"))
(allow file-read* (subpath "$temp_dir"))
(allow file-read* (subpath "$executable"))
(allow file-write* (subpath "$worktree"))
(allow file-write* (subpath "$agent_home"))
(allow file-write* (subpath "$temp_dir"))
(deny file-write* (subpath "$worktree/.git"))
(deny file-write* (subpath "$worktree/.git/config"))
(deny file-write* (subpath "$worktree/.git/refs"))
(deny file-write* (subpath "$worktree/.git/hooks"))
PROFILE
  if [[ "$allow_network" == "true" ]]; then
    local host
    for host in $provider_hosts; do
      [[ "$host" =~ ^[A-Za-z0-9.-]+$ ]] || { echo "ERROR: invalid provider host: $host" >&2; return 1; }
      printf '(allow network-outbound (remote tcp "%s:443"))\n' "$host" >> "$profile"
    done
  fi
}

_run_sandboxed_impl() {
  local mode="$1" worktree="$2" home_dir="$3" temp_dir="$4" allow_network="$5"
  shift 5
  local command=("$@")
  local provider_hosts="${AGENT_PROVIDER_HOSTS:-}"
  local path_value="${PATH:-/usr/bin:/bin}"
  local -a clean_env=(env -i "PATH=$path_value" "HOME=$home_dir" "PWD=$worktree" "TMPDIR=$temp_dir"
    "GIT_CONFIG_NOSYSTEM=1" "GIT_CONFIG_GLOBAL=/dev/null" "GIT_CONFIG_SYSTEM=/dev/null"
    "GIT_TERMINAL_PROMPT=0" "GIT_SSH_COMMAND=ssh -oIdentityAgent=none -oIdentitiesOnly=yes")
  while IFS= read -r -d '' item; do clean_env+=("$item"); done < <(copy_allowed_env "${AGENT_ENV_ALLOWLIST:-}")

  case "$mode" in
    auto)
      if [[ "$allow_network" == "true" ]]; then
        if sandbox_exec_works; then
          mode=macos
        else
          echo "ERROR: no provider-aware network sandbox is available for the agent" >&2
          mode=none
        fi
      elif sandbox_exec_works; then
        mode=macos
      elif command -v bwrap >/dev/null 2>&1; then
        mode=bwrap
      elif command -v docker >/dev/null 2>&1; then
        mode=docker
      else
        mode=none
      fi
      ;;
  esac

  case "$mode" in
    macos)
      local profile="$temp_dir/sandbox.sb"
      write_macos_profile "$profile" "$worktree" "$home_dir" "$temp_dir" "$allow_network" "$provider_hosts" "${AGENT_EXECUTABLE:-}"
      sandbox-exec -f "$profile" -- "${clean_env[@]}" "${command[@]}"
      ;;
    bwrap)
      [[ "$allow_network" != "true" ]] || { echo "ERROR: bwrap cannot enforce provider-only egress; refusing networked agent" >&2; return 125; }
      local -a args=(--die-with-parent --new-session --unshare-pid --unshare-ipc --unshare-uts
        --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /sbin /sbin
        --ro-bind /lib /lib --proc /proc --dev /dev --tmpfs /tmp
        --bind "$worktree" "$worktree" --bind "$home_dir" "$home_dir" --bind "$temp_dir" "$temp_dir"
        --chdir "$worktree")
      [[ -e "$worktree/.git" ]] && args+=(--ro-bind "$worktree/.git" "$worktree/.git")
      [[ "$allow_network" == "true" ]] || args+=(--unshare-net)
      [[ -d /lib64 ]] && args+=(--ro-bind /lib64 /lib64)
      [[ -f /etc/resolv.conf ]] && args+=(--ro-bind /etc/resolv.conf /etc/resolv.conf)
      "${clean_env[@]}" bwrap "${args[@]}" -- "${command[@]}"
      ;;
    docker)
      local image="${SANDBOX_IMAGE:-}"
      [[ -n "$image" ]] || { echo "ERROR: SANDBOX_IMAGE must name a compatible image containing bash and the requested command" >&2; return 125; }
      local network_arg=--network=none
      [[ "$allow_network" != "true" ]] || { echo "ERROR: Docker cannot enforce provider-only egress; refusing networked agent" >&2; return 125; }
      local -a docker_env=()
      while IFS= read -r -d '' item; do docker_env+=(--env "$item"); done < <(copy_allowed_env "${AGENT_ENV_ALLOWLIST:-}")
      local -a git_mount=()
      [[ -e "$worktree/.git" ]] && git_mount+=(--mount "type=bind,src=$worktree/.git,dst=$worktree/.git,readonly")
      docker run --rm --user "$(id -u):$(id -g)" "$network_arg" "${docker_env[@]}" \
        --read-only --tmpfs /tmp --mount "type=bind,src=$worktree,dst=$worktree" "${git_mount[@]}" \
        --mount "type=bind,src=$home_dir,dst=$home_dir" --mount "type=bind,src=$temp_dir,dst=$temp_dir" \
        -w "$worktree" "$image" bash -c 'exec "$@"' bash "${command[@]}"
      ;;
    none)
      echo "ERROR: no disposable sandbox backend is available" >&2
      return 125
      ;;
    *)
      echo "ERROR: unsupported sandbox mode: $mode" >&2
      return 125
      ;;
  esac
}

# Every backend starts in the disposable PR worktree. The subshell prevents a
# validation or agent invocation from changing the orchestrator's directory.
run_sandboxed() {
  local worktree="$2"
  (cd "$worktree" && _run_sandboxed_impl "$@")
}
