# Android voice keyboard

A native Android keyboard (`InputMethodService`) for the self-hosted Voice Keyboard
server. It records 16 kHz mono PCM on the phone, streams it to `/v1/stream`,
and types each transcribed segment into the focused text field. The setup app
handles pairing, the microphone permission, enabling the keyboard, and the
dictation language.

- Package: `ai.algofly.voicekeyboard`, minSdk 26 (Android 8), targetSdk 35
- Kotlin, plain Views, one dependency (OkHttp)
- Streams in the room `mobile-<client>`, so dictated text is not also typed on desktop clients
- Sessions stop after 10 minutes, or when the keyboard is hidden

## Install

1. On the phone, open `https://<your-server>/downloads` and download the APK.
2. Allow your browser to install unknown apps when Android asks, then install it.
3. Open **Voice Keyboard**.
4. On the `/downloads` page, tap **Pair this phone**. The `voicekeyboard://pair`
   link opens the app and pairs it. If the link does not open the app, type the
   server URL and pairing code into the app and tap **Pair**.
5. Tap **Allow microphone**.
6. Tap **Open keyboard settings** and turn on Voice Keyboard.
7. Tap **Choose keyboard**, or use the globe key on any keyboard, to switch to it.

To use the keyboard, tap the mic to start, then tap it again to finish. The
globe key switches to the next keyboard (long-press shows the picker). Backspace
repeats while held, and Enter runs the field's action (Send, Search, and so on)
or inserts a newline.

## Build from source

You need JDK 17+ with `javac` (a JRE alone does not work) and the Android SDK
(`platforms;android-35`, `build-tools;35.0.0`).

```sh
cd mobile/android
echo "sdk.dir=$HOME/Android/Sdk" > local.properties   # gitignored
export JAVA_HOME=$HOME/Android/jdk21                     # any full JDK
./gradlew assembleDebug      # app/build/outputs/apk/debug/app-debug.apk
./gradlew assembleRelease    # app/build/outputs/apk/release/app-release.apk
./gradlew lintRelease
cp app/build/outputs/apk/release/app-release.apk ../releases/android/VoiceKeyboard.apk
```

Versions: Gradle 8.10.2 (wrapper), AGP 8.7.3, Kotlin 2.0.21, OkHttp 4.12.0.

## Release signing

The release build reads `~/.android/voice-keyboard-release.properties`:

```properties
storeFile=/home/<you>/.android/voice-keyboard-release.jks
storePassword=...
keyAlias=voicekeyboard
keyPassword=...
```

If that file or its keystore is missing, `assembleRelease` falls back to the
debug key. The APK still installs, but Android rejects later updates signed with
the real key. Keep the keystore and properties file outside the repo with
`chmod 600`, and back them up: every update must be signed with the same key.

To create a keystore:

```sh
keytool -genkeypair -keystore ~/.android/voice-keyboard-release.jks -storetype PKCS12 \
  -alias voicekeyboard -keyalg RSA -keysize 4096 -validity 10000 \
  -dname "CN=Voice Keyboard"
```

To check a build:

```sh
$ANDROID_HOME/build-tools/35.0.0/apksigner verify --print-certs ../releases/android/VoiceKeyboard.apk
$ANDROID_HOME/build-tools/35.0.0/aapt2 dump badging ../releases/android/VoiceKeyboard.apk
```

## Where the APK goes

Releases are published with `tools/release.sh <version>`, which attaches
the APK to a GitHub release. It is not committed to git. The backend mirrors the
newest release and serves it at `/downloads/android/VoiceKeyboard.apk`.

## Notes

- Cleartext `http://` is allowed (network security config) so LAN servers work.
- The credential is stored in app-private SharedPreferences. If the server
  rejects it (close code 4401), the keyboard asks you to pair again.
- The app stores the server URL it paired through, not the `server` value in
  the pair response. So a phone paired on the LAN keeps using the LAN URL.
