import fs from "fs";
import { createRequire } from "module";

const realRequire = createRequire(import.meta.url);
globalThis.require = (name) => {
  if (name === "dotenv") {
    return { config: () => ({ parsed: {} }) };
  }
  return realRequire(name);
};

async function main() {
  const modulePath = process.argv[2];
  if (!modulePath) {
    throw new Error("proxy-utils module path is required");
  }
  if (process.env.SUBSTORE_WORK_DIR) {
    process.chdir(process.env.SUBSTORE_WORK_DIR);
  }

  const raw = fs.readFileSync(0, "utf8");
  const { parse, produce } = await import(modulePath);

  // Sub-Store logs parser activation through console; stdout is reserved for YAML.
  console.log = () => {};
  console.info = () => {};
  console.warn = () => {};
  console.error = () => {};

  const proxies = parse(raw);
  if (!Array.isArray(proxies) || proxies.length === 0) {
    throw new Error("subscription contains no supported proxies");
  }
  const output = produce(proxies, "Mihomo", "external");
  if (typeof output !== "string" || output.length === 0) {
    throw new Error("Mihomo conversion produced no output");
  }
  process.stdout.write(output);
}

main().catch(() => {
  process.stderr.write("subscription conversion failed\n");
  process.exitCode = 1;
});
