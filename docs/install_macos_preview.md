# Install the GPA macOS technical preview

GPA's public technical-preview DMG is built by GitHub Actions from the tagged
source. It is ad-hoc signed, but it is not signed with an Apple Developer ID and
is not notarized. macOS will therefore require an explicit first-launch choice.
Do not disable Gatekeeper globally.

## Download and verify

1. Open the latest [GPA prerelease](https://github.com/hanshenmesen/gpa/releases).
2. Download the DMG matching the published architecture and its adjacent
   `.sha256` file into the same folder.
3. In Terminal, change to that folder and run:

   ```bash
   shasum -a 256 -c GPA-macOS-*-unsigned.dmg.sha256
   ```

   Continue only when the command prints `OK`. A mismatch means the download
   must not be opened; delete it and report the release URL privately through
   the repository's security advisory form.

## Install and open

1. Open the verified DMG.
2. Drag `GPA.app` onto the `Applications` shortcut in the same window.
3. Open `/Applications/GPA.app` once. macOS may block the unsigned preview.
4. Open **System Settings → Privacy & Security**, locate the GPA message, choose
   **Open Anyway**, and confirm that you intend to open this copy.

This exception applies to the selected app. Never use commands that globally
disable Gatekeeper, and never download a GPA build from an Issue attachment or
unofficial mirror.

## First safe run

1. Leave desktop automation disabled while exploring the interface.
2. Configure an OpenAI-compatible provider only if you want model-assisted
   intent analysis; GPA stores that key locally.
3. Create a small test Replay using public or synthetic data.
4. Inspect the generated goal, merged steps, target applications, environment
   evidence and permissions before enabling desktop automation.
5. Keep the emergency stop visible during the first Replay.

macOS may ask for Accessibility, Input Monitoring or Screen Recording only when
the relevant feature needs it. Grant the minimum required permission and revoke
it later in System Settings if you stop testing GPA.

## Remove GPA

Quit GPA, move `/Applications/GPA.app` to Trash, then remove its permissions in
**System Settings → Privacy & Security**. GPA's local workflow data remains in
the current user's application-data directory so an uninstall does not silently
destroy recordings. Delete that data only after exporting anything you need.

## Report a result

- Use the [Bug report](https://github.com/hanshenmesen/gpa/issues/new/choose)
  for a reproducible defect.
- Use a compatibility report when the same Replay differs on another Mac.
- Use the real-world workflow form for a public or synthetic task GPA should
  learn to reproduce.

Remove account names, local paths, customer data, cookies, credentials and
private screen content before sharing logs or screenshots.
