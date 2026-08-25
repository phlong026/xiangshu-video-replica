import { BlockList, isIP } from "node:net";

const message =
  "VITE_API_BASE_URL must be a non-loopback HTTPS origin for the customer cloud build";

let url;
try {
  url = new URL(process.env.VITE_API_BASE_URL);
} catch {
  console.error(message);
  process.exit(1);
}

const hostname = url.hostname
  .replace(/^\[/, "")
  .replace(/\]$/, "")
  .replace(/\.$/, "")
  .toLowerCase();
const addressFamily = isIP(hostname);
const loopbackAddresses = new BlockList();
loopbackAddresses.addSubnet("127.0.0.0", 8, "ipv4");
loopbackAddresses.addAddress("::1", "ipv6");

const isLoopbackAddress =
  addressFamily !== 0 &&
  loopbackAddresses.check(hostname, addressFamily === 4 ? "ipv4" : "ipv6");
const isLoopbackHostname = hostname === "localhost" || hostname.endsWith(".localhost");

if (
  url.protocol !== "https:" ||
  isLoopbackAddress ||
  isLoopbackHostname ||
  url.username ||
  url.password ||
  url.port ||
  url.pathname !== "/" ||
  url.search ||
  url.hash
) {
  console.error(message);
  process.exit(1);
}
