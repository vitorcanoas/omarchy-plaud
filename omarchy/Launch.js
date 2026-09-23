// Resolve only our installed application. Never accept commands, URLs or args
// from widget settings or IPC; DesktopEntry handles argv and working directory.
function entry(entries) {
  return entries.byId("plaud-linux")
}

function open(entries) {
  var app = entry(entries)
  if (!app) return false
  app.execute()
  return true
}
