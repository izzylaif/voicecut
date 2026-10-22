#!/usr/bin/env bash
# Run a command; when it fails, report the end of its output as an error
# annotation, which anyone can read on the run's page or through the API
# (the full log needs a signed-in user).
#
#   .github/scripts/run.sh "What failed" command [arguments...]
set -uo pipefail

title="$1"
shift
log="$(mktemp)"
"$@" 2>&1 | tee "${log}"
status=${PIPESTATUS[0]}
if [[ ${status} -ne 0 ]]; then
  message="$(tail -c 3500 "${log}" | sed -e 's/%/%25/g' -e 's/\r/%0D/g' | awk '{ printf "%s%%0A", $0 }')"
  echo "::error title=${title} (exit ${status})::${message}"
fi
rm -f "${log}"
exit "${status}"
