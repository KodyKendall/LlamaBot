/**
 * Manages cycling loading verbs for the thinking indicator
 */

export class LoadingVerbs {
  constructor() {
    this.verbs = [
      "Frolicking",
      "Dreaming",
      "Cyborging",
      "Llama'ing",
      "Grazing",
      "Thinking",
      "Working",
      "Cogitating",
      "Circuiting",
      "Llaminating",
      "Synapsing",
      "Daydreaming",
      "Scheming",
      "Bootstrapping",
      "Nibbling",
      "Cog-smirking",
      "Ruminating",
      "Algorithming",
      "Pixelating",
      "Woolgathering",
      "Subprocess-ing",
      "Cache-napping",
      "Idea-herding",
      "Glitch-dancing",
      "Zen-loading",
      "Overfitting",
      "Refactoring",
      "Quantum-idling",
      "Bit-munching",
      "Syntax-twirling",
      "Buffer-pondering",
      "Thread-spinning",
      "GPU-sighing",
      "Re-prompting",
      "Meta-ruminating",
      "Token-tickling",
      "Server-stretching"
    ];
    this.intervalId = null;
  }

  /**
   * Get a random verb from the list
   */
  getRandomVerb() {
    return this.verbs[Math.floor(Math.random() * this.verbs.length)];
  }

  /**
   * Start cycling through verbs for an element
   * @param {HTMLElement} element - The element to update
   * @param {number} interval - Time between changes in milliseconds (default 5000)
   */
  startCycling(element, interval = 5000) {
    if (!element) return;

    // Set initial random verb
    element.textContent = `🦙 ${this.getRandomVerb()}...`;

    // Stop any existing interval
    this.stopCycling();

    // Start new interval
    this.intervalId = setInterval(() => {
      element.textContent = `🦙 ${this.getRandomVerb()}...`;
    }, interval);
  }

  /**
   * Stop the cycling interval
   */
  stopCycling() {
    if (this.intervalId) {
      clearInterval(this.intervalId);
      this.intervalId = null;
    }
  }

  /**
   * Get a static verb for one-time use
   */
  static getStaticVerb() {
    const verbs = [
      "Frolicking",
      "Dreaming",
      "Cyborging",
      "Llama'ing",
      "Grazing",
      "Thinking",
      "Working",
      "Cogitating",
      "Circuiting",
      "Llaminating",
      "Synapsing",
      "Daydreaming",
      "Scheming",
      "Bootstrapping",
      "Nibbling",
      "Cog-smirking",
      "Ruminating",
      "Algorithming",
      "Pixelating",
      "Woolgathering",
      "Subprocess-ing",
      "Cache-napping",
      "Idea-herding",
      "Glitch-dancing",
      "Zen-loading",
      "Overfitting",
      "Refactoring",
      "Quantum-idling",
      "Bit-munching",
      "Syntax-twirling",
      "Buffer-pondering",
      "Thread-spinning",
      "GPU-sighing",
      "Re-prompting",
      "Meta-ruminating",
      "Token-tickling",
      "Server-stretching"
    ];
    return verbs[Math.floor(Math.random() * verbs.length)];
  }
}
