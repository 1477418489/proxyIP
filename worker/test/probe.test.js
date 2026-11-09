import assert from "node:assert/strict";
import test from "node:test";
import { createHandler, publicAddress } from "../src/probe.js";

const TOKEN = "a-test-token-with-at-least-32-characters";
const pause = () => new Promise((resolve) => setImmediate(resolve));

class FakeWebSocket {
  constructor() {
    this.listeners = new Map();
    this.sent = [];
    this.closed = [];
  }
  accept() { this.accepted = true; }
  send(data) { this.sent.push(data); }
  close(code, reason) { this.closed.push({ code, reason }); }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  emit(type, event = {}) { this.listeners.get(type)?.(event); }
}

function fixture({ hangConnect = false, failConnect = false, blockWrites = false } = {}) {
  const websocket = new FakeWebSocket();
  const received = [];
  let readController;
  let writeController;
  let resolveWrite;
  let closeCount = 0;
  let timerCallback;
  let timerCleared = false;
  let connectCount = 0;
  const socket = {
    opened: hangConnect ? new Promise(() => {}) : failConnect ? Promise.reject(new Error("connection refused")) : Promise.resolve(),
    closed: new Promise(() => {}),
    readable: new ReadableStream({ start(controller) { readController = controller; } }),
    writable: new WritableStream({
      start(controller) { writeController = controller; },
      async write(data) {
        received.push(new Uint8Array(data));
        if (blockWrites) await new Promise((resolve) => { resolveWrite = resolve; });
      },
    }),
    close() {
      closeCount += 1;
      try { readController.close(); } catch { /* already closed */ }
      resolveWrite?.();
      try { writeController.error(new Error("closed")); } catch { /* already closed */ }
      return Promise.resolve();
    },
  };
  const fetch = createHandler({
    connect(address) { connectCount += 1; assert.equal(address.hostname, "8.8.8.8"); return socket; },
    makePair: () => ({ 0: {}, 1: websocket }),
    makeResponse: (body, options) => ({ body, ...options }),
    setTimer(callback, duration) { assert.equal(duration, 25_000); timerCallback = callback; return 1; },
    clearTimer() { timerCleared = true; },
    now: (() => { let value = 0; return () => { value += 5; return value; }; })(),
  });
  return {
    fetch, websocket, received,
    incoming: (bytes) => readController.enqueue(bytes),
    eof: () => readController.close(),
    timeout: () => timerCallback(),
    get closeCount() { return closeCount; },
    get connectCount() { return connectCount; },
    get timerCleared() { return timerCleared; },
    get socket() { return socket; },
  };
}

function request({ token = TOKEN, query = "ip=8.8.8.8&port=443", upgrade = "websocket" } = {}) {
  const value = new Request(`https://probe.example/probe?${query}`, {
    headers: { Authorization: `Bearer ${token}`, Upgrade: upgrade },
  });
  Object.defineProperty(value, "cf", { value: { colo: "HKG" } });
  return value;
}

test("strict IP boundaries", () => {
  for (const address of ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "2001:4860:4860::8888"]) {
    assert.equal(publicAddress(address), address);
  }
  for (const address of [
    "localhost", "127.0.0.1", "10.0.0.1", "100.64.0.1", "169.254.169.254", "172.16.1.1",
    "192.168.1.1", "192.0.0.9", "192.0.2.1", "192.88.99.1", "198.18.1.1", "198.51.100.1",
    "203.0.113.1", "224.0.0.1", "255.255.255.255", "0.0.0.0", "127.1", "0x7f000001",
    "0177.0.0.1", "::1", "::", "fc00::1", "fe80::1", "fe80::1%eth0", "ff02::1", "::ffff:8.8.8.8",
    "2001:db8::1", "2001::1", "2002:7f00:1::", "3fff::1", "3ffe::1", "100::1",
  ]) assert.throws(() => publicAddress(address), undefined, address);
});

