#!/usr/bin/env bash
# Sheepod library
#
# Dependencies: podman, python3

cleanup() {
  # Cleanup all unused images
  podman image prune --all --force
}

config-load() {
  local config_file=$1

  if [ ! -r "$config_file" ]; then
    # If specified a config file other than the default one, we validate if it exists, otherwise we
    # silently ignore it
    if [ "$config_file" != "/etc/sheepod/env" ]; then
      echo "File '$env_file' not readable!" >&2
      exit 1
    fi
  else
    _load-env-file "$config_file"
  fi

  # Set defaults
  SHEEPOD_HOST_VOLUME_BASEPATH=${SHEEPOD_HOST_VOLUME_BASEPATH:-"/srv"}
  SHEEPOD_SYSTEMD_PREFIX=${SHEEPOD_SYSTEMD_PREFIX:-"ct"}
  SHEEPOD_SYSTEMD_UNITS_PATH=${SHEEPOD_SYSTEMD_UNITS_PATH:-"/etc/systemd/system"}
  SHEEPOD_SYSTEMD_DEPENDENCIES=${SHEEPOD_SYSTEMD_DEPENDENCIES:-""}
}

# Load environment variables, but don't overwrite variables already set so we can override variables
# via container vars
_load-env-file() {
  local env_file=$1

  # Save a reference of all existing variables
  while IFS= read -rd $'\0' var; do
    name="${var%%=*}"
    [[ "$name" =~ ^[a-zA-Z0-9]+$ ]] || continue
    local "existing_${name}"=1
  done < <(printenv -0)

  while IFS= read -rd $'\0' var; do
    # These vars are set by default on Bash even on an empty environment, so we skip them
    [[ "$var" =~ ^(SHLVL|PWD|_)= ]] && continue

    # We don't override the environment variables already set
    local varname="existing_${var%%=*}"
    test -n "${!varname}" && continue

    # Then parse each value and export them individually
    local value="${var#*=}"
    eval "$(printf "export %s=%q" "$name" "$value")"
  done < <(env -i bash -c "set -a; source \"$env_file\"; printenv -0")
}

run-mysql-query() {
  local ns=$1; shift
  local container=$1; shift

  local fullname="$ns-$container"
  local root_password; root_password=$(get-secret "$ns" "${container}-root-password"); status=$?

  if [ $status -ne 0 ]; then
    echo "Failure to run SQL query!"
    return 1
  fi

  podman exec -i "$fullname" mysql --user=root --password="$root_password" "$@"
}

create-mysql-user() {
  local ns=$1
  local db_container=$2
  local username=$3

  local existing
  existing=$(echo "SELECT 'yes' FROM mysql.user WHERE CONCAT(User, '@', Host) = '${username}@%'" | \
    run-mysql-query "$ns" "$db_container" --skip-column-names); status=$?

  if [ $status -ne 0 ]; then
    echo "Failure to create MySQL user '$username' on [$ns] $db_container"
    return 1
  fi

  if [ "$existing" = "yes" ]; then
    return
  fi

  local password_key="${db_container}-${username}-password"
  create-secret "$ns" "$password_key" --random
  local password; password=$(get-secret "$ns" "$password_key")
  echo "CREATE USER '$username'@'%' IDENTIFIED BY '$password';" | \
    run-mysql-query "$ns" "$db_container" >/dev/null || return 1

  echo "Created MySQL user '$username' on [$ns] $db_container"
}

