/** Request-local SQLite memory context. Python owns selection and byte limits. */
import { readFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  const contextFile = process.env.DJHX_MEMORY_CONTEXT_FILE;
  if (!contextFile) return;

  pi.on("context", (event) => {
    let text = "";
    try {
      const value = JSON.parse(readFileSync(contextFile, "utf8"));
      if (typeof value.text === "string") text = value.text;
    } catch {
      return;
    }
    if (!text) return;
    const messages = [...event.messages];
    let at = -1;
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === "user") { at = i; break; }
    }
    if (at < 0) return;
    messages.splice(at, 0, {
      role: "user",
      content: [{ type: "text", text }],
      timestamp: Date.now(),
    });
    return { messages };
  });
}
