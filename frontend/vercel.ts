const apiUrl = process.env.BACKEND_API_URL;

if (!apiUrl) {
  throw new Error('Set BACKEND_API_URL in Vercel to the deployed CineMood Express service URL.');
}

const backend = new URL(apiUrl);
if (backend.protocol !== 'https:' || backend.username || backend.password) {
  throw new Error('BACKEND_API_URL must be an HTTPS URL without embedded credentials.');
}

export const config = {
  framework: 'vite',
  buildCommand: 'npm run build',
  outputDirectory: 'dist',
  rewrites: [
    { source: '/api/:path*', destination: `${backend.origin}/api/:path*` },
    { source: '/(.*)', destination: '/index.html' },
  ],
};
