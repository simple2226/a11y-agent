/** @type {import('next').NextConfig} */
export default {
  reactStrictMode: true,
  // REPORT_DIR points at the local `out/` the CLI writes. Set NEXT_PUBLIC_API_URL
  // instead to read from the deployed API.
  env: { REPORT_DIR: process.env.REPORT_DIR ?? "../out" },
};
