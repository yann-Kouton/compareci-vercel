import { jwtVerify, createRemoteJWKSet } from 'jose';
import crypto from 'crypto';

// Clés publiques de Google pour vérifier les tokens Firebase Auth (public, pas un secret)
const JWKS = createRemoteJWKSet(
  new URL('https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com')
);

const SUPER_ADMIN_EMAIL = 'esmelyann@gmail.com';

async function verifyFirebaseToken(idToken, projectId) {
  const { payload } = await jwtVerify(idToken, JWKS, {
    issuer: `https://securetoken.google.com/${projectId}`,
    audience: projectId,
  });
  return payload; // payload.sub = uid, payload.email = email
}

function cloudinarySignature(params, apiSecret) {
  const sorted = Object.keys(params)
    .sort()
    .map((k) => `${k}=${params[k]}`)
    .join('&');
  return crypto.createHash('sha1').update(sorted + apiSecret).digest('hex');
}

export default async function handler(req, res) {
  // CORS (nécessaire pour que l'app mobile puisse appeler cette route)
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');

  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'POST') return res.status(405).json({ error: 'Méthode non autorisée' });

  // 1. Vérifier l'identité via le token Firebase Auth envoyé par l'app
  const authHeader = req.headers.authorization || '';
  const idToken = authHeader.replace('Bearer ', '');
  if (!idToken) return res.status(401).json({ error: 'Non authentifié' });

  let payload;
  try {
    payload = await verifyFirebaseToken(idToken, process.env.FIREBASE_PROJECT_ID);
  } catch (e) {
    return res.status(401).json({ error: 'Token invalide' });
  }

  // 2. Lire la requête
  const { publicId, resourceType = 'image', ownerUid } = req.body || {};
  if (!publicId) return res.status(400).json({ error: 'publicId manquant' });

  // 3. Autorisation : le propriétaire de la ressource OU l'admin
  const uid = payload.sub;
  const email = payload.email;
  const authorized = uid === ownerUid || email === SUPER_ADMIN_EMAIL;
  if (!authorized) return res.status(403).json({ error: 'Non autorisé' });

  // 4. Suppression signée côté Cloudinary (la clé secrète reste ici, jamais côté client)
  const timestamp = Math.floor(Date.now() / 1000);
  const signature = cloudinarySignature(
    { public_id: publicId, timestamp },
    process.env.CLOUDINARY_API_SECRET
  );

  const cloudinaryBody = new URLSearchParams({
    public_id: publicId,
    timestamp: String(timestamp),
    api_key: process.env.CLOUDINARY_API_KEY,
    signature,
  });

  try {
    const cloudinaryRes = await fetch(
      `https://api.cloudinary.com/v1_1/${process.env.CLOUD_NAME}/${resourceType}/destroy`,
      { method: 'POST', body: cloudinaryBody }
    );
    const result = await cloudinaryRes.json();
    return res.status(200).json({ success: true, result });
  } catch (e) {
    return res.status(500).json({ error: 'Échec suppression Cloudinary' });
  }
}
