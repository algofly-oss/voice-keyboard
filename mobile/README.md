# Mobile keyboards

Native keyboards for Android and iOS. They type dictated text into any app on
the phone itself. (To type on a computer from the phone, use the web app.)

| | Android | iOS / iPadOS |
| --- | --- | --- |
| Source | [android/](android/README.md) — Kotlin `InputMethodService` | [ios/](ios/README.md) — Swift app + keyboard extension |
| Records audio in | the keyboard | the containing app (iOS keyboards cannot use the mic) |
| Distribution | self-signed APK from `/downloads` | signed Ad Hoc build from `/downloads` (1 year, auto re-signed), or unsigned `.ipa` via SideStore |

## Pairing

Both apps register the `voicekeyboard://` URL scheme. On a phone logged in to
the web app, `/downloads` shows **Pair this phone**. That opens `voicekeyboard://pair?server=…&token=…`. The app
exchanges the install token (the same one the desktop command uses) at `POST /api/pair` for a long-lived device credential.
Both apps also accept a server URL and pairing code typed in by hand.

## Protocol

The keyboards stream 16 kHz mono PCM to `wss://host/v1/stream?token=<credential>`
and insert each `segment` as it arrives. Each device uses its own room,
`mobile-<client>`, so phone dictation is not also typed on paired computers.

## Publishing

Builds live in GitHub Releases (tags `voice-keyboard-v*`), not in git.

```bash
tools/release_mobile.sh 1.0.0   # build + sign the APK, tag, create the release
```

The tag starts the "Voice Keyboard iOS" workflow. It builds the unsigned `.ipa` on a
GitHub macOS runner and attaches it to the same release, usually within about
10 minutes.

The backend mirrors the newest release into its data volume
(`/data/releases`). It checks on start-up and every `RELEASE_SYNC_SECONDS`
(default 1 hour), and `/downloads` serves the files from there. Phones never
talk to GitHub, which matters because release assets of a private repository
need a token. Configure it in `backend/.env`:

```bash
GITHUB_REPO=owner/repo
GITHUB_TOKEN=github_pat_...   # fine-grained, read-only "Contents" on that repo
```

Set `IOS_DOWNLOAD_URL` if you also distribute through TestFlight.
