<!-- Shopping list of certs, secrets, and accounts the user must provide for signed releases. Map each item to env var / GH secret name and explain what fails without it. -->

# AutoApply Next: code-signing + update-feed checklist

This is the full set of credentials the release pipeline expects. Every item is
optional in the sense that the pipeline will still produce an artefact without
it, but the artefact will be marked `DEV-BUILD` and users will get scary OS
warnings on first launch. Fill these in once and store them in the GitHub
repository's Settings, Secrets and variables, Actions.

Status legend used below:

- `required for signed mac release`
- `required for signed windows release`
- `required for in-app updates`

---

## macOS

You need an Apple Developer Program account (USD 99/year) and a Developer ID
Application certificate. App Store Connect signing is not used.

### 1. Apple ID

- **What it is:** the email address of the Apple developer account.
- **Where to get it:** the email you sign in to <https://developer.apple.com>
  with. Must be enrolled in the Apple Developer Program.
- **GH secret:** `APPLE_ID`
- **Env var (local):** `APPLE_ID`
- **Fails without it:** notarization step in
  `packaging/macos/sign-notarize.sh` prints `MISSING_CERTS: APPLE_ID` and
  exits 0 with an unsigned bundle.
- **Status:** required for signed mac release.

### 2. Apple Team ID

- **What it is:** the 10-character team identifier from your developer
  account, e.g. `ABCDE12345`.
- **Where to get it:** <https://developer.apple.com/account>, top-right
  Membership tile, or run `xcrun altool --list-providers -u <apple_id>`.
- **GH secret:** `APPLE_TEAM_ID`
- **Env var (local):** `APPLE_TEAM_ID`
- **Fails without it:** same as Apple ID. notarytool refuses to submit
  without the team id.
- **Status:** required for signed mac release.

### 3. App-specific password

- **What it is:** a one-off password that lets notarytool authenticate
  without your Apple ID password or 2FA prompt.
- **Where to get it:** <https://appleid.apple.com>, Sign-In and Security,
  App-Specific Passwords, generate one and label it "autoapply-next ci".
- **GH secret:** `APPLE_APP_PASSWORD`
- **Env var (local):** `APPLE_APP_PASSWORD`
- **Fails without it:** notarytool exits with auth error; pipeline falls
  back to producing an unsigned dev artefact.
- **Status:** required for signed mac release.

### 4. Developer ID Application signing identity (the human-readable name)

- **What it is:** the CN of the Developer ID Application certificate, in
  the exact form codesign expects, e.g.
  `Developer ID Application: Sagar Verma (ABCDE12345)`.
- **Where to get it:** after you generate the cert in step 5, run
  `security find-identity -v -p codesigning` and copy the matching line.
- **GH secret:** `APPLE_DEV_ID_SIGNING_IDENTITY`
- **Env var (local):** `APPLE_DEV_ID_SIGNING_IDENTITY`
- **Fails without it:** codesign cannot pick the right key from the
  keychain.
- **Status:** required for signed mac release.

### 5. Developer ID Application certificate (p12 bytes)

- **What it is:** the actual private key + cert, exported from Keychain
  Access as a `.p12` file, then base64-encoded so it fits in a GH secret.
- **Where to get it:**
  1. <https://developer.apple.com/account/resources/certificates>, create a
     new Developer ID Application certificate (CSR generated in Keychain
     Access on your mac).
  2. Download and double-click the resulting `.cer` to install it. Keychain
     Access shows the cert with its private key under "login".
  3. Right-click the cert (not the key) and choose Export, format
     "Personal Information Exchange (.p12)", set a strong password.
  4. base64-encode it: `base64 -i devid.p12 | pbcopy`.
- **GH secret:** `APPLE_DEV_ID_CERT_P12` (paste the base64 string)
- **GH secret:** `APPLE_DEV_ID_CERT_PASSWORD` (the p12 password)
- **Env var (local):** not used; on your local mac the cert lives in
  Keychain Access already.
- **Fails without it:** the GH runner has no signing identity in its
  keychain, so even if `APPLE_DEV_ID_SIGNING_IDENTITY` is set, codesign
  fails with "no identity found". Pipeline degrades to dev artefact.
- **Status:** required for signed mac release.

---

## Windows

You have two options. Pick **one**.

### Option A: local cert by SHA1 thumbprint (cheapest, OV cert ~USD 200/yr)

#### 1. Code-signing certificate (pfx bytes)

- **What it is:** an OV or EV code-signing certificate exported as
  `.pfx`, then base64-encoded.
- **Where to get it:** Sectigo, DigiCert, SSL.com, etc. OV is fine for
  most cases. EV unlocks instant SmartScreen reputation but requires a
  hardware token (use Option B instead).
- **GH secret:** `WIN_SIGNING_CERT_PFX` (base64-encoded bytes)
- **GH secret:** `WIN_SIGNING_CERT_PASSWORD`
- **Env var (local):** not used; install the pfx into
  `CurrentUser\My` once with `Import-PfxCertificate`.
- **Fails without it:** the runner has no cert in its store, so signtool
  cannot find the cert by thumbprint. Pipeline degrades to dev artefact.
- **Status:** required for signed windows release (Option A).

#### 2. Cert SHA1 thumbprint

- **What it is:** the SHA1 thumbprint of the cert above, used by signtool
  to pick the right cert.
- **Where to get it:** after importing the pfx, run
  `Get-ChildItem Cert:\CurrentUser\My | Format-List Subject, Thumbprint`.
  Copy the Thumbprint value (40 hex chars).
