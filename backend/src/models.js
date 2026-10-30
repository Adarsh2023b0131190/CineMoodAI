import mongoose from 'mongoose';

const userSchema = new mongoose.Schema({
  name: { type: String, required: true, trim: true, minlength: 1, maxlength: 80 },
  email: { type: String, required: true, trim: true, lowercase: true, maxlength: 254, unique: true, index: true },
  passwordHash: { type: String, required: true, select: false },
}, { timestamps: true });

const movieRatingSchema = new mongoose.Schema({
  userId: { type: mongoose.Schema.Types.ObjectId, ref: 'User', required: true },
  movieId: { type: Number, required: true, min: 1, validate: Number.isInteger },
  rating: { type: Number, required: true, min: 0.5, max: 5, validate: value => Number.isInteger(value * 2) },
}, { timestamps: true });
movieRatingSchema.index({ userId: 1, movieId: 1 }, { unique: true });

const sessionSchema = new mongoose.Schema({
  tokenHash: { type: String, required: true, unique: true, select: false },
  userId: { type: mongoose.Schema.Types.ObjectId, ref: 'User', required: true, index: true },
  expiresAt: { type: Date, required: true, index: { expires: 0 } },
}, { timestamps: true });

export function createModels(connection = mongoose) {
  return {
    User: connection.models.User || connection.model('User', userSchema),
    MovieRating: connection.models.MovieRating || connection.model('MovieRating', movieRatingSchema),
    Session: connection.models.Session || connection.model('Session', sessionSchema),
  };
}

export const { User, MovieRating, Session } = createModels();
