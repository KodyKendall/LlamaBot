/**
 * Screenshot Annotator Manager
 * Captures screen using getDisplayMedia and allows annotation with Fabric.js
 * Similar to video recording but captures a single frame for annotation
 */

export class ScreenshotAnnotator {
  constructor() {
    this.isCapturing = false;
    this.fabricCanvas = null;
    this.modal = null;
    this.currentTool = 'select'; // 'select', 'pen', 'rectangle', 'arrow', 'text', 'crop'
    this.currentColor = '#ff4444';
    this.brushWidth = 3;
    this.onAttachCallback = null;
    this.cropRect = null;
    this.isCropping = false;
    this.imageHistory = []; // Stack of previous image states for undo
  }

  /**
   * Capture a screenshot using getDisplayMedia
   * @returns {Promise<string>} - Data URL of the captured screenshot
   */
  async captureScreen() {
    const constraints = {
      audio: false,
      video: {
        width: { ideal: 1920 },
        height: { ideal: 1080 },
        frameRate: { ideal: 1 }
      }
    };

    const stream = await navigator.mediaDevices.getDisplayMedia(constraints);

    // Create video element to capture frame
    const video = document.createElement('video');
    video.srcObject = stream;
    video.muted = true;

    await new Promise((resolve) => {
      video.onloadedmetadata = () => {
        video.play();
        resolve();
      };
    });

    // Wait a frame for the video to render
    await new Promise(resolve => setTimeout(resolve, 100));

    // Create canvas to capture the frame
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;

    const ctx = canvas.getContext('2d');
    ctx.drawImage(video, 0, 0);

    // Stop the stream immediately after capture
    stream.getTracks().forEach(track => track.stop());

    // Convert to data URL
    return canvas.toDataURL('image/png');
  }

  /**
   * Start the screenshot capture and annotation flow
   * @param {Function} onAttach - Callback when user wants to attach the screenshot
   */
  async startCapture(onAttach) {
    if (this.isCapturing) return;

    this.isCapturing = true;
    this.onAttachCallback = onAttach;

    try {
      const screenshotDataUrl = await this.captureScreen();
      this.showAnnotationModal(screenshotDataUrl);
    } catch (err) {
      console.log('Screenshot cancelled or failed:', err.message);
      this.isCapturing = false;
      throw err;
    }
  }

  /**
   * Show the annotation modal with Fabric.js canvas
   * @param {string} imageDataUrl - The screenshot as a data URL
   */
  showAnnotationModal(imageDataUrl) {
    // Create modal
    this.modal = document.createElement('div');
    this.modal.className = 'screenshot-annotation-modal';
    this.modal.innerHTML = `
      <div class="annotation-content">
        <div class="annotation-header">
          <h3>Annotate Screenshot</h3>
          <div class="annotation-tools">
            <button class="tool-btn active" data-tool="select" title="Select/Move">
              <i class="fa-solid fa-arrow-pointer"></i>
            </button>
            <button class="tool-btn" data-tool="pen" title="Draw">
              <i class="fa-solid fa-pen"></i>
            </button>
            <button class="tool-btn" data-tool="rectangle" title="Rectangle">
              <i class="fa-regular fa-square"></i>
            </button>
            <button class="tool-btn" data-tool="arrow" title="Arrow">
              <i class="fa-solid fa-arrow-right"></i>
            </button>
            <button class="tool-btn" data-tool="text" title="Text">
              <i class="fa-solid fa-font"></i>
            </button>
            <button class="tool-btn" data-tool="crop" title="Crop">
              <i class="fa-solid fa-crop"></i>
            </button>
            <div class="tool-divider"></div>
            <input type="color" class="color-picker" value="#ff4444" title="Color">
            <button class="tool-btn" data-action="undo" title="Undo">
              <i class="fa-solid fa-rotate-left"></i>
            </button>
            <button class="tool-btn" data-action="clear" title="Clear All">
              <i class="fa-solid fa-trash"></i>
            </button>
            <div class="crop-actions hidden">
              <button class="tool-btn crop-apply" data-action="apply-crop" title="Apply Crop">
                <i class="fa-solid fa-check"></i>
              </button>
              <button class="tool-btn crop-cancel" data-action="cancel-crop" title="Cancel Crop">
                <i class="fa-solid fa-xmark"></i>
              </button>
            </div>
          </div>
        </div>
        <div class="annotation-canvas-container">
          <canvas id="annotation-canvas"></canvas>
        </div>
        <div class="annotation-actions">
          <button class="attach-btn">
            <i class="fa-solid fa-paperclip"></i> Attach to Message
          </button>
          <button class="download-btn">
            <i class="fa-solid fa-download"></i> Download
          </button>
          <button class="discard-btn">
            <i class="fa-solid fa-xmark"></i> Discard
          </button>
        </div>
      </div>
    `;

    document.body.appendChild(this.modal);

    // Initialize Fabric.js canvas
    this.initFabricCanvas(imageDataUrl);

    // Setup event listeners
    this.setupModalEvents();
  }

