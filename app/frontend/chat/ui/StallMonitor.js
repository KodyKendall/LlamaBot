/** Time-based state for the generic "no inbound activity" affordance. */
export class StallMonitor {
  constructor({ thresholdMs = 90000, now = () => Date.now() } = {}) {
    this.thresholdMs = thresholdMs;
    this.now = now;
    this.lastInboundAt = null;
  }

  start() {
    this.lastInboundAt = this.now();
  }

  markActivity() {
    this.lastInboundAt = this.now();
  }

  stop() {
    this.lastInboundAt = null;
  }

  shouldWarn(isAgentRunning) {
    return Boolean(
      isAgentRunning &&
      this.lastInboundAt !== null &&
      this.now() - this.lastInboundAt >= this.thresholdMs
    );
  }
}
