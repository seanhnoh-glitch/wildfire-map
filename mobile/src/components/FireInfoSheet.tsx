import React from "react";
import { StyleSheet, Text, TouchableOpacity, View, ActivityIndicator } from "react-native";
import type { Fire, PredictResponse } from "../lib/api";

type Props = {
  fire: Fire;
  prediction: PredictResponse | null;
  predicting: boolean;
  maxHours: number;
  visibleHour: number;
  onPredict: () => void;
  onChangeHour: (h: number) => void;
  onClose: () => void;
};

export function FireInfoSheet({
  fire,
  prediction,
  predicting,
  maxHours,
  visibleHour,
  onPredict,
  onChangeHour,
  onClose,
}: Props) {
  return (
    <View style={styles.sheet}>
      <View style={styles.handle} />
      <View style={styles.headerRow}>
        <Text style={styles.title}>🔥 {fire.name}</Text>
        <TouchableOpacity onPress={onClose}>
          <Text style={styles.close}>✕</Text>
        </TouchableOpacity>
      </View>

      <Text style={styles.meta}>
        {fire.distance_km.toFixed(1)} km away
        {fire.size_acres != null ? ` · ${fire.size_acres.toLocaleString()} acres` : ""}
        {fire.percent_contained != null ? ` · ${fire.percent_contained}% contained` : ""}
      </Text>
      {fire.county || fire.state ? (
        <Text style={styles.sub}>{[fire.county, fire.state].filter(Boolean).join(", ")}</Text>
      ) : null}

      {!prediction ? (
        <TouchableOpacity style={styles.predictBtn} onPress={onPredict} disabled={predicting}>
          {predicting ? (
            <ActivityIndicator color="#fff" />
          ) : (
            <Text style={styles.predictText}>Forecast spread →</Text>
          )}
        </TouchableOpacity>
      ) : (
        <View style={styles.forecast}>
          <Text style={styles.forecastLabel}>
            Forecast · engine: {prediction.engine} · fuel {prediction.parameters.fuel_model} · wind{" "}
            {Math.round(prediction.parameters.wind_speed_kmh)} km/h
          </Text>
          <View style={styles.stepper}>
            <TouchableOpacity
              style={styles.stepBtn}
              onPress={() => onChangeHour(Math.max(1, visibleHour - 1))}
            >
              <Text style={styles.stepText}>−</Text>
            </TouchableOpacity>
            <Text style={styles.hourText}>+{visibleHour}h</Text>
            <TouchableOpacity
              style={styles.stepBtn}
              onPress={() => onChangeHour(Math.min(maxHours, visibleHour + 1))}
            >
              <Text style={styles.stepText}>+</Text>
            </TouchableOpacity>
          </View>
          <Text style={styles.disclaimer}>
            Research estimate — wind/fuel/terrain-driven elliptical model, not operational guidance.
          </Text>
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  sheet: {
    position: "absolute",
    bottom: 0,
    left: 0,
    right: 0,
    backgroundColor: "#fff",
    borderTopLeftRadius: 20,
    borderTopRightRadius: 20,
    padding: 18,
    paddingBottom: 30,
    shadowColor: "#000",
    shadowOpacity: 0.2,
    shadowRadius: 12,
    shadowOffset: { width: 0, height: -3 },
    elevation: 10,
  },
  handle: { alignSelf: "center", width: 40, height: 4, borderRadius: 2, backgroundColor: "#ddd", marginBottom: 10 },
  headerRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  title: { fontSize: 18, fontWeight: "700", color: "#1c1c1a", flex: 1 },
  close: { fontSize: 18, color: "#999", paddingLeft: 12 },
  meta: { marginTop: 6, fontSize: 14, color: "#333" },
  sub: { fontSize: 13, color: "#777", marginTop: 2 },
  predictBtn: {
    marginTop: 16,
    backgroundColor: "#d85a30",
    borderRadius: 12,
    paddingVertical: 13,
    alignItems: "center",
  },
  predictText: { color: "#fff", fontSize: 15, fontWeight: "600" },
  forecast: { marginTop: 14 },
  forecastLabel: { fontSize: 12, color: "#666" },
  stepper: { flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 22, marginTop: 12 },
  stepBtn: {
    width: 44,
    height: 44,
    borderRadius: 22,
    backgroundColor: "#f0ece3",
    alignItems: "center",
    justifyContent: "center",
  },
  stepText: { fontSize: 24, color: "#d85a30", fontWeight: "700" },
  hourText: { fontSize: 20, fontWeight: "700", color: "#1c1c1a", minWidth: 70, textAlign: "center" },
  disclaimer: { fontSize: 11, color: "#999", marginTop: 12, textAlign: "center" },
});
