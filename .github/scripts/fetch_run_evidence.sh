# Sourced by the ios-testflight gate. Downloads the evidence gate_check.py needs for one run
# into $RUNNER_TEMP/<prefix>-{run,workflow,compare,jobs}.json and <prefix>-log.txt.
# Requires: gh (GH_TOKEN), jq, REPO, RUNNER_TEMP. The run id is validated by the caller's
# validate_inputs step and re-checked here.
fetch_run_evidence() {
  local id="$1" prefix="$RUNNER_TEMP/$2" job_name="$3" workflow_id head_sha job_id
  [[ "$id" =~ ^[1-9][0-9]{0,19}$ ]] || { echo "::error::bad run id"; return 1; }
  gh api "repos/${REPO}/actions/runs/${id}" > "${prefix}-run.json"
  workflow_id=$(jq -r '.workflow_id // ""' "${prefix}-run.json")
  [[ "$workflow_id" =~ ^[0-9]+$ ]] || { echo "::error::the run has no workflow id"; return 1; }
  gh api "repos/${REPO}/actions/workflows/${workflow_id}" > "${prefix}-workflow.json"
  head_sha=$(jq -r '.head_sha // ""' "${prefix}-run.json")
  [[ "$head_sha" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::the run has no head sha"; return 1; }
  # base=run's revision, head=current main: status ahead/identical means the revision is on main.
  gh api "repos/${REPO}/compare/${head_sha}...main" --jq '{status: .status}' > "${prefix}-compare.json"
  gh api --paginate "repos/${REPO}/actions/runs/${id}/jobs?per_page=100" --jq '.jobs[]' | jq -s '.' > "${prefix}-jobs.json"
  job_id=$(jq -r --arg n "$job_name" '[.[] | select(.name == $n)] | if length == 1 then .[0].id else empty end' "${prefix}-jobs.json")
  if [[ "$job_id" =~ ^[0-9]+$ ]]; then
    # gh refuses log bodies that contain terminal escape codes (Xcode/Gradle colour output), so fetch with curl.
    curl -sSfL --retry 3 -H "Authorization: Bearer ${GH_TOKEN}" -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/${REPO}/actions/jobs/${job_id}/logs" | tr -d '\r' > "${prefix}-log.txt"
  else
    : > "${prefix}-log.txt"
  fi
}
