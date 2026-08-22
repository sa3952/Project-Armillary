#!/usr/bin/env bash
set -euo pipefail

root="."
verifier_args=()
while (($#)); do
  case "$1" in
    --root)
      [[ $# -ge 2 ]] || {
        echo "PUBLICATION CANDIDATE FAILED: --root requires a value" >&2
        exit 2
      }
      root="$2"
      shift 2
      ;;
    *)
      verifier_args+=("$1")
      shift
      ;;
  esac
done

root="$(cd -- "$root" && pwd -P)"
cache_parent="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
cache_root="$(mktemp -d "$cache_parent/project-armillary-pycache.XXXXXX")"
cleanup() {
  chmod -R u+w -- "$cache_root" 2>/dev/null || true
  rm -rf -- "$cache_root"
}
trap cleanup EXIT

# These controls must exist before Python imports the scripts package.  Setting
# them inside verify_publication_candidate.py is too late to protect the
# module's own startup path.
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="$cache_root"

cd -- "$root"
if ((${#verifier_args[@]})); then
  "${PYTHON:-python3}" -B -m scripts.publication.verify_publication_candidate \
    --root . "${verifier_args[@]}"
else
  "${PYTHON:-python3}" -B -m scripts.publication.verify_publication_candidate \
    --root .
fi

pollution="$({
  find . -path './.git' -prune -o \
    \( -type d -name '__pycache__' -o -type f \( -name '*.pyc' -o -name '*.pyo' \) \) \
    -print -quit
} || true)"
if [[ -n "$pollution" ]]; then
  echo "PUBLICATION CANDIDATE FAILED: verifier polluted source: $pollution" >&2
  exit 1
fi
