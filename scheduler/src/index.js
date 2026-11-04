import { WorkerEntrypoint } from "cloudflare:workers";
import handler from "./handler.js";
import { runTick, probe } from "./runtime.js";

// Reachable only through a named service binding. No caller-supplied day or force.
export class BuilderTrigger extends WorkerEntrypoint {
  async tick() { return await runTick(this.env); }
  async probe() { return await probe(this.env); }
}

export default handler;
