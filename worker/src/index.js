import { connect } from "cloudflare:sockets";
import { createHandler } from "./probe.js";

export default {
  fetch: createHandler({ connect }),
};