- **GH secret:** `WIN_SIGNING_CERT_THUMBPRINT`
- **Env var (local):** `WIN_SIGNING_CERT_THUMBPRINT`
- **Fails without it:** `packaging/windows/sign.ps1` prints
  `MISSING_CERTS: WIN_SIGNING_CERT_THUMBPRINT` and exits 0.
- **Status:** required for signed windows release (Option A).

### Option B: SSL.com eSigner cloud HSM (no hardware token, ~USD 300/yr)

#### 3. eSigner credentials

- **What it is:** SSL.com's cloud HSM where the private key lives.
  signtool talks to it via the eSignerKSP provider.
- **Where to get it:** sign up at <https://www.ssl.com/esigner/>. The
  dashboard gives you a username, password, TOTP secret, and credential id.
  Install the eSigner KSP locally on the runner image (the workflow assumes
  `C:\Program Files\SSL.com\eSignerKSP\eSignerKSP.dll`).
- **GH secrets:**
  - `WIN_SIGNING_USE_ESIGNER` set to literal `"1"` (or a vars value)
  - `ESIGNER_USERNAME`
  - `ESIGNER_PASSWORD`
  - `ESIGNER_TOTP_SECRET`
  - `ESIGNER_CREDENTIAL_ID`
- **Env var (local):** same names.
- **Fails without it:** `sign.ps1` prints `MISSING_CERTS:` listing the
  missing var(s) and exits 0.
- **Status:** required for signed windows release (Option B).

### 4. Timestamp URL (optional)

- **What it is:** an RFC 3161 timestamp authority URL.
- **Where to get it:** defaults to `http://timestamp.digicert.com`,
  no action needed. Override only if your cert vendor requires their own.
- **GH secret / env var:** `WIN_TS_URL` (optional)
- **Fails without it:** uses the default, which works.

---

## tufup (in-app updater)

The app uses [tufup](https://github.com/dennisvang/tufup) for signed
in-app updates. tufup follows TUF, so you need three Ed25519 key pairs:
one each for the `targets`, `snapshot`, and `timestamp` roles. (`root` is
typically held offline by you, not by CI.)

### 1. Targets role key

- **What it is:** the Ed25519 private key that signs the list of release
  payloads (the actual zips users download).
- **Where to get it:** generate locally with
  `python -m tufup init` (run once to bootstrap the repo) or
  `python -c "from securesystemslib.interface import generate_and_write_ed25519_keypair_with_prompt; generate_and_write_ed25519_keypair_with_prompt('targets')"`.
  The public key goes into your tufup repo metadata; the private key
  (the file without `.pub`) becomes the secret.
- **GH secret:** `TUFUP_TARGETS_KEY` (the file contents)
- **Env var (local):** the path to the key file, used by `tufup` commands.
- **Fails without it:** workflow prints `MISSING_CERTS: TUFUP_TARGETS_KEY`
  and skips the tufup payload step. The GitHub Release still contains
  the binaries, but the in-app updater will not see this version.
- **Status:** required for in-app updates.

### 2. Snapshot role key

- **What it is:** Ed25519 private key that signs the snapshot metadata
  (which freezes the set of role versions).
- **Where to get it:** same generation flow as targets.
- **GH secret:** `TUFUP_SNAPSHOT_KEY`
- **Env var (local):** path to key file.
- **Fails without it:** same as targets; metadata update is skipped.
- **Status:** required for in-app updates.

### 3. Timestamp role key

- **What it is:** Ed25519 private key that signs the timestamp metadata
  (the short-lived "this update feed is alive" claim).
- **Where to get it:** same generation flow as targets.
- **GH secret:** `TUFUP_TIMESTAMP_KEY`
- **Env var (local):** path to key file.
- **Fails without it:** same as targets; metadata update is skipped.
- **Status:** required for in-app updates.

### 4. Root role key (offline)

- **What it is:** the master key. Keep it on encrypted offline storage
  (Yubikey, hardware wallet, paper backup in a safe).
- **Where to get it:** generate once locally with `python -m tufup init`
  and **do not put it in CI**. You rotate the other three role keys with
  this one when needed.
- **GH secret:** none. Deliberately not configured in CI.
- **Status:** required to bootstrap and rotate the update feed, never
  used in the release workflow itself.

---

## Quick sanity check before you tag a release

Run through this list:

- [ ] `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD`,
      `APPLE_DEV_ID_SIGNING_IDENTITY`, `APPLE_DEV_ID_CERT_P12`,
      `APPLE_DEV_ID_CERT_PASSWORD` all set in GH Secrets.
- [ ] Either `WIN_SIGNING_CERT_PFX` + `WIN_SIGNING_CERT_PASSWORD` +
      `WIN_SIGNING_CERT_THUMBPRINT`, or the SSL.com eSigner set, set in
      GH Secrets.
- [ ] `TUFUP_TARGETS_KEY`, `TUFUP_SNAPSHOT_KEY`, `TUFUP_TIMESTAMP_KEY`
      set in GH Secrets.
- [ ] tufup repo layout exists (metadata/, targets/) in whatever feed
      host you picked (GH Pages branch, S3, etc.).
- [ ] You tagged with the form `vX.Y.Z` so the workflow triggers.

If any line is missing, the workflow still finishes and uploads
`-DEV-BUILD` artefacts. Users will see Gatekeeper / SmartScreen warnings.
