import { Platform } from "react-native";

/**
 * Base URL of the FastAPI backend.
 *
 * IMPORTANT: a phone/emulator can't reach your laptop via "localhost".
 *  - Android emulator:  http://10.0.2.2:8000  (special alias for host machine)
 *  - iOS simulator:     http://localhost:8000
 *  - Physical device:   http://<your-laptop-LAN-IP>:8000  (e.g. 192.168.1.42)
 *
 * Set EXPO_PUBLIC_API_URL in an .env / eas env to override for real devices.
 */
const fromEnv = process.env.EXPO_PUBLIC_API_URL;

export const API_BASE =
  fromEnv ??
  (Platform.OS === "android" ? "http://10.0.2.2:8000" : "http://localhost:8000");
