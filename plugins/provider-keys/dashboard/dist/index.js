// The web dashboard gets no page from this plugin; the key menu lives in Hermes Desktop.
(function () {
  if (window.__HERMES_PLUGINS__) {
    window.__HERMES_PLUGINS__.register("provider-keys", function () { return null; });
  }
})();
