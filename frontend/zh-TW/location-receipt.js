(function attachLocationReceipt(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.LocationReceipt = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function buildLocationReceipt() {
  "use strict";

  const COORDINATE_EQUALITY_TOLERANCE_DEGREES = 1e-9;

  function sameCoordinate(currentValue, selectedValue) {
    const current = Number(currentValue);
    const selected = Number(selectedValue);
    return Number.isFinite(current)
      && Number.isFinite(selected)
      && Math.abs(current - selected) <= COORDINATE_EQUALITY_TOLERANCE_DEGREES;
  }

  function manualLocation(latitude, longitude, altitudeM) {
    return {
      latitude: Number(latitude),
      longitude: Number(longitude),
      altitude_m: Number(altitudeM),
      location_source: "manual",
      location_precision: "user_supplied_coordinates",
    };
  }

  function buildLocationInput({ latitude, longitude, altitudeM, selectedPlace }) {
    const manual = manualLocation(latitude, longitude, altitudeM);
    if (!selectedPlace) return manual;

    const traceable = typeof selectedPlace.source === "string"
      && selectedPlace.source.length > 0
      && typeof selectedPlace.source_record_id === "string"
      && selectedPlace.source_record_id.length > 0
      && typeof selectedPlace.location_precision === "string"
      && selectedPlace.location_precision.length > 0;
    const unchanged = sameCoordinate(latitude, selectedPlace.latitude)
      && sameCoordinate(longitude, selectedPlace.longitude);
    if (!traceable || !unchanged) return manual;

    return {
      latitude: manual.latitude,
      longitude: manual.longitude,
      altitude_m: manual.altitude_m,
      place_label: selectedPlace.display_name || selectedPlace.name,
      location_source: selectedPlace.source,
      source_record_id: selectedPlace.source_record_id,
      location_precision: selectedPlace.location_precision,
    };
  }

  return Object.freeze({ buildLocationInput, sameCoordinate });
});
