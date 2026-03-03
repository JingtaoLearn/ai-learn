const express = require("express");
const jwt = require("jsonwebtoken");
const session = require("express-session");
const path = require("path");

const app = express();
const PORT = process.env.PORT || 80;
const AUTH_SHARED_SECRET = process.env.AUTH_SHARED_SECRET;
const LOGIN_URL = process.env.LOGIN_URL || "https://ms-login-jingtao.azurewebsites.net/auth/login";
const SELF_CALLBACK = process.env.SELF_CALLBACK || "https://auth-demo.ai.jingtao.fun/auth/callback";

app.set("trust proxy", 1);

app.use(session({
  secret: process.env.SESSION_SECRET || "auth-demo-secret",
  resave: false,
  saveUninitialized: false,
  cookie: {
    secure: process.env.NODE_ENV === "production",
    httpOnly: true,
    maxAge: 1000 * 60 * 60 * 24,
  },
}));

app.use(express.urlencoded({ extended: true }));

// Auth callback - receives JWT from auth server
app.post("/auth/callback", (req, res) => {
  const { token } = req.body;
  if (!token) return res.status(400).send("Missing token");

  try {
    const payload = jwt.verify(token, AUTH_SHARED_SECRET);
    req.session.user = { email: payload.email, displayName: payload.displayName };
    res.redirect("/");
  } catch (err) {
    console.error("JWT verification failed:", err.message);
    res.status(401).send("Invalid or expired token");
  }
});

// Logout
app.get("/auth/logout", (req, res) => {
  req.session.destroy(() => res.redirect("/"));
});

// Main page - requires auth
app.get("/", (req, res) => {
  if (!req.session.user) {
    const redirect = encodeURIComponent(SELF_CALLBACK);
    return res.redirect(`${LOGIN_URL}?redirect=${redirect}`);
  }

  const { email, displayName } = req.session.user;
  res.send(`<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Auth Demo</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
    .card { background: #1e293b; border-radius: 16px; padding: 48px; max-width: 480px; width: 90%; text-align: center; box-shadow: 0 25px 50px rgba(0,0,0,0.3); }
    .badge { display: inline-block; background: #22c55e; color: #fff; font-size: 12px; font-weight: 600; padding: 4px 12px; border-radius: 999px; margin-bottom: 24px; text-transform: uppercase; letter-spacing: 1px; }
    .avatar { width: 80px; height: 80px; background: #3b82f6; border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto 20px; font-size: 32px; font-weight: 700; color: #fff; }
    h1 { font-size: 24px; margin-bottom: 8px; }
    .email { color: #94a3b8; font-size: 14px; margin-bottom: 32px; }
    .info { background: #0f172a; border-radius: 8px; padding: 16px; margin-bottom: 24px; text-align: left; font-size: 13px; line-height: 2; }
    .info .label { color: #64748b; }
    .info .value { color: #e2e8f0; float: right; }
    .logout { display: inline-block; background: #ef4444; color: #fff; text-decoration: none; padding: 10px 24px; border-radius: 8px; font-size: 14px; font-weight: 500; transition: background 0.2s; }
    .logout:hover { background: #dc2626; }
    .footer { margin-top: 24px; font-size: 12px; color: #475569; }
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">✓ Authenticated</div>
    <div class="avatar">${(displayName || email)[0].toUpperCase()}</div>
    <h1>${displayName || "User"}</h1>
    <p class="email">${email}</p>
    <div class="info">
      <div><span class="label">Display Name</span><span class="value">${displayName || "N/A"}</span></div>
      <div><span class="label">Email</span><span class="value">${email}</span></div>
      <div><span class="label">Auth Method</span><span class="value">Microsoft OAuth (via Proxy)</span></div>
      <div><span class="label">Session</span><span class="value">24h cookie</span></div>
    </div>
    <a href="/auth/logout" class="logout">Sign Out</a>
    <p class="footer">Auth Demo — Microsoft OAuth Proxy Prototype</p>
  </div>
</body>
</html>`);
});

app.listen(PORT, () => console.log(`Auth Demo running on port ${PORT}`));
