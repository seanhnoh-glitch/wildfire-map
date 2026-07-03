import React, { useCallback, useMemo, useRef, useState } from "react";
import { Alert, StyleSheet, View } from "react-native";
import * as Location from "expo-location";
// MapLibre React Native v11: named exports; MapView->Map, ShapeSource->GeoJSONSource,
// FillLayer/LineLayer/CircleLayer -> unified <Layer type=... paint=... />.
// (setAccessToken was removed in v11 — MapLibre needs no token.)
import { Map, Camera, GeoJSONSource, Layer } from "@maplibre/maplibre-react-native";

import { api, type Fire, type NearbyFiresResponse, type PredictResponse } from "../lib/api";
import { OSM_RASTER_STYLE } from "../lib/mapStyle";
import { SearchBar } from "../components/SearchBar";
import { FireInfoSheet } from "../components/FireInfoSheet";

const DEFAULT_CENTER: [number, number] = [-119.4, 37.5]; // California
const FIRE_COLOR = "#d85a30";
const PERIM_COLOR = "#b5321f";

export default function MapScreen() {
  const cameraRef = useRef<any>(null);
  const [center] = useState<[number, number]>(DEFAULT_CENTER);
  const [zoom] = useState(6);
  const [data, setData] = useState<NearbyFiresResponse | null>(null);
  const [selected, setSelected] = useState<Fire | null>(null);
  const [prediction, setPrediction] = useState<PredictResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [predicting, setPredicting] = useState(false);
  const [visibleHour, setVisibleHour] = useState(6);

  const flyTo = useCallback((lon: number, lat: number, z = 9) => {
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

  // FeatureCollection of fire points for the circle layer.
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
      <Map style={styles.map} mapStyle={OSM_RASTER_STYLE as any} onPress={() => setSelected(null)}>
        <Camera ref={cameraRef} defaultSettings={{ centerCoordinate: center, zoomLevel: zoom }} />

        {/* Declaration order = draw order (bottom -> top). */}

        {/* Official mapped perimeters (NIFC) */}
        {data?.perimeters ? (
          <GeoJSONSource id="perimeters" data={data.perimeters as any}>
            <Layer id="perim-fill" type="fill" paint={{ "fill-color": PERIM_COLOR, "fill-opacity": 0.18 }} />
            <Layer id="perim-line" type="line" paint={{ "line-color": PERIM_COLOR, "line-width": 2 }} />
          </GeoJSONSource>
        ) : null}

        {/* Forecast isochrones (graduated by hour) */}
        {visibleIsochrones ? (
          <GeoJSONSource id="isochrones" data={visibleIsochrones as any}>
            <Layer
              id="iso-fill"
              type="fill"
              paint={{
                "fill-color": [
                  "interpolate",
                  ["linear"],
                  ["get", "hours"],
                  1, "#ffd08a",
                  3, "#f08a3c",
                  6, "#c0261a",
                ],
                "fill-opacity": 0.28,
              }}
            />
            <Layer
              id="iso-line"
              type="line"
              paint={{ "line-color": "#c0261a", "line-width": 1, "line-opacity": 0.6 }}
            />
          </GeoJSONSource>
        ) : null}

        {/* Satellite hotspots (NASA FIRMS), if configured */}
        {data?.hotspots ? (
          <GeoJSONSource id="hotspots" data={data.hotspots as any}>
            <Layer
              id="hotspots-circle"
              type="circle"
              paint={{ "circle-radius": 3, "circle-color": "#ff3b30", "circle-opacity": 0.7 }}
            />
          </GeoJSONSource>
        ) : null}

        {/* Active fire incident points (declared last => drawn on top) */}
        <GeoJSONSource
          id="fires"
          data={firePoints as any}
          onPress={(e: any) => {
            const id = e?.nativeEvent?.features?.[0]?.properties?.id;
            const fire = data?.fires.find((f) => f.id === id);
            if (fire) onSelectFire(fire);
          }}
        >
          <Layer
            id="fires-circle"
            type="circle"
            paint={{
              "circle-radius": 8,
              "circle-color": FIRE_COLOR,
              "circle-stroke-color": "#fff",
              "circle-stroke-width": 2,
            }}
          />
        </GeoJSONSource>
      </Map>

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
