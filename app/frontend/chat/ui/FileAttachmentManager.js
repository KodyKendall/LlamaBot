/**
 * FileAttachmentManager - Handles file attachments for chat messages
 *
 * Supports PDF and image files, encoding them as base64 for transmission
 * over WebSocket to LangChain agents.
 */

const MAX_FILE_SIZE_MB = 250;
const MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024;

const ALLOWED_TYPES = {
  'application/pdf': { ext: 'pdf', icon: 'fa-file-pdf' },
  'image/png': { ext: 'png', icon: 'fa-file-image' },
  'image/jpeg': { ext: 'jpg', icon: 'fa-file-image' },
  'image/gif': { ext: 'gif', icon: 'fa-file-image' },
  'image/webp': { ext: 'webp', icon: 'fa-file-image' },
  'video/webm': { ext: 'webm', icon: 'fa-file-video' },
  'video/mp4': { ext: 'mp4', icon: 'fa-file-video' },
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': { ext: 'xlsx', icon: 'fa-file-excel' },
  'application/vnd.ms-excel': { ext: 'xls', icon: 'fa-file-excel' },
  'text/csv': { ext: 'csv', icon: 'fa-file-csv' },
};

export class FileAttachmentManager {
  constructor() {
    this.attachments = [];
    this.fileInput = null;
    this.uploadFileInput = null;
    this.attachButton = null;
    this.previewContainer = null;
    this.attachMenu = null;
  }

  /**
   * Initialize the file attachment manager
   * @param {HTMLElement} attachButton - The button that triggers file selection
   * @param {HTMLInputElement} fileInput - The hidden file input element
   * @param {HTMLElement} previewContainer - Container for attachment previews
   */
  init(attachButton, fileInput, previewContainer) {
    this.attachButton = attachButton;
    this.fileInput = fileInput;
    this.previewContainer = previewContainer;

    if (!this.attachButton || !this.fileInput) {
      console.warn('FileAttachmentManager: Missing required elements');
      return;
    }

    // Handle file selection (attach for AI)
    this.fileInput.addEventListener('change', (e) => {
      this.handleFileSelect(e.target.files);
    });
  }

