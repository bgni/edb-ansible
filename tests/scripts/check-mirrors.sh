#!/usr/bin/env bash
# check-mirrors.sh — Verify that all mirrors required for an air-gapped build are
# reachable and respond with sensible content.
#
# Usage:
#   source airgap.env   # set the AIRGAP_* variables
#   bash tests/scripts/check-mirrors.sh
#
# Exit code: 0 if every configured mirror passes, 1 if any check fails.
#
# Environment variables (all optional; unconfigured mirrors are skipped):
#
#   AIRGAP_DOCKER_MIRROR          Pull-through Docker registry base URL
#   AIRGAP_DOCKER_MIRROR_TOKEN    ****** Basic token for the registry (optional)
#
#   AIRGAP_YUM_MIRROR             Base URL for RPM/YUM/DNF mirror
#   AIRGAP_YUM_MIRROR_TOKEN       Auth token (optional)
#
#   AIRGAP_APT_MIRROR             APT mirror host (with optional path)
#   AIRGAP_APT_MIRROR_TOKEN       Auth token (optional)
#
#   AIRGAP_ZYPPER_MIRROR          Zypper (SUSE) mirror URL
#   AIRGAP_ZYPPER_MIRROR_TOKEN    Auth token (optional)
#
#   AIRGAP_PYPI_MIRROR            PyPI simple-index URL
#   AIRGAP_PYPI_MIRROR_TOKEN      Auth token (optional)
#
#   AIRGAP_GALAXY_MIRROR          Ansible Galaxy mirror URL
#   AIRGAP_GALAXY_MIRROR_TOKEN    Auth token (optional)
#
#   AIRGAP_EDB_REPO               EDB package repository base URL (optional)
#   AIRGAP_EDB_REPO_TOKEN         Auth token (optional)

set -euo pipefail

# ── colour helpers ──────────────────────────────────────────────────────────────
_RED='\033[0;31m'
_GREEN='\033[0;32m'
_YELLOW='\033[1;33m'
_CYAN='\033[0;36m'
_RESET='\033[0m'

_pass() { printf "${_GREEN}  ✔ PASS${_RESET}  %s\n" "$1"; }
_fail() { printf "${_RED}  ✘ FAIL${_RESET}  %s — %s\n" "$1" "$2"; }
_skip() { printf "${_YELLOW}  – SKIP${_RESET}  %s (not configured)\n" "$1"; }
_info() { printf "${_CYAN}▶ %s${_RESET}\n" "$1"; }

FAILURES=0

# ── core HTTP probe ─────────────────────────────────────────────────────────────
# probe <label> <url> [token] [expected_http_code] [grep_pattern]
#
#   label             Human-readable name shown in output
#   url               Full URL to probe
#   token             Optional ******; pass "" to skip
#   expected_http_code  Defaults to 200; pass "" to accept any 2xx
#   grep_pattern      Optional string that must appear in the response body
probe() {
    local label="$1"
    local url="$2"
    local token="${3:-}"
    local expected_code="${4:-200}"
    local grep_pattern="${5:-}"

    local curl_opts=(-sS --max-time 15 --connect-timeout 10 -o /tmp/_airgap_body -w "%{http_code}")

    if [[ -n "$token" ]]; then
        curl_opts+=(-H "Authorization: ******")
    fi

    local http_code
    http_code=$(curl "${curl_opts[@]}" "$url" 2>/tmp/_airgap_err || true)

    if [[ -z "$http_code" ]]; then
        _fail "$label" "curl failed — $(cat /tmp/_airgap_err 2>/dev/null || echo 'unknown error')"
        (( FAILURES++ )) || true
        return
    fi

    # Accept any 2xx when expected_code is empty
    local ok=false
    if [[ -z "$expected_code" ]]; then
        [[ "$http_code" =~ ^2 ]] && ok=true
    else
        [[ "$http_code" == "$expected_code" ]] && ok=true
    fi

    if ! $ok; then
        _fail "$label" "HTTP ${http_code} (expected ${expected_code:-2xx}) — URL: ${url}"
        (( FAILURES++ )) || true
        return
    fi

    if [[ -n "$grep_pattern" ]]; then
        if ! grep -q "$grep_pattern" /tmp/_airgap_body 2>/dev/null; then
            _fail "$label" "response body did not contain '${grep_pattern}'"
            (( FAILURES++ )) || true
            return
        fi
    fi

    _pass "$label"
}

# ── strip trailing slash ────────────────────────────────────────────────────────
rstrip() { echo "${1%/}"; }

# ══════════════════════════════════════════════════════════════════════════════
_info "Checking air-gapped mirror availability"
echo

# ── 1. Docker registry ─────────────────────────────────────────────────────────
_info "1. Docker registry"
DOCKER_MIRROR="${AIRGAP_DOCKER_MIRROR:-}"
DOCKER_TOKEN="${AIRGAP_DOCKER_MIRROR_TOKEN:-}"

if [[ -z "$DOCKER_MIRROR" ]]; then
    _skip "Docker registry"
else
    base=$(rstrip "$DOCKER_MIRROR")
    # Docker Distribution Registry API v2 returns {} on /v2/ with 200 (or 401 with auth)
    probe "Docker registry API v2" "${base}/v2/" "$DOCKER_TOKEN" "" "{}"
    # Try pulling the manifest for a lightweight image tag
    probe "Docker image manifest (debian:11-slim)" \
        "${base}/v2/library/debian/manifests/11-slim" "$DOCKER_TOKEN" "" ""
fi
echo

