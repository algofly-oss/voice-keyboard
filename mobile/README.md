# Android keyboard

A native Android keyboard (`InputMethodService`, Kotlin) that records and
types dictated text into any app on the phone. To type on a computer from
the phone, use the web app instead. Source and build instructions are in
[android/](android/README.md).

## Pairing

The app registers the `voicekeyboard://` URL scheme. On a phone logged in to
the web app, `/downloads` shows **Pair this phone**, which opens
`voicekeyboard://pair?server=…&token=…`. The app exchanges the install token
(the same one the desktop command uses) at `POST /api/pair` for a long-lived
device credential. A server URL and pairing code can also be typed in by hand.

## Protocol

The keyboard streams 16 kHz mono PCM to `wss://host/v1/stream?token=<credential>`
and inserts each `segment` as it arrives. Each device uses its own room,
`mobile-<client>`, so phone dictation is not also typed on paired computers.

## Publishing

Builds live in GitHub Releases (tags `voice-keyboard-v*`), not in git:

```bash
tools/release.sh 1.0.0   # builds the desktop binaries and signed APK, tags, creates the release
```

The backend mirrors the newest release into its data volume (`/data/releases`)
on start-up and every `RELEASE_SYNC_SECONDS` (default one hour), and
`/downloads` serves it from there. Phones never talk to GitHub, which matters
because release assets of a private repository need a token:

```bash
GITHUB_REPO=owner/repo
GITHUB_TOKEN=github_pat_...   # fine-grained, read-only "Contents" on that repo
```
