// worker.js is fetched and hash-verified on the user's PC; its bytes stay intact.
import original from "./worker.js";
import { createRadarBridge } from "./bridge.js";
export * from "./worker.js";
export default createRadarBridge(original);