  /**
   * Initialize Fabric.js canvas with the screenshot as background
   * @param {string} imageDataUrl - The screenshot as a data URL
   */
  initFabricCanvas(imageDataUrl) {
    const canvasContainer = this.modal.querySelector('.annotation-canvas-container');
    const canvasEl = this.modal.querySelector('#annotation-canvas');

    // Load the image to get dimensions
    const img = new Image();
    img.onload = () => {
      // Calculate display size (fit within viewport)
      const maxWidth = window.innerWidth * 0.85;
      const maxHeight = window.innerHeight * 0.65;

      let displayWidth = img.width;
      let displayHeight = img.height;

      // Scale down if needed
      const scaleX = maxWidth / img.width;
      const scaleY = maxHeight / img.height;
      const scale = Math.min(scaleX, scaleY, 1);

      displayWidth = img.width * scale;
      displayHeight = img.height * scale;

      // Set canvas dimensions
      canvasEl.width = displayWidth;
      canvasEl.height = displayHeight;
      canvasContainer.style.width = `${displayWidth}px`;
      canvasContainer.style.height = `${displayHeight}px`;

      // Initialize Fabric canvas
      this.fabricCanvas = new fabric.Canvas('annotation-canvas', {
        width: displayWidth,
        height: displayHeight,
        selection: true,
        backgroundColor: '#1a1a1a'
      });

      // Store original dimensions for export
      this.originalWidth = img.width;
      this.originalHeight = img.height;
      this.displayScale = scale;

      // Set background image
      fabric.Image.fromURL(imageDataUrl, (fabricImg) => {
        fabricImg.scaleToWidth(displayWidth);
        this.fabricCanvas.setBackgroundImage(fabricImg, this.fabricCanvas.renderAll.bind(this.fabricCanvas));
      });

      // Set initial tool
      this.setTool('select');
    };
    img.src = imageDataUrl;
  }

