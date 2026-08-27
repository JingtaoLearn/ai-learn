const {
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

function statesEqual(providedState, expectedState) {
  const provided = Buffer.from(providedState);
  const expected = Buffer.from(expectedState);
  return (
    provided.length === expected.length &&
    timingSafeEqual(provided, expected)
  );
}

function activeOAuthFlows(flows, currentTime) {
  if (!flows || Array.isArray(flows) || typeof flows !== "object") {
    return {};
  }

  return Object.fromEntries(
    Object.entries(flows).filter(([, flow]) => {
      const age = currentTime - flow?.issuedAt;
      return (
        Number.isFinite(flow?.issuedAt) &&
        age >= 0 &&
        age <= OAUTH_STATE_TTL_MS
      );
    })
  );
}

function saveSession(req) {
  return new Promise((resolve, reject) => {
    req.session.save((error) => {
      if (error) reject(error);
      else resolve();
    });
  });
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
  now = Date.now,
} = {}) {
  const config = readConfig(env);
  const client =
    msalClient ||
    new msal.ConfidentialClientApplication(config.msalConfig);
  const app = express();

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

  function consumeOAuthFlow(req, providedState) {
    if (
      typeof providedState !== "string" ||
      providedState.length === 0
    ) {
      return { consumed: false, flow: null };
    }

    const flows = req.session.oauthFlows;
    if (!flows || Array.isArray(flows) || typeof flows !== "object") {
      return { consumed: false, flow: null };
    }

    const storedState = Object.keys(flows).find((candidate) =>
      statesEqual(providedState, candidate)
    );
    if (!storedState) {
      return { consumed: false, flow: null };
    }

    const flow = flows[storedState];
    delete flows[storedState];
    req.session.oauthFlows = flows;

    const age = now() - flow?.issuedAt;
    if (
      !Number.isFinite(flow?.issuedAt) ||
      age < 0 ||
      age > OAUTH_STATE_TTL_MS
    ) {
      return { consumed: true, flow: null };
    }

    return { consumed: true, flow };
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

      const issuedAt = now();
      const flows = activeOAuthFlows(
        req.session.oauthFlows,
        issuedAt
      );
      while (
        Object.keys(flows).length >=
        MAX_OUTSTANDING_OAUTH_STATES
      ) {
        delete flows[Object.keys(flows)[0]];
      }
      const state = randomBytes(32).toString("base64url");

      const authUrl = await client.getAuthCodeUrl({
        scopes: SCOPES,
        redirectUri: config.redirectUri,
        state,
      });
      flows[state] = {
        issuedAt,
        callbackUrl: binding.callbackUrl,
        audience: binding.audience,
      };
      req.session.oauthFlows = flows;
      res.redirect(authUrl);
    } catch {
      logger.error("Login request failed");
      renderError(res, 500, "Failed to initiate login");
    }
  });

  app.get("/auth/callback", async (req, res) => {
    const stateResult = consumeOAuthFlow(req, req.query.state);
    if (stateResult.consumed) {
      try {
        await saveSession(req);
      } catch {
        logger.error("OAuth state consumption failed");
        return renderError(res, 500, "Authentication failed");
      }
    }
    if (!stateResult.flow) {
      logger.error("OAuth callback rejected");
      return renderError(res, 400, "Authentication failed");
    }
    if (req.query.error) {
      return renderError(res, 400, "Authentication was denied");
    }

    try {
      const { callbackUrl, audience } = stateResult.flow;
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