  /**
   * Initialize the upload-to-assets flow
   * @param {HTMLElement} menu - The dropdown menu element
   * @param {HTMLElement} attachForAiBtn - Button to attach for AI
   * @param {HTMLElement} uploadToAssetsBtn - Button to upload to assets
   * @param {HTMLInputElement} uploadFileInput - Hidden file input for asset uploads
   */
  initUploadMenu(menu, attachForAiBtn, uploadToAssetsBtn, uploadFileInput) {
    this.attachMenu = menu;
    this.uploadFileInput = uploadFileInput;

    if (!menu || !attachForAiBtn || !uploadToAssetsBtn || !uploadFileInput) {
      // Fallback: if menu elements missing, use old direct-click behavior
      if (this.attachButton && this.fileInput) {
        this.attachButton.addEventListener('click', () => {
          this.fileInput.click();
        });
      }
      return;
    }

    // Click paperclip -> toggle menu
    this.attachButton.addEventListener('click', (e) => {
      e.stopPropagation();
      this.toggleMenu();
    });

    // "Attach for AI" -> open file picker for LLM attachments
    attachForAiBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      this.hideMenu();
      this.fileInput.click();
    });

    // "Upload to Assets" -> open file picker for asset upload
    uploadToAssetsBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      this.hideMenu();
      this.uploadFileInput.click();
    });

    // Handle upload file selection
    this.uploadFileInput.addEventListener('change', async (e) => {
      await this.handleUploadToAssets(e.target.files);
      this.uploadFileInput.value = '';
    });

    // Close menu when clicking outside
    document.addEventListener('click', (e) => {
      if (this.attachMenu && !this.attachMenu.classList.contains('hidden')) {
        if (!this.attachMenu.contains(e.target) && !this.attachButton.contains(e.target)) {
          this.hideMenu();
        }
      }
    });
  }

  toggleMenu() {
    if (!this.attachMenu) return;
    this.attachMenu.classList.toggle('hidden');
  }

  hideMenu() {
    if (!this.attachMenu) return;
    this.attachMenu.classList.add('hidden');
  }

  /**
   * Initialize the file browser panel for browsing existing uploaded files
   */
  initFileBrowser(browseBtn, panel, listEl, closeBtn) {
    this.fileBrowserPanel = panel;
    this.fileBrowserList = listEl;

    if (!browseBtn || !panel || !listEl) return;

    browseBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      this.hideMenu();
      this.toggleFileBrowser();
    });

    if (closeBtn) {
      closeBtn.addEventListener('click', () => this.hideFileBrowser());
    }

    // Close when clicking outside
    document.addEventListener('click', (e) => {
      if (panel && !panel.classList.contains('hidden')) {
        if (!panel.contains(e.target) && !this.attachButton?.contains(e.target)) {
          this.hideFileBrowser();
        }
      }
    });
  }

  async toggleFileBrowser() {
    if (!this.fileBrowserPanel) return;
    const isHidden = this.fileBrowserPanel.classList.contains('hidden');
    if (isHidden) {
      await this.loadFileBrowser();
      this.fileBrowserPanel.classList.remove('hidden');
    } else {
      this.hideFileBrowser();
    }
  }

  hideFileBrowser() {
    if (this.fileBrowserPanel) this.fileBrowserPanel.classList.add('hidden');
  }

  async loadFileBrowser() {
    if (!this.fileBrowserList) return;
    this.fileBrowserList.innerHTML = '<div class="file-browser-loading">Loading...</div>';

    try {
      const response = await fetch('/api/uploaded-files');
      if (!response.ok) throw new Error('Failed to load files');
      const data = await response.json();
      const files = data.files || [];

      if (files.length === 0) {
        this.fileBrowserList.innerHTML = '<div class="file-browser-empty">No files uploaded yet</div>';
        return;
      }

      // Group by folder
      const grouped = {};
      for (const f of files) {
        if (!grouped[f.folder]) grouped[f.folder] = [];
        grouped[f.folder].push(f);
      }

      let html = '';
      for (const [folder, folderFiles] of Object.entries(grouped)) {
        const label = folder.includes('images') ? 'Images' : 'Imports';
        html += `<div class="file-browser-group-label">${label}</div>`;
        for (const f of folderFiles) {
          const sizeStr = this.formatFileSize(f.size);
          const isImage = folder.includes('images');
          const icon = isImage ? 'fa-file-image' : 'fa-file';
          // Check if already attached
          const alreadyAttached = this.attachments.some(a => a.path === f.path);
          html += `
            <div class="file-browser-item ${alreadyAttached ? 'file-browser-item--attached' : ''}" data-path="${f.path}" data-filename="${f.filename}" data-size="${f.size}" data-folder="${f.folder}">
              <i class="fa-solid ${icon}"></i>
              <span class="file-browser-item-name" title="${f.path}">${f.filename}</span>
              <span class="file-browser-item-size">${sizeStr}</span>
              <button class="file-browser-attach-btn" title="${alreadyAttached ? 'Already attached' : 'Attach to message'}">
                ${alreadyAttached ? '<i class="fa-solid fa-check"></i>' : '<i class="fa-solid fa-plus"></i>'}
              </button>
            </div>
          `;
        }
      }

      this.fileBrowserList.innerHTML = html;

      // Add click handlers
      this.fileBrowserList.querySelectorAll('.file-browser-item:not(.file-browser-item--attached)').forEach(item => {
        item.querySelector('.file-browser-attach-btn').addEventListener('click', (e) => {
          e.stopPropagation();
          this.attachUploadedFile(item.dataset);
          // Mark as attached
          item.classList.add('file-browser-item--attached');
          const btn = item.querySelector('.file-browser-attach-btn');
          btn.innerHTML = '<i class="fa-solid fa-check"></i>';
          btn.title = 'Already attached';
        });
      });
    } catch (err) {
      console.error('Failed to load uploaded files:', err);
      this.fileBrowserList.innerHTML = '<div class="file-browser-empty">Failed to load files</div>';
    }
  }

  attachUploadedFile(dataset) {
    const { path, filename, size, folder } = dataset;
    const isImage = folder.includes('images');
    const ext = filename.split('.').pop().toLowerCase();
    this.attachments.push({
      type: 'uploaded_file',
      filename,
      path,
      mime_type: isImage ? `image/${ext === 'jpg' ? 'jpeg' : ext}` : `application/${ext}`,
      size: parseInt(size, 10),
    });
    this.renderPreview();
  }

  /**
   * Upload files to the Rails assets/imported folder via API
   * @param {FileList} fileList - Files to upload
   */
  async handleUploadToAssets(fileList) {
    const files = Array.from(fileList);
    const results = [];

    for (const file of files) {
      try {
        const formData = new FormData();
        formData.append('file', file);

        const response = await fetch('/api/upload-to-assets', {
          method: 'POST',
          body: formData,
        });

        if (!response.ok) {
          const err = await response.json().catch(() => ({ detail: response.statusText }));
          throw new Error(err.detail || 'Upload failed');
        }

        const result = await response.json();
        results.push(result);
      } catch (err) {
        console.error(`Failed to upload ${file.name}:`, err);
        alert(`Failed to upload ${file.name}: ${err.message}`);
      }
    }

    if (results.length > 0) {
      // Auto-attach uploaded files as references (no base64, just path metadata)
      for (const result of results) {
        const ext = result.filename.split('.').pop().toLowerCase();
        const isImage = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(ext);
        this.attachments.push({
          type: 'uploaded_file',
          filename: result.filename,
          path: result.path,
          mime_type: isImage ? `image/${ext === 'jpg' ? 'jpeg' : ext}` : `application/${ext}`,
          size: result.size,
        });
      }
      this.renderPreview();
      this.showUploadSuccess(results);
    }
  }

  /**
   * Show a temporary success notification for uploaded files
   */
  showUploadSuccess(results) {
    const notification = document.createElement('div');
    notification.className = 'upload-success-notification';
    notification.innerHTML = `
      <i class="fa-solid fa-check-circle"></i>
      <span>Uploaded to assets: ${results.map(r => r.filename).join(', ')}</span>
    `;
    document.body.appendChild(notification);
    setTimeout(() => notification.remove(), 4000);
  }

  /**
   * Setup paste functionality for image attachments
   * @param {HTMLElement} inputElement - The input/textarea element to listen for paste events
   */
  setupPaste(inputElement) {
    if (!inputElement) {
      console.warn('FileAttachmentManager: Missing input element for paste');
      return;
    }

    inputElement.addEventListener('paste', async (e) => {
      const clipboardData = e.clipboardData;
      if (!clipboardData) return;

      const items = clipboardData.items;
      const imageFiles = [];

      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        // Check if the item is an image
        if (item.type.startsWith('image/')) {
          const file = item.getAsFile();
          if (file) {
            imageFiles.push(file);
          }
        }
      }

      // If we found images, handle them as attachments
      if (imageFiles.length > 0) {
        // Don't prevent default if there's also text - let text paste through
        // But do handle the images
        await this.handleFileSelect(imageFiles);
      }
    });
  }

  /**
   * Setup drag and drop functionality for file attachments
   * @param {HTMLElement} container - The container element to listen for drag events
   * @param {HTMLElement} overlay - The overlay element to show/hide during drag
   */
  setupDragAndDrop(container, overlay) {
    if (!container || !overlay) {
      console.warn('FileAttachmentManager: Missing drag-drop elements');
      return;
    }

    this.dropContainer = container;
    this.dropOverlay = overlay;
    this.dragCounter = 0;

    // Prevent default drag behaviors on document to avoid browser opening files
    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
      container.addEventListener(eventName, (e) => {
        e.preventDefault();
        e.stopPropagation();
      });
    });

    // Show overlay on drag enter
    container.addEventListener('dragenter', (e) => {
      this.dragCounter++;
      if (e.dataTransfer.types.includes('Files')) {
        this.dropOverlay.classList.remove('hidden');
      }
    });

    // Hide overlay on drag leave (only when actually leaving the container)
    container.addEventListener('dragleave', () => {
      this.dragCounter--;
      if (this.dragCounter === 0) {
        this.dropOverlay.classList.add('hidden');
      }
    });

    // Handle drop
    container.addEventListener('drop', (e) => {
      this.dragCounter = 0;
      this.dropOverlay.classList.add('hidden');

      const files = e.dataTransfer.files;
      if (files.length > 0) {
        this.handleFileSelect(files);
      }
    });
  }

  /**
   * Handle file selection from input or drag-drop
   * @param {FileList} fileList - The files to process
   */
  async handleFileSelect(fileList) {
    const files = Array.from(fileList);

    for (const file of files) {
      // Validate file type
      if (!ALLOWED_TYPES[file.type]) {
        console.warn(`File type not allowed: ${file.type}`);
        alert(`File type not supported: ${file.name}\nAllowed: PDF, PNG, JPEG, GIF, WebP, WebM, MP4`);
        continue;
      }

      // Validate file size
      if (file.size > MAX_FILE_SIZE_BYTES) {
        console.warn(`File too large: ${file.name} (${(file.size / 1024 / 1024).toFixed(1)}MB)`);
        alert(`File too large: ${file.name}\nMax size: ${MAX_FILE_SIZE_MB}MB`);
        continue;
      }

      try {
        const encoded = await this.encodeFile(file);
        this.attachments.push({
          filename: file.name,
          mime_type: file.type,
          data: encoded,
          size: file.size,
        });
      } catch (err) {
        console.error(`Failed to encode file: ${file.name}`, err);
        alert(`Failed to process file: ${file.name}`);
      }
    }

    // Clear the input so the same file can be selected again
    this.fileInput.value = '';

    // Update the preview
    this.renderPreview();
  }

  /**
   * Encode a file to base64
   * @param {File} file - The file to encode
   * @returns {Promise<string>} - Base64 encoded string (without data URI prefix)
   */
  encodeFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();

      reader.onload = () => {
        // FileReader returns data:mime;base64,XXXX - we only want the XXXX part
        const result = reader.result;
        const base64 = result.split(',')[1];
        resolve(base64);
      };

      reader.onerror = () => {
        reject(reader.error);
      };

      reader.readAsDataURL(file);
    });
  }

  /**
   * Get all current attachments
   * @returns {Array} - Array of attachment objects with filename, mime_type, data, and size
   */
  getAttachments() {
    return this.attachments.map(({ type, filename, mime_type, data, size, path }) => {
      if (type === 'uploaded_file') {
        return { type: 'uploaded_file', filename, mime_type, path, size };
      }
      return { filename, mime_type, data, size };
    });
  }

  /**
   * Check if there are any attachments
   * @returns {boolean}
   */
  hasAttachments() {
    return this.attachments.length > 0;
  }

  /**
   * Clear all attachments
   */
  clearAttachments() {
    this.attachments = [];
    this.renderPreview();
  }

  /**
   * Remove a specific attachment by index
   * @param {number} index - Index of attachment to remove
   */
  removeAttachment(index) {
    this.attachments.splice(index, 1);
    this.renderPreview();
  }

  /**
   * Render the attachment preview badges
   */
  renderPreview() {
    if (!this.previewContainer) return;

    if (this.attachments.length === 0) {
      this.previewContainer.classList.add('hidden');
      this.previewContainer.innerHTML = '';
      return;
    }

    this.previewContainer.classList.remove('hidden');

    const badges = this.attachments.map((attachment, index) => {
      const typeInfo = ALLOWED_TYPES[attachment.mime_type] || { icon: 'fa-file' };
      const sizeStr = this.formatFileSize(attachment.size);
      const isUploaded = attachment.type === 'uploaded_file';

      return `
        <div class="attachment-badge ${isUploaded ? 'attachment-badge--uploaded' : ''}" data-index="${index}">
          <i class="fa-solid ${isUploaded ? 'fa-cloud-arrow-up' : typeInfo.icon}"></i>
          <span class="attachment-name" title="${isUploaded ? attachment.path : attachment.filename}">${this.truncateFilename(attachment.filename)}</span>
          <span class="attachment-size">(${sizeStr})</span>
          <button class="attachment-remove" data-index="${index}" title="Remove">
            <i class="fa-solid fa-xmark"></i>
          </button>
        </div>
      `;
    }).join('');

    this.previewContainer.innerHTML = badges;

    // Add remove button handlers
    this.previewContainer.querySelectorAll('.attachment-remove').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        const index = parseInt(btn.dataset.index, 10);
        this.removeAttachment(index);
      });
    });
  }

  /**
   * Format file size for display
   * @param {number} bytes - File size in bytes
   * @returns {string} - Formatted size string
   */
  formatFileSize(bytes) {
    if (bytes < 1024) return `${bytes}B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
  }

  /**
   * Truncate filename for display
   * @param {string} filename - Original filename
   * @param {number} maxLength - Maximum length
   * @returns {string} - Truncated filename
   */
  truncateFilename(filename, maxLength = 20) {
    if (filename.length <= maxLength) return filename;

    const ext = filename.split('.').pop();
    const name = filename.slice(0, -(ext.length + 1));
    const truncatedName = name.slice(0, maxLength - ext.length - 4) + '...';
    return `${truncatedName}.${ext}`;
  }
}

export default FileAttachmentManager;
