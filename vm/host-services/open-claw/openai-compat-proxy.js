#!/usr/bin/env node
// OpenAI-compatible proxy that flattens content arrays to strings
// for endpoints that don't support the multimodal content format.
//
// OpenClaw sends: {"content": [{"type":"text","text":"hello"}]}
// Endpoint expects: {"content": "hello"}

const http = require('http');
const https = require('https');

const UPSTREAM = process.env.S_LLM_API_URL || '';
const PORT = parseInt(process.env.PROXY_PORT || '19999', 10);

if (!UPSTREAM) {
  console.error('[proxy] S_LLM_API_URL is not set. Exiting.');
  process.exit(1);
}

function flattenContent(messages) {
  if (!Array.isArray(messages)) return messages;
  return messages.map(msg => {
    if (Array.isArray(msg.content)) {
      // Extract text parts only, join them
      const textParts = msg.content
        .filter(p => p.type === 'text')
        .map(p => p.text);
      return { ...msg, content: textParts.join('\n') };
    }
    return msg;
  });
}

const server = http.createServer((req, res) => {
  let body = '';
  req.on('data', chunk => body += chunk);
  req.on('end', () => {
    let payload = body;
    try {
      const parsed = JSON.parse(body);
      if (parsed.messages) {
        parsed.messages = flattenContent(parsed.messages);
      }
      payload = JSON.stringify(parsed);
    } catch {}

    const url = new URL(UPSTREAM + req.url);
    const headers = { ...req.headers, host: url.hostname, 'content-length': Buffer.byteLength(payload) };
    delete headers['transfer-encoding'];

    const fwdReq = https.request({
      hostname: url.hostname,
      port: url.port || 443,
      path: url.pathname + url.search,
      method: req.method,
      headers,
    }, fwdRes => {
      res.writeHead(fwdRes.statusCode, fwdRes.headers);
      fwdRes.pipe(res);
    });

    fwdReq.on('error', e => {
      console.error('[proxy] upstream error:', e.message);
      res.writeHead(502);
      res.end('proxy error');
    });
    fwdReq.end(payload);
  });
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[proxy] listening on 127.0.0.1:${PORT}, upstream: ${UPSTREAM}`);
});