  /**
   * Setup event listeners for the modal
   */
  setupModalEvents() {
    // Tool buttons
    this.modal.querySelectorAll('.tool-btn[data-tool]').forEach(btn => {
      btn.addEventListener('click', () => {
        this.setTool(btn.dataset.tool);
        this.modal.querySelectorAll('.tool-btn[data-tool]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
      });
    });

    // Color picker
    const colorPicker = this.modal.querySelector('.color-picker');
    colorPicker.addEventListener('input', (e) => {
      this.currentColor = e.target.value;
      if (this.fabricCanvas.isDrawingMode) {
        this.fabricCanvas.freeDrawingBrush.color = this.currentColor;
      }
    });

    // Undo button
    this.modal.querySelector('[data-action="undo"]').addEventListener('click', () => {
      this.undo();
    });

    // Clear button
    this.modal.querySelector('[data-action="clear"]').addEventListener('click', () => {
      this.clearAnnotations();
    });

    // Apply crop button
    this.modal.querySelector('[data-action="apply-crop"]').addEventListener('click', () => {
      this.applyCrop();
    });

    // Cancel crop button
    this.modal.querySelector('[data-action="cancel-crop"]').addEventListener('click', () => {
      this.cancelCrop();
    });

    // Attach button
    this.modal.querySelector('.attach-btn').addEventListener('click', () => {
      this.attachScreenshot();
    });

    // Download button
    this.modal.querySelector('.download-btn').addEventListener('click', () => {
      this.downloadScreenshot();
    });

    // Discard button
    this.modal.querySelector('.discard-btn').addEventListener('click', () => {
      this.closeModal();
    });

    // Close on backdrop click
    this.modal.addEventListener('click', (e) => {
      if (e.target === this.modal) {
        this.closeModal();
      }
    });

    // Escape key to close
    const escHandler = (e) => {
      if (e.key === 'Escape') {
        this.closeModal();
        document.removeEventListener('keydown', escHandler);
      }
    };
    document.addEventListener('keydown', escHandler);
  }

  /**
   * Set the current drawing tool
   * @param {string} tool - Tool name
   */
  setTool(tool) {
    this.currentTool = tool;

    if (!this.fabricCanvas) return;

    // Disable drawing mode by default
    this.fabricCanvas.isDrawingMode = false;
    this.fabricCanvas.selection = true;
    this.fabricCanvas.defaultCursor = 'default';

    // Remove any existing mouse handlers
    this.fabricCanvas.off('mouse:down');
    this.fabricCanvas.off('mouse:move');
    this.fabricCanvas.off('mouse:up');

    switch (tool) {
      case 'select':
        // Default selection mode
        break;

      case 'pen':
        this.fabricCanvas.isDrawingMode = true;
        this.fabricCanvas.freeDrawingBrush = new fabric.PencilBrush(this.fabricCanvas);
        this.fabricCanvas.freeDrawingBrush.color = this.currentColor;
        this.fabricCanvas.freeDrawingBrush.width = this.brushWidth;
        break;

      case 'rectangle':
        this.setupRectangleTool();
        break;

      case 'arrow':
        this.setupArrowTool();
        break;

      case 'text':
        this.setupTextTool();
        break;

      case 'crop':
        this.setupCropTool();
        break;
    }
  }

  /**
   * Setup rectangle drawing tool
   */
  setupRectangleTool() {
    let isDrawing = false;
    let startX, startY;
    let rect;

    this.fabricCanvas.selection = false;
    this.fabricCanvas.defaultCursor = 'crosshair';

    this.fabricCanvas.on('mouse:down', (opt) => {
      if (opt.target) return; // Don't start if clicking an object

      isDrawing = true;
      const pointer = this.fabricCanvas.getPointer(opt.e);
      startX = pointer.x;
      startY = pointer.y;

      rect = new fabric.Rect({
        left: startX,
        top: startY,
        width: 0,
        height: 0,
        fill: 'transparent',
        stroke: this.currentColor,
        strokeWidth: 3,
        selectable: true
      });
      this.fabricCanvas.add(rect);
    });

    this.fabricCanvas.on('mouse:move', (opt) => {
      if (!isDrawing || !rect) return;

      const pointer = this.fabricCanvas.getPointer(opt.e);

      if (pointer.x < startX) {
        rect.set({ left: pointer.x });
      }
      if (pointer.y < startY) {
        rect.set({ top: pointer.y });
      }

      rect.set({
        width: Math.abs(pointer.x - startX),
        height: Math.abs(pointer.y - startY)
      });

      this.fabricCanvas.renderAll();
    });

    this.fabricCanvas.on('mouse:up', () => {
      isDrawing = false;
      if (rect && rect.width < 5 && rect.height < 5) {
        this.fabricCanvas.remove(rect);
      }
      rect = null;
    });
  }

  /**
   * Setup arrow drawing tool
   */
  setupArrowTool() {
    let isDrawing = false;
    let startX, startY;
    let arrow;

    this.fabricCanvas.selection = false;
    this.fabricCanvas.defaultCursor = 'crosshair';

    this.fabricCanvas.on('mouse:down', (opt) => {
      if (opt.target) return;

      isDrawing = true;
      const pointer = this.fabricCanvas.getPointer(opt.e);
      startX = pointer.x;
      startY = pointer.y;
    });

    this.fabricCanvas.on('mouse:move', (opt) => {
      if (!isDrawing) return;

      const pointer = this.fabricCanvas.getPointer(opt.e);

      // Remove previous arrow while drawing
      if (arrow) {
        this.fabricCanvas.remove(arrow);
      }

      arrow = this.createArrow(startX, startY, pointer.x, pointer.y);
      this.fabricCanvas.add(arrow);
      this.fabricCanvas.renderAll();
    });

    this.fabricCanvas.on('mouse:up', () => {
      isDrawing = false;
      arrow = null;
    });
  }

  /**
   * Create an arrow shape
   */
  createArrow(x1, y1, x2, y2) {
    const headLength = 15;
    const angle = Math.atan2(y2 - y1, x2 - x1);

    const line = new fabric.Line([x1, y1, x2, y2], {
      stroke: this.currentColor,
      strokeWidth: 3,
      selectable: false
    });

    // Arrow head points
    const headX1 = x2 - headLength * Math.cos(angle - Math.PI / 6);
    const headY1 = y2 - headLength * Math.sin(angle - Math.PI / 6);
    const headX2 = x2 - headLength * Math.cos(angle + Math.PI / 6);
    const headY2 = y2 - headLength * Math.sin(angle + Math.PI / 6);

    const head = new fabric.Polygon([
      { x: x2, y: y2 },
      { x: headX1, y: headY1 },
      { x: headX2, y: headY2 }
    ], {
      fill: this.currentColor,
      selectable: false
    });

    // Group line and head
    return new fabric.Group([line, head], {
      selectable: true
    });
  }

  /**
   * Setup text tool
   */
  setupTextTool() {
    this.fabricCanvas.selection = false;
    this.fabricCanvas.defaultCursor = 'text';

    this.fabricCanvas.on('mouse:down', (opt) => {
      if (opt.target) return;

      const pointer = this.fabricCanvas.getPointer(opt.e);

      const text = new fabric.IText('Type here...', {
        left: pointer.x,
        top: pointer.y,
        fontSize: 20,
        fill: this.currentColor,
        fontFamily: 'Arial',
        selectable: true,
        editable: true
      });

      this.fabricCanvas.add(text);
      this.fabricCanvas.setActiveObject(text);
      text.enterEditing();
      text.selectAll();
      this.fabricCanvas.renderAll();

      // Switch to select mode after adding text
      this.setTool('select');
      this.modal.querySelector('[data-tool="select"]').classList.add('active');
      this.modal.querySelector('[data-tool="text"]').classList.remove('active');
    });
  }

  /**
   * Setup crop tool - draw a selection rectangle to crop the image
   */
  setupCropTool() {
    let isDrawing = false;
    let startX, startY;

    this.fabricCanvas.selection = false;
    this.fabricCanvas.defaultCursor = 'crosshair';
    this.isCropping = true;

    // Show crop action buttons
    const cropActions = this.modal.querySelector('.crop-actions');
    if (cropActions) cropActions.classList.remove('hidden');

    // Remove any existing crop rectangle
    if (this.cropRect) {
      this.fabricCanvas.remove(this.cropRect);
      this.cropRect = null;
    }

    this.fabricCanvas.on('mouse:down', (opt) => {
      if (opt.target && opt.target !== this.cropRect) return;

      // Remove previous crop rect when starting new selection
      if (this.cropRect) {
        this.fabricCanvas.remove(this.cropRect);
        this.cropRect = null;
      }

      isDrawing = true;
      const pointer = this.fabricCanvas.getPointer(opt.e);
      startX = pointer.x;
      startY = pointer.y;

      this.cropRect = new fabric.Rect({
        left: startX,
        top: startY,
        width: 0,
        height: 0,
        fill: 'rgba(0, 200, 255, 0.15)',
        stroke: '#00c8ff',
        strokeWidth: 2,
        strokeDashArray: [5, 5],
        selectable: false,
        evented: false,
        shadow: new fabric.Shadow({
          color: 'rgba(0, 0, 0, 0.5)',
          blur: 4,
          offsetX: 0,
          offsetY: 0
        })
      });
      this.fabricCanvas.add(this.cropRect);
    });

    this.fabricCanvas.on('mouse:move', (opt) => {
      if (!isDrawing || !this.cropRect) return;

      const pointer = this.fabricCanvas.getPointer(opt.e);

      let left = Math.min(startX, pointer.x);
      let top = Math.min(startY, pointer.y);
      let width = Math.abs(pointer.x - startX);
      let height = Math.abs(pointer.y - startY);

      this.cropRect.set({ left, top, width, height });
      this.fabricCanvas.renderAll();
    });

    this.fabricCanvas.on('mouse:up', () => {
      isDrawing = false;
      // Remove if too small
      if (this.cropRect && this.cropRect.width < 10 && this.cropRect.height < 10) {
        this.fabricCanvas.remove(this.cropRect);
        this.cropRect = null;
      }
    });
  }

  /**
   * Apply the crop to the image
   */
  applyCrop() {
    if (!this.cropRect) {
      alert('Please draw a crop selection first.');
      return;
    }

    const rect = this.cropRect;
    const left = rect.left;
    const top = rect.top;
    const width = rect.width;
    const height = rect.height;

    // Remove the crop rectangle
    this.fabricCanvas.remove(this.cropRect);
    this.cropRect = null;

    // Deselect everything
    this.fabricCanvas.discardActiveObject();

    // Save current state to history before cropping (for undo)
    const multiplier = 1 / this.displayScale;
    const currentStateDataUrl = this.fabricCanvas.toDataURL({
      format: 'png',
      quality: 1,
      multiplier: multiplier
    });
    this.imageHistory.push({
      dataUrl: currentStateDataUrl,
      width: this.originalWidth,
      height: this.originalHeight
    });

    // Export current state at original resolution
    const fullDataUrl = this.fabricCanvas.toDataURL({
      format: 'png',
      quality: 1,
      multiplier: multiplier
    });

    // Calculate crop coordinates at original resolution
    const cropLeft = left * multiplier;
    const cropTop = top * multiplier;
    const cropWidth = width * multiplier;
    const cropHeight = height * multiplier;

    // Create a temporary canvas to crop the image
    const img = new Image();
    img.onload = () => {
      const tempCanvas = document.createElement('canvas');
      tempCanvas.width = cropWidth;
      tempCanvas.height = cropHeight;
      const ctx = tempCanvas.getContext('2d');
      ctx.drawImage(img, cropLeft, cropTop, cropWidth, cropHeight, 0, 0, cropWidth, cropHeight);

      const croppedDataUrl = tempCanvas.toDataURL('image/png');

      // Reinitialize the canvas with cropped image
      this.reinitializeWithImage(croppedDataUrl, cropWidth, cropHeight);
    };
    img.src = fullDataUrl;
  }

  /**
   * Reinitialize canvas with a new image after cropping
   */
  reinitializeWithImage(imageDataUrl, originalWidth, originalHeight) {
    // Dispose old canvas
    this.fabricCanvas.dispose();

    const canvasContainer = this.modal.querySelector('.annotation-canvas-container');
    const canvasEl = this.modal.querySelector('#annotation-canvas');

    // Calculate new display size
    const maxWidth = window.innerWidth * 0.85;
    const maxHeight = window.innerHeight * 0.65;

    const scaleX = maxWidth / originalWidth;
    const scaleY = maxHeight / originalHeight;
    const scale = Math.min(scaleX, scaleY, 1);

    const displayWidth = originalWidth * scale;
    const displayHeight = originalHeight * scale;

    // Update canvas dimensions
    canvasEl.width = displayWidth;
    canvasEl.height = displayHeight;
    canvasContainer.style.width = `${displayWidth}px`;
    canvasContainer.style.height = `${displayHeight}px`;

    // Reinitialize Fabric canvas
    this.fabricCanvas = new fabric.Canvas('annotation-canvas', {
      width: displayWidth,
      height: displayHeight,
      selection: true,
      backgroundColor: '#1a1a1a'
    });

    // Update stored dimensions
    this.originalWidth = originalWidth;
    this.originalHeight = originalHeight;
    this.displayScale = scale;

    // Set new background image
    fabric.Image.fromURL(imageDataUrl, (fabricImg) => {
      fabricImg.scaleToWidth(displayWidth);
      this.fabricCanvas.setBackgroundImage(fabricImg, this.fabricCanvas.renderAll.bind(this.fabricCanvas));
    });

    // Exit crop mode
    this.cancelCrop();
  }

  /**
   * Cancel cropping and return to select mode
   */
  cancelCrop() {
    // Remove crop rectangle if exists
    if (this.cropRect) {
      this.fabricCanvas.remove(this.cropRect);
      this.cropRect = null;
    }

    this.isCropping = false;

    // Hide crop action buttons
    const cropActions = this.modal.querySelector('.crop-actions');
    if (cropActions) cropActions.classList.add('hidden');

    // Switch to select tool
    this.setTool('select');
    this.modal.querySelectorAll('.tool-btn[data-tool]').forEach(b => b.classList.remove('active'));
    this.modal.querySelector('[data-tool="select"]').classList.add('active');
  }

  /**
   * Undo last action (annotation or crop)
   */
  undo() {
    const objects = this.fabricCanvas.getObjects();

    // If there are annotations, remove the last one
    if (objects.length > 0) {
      this.fabricCanvas.remove(objects[objects.length - 1]);
      this.fabricCanvas.renderAll();
    }
    // If no annotations but we have crop history, undo the last crop
    else if (this.imageHistory.length > 0) {
      this.undoCrop();
    }
  }

  /**
   * Undo the last crop operation
   */
  undoCrop() {
    if (this.imageHistory.length === 0) return;

    const previousState = this.imageHistory.pop();
    this.reinitializeWithImage(previousState.dataUrl, previousState.width, previousState.height);
  }

  /**
   * Clear all annotations
   */
  clearAnnotations() {
    const objects = this.fabricCanvas.getObjects();
    objects.forEach(obj => this.fabricCanvas.remove(obj));
    this.fabricCanvas.renderAll();
  }

  /**
   * Get the final image with annotations as a blob
   * @returns {Promise<{blob: Blob, dataUrl: string}>}
   */
  async getAnnotatedImage() {
    // Deselect all objects to remove selection handles
    this.fabricCanvas.discardActiveObject();
    this.fabricCanvas.renderAll();

    // Export at original resolution for quality
    const multiplier = 1 / this.displayScale;
    const dataUrl = this.fabricCanvas.toDataURL({
      format: 'png',
      quality: 1,
      multiplier: multiplier
    });

    // Convert data URL to blob
    const response = await fetch(dataUrl);
    const blob = await response.blob();

    return { blob, dataUrl };
  }

  /**
   * Attach screenshot to message
   */
  async attachScreenshot() {
    try {
      const { blob, dataUrl } = await this.getAnnotatedImage();

      // Generate filename with timestamp
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
      const filename = `screenshot-${timestamp}.png`;

      // Convert to base64 for attachment
      const base64 = dataUrl.split(',')[1];

      if (this.onAttachCallback) {
        this.onAttachCallback({
          filename: filename,
          mime_type: 'image/png',
          data: base64,
          size: blob.size
        });
      }

      this.closeModal();
    } catch (err) {
      console.error('Failed to attach screenshot:', err);
      alert('Failed to attach screenshot. Please try again.');
    }
  }

  /**
   * Download the annotated screenshot
   */
  async downloadScreenshot() {
    try {
      const { blob } = await this.getAnnotatedImage();

      const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `screenshot-${timestamp}.png`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error('Failed to download screenshot:', err);
      alert('Failed to download screenshot. Please try again.');
    }
  }

  /**
   * Close the modal and cleanup
   */
  closeModal() {
    if (this.fabricCanvas) {
      this.fabricCanvas.dispose();
      this.fabricCanvas = null;
    }
    if (this.modal) {
      this.modal.remove();
      this.modal = null;
    }
    this.isCapturing = false;
    this.onAttachCallback = null;
  }
}

export default ScreenshotAnnotator;
