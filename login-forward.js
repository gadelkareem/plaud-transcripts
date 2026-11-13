// Used by login.ps1: runs Plaud's official login CLI inside a throwaway Node container.
//
// Two things the container needs that a normal PC does not:
// 1. The CLI listens for the browser callback on the container's own loopback
//    (127.0.0.1:8199), which a published Docker port cannot reach. A small forwarder
//    passes the container's outward address on the same port to it.
// 2. There is no browser in the container, so a stand-in "xdg-open" prints the login
//    link for the owner to open on the PC.
const fs = require("fs");
const net = require("net");
const os = require("os");
const { spawn } = require("child_process");

const PORT = 8199;
const version = process.env.PLAUD_CLI_VERSION || "0.3.15";

const address = Object.values(os.networkInterfaces())
  .flat()
  .find((a) => a && a.family === "IPv4" && !a.internal);
if (!address) {
  console.error("login: no network address found in the container");
  process.exit(1);
}
const forwarder = net
  .createServer((client) => {
    const upstream = net.connect(PORT, "127.0.0.1");
    client.pipe(upstream).pipe(client);
    client.on("error", () => upstream.destroy());
    upstream.on("error", () => client.destroy());
  })
  .listen(PORT, address.address);

// The CLI starts its "browser" with no output attached, so the stand-in saves the link
// to a file and this process prints it.
const LINK_FILE = "/tmp/plaud-login-link";
const SHOW_LINK = "/usr/local/bin/plaud-show-link";
fs.writeFileSync(SHOW_LINK, `#!/bin/sh\nprintf "%s" "$1" > ${LINK_FILE}\n`, { mode: 0o755 });
fs.writeFileSync("/usr/local/bin/xdg-open", `#!/bin/sh\nexec ${SHOW_LINK} "$@"\n`, { mode: 0o755 });
const watcher = setInterval(() => {
  if (!fs.existsSync(LINK_FILE)) return;
  clearInterval(watcher);
  const link = fs.readFileSync(LINK_FILE, "utf8").trim();
  console.log(`\n\nOpen this link in a browser on this PC (within 2 minutes):\n\n${link}\n`);
}, 300);

const cli = spawn("npx", ["--yes", `@plaud-ai/cli@${version}`, "login"], {
  stdio: "inherit",
  env: { ...process.env, BROWSER: SHOW_LINK },
});
cli.on("exit", (code) => {
  clearInterval(watcher);
  forwarder.close();
  process.exit(code === null ? 1 : code);
});
