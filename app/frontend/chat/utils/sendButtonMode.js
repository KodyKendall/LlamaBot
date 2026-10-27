/**
 * What the send button does right now.
 *
 * A message sent while Leo is working is queued for him — it no longer cancels
 * the run. So with text in the box the button sends (arrow icon), the same as
 * pressing Enter; only with an empty box does it become the red Stop button.
 * Stop stays the one way to cancel — including background research, which
 * keeps going after Leo's turn ends.
 */
export function sendButtonAction({ running, backgroundRunning = false, hasText }) {
  return (running || backgroundRunning) && !hasText ? 'stop' : 'send';
}
