const assert = require("node:assert/strict");
const { test } = require("node:test");

const jwt = require("jsonwebtoken");

const { createApp } = require("../app");

const DEFAULT_CALLBACK = "https://note.example/auth/callback";
const QUANT_CALLBACK = "https://quant.example/auth/callback";
const AUTH_SHARED_SECRET = "test-auth-shared-secret-with-32-bytes";

function createTestApp({
  callbacks = {
    [DEFAULT_CALLBACK]: "note-app",
    [QUANT_CALLBACK]: "quant-app",
  },
  allowedCallbacks = [QUANT_CALLBACK],
  graphRequest = async () => ({
    mail: "person@example.com",
    displayName: "Example Person",
  }),
  logger = { error() {} },
  nodeEnv = "test",
} = {}) {
  const msalClient = {
    async getAuthCodeUrl() {
      return "https://login.microsoftonline.com/authorize";
    },
    async acquireTokenByCode() {
      return { accessToken: "graph-access-token" };
    },
  };

  return createApp({
    env: {
      NODE_ENV: nodeEnv,
      SESSION_SECRET: "test-session-secret-with-32-bytes",
      AUTH_SHARED_SECRET,
      AZURE_REDIRECT_URI: "https://login.example/auth/callback",
      NOTE_APP_CALLBACK_URL: DEFAULT_CALLBACK,
      ALLOWED_CALLBACKS: allowedCallbacks.join(","),
      DOWNSTREAM_CLIENTS: JSON.stringify(callbacks),
    },
    msalClient,
    graphRequest,
    logger,
  });
}

async function withServer(app, callback) {
  const server = app.listen(0, "127.0.0.1");
  await new Promise((resolve, reject) => {
    server.once("listening", resolve);
    server.once("error", reject);
  });

  try {
    const { port } = server.address();
    await callback(`http://127.0.0.1:${port}`);
  } finally {
    await new Promise((resolve, reject) => {
      server.close((error) => (error ? reject(error) : resolve()));
    });
  }
}

async function beginLogin(baseUrl, redirect, headers = {}) {
  const query = redirect ? `?redirect=${encodeURIComponent(redirect)}` : "";
  const response = await fetch(`${baseUrl}/auth/login${query}`, {
    headers,
    redirect: "manual",
  });
  const setCookie = response.headers.get("set-cookie");

  return {
    response,
    cookie: setCookie && setCookie.split(";", 1)[0],
    setCookie,
  };
}

async function completeLogin(baseUrl, cookie) {
  return fetch(`${baseUrl}/auth/callback?code=test-code`, {
    headers: { Cookie: cookie },
    redirect: "manual",
  });
}

function extractAutoPost(body) {
  const action = body.match(/<form id="f" method="POST" action="([^"]+)">/);
  const token = body.match(/<input type="hidden" name="token" value="([^"]+)" \/>/);
  assert.ok(action, "auto-POST form action is present");
  assert.ok(token, "auto-POST token is present");
  return { action: action[1], token: token[1] };
}

test("binds an exactly allowed quant callback to its configured audience", async () => {
  await withServer(createTestApp(), async (baseUrl) => {
    const login = await beginLogin(baseUrl, QUANT_CALLBACK);
    assert.equal(login.response.status, 302);
    assert.equal(
      login.response.headers.get("location"),
      "https://login.microsoftonline.com/authorize"
    );

    const response = await completeLogin(baseUrl, login.cookie);
    assert.equal(response.status, 200);
    const { action, token } = extractAutoPost(await response.text());
    assert.equal(action, QUANT_CALLBACK);

    const decoded = jwt.verify(token, AUTH_SHARED_SECRET, {
      algorithms: ["HS256"],
      audience: "quant-app",
    });
    assert.equal(decoded.email, "person@example.com");
    assert.equal(decoded.displayName, "Example Person");
    assert.equal(decoded.aud, "quant-app");
    assert.ok(decoded.exp - decoded.iat >= 29 && decoded.exp - decoded.iat <= 30);
  });
});

