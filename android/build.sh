#!/bin/sh
# Builds voice-debug.apk with only the Android SDK command-line tools + a JDK (no Gradle).
set -e
cd "$(dirname "$0")"
SDK=${ANDROID_HOME:-$HOME/Android/Sdk}
BT=$SDK/build-tools/$(ls "$SDK/build-tools" | sort -V | tail -1)
JAR=$SDK/platforms/android-36/android.jar
[ -f "$JAR" ] || { echo "Missing $JAR — install it: sdkmanager 'platforms;android-36'"; exit 1; }

rm -rf build && mkdir -p build/classes build/assets
# the shared UI (../web) is the whole app; test.js stays out of the APK
cp ../web/*.html ../web/*.css ../web/*.png build/assets/ && cp ../web/icons.js ../web/voxtral.js ../web/app.js build/assets/ && cp -r ../web/vendor build/assets/ && cp ../web/local.js build/assets/
# offline Greek ↔ English translation (Firefox models, 42 MB), kept out of git: tools/fetch-models.sh
[ -d ../models/bergamot/elen ] && mkdir -p build/assets/models && cp -r ../models/bergamot build/assets/models/
cp -r ../THIRD-PARTY-NOTICES.md ../licenses build/assets/
"$BT/aapt2" compile --dir res -o build/res.zip
"$BT/aapt2" link -I "$JAR" --manifest AndroidManifest.xml -o build/app.apk build/res.zip -A build/assets \
    --min-sdk-version 24 --target-sdk-version 36 --version-code 2 --version-name 0.1.0-beta.1
javac -encoding UTF-8 --release 11 -cp "$JAR" -d build/classes src/gr/voice/app/*.java
"$BT/d8" --release --min-api 24 --lib "$JAR" --output build $(find build/classes -name '*.class')
(cd build && zip -q app.apk classes.dex)
"$BT/zipalign" -f -p 4 build/app.apk build/aligned.apk
# debug keystore: created by Android Studio/SDK; make one if missing
KS=$HOME/.android/debug.keystore
[ -f "$KS" ] || keytool -genkeypair -keystore "$KS" -storepass android -alias androiddebugkey \
    -keypass android -keyalg RSA -validity 10000 -dname "CN=Android Debug,O=Android,C=US"
"$BT/apksigner" sign --ks "$KS" --ks-pass pass:android --out voice-debug.apk build/aligned.apk
echo "Built $(pwd)/voice-debug.apk"


# self-check of the shared core
command -v node >/dev/null && node ../web/test.js