create-mysql-schema() {
  local ns=$1
  local db_container=$2
  local schema=$3
  local username=$4

  local password_key="${db_container}-${username}-password"
  local password; password=$(get-secret "$ns" "$password_key"); status=$?

  if [ $status -ne 0 ]; then
    echo "Failure to create MySQL schema '$schema' on [$ns] $db_container"
    return 1
  fi

  echo "CREATE DATABASE IF NOT EXISTS \`$schema\`;
  GRANT ALL ON \`${schema//_/\\_}\`.* TO '$username'@'%';" | \
    run-mysql-query "$ns" "$db_container" >/dev/null

  echo "Created MySQL schema '$schema' on [$ns] $db_container"
}

rm-container() {
  readonly SHEEPOD_SYSTEMD_UNITS_PATH SHEEPOD_SYSTEMD_PREFIX
  local namespace=$1
  local name=$2

  local fullname="$namespace-$name"
  local systemd_name="${SHEEPOD_SYSTEMD_PREFIX}-${fullname}"

  systemctl disable --now "$systemd_name" 2> /dev/null || true
  podman rm --force --ignore --volumes "$fullname"
  rm -f "${SHEEPOD_SYSTEMD_UNITS_PATH}/${SHEEPOD_SYSTEMD_PREFIX}-${fullname}.service"
  systemctl daemon-reload
}

wait-container-healthy() {
  local ns=$1
  local name=$2
  local fullname="$ns-$name"
  readonly SHEEPOD_SYSTEMD_UNITS_PATH SHEEPOD_SYSTEMD_PREFIX

  # Check if this is a valid systemd service
  local systemd_name="${SHEEPOD_SYSTEMD_PREFIX}-${fullname}"
  if [ ! -f "${SHEEPOD_SYSTEMD_UNITS_PATH}/${systemd_name}.service" ]; then
    echo "Container is not a systemd service: [$ns] $name" >&2
    return 1
  fi

  if ! podman container exists "$fullname"; then
    echo "Container is not up: [$ns] $name" >&2
    return 1
  fi

  local status health

  echo -n "Waiting for container: [$ns] $name" >&2
  while true; do
    health=$(podman container inspect --format "{{.State.Health.Status}}" "$fullname" 2>/dev/null)
    status=$?

    if [ $status -ne 0 ]; then
      echo >&2
      echo "Container '$fullname' not found!" >&2
      return 1
    fi

    if [ "$health" = "healthy" ]; then
      echo >&2
      echo "Container ready: [$ns] $name" >&2
      return 0
    fi

    sleep 1
    echo -n "." >&2
  done
}

container-exit-code() {
  local ns=$1
  local name=$2
  local fullname="$ns-$name" i=0
  readonly SHEEPOD_SYSTEMD_UNITS_PATH SHEEPOD_SYSTEMD_PREFIX

  # Check if this is a valid systemd service
  local systemd_name="${SHEEPOD_SYSTEMD_PREFIX}-${fullname}"
  if [ ! -f "${SHEEPOD_SYSTEMD_UNITS_PATH}/${systemd_name}.service" ]; then
    echo "Container is not a systemd service: [$ns] $name" >&2
    return 1
  fi

  local unit_status; unit_status=$(systemctl show "$systemd_name" --property=ActiveState --value 2>/dev/null)
  if [ "$unit_status" != "active" ]; then
    systemctl show "$systemd_name" --property=ExecMainStatus --value
    return 0
  fi

  echo -n "Waiting for container to exit: [$ns] $name" >&2
  for (( i=0; i<10; i++ )); do
    if [ "$(systemctl show "$systemd_name" --property=ActiveState --value 2>/dev/null)" != "active" ]; then
      systemctl show "$systemd_name" --property=ExecMainStatus --value
      echo >&2 && return 0
    fi

    sleep 1
    echo -n "." >&2
  done

  echo >&2
  echo "Container did not exit in time" >&2
  return 1
}

container-logs() {
  local ns=$1; shift
  local name=$1; shift
  local follow=$1

  local options=()
  if [ "$follow" = "--follow" ] || [ "$follow" = "-f" ]; then
    local options=("-f")
  fi

  readonly SHEEPOD_SYSTEMD_PREFIX
  local fullname="$ns-$name"

  if ! podman container exists "$fullname"; then
    echo "Container is not up: [$ns] $name" >&2
    return
  fi

  podman logs "${options[@]}" "${fullname}" 2>&1
}

create-container() {
  readonly SHEEPOD_SYSTEMD_UNITS_PATH SHEEPOD_SYSTEMD_PREFIX SHEEPOD_SYSTEMD_DEPENDENCIES
  local namespace=$1; shift
  local name=$1; shift

  local health_cmd="" health_interval="30s" health_timeout="1s" health_retries="3"
  local health_start_period="0s" health_on_failure="kill"
  local user="" env_file="" systemd_restart_policy="" secrets=() volumes=()
  local options=() args=()
  local _parse_args=0
  while [ $# -gt 0 ]; do
    # At this point on we're only supposed to parse the arguments, not options
    if [ $_parse_args -eq 1 ]; then
      args+=("$1")
      shift
      continue
    fi

    case $1 in
      # Encapsulate the following health check arguments so we have access to their values:
      # --health-cmd, --health-interval, --health-start-period, --health-timeout, --health-retries
      # --health-on-failure
      --health-cmd=*)
        health_cmd="${1#*=}"
        shift
        ;;
      --health-cmd)
        test -z "${2:-}" && echo "No value for $1" && return 1
        health_cmd="$2"
        shift; shift
        ;;

      --health-interval=*)
        health_interval="${1#*=}"
        shift
        ;;
      --health-interval)
        test -z "${2:-}" && echo "No value for $1" && return 1
        health_interval="$2"
        shift; shift
        ;;

      --health-start-period=*)
        health_start_period="${1#*=}"
        shift
        ;;
      --health-start-period)
        test -z "${2:-}" && echo "No value for $1" && return 1
        health_start_period="$2"
        shift; shift
        ;;

      --health-timeout=*)
        health_timeout="${1#*=}"
        shift
        ;;
      --health-timeout)
        test -z "${2:-}" && echo "No value for $1" && return 1
        health_timeout="$2"
        shift; shift
        ;;

      --health-retries=*)
        health_retries="${1#*=}"
        shift
        ;;
      --health-retries)
        test -z "${2:-}" && echo "No value for $1" && return 1
        health_retries="$2"
        shift; shift
        ;;

      --health-on-failure=*)
        health_on_failure="${1#*=}"
        shift
        ;;
      --health-on-failure)
        test -z "${2:-}" && echo "No value for $1" && return 1
        health_on_failure="$2"
        shift; shift
        ;;

      --user=*)
        user="${1#*=}"
        shift
        ;;
      --user)
        test -z "${2:-}" && echo "No value for $1" && return 1
        user="$2"
        shift; shift
        ;;

      --env-file=*)
        env_file="${1#*=}"
        shift
        ;;
      --env-file)
        test -z "${2:-}" && echo "No value for $1" && return 1
        env_file="$2"
        shift; shift
        ;;

      --systemd-restart-policy=*)
        systemd_restart_policy="${1#*=}"
        shift
        ;;
      --systemd-restart-policy)
        test -z "${2:-}" && echo "No value for $1" && return 1
        systemd_restart_policy="$2"
        shift; shift
        ;;

      --secret=*)
        secrets+=("${1#*=}")
        shift
        ;;
      --secret)
        test -z "${2:-}" && echo "No value for $1" && return 1
        secrets+=("$2")
        shift; shift
        ;;

      --volume=*)
        volumes+=("${1#*=}")
        shift
        ;;
      --volume)
        test -z "${2:-}" && echo "No value for $1" && return 1
        volumes+=("$2")
        shift; shift
        ;;

      --)
        _parse_args=1
        shift
        ;;
      -*)
        options+=("$1" "$2")
        shift; shift
        ;;
      *)
        _parse_args=1
        args+=("$1")
        shift
        ;;
    esac
  done

  local fullname="$namespace-$name"
  local systemd_name="${SHEEPOD_SYSTEMD_PREFIX}-${fullname}"

  systemctl stop "$systemd_name" 2> /dev/null || true
  podman rm --force --ignore --volumes "$fullname" 2>/dev/null

  # These options can only be set if --health-cmd is also set
  if [ -n "$health_cmd" ]; then
    options+=(--health-cmd "$health_cmd" --health-startup-cmd "$health_cmd")
    [ -n "$health_interval" ] && options+=(--health-interval "$health_interval")
    [ -n "$health_start_period" ] && options+=(--health-start-period "$health_start_period")
    [ -n "$health_timeout" ] && options+=(--health-timeout "$health_timeout")
    [ -n "$health_retries" ] && options+=(--health-retries "$health_retries")
    [ -n "$health_on_failure" ] && options+=(--health-on-failure "$health_on_failure")

    # Startup healthchecks are more frequent than regular healthchecks
    if [ -n "$health_start_period" ]; then
      options+=(--health-startup-interval 2s --health-startup-timeout 1s)
    fi
  fi

  if [ -n "$user" ]; then
    # Parse user with id-mapping embedded
    # --user mysql:idmap=10000,size=1000 \
    if [[ $user == *":"* ]]; then
      local username mappings mappings_array mapdef key value idmapping_start size=1999

      username=${user%:*}
      mappings=${user#*:}
      IFS=, read -ra mappings_array <<< "$mappings"

      for mapdef in "${mappings_array[@]}"; do
        key=${mapdef%=*}
        value=${mapdef#*=}

        case "$key" in
          idmap) idmapping_start="$value" ;;
          size) size="$value" ;;
        esac
      done

      if [ -z "$idmapping_start" ] || [ -z "$size" ]; then
        echo "When passing a mapping along with --user, you need to specify both 'idmap' and 'size'!"
        echo "Example: --user mysql:idmap=10000,size=1000"
        return 1
      fi

      options+=(--userns "auto:gidmapping=0:${idmapping_start}:${size},uidmapping=0:${idmapping_start}:${size}")
      user="$username"
    fi

    options+=(--user "$user")
  fi

  if [ "${#volumes[@]}" -gt 0 ]; then
    readonly SHEEPOD_HOST_VOLUME_BASEPATH
    local chown_perms

    # When declaring volumes and we have both an user and id-mapping set, then we try to change the
    # ownership of that folder to that user on the host volume. This is somewhat similar to what the
    # volume flag `U` does, but instead of doing it recursively, we do it only at the root volume so
    # that you wouldn't need to `chown` that path on the host before creating the container. That
    # is, however, not a replacement for the `U` volume mount option, as it won't change ownership
    # in files recursively, it's rather a convenience for data directories.
    if [ -n "$user" ] && [ -n "$idmapping_start" ]; then
      local oci_image full_id_cmd
      oci_image="${args[0]}"
      full_id_cmd="echo \$(( \$(id -u) + $idmapping_start )):\$(( \$(id -g) + $idmapping_start ))"
      # TODO: Not all images have shell installed -- fix this
      chown_perms=$(podman run --rm --quiet --entrypoint '["/bin/sh","-c"]' --user "$user" \
        "$oci_image" "$full_id_cmd")
    fi

    local volume
    for volume in "${volumes[@]}"; do
      local host_volume="${volume%%:*}"

      # Add a %path% syntax sugar that will automatically set the host path to a well-known path
      # For instance: the container named `database` on namespace `prd.onlyoffice` and a volume
      #               mapping `%data%/lib` is translated to `/srv/data/prd.onlyoffice/database/lib`
      if [[ "$host_volume" =~ ^%(.*)%(.*) ]]; then
        host_volume="$(get-host-volume "$namespace" "$name" "${BASH_REMATCH[1]}")${BASH_REMATCH[2]}"
        volume="${host_volume}:${volume#*:}"
      fi

      # It's only considered a host volume if it starts with a slash (absolute path), otherwise
      # that's a named volume and we don't touch them in that case
      if [[ "$host_volume" =~ ^/ ]]; then
        # When the mounted volume doesn't exist, we attempt to create a folder. If the intention is
        # for it to be a file, then it must exist beforehand.
        if [ ! -e "$host_volume" ]; then
          mkdir -p "$host_volume" || return 1
        fi

        if [ -n "$chown_perms" ]; then
          chown "$chown_perms" "$host_volume" || return 1
        fi
      fi

      options+=(--volume "$volume")
    done
  fi

  # Apply namespace to secrets
  if [ "${#secrets[@]}" -gt 0 ]; then
    local secret
    for secret in "${secrets[@]}"; do
      options+=(--secret "$namespace-$secret")
    done
  fi

  # Adds a `import` syntax so that we read a file from a --env-file at container build time and
  # convert them into several --env parameters instead.
  if [ -n "$env_file" ]; then
    # Checks if the string $env_file is prepended by `import:`
    if [[ "$env_file" =~ ^import:(.*) ]]; then
      env_file="${BASH_REMATCH[1]}"

      if [ ! -r "$env_file" ]; then
        echo "File argument passed to --env-file is not readable!"
        return 1
      fi

	    while IFS= read -rd $'\0' var; do
		    [[ "$var" =~ ^(SHLVL|PWD|_)= ]] || options+=(--env "$var")
	    done < <(env -i bash -c "set -a; source $env_file; printenv -0")
	  else
	    options+=(--env-file "$env_file")
	  fi
  fi

  # By default we always add the container to its namespace network (and create it if needed)
  options+=(--network "$namespace")
  create-network "$namespace" >/dev/null || return 1

  podman create --name "$fullname" "${options[@]}" "${args[@]}" >/dev/null || return 1

  local generate_args=(--new --name --container-prefix="$SHEEPOD_SYSTEMD_PREFIX")
  test -n "$systemd_restart_policy" && generate_args+=(--restart-policy="$systemd_restart_policy")
  if [ -n "$SHEEPOD_SYSTEMD_DEPENDENCIES" ]; then
    generate_args+=(
      --requires="$SHEEPOD_SYSTEMD_DEPENDENCIES"
      --after="$SHEEPOD_SYSTEMD_DEPENDENCIES"
    )
  fi

  output_file="$SHEEPOD_SYSTEMD_UNITS_PATH/$SHEEPOD_SYSTEMD_PREFIX-$fullname.service"
  podman generate systemd "${generate_args[@]}" "$fullname" > "$output_file"

  podman rm --volumes "$fullname" >/dev/null

  systemctl daemon-reload && systemctl enable --now "$systemd_name" 2>/dev/null
  echo "Container created: [$namespace] $name (systemd: $systemd_name)"
}

registry-auth() {
  local url=$1
  local credential_ns=$2
  local username_secret=$3
  local password_secret=$4

  local username; username="$(get-secret "$credential_ns" "$username_secret")"
  get-secret "$credential_ns" "$password_secret" | \
    podman login --username "$username" --password-stdin "$url"
}

create-network() {
  local network=$1
  if podman network exists "$network"; then
    echo "Network exists: $network"
    return
  fi

  podman network create "$network" >/dev/null || true
  echo "Network created: $network"
}

# (TODO: where to put all this?)
# urlencode() {
#   local LC_ALL=C
#   local string="${*:-$(cat -)}"
#   local length="${#string}"
#   local char i
#
#   for (( i = 0; i < length; i++ )); do
#     char="${string:i:1}"
#     if [[ "$char" == [a-zA-Z0-9.~_-] ]]; then
#       echo -n "$char"
#     else
#       printf '%%%02X' "'$char"
#     fi
#   done
#   printf '\n'
# }
#
# urldecode() {
#   local input="${*:-$(cat -)}"
#   local encoded="${input//+/ }"
#   printf '%b\n' "${encoded//%/\\x}"
# }
#
# trim() {
#   xargs
# }

create-secret() {
  local ns=$1; shift
  local name=$1; shift
  local options=("$@")

  local fullname="$ns-$name"
  local RANDOM=no UPDATE=no TRIM=no

  if [ "${#options[@]}" -gt 0 ]; then
    local option
    for option in "${options[@]}"; do
      case "$option" in
        --random) RANDOM=yes ;;
        --update) UPDATE=yes ;;
        --trim) TRIM=yes ;;
        *) echo "Option not recognized: $option" >&2 && return 1 ;;
      esac
    done
  fi

  local passwd
  if [ "$RANDOM" = "yes" ]; then
    passwd="$(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w 16 | head -n 1 || true)"
  else
    IFS='' read -d '' -r passwd
  fi

  if [ "$TRIM" = "yes" ]; then
    passwd="$(printf "%s" "$passwd" | sed -z '$ s/[[:space:]]*$//')"
  fi

  printf "%s" "$passwd" | podman secret create "$fullname" - &>/dev/null; status=$?

  if [ $status -ne 0 ]; then
    # podman-secret-exists is only available on 4.5.0
    if ! podman secret inspect "$fullname" >/dev/null 2>&1; then
      echo "Failure to create secret: [$ns] $name"
      return 1
    fi

    if [ "$UPDATE" = "yes" ]; then
      podman secret rm "$fullname" >/dev/null
      printf "%s" "$passwd" | podman secret create "$fullname" - &>/dev/null
      echo "Secret updated: [$ns] $name"
    else
      echo "Secret exists: [$ns] $name"
    fi
  else
    echo "Secret created: [$ns] $name"
  fi

  return 0
}

update-secret() {
  local ns=$1
  local name=$2
  local option=$3

  local fullname="$ns-$name"

  # podman-secret-exists is only available on 4.5.0
  if ! podman secret inspect "$fullname" >/dev/null 2>&1; then
    echo "Secret '$fullname' not found!" >&2
    return 1
  fi

  podman secret rm "$fullname" >/dev/null
  create-secret "$ns" "$name" "$option"
}

get-secret() {
  # TODO: Ensure we fail if a driver other than file is used
  local ns=$1
  local name=$2

  local fullname="${ns}-${name}"
  local secrets_path; secrets_path="$(dirname \
    "$(podman secret inspect -f "{{.Spec.Driver.Options.path}}" "$fullname")" \
  )"
  local keyid; keyid=$(_jq "['nameToID']['$fullname']" "${secrets_path}/secrets.json"); status=$?

  if [ $status -ne 0 ]; then
    echo "Secret '$fullname' not found!" >&2
    return 1
  fi

  _jq "['$keyid']" "${secrets_path}/filedriver/secretsdata.json" | base64 -d
}

_jq() {
  # This emulates partially `jq` and it's only being used here until `podman secrets` gets more
  # features such as reading secrets
  # See: https://github.com/containers/podman/issues/18667
  local query=$1
  local filename=$2
  local value; value=$(python3 -c "import json, sys; print(json.loads(sys.stdin.read())${query})" \
    2>&1 < "$filename"); status=$?

  test $status -ne 0 && return 1
  echo -n "$value"
}

require-secrets() {
  local ns=$1; shift

  local secret
  for secret in "$@"; do
    local fullname="$ns-$secret"

    # podman-secret-exists is only available on 4.5.0
    if ! podman secret inspect "$fullname" >/dev/null 2>&1; then
      echo "Secret '$fullname' not found!" >&2
      return 1
    fi
  done
}

require-envs() {
	local envs=("$@") failed=no env=""
	for env in "${envs[@]}"; do
		if test -z "${!env}"; then
			echo "The environment variable '$env' is required!" >&2
			failed=yes
		fi
	done

	[ $failed = "no" ]
}

# Gets the corresponding host volume path for the %path% syntax sugar when creating containers.
# The %path% syntax sugar will be replaced by a well-known path on the host based on the variable
# $SHEEPOD_HOST_VOLUME_BASEPATH (typically /srv).
#
# For instance: the container named `database` on namespace `prd.onlyoffice` and a volume mapping to
#               `%data%` is translated to `/srv/data/prd.onlyoffice/database`
get-host-volume() {
  local ns=$1; shift
  local name=$1; shift
  local volume=$1

  echo "${SHEEPOD_HOST_VOLUME_BASEPATH}/${volume}/${ns}/${name}"
}

get-container-ip() {
  local ns=$1
  local name=$2

  local fullname="$ns-$name"

  if ! podman container exists "$fullname"; then
    echo "Container is not up: [$ns] $name" >&2
    return 1
  fi

  local ips
  IFS=, read -ra ips <<< \
    "$(podman inspect -f "{{range.NetworkSettings.Networks}}{{.IPAddress}},{{end}}" "$fullname")"

  echo "${ips[0]}"
}

exec-on-container() {
  local ns=$1; shift
  local name=$1; shift
  local fullname="$ns-$name"

  podman exec "$fullname" "$@"
}

restart-container() {
  local ns=$1
  local name=$2
  local fullname="$ns-$name"

  podman restart "$fullname" >/dev/null
  echo "Container restarted: [$ns] $name"
}