test("auth and input failures do not open TCP", async () => {
  const f = fixture();
  assert.equal((await f.fetch(request(), {})).status, 503);
  assert.equal((await f.fetch(request({ token: "incorrect" }), { PROBE_TOKEN: TOKEN })).status, 401);
  assert.equal((await f.fetch(request({ upgrade: "no" }), { PROBE_TOKEN: TOKEN })).status, 426);
  for (const query of ["ip=127.0.0.1&port=443", "ip=8.8.8.8&port=0", "ip=8.8.8.8&port=25", "ip=8.8.8.8&port=65536", "ip=8.8.8.8&port=443&ip=1.1.1.1"]) {
    assert.equal((await f.fetch(request({ query }), { PROBE_TOKEN: TOKEN })).status, 400);
  }
  assert.equal(f.connectCount, 0);
});

test("ready precedes bytes, TCP forwarding and client close clean up", async () => {
  const f = fixture();
  const response = await f.fetch(request(), { PROBE_TOKEN: TOKEN });
  assert.equal(response.status, 101);
  assert.deepEqual(JSON.parse(f.websocket.sent[0]), {
    type: "ready", protocol: 1, candidate: "8.8.8.8:443", colo: "HKG", connect_ms: 5,
  });
  f.websocket.emit("message", { data: Uint8Array.of(1, 2, 3).buffer });
  f.incoming(Uint8Array.of(4, 5));
  await pause();
  assert.deepEqual([...f.received[0]], [1, 2, 3]);
  assert.deepEqual([...f.websocket.sent[1]], [4, 5]);
  f.websocket.emit("close");
  await pause();
  assert.equal(f.closeCount, 1);
  assert.equal(f.timerCleared, true);
  assert.equal(f.socket.readable.locked, false);
  assert.equal(f.socket.writable.locked, false);
});

test("text frames and oversized frames close TCP", async () => {
  for (const data of ["not binary", new Uint8Array(65 * 1024)]) {
    const f = fixture();
    await f.fetch(request(), { PROBE_TOKEN: TOKEN });
    f.websocket.emit("message", { data });
    await pause();
    assert.equal(f.closeCount, 1);
    assert.ok([1003, 1009].includes(f.websocket.closed[0].code));
  }
});

test("pending TCP writes have a bounded queue", async () => {
  const f = fixture({ blockWrites: true });
  await f.fetch(request(), { PROBE_TOKEN: TOKEN });
  f.websocket.emit("message", { data: new Uint8Array(40 * 1024) });
  await pause();
  f.websocket.emit("message", { data: new Uint8Array(40 * 1024) });
  await pause();
  assert.equal(f.closeCount, 1);
  assert.equal(f.websocket.closed[0].code, 1009);
});

test("aggregate byte budget is enforced", async () => {
  const f = fixture();
  await f.fetch(request(), { PROBE_TOKEN: TOKEN });
  for (let i = 0; i < 5; i++) {
    f.incoming(new Uint8Array(60 * 1024));
    await pause();
  }
  assert.equal(f.closeCount, 1);
  assert.equal(f.websocket.closed[0].code, 1009);
  assert.equal(f.websocket.sent.length, 5); // ready plus four bounded frames
});

test("deadline closes an established tunnel", async () => {
  const f = fixture();
  await f.fetch(request(), { PROBE_TOKEN: TOKEN });
  f.timeout();
  await pause();
  assert.equal(f.closeCount, 1);
  assert.equal(f.websocket.closed[0].code, 1008);
});

test("deadline also closes a pending TCP connection", async () => {
  const f = fixture({ hangConnect: true });
  const pending = f.fetch(request(), { PROBE_TOKEN: TOKEN });
  f.timeout();
  assert.equal((await pending).status, 502);
  assert.equal(f.closeCount, 1);
});

test("connection refusal and EOF release resources", async () => {
  const failed = fixture({ failConnect: true });
  assert.equal((await failed.fetch(request(), { PROBE_TOKEN: TOKEN })).status, 502);
  assert.equal(failed.closeCount, 1);
  const f = fixture();
  await f.fetch(request(), { PROBE_TOKEN: TOKEN });
  f.eof();
  await pause();
  assert.equal(f.closeCount, 1);
  assert.equal(f.socket.readable.locked, false);
});
