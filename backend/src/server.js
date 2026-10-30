import 'dotenv/config';
import { createApp } from './app.js';
import { connectDatabase } from './database.js';

const port = Number(process.env.PORT || 5000);
const app = createApp();

try {
  await connectDatabase();
  app.listen(port, () => console.log(`CineMood API listening on http://localhost:${port} (MongoDB connected)`));
} catch (error) {
  console.error(`CineMood API startup failed: ${error.message}`);
  process.exitCode = 1;
}
