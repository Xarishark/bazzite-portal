import deckyPlugin from "@decky/rollup";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";

// Trusted repository artwork only; keep Portal's SVG as the single source.
const artwork = {
  name: "portal-artwork",
  load(id) {
    if (!["bazzite-logo.svg?raw", "bazzite-portal-circle-check-symbolic.svg?raw",
      "bazzite-portal-circle-x-symbolic.svg?raw"].some(name => id.endsWith(name))) return null;
    return `export default ${JSON.stringify(readFileSync(id.slice(0, -4), "utf8"))};`;
  },
};
const licenses = {
  name: "bundled-licenses",
  generateBundle() {
    const included = new Map();
    for (const id of this.getModuleIds()) {
      if (!id.includes("node_modules") || id.includes("\0")) continue;
      let directory = dirname(id);
      while (!existsSync(join(directory, "package.json")) && dirname(directory) !== directory) directory = dirname(directory);
      if (!existsSync(join(directory, "package.json"))) continue;
      const metadata = JSON.parse(readFileSync(join(directory, "package.json"), "utf8"));
      const key = `${metadata.name}@${metadata.version}`;
      if (included.has(key)) continue;
      const notices = readdirSync(directory).filter(name => /^(?:licen[cs]e|copying|notice)(?:\.|$)/i.test(name))
        .map(name => readFileSync(join(directory, name), "utf8"));
      included.set(key, [key, metadata.license || "", ...notices].join("\n"));
    }
    this.emitFile({type: "asset", fileName: "THIRD_PARTY_LICENSES.txt", source: [...included.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([, value]) => value).join("\n\n---\n\n")});
  },
};
export default deckyPlugin({ plugins: [artwork, licenses] });
