import { isIP } from "node:net";

const DURATION_MS = 25_000;
const BYTE_LIMIT = 256 * 1024;
const FRAME_LIMIT = 64 * 1024;
const QUEUE_LIMIT = 64 * 1024;

const IPV4_DENY = [
  ["0.0.0.0", 8], ["10.0.0.0", 8], ["100.64.0.0", 10],
  ["127.0.0.0", 8], ["169.254.0.0", 16], ["172.16.0.0", 12],
  ["192.0.0.0", 24], ["192.0.2.0", 24], ["192.88.99.0", 24],
  ["192.168.0.0", 16], ["198.18.0.0", 15], ["198.51.100.0", 24],
  ["203.0.113.0", 24], ["224.0.0.0", 4], ["240.0.0.0", 4],
].map(([address, prefix]) => [ipv4Number(address), prefix]);

function ipv4Number(address) {
  return address.split(".").reduce((value, octet) => value * 256 + Number(octet), 0) >>> 0;
}

function ipv6Words(address) {
  const [left, right] = address.split("::");
  const start = left ? left.split(":") : [];
  const end = right ? right.split(":") : [];
  const words = right === undefined ? start : [...start, ...Array(8 - start.length - end.length).fill("0"), ...end];
  return words.map((word) => parseInt(word, 16));
}

export function publicAddress(input) {
  if (typeof input !== "string" || input.length > 45 || input.includes("%")) {
    throw new Error("Invalid IP address");
  }
  const family = isIP(input);
  if (family === 4) {
    const value = ipv4Number(input);
    for (const [network, prefix] of IPV4_DENY) {
      const shift = 32 - prefix;
      if ((value >>> shift) === (network >>> shift)) throw new Error("Reserved IPv4 address");
    }
  } else if (family === 6) {
    if (input.includes(".")) throw new Error("Embedded IPv4 is not supported");
    const [first, second] = ipv6Words(input);
    if ((first & 0xe000) !== 0x2000) {
      throw new Error("Only global unicast IPv6 addresses are allowed");
    }
    if (
      (first === 0x2001 && (second & 0xfe00) === 0)
      || (first === 0x2001 && second === 0x0db8)
      || first === 0x2002
      || first === 0x3ffe
      || (first === 0x3fff && (second & 0xf000) === 0)
    ) {
      throw new Error("Reserved IPv6 address");
    }
  } else {
    throw new Error("Invalid IP literal");
  }
  return input.toLowerCase();
}

function tokenMatches(actual, expected) {
  const left = new TextEncoder().encode(actual);
  const right = new TextEncoder().encode(expected);
  let difference = left.length ^ right.length;
  for (let index = 0; index < Math.max(left.length, right.length); index++) {
    difference |= (left[index] ?? 0) ^ (right[index] ?? 0);
  }
  return difference === 0;
}

function safely(operation) {
  try {
    return Promise.resolve(operation());
  } catch (error) {
    return Promise.reject(error);
  }
}

