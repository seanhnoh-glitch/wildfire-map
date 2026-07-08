# Mobile app — bring-up guide (Windows / Android)

> ⚠️ **Secondary / not actively maintained.** The **web map served at `/`** (open
> `http://localhost:8000`) is the primary, maintained UI — it works in any phone
> browser, no build required. This React Native app is from an earlier phase: it
> still works against the API and gets all the backend modeling improvements
> (water barriers, humidity-driven moisture, aspect, midflame wind), but it lacks
> the web map's newer UI features (viewport hotspots, dots centered on perimeters,
> the 24 h horizon and color styling) and forecasts default to 6 h. Use it only if
> you specifically want a native app.

Step-by-step to get the map running on an Android emulator or a physical phone.
The app uses MapLibre native modules, so it **cannot run in Expo Go** — you build
a small custom dev client with `expo prebuild` + `expo run:android`. That sounds
heavier than it is; it's three commands once the tooling is installed.

> On Windows you can build/run the **Android** app. **iOS needs a Mac** (Xcode) —
> the code is cross-platform, but there's no way to build an iOS app from Windows.

---

## 1. Install the tooling (one time)

| Tool | Why | Get it |
|---|---|---|
| **Node.js LTS (≥18)** | run Expo / npm | https://nodejs.org/ |
| **JDK 17** | Android Gradle build | `winget install EclipseAdoptium.Temurin.17.JDK` |
| **Android Studio** | Android SDK + emulator | https://developer.android.com/studio |

After installing Android Studio, open it once and use **More Actions → SDK
Manager** to confirm these are installed:
- **SDK Platforms:** Android 14 (API 34)
- **SDK Tools:** Android SDK Build-Tools, Android SDK Platform-Tools, Android Emulator

Then set environment variables (PowerShell, permanent):

```powershell
setx ANDROID_HOME "$env:LOCALAPPDATA\Android\Sdk"
setx JAVA_HOME "C:\Program Files\Eclipse Adoptium\jdk-17.0.11.9-hotspot"   # adjust to your JDK path
# Add platform-tools to PATH (so `adb` works). Restart the terminal afterwards.
setx PATH "$env:PATH;$env:LOCALAPPDATA\Android\Sdk\platform-tools"
```

Close and reopen your terminal so the variables take effect. Verify:

```powershell
node -v ; java -version ; adb version
```

### Create an emulator (or use a real phone)

- **Emulator:** Android Studio → **More Actions → Virtual Device Manager → Create
  Device** → pick e.g. *Pixel 7*, a system image (API 34), Finish. Launch it with
  the ▶ button. It must be **running** before `expo run:android`.
- **Physical phone (often easier + real GPS):** enable **Developer options →
  USB debugging**, plug in via USB, accept the "Allow USB debugging" prompt.
  Confirm it's seen: `adb devices` should list it.

---

## 2. Start the backend so the phone can reach it

The app talks to the FastAPI backend. A phone/emulator can't reach your laptop
via `localhost`, so this is the #1 gotcha.

Start the backend bound to all interfaces:

```powershell
cd "C:\Users\seanh\Claude\Projects\wildfire-map\backend"
.\.venv\Scripts\activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Point the app at it — pick the row that matches how you're running:

| Running on | API base URL |
|---|---|
| **Android emulator** | `http://10.0.2.2:8000` (special alias for your PC — **already the default**) |
| **Physical phone (same Wi‑Fi)** | `http://<your-PC-LAN-IP>:8000`, e.g. `http://192.168.1.42:8000` |

Find your LAN IP: `ipconfig` → the IPv4 address of your Wi‑Fi adapter.

For a physical phone, set it via env (Expo auto-loads `mobile/.env`):

```
# mobile/.env
EXPO_PUBLIC_API_URL=http://192.168.1.42:8000
```

> **Windows Firewall:** the first time, allow Python/uvicorn through the firewall
> on **Private networks** when prompted, or the phone's requests will silently
> hang. If you missed the prompt: Windows Security → Firewall → Allow an app →
> add Python for Private networks.

Sanity check from the phone's browser: open `http://<that URL>/health` — you
should see `{"status":"ok",...}`. If that doesn't load, the app won't either;
fix the network first.

---

## 3. Install and run the app

```powershell
cd "C:\Users\seanh\Claude\Projects\wildfire-map\mobile"
npm install
npx expo prebuild            # generates the native android/ project (one time-ish)
npx expo run:android         # builds + installs + launches on the emulator/phone
```

The first `run:android` compiles native code and can take several minutes.
Subsequent runs are fast — after the first build you can just `npx expo start
--dev-client` and press `a`.

You should see: a map, a search box up top. Type an address (e.g. `Paradise,
CA`), or tap the ◎ button to use device location. Orange dots are active fires —
tap one, then **Forecast spread →**, and step the **+h** control to animate the
predicted isochrones.

---

## 4. What the code already handles (v11 API)

The MapLibre **v11** API was applied in `src/screens/MapScreen.tsx`:
- named exports (`import { Map, Camera, GeoJSONSource, Layer } from ...`)
- `Map` (not `MapView`), `GeoJSONSource` (not `ShapeSource`, `data` not `shape`)
- unified `<Layer type="fill|line|circle" paint={{...}} />` with style-spec
  (kebab-case) paint props
- no `setAccessToken` (removed in v11 — MapLibre needs no token)
- source `onPress` reads `e.nativeEvent.features`

The Expo config plugin `@maplibre/maplibre-react-native` is already in
`app.json`, which is what makes `prebuild` wire up the native SDK.

---

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| App shows **"Network request failed"** / fires never load | API URL wrong for your target (see §2). Emulator→`10.0.2.2`, phone→LAN IP. Test `/health` in the phone browser first. |
| `/health` won't load on the phone | Windows Firewall blocking uvicorn on Private network; or phone not on the same Wi‑Fi; or backend not bound to `0.0.0.0`. |
| **Map is blank / gray** but UI shows | OSM tiles not loading — check the device has internet; some networks block `tile.openstreetmap.org`. Swap `tiles` in `src/lib/mapStyle.ts` for a MapTiler URL. |
| `expo run:android` → **"No connected devices"** | Start the emulator (Virtual Device Manager ▶) or plug in the phone; confirm with `adb devices`. |
| Gradle build fails on **JDK/Java version** | Ensure JDK **17** and `JAVA_HOME` points to it (`java -version`). JDK 21/8 will fail. |
| `SDK location not found` | `ANDROID_HOME` unset or wrong; restart terminal after `setx`. |
| `adb` not recognized | platform-tools not on PATH (see §1). |
| Native/config change not taking effect | Re-run `npx expo prebuild --clean` then `npx expo run:android`. |
| Hotspots (red dots) never appear | That layer needs a free **FIRMS_MAP_KEY** in `backend/.env`; everything else works without it. |

---

## 6. Nice-to-haves once it runs

- **Real tiles:** replace the OSM raster in `src/lib/mapStyle.ts` with a free
  MapTiler key (satellite imagery makes fire context much clearer).
- **iOS:** on a Mac, `npx expo run:ios` (needs Xcode). No code changes.
- **Share a build:** use EAS Build (`eas build -p android --profile preview`) to
  get an installable APK without the local toolchain.
