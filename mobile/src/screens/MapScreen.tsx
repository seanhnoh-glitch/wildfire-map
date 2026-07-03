import React, { useCallback, useMemo, useRef, useState } from "react";
import { Alert, StyleSheet, View } from "react-native";
import * as Location from "expo-location";
import MapLibreGL from "@maplibre/maplibre-react-native";

import { api, type Fire, type NearbyFiresResponse, type PredictResponse } from "../lib/api";
import { OSM_RASTER_STYLE } from "../lib/mapStyle";
import { SearchBar } from "../components/SearchBar";
import { FireInfoSheet } from "../components/FireInfoSheet";

// MapLibre needs no access token; set to null explicitly.
MapLibreGL.setAccessToken(null);

const DEFAULT_CENTER: [number, number] = [-119.4, 37.5]; // California
const FIRE_COLOR = "#d85a30";
const PERIM_COLOR = "#b5321f";

export default function MapScreen() {
  const cameraRef = useRef<MapLibreGL.Camera>(null);
  const [center, setCenter] = useState<[number, number]>(DEFAULT_CENTER);
  const [zoom, setZoom] = useState(6);
  const [data, setData] = useState<NearbyFiresResponse | null>(null);
  const [selected, setSelected] = useState<Fire | null>(null);
  const [prediction, setPrediction] = useState<PredictResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [predicting, setPredicting] = useState(false);
  const [visibleHour, setVisibleHour] = useState(6);

  const flyTo = useCallback((lon: number, lat: number, z = 9) => {
    setCenter([lon, lat]);
    setZoom(z);
    cameraRef.current?.setCamera({
      centerCoordinate: [lon, lat],
      zoomLevel: z,
      animationDuration: 800,
    });
  }, []);

  const loadFires = useCallback(async (lat: number, lon: number) => {
    setLoading(true);
    setSelected(null);
    setPrediction(null);
    try {
      const res = await api.nearbyFires(lat, lon, 120);
      setData(res);
      if (res.count === 0) {
        Alert.alert("No active wildfires", "No significant active wildfires within 120 km.");
      }
    } catch (e: any) {
      Alert.alert("Couldn't load fires", e.message ?? String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const onSearch = useCallback(
    async (address: string) => {
      setLoading(true);
      try {
        const g = await api.geocode(address);
        flyTo(g.lon, g.lat);
        await loadFires(g.lat, g.lon);
      } catch (e: any) {
        Alert.alert("Address not found", e.message ?? String(e));
        setLoading(false);
      }
    },
    [flyTo, loadFires]
  );

  const onUseLocation = useCallback(async () => {
    const { status } = await Location.requestForegroundPermissionsAsync();
    if (status !== "granted") {
      Alert.alert("Location blocked", "Enable location access, or type your address instead.");
      return;
    }
    const pos = await Location.getCurrentPositionAsync({});
    flyTo(pos.coords.longitude, pos.coords.latitude);
    await loadFires(pos.coords.latitude, pos.coords.longitude);
  }, [flyTo, loadFires]);

  const onSelectFire = useCallback(
    (fire: Fire) => {
      setSelected(fire);
      setPrediction(null);
      flyTo(fire.lon, fire.lat, 11);
    },
    [flyTo]
  );

  const onPredict = useCallback(async () => {
    if (!selected) return;
    setPredicting(true);
    try {
      const res = await api.predict({
        lat: selected.lat,
        lon: selected.lon,
        duration_hours: 6,
        step_minutes: 60,
      });
      setPrediction(res);
      setVisibleHour(6);
    } catch (e: any) {
      Alert.alert("Forecast failed", e.message ?? String(e));
    } finally {
      setPredicting(false);
    }
  }, [selected]);

  // FeatureCollection of fire points for a symbol/circle layer.
  const firePoints = useMemo(() => {
    const feats = (data?.fires ?? []).map((f) => ({
      type: "Feature" as const,
      id: f.id,
      geometry: { type: "Point" as const, coordinates: [f.lon, f.lat] },
      properties: { id: f.id, name: f.name },
    }));
    return { type: "FeatureCollection" as const, features: feats };
  }, [data]);

  // Only show isochrones up to the selected hour.
  const visibleIsochrones = useMemo(() => {
    if (!prediction) return null;
    const feats = prediction.isochrones.features.filter(
      (f: any) => f.properties.hours <= visibleHour + 0.001
    );
    return { type: "FeatureCollection" as const, features: feats };
  }, [prediction, visibleHour]);

  const maxHours = prediction
    ? Math.max(...prediction.isochrones.features.map((f: any) => f.properties.hours))
    : 6;

  return (
    <View style={styles.container}>
      <MapLibreGL.MapView
        style={styles.map}
        mapStyle={OSM_RASTER_STYLE as any}
        onPress={() => setSelected(null)}
      >
        <MapLibreGL.Camera ref={cameraRef} defaultSettings={{ centerCoordinate: center, zoomLevel: zoom }} />

        {/* Official mapped perimeters (NIFC) */}
        {data?.perimeters ? (
          <MapLibreGL.ShapeSource id="perimeters" shape={data.perimeters as any}>
            <MapLibreGL.FillLayer
              id="perim-fill"
              style={{ fillColor: PERIM_COLOR, fillOpacity: 0.18 }}
            />
            <MapLibreGL.LineLayer
              id="perim-line"
              style={{ lineColor: PERIM_COLOR, lineWidth: 2 }}
            />
          </MapLibreGL.ShapeSource>
        ) : null}

        {/* Forecast isochrones (graduated by hour) */}
        {visibleIsochrones ? (
          <MapLibreGL.ShapeSource id="isochrones" shape={visibleIsochrones as any}>
            <MapLibreGL.FillLayer
              id="iso-fill"
              style={{
                fillColor: [
                  "interpolate",
                  ["linear"],
                  ["get", "hours"],
                  1,
                  "#ffd08a",
                  3,
                  "#f08a3c",
                  6,
                  "#c0261a",
                ],
                fillOpacity: 0.28,
              }}
              belowLayerID="fires-circle"
            />
            <MapLibreGL.LineLayer
              id="iso-line"
              style={{ lineColor: "#c0261a", lineWidth: 1, lineOpacity: 0.6 }}
            />
          </MapLibreGL.ShapeSource>
        ) : null}

        {/* Satellite hotspots (NASA FIRMS), if configured */}
        {data?.hotspots ? (
          <MapLibreGL.ShapeSource id="hotspots" shape={data.hotspots as any}>
            <MapLibreGL.CircleLayer
              id="hotspots-circle"
              style={{ circleRadius: 3, circleColor: "#ff3b30", circleOpacity: 0.7 }}
            />
          </MapLibreGL.ShapeSource>
        ) : null}

        {/* Active fire incident points */}
        <MapLibreGL.ShapeSource
          id="fires"
          shape={firePoints as any}
          onPress={(e) => {
            const id = e.features?.[0]?.properties?.id;
            const fire = data?.fires.find((f) => f.id === id);
            if (fire) onSelectFire(fire);
          }}
        >
          <MapLibreGL.CircleLayer
            id="fires-circle"
            style={{
              circleRadius: 8,
              circleColor: FIRE_COLOR,
              circleStrokeColor: "#fff",
              circleStrokeWidth: 2,
            }}
          />
        </MapLibreGL.ShapeSource>
      </MapLibreGL.MapView>

      <SearchBar onSubmit={onSearch} onUseLocation={onUseLocation} loading={loading} />

      {selected ? (
        <FireInfoSheet
          fire={selected}
          prediction={prediction}
          predicting={predicting}
          maxHours={maxHours}
          visibleHour={visibleHour}
          onPredict={onPredict}
          onChangeHour={setVisibleHour}
          onClose={() => setSelected(null)}
        />
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  map: { flex: 1 },
});
