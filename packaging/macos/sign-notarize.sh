#!/usr/bin/env bash
# Codesign + notarize + staple a built AutoApply Next .app bundle. Degrades to "unsigned dev artefact" when certs/secrets are missing so CI never hard-fails on a fork.
set -euo pipefail

# Usage: ./sign-notarize.sh <path-to-AutoApply Next.app>
#
# Env vars consumed:
#   APPLE_ID                        Apple developer account email
#   APPLE_TEAM_ID                   10-char Apple team id
#   APPLE_APP_PASSWORD              app-specific password for notarytool
#   APPLE_DEV_ID_SIGNING_IDENTITY   e.g. "Developer ID Application: ACME Pty Ltd (TEAMID12345)"
#
# Behaviour:
#   If any required var is missing, prints "MISSING_CERTS: <list>" and exits 0.
#   The unsigned .app is still left in place so CI can upload it as a dev
#   build. The output zip will be ${APP_BASENAME%.app}-unsigned.zip in that case.

if [[ $# -lt 1 ]]; then
    echo "ERROR: missing .app path argument" >&2
    echo "usage: $0 /absolute/path/to/AutoApply Next.app" >&2
    exit 2
fi

APP_PATH="$1"

if [[ ! -d "$APP_PATH" ]]; then
    echo "ERROR: bundle not found: $APP_PATH" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTITLEMENTS_PATH="${SCRIPT_DIR}/entitlements.plist"

if [[ ! -f "$ENTITLEMENTS_PATH" ]]; then
    echo "ERROR: entitlements not found at ${ENTITLEMENTS_PATH}" >&2
    exit 2
fi

APP_BASENAME="$(basename "$APP_PATH")"
APP_DIR="$(dirname "$APP_PATH")"
ZIP_NAME_SIGNED="${APP_BASENAME%.app}.zip"
ZIP_NAME_UNSIGNED="${APP_BASENAME%.app}-unsigned.zip"

# ---------------------------------------------------------------------------
# Verify required env vars. Missing any -> ship unsigned and exit 0.
# ---------------------------------------------------------------------------
missing=()
for var in APPLE_ID APPLE_TEAM_ID APPLE_APP_PASSWORD APPLE_DEV_ID_SIGNING_IDENTITY; do
    if [[ -z "${!var:-}" ]]; then
        missing+=("$var")
    fi
done

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "MISSING_CERTS: ${missing[*]}"
    echo "Producing unsigned dev artefact at ${APP_DIR}/${ZIP_NAME_UNSIGNED}"
    (cd "$APP_DIR" && ditto -c -k --keepParent "$APP_BASENAME" "$ZIP_NAME_UNSIGNED")
    echo "Unsigned zip created: ${APP_DIR}/${ZIP_NAME_UNSIGNED}"
    exit 0
fi

# ---------------------------------------------------------------------------
# Codesign
# ---------------------------------------------------------------------------
echo "Codesigning ${APP_PATH} with identity: ${APPLE_DEV_ID_SIGNING_IDENTITY}"
codesign \
    --deep \
    --force \
    --options runtime \
    --entitlements "$ENTITLEMENTS_PATH" \
    --sign "$APPLE_DEV_ID_SIGNING_IDENTITY" \
    --timestamp \
    "$APP_PATH"

echo "Verifying signature"
codesign --verify --deep --strict --verbose=2 "$APP_PATH"

# ---------------------------------------------------------------------------
# Zip for notarization (notarytool only accepts zip / dmg / pkg)
# ---------------------------------------------------------------------------
echo "Creating zip for notarization at ${APP_DIR}/${ZIP_NAME_SIGNED}"
(cd "$APP_DIR" && ditto -c -k --keepParent "$APP_BASENAME" "$ZIP_NAME_SIGNED")

# ---------------------------------------------------------------------------
# Notarize
# ---------------------------------------------------------------------------
echo "Submitting to Apple notary service (this can take several minutes)"
xcrun notarytool submit \
    "${APP_DIR}/${ZIP_NAME_SIGNED}" \
    --apple-id "$APPLE_ID" \
    --team-id "$APPLE_TEAM_ID" \
    --password "$APPLE_APP_PASSWORD" \
    --wait

# ---------------------------------------------------------------------------
# Staple the ticket onto the .app so it works offline
# ---------------------------------------------------------------------------
echo "Stapling notarization ticket"
xcrun stapler staple "$APP_PATH"
xcrun stapler validate "$APP_PATH"

# Re-zip after stapling so the distributed archive includes the ticket.
rm -f "${APP_DIR}/${ZIP_NAME_SIGNED}"
(cd "$APP_DIR" && ditto -c -k --keepParent "$APP_BASENAME" "$ZIP_NAME_SIGNED")

echo "DONE: signed + notarized + stapled at ${APP_PATH}"
echo "Distributable zip: ${APP_DIR}/${ZIP_NAME_SIGNED}"
