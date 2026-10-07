QuantDinger Android release signing (NOT tracked in git — see .gitignore)

Files (create locally, never commit):
  quantdinger-release.jks  — PKCS12 keystore (alias: quantdinger)
  keystore.properties       — storePassword / keyPassword / keyAlias / storeFile

Security:
  - This folder previously had a real keystore + plaintext passwords committed
    to git. Those credentials must be treated as compromised: regenerate the
    keystore and re-enroll with Play App Signing before the app is ever
    published, then rotate any values that were reused elsewhere.
  - quantdinger-release.jks and keystore.properties are now gitignored. Keep
    them local only, or inject them in CI from secrets, e.g.:
      echo "$ANDROID_KEYSTORE_BASE64" | base64 -d > mobile/signing/quantdinger-release.jks
      cat > mobile/signing/keystore.properties <<EOF
      storePassword=$ANDROID_STORE_PASSWORD
      keyPassword=$ANDROID_KEY_PASSWORD
      keyAlias=quantdinger
      storeFile=quantdinger-release.jks
      EOF

Build:
  Once the Capacitor android/ platform is added (`npx cap add android`),
  Gradle reads ../signing from android/ (see android/app/build.gradle).
