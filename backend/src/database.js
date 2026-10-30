import mongoose from 'mongoose';

export async function connectDatabase(uri = process.env.MONGODB_URI, options = {}) {
  if (!uri || typeof uri !== 'string' || !uri.trim()) {
    throw new Error('MONGODB_URI is required. Set it in backend/.env before starting the API.');
  }
  try {
    await mongoose.connect(uri, { serverSelectionTimeoutMS: 5000, ...options });
    await mongoose.connection.db.admin().command({ ping: 1 });
    return mongoose.connection;
  } catch (error) {
    await mongoose.connection.close().catch(() => {});
    const reason = /ServerSelection/.test(error?.name || '')
      ? 'MongoDB could not be reached. Confirm the MongoDB Community Server is running and MONGODB_URI points to it.'
      : 'MongoDB connection failed. Check MONGODB_URI and the database server configuration.';
    throw new Error(reason, { cause: error });
  }
}
