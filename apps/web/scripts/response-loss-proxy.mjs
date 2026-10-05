// Response-loss staging proxy for save-recovery acceptance.
//
// Sits between the web app (API_INTERNAL_URL) and the real backend and can
// swallow save responses in two precisely scoped ways, so the browser can be
// driven through REAL recovery flows without touching the running stack:
//
//   pass              forward everything unchanged.
//   lose-after-commit matching save POSTs are FULLY forwarded — the backend
//                     processes and commits — and only then is the client
//                     socket destroyed: the browser sees a network failure
//                     even though the save landed ("已提交但响应丢失").
//   lose-before-send  matching save POSTs are destroyed before contacting the
//                     backend: nothing is committed, outcome unknown.
//
// Only POST /notes/{id}/revisions and POST /encounters/{id}/student-notes are
// affected; verification lookups (GET) always pass through.
//
// Usage: node scripts/response-loss-proxy.mjs [listenPort=8001] [targetPort=8000]
// Mode switch: curl -X POST http://127.0.0.1:<listen>/__mode -d '"lose-after-commit"'
//              (body is one of "pass" | "lose-after-commit" | "lose-before-send")
import http from "node:http";

const LISTEN_PORT = Number(process.argv[2] ?? 8001);
const TARGET_HOST = "127.0.0.1";
const TARGET_PORT = Number(process.argv[3] ?? 8000);
const SAVE_PATH = /\/(notes\/[0-9a-f-]+\/revisions|encounters\/[0-9a-f-]+\/student-notes)$/;
const MODES = new Set(["pass", "lose-after-commit", "lose-before-send"]);

let mode = "pass";

const passThrough = (req, res) => {
  const upstream = http.request(
    {
      host: TARGET_HOST,
      port: TARGET_PORT,
      path: req.url,
      method: req.method,
      headers: { ...req.headers, host: `${TARGET_HOST}:${TARGET_PORT}` },
    },
    (up) => {
      res.writeHead(up.statusCode, up.headers);
      up.pipe(res);
    },
  );
  upstream.on("error", () => res.destroy());
  req.pipe(upstream);
};

const loseAfterCommit = (req, res) => {
  // Fully forwarded: the backend processes and commits the save. The
  // upstream response is then consumed and the CLIENT socket is destroyed
  // without writing anything — the browser sees a network failure.
  const upstream = http.request(
    {
      host: TARGET_HOST,
      port: TARGET_PORT,
      path: req.url,
      method: req.method,
      headers: { ...req.headers, host: `${TARGET_HOST}:${TARGET_PORT}` },
    },
    (up) => {
      up.resume();
      up.on("end", () => res.destroy());
    },
  );
  upstream.on("error", () => res.destroy());
  req.pipe(upstream);
};

const loseBeforeSend = (req, res) => {
  // The request never leaves the proxy: nothing can be committed, and the
  // client socket dies with no response — outcome unknown to the app.
  req.resume();
  res.destroy();
};

http
  .createServer((req, res) => {
    const pathOnly = req.url.split("?")[0];
    if (pathOnly === "/__mode") {
      res.writeHead(200, { "content-type": "application/json" });
      if (req.method === "POST") {
        let body = "";
        req.on("data", (chunk) => (body += chunk));
        req.on("end", () => {
          try {
            const requested = JSON.parse(body);
            if (MODES.has(requested)) {
              mode = requested;
            } else {
              res.writeHead(400);
              res.end(JSON.stringify({ error: `mode must be one of ${[...MODES].join(", ")}` }));
              return;
            }
          } catch {
            res.writeHead(400);
            res.end(JSON.stringify({ error: "body must be a JSON string" }));
            return;
          }
          res.end(JSON.stringify({ mode }));
        });
        return;
      }
      res.end(JSON.stringify({ mode }));
      return;
    }
    if (req.method === "POST" && SAVE_PATH.test(pathOnly) && mode !== "pass") {
      (mode === "lose-after-commit" ? loseAfterCommit : loseBeforeSend)(req, res);
      return;
    }
    passThrough(req, res);
  })
  .listen(LISTEN_PORT, "127.0.0.1", () => {
    console.log(`response-loss proxy 127.0.0.1:${LISTEN_PORT} -> ${TARGET_HOST}:${TARGET_PORT} (mode: ${mode})`);
  });
