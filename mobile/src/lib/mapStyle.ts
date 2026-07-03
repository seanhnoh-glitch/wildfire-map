/**
 * A minimal, keyless MapLibre raster style using OpenStreetMap tiles so the app
 * renders a real map with zero signup. OSM's public tile server is fine for
 * development/prototyping but is NOT for production traffic — for a real launch,
 * swap `tiles` for a MapTiler / Stadia / self-hosted vector or raster endpoint.
 */
export const OSM_RASTER_STYLE = {
  version: 8,
  sources: {
    osm: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      attribution: "© OpenStreetMap contributors",
    },
  },
  layers: [{ id: "osm", type: "raster", source: "osm" }],
} as const;
