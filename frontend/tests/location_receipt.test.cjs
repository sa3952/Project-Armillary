const test = require("node:test");
const assert = require("node:assert/strict");

const LocationReceipt = require("../zh-TW/location-receipt.js");

test("selected catalog point keeps its source and precision in the chart request", () => {
  const location = LocationReceipt.buildLocationInput({
    latitude: 24.1469,
    longitude: 120.6839,
    altitudeM: 0,
    selectedPlace: {
      display_name: "Taichung, 04, TW",
      latitude: 24.1469,
      longitude: 120.6839,
      source: "geonames_cities500",
      source_record_id: "1668399",
      location_precision: "place_representative_point",
    },
  });

  assert.deepEqual(location, {
    latitude: 24.1469,
    longitude: 120.6839,
    altitude_m: 0,
    place_label: "Taichung, 04, TW",
    location_source: "geonames_cities500",
    source_record_id: "1668399",
    location_precision: "place_representative_point",
  });
});

test("manual or changed coordinates cannot retain a catalog source assertion", () => {
  const selectedPlace = {
    display_name: "Taichung, 04, TW",
    latitude: 24.1469,
    longitude: 120.6839,
    source: "geonames_cities500",
    source_record_id: "1668399",
    location_precision: "place_representative_point",
  };

  assert.deepEqual(
    LocationReceipt.buildLocationInput({
      latitude: 24.2,
      longitude: 120.6839,
      altitudeM: 12,
      selectedPlace,
    }),
    {
      latitude: 24.2,
      longitude: 120.6839,
      altitude_m: 12,
      location_source: "manual",
      location_precision: "user_supplied_coordinates",
    }
  );
  assert.deepEqual(
    LocationReceipt.buildLocationInput({
      latitude: 24.2,
      longitude: 120.7,
      altitudeM: 12,
      selectedPlace: null,
    }),
    {
      latitude: 24.2,
      longitude: 120.7,
      altitude_m: 12,
      location_source: "manual",
      location_precision: "user_supplied_coordinates",
    }
  );
});

test("equivalent numeric formatting does not discard a valid catalog receipt", () => {
  const location = LocationReceipt.buildLocationInput({
    latitude: "24.1469000000",
    longitude: "120.6839000000",
    altitudeM: "0",
    selectedPlace: {
      name: "Taichung",
      latitude: 24.1469,
      longitude: 120.6839,
      source: "geonames_cities500",
      source_record_id: "1668399",
      location_precision: "place_representative_point",
    },
  });

  assert.equal(location.location_source, "geonames_cities500");
  assert.equal(location.place_label, "Taichung");
});
