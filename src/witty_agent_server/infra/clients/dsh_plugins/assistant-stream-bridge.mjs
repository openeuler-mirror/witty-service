/**
 * Relay dsh's process-local agent/assistant-stream frames to the SDK client
 * as witty.assistant.stream notifications. Subagent sessions are skipped;
 * forked root sessions (parentSession lineage) are not.
 */

export const name = 'witty-assistant-stream-bridge';

function writeNotification(payload) {
  try {
    process.stdout.write(`${JSON.stringify(payload)}\n`);
  } catch {
    // stdout is the SDK transport; a write failure means shutdown.
  }
}

/**
 * @param {object} ctx - dsh Cordis context.
 */
export function apply(ctx) {
  ctx.on('agent/assistant-stream', ({ agent, frame }) => {
    const session = agent?.session;
    if (session === undefined || frame === undefined) return;
    const header = session.header;
    if (header?.origin === 'subagent' || (header?.delegationDepth ?? 0) > 0) return;
    writeNotification({
      jsonrpc: '2.0',
      method: 'witty.assistant.stream',
      params: { sessionId: String(session.id), frame },
    });
  });
}
