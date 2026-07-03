import { API_BASE } from "./config";

export type GeocodeResult = { lat: number; lon: number; label: string };

export type Fire = {
  id: string;
  name: string;
  lat: number;
  lon: number;
  distance_km: number;
  size_acres: number | null;
  percent_contained: number | null;
  discovery_time: string | null;
  county: string | null;
  state: string | null;
};

export type FeatureCollection = {
  type: "FeatureCollection";
  features: any[];
  properties?: Record<string, any>;
};

export type NearbyFiresResponse = {
  query: { lat: number; lon: number };
  radius_km: number;
  count: number;
  fires: Fire[];
  hotspots: FeatureCollection | null;
  perimeters: FeatureCollection | null;
};

export type PredictResponse = {
  engine: string;
  parameters: Record<string, any>;
  isochrones: FeatureCollection;
  notes: string[];
};

async function get<T>(path: string, params: Record<string, any>): Promise<T> {
  const qs = new URLSearchParams(
    Object.entries(params).map(([k, v]) => [k, String(v)])
  ).toString();
  const res = await fetch(`${API_BASE}${path}?${qs}`);
  if (!res.ok) throw new Error(`${res.status}: ${(await res.text()).slice(0, 160)}`);
  return res.json();
}

export const api = {
  geocode: (address: string) => get<GeocodeResult>("/geocode", { address }),

  nearbyFires: (lat: number, lon: number, radius_km = 80) =>
    get<NearbyFiresResponse>("/fires/nearby", { lat, lon, radius_km }),

  predict: async (body: {
    lat: number;
    lon: number;
    duration_hours?: number;
    step_minutes?: number;
    wind_speed_kmh?: number;
    wind_direction_deg?: number;
  }): Promise<PredictResponse> => {
    const res = await fetch(`${API_BASE}/predict`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`${res.status}: ${(await res.text()).slice(0, 160)}`);
    return res.json();
  },
};
