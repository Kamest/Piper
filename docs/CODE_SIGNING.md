# Code signing policy

Piper is open-source software licensed under Apache-2.0. This policy applies to the desktop
application built from [Kamest/Piper](https://github.com/Kamest/Piper).

Free code signing provided by SignPath.io, certificate by SignPath Foundation

## What is signed

When configured, Windows release builds sign `Piper.exe` in the unpacked application, then package
that signed directory into an NSIS installer and sign the installer. Each architecture has its own
two signing requests. Third-party Electron components retain their upstream signatures; they are not
re-signed as Piper. The generated NSIS uninstaller is not separately signed by this integration.
Only CI builds from this repository's source on GitHub-hosted runners are eligible, and only for
`desktop-v*` tags. Arbitrary local binaries and pull-request builds are never submitted for signing.

macOS builds without a certificate receive an ad-hoc bundle signature with hardened runtime disabled.
This seals resources but does not identify a trusted developer or notarize the app. With Apple
credentials configured, the existing certificate signing and notarization flow applies instead.

## Roles and approval

| Role | Members |
| --- | --- |
| Committers and reviewers | [@Kamest](https://github.com/Kamest) (maintainer) |
| Approvers | [@Kamest](https://github.com/Kamest) (maintainer) |

Contributors submit source changes for review. The maintainer reviews and merges changes, controls
release tags and signing configuration, and approves each signing request in SignPath after checking
its source commit, workflow and artifacts. CI is a submitter only; it must not have approval rights.
Configure a SignPath release policy with mandatory maintainer approval and a GitHub trusted build
origin restricted to this repository and release workflow. Signing does not publish a release;
the maintainer separately reviews and publishes the draft.

## Privacy

The desktop app sends no telemetry, analytics, transcripts or personal data to Piper's maintainers
or the signing provider. Its background external requests are update checks and downloads against
GitHub releases. Otherwise it communicates with the user's own Piper service; the destinations and
data used by that service are under the user's control. User-requested prerequisite downloads during
setup contact the relevant upstream download service. Signing is a build-time operation, not an
application connection to SignPath.

## Maintainer setup

1. Apply to [SignPath Foundation](https://signpath.org/) for the public repository and link this policy.
   Configure the GitHub trusted build integration and a release signing policy with the roles above.
2. Import `.signpath/windows-app.xml` with slug `windows-app` and
   `.signpath/windows-installer.xml` with slug `windows-installer` into the same project. Both use
   SignPath's [artifact configuration XML schema](https://docs.signpath.io/artifact-configuration/syntax)
   (`http://signpath.io/artifact-configuration/v1`): the outer ZIP is the GitHub Actions artifact,
   and only the selected PE file receives Authenticode signing. Review real x64/arm64 sample artifacts
   in SignPath before enabling the release policy.
3. Set repository variables `SIGNPATH_ORGANIZATION_ID`, `SIGNPATH_PROJECT_SLUG`,
   `SIGNPATH_POLICY_SLUG` and repository secret `SIGNPATH_API_TOKEN` (submitter only). Setting the
   organization ID enables the Windows tag-build path; leave it unset until the other settings work.
   Each approval has a 15-minute CI wait limit within the 60-minute build job.
4. Confirm that the certificate subject's common name is `SignPath Foundation`. The signed path sets
   `win.signtoolOptions.publisherName` to this value, which electron-builder embeds as `publisherName`
   in `resources/app-update.yml`. electron-updater checks future Windows installers against it.
   Change the configuration if the issued certificate uses a different subject. Without SignPath,
   existing unsigned and locally certificate-signed build behavior is preserved.
5. Verify real installation and an update between two signed releases on each Windows architecture.
   External signing changes installer bytes: CI regenerates SHA-512, size and differential blockmap
   before uploading the final installer and updater metadata. Do not reuse the unsigned metadata.

The integration uses the [SignPath GitHub action](https://docs.signpath.io/trusted-build-systems/github)
pinned to release `v3.0`, with uploaded artifact IDs, completion waits and signed artifact downloads.
No signing account, certificate or credential is stored in this repository.
