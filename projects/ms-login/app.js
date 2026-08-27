const {
  createHash,
  randomBytes,
  timingSafeEqual,
} = require("node:crypto");
const path = require("path");

const msal = require("@azure/msal-node");
const axios = require("axios");
const express = require("express");
const session = require("express-session");
const jwt = require("jsonwebtoken");

const SCOPES = ["openid", "profile", "email", "User.Read"];
const DEFAULT_REDIRECT_URI =
  "https://ms-login.ai.jingtao.fun/auth/callback";
const DEFAULT_CALLBACK_URL =
  "https://note.ai.jingtao.fun/auth/callback";
const OAUTH_STATE_TTL_MS = 5 * 60 * 1000;
const MAX_OUTSTANDING_OAUTH_STATES = 5;
const MAX_GLOBAL_OAUTH_STATES = 1000;

function parseDownstreamClients(value) {
  const clients = JSON.parse(value || "{}");
  if (!clients || Array.isArray(clients) || typeof clients !== "object") {
    throw new Error("DOWNSTREAM_CLIENTS must be a JSON object");
  }
  return clients;
}

function readConfig(env) {
  const isProduction = env.NODE_ENV === "production";
  if (isProduction && !env.SESSION_SECRET) {
    throw new Error("SESSION_SECRET is required in production");
  }
  if (isProduction && !env.AUTH_SHARED_SECRET) {
    throw new Error("AUTH_SHARED_SECRET is required in production");
  }

  const defaultCallbackUrl =
    env.NOTE_APP_CALLBACK_URL || DEFAULT_CALLBACK_URL;
  const allowedCallbacks = new Set(
    (env.ALLOWED_CALLBACKS || "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean)
  );
  allowedCallbacks.add(defaultCallbackUrl);

  return {
    nodeEnv: env.NODE_ENV,
    port: env.PORT || 3000,
    sessionSecret:
      env.SESSION_SECRET || "development-only-session-secret",
    authSharedSecret: env.AUTH_SHARED_SECRET,
    redirectUri: env.AZURE_REDIRECT_URI || DEFAULT_REDIRECT_URI,
    defaultCallbackUrl,
    allowedCallbacks,
    downstreamClients: parseDownstreamClients(env.DOWNSTREAM_CLIENTS),
    msalConfig: {
      auth: {
        clientId: env.AZURE_CLIENT_ID,
        authority: "https://login.microsoftonline.com/common",
        clientSecret: env.AZURE_CLIENT_SECRET,
      },
    },
  };
}

function isSafeCallback(callbackUrl) {
  try {
    const parsed = new URL(callbackUrl);
    return parsed.protocol === "https:" || parsed.protocol === "http:";
  } catch {
    return false;
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function digestState(state) {
  return createHash("sha256").update(state).digest();
}

function resolveLimit(value, fallback) {
  return Number.isInteger(value) && value > 0 ? value : fallback;
}

async function requestGraphProfile(accessToken) {
  const response = await axios.get(
    "https://graph.microsoft.com/v1.0/me",
    {
      headers: { Authorization: `Bearer ${accessToken}` },
    }
  );
  return response.data;
}

function createApp({
  env = process.env,
  msalClient,
  graphRequest = requestGraphProfile,
  logger = console,
  maxOAuthStates = MAX_GLOBAL_OAUTH_STATES,
  maxOAuthStatesPerSession = MAX_OUTSTANDING_OAUTH_STATES,
  now = Date.now,
  sessionStore,
} = {}) {
  const config = readConfig(env);
  const globalStateLimit = resolveLimit(
    maxOAuthStates,
    MAX_GLOBAL_OAUTH_STATES
  );
  const perSessionStateLimit = resolveLimit(
    maxOAuthStatesPerSession,
    MAX_OUTSTANDING_OAUTH_STATES
  );
  const client =
    msalClient ||
    new msal.ConfidentialClientApplication(config.msalConfig);
  const app = express();
  // Session snapshots must never be able to restore consumed states.
  const oauthStateRegistry = new Map();

  app.set("view engine", "ejs");
  app.set("views", path.join(__dirname, "views"));
  app.set("trust proxy", 1);

  app.use((req, res, next) => {
    res.set({
      "Cache-Control": "no-store",
      Pragma: "no-cache",
      "Referrer-Policy": "no-referrer",
      "X-Content-Type-Options": "nosniff",
      "X-Frame-Options": "DENY",
    });
    next();
  });

  app.use(
    session({
      secret: config.sessionSecret,
      ...(sessionStore ? { store: sessionStore } : {}),
      resave: false,
      saveUninitialized: false,
      cookie: {
        secure: config.nodeEnv === "production",
        httpOnly: true,
        sameSite: "lax",
        maxAge: 1000 * 60 * 60,
      },
    })
  );

  function renderError(res, status, message) {
    return res.status(status).render("error", {
      message,
      detail: null,
    });
  }

  function getBoundCallback(requestedCallback) {
    if (
      requestedCallback &&
      config.allowedCallbacks.has(requestedCallback) &&
      isSafeCallback(requestedCallback)
    ) {
      return {
        callbackUrl: requestedCallback,
        audience: config.downstreamClients[requestedCallback],
      };
    }

    return {
      callbackUrl: config.defaultCallbackUrl,
      audience:
        config.downstreamClients[config.defaultCallbackUrl],
    };
  }

  function purgeExpiredOAuthStates(currentTime) {
    for (const [key, flow] of oauthStateRegistry) {
      if (
        !Number.isFinite(flow.issuedAt) ||
        !Number.isFinite(flow.expiresAt) ||
        currentTime < flow.issuedAt ||
        currentTime >= flow.expiresAt
      ) {
        oauthStateRegistry.delete(key);
      }
    }
  }

  function registerOAuthFlow(sessionID, callbackUrl, audience) {
    const issuedAt = now();
    purgeExpiredOAuthStates(issuedAt);

    const sessionEntries = [...oauthStateRegistry].filter(
      ([, flow]) => flow.sessionID === sessionID
    );
    while (sessionEntries.length >= perSessionStateLimit) {
      const [key] = sessionEntries.shift();
      oauthStateRegistry.delete(key);
    }
    while (oauthStateRegistry.size >= globalStateLimit) {
      const oldestKey = oauthStateRegistry.keys().next().value;
      oauthStateRegistry.delete(oldestKey);
    }

    let state;
    let stateDigest;
    let key;
    do {
      state = randomBytes(32).toString("base64url");
      stateDigest = digestState(state);
      key = stateDigest.toString("hex");
    } while (oauthStateRegistry.has(key));

    const flow = {
      stateDigest,
      sessionID,
      callbackUrl,
      audience,
      issuedAt,
      expiresAt: issuedAt + OAUTH_STATE_TTL_MS,
    };
    oauthStateRegistry.set(key, flow);
    return { flow, key, state };
  }

  function consumeOAuthFlow(sessionID, providedState) {
    const currentTime = now();
    purgeExpiredOAuthStates(currentTime);

    if (
      typeof providedState !== "string" ||
      providedState.length === 0
    ) {
      return null;
    }

    const providedDigest = digestState(providedState);
    const key = providedDigest.toString("hex");
    const flow = oauthStateRegistry.get(key);
    if (
      !flow ||
      !timingSafeEqual(providedDigest, flow.stateDigest) ||
      flow.sessionID !== sessionID
    ) {
      return null;
    }

    oauthStateRegistry.delete(key);
    return flow;
  }

  app.get("/", (req, res) => {
    res.render("index", { user: req.session.user || null });
  });

  app.get("/auth/login", async (req, res) => {
    try {
      const binding = getBoundCallback(req.query.redirect);
      if (
        !binding.audience ||
        !config.allowedCallbacks.has(binding.callbackUrl) ||
        !isSafeCallback(binding.callbackUrl)
      ) {
        throw new Error("Downstream audience is not configured");
      }

      const registeredState = registerOAuthFlow(
        req.sessionID,
        binding.callbackUrl,
        binding.audience
      );
      let authUrl;
      try {
        authUrl = await client.getAuthCodeUrl({
          scopes: SCOPES,
          redirectUri: config.redirectUri,
          state: registeredState.state,
        });
      } catch (error) {
        if (
          oauthStateRegistry.get(registeredState.key) ===
          registeredState.flow
        ) {
          oauthStateRegistry.delete(registeredState.key);
        }
        throw error;
      }
      req.session.oauthSession = true;
      res.redirect(authUrl);
    } catch {
      logger.error("Login request failed");
      renderError(res, 500, "Failed to initiate login");
    }
  });

  app.get("/auth/callback", async (req, res) => {
    const flow = consumeOAuthFlow(req.sessionID, req.query.state);
    if (!flow) {
      logger.error("OAuth callback rejected");
      return renderError(res, 400, "Authentication failed");
    }
    if (req.query.error) {
      return renderError(res, 400, "Authentication was denied");
    }

    try {
      const { callbackUrl, audience } = flow;
      if (
        !callbackUrl ||
        !audience ||
        !config.allowedCallbacks.has(callbackUrl) ||
        config.downstreamClients[callbackUrl] !== audience ||
        !isSafeCallback(callbackUrl)
      ) {
        throw new Error("Downstream callback is not configured");
      }
      if (
        typeof req.query.code !== "string" ||
        req.query.code.length === 0
      ) {
        return renderError(res, 400, "Authentication failed");
      }

      const response = await client.acquireTokenByCode({
        code: req.query.code,
        scopes: SCOPES,
        redirectUri: config.redirectUri,
      });
      const user = await graphRequest(response.accessToken);
      const email = user.mail || user.userPrincipalName;
      const displayName = user.displayName;

      req.session.user = { displayName, email };

      const token = jwt.sign(
        { email, displayName },
        config.authSharedSecret,
        {
          algorithm: "HS256",
          audience,
          expiresIn: "30s",
        }
      );

      res.send(`<!DOCTYPE html>
<html><body>
<form id="f" method="POST" action="${escapeHtml(callbackUrl)}">
  <input type="hidden" name="token" value="${escapeHtml(token)}" />
</form>
<script>document.getElementById("f").submit();</script>
<noscript>Click to continue: <button type="submit" form="f">Continue</button></noscript>
</body></html>`);
    } catch {
      logger.error("OAuth callback failed");
      renderError(res, 500, "Authentication failed");
    }
  });

  app.get("/logout", (req, res) => {
    req.session.destroy(() => {
      res.redirect("/");
    });
  });

  return app;
}

function start() {
  require("dotenv").config();
  const app = createApp();
  const port = process.env.PORT || 3000;
  app.listen(port, () => {
    console.log(`MS Login app running on port ${port}`);
  });
}

if (require.main === module) {
  try {
    start();
  } catch (error) {
    console.error(`MS Login startup failed: ${error.message}`);
    process.exitCode = 1;
  }
}

module.exports = { createApp };
