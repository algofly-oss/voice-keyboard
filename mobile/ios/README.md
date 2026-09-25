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

iOS never runs unsigned apps, and a website cannot install one. The lowest-effort
route without a developer account is sideloading:

1. Install [SideStore](https://sidestore.io) (or AltStore) once, using a
   computer and your Apple ID.
2. On the phone, open `https://your-host/downloads` and tap **Install with
   SideStore**. It downloads the unsigned `.ipa` and signs it with your Apple
   ID.
3. On the same page, tap **Pair this phone** and allow the microphone.
4. **Settings → General → Keyboard → Keyboards → Add New Keyboard → Voice
   Keyboard**, then turn on **Allow Full Access**. The keyboard needs it to read
   the shared App Group.

With a free Apple ID the signature lasts 7 days; SideStore refreshes it on the
phone. A paid developer account gives one-year signatures or a TestFlight link
(`IOS_DOWNLOAD_URL`).

Sideloading tools rename the App Group when they re-sign. The app works out the
renamed group at runtime (`SharedStore.appGroup`), so the keyboard and the app
still share settings. This has not yet been tested on a real device.

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
`tools/release_mobile.sh` and `tools/fetch_mobile_release.sh`).

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
