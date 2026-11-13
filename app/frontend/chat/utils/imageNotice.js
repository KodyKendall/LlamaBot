/**
 * LP-44: one rule for "is this an image?" across both attach paths, and the
 * notice shown when the selected model can't see images.
 *
 * The paperclip sends the picture's data; the Uploaded files browser sends only a
 * path (the server reads the file). Both are images to the user, so both count.
 * There is no automatic model switch any more: the user is told which models on
 * this box can see images and switches themselves.
 */

const IMAGE_EXTENSION = /\.(png|jpe?g|gif|webp)$/i;

export function isImageAttachment(attachment) {
  if (!attachment) return false;
  if ((attachment.mime_type || '').startsWith('image/')) return true;
  return attachment.type === 'uploaded_file' && IMAGE_EXTENSION.test(attachment.path || attachment.filename || '');
}

export function hasAnyImage(attachments) {
  return (attachments || []).some(isImageAttachment);
}

/** Labels of the enabled models in the picker that can see images. */
export function visionCapableModels(options, supportsImages) {
  return Array.from(options || [])
    .filter((o) => o.value && !o.disabled && supportsImages(o.value))
    .map((o) => o.dataset?.shortLabel || (o.textContent || '').trim() || o.value);
}

export function cannotSeeImagesNotice(modelLabel, capableLabels) {
  const head = `${modelLabel} can't see images, so Leo won't see the picture you attached.`;
  if (!capableLabels || capableLabels.length === 0) {
    return `${head} No model on this workspace can see images right now.`;
  }
  return `${head} Models here that can: ${capableLabels.join(', ')}. Switch models to have Leo look at it.`;
}
