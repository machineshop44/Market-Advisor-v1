# Market Advisor Companion (Android)

Native multi-broker arm/disarm + WebView monitor for the desktop HTTPS companion API.

## Requirements

- Android Studio (SDK 35+ / **JDK 21**). Gradle is pinned via user `~/.gradle/gradle.properties` and this project's `gradle.properties` — do not use Android Studio's bundled JBR (Java 25) as the Gradle JDK.
- Market Advisor **1.29.1+** with Web Monitor enabled (companion **1.17.1** larger live monitor panel + text zoom; 1.17.0 clearer reauth/BP, ET live/$0 park, version sync; 1.16.x risk/session/shadow/frac/ET sandbox/WF)
- Companion app **1.17.1+** (older desktops still work; missing keys are ignored)
- Strong monitor **User/Pass** on the PC
- Bind: **Home Wi‑Fi + away**, HTTPS on
- TLS fingerprint via **Show setup QR** on the PC (Settings → Companion…) or paste manually

## Setup

1. On the PC: Settings → **Companion…** — set User/Pass, bind to all interfaces; Save Configuration; open **Show setup QR**.
2. Allow the monitor port in Windows Firewall; for away access, port-forward on the router.
3. Open this `android/` folder in Android Studio → Run / Build APK.
4. In the app Settings → **Scan setup QR** (camera permission once), or enter fields manually:
   - URL: `https://<lan-or-public-ip>:8791/`
   - Same User/Pass as the PC
   - TLS fingerprint
5. On the home screen, arm/disarm **Robinhood / Coinbase / E\*TRADE** independently (requires Companion Controls on the PC). Live monitor WebView stays below.

## Build / install

```bat
cd android
gradlew.bat assembleDebug
```

Install the APK from `android/app/build/outputs/apk/debug/`, or Run the `app` configuration from Android Studio onto a device/emulator.

**Share APK (on phone):** Settings → **Share APK** — copies this install into cache and opens the system share sheet (Nearby Share / Files / Drive).

**Publish to Google Drive apks** (same convention as Ava Bedtime):

```powershell
cd android
.\publish-apk-to-drive.ps1 -Build
```

Copies to `G:\My Drive\apks\MarketAdvisorCompanion-<version>-<code>.apk` and removes older companion APKs.

## Security

- Login is sent over **HTTPS (TLS)** — not cleartext.
- Wrong passwords lock out after 5 failures on the PC.
- No buy/sell from the phone.
- Launcher icon matches the desktop Market Advisor brand icon.
