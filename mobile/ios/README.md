# iOS / iPadOS keyboard

A containing app plus a custom keyboard extension. Requires iOS 17 and a Mac
with Xcode 16 to build.

## How it works

iOS does not let keyboard extensions use the microphone, so the work is split:

- **Voice Keyboard app** — pairing, microphone permission, and the recording
  session. It streams 16 kHz PCM to `/v1/stream` with its device credential.
  The `audio` background mode keeps it alive while you are in another app.
- **Keyboard extension** — mic, space, backspace, return and 🌐 keys. It sends
  start/stop to the app through Darwin notifications and inserts the segments
  the app publishes to the shared App Group.

The first mic tap opens the app, which starts listening at once. You swipe back
to the app you were typing in and keep talking. After that the session stays
ready for five minutes, so later taps start dictating straight from the
keyboard. Phone dictation uses its own room, `mobile-<client>`, so the text is
not also typed on paired computers.

## User install

### Signed Ad Hoc install (recommended, needs the $99/yr Apple Developer Program)

On the iPhone, log in to the web app, open `/downloads`, and:

1. Tap **Register this iPhone**, then open **Settings → Profile Downloaded →
   Install**. The profile only reports the device ID (UDID) to your server. The
   server then starts the Ad Hoc workflow, which registers the device with
   Apple and signs a build that includes it (about 10 minutes).
2. Tap **Install on this iPhone**. It is a standard over-the-air install, with
   nothing to trust and no computer needed.
3. Tap **Pair this phone**, allow the microphone, then add the keyboard in
   **Settings → General → Keyboard → Keyboards** and turn on **Allow Full
   Access**.

The signature lasts one year. A monthly workflow run re-signs the build when
less than 30 days remain, and `/downloads` shows the new date. Tap **Install on
this iPhone** again to renew it; settings and pairing are kept. Apple allows
100 registered iPhones per membership year.

**One-time setup:**

1. Enroll in the [Apple Developer Program](https://developer.apple.com/programs/enroll/).
2. In App Store Connect → Users and Access → Integrations → App Store Connect
   API, create a key with the **Admin** role (needed for cloud-managed signing
   certificates). Download the `.p8` file.
3. Add the repository secrets:

   ```bash
   gh secret set APPLE_TEAM_ID  --body XXXXXXXXXX       # Membership details page
   gh secret set ASC_KEY_ID     --body XXXXXXXXXX
   gh secret set ASC_ISSUER_ID  --body xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   gh secret set ASC_KEY_P8     < AuthKey_XXXXXXXXXX.p8
   ```

4. Run `tools/release_mobile.sh <version>`, or run the "Voice Keyboard iOS Ad
   Hoc" workflow once. Xcode's automatic signing then registers the bundle
   IDs, the App Group, and the Ad Hoc profile. If `ai.algofly.voicekeyboard`
   is taken, change the IDs in `project.yml` and `Shared/SharedStore.swift`.

### Sideloading without a developer account

`/downloads` → **No Apple developer account? Sideload instead** offers the
unsigned `.ipa` for [SideStore](https://sidestore.io) or AltStore. They sign it
with your free Apple ID, so it must be refreshed every 7 days. The app detects
the App Group that these tools rename when they re-sign, so it keeps working.

## Build

```bash
brew install xcodegen
cd mobile/ios
xcodegen            # creates VoiceKeyboard.xcodeproj (git-ignored)
open VoiceKeyboard.xcodeproj
```

In Xcode, set your team for both targets. If `ai.algofly.voicekeyboard` is not
your bundle ID, change the IDs and the App Group
`group.ai.algofly.voicekeyboard` in `project.yml` and `Shared/SharedStore.swift`.
Run on a physical device, since the Simulator has limited keyboard-extension
and microphone support.

## Build the unsigned .ipa without a Mac

The GitHub Actions workflow `.github/workflows/voice-keyboard-ios.yml` builds it
on a macOS runner. Branch pushes only check that it compiles; a
`voice-keyboard-v*` tag attaches the `.ipa` to that GitHub release (see
`tools/release_mobile.sh`). The backend mirrors it from there.

## Distribute through TestFlight (optional)

Archive, upload to App Store Connect, and add testers to a TestFlight group.
Put the public TestFlight link (or the App Store URL) in `backend/.env`:

```bash
IOS_DOWNLOAD_URL=https://testflight.apple.com/join/XXXXXXXX
```

Review notes: keyboards that request Full Access must explain why, and they
must still work in a basic way without it. This keyboard's space, backspace and
return keys work without Full Access, and it shows instructions in place of the
mic. The keyboard opens the app through the responder chain, a common but
unofficial technique. If a future iOS release blocks it, the keyboard tells the
user to open the app manually.