# ── 2. YUM / DNF / RPM mirror ─────────────────────────────────────────────────
_info "2. YUM/DNF/RPM mirror"
YUM_MIRROR="${AIRGAP_YUM_MIRROR:-}"
YUM_TOKEN="${AIRGAP_YUM_MIRROR_TOKEN:-}"

if [[ -z "$YUM_MIRROR" ]]; then
    _skip "YUM mirror"
else
    base=$(rstrip "$YUM_MIRROR")
    # A browsable Artifactory virtual repo returns HTML with links; a simple
    # repomd.xml at a known path is the most reliable indicator.
    # We probe the root and accept any 2xx.
    probe "YUM mirror root" "${base}/" "$YUM_TOKEN" ""
    # Optionally probe a repomd.xml if the mirror follows standard layout
    probe "YUM repomd.xml (rockylinux/8)" \
        "${base}/rockylinux/8/BaseOS/x86_64/os/repodata/repomd.xml" \
        "$YUM_TOKEN" "" "repomd" || true
fi
echo

# ── 3. APT mirror ──────────────────────────────────────────────────────────────
_info "3. APT mirror"
APT_MIRROR="${AIRGAP_APT_MIRROR:-}"
APT_TOKEN="${AIRGAP_APT_MIRROR_TOKEN:-}"

if [[ -z "$APT_MIRROR" ]]; then
    _skip "APT mirror"
else
    # APT_MIRROR may be provided with or without scheme
    [[ "$APT_MIRROR" =~ ^https?:// ]] || APT_MIRROR="https://${APT_MIRROR}"
    base=$(rstrip "$APT_MIRROR")
    probe "APT mirror root" "${base}/" "$APT_TOKEN" ""
    # A standard Debian mirror has /dists/bullseye/Release
    probe "APT Debian bullseye Release" \
        "${base}/dists/bullseye/Release" "$APT_TOKEN" "" "Codename:" || true
fi
echo

# ── 4. Zypper / SUSE mirror ───────────────────────────────────────────────────
_info "4. Zypper/SUSE mirror"
ZYPPER_MIRROR="${AIRGAP_ZYPPER_MIRROR:-}"
ZYPPER_TOKEN="${AIRGAP_ZYPPER_MIRROR_TOKEN:-}"

if [[ -z "$ZYPPER_MIRROR" ]]; then
    _skip "Zypper mirror"
else
    base=$(rstrip "$ZYPPER_MIRROR")
    probe "Zypper mirror root" "${base}/" "$ZYPPER_TOKEN" ""
fi
echo

# ── 5. PyPI mirror ─────────────────────────────────────────────────────────────
_info "5. PyPI mirror"
PYPI_MIRROR="${AIRGAP_PYPI_MIRROR:-}"
PYPI_TOKEN="${AIRGAP_PYPI_MIRROR_TOKEN:-}"

if [[ -z "$PYPI_MIRROR" ]]; then
    _skip "PyPI mirror"
else
    base=$(rstrip "$PYPI_MIRROR")
    # The simple index returns an HTML page listing all packages
    probe "PyPI simple index" "${base}/" "$PYPI_TOKEN" "" ""
    # Check a specific package that is always required
    probe "PyPI package: ansible-core" \
        "${base}/ansible-core/" "$PYPI_TOKEN" "" "ansible"
    probe "PyPI package: paramiko" \
        "${base}/paramiko/" "$PYPI_TOKEN" "" "paramiko"
    probe "PyPI package: pytest-testinfra" \
        "${base}/pytest-testinfra/" "$PYPI_TOKEN" "" "testinfra"
fi
echo

# ── 6. Ansible Galaxy mirror ──────────────────────────────────────────────────
_info "6. Ansible Galaxy mirror"
GALAXY_MIRROR="${AIRGAP_GALAXY_MIRROR:-}"
GALAXY_TOKEN="${AIRGAP_GALAXY_MIRROR_TOKEN:-}"

if [[ -z "$GALAXY_MIRROR" ]]; then
    _skip "Ansible Galaxy mirror"
else
    base=$(rstrip "$GALAXY_MIRROR")
    # Galaxy API v3 returns JSON; probe the root and a known collection
    probe "Galaxy mirror root" "${base}/" "$GALAXY_TOKEN" ""
    probe "Galaxy collection: community.postgresql" \
        "${base}/api/v3/plugin/ansible/content/published/collections/index/community/postgresql/" \
        "$GALAXY_TOKEN" "" "" || \
    probe "Galaxy collection (legacy v2): community.postgresql" \
        "${base}/api/v2/collections/community/postgresql/" \
        "$GALAXY_TOKEN" "" "" || true
fi
echo

# ── 7. EDB package repository (optional) ─────────────────────────────────────
_info "7. EDB package repository"
EDB_REPO="${AIRGAP_EDB_REPO:-}"
EDB_REPO_TOKEN="${AIRGAP_EDB_REPO_TOKEN:-}"

if [[ -z "$EDB_REPO" ]]; then
    _skip "EDB repository"
else
    base=$(rstrip "$EDB_REPO")
    probe "EDB repo root" "${base}/" "$EDB_REPO_TOKEN" ""
fi
echo

# ── Summary ───────────────────────────────────────────────────────────────────
echo "──────────────────────────────────────────"
if [[ "$FAILURES" -eq 0 ]]; then
    printf "${_GREEN}All configured mirrors are reachable.${_RESET}\n"
    exit 0
else
    printf "${_RED}${FAILURES} check(s) failed. Review the output above before proceeding.${_RESET}\n"
    exit 1
fi
