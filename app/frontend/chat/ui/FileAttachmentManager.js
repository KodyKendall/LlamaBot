/**
 * FileAttachmentManager - Handles file attachments for chat messages
 *
 * Supports PDF and image files, encoding them as base64 for transmission
 * over WebSocket to LangChain agents.
 */

const MAX_FILE_SIZE_MB = 25;
const MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024;

const ALLOWED_TYPES = {
  'application/pdf': { ext: 'pdf', icon: 'fa-file-pdf' },
  'image/png': { ext: 'png', icon: 'fa-file-image' },
  'image/jpeg': { ext: 'jpg', icon: 'fa-file-image' },
  'image/gif': { ext: 'gif', icon: 'fa-file-image' },
  'image/webp': { ext: 'webp', icon: 'fa-file-image' },
};

export class FileAttachmentManager {
  constructor() {
    this.attachments = [];
    this.fileInput = null;
    this.attachButton = null;
    this.previewContainer = null;
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

    // Click button -> trigger file input
    this.attachButton.addEventListener('click', () => {
      this.fileInput.click();
    });

    // Handle file selection
    this.fileInput.addEventListener('change', (e) => {
      this.handleFileSelect(e);
    });
  }

  /**
   * Handle file selection from the input
   * @param {Event} event - The change event from file input
   */
  async handleFileSelect(event) {
    const files = Array.from(event.target.files);

    for (const file of files) {
      // Validate file type
      if (!ALLOWED_TYPES[file.type]) {
        console.warn(`File type not allowed: ${file.type}`);
        alert(`File type not supported: ${file.name}\nAllowed: PDF, PNG, JPEG, GIF, WebP`);
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
   * @returns {Array} - Array of attachment objects with filename, mime_type, and data
   */
  getAttachments() {
    // Return only the fields needed for transmission (exclude size)
    return this.attachments.map(({ filename, mime_type, data }) => ({
      filename,
      mime_type,
      data,
    }));
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

      return `
        <div class="attachment-badge" data-index="${index}">
          <i class="fa-solid ${typeInfo.icon}"></i>
          <span class="attachment-name" title="${attachment.filename}">${this.truncateFilename(attachment.filename)}</span>
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
