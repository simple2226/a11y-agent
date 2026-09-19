/**
 * Assemble .amplify-hosting/ from a standalone Next.js build.
 *
 * Amplify's WEB_COMPUTE deploy looks for .amplify-hosting/deploy-manifest.json.
 * It normally writes that itself via its built-in Next.js adapter, but that
 * adapter only runs when Amplify's own framework detection fires -- which it
 * does not reliably do for an app in a subdirectory. Building the bundle here
 * removes the guesswork: the output is the same either way, and it is produced
 * by our build rather than by detection we cannot see.
 *
 * Layout Amplify expects:
 *   .amplify-hosting/deploy-manifest.json
 *   .amplify-hosting/static/**            served directly from /
 *   .amplify-hosting/compute/default/**   node server, entrypoint server.js
 */
import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";

const webRoot = path.resolve(import.meta.dirname, "..");
const next = path.join(webRoot, ".next");
const out = path.join(webRoot, ".amplify-hosting");
const compute = path.join(out, "compute", "default");
const staticRoot = path.join(out, "static");

const standalone = path.join(next, "standalone");
if (!existsSync(standalone)) {
  console.error(
    'Missing .next/standalone. next.config.mjs must set output: "standalone".',
  );
  process.exit(1);
}

await rm(out, { recursive: true, force: true });
await mkdir(compute, { recursive: true });
await mkdir(staticRoot, { recursive: true });

// The standalone server and its trimmed node_modules.
await cp(standalone, compute, { recursive: true });

// Standalone deliberately omits the static chunks; the server still expects
// them on disk at .next/static, so copy them in beside it.
await cp(path.join(next, "static"), path.join(compute, ".next", "static"), {
  recursive: true,
});

// Anything in public/ is served straight from the CDN, not through compute.
const publicDir = path.join(webRoot, "public");
if (existsSync(publicDir)) {
  await cp(publicDir, staticRoot, { recursive: true });
}

// Static chunks are immutable and content-hashed, so serve them from the CDN
// too rather than waking the compute resource for every asset.
await cp(path.join(next, "static"), path.join(staticRoot, "_next", "static"), {
  recursive: true,
});

const { version } = JSON.parse(
  await readFile(path.join(webRoot, "node_modules", "next", "package.json"), "utf-8"),
);

const manifest = {
  version: 1,
  routes: [
    // Hashed assets: CDN only, never compute.
    {
      path: "/_next/static/*",
      target: { kind: "Static", cacheControl: "public, max-age=31536000, immutable" },
    },
    // Anything else that exists as a static file wins; otherwise the server
    // handles it. That covers SSR pages and both /api routes.
    { path: "/*", target: { kind: "Static" }, fallback: { kind: "Compute", src: "default" } },
    { path: "/*", target: { kind: "Compute", src: "default" } },
  ],
  computeResources: [
    { name: "default", entrypoint: "server.js", runtime: "nodejs20.x" },
  ],
  framework: { name: "next", version },
};

await writeFile(
  path.join(out, "deploy-manifest.json"),
  JSON.stringify(manifest, null, 2) + "\n",
);

console.log(`amplify bundle ready  (next ${version})`);
console.log(`  compute entrypoint  compute/default/server.js`);
console.log(`  static root         static/`);