test("does not use an unknown callback and preserves the default consumer", async () => {
  const unknownCallback = "https://attacker.example/auth/callback";

  await withServer(createTestApp(), async (baseUrl) => {
    const login = await beginLogin(baseUrl, unknownCallback);
    assert.equal(login.response.status, 302);

    const response = await completeLogin(baseUrl, login.cookie);
    const { action, token } = extractAutoPost(await response.text());
    assert.equal(action, DEFAULT_CALLBACK);
    assert.doesNotMatch(action, /attacker/);
    assert.equal(
      jwt.verify(token, AUTH_SHARED_SECRET, {
        algorithms: ["HS256"],
        audience: "note-app",
      }).aud,
      "note-app"
    );
  });
});

test("rejects allowlisted callbacks that have no downstream audience", async () => {
  await withServer(
    createTestApp({
      callbacks: { [DEFAULT_CALLBACK]: "note-app" },
      allowedCallbacks: [QUANT_CALLBACK],
    }),
    async (baseUrl) => {
      const response = await beginLogin(baseUrl, QUANT_CALLBACK);
      assert.equal(response.response.status, 500);
      assert.equal(response.response.headers.get("location"), null);
      assert.doesNotMatch(await response.response.text(), /quant\.example/);
    }
  );
});

test("escapes the configured auto-POST action", async () => {
  const callback =
    'https://client.example/auth/callback?next="><script>alert(1)</script>';

  await withServer(
    createTestApp({
      callbacks: { [callback]: "client-app" },
      allowedCallbacks: [callback],
    }),
    async (baseUrl) => {
      const login = await beginLogin(baseUrl, callback);
      const response = await completeLogin(baseUrl, login.cookie);
      const body = await response.text();

      assert.match(
        body,
        /action="https:\/\/client\.example\/auth\/callback\?next=&quot;&gt;&lt;script&gt;alert\(1\)&lt;\/script&gt;"/
      );
      assert.doesNotMatch(body, /<script>alert\(1\)<\/script>/);
    }
  );
});

test("sets hardened production session cookies and auth response headers", async () => {
  await withServer(
    createTestApp({ nodeEnv: "production" }),
    async (baseUrl) => {
      const { response, setCookie } = await beginLogin(baseUrl, QUANT_CALLBACK, {
        "X-Forwarded-Proto": "https",
      });

      assert.match(setCookie, /;\s*Secure/i);
      assert.match(setCookie, /;\s*HttpOnly/i);
      assert.match(setCookie, /;\s*SameSite=Lax/i);
      assert.equal(response.headers.get("cache-control"), "no-store");
      assert.equal(response.headers.get("pragma"), "no-cache");
      assert.equal(response.headers.get("x-content-type-options"), "nosniff");
      assert.equal(response.headers.get("x-frame-options"), "DENY");
      assert.equal(response.headers.get("referrer-policy"), "no-referrer");
    }
  );
});

test("requires production session and signing secrets", () => {
  const commonEnv = {
    NODE_ENV: "production",
    NOTE_APP_CALLBACK_URL: DEFAULT_CALLBACK,
    DOWNSTREAM_CLIENTS: JSON.stringify({ [DEFAULT_CALLBACK]: "note-app" }),
  };

  assert.throws(
    () => createApp({ env: commonEnv }),
    /SESSION_SECRET is required in production/
  );
  assert.throws(
    () =>
      createApp({
        env: { ...commonEnv, SESSION_SECRET: "configured-session-secret" },
      }),
    /AUTH_SHARED_SECRET is required in production/
  );
});

test("does not expose or log access tokens and secrets on callback errors", async () => {
  const logged = [];
  const logger = {
    error(...args) {
      logged.push(args.join(" "));
    },
  };
  const sensitiveError =
    `request failed with graph-access-token and ${AUTH_SHARED_SECRET}`;

  await withServer(
    createTestApp({
      nodeEnv: "production",
      graphRequest: async () => {
        throw new Error(sensitiveError);
      },
      logger,
    }),
    async (baseUrl) => {
      const login = await beginLogin(baseUrl, QUANT_CALLBACK, {
        "X-Forwarded-Proto": "https",
      });
      const response = await completeLogin(baseUrl, login.cookie);
      const body = await response.text();

      assert.equal(response.status, 500);
      assert.doesNotMatch(body, /graph-access-token/);
      assert.doesNotMatch(body, new RegExp(AUTH_SHARED_SECRET));
      assert.doesNotMatch(logged.join("\n"), /graph-access-token/);
      assert.doesNotMatch(logged.join("\n"), new RegExp(AUTH_SHARED_SECRET));
    }
  );
});
