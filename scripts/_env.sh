# Load <repo>/.env without overriding variables already set in the shell.
# Usage: . scripts/_env.sh   (cwd must be the repo root)
load_dotenv() {
  [ -f .env ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%=*}"; value="${line#*=}"
    case "$key" in *[!A-Za-z0-9_]*) continue ;; esac
    if [ -z "${!key+x}" ]; then export "$key=$value"; fi
  done < .env
}
load_dotenv
