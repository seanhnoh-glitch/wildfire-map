import React, { useState } from "react";
import {
  ActivityIndicator,
  StyleSheet,
  TextInput,
  TouchableOpacity,
  View,
  Text,
} from "react-native";

type Props = {
  onSubmit: (address: string) => void;
  onUseLocation: () => void;
  loading?: boolean;
};

export function SearchBar({ onSubmit, onUseLocation, loading }: Props) {
  const [value, setValue] = useState("");
  return (
    <View style={styles.wrap}>
      <View style={styles.row}>
        <TextInput
          style={styles.input}
          placeholder="Enter your address"
          placeholderTextColor="#8a8a8a"
          value={value}
          onChangeText={setValue}
          onSubmitEditing={() => value.trim() && onSubmit(value.trim())}
          returnKeyType="search"
        />
        <TouchableOpacity
          style={styles.locBtn}
          onPress={onUseLocation}
          accessibilityLabel="Use my location"
        >
          <Text style={styles.locIcon}>◎</Text>
        </TouchableOpacity>
      </View>
      {loading ? (
        <View style={styles.loading}>
          <ActivityIndicator size="small" color="#d85a30" />
          <Text style={styles.loadingText}>Checking fires…</Text>
        </View>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { position: "absolute", top: 52, left: 12, right: 12, zIndex: 10 },
  row: { flexDirection: "row", gap: 8 },
  input: {
    flex: 1,
    backgroundColor: "#fff",
    borderRadius: 12,
    paddingHorizontal: 14,
    paddingVertical: 12,
    fontSize: 15,
    color: "#1c1c1a",
    shadowColor: "#000",
    shadowOpacity: 0.15,
    shadowRadius: 6,
    shadowOffset: { width: 0, height: 2 },
    elevation: 3,
  },
  locBtn: {
    width: 46,
    backgroundColor: "#fff",
    borderRadius: 12,
    alignItems: "center",
    justifyContent: "center",
    elevation: 3,
  },
  locIcon: { fontSize: 22, color: "#d85a30" },
  loading: { flexDirection: "row", alignItems: "center", gap: 8, marginTop: 8, marginLeft: 4 },
  loadingText: { color: "#333", fontSize: 13 },
});