// Dependency injection is only used by the offline lifecycle tests. The
// deployed entry point supplies the actual cloudflare:sockets connect().
export function createHandler({
  connect,
  makePair = () => new WebSocketPair(),
  makeResponse = (body, options) => new Response(body, options),
  now = () => Date.now(),
  setTimer = (callback, duration) => setTimeout(callback, duration),
  clearTimer = (timer) => clearTimeout(timer),
}) {
  return async function fetch(request, env) {
    const reply = (status, message) =>
      makeResponse(JSON.stringify({ error: message }), {
        status,
        headers: { "content-type": "application/json", "cache-control": "no-store" },
      });
    const url = new URL(request.url);
    if (url.pathname !== "/probe") return reply(404, "Not found");
    if (request.method !== "GET") return reply(405, "GET required");
    if (typeof env.PROBE_TOKEN !== "string" || env.PROBE_TOKEN.length < 32) {
      return reply(503, "Configure a PROBE_TOKEN of at least 32 characters");
    }
    const authorization = request.headers.get("authorization") ?? "";
    if (authorization.length > 512 || !tokenMatches(authorization, `Bearer ${env.PROBE_TOKEN}`)) {
      return reply(401, "Unauthorized");
    }
    if ((request.headers.get("upgrade") ?? "").toLowerCase() !== "websocket") {
      return reply(426, "WebSocket upgrade required");
    }
    let ip;
    let port;
    try {
      if (url.searchParams.getAll("ip").length !== 1 || url.searchParams.getAll("port").length !== 1) {
        throw new Error("One ip and port are required");
      }
      ip = publicAddress(url.searchParams.get("ip"));
      const rawPort = url.searchParams.get("port");
      if (!/^[1-9][0-9]{0,4}$/.test(rawPort)) throw new Error("Invalid port");
      port = Number(rawPort);
      if (port > 65535 || port === 25) throw new Error("Invalid port");
    } catch {
      return reply(400, "A public IP literal and valid port are required");
    }

    let socket;
    let websocket;
    let reader;
    let writer;
    let stopped = false;
    let timer;
    let bytes = 0;
    let queuedBytes = 0;
    let writes = Promise.resolve();
    let timeoutReject;
    const deadline = new Promise((_, reject) => { timeoutReject = reject; });
    const shutdown = async (code = 1000, reason = "Closed") => {
      if (stopped) return;
      stopped = true;
      clearTimer(timer);
      try { websocket?.close(code, reason); } catch { /* already closed */ }
      // Closing the socket first unblocks pending reads and writes, including
      // a writer currently stalled behind the remote peer.
      await Promise.allSettled([
        safely(() => socket?.close()),
        safely(() => reader?.cancel()),
        safely(() => writer?.abort()),
      ]);
      try { reader?.releaseLock(); } catch { /* read still unwinding */ }
      try { writer?.releaseLock(); } catch { /* write still unwinding */ }
    };
    timer = setTimer(() => {
      timeoutReject(new Error("Probe deadline exceeded"));
      void shutdown(1008, "Time limit");
    }, DURATION_MS);

    const reserveBytes = (length) => {
      bytes += length;
      if (bytes > BYTE_LIMIT) {
        void shutdown(1009, "Byte limit");
        return false;
      }
      return true;
    };

    try {
      const started = now();
      socket = connect({ hostname: ip, port });
      // Socket.closed can reject before .opened. Attach the observer now.
      socket.closed.catch(() => { void shutdown(1011, "TCP error"); });
      await Promise.race([socket.opened, deadline]);
      if (stopped) throw new Error("Socket closed while connecting");
      const connectMs = Math.max(0, now() - started);
      const pair = makePair();
      websocket = pair[1];
      websocket.accept();
      reader = socket.readable.getReader();
      writer = socket.writable.getWriter();
      websocket.addEventListener("close", () => { void shutdown(); });
      websocket.addEventListener("error", () => { void shutdown(1011, "WebSocket error"); });
      websocket.addEventListener("message", (event) => {
        if (stopped) return;
        const value = event.data;
        const data = value instanceof ArrayBuffer
          ? new Uint8Array(value)
          : ArrayBuffer.isView(value)
            ? new Uint8Array(value.buffer, value.byteOffset, value.byteLength)
            : null;
        if (data === null) {
          void shutdown(1003, "Binary frames required");
          return;
        }
        if (data.byteLength > FRAME_LIMIT || queuedBytes + data.byteLength > QUEUE_LIMIT) {
          void shutdown(1009, "Frame or queue limit");
          return;
        }
        if (!reserveBytes(data.byteLength)) return;
        queuedBytes += data.byteLength;
        writes = writes.then(async () => {
          try {
            if (!stopped) await writer.write(data);
          } finally {
            queuedBytes -= data.byteLength;
          }
        }).catch(() => { void shutdown(1011, "TCP write failed"); });
      });

      const candidate = ip.includes(":") ? `[${ip}]:${port}` : `${ip}:${port}`;
      websocket.send(JSON.stringify({
        type: "ready", protocol: 1, candidate,
        colo: /^[A-Z]{3}$/.test(request.cf?.colo ?? "") ? request.cf.colo : "ZZZ",
        connect_ms: connectMs,
      }));
      void (async () => {
        try {
          while (!stopped) {
            const { value, done } = await reader.read();
            if (stopped) break;
            if (done) {
              await shutdown(1000, "TCP EOF");
              break;
            }
            if (!reserveBytes(value.byteLength)) break;
            for (let offset = 0; offset < value.byteLength && !stopped; offset += FRAME_LIMIT) {
              websocket.send(value.slice(offset, offset + FRAME_LIMIT));
            }
          }
        } catch {
          await shutdown(1011, "TCP read failed");
        }
      })();
      return makeResponse(null, { status: 101, webSocket: pair[0] });
    } catch {
      await shutdown(1011, "TCP connect failed");
      return reply(502, "Candidate TCP connection failed");
    }
  };
}
