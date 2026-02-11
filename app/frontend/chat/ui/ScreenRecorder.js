/**
 * Screen Recorder Manager
 * Handles screen recording with low-res settings (720p @ 15fps, 1Mbps)
 * Uses browser's native getDisplayMedia for screen capture
 */

export class ScreenRecorder {
  constructor() {
    this.mediaRecorder = null;
    this.recordedChunks = [];
    this.stream = null;
    this.displayStream = null; // Screen capture stream
    this.micStream = null; // Microphone stream
    this.isRecording = false;
    this.startTime = null;
    this.timerInterval = null;
    this.onStopCallback = null;
  }

  /**
   * Start screen recording
   * @param {Function} onTimerUpdate - Callback with formatted time string (MM:SS)
   * @param {Function} onStop - Callback when recording stops (from user or stream end)
   */
  async startRecording(onTimerUpdate, onStop) {
    // Get screen capture (video only, no tab audio)
    const displayConstraints = {
      audio: false,
      video: {
        width: { ideal: 1280, max: 1280 },
        height: { ideal: 720, max: 720 },
        frameRate: { ideal: 15 }
      }
    };

    const displayStream = await navigator.mediaDevices.getDisplayMedia(displayConstraints);

    // Get microphone audio
    let micStream = null;
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    } catch (err) {
      console.warn('Microphone access denied, recording without audio:', err.message);
    }

    // Combine streams
    const tracks = [...displayStream.getVideoTracks()];
    if (micStream) {
      tracks.push(...micStream.getAudioTracks());
    }

    this.stream = new MediaStream(tracks);
    this.displayStream = displayStream; // Keep reference to stop later
    this.micStream = micStream;
    this.onStopCallback = onStop;

    const options = {
      mimeType: 'video/webm; codecs=vp9',
      videoBitsPerSecond: 1000000 // 1 Mbps
    };

    this.mediaRecorder = new MediaRecorder(this.stream, options);
    this.recordedChunks = [];

    this.mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) {
        this.recordedChunks.push(e.data);
      }
    };

    // Start timer
    this.startTime = Date.now();
    this.timerInterval = setInterval(() => {
      const elapsed = Math.floor((Date.now() - this.startTime) / 1000);
      const mins = String(Math.floor(elapsed / 60)).padStart(2, '0');
      const secs = String(elapsed % 60).padStart(2, '0');
      if (onTimerUpdate) {
        onTimerUpdate(`${mins}:${secs}`);
      }
    }, 1000);

    this.mediaRecorder.start();
    this.isRecording = true;

    // Handle stream ending (user clicks "Stop sharing" in browser UI)
    // Only listen on display stream video track since that's what "Stop sharing" affects
    this.displayStream.getVideoTracks().forEach(track => {
      track.onended = () => {
        this.stopRecording();
      };
    });
  }

  /**
   * Stop recording and return the blob
   * @returns {Promise<Blob|null>} The recorded video blob
   */
  stopRecording() {
    return new Promise((resolve) => {
      if (!this.mediaRecorder || this.mediaRecorder.state === 'inactive') {
        this.isRecording = false;
        resolve(null);
        return;
      }

      clearInterval(this.timerInterval);
      this.timerInterval = null;

      this.mediaRecorder.onstop = () => {
        const blob = new Blob(this.recordedChunks, { type: 'video/webm' });

        // Stop all streams
        this.stream?.getTracks().forEach(t => t.stop());
        this.displayStream?.getTracks().forEach(t => t.stop());
        this.micStream?.getTracks().forEach(t => t.stop());

        this.isRecording = false;
        this.recordedChunks = [];
        this.displayStream = null;
        this.micStream = null;

        if (this.onStopCallback) {
          this.onStopCallback(blob);
        }

        resolve(blob);
      };

      this.mediaRecorder.stop();
    });
  }

  /**
   * Show preview modal with video playback and download/discard options
   * @param {Blob} blob - The recorded video blob
   */
  showPreviewModal(blob) {
    if (!blob) return;

    const videoUrl = URL.createObjectURL(blob);

    // Create modal
    const modal = document.createElement('div');
    modal.className = 'recording-preview-modal';
    modal.innerHTML = `
      <div class="recording-preview-content">
        <h3>Recording Preview</h3>
        <video controls autoplay></video>
        <div class="recording-preview-actions">
          <button class="download-btn">
            <i class="fa-solid fa-download"></i> Download
          </button>
          <button class="discard-btn">
            <i class="fa-solid fa-trash"></i> Discard
          </button>
        </div>
      </div>
    `;

    // Set video src after adding to DOM to avoid autoplay issues
    const video = modal.querySelector('video');
    video.src = videoUrl;

    // Download handler
    modal.querySelector('.download-btn').addEventListener('click', () => {
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
      const a = document.createElement('a');
      a.href = videoUrl;
      a.download = `recording-${timestamp}.webm`;
      a.click();
      URL.revokeObjectURL(videoUrl);
      modal.remove();
    });

    // Discard handler
    modal.querySelector('.discard-btn').addEventListener('click', () => {
      URL.revokeObjectURL(videoUrl);
      modal.remove();
    });

    // Close on backdrop click
    modal.addEventListener('click', (e) => {
      if (e.target === modal) {
        URL.revokeObjectURL(videoUrl);
        modal.remove();
      }
    });

    document.body.appendChild(modal);
  }
}
