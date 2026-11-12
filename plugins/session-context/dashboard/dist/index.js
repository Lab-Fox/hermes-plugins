// The web dashboard gets no page from this plugin; the UI lives in Hermes Desktop.
(function () {
  if (window.__HERMES_PLUGINS__) {
    window.__HERMES_PLUGINS__.register("session-context", function () { return null; });
  }
})();
