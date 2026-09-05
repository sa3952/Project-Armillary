(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SensitivityTime = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const HOUR_SECONDS = 3600;
  const LAST_SECOND = HOUR_SECONDS - 1;

  function clampSecond(value) {
    return Math.max(0, Math.min(LAST_SECOND, Math.round(value)));
  }

  function secondFromRatio(value) {
    const ratio = Math.max(0, Math.min(1, Number(value)));
    return clampSecond(ratio * LAST_SECOND);
  }

  function clockOf(value) {
    const second = clampSecond(value);
    const minute = Math.floor(second / 60);
    const remainder = second % 60;
    const pad = (number) => (number < 10 ? `0${number}` : String(number));
    return `14:${pad(minute)}:${pad(remainder)}`;
  }

  return Object.freeze({
    HOUR_SECONDS,
    LAST_SECOND,
    clampSecond,
    secondFromRatio,
    clockOf,
  });
});
