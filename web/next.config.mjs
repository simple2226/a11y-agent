/** @type {import('next').NextConfig} */
export default {
  reactStrictMode: true,

  // "standalone" emits .next/standalone with a self-contained server.js and
  // only the node_modules the app actually uses. That is what the Amplify
  // Hosting compute bundle needs; see scripts/build-amplify-bundle.mjs.
  // It changes `next build` output only -- `next dev` is unaffected.
  output: "standalone",

  // REPORT_DIR points at the local `out/` the CLI writes. Set NEXT_PUBLIC_API_URL
  // instead to read from the deployed API.
  env: { REPORT_DIR: process.env.REPORT_DIR ?? "../out" },
};
