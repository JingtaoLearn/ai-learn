require("dotenv").config();

const express = require("express");
const session = require("express-session");
const msal = require("@azure/msal-node");
const axios = require("axios");
const jwt = require("jsonwebtoken");
const path = require("path");

const app = express();
const PORT = process.env.PORT || 3000;

// MSAL configuration
const msalConfig = {
  auth: {
    clientId: process.env.AZURE_CLIENT_ID,
    authority: "https://login.microsoftonline.com/common",
    clientSecret: process.env.AZURE_CLIENT_SECRET,
  },
};

const REDIRECT_URI =
  process.env.AZURE_REDIRECT_URI ||
  "https://ms-login.ai.jingtao.fun/auth/callback";
const SCOPES = ["openid", "profile", "email", "User.Read"];
const AUTH_SHARED_SECRET = process.env.AUTH_SHARED_SECRET;
const DEFAULT_CALLBACK_URL =
  process.env.NOTE_APP_CALLBACK_URL ||
  "https://note.ai.jingtao.fun/auth/callback";

// Allowed callback URL patterns (whitelist)
const ALLOWED_CALLBACKS = (process.env.ALLOWED_CALLBACKS || "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);
// Always allow the default
if (DEFAULT_CALLBACK_URL) ALLOWED_CALLBACKS.push(DEFAULT_CALLBACK_URL);

function isAllowedCallback(url) {
  try {
    const parsed = new URL(url);
    return ALLOWED_CALLBACKS.some((allowed) => {
      try {
        const a = new URL(allowed);
        return parsed.origin === a.origin && parsed.pathname === a.pathname;
      } catch {
        return false;
      }
    });
  } catch {
    return false;
  }
}

const cca = new msal.ConfidentialClientApplication(msalConfig);

// View engine
app.set("view engine", "ejs");
app.set("views", path.join(__dirname, "views"));

// Trust proxy (behind nginx-proxy)
app.set("trust proxy", 1);

// Session middleware
app.use(
  session({
    secret: process.env.SESSION_SECRET || "change-me-in-production",
    resave: false,
    saveUninitialized: false,
    cookie: {
      secure: process.env.NODE_ENV === "production",
      httpOnly: true,
      maxAge: 1000 * 60 * 60, // 1 hour
    },
  })
);

// Landing page
app.get("/", (req, res) => {
  res.render("index", { user: req.session.user || null });
});

// Initiate Microsoft OAuth
app.get("/auth/login", async (req, res) => {
  try {
    // Store the caller's callback URL in session
    const redirect = req.query.redirect;
    if (redirect && isAllowedCallback(redirect)) {
      req.session.callbackUrl = redirect;
    } else {
      req.session.callbackUrl = DEFAULT_CALLBACK_URL;
    }

    const authCodeUrlParameters = {
      scopes: SCOPES,
      redirectUri: REDIRECT_URI,
    };

    const authUrl = await cca.getAuthCodeUrl(authCodeUrlParameters);
    res.redirect(authUrl);
  } catch (error) {
    console.error("Login error:", error);
    res.render("error", {
      message: "Failed to initiate login",
      detail: error.message,
    });
  }
});

// OAuth callback
app.get("/auth/callback", async (req, res) => {
  if (req.query.error) {
    return res.render("error", {
      message: req.query.error,
      detail: req.query.error_description || "Authentication was denied",
    });
  }

  try {
    const tokenRequest = {
      code: req.query.code,
      scopes: SCOPES,
      redirectUri: REDIRECT_URI,
    };

    const response = await cca.acquireTokenByCode(tokenRequest);

    // Fetch user profile from Microsoft Graph
    const graphResponse = await axios.get("https://graph.microsoft.com/v1.0/me", {
      headers: { Authorization: `Bearer ${response.accessToken}` },
    });

    const user = graphResponse.data;
    const email = user.mail || user.userPrincipalName;
    const displayName = user.displayName;

    req.session.user = {
      displayName,
      email,
    };

    // Generate JWT and redirect to note-app via auto-POST
    const token = jwt.sign(
      { email, displayName },
      AUTH_SHARED_SECRET,
      { expiresIn: "30s" }
    );

    const callbackUrl = req.session.callbackUrl || DEFAULT_CALLBACK_URL;
    res.send(`<!DOCTYPE html>
<html><body>
<form id="f" method="POST" action="${callbackUrl}">
  <input type="hidden" name="token" value="${token}" />
</form>
<script>document.getElementById("f").submit();</script>
<noscript>Click to continue: <button type="submit" form="f">Continue</button></noscript>
</body></html>`);
  } catch (error) {
    console.error("Callback error:", error);
    res.render("error", {
      message: "Authentication failed",
      detail: error.message,
    });
  }
});

// Logout
app.get("/logout", (req, res) => {
  req.session.destroy(() => {
    res.redirect("/");
  });
});

app.listen(PORT, () => {
  console.log(`MS Login app running on port ${PORT}`);
});
